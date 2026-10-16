"""PTY 假 DUT。

使用 pty.openpty() 與獨立 thread 狀態機模擬 DUT（開機 prompt、login、password、prompt、echo）。
stdlib-only，不得 import sw_core／serialwrap／serialwrapd。
"""
from __future__ import annotations

import os
import select
import threading
import time
from pathlib import Path


class FakeTarget:
    def __init__(
        self,
        workdir: Path,
        *,
        prompt: str = "root@fake:~# ",
        user: str = "root",
        password: str = "pw",
    ) -> None:
        self.workdir = Path(workdir)
        self.prompt = prompt
        self.user = user
        self.password = password
        self.master_fd: int | None = None
        self.slave_fd: int | None = None
        self.slave_path: str | None = None
        self.link_path: Path | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._rx_log = bytearray()
        self._lock = threading.Lock()
        self._thread_exc: BaseException | None = None

    def start(self, by_id_dir: Path, name: str = "usb-FAKE_TARGET_0-if00") -> Path:
        if os.name != "posix":
            raise RuntimeError("FakeTarget 僅支援 POSIX 平台")
        import pty
        import termios

        self.master_fd, self.slave_fd = pty.openpty()
        attrs = termios.tcgetattr(self.slave_fd)
        attrs[0] = 0
        attrs[1] = 0
        attrs[2] = termios.CREAD | termios.CLOCAL | termios.CS8
        attrs[3] = 0
        attrs[6][termios.VMIN] = 1
        attrs[6][termios.VTIME] = 0
        termios.tcsetattr(self.slave_fd, termios.TCSANOW, attrs)
        self.slave_path = os.ttyname(self.slave_fd)

        by_id_dir = Path(by_id_dir)
        by_id_dir.mkdir(parents=True, exist_ok=True)
        self.link_path = by_id_dir / name
        if self.link_path.is_symlink() or self.link_path.exists():
            self.link_path.unlink()
        os.symlink(self.slave_path, self.link_path)

        self._stop.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        return self.link_path

    def _run_loop(self) -> None:
        assert self.master_fd is not None
        try:
            os.write(self.master_fd, b"login: ")
        except OSError:
            return

        state = "LOGIN"
        line_buf = bytearray()
        prev_was_cr = False

        try:
            while not self._stop.is_set():
                try:
                    rlist, _, _ = select.select([self.master_fd], [], [], 0.05)
                except (OSError, ValueError):
                    break
                if not rlist:
                    continue
                try:
                    chunk = os.read(self.master_fd, 4096)
                except OSError:
                    break
                if not chunk:
                    break

                with self._lock:
                    self._rx_log.extend(chunk)

                for b in chunk:
                    byte = bytes([b])
                    if byte == b"\n" and prev_was_cr:
                        prev_was_cr = False
                        continue
                    prev_was_cr = (byte == b"\r")

                    if state == "LOGIN":
                        if byte in (b"\r", b"\n"):
                            line_str = line_buf.decode("utf-8", errors="replace")
                            line_buf.clear()
                            if line_str == self.user:
                                os.write(self.master_fd, b"\r\nPassword: ")
                                state = "PASSWORD"
                            else:
                                os.write(self.master_fd, b"\r\nlogin: ")
                        else:
                            line_buf.append(b)
                    elif state == "PASSWORD":
                        if byte in (b"\r", b"\n"):
                            line_str = line_buf.decode("utf-8", errors="replace")
                            line_buf.clear()
                            if line_str == self.password:
                                os.write(self.master_fd, b"\r\n" + self.prompt.encode("utf-8"))
                                state = "READY"
                            else:
                                os.write(self.master_fd, b"\r\nLogin incorrect\r\nlogin: ")
                                state = "LOGIN"
                        else:
                            line_buf.append(b)
                    elif state == "READY":
                        if byte == b"\x03":
                            line_buf.clear()
                            os.write(self.master_fd, b"^C\r\n" + self.prompt.encode("utf-8"))
                        elif byte in (b"\r", b"\n"):
                            cmd = line_buf.decode("utf-8", errors="replace")
                            line_buf.clear()
                            out = (cmd + "\r\n").encode("utf-8")
                            if cmd.startswith("echo "):
                                val = cmd[5:]
                                out += (val + "\r\n").encode("utf-8")
                            elif cmd == "echo":
                                out += b"\r\n"
                            out += self.prompt.encode("utf-8")
                            os.write(self.master_fd, out)
                        else:
                            line_buf.append(b)
        except OSError:
            pass
        except BaseException as e:
            self._thread_exc = e

    def rx_log(self, timeout_s: float = 1.0) -> bytes:
        if self._thread_exc is not None:
            raise RuntimeError(f"FakeTarget reader thread died: {self._thread_exc}")
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self._stop.is_set():
                break
            if self._thread is not None and not self._thread.is_alive():
                if self._thread_exc is not None:
                    raise RuntimeError(f"FakeTarget reader thread died: {self._thread_exc}")
                break
            if self.master_fd is None:
                break
            try:
                rlist, _, _ = select.select([self.master_fd], [], [], 0)
                if not rlist:
                    break
            except (OSError, ValueError):
                break
            time.sleep(0.01)

        if self._thread_exc is not None:
            raise RuntimeError(f"FakeTarget reader thread died: {self._thread_exc}")
        with self._lock:
            return bytes(self._rx_log)

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
            self._thread = None
        for fd in (self.master_fd, self.slave_fd):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        self.master_fd = None
        self.slave_fd = None
        if self.link_path and self.link_path.is_symlink():
            try:
                self.link_path.unlink()
            except OSError:
                pass
        self.link_path = None
