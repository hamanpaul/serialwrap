"""可組態的 PTY fake target（基於 DutModel 與 POSIX PTY）。

內部採用 DutModel 狀態機，透過 PTY master/slave 提供與既有 FakeTarget 完全相容的公開介面。
"""
from __future__ import annotations

import os
import pathlib
import pty
import select
import sys
import termios
import threading
import time

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_BRIDGE_SRC = _ROOT / "tools" / "uart_stdio_bridge" / "src"
if str(_BRIDGE_SRC) not in sys.path:
    sys.path.insert(0, str(_BRIDGE_SRC))

try:
    from .target_config import CommandOverride, LoginStep, TargetConfig
except ImportError:
    from target_config import CommandOverride, LoginStep, TargetConfig
from uart_stdio_bridge.dut.model import DutModel, Emit

__all__ = [
    "CommandOverride",
    "FakeTarget",
    "LoginStep",
    "TargetConfig",
]


class FakeTarget:
    """PTY 假裝置，模擬 UART target 的行為。

    使用方式::

        cfg = TargetConfig.from_yaml(yaml_target_section)
        target = FakeTarget(cfg)
        target.start()
        # ... 使用 target.slave_path 給 daemon ...
        target.stop()
    """

    def __init__(self, config: TargetConfig | None = None) -> None:
        self.config = config or TargetConfig()
        self._model = DutModel(self.config)
        self.master_fd, self.slave_fd = pty.openpty()
        self._configure_raw(self.slave_fd)
        self.slave_path: str = os.ttyname(self.slave_fd)
        self._stop = threading.Event()
        self._io_thread = threading.Thread(
            target=self._io_loop, daemon=True, name="fake-target-io"
        )
        self._lock = threading.Lock()

    @property
    def model(self) -> DutModel:
        """取得內部 DutModel 實例。"""
        return self._model

    @staticmethod
    def _configure_raw(fd: int) -> None:
        attrs = termios.tcgetattr(fd)
        attrs[0] = 0  # iflag
        attrs[1] = 0  # oflag
        attrs[2] = termios.CREAD | termios.CLOCAL | termios.CS8  # cflag
        attrs[3] = 0  # lflag
        attrs[6][termios.VMIN] = 1
        attrs[6][termios.VTIME] = 0
        termios.tcsetattr(fd, termios.TCSANOW, attrs)

    def start(self) -> None:
        """啟動 target，送出 boot banner 並啟動 I/O 迴圈。"""
        now_ms = int(time.monotonic() * 1000)
        with self._lock:
            boot_emits = self._model.boot(now_ms)
            for emit in boot_emits:
                self._write(emit.data)
        self._io_thread.start()

    def stop(self) -> None:
        """停止 target 並關閉 PTY。"""
        self._stop.set()
        if self._io_thread.is_alive():
            self._io_thread.join(timeout=2.0)
        for fd in (self.master_fd, self.slave_fd):
            try:
                os.close(fd)
            except OSError:
                pass

    def pause_responding(self, duration_s: float) -> None:
        """暫停回應（模擬 target hang）。"""
        now_ms = int(time.monotonic() * 1000)
        duration_ms = int(duration_s * 1000)
        with self._lock:
            self._model.pause_responding(now_ms, duration_ms)

    def _write(self, data: bytes) -> None:
        if not data:
            return
        try:
            offset = 0
            while offset < len(data):
                n = os.write(self.master_fd, data[offset:])
                if n <= 0:
                    break
                offset += n
        except OSError:
            pass

    def _io_loop(self) -> None:
        while not self._stop.is_set():
            now_ms = int(time.monotonic() * 1000)
            with self._lock:
                next_d = self._model.next_deadline_ms()
                poll_emits = self._model.poll(now_ms)
                for emit in poll_emits:
                    self._write(emit.data)

            if next_d is not None:
                timeout_s = max(0.0, (next_d - now_ms) / 1000.0)
                timeout_s = min(timeout_s, 0.05)
            else:
                timeout_s = 0.05

            try:
                rlist, _, _ = select.select([self.master_fd], [], [], timeout_s)
            except (OSError, ValueError):
                return

            if self._stop.is_set():
                break

            if self.master_fd in rlist:
                try:
                    chunk = os.read(self.master_fd, 4096)
                except (BlockingIOError, OSError):
                    chunk = b""

                if chunk:
                    now_ms = int(time.monotonic() * 1000)
                    with self._lock:
                        feed_emits = self._model.feed(chunk, now_ms)
                        for emit in feed_emits:
                            self._write(emit.data)
                        poll_emits = self._model.poll(now_ms)
                        for emit in poll_emits:
                            self._write(emit.data)
