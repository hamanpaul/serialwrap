from __future__ import annotations

import errno
import fcntl
import os
import re
import select
import termios
import threading
import time
from typing import Any

from .config import UartProfile
from .wal import WalWriter

_BAUD_MAP = {
    9600: termios.B9600,
    19200: termios.B19200,
    38400: termios.B38400,
    57600: termios.B57600,
    115200: termios.B115200,
    230400: termios.B230400,
    460800: termios.B460800,
    921600: termios.B921600,
}


class UARTBridge:
    def __init__(self, com: str, device_path: str, profile: UartProfile, wal: WalWriter) -> None:
        self.com = com
        self.device_path = device_path
        self.profile = profile
        self.wal = wal

        self._serial_fd: int | None = None
        self._pty_master: int | None = None
        self._pty_slave: int | None = None
        self._pty_slave_path: str | None = None
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._write_lock = threading.Lock()
        self._rx_lock = threading.Lock()
        self._rx_text = ""
        self._rx_max_chars = 65536

    @property
    def vtty_path(self) -> str | None:
        return self._pty_slave_path

    def _set_nonblock(self, fd: int) -> None:
        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

    def _configure_serial(self, fd: int) -> None:
        attrs = termios.tcgetattr(fd)
        attrs[0] = 0
        attrs[1] = 0
        attrs[3] = 0

        cflag = termios.CREAD | termios.CLOCAL

        if self.profile.data_bits == 7:
            cflag |= termios.CS7
        else:
            cflag |= termios.CS8

        parity = self.profile.parity.upper()
        if parity == "E":
            cflag |= termios.PARENB
        elif parity == "O":
            cflag |= termios.PARENB | termios.PARODD

        if self.profile.stop_bits == 2:
            cflag |= termios.CSTOPB

        if self.profile.flow_control.lower() == "rtscts" and hasattr(termios, "CRTSCTS"):
            cflag |= termios.CRTSCTS

        attrs[2] = cflag

        speed = _BAUD_MAP.get(self.profile.baud, termios.B115200)
        if hasattr(termios, "cfsetispeed") and hasattr(termios, "cfsetospeed"):
            termios.cfsetispeed(attrs, speed)
            termios.cfsetospeed(attrs, speed)
        else:
            attrs[4] = speed
            attrs[5] = speed

        termios.tcsetattr(fd, termios.TCSANOW, attrs)

    def _configure_pty_slave(self, fd: int) -> None:
        # Disable line discipline echo/canonical mode to prevent
        # bridge RX bytes from being looped back as pseudo human TX.
        attrs = termios.tcgetattr(fd)
        attrs[0] = 0
        attrs[1] = 0
        attrs[2] = termios.CREAD | termios.CLOCAL | termios.CS8
        attrs[3] = 0
        attrs[6][termios.VMIN] = 1
        attrs[6][termios.VTIME] = 0
        termios.tcsetattr(fd, termios.TCSANOW, attrs)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return

        serial_fd = os.open(self.device_path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        self._configure_serial(serial_fd)
        self._set_nonblock(serial_fd)

        pty_master, pty_slave = os.openpty()
        self._set_nonblock(pty_master)
        self._configure_pty_slave(pty_slave)
        self._pty_slave_path = os.ttyname(pty_slave)

        self._serial_fd = serial_fd
        self._pty_master = pty_master
        self._pty_slave = pty_slave

        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, name=f"serialwrap-uart-{self.com}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)

        for fd in (self._serial_fd, self._pty_master, self._pty_slave):
            if fd is None:
                continue
            try:
                os.close(fd)
            except OSError:
                pass

        self._serial_fd = None
        self._pty_master = None
        self._pty_slave = None

    def _write_all(self, fd: int, payload: bytes) -> None:
        view = memoryview(payload)
        sent = 0
        while sent < len(payload):
            try:
                n = os.write(fd, view[sent:])
            except BlockingIOError:
                time.sleep(0.01)
                continue
            except OSError as exc:
                if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                    time.sleep(0.01)
                    continue
                raise
            if n <= 0:
                break
            sent += n

    def _append_rx_text(self, payload: bytes) -> None:
        text = payload.decode("utf-8", errors="replace")
        with self._rx_lock:
            self._rx_text += text
            if len(self._rx_text) > self._rx_max_chars:
                self._rx_text = self._rx_text[-self._rx_max_chars :]

    def _loop(self) -> None:
        assert self._serial_fd is not None
        assert self._pty_master is not None
        serial_fd = self._serial_fd
        pty_master = self._pty_master

        while not self._stop_event.is_set():
            try:
                rlist, _, _ = select.select([serial_fd, pty_master], [], [], 0.2)
            except OSError:
                break

            for fd in rlist:
                try:
                    data = os.read(fd, 8192)
                except BlockingIOError:
                    continue
                except OSError:
                    self._stop_event.set()
                    break

                if not data:
                    continue

                if fd == serial_fd:
                    self.wal.append(com=self.com, direction="RX", source="device", payload=data)
                    self._append_rx_text(data)
                    try:
                        self._write_all(pty_master, data)
                    except OSError:
                        pass
                else:
                    self.wal.append(com=self.com, direction="TX", source="human", payload=data)
                    try:
                        with self._write_lock:
                            self._write_all(serial_fd, data)
                    except OSError:
                        self._stop_event.set()
                        break

    def send_command(self, cmd: str, *, source: str, cmd_id: str | None = None) -> None:
        if self._serial_fd is None:
            raise RuntimeError("serial not ready")
        payload = cmd.encode("utf-8", errors="replace")
        if not payload.endswith(b"\n"):
            payload += b"\n"
        with self._write_lock:
            self._write_all(self._serial_fd, payload)
        self.wal.append(com=self.com, direction="TX", source=source, payload=payload, cmd_id=cmd_id)

    def send_secret(self, secret: str) -> None:
        if self._serial_fd is None:
            raise RuntimeError("serial not ready")
        payload = secret.encode("utf-8", errors="replace")
        if not payload.endswith(b"\n"):
            payload += b"\n"
        with self._write_lock:
            self._write_all(self._serial_fd, payload)

    def clear_rx_buffer(self) -> None:
        with self._rx_lock:
            self._rx_text = ""

    def wait_for_regex(self, pattern: str, timeout_s: float) -> bool:
        deadline = time.monotonic() + timeout_s
        regex = re.compile(pattern)
        while time.monotonic() < deadline:
            with self._rx_lock:
                snapshot = self._rx_text
            if regex.search(snapshot):
                return True
            time.sleep(0.05)
        return False

    def snapshot(self) -> dict[str, Any]:
        return {
            "com": self.com,
            "device_path": self.device_path,
            "vtty": self._pty_slave_path,
            "running": bool(self._thread and self._thread.is_alive()),
        }
