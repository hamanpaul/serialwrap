"""P2 bridge 支援的 Linux L1.5 黑箱測試工具。"""
from __future__ import annotations

import atexit
import base64
import hashlib
import json
import os
import re
import select
import shlex
import signal
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bb_harness import ab_env, repo_root

ROOT = repo_root()
BRIDGE_SRC = ROOT / "tools" / "uart_stdio_bridge" / "src"
if str(BRIDGE_SRC) not in sys.path:
    sys.path.insert(0, str(BRIDGE_SRC))

_bundle_work: tempfile.TemporaryDirectory[str] | None = None
_bundle_pair: tuple[Path, Path] | None = None


def _cleanup_bundles() -> None:
    global _bundle_work
    if _bundle_work is not None:
        _bundle_work.cleanup()
        _bundle_work = None


atexit.register(_cleanup_bundles)


def bundle_manifest_path(bundle: Path, expected_version: str | None = None) -> Path:
    """依實際 bundle 佈局定位 manifest，優先遵循 current-release 的版本參照。"""
    bundle = Path(bundle).resolve()
    current_path = bundle / "bin" / "current-release.json"
    version = expected_version
    current_version: str | None = None
    if current_path.is_file():
        current = json.loads(current_path.read_text(encoding="utf-8"))
        current_version = current.get("bundle_version")
        if not isinstance(current_version, str) or not current_version:
            raise AssertionError(f"current-release.json 缺少 bundle_version：{current_path}")
        if expected_version is not None and current_version != expected_version:
            raise AssertionError(
                f"current-release 版本應為 {expected_version}，實際為 {current_version}：{current_path}"
            )
        version = current_version

    if version is not None:
        version_manifest = bundle / "versions" / version / "release-manifest.json"
        if version_manifest.is_file():
            return version_manifest
        legacy_manifest = bundle / "release-manifest.json"
        if not current_path.is_file() and legacy_manifest.is_file():
            return legacy_manifest
        raise AssertionError(f"bundle 缺少版本 manifest：{version_manifest}")

    legacy_manifest = bundle / "release-manifest.json"
    if legacy_manifest.is_file():
        return legacy_manifest
    version_manifests = sorted((bundle / "versions").glob("*/release-manifest.json"))
    if len(version_manifests) == 1:
        return version_manifests[0]
    raise AssertionError(f"無法唯一定位 bundle manifest：{bundle}")


def update_bundles() -> tuple[Path, Path]:
    """建立本 pytest 行程共用的 0.4.0／0.4.1 test-hooks bundle。"""
    global _bundle_work, _bundle_pair
    if _bundle_pair is not None:
        return _bundle_pair
    external_old = os.environ.get("SW_G5_BUNDLE_OLD")
    external_new = os.environ.get("SW_G5_BUNDLE_NEW")
    if external_old or external_new:
        if not external_old or not external_new:
            raise AssertionError("SW_G5_BUNDLE_OLD 與 SW_G5_BUNDLE_NEW 必須同時設定")
        bundles = (Path(external_old).resolve(), Path(external_new).resolve())
        repo = ROOT.resolve()
        for bundle, expected in zip(bundles, ("0.4.0", "0.4.1"), strict=True):
            if repo not in bundle.parents:
                raise AssertionError(f"外部 G5 bundle 必須位於本 worktree：{bundle}")
            cli = bundle / "bin" / "serialwrap"
            if not cli.is_file() or not os.access(cli, os.X_OK):
                raise AssertionError(f"G5 bundle 缺少可執行 CLI：{cli}")
            manifest_path = bundle_manifest_path(bundle, expected)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest.get("bundle_version") != expected:
                raise AssertionError(f"G5 bundle 版本應為 {expected}：{manifest}")
        _bundle_pair = bundles
        return _bundle_pair

    temp_root = ROOT / "target" / "g5-tmp"
    temp_root.mkdir(parents=True, exist_ok=True)
    _bundle_work = tempfile.TemporaryDirectory(prefix="serialwrap-g5-bundles-", dir=temp_root)
    root = Path(_bundle_work.name)
    cargo_target = root / "cargo-target"
    script = ROOT / "scripts" / "rust" / "mkbundle.sh"
    outputs: list[Path] = []
    for version in ("0.4.0", "0.4.1"):
        out = root / f"bundle-{version}"
        env = dict(os.environ)
        env["CARGO_TARGET_DIR"] = str(cargo_target)
        result = subprocess.run(
            ["bash", str(script), "--dev", "--version-override", version, "--out", str(out)],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=900,
            check=False,
        )
        assert result.returncode == 0, f"建置 {version} dev bundle 失敗：\n{result.stdout}\n{result.stderr}"
        cli = out / "bin" / "serialwrap"
        assert cli.is_file() and os.access(cli, os.X_OK), f"bundle 缺少 CLI：{cli}"
        manifest_path = bundle_manifest_path(out, version)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert manifest["bundle_version"] == version
        outputs.append(out)
    _bundle_pair = (outputs[0], outputs[1])
    return _bundle_pair


def json_stdout(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    for line in reversed(result.stdout.splitlines()):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise AssertionError(f"CLI stdout 不含 JSON 物件：{result.stdout!r} {result.stderr!r}")


def run_cli(
    cli: Path,
    args: list[str],
    env: dict[str, str],
    *,
    timeout_s: float = 90.0,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(cli), *args],
        env=env,
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=timeout_s,
        check=False,
    )


# ── PID namespace 隔離（pidns_rig）──────────────────────────────────────────
# 宿主上的生產 serialwrapd（pipx／systemd）與容器內 daemon 會被 CLI 的 /proc 多開偵測
# （matching_daemons，fail-closed）看見，使 rig 誤判 DEVICE_STILL_HELD／DAEMON_ALREADY_RUNNING_ELSEWHERE。
# 因此凡「自啟 daemon 再呼叫 CLI daemon start／restart／update／supervision」的 Linux rig，整段
# （bridge、daemon、CLI）於獨立 user+pid+mount namespace 內重跑；生產行程在 namespace 內不可見，
# 偵測語意本身不改（仍 fail-closed）。
PIDNS_OUTER_ENV = "SW_PIDNS_OUTER"
PIDNS_CHILD_TIMEOUT_S = 1500.0
_UNSHARE_PIDNS_ARGV = (
    "unshare", "--user", "--map-current-user", "--pid", "--fork", "--mount", "--mount-proc",
)


def current_pidns_link() -> str | None:
    """本行程所屬 pid namespace 的連結字串（`pid:[inode]`）；無法讀取回 None。"""
    try:
        return os.readlink("/proc/self/ns/pid")
    except OSError:
        return None


def in_isolated_pidns() -> bool:
    """行程是否位於外層 wrapper（`SW_PIDNS_OUTER` 記錄其 pid namespace）建立的不同 namespace 內。"""
    outer = os.environ.get(PIDNS_OUTER_ENV)
    current = current_pidns_link()
    return bool(outer) and current is not None and current != outer


def unshare_pidns_argv() -> list[str] | None:
    """可用時回傳非特權 user+pid+mount namespace 的 argv 前綴；不可用回 None（呼叫端 skip）。"""
    if shutil.which(_UNSHARE_PIDNS_ARGV[0]) is None:
        return None
    argv = list(_UNSHARE_PIDNS_ARGV)
    try:
        probe = subprocess.run(argv + ["true"], capture_output=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return argv if probe.returncode == 0 else None


def record_wrapper_pid(proc_pid: int, nodeid: str, outer_ns: str | None = None) -> None:
    """將外層 unshare wrapper 的 host pid 寫入 SW_AB_PIDLOG（未設定即略過）。

    子 pytest 內的 daemon 列只能記 namespace 內部 pid；wrapper 行程位於外層（host）namespace，釋放檢查
    以 host /proc 核對其已被回收（scripts/rust/ab_blackbox_1b.py `_release_check`）。
    """
    pidlog = os.environ.get("SW_AB_PIDLOG")
    if not pidlog:
        return
    row = {
        "pid": int(proc_pid),
        "children": [],
        "socket": None,
        "lock": None,
        "run_dir": None,
        "pid_ns": outer_ns if outer_ns is not None else current_pidns_link(),
        "wrapper": "unshare",
        "nodeid": nodeid,
    }
    with open(pidlog, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, separators=(",", ":")) + "\n")


def run_nodeid_in_pidns(nodeid: str, rootpath: Path) -> tuple[str, str]:
    """於新 pid namespace 內重跑單一 pytest nodeid；回傳 (outcome, message)。

    outcome：``passed``／``skipped``／``failed``，或能力不足時 ``unavailable``（呼叫端 skip，絕不視為 PASS）。
    子行程以 junit 回報結果，避免 skip 被誤判為通過；SW_RESOURCE_REPORT 不傳入子行程，不覆寫外層 L1 報告。
    """
    import xml.etree.ElementTree as ElementTree

    prefix = unshare_pidns_argv()
    if prefix is None:
        return "unavailable", "此宿主無法以非特權 unshare 建立 user+pid+mount namespace（環境能力不足，非 PASS）"
    outer = current_pidns_link()
    if outer is None:
        return "unavailable", "無法讀取 /proc/self/ns/pid（環境能力不足，非 PASS）"
    env = dict(os.environ)
    env.pop("SW_RESOURCE_REPORT", None)
    # 根 conftest 於 import 時自 os.environ 移除 A/B 選擇器（已由 ab_env 模組擷取）；子行程須還原同一組選擇器，
    # 否則 rust-only 案例會在子行程被誤判為 Python 基線而 skip。
    env.update(ab_env())
    env[PIDNS_OUTER_ENV] = outer
    work = Path(tempfile.mkdtemp(prefix="sw-pidns-"))
    try:
        junit = work / "child.xml"
        cmd = prefix + [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", f"--junitxml={junit}", nodeid]
        # namespace 內的 init（即子 pytest）忽略未註冊的 SIGTERM，且 unshare 本身被殺時 init 會殘留；
        # 故以獨立 session 啟動，逾時時對整個行程群組 SIGKILL，確保 namespace 內的 daemon／bridge 一併回收。
        out_file = work / "child.out"
        with out_file.open("wb") as out:
            proc = subprocess.Popen(
                cmd, cwd=str(rootpath), env=env, stdin=subprocess.DEVNULL,
                stdout=out, stderr=subprocess.STDOUT, start_new_session=True,
            )
            record_wrapper_pid(proc.pid, nodeid, outer)
            try:
                returncode = proc.wait(timeout=PIDNS_CHILD_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait(timeout=30)
                return "failed", f"namespace 子行程逾時（{PIDNS_CHILD_TIMEOUT_S:.0f} s）：{nodeid}"
        text = out_file.read_text(encoding="utf-8", errors="replace")
        tail = text[-4000:]
        del returncode
        if not junit.is_file():
            return "failed", f"namespace 子行程未產生 junit（rc={proc.returncode}）：\n{tail}"
        root = ElementTree.parse(junit).getroot()
        cases = list(root.iter("testcase"))
        if len(cases) != 1:
            return "failed", f"namespace 子行程應恰收集 1 個案例，實際 {len(cases)}：{nodeid}\n{tail}"
        case = cases[0]
        for kind in ("failure", "error"):
            node = case.find(kind)
            if node is not None:
                return "failed", f"namespace 子行程失敗（{kind}）：\n{(node.get('message') or '')}\n{tail}"
        skipped = case.find("skipped")
        if skipped is not None:
            return "skipped", f"namespace 子行程 skip：{skipped.get('message') or ''}"
        return "passed", ""
    except subprocess.TimeoutExpired:
        return "failed", f"namespace 子行程逾時（{PIDNS_CHILD_TIMEOUT_S:.0f} s）：{nodeid}"
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _profile_yaml() -> str:
    return """profiles:
  l15-shell:
    platform: prpl
    prompt_regex: '(?m)^root@fake:~# '
    ready_probe: 'echo __READY__${nonce}'
    timeout_s: 3
    quiet_window_s: 0.05
    hard_timeout_s: 20
targets:
  - com: COM0
    profile: l15-shell
    device_by_id: usb-FAKE_TARGET_0-if00
"""


def prepare_profile(env: dict[str, str]) -> Path:
    profile_dir = Path(env["SERIALWRAP_CONFIG_DIR"]) / "profiles"
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "l15.yaml").write_text(_profile_yaml(), encoding="utf-8")
    config = Path(env["SERIALWRAP_CONFIG_DIR"]) / "config.yaml"
    if not config.exists():
        config.write_text("supervision_mode: on-demand\n", encoding="utf-8")
    return profile_dir


def start_python_daemon(env: dict[str, str], profile_dir: Path, *, state: str = "READY"):
    from bb_daemon import PyDaemon

    run_dir = ROOT / "target" / f"g5r-{os.getpid() % 100000:05d}-{time.time_ns() % 1000000:06d}"
    run_dir.mkdir(parents=True, exist_ok=False)
    env["SERIALWRAP_RUN_DIR"] = str(run_dir)
    env["SW_G5_TEMP_RUN_DIR"] = str(run_dir)
    daemon = PyDaemon(
        env,
        profile_dir=profile_dir,
        socket=str(run_dir / "serialwrapd.sock"),
        lock=run_dir / "serialwrapd.lock",
    )
    try:
        daemon.start(timeout_s=20.0)
        wait_session(env, state=state)
    except BaseException as exc:
        try:
            stop_owned_python_daemon(daemon, env)
        except BaseException as cleanup_error:
            exc.add_note(f"隔離 Python daemon 清理失敗：{type(cleanup_error).__name__}: {cleanup_error}")
        raise
    return daemon


def endpoint(env: dict[str, str]) -> str:
    return str(Path(env["SERIALWRAP_RUN_DIR"]) / "serialwrapd.sock")


def rpc(env: dict[str, str], method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    from bb_daemon import _send_rpc

    response = _send_rpc(endpoint(env), method, params or {}, timeout_s=5.0)
    assert response is not None, f"{method} 沒有 RPC 回應"
    return response


def wait_session(env: dict[str, str], state: str = "READY", timeout_s: float = 20.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    last: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        response = rpc(env, "session.list")
        last = next((row for row in response.get("sessions", []) if row.get("com") == "COM0"), None)
        if last is not None and last.get("state") == state:
            return last
        time.sleep(0.05)
    raise AssertionError(f"COM0 未到 {state}：{last}")


def daemon_pid(env: dict[str, str]) -> int:
    try:
        status = rpc(env, "health.status")
    except AssertionError:
        status = {}
    pid = status.get("pid")
    assert status.get("ok") is True and isinstance(pid, int) and pid > 0, status
    return pid


def stop_current_daemon(cli: Path, env: dict[str, str], expected_pid: int | None = None) -> None:
    """只停止本測試隔離 endpoint 上且 PID 符合預期的 daemon。"""
    status = rpc(env, "health.status")
    pid = status.get("pid")
    if not status.get("ok"):
        return
    assert isinstance(pid, int) and pid > 0, status
    if expected_pid is not None:
        assert pid == expected_pid, f"隔離 endpoint PID 改變：expected={expected_pid}, actual={pid}"
    stopped = run_cli(cli, ["daemon", "stop"], env, timeout_s=30.0)
    assert stopped.returncode == 0, stopped.stdout + stopped.stderr
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        from bb_daemon import _send_rpc

        if _send_rpc(endpoint(env), "health.ping", {}, timeout_s=0.2) is None:
            return
        time.sleep(0.05)
    raise AssertionError(f"daemon stop 後 endpoint 仍回應：pid={pid}")


def stop_owned_python_daemon(daemon: Any, env: dict[str, str]) -> None:
    """僅在 RPC 回報的 PID 仍是此 PyDaemon 子行程時送 stop，避免誤停後繼 daemon。"""
    proc = daemon.proc
    if proc is None:
        _cleanup_short_run_dir(env)
        return
    if proc.poll() is not None:
        proc.wait(timeout=0)
        daemon.proc = None
        _cleanup_short_run_dir(env)
        return
    try:
        status = rpc(env, "health.status")
    except AssertionError:
        status = {}
    if status.get("pid") == proc.pid:
        daemon.stop()
    else:
        try:
            proc.wait(timeout=10.0)
        except subprocess.TimeoutExpired:
            # endpoint 已不屬於本行程時不可對它送 daemon.stop；只清理 PyDaemon
            # 自己建立的 process group，避免失敗路徑留下隔離 daemon。
            if os.name == "posix":
                try:
                        os.killpg(proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            else:
                proc.terminate()
            try:
                proc.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                if os.name == "posix":
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                else:
                    proc.kill()
                proc.wait(timeout=3.0)
        daemon.proc = None
    _cleanup_short_run_dir(env)


def _cleanup_short_run_dir(env: dict[str, str]) -> None:
    path = env.pop("SW_G5_TEMP_RUN_DIR", None)
    if path:
        shutil.rmtree(path, ignore_errors=True)


def make_install_root(tmp_path: Path, env: dict[str, str]) -> Path:
    root = tmp_path / "install-root"
    env["SERIALWRAP_INSTALL_ROOT"] = str(root)
    return root


def select_bundle(env: dict[str, str], bundle: Path) -> Path:
    env["SERIALWRAP_RELEASE_MANIFEST"] = str(bundle_manifest_path(bundle))
    return bundle / "bin" / "serialwrap"


def current_release_path(install_root: Path) -> Path:
    """安裝根的 current-release.json：POSIX 位於安裝根下，Windows 位於 bin\\（與 install_layout.rs 一致）。"""
    return install_root / "bin" / "current-release.json" if os.name == "nt" else install_root / "current-release.json"


def installed_version(env: dict[str, str]) -> str:
    current = json.loads(
        current_release_path(Path(env["SERIALWRAP_INSTALL_ROOT"])).read_text(encoding="utf-8")
    )
    version = current.get("bundle_version")
    assert isinstance(version, str) and version
    return version


def make_push_file(path: Path, size: int = 1024 * 1024) -> tuple[str, str]:
    """建立可重現的大檔案，回傳 MD5 與遠端路徑。"""
    block = bytes(range(256))
    path.write_bytes((block * ((size + len(block) - 1) // len(block)))[:size])
    return hashlib.md5(path.read_bytes()).hexdigest(), f"/tmp/{path.name}"


def wire_payloads(evidence_dir: Path, direction: str) -> list[bytes]:
    path = evidence_dir / "wire.ndjson"
    if not path.is_file():
        return []
    result: list[bytes] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        if row.get("dir") == direction:
            result.append(base64.b64decode(row["payload_b64"], validate=True))
    return result


def tx_payloads(evidence_dir: Path) -> list[bytes]:
    return wire_payloads(evidence_dir, "TX")


def tx_bytes(evidence_dir: Path) -> bytes:
    return b"".join(tx_payloads(evidence_dir))


def rx_bytes(evidence_dir: Path) -> bytes:
    return b"".join(wire_payloads(evidence_dir, "RX"))


def wal_rows(env: dict[str, str]) -> list[dict[str, Any]]:
    path = Path(env["SERIALWRAP_WAL_DIR"]) / "raw.wal.ndjson"
    if not path.is_file():
        return []
    return [
        json.loads(line.rstrip("\r\n"))
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _read_ready(proc: subprocess.Popen[str], timeout_s: float = 20.0) -> dict[str, Any]:
    assert proc.stdout is not None
    ready: list[dict[str, Any]] = []
    failure: list[BaseException] = []

    def read_line() -> None:
        try:
            line = proc.stdout.readline()
            if not line:
                raise RuntimeError("P2 bridge 未輸出 BRIDGE_READY")
            ready.append(json.loads(line))
        except BaseException as exc:
            failure.append(exc)

    thread = threading.Thread(target=read_line, daemon=True)
    thread.start()
    thread.join(timeout_s)
    if thread.is_alive():
        raise TimeoutError("P2 bridge 等待 BRIDGE_READY 逾時")
    if failure:
        raise AssertionError(f"P2 bridge 未就緒：{failure[0]}") from failure[0]
    payload = ready[0]
    assert payload.get("event") == "BRIDGE_READY" and payload.get("slave_path")
    return payload


@dataclass
class BridgeTarget:
    root: Path
    evidence_dir: Path
    process: subprocess.Popen[str]
    control: Any
    slave_path: str
    remote_root: Path
    stderr_file: Any

    @classmethod
    def start(cls, root: Path, env: dict[str, str], *, delay_ms: int = 1) -> "BridgeTarget":
        from uart_stdio_bridge.bridge.control import ControlClient

        root = Path(root)
        root.mkdir(parents=True, exist_ok=True)
        evidence = root / "bridge-evidence"
        link = Path(env["SERIALWRAP_BY_ID_DIR"]) / "usb-FAKE_TARGET_0-if00"
        dut_argv = [sys.executable, str(Path(__file__).resolve()), "--dut", str(root / "remote-files"), str(delay_ms)]
        bridge_env = dict(env)
        existing = bridge_env.get("PYTHONPATH", "")
        bridge_env["PYTHONPATH"] = os.pathsep.join([str(BRIDGE_SRC), str(ROOT), existing]).rstrip(os.pathsep)
        stderr_file = open(root / "bridge.stderr", "ab")
        command = [
            sys.executable, "-m", "uart_stdio_bridge", "run",
            "--run-id", f"g5-{os.getpid()}-{time.time_ns()}",
            "--case-id", "g5-l15-blackbox",
            "--port-id", "COM0",
            "--evidence-dir", str(evidence),
            "--endpoint", "posix-pty",
            "--by-id-link", str(link),
            "--dut-mode", "local-process",
            "--dut-argv-json", json.dumps(dut_argv),
        ]
        proc = subprocess.Popen(
            command,
            cwd=ROOT,
            env=bridge_env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=stderr_file,
            text=True,
            bufsize=1,
            start_new_session=(os.name == "posix"),
        )
        try:
            ready = _read_ready(proc)
            control = ControlClient(Path(ready["control_info"]), timeout_s=30.0)
            return cls(root, evidence, proc, control, str(ready["slave_path"]), root / "remote-files", stderr_file)
        except BaseException:
            if os.name == "posix":
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            else:
                proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                if os.name == "posix":
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                else:
                    proc.kill()
                proc.wait(timeout=5)
            stderr_file.close()
            raise

    def mark(self, label: str, pid: int | str) -> None:
        result = self.control.call("mark", {"label": label, "op_id": str(pid)})
        assert "wire_seq" in result

    def stop(self) -> None:
        """先要求 bridge 正常收尾；失敗時終止本輪 process group，避免 DUT 子行程殘留。"""
        shutdown_error: BaseException | None = None
        try:
            if self.process.poll() is None:
                try:
                    self.control.call("shutdown")
                except BaseException as exc:
                    shutdown_error = exc
                try:
                    self.process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    if os.name == "posix":
                        try:
                            os.killpg(self.process.pid, signal.SIGTERM)
                        except ProcessLookupError:
                            pass
                    else:
                        self.process.terminate()
                    try:
                        self.process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        if os.name == "posix":
                            try:
                                os.killpg(self.process.pid, signal.SIGKILL)
                            except ProcessLookupError:
                                pass
                        else:
                            self.process.kill()
                        self.process.wait(timeout=5)
            if self.process.poll() is None:
                raise RuntimeError(f"P2 bridge process group 仍存活：pid={self.process.pid}")
            if shutdown_error is not None:
                raise RuntimeError(f"P2 bridge 未完成 shutdown RPC：{shutdown_error}") from shutdown_error
            if self.process.returncode != 0:
                raise RuntimeError(f"P2 bridge exit={self.process.returncode}")
        finally:
            if self.process.poll() is None:
                if os.name == "posix":
                    try:
                        os.killpg(self.process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                else:
                    self.process.kill()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
            self.control.close()
            if not self.stderr_file.closed:
                self.stderr_file.close()


def finish_test_rig(bridge: BridgeTarget, daemon: Any, cli: Path, env: dict[str, str]) -> None:
    """即使 endpoint 或 daemon 清理失敗，也繼續回收本測試建立的 daemon 與 P2 DUT。"""
    active_error = sys.exc_info()[1]
    errors: list[BaseException] = []
    try:
        status = rpc(env, "health.status")
    except AssertionError:
        status = {"ok": False}
    except BaseException as exc:
        errors.append(exc)
        status = {"ok": False}
    pid = status.get("pid") if status.get("ok") else None
    if isinstance(pid, int):
        try:
            stop_current_daemon(cli, env, pid)
        except BaseException:
            # CLI stop 失敗時，僅透過已核對的隔離 endpoint 重試，不掃描或選擇其他 daemon。
            try:
                current = rpc(env, "health.status")
                if current.get("ok") is True and current.get("pid") == pid:
                    stopped = rpc(env, "daemon.stop")
                    if stopped.get("ok") is not True:
                        raise RuntimeError(f"隔離 daemon.stop 失敗：{stopped}")
            except BaseException as exc:
                errors.append(exc)
    try:
        stop_owned_python_daemon(daemon, env)
    except BaseException as exc:
        errors.append(exc)
    try:
        bridge.stop()
    except BaseException as exc:
        errors.append(exc)
    if errors and active_error is None:
        raise RuntimeError(
            "隔離 rig 清理失敗：" + "; ".join(f"{type(exc).__name__}: {exc}" for exc in errors)
        ) from errors[0]


def holder_pids(slave_path: str) -> list[int]:
    target = os.path.realpath(slave_path)
    result: list[int] = []
    proc_root = Path("/proc")
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        fd_dir = entry / "fd"
        try:
            held = any(os.path.realpath(os.readlink(fd)) == target for fd in fd_dir.iterdir())
        except (OSError, PermissionError):
            continue
        if held:
            result.append(int(entry.name))
    return sorted(result)


def wait_holder(slave_path: str, pid: int | None = None, *, present: bool, timeout_s: float = 10.0) -> int | None:
    deadline = time.monotonic() + timeout_s
    last: list[int] = []
    while time.monotonic() < deadline:
        last = holder_pids(slave_path)
        if present:
            match = pid if pid is not None and pid in last else (last[0] if pid is None and last else None)
            if match is not None:
                return match
        elif not last or (pid is not None and pid not in last):
            return None
        time.sleep(0.02)
    raise AssertionError(f"tty holder 狀態不符：present={present} pid={pid} holders={last}")


def control_marks(evidence_dir: Path) -> list[dict[str, Any]]:
    path = evidence_dir / "control.ndjson"
    if not path.is_file():
        return []
    return [
        row for row in (json.loads(line) for line in path.read_text(encoding="utf-8").splitlines())
        if row.get("action") == "mark" and row.get("phase") == "completed"
    ]


class RaisingThread(threading.Thread):
    """背景行程包裝：目標函式的例外不會被 threading 默默吞掉，於 `join()` 時原樣重新拋出。

    若目標仍在執行中，`join(timeout)` 只回傳而不拋例外（呼叫端仍可用 `is_alive()` 判斷逾時）。
    """

    def __init__(self, target: Callable[[], None]) -> None:
        super().__init__(daemon=True)
        self._target_fn = target
        self.error: BaseException | None = None

    def run(self) -> None:
        try:
            self._target_fn()
        except BaseException as exc:  # noqa: BLE001 - 於 join 端重新拋出
            self.error = exc

    def join(self, timeout: float | None = None) -> None:
        super().join(timeout)
        if not self.is_alive() and self.error is not None:
            raise self.error


def start_file_push(
    cli: Path,
    env: dict[str, str],
    source: Path,
    remote: str,
    *,
    ack_mode: str = "none",
    chunk_timeout_s: float | None = None,
) -> tuple[RaisingThread, list[subprocess.CompletedProcess[str]]]:
    """背景啟動 file push；`ack_mode="none"` 為預設（吞吐優先，佔住傳輸中的 session），
    需要「每個 chunk 等板端 ACK」（DUT 延遲 ACK 期間 TX 必須靜止）時傳 `ack_mode="auto"`。
    `chunk_timeout_s` 非 None 時以 `--chunk-timeout` 覆寫單一 chunk 等待逾時；未指定則沿用 profile 推導值（下限 5 s）。"""
    result: list[subprocess.CompletedProcess[str]] = []
    # 橋接 DUT 上 echo ACK 約 1.6 KB/s，1 MiB 需約 640 s，超過 180 s 逾時；本 helper 只用來佔住傳輸中的 session，
    # 故預設改 ack-mode none + chunk 4096（實測約 65 KB/s，約 16 s）。逾時與檔案大小不變，斷言由各測試自行判定。
    argv = ["--timeout", "180", "file", "push", "--selector", "COM0", "--local", str(source), "--remote", remote, "--ack-mode", ack_mode, "--chunk-size", "4096"]
    if chunk_timeout_s is not None:
        argv += ["--chunk-timeout", f"{chunk_timeout_s:g}"]
    thread = RaisingThread(
        lambda: result.append(run_cli(cli, argv, env, timeout_s=180.0)),
    )
    thread.start()
    return thread, result


def wait_wire_contains(evidence_dir: Path, needle: bytes, *, timeout_s: float = 10.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if needle in tx_bytes(evidence_dir):
            return
        time.sleep(0.02)
    raise AssertionError(f"P2 wire.ndjson 未觀察到 TX：{needle!r}")


def wait_rx_contains(evidence_dir: Path, needle: bytes, *, timeout_s: float = 10.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if needle in rx_bytes(evidence_dir):
            return
        time.sleep(0.02)
    raise AssertionError(f"P2 wire.ndjson 未觀察到 RX：{needle!r}")


def wait_pause(pause_dir: Path, point: str, *, timeout_s: float = 20.0) -> Path:
    marker = pause_dir / f"{point.replace('/', '_').replace(':', '_')}.reached"
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if marker.is_file():
            assert marker.read_text(encoding="utf-8") == point
            return marker
        time.sleep(0.02)
    raise AssertionError(f"update hook 未抵達 {point}：{marker}")


def resume_pause(pause_dir: Path, point: str) -> None:
    pause_dir.mkdir(parents=True, exist_ok=True)
    (pause_dir / f"{point.replace('/', '_').replace(':', '_')}.resume").write_text("resume\n", encoding="utf-8")


def _parse_shell_arg(value: str) -> str:
    return shlex.split(value)[0]


def _target_command(command: str, files: dict[str, bytes], root: Path) -> tuple[bytes | None, bool]:
    if command == "hold":
        return None, True
    if "SW_XFER_DECODER_B64_" in command:
        return b"SW_XFER_DECODER_B64_42\r\n", False
    if "SW_XFER_ENCODER_B64_" in command:
        return b"SW_XFER_ENCODER_B64_42\r\n", False
    if command.startswith("printf '%s' ") and " | base64 -d " in command:
        head, tail = command.split(" | base64 -d ", 1)
        encoded = _parse_shell_arg(head[len("printf '%s' "):])
        op_and_path = tail.split(" &&", 1)[0]
        if op_and_path.startswith(">> "):
            op, path_text = ">>", op_and_path[3:]
        elif op_and_path.startswith("> "):
            op, path_text = ">", op_and_path[2:]
        else:
            return b"", False
        path = _parse_shell_arg(path_text)
        data = base64.b64decode(encoded, validate=True)
        files[path] = files.get(path, b"") + data if op == ">>" else data
        remote_file = root / Path(path).name
        remote_file.parent.mkdir(parents=True, exist_ok=True)
        if op == ">>":
            with remote_file.open("ab") as stream:
                stream.write(data)
        else:
            remote_file.write_bytes(data)
        return b"SW_XFER_CHUNK_42\r\n", False
    if command.startswith("md5sum "):
        path = _parse_shell_arg(command[len("md5sum "):].split(" &&", 1)[0])
        data = files.get(path, b"")
        digest = hashlib.md5(data).hexdigest().encode("ascii")
        return digest + b"  " + path.encode("utf-8") + b"\r\nSW_XFER_MD5_42\r\n", False
    if command.startswith("mv "):
        args = shlex.split(command.split(" &&", 1)[0])
        if len(args) == 3:
            source, destination = args[1:]
            files[destination] = files.pop(source, b"")
            (root / Path(destination).name).write_bytes(files[destination])
            (root / Path(source).name).unlink(missing_ok=True)
            return b"SW_XFER_MV_42\r\n", False
    if command.startswith("rm -f "):
        path = _parse_shell_arg(command[len("rm -f "):].split(" &&", 1)[0])
        files.pop(path, None)
        (root / Path(path).name).unlink(missing_ok=True)
        return b"SW_XFER_CLEANUP_42\r\n", False
    if command.startswith("echo "):
        return command[5:].encode("utf-8") + b"\r\n", False
    if command == "echo":
        return b"\r\n", False
    return command.encode("utf-8") + b"\r\n", False


def _dut_main(argv: list[str]) -> int:
    root = Path(argv[0])
    delay_ms = int(argv[1])
    root.mkdir(parents=True, exist_ok=True)
    files: dict[str, bytes] = {}
    pending = bytearray()
    holding = False
    previous_cr = False
    os.write(1, b"root@fake:~# ")
    while True:
        chunk = os.read(0, 65536)
        if not chunk:
            return 0
        if b"\x55\x55" in chunk:
            prefix, suffix = chunk.split(b"\x55\x55", 1)
            if prefix:
                chunk = prefix + suffix
            else:
                chunk = suffix
            os.write(1, b"\x00\xcc")
            if not chunk:
                continue
        for value in chunk:
            byte = bytes((value,))
            if holding:
                if byte == b"\x03":
                    holding = False
                    os.write(1, b"^C\r\nroot@fake:~# ")
                continue
            os.write(1, byte)
            if byte == b"\n" and previous_cr:
                previous_cr = False
                continue
            previous_cr = byte == b"\r"
            if byte not in (b"\r", b"\n"):
                pending.extend(byte)
                continue
            command = pending.decode("utf-8", errors="replace")
            pending.clear()
            output, hold = _target_command(command, files, root)
            if hold:
                holding = True
                continue
            # 送出的 chunk 指令只含 sentinel 前綴 `SW_XFER_CHUNK_`（數值於 target 端 `$((6*7))` 展開），
            # 不含 `SW_XFER_CHUNK_42` 全字；舊判斷永不成立，delay_ms 形同虛設。改為首個 chunk 延遲一次。
            if delay_ms and "SW_XFER_CHUNK_" in command:
                time.sleep(delay_ms / 1000)
                delay_ms = 0
            if output:
                os.write(1, output)
            os.write(1, b"root@fake:~# ")


if __name__ == "__main__" and len(sys.argv) >= 3 and sys.argv[1] == "--dut":
    raise SystemExit(_dut_main(sys.argv[2:]))
