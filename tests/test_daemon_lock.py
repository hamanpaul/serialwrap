import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import pytest

# POSIX-only：daemon_lock 經 lock_posix.py:4 頂層 import fcntl；非 POSIX 整檔略過、不 import。
if os.name != "posix":
    pytest.skip("SingletonLock POSIX 後端依賴 fcntl（lock_posix.py:4）", allow_module_level=True)

from sw_core.daemon_lock import SingletonLock  # noqa: E402

# 本檔混合 memory_only（SingletonLock 單元）與 process_rpc（真 daemon 子行程，P5-T19 #72／#101），
# 依 tests/test_resource_markers.py 規則 2：模組層只放平台 marker，資源 marker 逐項標註。
pytestmark = [pytest.mark.posix_only]

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.memory_only
class TestDaemonLock(unittest.TestCase):
    def test_singleton_lock_pid_written(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            lock_path = str(Path(td) / "serialwrapd.lock")
            socket_path = str(Path(td) / "serialwrapd.sock")
            lk = SingletonLock(lock_path, socket_path)
            lk.acquire()
            text = Path(lock_path).read_text(encoding="utf-8").strip()
            self.assertTrue(text.isdigit())
            lk.release()


def _isolated_daemon_env(root: Path) -> dict[str, str]:
    """throwaway daemon 的隔離 env（9 個目錄變數＋空 profile dir；不碰 production daemon）。"""
    env = {k: v for k, v in os.environ.items() if not k.startswith("SERIALWRAP_")}
    for name in ("STATE", "RUN", "CONFIG", "WAL", "LOG", "EVENTS", "EVENTS_RUNTIME", "BY_ID", "BY_PATH"):
        path = root / name.lower()
        path.mkdir(parents=True, exist_ok=True)
        env[f"SERIALWRAP_{name}_DIR"] = str(path)
    (root / "config" / "profiles").mkdir(parents=True, exist_ok=True)
    env["SERIALWRAP_TEST_SYSTEM_CONFIG_PATH"] = str(root / "system" / "config.yaml")
    env["HOME"] = str(root / "home")
    env["XDG_RUNTIME_DIR"] = ""
    env["PYTHONPATH"] = str(ROOT)
    return env


def _wait_ping(sock: Path, timeout_s: float = 15.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(2.0)
                s.connect(str(sock))
                s.sendall(b'{"id":1,"method":"health.ping"}\n')
                if b'"pong":true' in s.recv(4096):
                    return
        except OSError:
            pass
        time.sleep(0.05)
    raise AssertionError(f"daemon 未在 {timeout_s}s 內就緒：{sock}")


def _spawn_daemon(env: dict[str, str], stderr_path: Path) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [sys.executable, str(ROOT / "serialwrapd.py")],
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=stderr_path.open("wb"),
    )


@pytest.mark.process_rpc
@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT], ids=["SIGTERM", "SIGINT"])
def test_sigterm_orderly_shutdown_posix(tmp_path: Path, sig: signal.Signals) -> None:
    """P5-T19 #72（Python 特性測試，#81 集合）：SIGTERM／SIGINT → 有序停機（daemon.py:293-301）。

    RPC server 停止並移除 socket 檔、service.stop（drain、WAL close）、釋放鎖後 exit 0；新 daemon 可再取得鎖。
    Rust 對應：crates/sw-daemon/tests/daemon_process.rs::sigterm_sigint_orderly_stop。
    """
    env = _isolated_daemon_env(tmp_path)
    sock = Path(env["SERIALWRAP_RUN_DIR"]) / "serialwrapd.sock"
    proc = _spawn_daemon(env, tmp_path / "d1.stderr")
    try:
        _wait_ping(sock)
        proc.send_signal(sig)
        assert proc.wait(timeout=30) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    assert not sock.exists(), "有序停機後 socket 檔應已移除"
    # 鎖已釋放：第二個 daemon 可取得並正常停機。
    proc2 = _spawn_daemon(env, tmp_path / "d2.stderr")
    try:
        _wait_ping(sock)
        proc2.send_signal(signal.SIGTERM)
        assert proc2.wait(timeout=30) == 0
    finally:
        if proc2.poll() is None:
            proc2.kill()
            proc2.wait()


@pytest.mark.process_rpc
def test_no_profiles_loaded_stderr_and_continue_posix(tmp_path: Path) -> None:
    """P5-T19 #101（Python 特性測試，#81 集合）：空 profile dir → stderr 恰為 ``serialwrapd: no profiles loaded\\n``，
    daemon 仍可 ``health.ping``；同 lock 的第二個 daemon exit 2，stderr 為 ``no profiles loaded`` 後接 ``DAEMON_ALREADY_RUNNING``。

    Rust 對應：daemon_process.rs::no_profiles_message_and_continue、second_daemon_same_lock_exits_2_with_stderr。
    """
    env = _isolated_daemon_env(tmp_path)
    sock = Path(env["SERIALWRAP_RUN_DIR"]) / "serialwrapd.sock"
    stderr_path = tmp_path / "d1.stderr"
    proc = _spawn_daemon(env, stderr_path)
    try:
        _wait_ping(sock)
        second = subprocess.run(
            [sys.executable, str(ROOT / "serialwrapd.py")],
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=30,
        )
        assert second.returncode == 2
        assert second.stderr.decode("utf-8") == "serialwrapd: no profiles loaded\nserialwrapd: DAEMON_ALREADY_RUNNING\n"
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=30) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    assert stderr_path.read_text(encoding="utf-8") == "serialwrapd: no profiles loaded\n"


if __name__ == "__main__":
    unittest.main()
