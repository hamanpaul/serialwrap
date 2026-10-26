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
import stat
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterable
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
    子行程 exit status 非零一律回 ``failed``（即使 junit 僅有一筆 passed；例如 conftest live guard 以
    ``session.exitstatus=1`` 回報的失敗），診斷訊息保留 exit code 與 stderr／stdout 尾段。
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
        err_file = work / "child.err"
        with out_file.open("wb") as out, err_file.open("wb") as err:
            proc = subprocess.Popen(
                cmd, cwd=str(rootpath), env=env, stdin=subprocess.DEVNULL,
                stdout=out, stderr=err, start_new_session=True,
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
        tail = out_file.read_text(encoding="utf-8", errors="replace")[-4000:]
        err_tail = err_file.read_text(encoding="utf-8", errors="replace")[-4000:]
        # 子行程 exit status 為權威：非零一律 failed，不得因 junit 單筆 passed 而放行。
        if returncode != 0:
            return "failed", (
                f"namespace 子行程以非零 exit code 結束（rc={returncode}）：{nodeid}\n"
                f"--- stderr 尾段 ---\n{err_tail}\n--- stdout 尾段 ---\n{tail}"
            )
        if not junit.is_file():
            return "failed", f"namespace 子行程未產生 junit（rc={returncode}）：\n--- stderr 尾段 ---\n{err_tail}\n--- stdout 尾段 ---\n{tail}"
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

    run_dir = Path(tempfile.mkdtemp(prefix="sw-", dir="/tmp"))
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


def assert_no_tx_after(evidence_dir: Path, baseline: bytes, marker: bytes) -> None:
    """凍結基準（`baseline`，凍結當下的累計 TX）之後不得新增任何 DUT TX，且 console `marker` 不得出現於 TX。"""
    current = tx_bytes(evidence_dir)
    assert current == baseline, f"凍結後 DUT TX 有增量（{len(current)} 對基準 {len(baseline)} bytes）"
    assert marker not in current, "凍結期間 console 輸入的 marker 出現於 DUT TX"


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


# ── 持有者取樣觀測器（restart 交接判準）────────────────────────────────────────
# 舊判準於操作「完成之後」才以 wait_holder 逐一探測，並依測試自身的呼叫順序寫入 closed／opened 標記；
# 因此即使新舊 holder 曾經重疊，`old_close < new_open` 也必然成立（假綠）。
# 本區段改於操作開始前即於背景以固定週期取樣 tty 的持有者集合，記錄每個 pid 的 opened／closed 邊緣，
# 並以 (st_dev, st_ino) 比對 tty 身分（與 sw_core `same_tty` 同一判準；測試端不得 import sw_core，故於此重述）。
# 限制：取樣為 polling，短於取樣週期的瞬時重疊無法被觀測；判準為「已取樣到的重疊不得存在」，不宣稱完備。

HOLDER_SAMPLE_PERIOD_S = 0.005
PROC_FD_PERMISSION_RETRIES = 40
PROC_FD_PERMISSION_RETRY_S = 0.005
_DEVPTS_MAJORS = frozenset(range(136, 144))


def _same_tty(fd_st: os.stat_result, tty_st: os.stat_result) -> bool:
    """fd 所指 inode 與 tty 是否為同一裝置。主判準為 (st_dev, st_ino)；字串與 st_rdev 皆不足為憑。

    非 devpts 的字元裝置另允許 rdev 相同之別名（同 sw_core `same_tty`）。
    """
    if (fd_st.st_dev, fd_st.st_ino) == (tty_st.st_dev, tty_st.st_ino):
        return True
    if not (stat.S_ISCHR(fd_st.st_mode) and stat.S_ISCHR(tty_st.st_mode)):
        return False
    rdev = tty_st.st_rdev
    if not rdev or fd_st.st_rdev != rdev:
        return False
    return os.major(rdev) not in _DEVPTS_MAJORS


def numeric_proc_pids() -> tuple[int, ...]:
    """回傳當前 PID namespace 的全部數字 PID；讀取失敗時不得縮成部分候選集合。"""
    return tuple(sorted({int(name) for name in os.listdir("/proc") if name.isdecimal() and int(name) > 0}))


def _proc_pid_state(pid: int) -> str | None:
    """回傳 `/proc/<pid>/stat` 的 state；行程消失時回傳 None。"""
    try:
        stat_line = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    tail = stat_line.rsplit(")", 1)[-1].strip()
    return tail[0] if tail else "?"


def _retry_proc_fd_access(pid: int, operation: Callable[[], Any]) -> Any | None:
    """短暫 EACCES 時重試；確認行程已消失或成為 zombie 後視為無 fd。"""
    for attempt in range(PROC_FD_PERMISSION_RETRIES):
        try:
            return operation()
        except PermissionError:
            try:
                state = _proc_pid_state(pid)
            except OSError:
                state = "?"
            if state in (None, "Z", "X"):
                return None
            if attempt + 1 == PROC_FD_PERMISSION_RETRIES:
                raise
            time.sleep(PROC_FD_PERMISSION_RETRY_S)
    return None


def observe_holders(tty_st: os.stat_result, *, pids: Iterable[int]) -> list[int]:
    """只掃描呼叫者明確指定的候選行程，回傳持有 `tty_st` 的 pid（已排序）。

    候選集合須由測試 rig 的行程 handle 提供，避免掃描無關系統行程或正式 daemon。掃描途中消失的行程／fd 略過；
    無權讀取或其他觀測錯誤向上傳遞，避免把未知 holder 當成空集合。
    """
    result: list[int] = []
    supplied = tuple(pids)
    if not supplied or any(type(pid) is not int or pid <= 0 for pid in supplied):
        raise ValueError("持有者觀測必須提供非空、正整數 PID 集合")
    candidates = sorted(set(supplied))
    for pid in candidates:
        fd_dir = f"/proc/{pid}/fd"
        try:
            fds = _retry_proc_fd_access(pid, lambda: os.listdir(fd_dir))
        except (FileNotFoundError, ProcessLookupError):
            continue
        if fds is None:
            continue
        for fd in fds:
            try:
                fd_st = _retry_proc_fd_access(pid, lambda: os.stat(f"{fd_dir}/{fd}"))
            except (FileNotFoundError, ProcessLookupError):
                continue
            if fd_st is None:
                break
            if _same_tty(fd_st, tty_st):
                result.append(pid)
                break
    return sorted(result)


@dataclass(frozen=True)
class HolderEdge:
    kind: str     # "opened"（本次取樣首次出現）或 "closed"（本次取樣首次消失）
    pid: int
    sample: int   # 取樣序號；排序依據（不採牆上時鐘）
    t_ns: int     # 該取樣的 time.monotonic_ns()，僅供診斷


@dataclass
class HolderTimeline:
    """背景取樣的結果：每筆為 (time.monotonic_ns(), 當時持有 tty 的 pid 集合)。"""

    samples: list[tuple[int, frozenset[int]]]

    def edges(self, pid: int) -> list[HolderEdge]:
        out: list[HolderEdge] = []
        previous = False
        for index, (t_ns, holders) in enumerate(self.samples):
            current = pid in holders
            if current and not previous:
                out.append(HolderEdge("opened", pid, index, t_ns))
            elif previous and not current:
                out.append(HolderEdge("closed", pid, index, t_ns))
            previous = current
        return out

    def overlap_samples(self, first: int, second: int) -> list[int]:
        """兩個 pid 於同一取樣中同時持有 tty 的取樣序號。"""
        return [index for index, (_, holders) in enumerate(self.samples) if first in holders and second in holders]


class HolderObservationUnavailable(PermissionError):
    """候選 PID 的 fd 不可讀；此觀測不能計為交接通過。"""


def assert_holder_handoff(timeline: HolderTimeline, old_pid: int, new_pid: int) -> None:
    """斷言 restart 交接：全部取樣中新舊 holder 不得同時存在；舊 holder 最後一次 closed 不晚於新 holder 首次 opened。

    判準只採用取樣時間線（操作期間取樣），不採用事後探測的呼叫順序。取樣起點須已見舊 holder，否則無法證明
    舊 holder 確實曾持有 tty（取樣起點缺口）。
    """
    assert old_pid != new_pid, "新舊 pid 相同，無法判定交接"
    assert timeline.samples, "持有者觀測器無任何取樣"
    assert old_pid in timeline.samples[0][1], f"取樣起點未見舊 holder（取樣起點缺口）：{old_pid}"
    overlap = timeline.overlap_samples(old_pid, new_pid)
    assert not overlap, f"新舊 holder 同時持有 tty（取樣序號 {overlap[:5]}）：old={old_pid} new={new_pid}"
    old_closes = [edge for edge in timeline.edges(old_pid) if edge.kind == "closed"]
    assert old_closes, f"舊 holder 於取樣結束前未釋放 tty：{old_pid}"
    new_opens = [edge for edge in timeline.edges(new_pid) if edge.kind == "opened"]
    assert new_opens, f"取樣中未見新 holder 開啟 tty：{new_pid}"
    assert old_closes[-1].sample <= new_opens[0].sample, (
        f"新 holder 早於舊 holder 釋放前開啟：old_close={old_closes[-1]} new_open={new_opens[0]}"
    )
    assert new_pid in timeline.samples[-1][1], f"取樣結束時新 holder 未持有 tty：{new_pid}"


class HolderObserver:
    """於背景以固定週期取樣指定 PID 的 tty 持有狀態；`stop()` 後以 `timeline` 取得結果。

    `start()` 會等到首次取樣完成才返回，故時間線起點必定反映呼叫者開始操作前所見的狀態。
    預設只掃描呼叫者提供的 PID；`discover_all=True` 時每筆取樣重新列舉目前 `/proc` 的數字 PID，
    適用於 `pidns_rig` 中只包含本次隔離測試行程的命名空間。tty 身分於 `start()` 時取得。
    背景例外於 `start()`／`stop()` 重新拋出。
    取樣為 polling（單次掃描時間依指定 PID 的 fd 數量與負載而定）；
    短於一次掃描週期的瞬時重疊無法被觀測。
    """

    def __init__(
        self,
        slave_path: str,
        *,
        pids: Iterable[int],
        period_s: float = HOLDER_SAMPLE_PERIOD_S,
        discover_all: bool = False,
    ) -> None:
        self._slave_path = slave_path
        self._period_s = period_s
        self._discover_all = bool(discover_all)
        if self._discover_all:
            assert in_isolated_pidns(), "discover_all 須在隔離 PID namespace 中使用"
        supplied = tuple(pids)
        if not supplied or any(type(pid) is not int or pid <= 0 for pid in supplied):
            raise ValueError("持有者觀測必須提供非空、正整數 PID 集合")
        self._pids = set(supplied)
        self._pid_lock = threading.Lock()
        self._tty_st: os.stat_result | None = None
        self._samples: list[tuple[int, frozenset[int]]] = []
        self._stop_event = threading.Event()
        self._first_sample = threading.Event()
        self._thread: threading.Thread | None = None
        self._error: BaseException | None = None
        self.timeline: HolderTimeline | None = None

    def watch_pid(self, pid: int) -> None:
        """將新啟動且屬本次交接的行程加入後續取樣。"""
        if not isinstance(pid, int) or pid <= 0:
            raise ValueError("持有者觀測 PID 必須為正整數")
        with self._pid_lock:
            self._pids.add(pid)

    def _candidate_pids(self) -> tuple[int, ...]:
        with self._pid_lock:
            candidates = set(self._pids)
        if self._discover_all:
            candidates.update(numeric_proc_pids())
        return tuple(sorted(candidates))

    def start(self) -> "HolderObserver":
        self._tty_st = os.stat(self._slave_path)
        self._thread = threading.Thread(target=self._run, name="holder-observer", daemon=True)
        self._thread.start()
        # 等待首次取樣：否則呼叫者可能在背景尚未取樣前就開始操作，使時間線起點缺漏舊 holder。
        if not self._first_sample.wait(timeout=30.0):
            self._stop_event.set()
            raise RuntimeError("持有者觀測器未於 30 秒內完成首次取樣")
        if self._error is not None:
            if isinstance(self._error, HolderObservationUnavailable):
                raise self._error
            raise RuntimeError(f"持有者觀測器啟動失敗：{self._error!r}") from self._error
        return self

    def _run(self) -> None:
        try:
            assert self._tty_st is not None
            while True:
                holders = frozenset(observe_holders(self._tty_st, pids=self._candidate_pids()))
                self._samples.append((time.monotonic_ns(), holders))
                self._first_sample.set()
                # 停止後仍會再取樣一次，使時間線終點反映操作結束時的實際狀態。
                if self._stop_event.is_set():
                    return
                self._stop_event.wait(self._period_s)
        except PermissionError as exc:
            unavailable = HolderObservationUnavailable(
                f"候選 PID fd 權限不足，持有狀態未知：{exc}"
            )
            unavailable.__cause__ = exc
            self._error = unavailable
        except BaseException as exc:  # noqa: BLE001 - 於 start()／stop() 端重新拋出
            self._error = exc
        finally:
            self._first_sample.set()

    def stop(self) -> HolderTimeline:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=30.0)
            if self._thread.is_alive():
                raise RuntimeError("持有者觀測器未於 30 秒內結束")
        if self._error is not None:
            if isinstance(self._error, HolderObservationUnavailable):
                raise self._error
            raise RuntimeError(f"持有者觀測器失敗：{self._error!r}") from self._error
        self.timeline = HolderTimeline(list(self._samples))
        return self.timeline

    def __enter__(self) -> "HolderObserver":
        return self.start()

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        try:
            self.stop()
        except BaseException as stop_exc:
            if exc_type is None:
                raise
            # 操作本身已失敗時保留原始例外，觀測器錯誤僅附註。
            exc.add_note(f"持有者觀測器亦失敗：{stop_exc!r}")


def replay_holder_edges(bridge: "BridgeTarget", timeline: HolderTimeline, pids: Iterable[int]) -> None:
    """將觀測器邊緣依取樣序回放為 bridge `opened`／`closed` 標記，僅供 control.ndjson 留存。

    判準不採用此處的標記順序或時間；同一取樣內 closed 先於 opened。
    """
    wanted = list(pids)
    edges = [edge for pid in wanted for edge in timeline.edges(pid)]
    for edge in sorted(edges, key=lambda e: (e.sample, 0 if e.kind == "closed" else 1)):
        bridge.mark(edge.kind, edge.pid)


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
