"""tools/uart_stdio_bridge/tests/helpers.py

測試輔助函式與 Fixture：提供 bridge_proc、FakeEndpoint、StubDutProcess、read_exact、write_all。
"""
from __future__ import annotations

import json
import os
import select
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

import pytest

# 確保 tests 目錄與 src 目錄在 sys.path
_TEST_DIR = Path(__file__).resolve().parent
_TOOL_ROOT = _TEST_DIR.parent
_SRC_DIR = _TOOL_ROOT / "src"
if str(_TEST_DIR) not in sys.path:
    sys.path.insert(0, str(_TEST_DIR))
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))


def pytest_configure(config: pytest.Config) -> None:
    """註冊工具測試專用 markers。"""
    config.addinivalue_line("markers", "posix_pty: 標記需要 POSIX PTY 的測試")
    config.addinivalue_line("markers", "windows_com: 標記需要 Windows COM 的測試")
    config.addinivalue_line("markers", "physical_uart: 標記需要實體 UART 的測試")
    config.addinivalue_line("markers", "docker: 標記需要 Docker 容器的測試")


def read_exact(fd: int, n: int, timeout_s: float = 10.0) -> bytes:
    """從檔案描述子讀取恰好 n 個位元組，處理短讀與非阻塞 I/O，逾時則拋出 TimeoutError。"""
    buf = bytearray()
    deadline = time.monotonic() + timeout_s
    poll = select.poll()
    poll.register(fd, select.POLLIN | select.POLLHUP | select.POLLERR)
    while len(buf) < n:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"read_exact 逾時：已讀取 {len(buf)}/{n} 位元組")
        events = poll.poll(int(max(remaining, 0.001) * 1000))
        if not events:
            continue
        for _, ev in events:
            if ev & (select.POLLIN | select.POLLHUP):
                try:
                    chunk = os.read(fd, n - len(buf))
                    if chunk:
                        buf.extend(chunk)
                    elif ev & select.POLLHUP:
                        raise EOFError(f"讀取至 EOF，僅取得 {len(buf)}/{n} 位元組")
                except BlockingIOError:
                    pass
            if ev & select.POLLERR:
                raise OSError("POLLERR 發生於檔案描述子")
    return bytes(buf)


def write_all(fd: int, data: bytes, timeout_s: float = 10.0) -> None:
    """將所有位元組寫入檔案描述子，處理短寫與 EAGAIN，逾時則拋出 TimeoutError。"""
    written = 0
    total = len(data)
    deadline = time.monotonic() + timeout_s
    poll = select.poll()
    poll.register(fd, select.POLLOUT | select.POLLERR)
    while written < total:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"write_all 逾時：已寫入 {written}/{total} 位元組")
        events = poll.poll(int(max(remaining, 0.001) * 1000))
        if not events:
            continue
        for _, ev in events:
            if ev & select.POLLOUT:
                try:
                    n = os.write(fd, data[written:])
                    written += n
                except BlockingIOError:
                    pass
            if ev & select.POLLERR:
                raise OSError("POLLERR 發生於檔案描述子")


class FakeEndpoint:
    """測試用 Fake 端點，實作 Task 3 Endpoint Protocol。"""

    def __init__(
        self,
        capabilities: dict[str, bool] | None = None,
        transport_mode: str = "virtual_pty",
        write_fn: Callable[[bytes, float], int] | None = None,
    ) -> None:
        self.kind = "posix-pty"
        self.transport_mode = transport_mode
        self.generation = 1
        self.is_open = False
        self._peer_open: bool | None = True
        self._caps = {
            "transport_disconnect": True,
            "by_id_link": True,
            "peer_state": True,
        }
        if capabilities is not None:
            self._caps.update(capabilities)
        self._write_fn = write_fn
        self.read_buf = bytearray()
        self.written = bytearray()
        self.disconnect_count = 0
        self.reconnect_count = 0

    def open(self) -> None:
        self.is_open = True

    def read(self, max_bytes: int, timeout_s: float) -> bytes:
        if not self.read_buf:
            return b""
        chunk = bytes(self.read_buf[:max_bytes])
        del self.read_buf[:max_bytes]
        return chunk

    def write_some(self, data: bytes, timeout_s: float) -> int:
        if self._write_fn is not None:
            return self._write_fn(data, timeout_s)
        self.written.extend(data)
        return len(data)

    def peer_open(self) -> bool | None:
        if not self._caps.get("peer_state", True):
            return None
        return self._peer_open

    def set_peer_open(self, state: bool | None) -> None:
        self._peer_open = state

    def capabilities(self) -> dict[str, bool]:
        return dict(self._caps)

    def disconnect(self, *, remove_link: bool = True) -> None:
        if not self._caps.get("transport_disconnect", False):
            raise OSError("HARNESS_CAPABILITY_UNAVAILABLE")
        self.disconnect_count += 1
        self.is_open = False

    def reconnect(self) -> int:
        self.reconnect_count += 1
        self.generation += 1
        self.is_open = True
        return self.generation

    def link_remove(self) -> None:
        if not self._caps.get("by_id_link", False):
            raise OSError("HARNESS_CAPABILITY_UNAVAILABLE")

    def link_add(self) -> None:
        if not self._caps.get("by_id_link", False):
            raise OSError("HARNESS_CAPABILITY_UNAVAILABLE")

    def close(self) -> None:
        self.is_open = False

    def describe(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "transport_mode": self.transport_mode,
            "generation": self.generation,
            "peer_open": self._peer_open,
        }


class StubDutProcess:
    """測試用可腳本化 DUT 行程，依腳本回傳 stdin 接受長度。"""

    def __init__(
        self,
        script: list[int] | None = None,
        *,
        mode: str = "local-process",
    ) -> None:
        self.script = list(script or [])
        self.mode = mode
        self.generation = 1
        self.is_spawned = False
        self.stdin_written = bytearray()
        self.stdout_buf = bytearray()
        self.stderr_buf = bytearray()
        self._poll_rc: int | None = None
        self.stdin_closed = False

    def spawn(self) -> None:
        self.is_spawned = True
        self._poll_rc = None

    def stdin_write_some(self, data: bytes) -> int:
        if self._poll_rc is not None or self.stdin_closed:
            from uart_stdio_bridge.bridge.dut_process import DutStdinClosed

            raise DutStdinClosed("DUT stdin 已關閉")
        if not self.script:
            # 預設全部接受
            self.stdin_written.extend(data)
            return len(data)
        n = self.script.pop(0)
        if n == 0:
            return 0
        accepted = min(n, len(data))
        self.stdin_written.extend(data[:accepted])
        return accepted

    def stdout_read(self, max_bytes: int) -> bytes:
        if not self.stdout_buf:
            return b""
        chunk = bytes(self.stdout_buf[:max_bytes])
        del self.stdout_buf[:max_bytes]
        return chunk

    def stderr_read(self, max_bytes: int) -> bytes:
        if not self.stderr_buf:
            return b""
        chunk = bytes(self.stderr_buf[:max_bytes])
        del self.stderr_buf[:max_bytes]
        return chunk

    def close_stdin(self) -> None:
        self.stdin_closed = True

    def terminate(self, grace_s: float = 2.0) -> int | None:
        self._poll_rc = 0
        return 0

    def kill(self) -> int | None:
        self._poll_rc = -9
        return -9

    def poll(self) -> int | None:
        return self._poll_rc

    def respawn(self, grace_s: float = 2.0) -> int:
        self.generation += 1
        self.is_spawned = True
        self._poll_rc = None
        self.stdin_closed = False
        return self.generation

    def describe(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "argv": ["stub-dut"],
            "pid": 99999,
            "generation": self.generation,
        }


class BridgeProcessHandle:
    """受管 bridge 外部子行程控制物件。"""

    def __init__(
        self,
        proc: subprocess.Popen,
        link: str,
        evidence: Path,
        control_info: Path,
        cwd: Path,
    ) -> None:
        self.proc = proc
        self.link = link
        self.evidence = evidence
        self.control_info = control_info
        self.cwd = cwd
        self._control: Any = None

    @property
    def control(self) -> Any:
        if self._control is None:
            from uart_stdio_bridge.bridge.control import ControlClient

            self._control = ControlClient(self.control_info)
        return self._control

    def wait(self, timeout_s: float = 10.0) -> int:
        return self.proc.wait(timeout=timeout_s)

    def cleanup(self) -> None:
        if self._control is not None:
            try:
                self._control.call("shutdown")
            except Exception:
                pass
            try:
                self._control.close()
            except Exception:
                pass
        if self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=2.0)
            except Exception:
                try:
                    self.proc.kill()
                    self.proc.wait(timeout=1.0)
                except Exception:
                    pass


@pytest.fixture
def bridge_proc(tmp_path: Path):
    """啟動外部 bridge 行程的 fixture 工廠函式。"""
    handles: list[BridgeProcessHandle] = []

    def _spawn(
        *,
        run_id: str = "run-test",
        case_id: str = "case-test",
        port_id: str = "COM0",
        extra_dut_argv: list[str] | None = None,
        extra_bridge_argv: list[str] | None = None,
        env_override: dict[str, str] | None = None,
        allow_dut_exit: bool = False,
    ) -> BridgeProcessHandle:
        evidence_dir = tmp_path / "evidence"
        evidence_dir.mkdir(parents=True, exist_ok=True)
        by_id_dir = tmp_path / "by-id"
        by_id_dir.mkdir(parents=True, exist_ok=True)
        by_id_link = by_id_dir / "fake-uart0"

        if extra_dut_argv and "--mode" in extra_dut_argv:
            dut_cmd = [sys.executable, "-m", "uart_stdio_bridge.dut", *extra_dut_argv]
        elif extra_dut_argv:
            dut_cmd = [sys.executable, "-m", "uart_stdio_bridge.dut", "--mode", "echo", *extra_dut_argv]
        else:
            dut_cmd = [sys.executable, "-m", "uart_stdio_bridge.dut", "--mode", "echo"]

        cmd = [
            sys.executable,
            "-m",
            "uart_stdio_bridge",
            "run",
            "--run-id",
            run_id,
            "--case-id",
            case_id,
            "--port-id",
            port_id,
            "--evidence-dir",
            str(evidence_dir),
            "--endpoint",
            "posix-pty",
            "--by-id-link",
            str(by_id_link),
            "--dut-mode",
            "local-process",
            "--dut-argv-json",
            json.dumps(dut_cmd),
        ]
        if allow_dut_exit:
            cmd.append("--allow-dut-exit")
        if extra_bridge_argv:
            cmd.extend(extra_bridge_argv)

        env = dict(os.environ)
        existing_pythonpath = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = (
            f"{_SRC_DIR}:{existing_pythonpath}" if existing_pythonpath else str(_SRC_DIR)
        )
        if env_override:
            env.update(env_override)

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(_TOOL_ROOT),
            env=env,
            text=True,
        )

        control_info_path: Path | None = None
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                stdout, stderr = proc.communicate(timeout=1.0)
                raise RuntimeError(
                    f"Bridge 子行程過早結束（exitcode={proc.returncode}）：\nstdout: {stdout}\nstderr: {stderr}"
                )
            line = proc.stdout.readline() if proc.stdout else ""
            if line:
                try:
                    evt = json.loads(line.strip())
                    if evt.get("event") == "BRIDGE_READY":
                        control_info_path = Path(evt["control_info"])
                        break
                except json.JSONDecodeError:
                    pass
            time.sleep(0.02)

        if control_info_path is None:
            proc.kill()
            stdout, stderr = proc.communicate(timeout=1.0)
            raise TimeoutError(
                f"Bridge 未在超時內發出 BRIDGE_READY：\nstdout: {stdout}\nstderr: {stderr}"
            )

        handle = BridgeProcessHandle(
            proc=proc,
            link=str(by_id_link),
            evidence=evidence_dir,
            control_info=control_info_path,
            cwd=_TOOL_ROOT,
        )
        handles.append(handle)
        return handle

    yield _spawn

    for h in handles:
        h.cleanup()
