"""S1M-241 ITEM 2：setup 以既有 ping 和已安裝 manifest 輸出 daemon 程式碼指紋。"""
from __future__ import annotations

import fcntl
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
import fcntl
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

def lock_is_free(path):
    """非阻塞取得排他鎖後立刻放開，確認舊 daemon 已執行 lock.release()。"""
    try:
        fd = os.open(path, os.O_RDWR)
    except OSError:
        return True
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return True
    finally:
        os.close(fd)


rpc("daemon.stop")
endpoint = os.environ["SERIALWRAP_TEST_ENDPOINT"]
lock_path = os.environ["SERIALWRAP_TEST_LOCK"]
deadline = time.monotonic() + 15
while time.monotonic() < deadline:
    # socket 關閉後 health.ping 會先失敗；flock 要等 service.stop() 之後的 lock.release()。
    if (
        rpc("health.ping") is None
        and not os.path.exists(endpoint)
        and lock_is_free(lock_path)
    ):
        break
    time.sleep(0.05)
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
# 不把子 PID namespace 的局部 PID 寫回外層 SERIALWRAP_TEST_PID_FILE。
# 外層 finally 只對啟動前寫入的舊 daemon PID 與 Popen 回收，避免 os.kill 打到外層其他行程。
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


def test_s1m241_systemctl_shim_waits_for_lock_and_keeps_outer_pid(tmp_path: Path) -> None:
    """restart 替身必須等舊 daemon 釋放 flock，且不得覆寫外層 PID 檔。

    ``daemon.stop`` 關閉 socket 後，``health.ping`` 會先失敗，但 ``lock.release()``
    仍在 ``service.stop()`` 之後。替身若在此時拉起新 daemon，會搶不到
    ``serialwrapd.lock``。替身又跑在 setup 的子 PID namespace，把該 namespace 的
    局部 PID 寫回外層 pid 檔會讓外層 ``os.kill`` 打到錯誤行程。
    """
    work = tmp_path / "work"
    source = work / "daemon-source"
    pristine = work / "daemon-pristine"
    for root in (source, pristine):
        (root / "sw_core").mkdir(parents=True)
        (root / "sw_core" / "marker.py").write_text("# pristine\n", encoding="utf-8")
    started = work / "daemon-started"
    (source / "serialwrapd.py").write_text(
        "import json, os, socket, time\n"
        "from pathlib import Path\n"
        "endpoint = os.environ['SERIALWRAP_TEST_ENDPOINT']\n"
        "Path(os.environ['SERIALWRAP_TEST_STARTED']).write_text(str(os.getpid()), encoding='ascii')\n"
        "try:\n"
        "    os.unlink(endpoint)\n"
        "except FileNotFoundError:\n"
        "    pass\n"
        "sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n"
        "sock.bind(endpoint)\n"
        "sock.listen(8)\n"
        "sock.settimeout(0.2)\n"
        "deadline = time.monotonic() + 25\n"
        "while time.monotonic() < deadline:\n"
        "    try:\n"
        "        conn, _addr = sock.accept()\n"
        "    except socket.timeout:\n"
        "        continue\n"
        "    with conn:\n"
        "        buf = b''\n"
        "        while b'\\n' not in buf:\n"
        "            chunk = conn.recv(4096)\n"
        "            if not chunk:\n"
        "                break\n"
        "            buf += chunk\n"
        "        if not buf:\n"
        "            continue\n"
        "        request = json.loads(buf.split(b'\\n', 1)[0])\n"
        "        conn.sendall(json.dumps({'id': request.get('id', 1), 'ok': True}).encode() + b'\\n')\n"
        "        if request.get('method') == 'daemon.stop':\n"
        "            break\n",
        encoding="utf-8",
    )
    endpoint = work / "run" / "serialwrapd.sock"
    endpoint.parent.mkdir(parents=True)
    lock_path = work / "run" / "serialwrapd.lock"
    lock_path.touch()
    profile_dir = work / "profiles"
    profile_dir.mkdir()
    pid_file = work / "daemon.pid"
    outer_pid = "424242"
    pid_file.write_text(outer_pid, encoding="ascii")
    log_path = work / "daemon.log"
    systemctl_log = work / "systemctl.ndjson"
    fake_bin = work / "fake-bin"
    shim = _write_systemctl_shim(fake_bin)
    env = os.environ.copy()
    env.update(
        {
            "SERIALWRAP_TEST_DAEMON_SOURCE": str(source),
            "SERIALWRAP_TEST_DAEMON_PRISTINE": str(pristine),
            "SERIALWRAP_TEST_ENDPOINT": str(endpoint),
            "SERIALWRAP_TEST_LOCK": str(lock_path),
            "SERIALWRAP_TEST_PROFILE_DIR": str(profile_dir),
            "SERIALWRAP_TEST_WORK": str(work),
            "SERIALWRAP_TEST_PID_FILE": str(pid_file),
            "SERIALWRAP_TEST_SYSTEMCTL_LOG": str(systemctl_log),
            "SERIALWRAP_TEST_DAEMON_LOG": str(log_path),
            "SERIALWRAP_TEST_STARTED": str(started),
        }
    )
    lock_fd = os.open(lock_path, os.O_RDWR)
    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    proc = subprocess.Popen(
        [sys.executable, str(shim), "--user", "restart", "serialwrap"],
        env=env,
        cwd=str(work),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        time.sleep(0.6)
        problems: list[str] = []
        if started.exists():
            problems.append("舊 daemon 仍持有 flock 時，替身已啟動新 daemon")
        if pid_file.read_text(encoding="ascii") != outer_pid:
            problems.append(
                "外層 PID 檔被改寫為 "
                f"{pid_file.read_text(encoding='ascii')!r}，不得寫入子 namespace 的局部 PID"
            )
        assert not problems, "; ".join(problems)
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)
        lock_fd = -1
        stdout, stderr = proc.communicate(timeout=20)
        assert proc.returncode == 0, f"替身應在鎖釋放後就緒：rc={proc.returncode} stdout={stdout!r} stderr={stderr!r}"
        assert started.exists(), "鎖釋放後應啟動新 daemon"
        assert pid_file.read_text(encoding="ascii") == outer_pid
    finally:
        if lock_fd >= 0:
            os.close(lock_fd)
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        if started.exists():
            try:
                os.kill(int(started.read_text(encoding="ascii")), signal.SIGTERM)
            except (OSError, ValueError):
                pass
        if endpoint.exists():
            endpoint.unlink()


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


def _spawn_isolated_daemon(
    work: Path,
    env: dict[str, str],
    endpoint: Path,
    stale_fingerprint: bool = False,
    override_version: str | None = None,
) -> tuple[subprocess.Popen, str]:
    run_dir = work / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    profile_dir = Path(env["SERIALWRAP_PROFILE_DIR"])
    profile_dir.mkdir(parents=True, exist_ok=True)
    daemon_log = work / "python-daemon.log"

    if stale_fingerprint or override_version is not None:
        daemon_source = work / "daemon-source"
        daemon_source.mkdir(parents=True, exist_ok=True)
        shutil.copytree(REPO / "sw_core", daemon_source / "sw_core")
        shutil.copy2(REPO / "serialwrapd.py", daemon_source / "serialwrapd.py")
        if override_version is not None:
            (daemon_source / "VERSION").write_text(f"{override_version}\n", encoding="utf-8")
        else:
            shutil.copy2(REPO / "VERSION", daemon_source / "VERSION")
        if stale_fingerprint:
            with (daemon_source / "sw_core" / "version.py").open("a", encoding="utf-8") as source:
                source.write("\n# 測試用不同指紋\n")
        entry = daemon_source / "serialwrapd.py"
    else:
        entry = REPO / "serialwrapd.py"

    log = daemon_log.open("w", encoding="utf-8")
    daemon = subprocess.Popen(
        [
            sys.executable,
            str(entry),
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
    log.close()
    ping = _wait_ready(str(endpoint), daemon)
    daemon_fp = ping.get("code_fingerprint", "")
    return daemon, daemon_fp


def _stop_daemon(daemon: subprocess.Popen | None, endpoint: str | Path) -> None:
    if daemon is None or daemon.poll() is not None:
        return
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


@pytest.mark.parametrize(
    "argv",
    [
        ["device", "list"],
        ["event", "list"],
        ["daemon", "status"],
    ],
)
def test_s1m241_formal_rpc_warning_paths_use_manifest(
    bundle, tmp_path: Path, argv: list[str]
) -> None:
    bundle_root = bundle.out()
    install_root = tmp_path / "installed"
    cli, manifest_path, manifest_fingerprint = _installed_layout(bundle_root, install_root)
    work = tmp_path / "isolated"
    endpoint = work / "run" / "serialwrapd.sock"
    env = _isolated_env(work, install_root, manifest_path, endpoint)

    daemon = None
    try:
        daemon, daemon_fp = _spawn_isolated_daemon(work, env, endpoint, stale_fingerprint=True)
        assert daemon_fp and daemon_fp != manifest_fingerprint

        result = subprocess.run(
            [str(cli)] + argv,
            env=env,
            cwd=str(work),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert result.returncode == 0, (
            f"CLI 命令 {argv} 失敗（rc={result.returncode}）："
            f"stdout={result.stdout!r}, stderr={result.stderr!r}"
        )
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        assert len(lines) == 1, f"stdout 必須只有一個 JSON：{result.stdout!r}"
        payload = json.loads(lines[0])
        assert payload.get("ok") is True

        expected_warning = (
            f"serialwrap: client 程式碼 {manifest_fingerprint} 與 daemon 程式碼 {daemon_fp} 不一致"
            "（daemon 可能仍在跑升級前的舊碼）；請執行 `serialwrap service restart`"
            "（on-demand 模式：`serialwrap daemon stop` 後 `serialwrap daemon start`）\n"
        )
        assert result.stderr == expected_warning, (
            f"stderr 必須完全等於指紋警告。\n預期：{expected_warning!r}\n實際：{result.stderr!r}"
        )
    finally:
        _stop_daemon(daemon, endpoint)


def test_s1m241_bundle_cli_and_daemon_share_fingerprint(bundle, tmp_path: Path) -> None:
    bundle_root = bundle.out()
    install_root = tmp_path / "installed"
    cli, manifest_path, manifest_fingerprint = _installed_layout(bundle_root, install_root)
    work = tmp_path / "isolated"
    endpoint = work / "run" / "serialwrapd.sock"
    env = _isolated_env(work, install_root, manifest_path, endpoint)

    daemon = None
    try:
        daemon, daemon_fp = _spawn_isolated_daemon(work, env, endpoint, stale_fingerprint=False)
        assert daemon_fp == manifest_fingerprint

        # 1. --version：純文字格式
        res_ver = subprocess.run(
            [str(cli), "--version"],
            env=env,
            cwd=str(work),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert res_ver.returncode == 0
        assert res_ver.stdout == f"serialwrap {VERSION} (code {manifest_fingerprint})\n"
        assert res_ver.stderr == ""

        # 2. help：純文字格式
        res_help = subprocess.run(
            [str(cli), "--help"],
            env=env,
            cwd=str(work),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert res_help.returncode == 0
        assert res_help.stdout.startswith("usage: serialwrap")
        assert res_help.stderr == ""

        # 3. 一般 RPC：單一 JSON，指紋相符時不警告
        res_rpc = subprocess.run(
            [str(cli), "device", "list"],
            env=env,
            cwd=str(work),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert res_rpc.returncode == 0
        lines = [line for line in res_rpc.stdout.splitlines() if line.strip()]
        assert len(lines) == 1
        rpc_payload = json.loads(lines[0])
        assert rpc_payload.get("ok") is True
        assert res_rpc.stderr == ""

        # 4. doctor：單一 JSON，daemon_code_current 維持 advisory 且使用相同指紋
        res_doc = subprocess.run(
            [str(cli), "doctor"],
            env=env,
            cwd=str(work),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert res_doc.returncode == 0
        doc_lines = [line for line in res_doc.stdout.splitlines() if line.strip()]
        assert len(doc_lines) == 1
        doc_payload = json.loads(doc_lines[0])
        checks = {c["check"]: c for c in doc_payload["checks"]}
        assert "daemon_code_current" in checks
        dcc = checks["daemon_code_current"]
        assert dcc["ok"] is True
        assert dcc["advisory"] is True
        assert f"code {manifest_fingerprint}" in dcc["detail"]
        assert res_doc.stderr == ""
    finally:
        _stop_daemon(daemon, endpoint)


def test_s1m241_rpc_warning_edge_cases(bundle, tmp_path: Path) -> None:
    bundle_root = bundle.out()
    install_root = tmp_path / "installed"
    cli, manifest_path, manifest_fingerprint = _installed_layout(bundle_root, install_root)
    work = tmp_path / "isolated"
    endpoint = work / "run" / "serialwrapd.sock"
    env = _isolated_env(work, install_root, manifest_path, endpoint)

    # 1. 版本不同：只印版本警告，不印指紋警告
    daemon_v = None
    try:
        daemon_v, daemon_fp = _spawn_isolated_daemon(
            work / "d_ver", env, endpoint, stale_fingerprint=True, override_version="0.2.0"
        )
        res_ver_diff = subprocess.run(
            [str(cli), "device", "list"],
            env=env,
            cwd=str(work),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert res_ver_diff.returncode == 0
        lines = [line for line in res_ver_diff.stdout.splitlines() if line.strip()]
        assert len(lines) == 1
        expected_ver_warning = (
            f"serialwrap: client 版本 {VERSION} 與 daemon 版本 0.2.0 不一致"
            "（建議統一安裝來源或重啟 daemon 對齊版本；serialwrap doctor 可查其他安裝）\n"
        )
        assert res_ver_diff.stderr == expected_ver_warning
    finally:
        _stop_daemon(daemon_v, endpoint)

    # 2. 缺 client 端指紋：靜默不警告
    no_fp_manifest = work / "no_fp_manifest.json"
    manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest_data["components"]["daemon"].pop("code_fingerprint", None)
    no_fp_manifest.write_text(json.dumps(manifest_data), encoding="utf-8")
    env_no_fp = dict(env)
    env_no_fp["SERIALWRAP_RELEASE_MANIFEST"] = str(no_fp_manifest)

    daemon_stale = None
    try:
        daemon_stale, daemon_fp = _spawn_isolated_daemon(
            work / "d_stale", env_no_fp, endpoint, stale_fingerprint=True
        )
        res_no_fp = subprocess.run(
            [str(cli), "device", "list"],
            env=env_no_fp,
            cwd=str(work),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert res_no_fp.returncode == 0
        lines = [line for line in res_no_fp.stdout.splitlines() if line.strip()]
        assert len(lines) == 1
        assert res_no_fp.stderr == ""

        # 3. 失敗 RPC：保留原錯誤行及指紋警告
        res_fail = subprocess.run(
            [str(cli), "device", "release", "--selector", "COM_NONEXISTENT"],
            env=env,
            cwd=str(work),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert res_fail.returncode == 2
        fail_lines = [line for line in res_fail.stdout.splitlines() if line.strip()]
        assert len(fail_lines) == 1
        fail_payload = json.loads(fail_lines[0])
        assert fail_payload.get("ok") is False
        expected_fp_warning = (
            f"serialwrap: client 程式碼 {manifest_fingerprint} 與 daemon 程式碼 {daemon_fp} 不一致"
            "（daemon 可能仍在跑升級前的舊碼）；請執行 `serialwrap service restart`"
            "（on-demand 模式：`serialwrap daemon stop` 後 `serialwrap daemon start`）\n"
        )
        assert expected_fp_warning in res_fail.stderr
        assert (
            "DEVICE_NOT_FOUND" in res_fail.stderr
            or "device.release" in res_fail.stderr
            or "error" in res_fail.stderr.lower()
        )
    finally:
        _stop_daemon(daemon_stale, endpoint)


def test_s1m241_setup_stale_on_demand_reports_manual_restart(bundle, tmp_path: Path) -> None:
    bundle_root = bundle.out()
    install_root = tmp_path / "installed"
    cli, manifest_path, manifest_fingerprint = _installed_layout(bundle_root, install_root)
    work = tmp_path / "isolated"
    run_dir = work / "run"
    run_dir.mkdir(parents=True)
    endpoint = run_dir / "serialwrapd.sock"
    env = _isolated_env(work, install_root, manifest_path, endpoint)
    Path(env["SERIALWRAP_CONFIG"]).write_text("supervision_mode: on-demand\n", encoding="utf-8")

    daemon = None
    try:
        daemon, daemon_fp = _spawn_isolated_daemon(work, env, endpoint, stale_fingerprint=True)
        assert daemon_fp != manifest_fingerprint

        result = subprocess.run(
            _isolated_pidns_prefix() + [str(cli), "setup", "--on-demand"],
            env=env,
            cwd=str(work),
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        assert result.returncode == 0
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        assert len(lines) == 1
        payload = json.loads(lines[0])
        daemon_code = payload["daemon_code"]
        assert daemon_code["stale"] is True
        assert daemon_code["reason"] == "fingerprint_mismatch"
        assert daemon_code["action"] == "manual_restart"
        assert daemon_code["hint"] == "on-demand 模式不自動重啟；請執行 `serialwrap daemon stop` 後 `serialwrap daemon start`"
        assert daemon_code["installed"] == manifest_fingerprint
        assert daemon_code["daemon"] == daemon_fp
        assert payload["setup"].get("daemon_restart") is None
        assert result.stderr == ""
    finally:
        _stop_daemon(daemon, endpoint)


def test_s1m241_setup_stale_systemd_no_restart_reports_skipped(bundle, tmp_path: Path) -> None:
    bundle_root = bundle.out()
    install_root = tmp_path / "installed"
    cli, manifest_path, manifest_fingerprint = _installed_layout(bundle_root, install_root)
    work = tmp_path / "isolated"
    run_dir = work / "run"
    run_dir.mkdir(parents=True)
    endpoint = run_dir / "serialwrapd.sock"
    env = _isolated_env(work, install_root, manifest_path, endpoint)
    Path(env["SERIALWRAP_CONFIG"]).write_text("supervision_mode: systemd-user\n", encoding="utf-8")

    fake_bin = work / "fake-bin"
    _write_systemctl_shim(fake_bin)
    env["PATH"] = str(fake_bin) + os.pathsep + env.get("PATH", "/usr/bin:/bin")
    systemctl_log = work / "systemctl.ndjson"
    daemon_log = work / "python-daemon.log"
    pid_file = work / "daemon.pid"
    env.update(
        {
            "SERIALWRAP_TEST_ENDPOINT": str(endpoint),
            "SERIALWRAP_TEST_LOCK": str(run_dir / "serialwrapd.lock"),
            "SERIALWRAP_TEST_PROFILE_DIR": env["SERIALWRAP_PROFILE_DIR"],
            "SERIALWRAP_TEST_WORK": str(work),
            "SERIALWRAP_TEST_PID_FILE": str(pid_file),
            "SERIALWRAP_TEST_SYSTEMCTL_LOG": str(systemctl_log),
            "SERIALWRAP_TEST_DAEMON_LOG": str(daemon_log),
        }
    )

    daemon = None
    try:
        daemon, daemon_fp = _spawn_isolated_daemon(work, env, endpoint, stale_fingerprint=True)
        assert daemon_fp != manifest_fingerprint

        result = subprocess.run(
            _isolated_pidns_prefix() + [str(cli), "setup", "--user", "--no-restart"],
            env=env,
            cwd=str(work),
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        assert result.returncode == 0
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        assert len(lines) == 1
        payload = json.loads(lines[0])
        daemon_code = payload["daemon_code"]
        assert daemon_code["stale"] is True
        assert daemon_code["reason"] == "fingerprint_mismatch"
        assert daemon_code["action"] == "skipped_no_restart_flag"
        assert daemon_code["hint"] == "已依 --no-restart 略過；準備好後執行 `serialwrap service restart`"
        assert daemon_code["installed"] == manifest_fingerprint
        assert daemon_code["daemon"] == daemon_fp
        assert payload["setup"].get("daemon_restart") is None
        assert result.stderr == ""

        # systemctl 不會被要求 restart
        calls = [json.loads(line) for line in systemctl_log.read_text(encoding="utf-8").splitlines()]
        assert ["--user", "restart", "serialwrap"] not in calls
    finally:
        _stop_daemon(daemon, endpoint)


