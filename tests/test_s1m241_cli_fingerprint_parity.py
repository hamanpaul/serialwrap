"""S1M-241 ITEM 2：setup 以既有 ping 和已安裝 manifest 輸出 daemon 程式碼指紋。"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
MKBUNDLE = REPO / "scripts" / "rust" / "mkbundle.sh"
VERSION = (REPO / "VERSION").read_text(encoding="utf-8").strip()

pytestmark = [pytest.mark.posix_only, pytest.mark.process_rpc]


def _require_cargo() -> None:
    if shutil.which("cargo") is None:
        if os.environ.get("SW_REQUIRE_CARGO") == "1":
            pytest.fail("SW_REQUIRE_CARGO=1 但找不到 cargo（證據不足，非 PASS）")
        pytest.skip("cargo 不存在（證據不足，非 PASS）")


class _LazyBundle:
    """第一次使用時建立一份共用的完整 dev bundle。"""

    def __init__(self, root: Path) -> None:
        self._root = root
        self._out: Path | None = None

    def out(self) -> Path:
        _require_cargo()
        if self._out is None:
            out = self._root / "dev bundle"
            args = [str(MKBUNDLE), "--dev", "--out", str(out)]
            result = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=900,
                cwd=str(REPO),
                check=False,
            )
            assert result.returncode == 0, (
                f"mkbundle.sh 建置失敗（rc={result.returncode}）：{result.stderr.strip()}"
            )
            self._out = out
        return self._out


@pytest.fixture(scope="module")
def bundle(tmp_path_factory) -> _LazyBundle:
    return _LazyBundle(tmp_path_factory.mktemp("s1m241-bundle", numbered=False))


def _installed_layout(bundle_root: Path, install_root: Path) -> tuple[Path, Path, str]:
    """在隔離根建立安裝版本、stable entry 與 current-release.json。"""
    version_source = bundle_root / "versions" / VERSION
    version_dir = install_root / "versions" / VERSION
    shutil.copytree(version_source, version_dir, symlinks=True)

    manifest_path = version_dir / "release-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    fingerprint = manifest["components"]["daemon"].get("code_fingerprint")
    assert isinstance(fingerprint, str) and fingerprint, "bundle manifest 缺少 daemon.code_fingerprint"

    bin_dir = install_root / "bin"
    bin_dir.mkdir(parents=True)
    for name in ("serialwrap", "serialwrapd"):
        os.symlink(f"../versions/{VERSION}/bin/{name}", bin_dir / name)
    (install_root / "current-release.json").write_text(
        json.dumps({"bundle_version": VERSION, "dir": f"versions/{VERSION}"}),
        encoding="utf-8",
    )
    return bin_dir / "serialwrap", manifest_path, fingerprint


def _isolated_env(
    root: Path,
    install_root: Path,
    manifest_path: Path,
    endpoint: Path,
) -> dict[str, str]:
    """每個子程序都明確收到隔離目錄、endpoint 與 manifest。"""
    env = dict(os.environ)
    for key in tuple(env):
        if key.startswith("SERIALWRAP_"):
            env.pop(key, None)

    paths = {
        "home": root / "home",
        "config": root / "config",
        "state": root / "state",
        "run": root / "run",
        "wal": root / "wal",
        "by-id": root / "by-id",
        "by-path": root / "by-path",
        "blog": root / "blog",
        "events": root / "events.d",
        "events-runtime": root / "events-rt",
        "data": root / "data",
        "system-config": root / "system-config",
    }
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True)

    env.update(
        {
            "HOME": str(paths["home"]),
            "XDG_CONFIG_HOME": str(root / "xdg-config"),
            "XDG_STATE_HOME": str(root / "xdg-state"),
            "XDG_DATA_HOME": str(root / "xdg-data"),
            "XDG_RUNTIME_DIR": str(root / "xdg-runtime"),
            "SERIALWRAP_CONFIG_DIR": str(paths["config"]),
            "SERIALWRAP_CONFIG": str(paths["config"] / "config.yaml"),
            "SERIALWRAP_STATE_DIR": str(paths["state"]),
            "SERIALWRAP_RUN_DIR": str(paths["run"]),
            "SERIALWRAP_WAL_DIR": str(paths["wal"]),
            "SERIALWRAP_BY_ID_DIR": str(paths["by-id"]),
            "SERIALWRAP_BY_PATH_DIR": str(paths["by-path"]),
            "SERIALWRAP_LOG_DIR": str(paths["blog"]),
            "SERIALWRAP_EVENTS_DIR": str(paths["events"]),
            "SERIALWRAP_EVENTS_RUNTIME_DIR": str(paths["events-runtime"]),
            "SERIALWRAP_DATA_DIR": str(paths["data"]),
            "SERIALWRAP_PROFILE_DIR": str(paths["config"] / "profiles"),
            "SERIALWRAP_TEST_SYSTEM_CONFIG_PATH": str(paths["system-config"] / "config.yaml"),
            "SERIALWRAP_INSTALL_ROOT": str(install_root),
            "SERIALWRAP_RELEASE_MANIFEST": str(manifest_path),
            "SERIALWRAP_ENDPOINT": str(endpoint),
            "SERIALWRAP_TTYMCU_PATH": str(paths["run"] / "dev" / "ttyMCU"),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    config = Path(env["SERIALWRAP_CONFIG"])
    config.write_text("supervision_mode: on-demand\n", encoding="utf-8")
    return env


def _send_rpc(endpoint: str, method: str) -> dict | None:
    harness_dir = str(REPO / "tests" / "contract_blackbox")
    if harness_dir not in sys.path:
        sys.path.insert(0, harness_dir)
    from bb_daemon import _send_rpc as send

    return send(endpoint, method, {}, timeout_s=1.0)


def _wait_ready(endpoint: str, daemon: subprocess.Popen, timeout_s: float = 30.0) -> dict:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if daemon.poll() is not None:
            raise AssertionError(f"隔離 Python daemon 提早結束 rc={daemon.returncode}")
        response = _send_rpc(endpoint, "health.ping")
        if response and response.get("ok") is True:
            return response
        time.sleep(0.2)
    raise AssertionError(f"隔離 Python daemon 未就緒：{endpoint}")


def _isolated_pidns_prefix() -> list[str]:
    """避免 setup 的行程掃描看見宿主 daemon；能力不足時明確記為 SKIP。"""
    harness_dir = str(REPO / "tests" / "contract_blackbox")
    if harness_dir not in sys.path:
        sys.path.insert(0, harness_dir)
    from bb_l15 import unshare_pidns_argv

    prefix = unshare_pidns_argv()
    if prefix is None:
        pytest.skip("無法以非特權 unshare 建立 pid namespace（環境能力不足，非 PASS）")
    return prefix


def _pid_is_running(pid: int) -> bool:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    except FileNotFoundError:
        return False
    return stat[stat.rfind(")") + 2] != "Z"


def _write_systemctl_shim(fake_bin: Path) -> Path:
    """建立只服務本測試的 systemctl 替身，restart 會重拉隔離 Python daemon。"""
    fake_bin.mkdir(parents=True, exist_ok=True)
    script = fake_bin / "systemctl"
    script.write_text(
        r'''#!/usr/bin/python3
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path


def rpc(method):
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(0.5)
    try:
        sock.connect(os.environ["SERIALWRAP_TEST_ENDPOINT"])
        data = json.dumps({"id": 1, "method": method, "params": {}}).encode() + b"\n"
        sock.sendall(data)
        buf = b""
        while b"\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                return None
            buf += chunk
        return json.loads(buf.split(b"\n", 1)[0])
    except OSError:
        return None
    finally:
        sock.close()


args = sys.argv[1:]
with open(os.environ["SERIALWRAP_TEST_SYSTEMCTL_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps(args) + "\n")
if args == ["--user", "is-active", "serialwrap"]:
    print("active")
    raise SystemExit(0)
if args != ["--user", "restart", "serialwrap"]:
    raise SystemExit(0)

rpc("daemon.stop")
deadline = time.monotonic() + 15
while time.monotonic() < deadline:
    if rpc("health.ping") is None:
        break
    time.sleep(0.1)
else:
    raise SystemExit("隔離舊 daemon 未停止")

source = Path(os.environ["SERIALWRAP_TEST_DAEMON_SOURCE"])
pristine = Path(os.environ["SERIALWRAP_TEST_DAEMON_PRISTINE"])
shutil.rmtree(source / "sw_core")
shutil.copytree(pristine / "sw_core", source / "sw_core")
command = [
    sys.executable,
    str(source / "serialwrapd.py"),
    "--socket",
    os.environ["SERIALWRAP_TEST_ENDPOINT"],
    "--lock",
    os.environ["SERIALWRAP_TEST_LOCK"],
    "--profile-dir",
    os.environ["SERIALWRAP_TEST_PROFILE_DIR"],
]
with open(os.environ["SERIALWRAP_TEST_DAEMON_LOG"], "a", encoding="utf-8") as log:
    proc = subprocess.Popen(
        command,
        env=os.environ.copy(),
        cwd=os.environ["SERIALWRAP_TEST_WORK"],
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
pid = proc.pid
try:
    status = Path(f"/proc/{pid}/status").read_text(encoding="utf-8")
    for line in status.splitlines():
        if line.startswith("NSpid:"):
            pid = int(line.split()[1])
            break
except OSError:
    pass
Path(os.environ["SERIALWRAP_TEST_PID_FILE"]).write_text(str(pid), encoding="ascii")
deadline = time.monotonic() + 20
while time.monotonic() < deadline:
    response = rpc("health.ping")
    if response and response.get("ok") is True:
        raise SystemExit(0)
    if proc.poll() is not None:
        raise SystemExit(f"隔離新 daemon 提早結束 rc={proc.returncode}")
    time.sleep(0.1)
raise SystemExit("隔離新 daemon 未就緒")
''',
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def test_s1m241_setup_current_manifest_is_reported(bundle, tmp_path: Path) -> None:
    bundle_root = bundle.out()
    install_root = tmp_path / "installed"
    cli, manifest_path, manifest_fingerprint = _installed_layout(bundle_root, install_root)
    work = tmp_path / "isolated"
    run_dir = work / "run"
    run_dir.mkdir(parents=True)
    endpoint = run_dir / "serialwrapd.sock"
    env = _isolated_env(work, install_root, manifest_path, endpoint)
    profile_dir = Path(env["SERIALWRAP_PROFILE_DIR"])
    profile_dir.mkdir(parents=True, exist_ok=True)
    daemon_log = work / "python-daemon.log"

    daemon: subprocess.Popen | None = None
    try:
        with daemon_log.open("w", encoding="utf-8") as log:
            daemon = subprocess.Popen(
                [
                    sys.executable,
                    str(REPO / "serialwrapd.py"),
                    "--socket",
                    str(endpoint),
                    "--lock",
                    str(run_dir / "serialwrapd.lock"),
                    "--profile-dir",
                    str(profile_dir),
                ],
                env=env,
                cwd=str(work),
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
        ping = _wait_ready(str(endpoint), daemon)
        assert ping.get("code_fingerprint") == manifest_fingerprint

        # daemon 啟動時會記錄 endpoint；移除該欄位，讓 setup 由明確的隔離 endpoint 預設值解析，
        # 保持測試只觀察 setup 自己的既有 ping 探測。
        Path(env["SERIALWRAP_CONFIG"]).write_text(
            "supervision_mode: on-demand\n", encoding="utf-8"
        )
        result = subprocess.run(
            _isolated_pidns_prefix() + [str(cli), "setup", "--on-demand"],
            env=env,
            cwd=str(work),
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        assert result.returncode == 0, (
            f"installed Rust CLI setup 失敗（rc={result.returncode}）："
            f"stdout={result.stdout!r}, stderr={result.stderr!r}, daemon={daemon_log.read_text(encoding='utf-8')}"
        )
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        assert len(lines) == 1, f"setup stdout 必須只有一個 JSON：{result.stdout!r}"
        payload = json.loads(lines[0])
        daemon_code = payload["daemon_code"]
        assert daemon_code["installed"] == manifest_fingerprint
        assert daemon_code["daemon"] == manifest_fingerprint
        assert daemon_code["action"] == "none"
        assert "checked" not in daemon_code
        assert result.stderr == ""
    finally:
        if daemon is not None and daemon.poll() is None:
            try:
                _send_rpc(str(endpoint), "daemon.stop")
                daemon.wait(timeout=5.0)
            except (OSError, subprocess.TimeoutExpired):
                daemon.terminate()
                try:
                    daemon.wait(timeout=5.0)
                except subprocess.TimeoutExpired:
                    daemon.kill()
                    daemon.wait(timeout=5.0)


def test_s1m241_setup_stale_systemd_restarts_and_verifies(bundle, tmp_path: Path) -> None:
    bundle_root = bundle.out()
    install_root = tmp_path / "installed"
    cli, manifest_path, manifest_fingerprint = _installed_layout(bundle_root, install_root)
    work = tmp_path / "isolated"
    run_dir = work / "run"
    run_dir.mkdir(parents=True)
    endpoint = run_dir / "serialwrapd.sock"
    env = _isolated_env(work, install_root, manifest_path, endpoint)
    Path(env["SERIALWRAP_CONFIG"]).write_text("supervision_mode: systemd-user\n", encoding="utf-8")

    daemon_source = work / "daemon-source"
    pristine_source = work / "daemon-pristine"
    for source in (daemon_source, pristine_source):
        source.mkdir()
        shutil.copytree(REPO / "sw_core", source / "sw_core")
        shutil.copy2(REPO / "serialwrapd.py", source / "serialwrapd.py")
    with (daemon_source / "sw_core" / "version.py").open("a", encoding="utf-8") as source:
        source.write("\n# 測試用舊版指紋\n")

    fake_bin = work / "fake-bin"
    _write_systemctl_shim(fake_bin)
    env["PATH"] = str(fake_bin) + os.pathsep + env.get("PATH", "/usr/bin:/bin")
    pid_file = work / "daemon.pid"
    systemctl_log = work / "systemctl.ndjson"
    daemon_log = work / "python-daemon.log"
    env.update(
        {
            "SERIALWRAP_TEST_DAEMON_SOURCE": str(daemon_source),
            "SERIALWRAP_TEST_DAEMON_PRISTINE": str(pristine_source),
            "SERIALWRAP_TEST_ENDPOINT": str(endpoint),
            "SERIALWRAP_TEST_LOCK": str(run_dir / "serialwrapd.lock"),
            "SERIALWRAP_TEST_PROFILE_DIR": env["SERIALWRAP_PROFILE_DIR"],
            "SERIALWRAP_TEST_WORK": str(work),
            "SERIALWRAP_TEST_PID_FILE": str(pid_file),
            "SERIALWRAP_TEST_SYSTEMCTL_LOG": str(systemctl_log),
            "SERIALWRAP_TEST_DAEMON_LOG": str(daemon_log),
        }
    )
    daemon: subprocess.Popen | None = None
    try:
        with daemon_log.open("w", encoding="utf-8") as log:
            daemon = subprocess.Popen(
                [
                    sys.executable,
                    str(daemon_source / "serialwrapd.py"),
                    "--socket",
                    str(endpoint),
                    "--lock",
                    str(run_dir / "serialwrapd.lock"),
                    "--profile-dir",
                    env["SERIALWRAP_PROFILE_DIR"],
                ],
                env=env,
                cwd=str(work),
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
        pid_file.write_text(str(daemon.pid), encoding="ascii")
        ping = _wait_ready(str(endpoint), daemon)
        assert ping.get("code_fingerprint") != manifest_fingerprint

        result = subprocess.run(
            _isolated_pidns_prefix() + [str(cli), "setup", "--user"],
            env=env,
            cwd=str(work),
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        assert result.returncode == 0, (
            f"installed Rust CLI setup 失敗（rc={result.returncode}）："
            f"stdout={result.stdout!r}, stderr={result.stderr!r}, daemon={daemon_log.read_text(encoding='utf-8')}"
        )
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        assert len(lines) == 1, f"setup stdout 必須只有一個 JSON：{result.stdout!r}"
        payload = json.loads(lines[0])
        daemon_code = payload["daemon_code"]
        assert daemon_code["stale"] is True
        assert daemon_code["reason"] == "fingerprint_mismatch"
        assert daemon_code["action"] == "restarted", (
            f"daemon_code={daemon_code!r}, setup={payload.get('setup')!r}, "
            f"systemctl={systemctl_log.read_text(encoding='utf-8')!r}, "
            f"daemon={daemon_log.read_text(encoding='utf-8')!r}"
        )
        assert daemon_code["after"] == "match"
        assert "checked" not in daemon_code
        assert payload["setup"]["daemon_restart"] == "ran"
        assert result.stderr == ""
        calls = [json.loads(line) for line in systemctl_log.read_text(encoding="utf-8").splitlines()]
        assert ["--user", "is-active", "serialwrap"] in calls
        assert ["--user", "restart", "serialwrap"] in calls
    finally:
        current_pid = int(pid_file.read_text(encoding="ascii")) if pid_file.exists() else None
        _send_rpc(str(endpoint), "daemon.stop")
        if current_pid is not None:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and _pid_is_running(current_pid):
                time.sleep(0.05)
            if _pid_is_running(current_pid):
                os.kill(current_pid, signal.SIGTERM)
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and _pid_is_running(current_pid):
                    time.sleep(0.05)
        if daemon is not None and daemon.poll() is None:
            try:
                daemon.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                daemon.terminate()
                try:
                    daemon.wait(timeout=5.0)
                except subprocess.TimeoutExpired:
                    daemon.kill()
                    daemon.wait(timeout=5.0)
