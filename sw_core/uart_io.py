from __future__ import annotations

import dataclasses
import errno
import fcntl
import os
import re
import select
import termios
import threading
import time
import uuid
from typing import Any, Callable

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

_STALE_CONSOLE_GRACE_S = 2.0


@dataclasses.dataclass
class ConsoleClient:
    client_id: str
    label: str
    master_fd: int
    slave_fd: int
    slave_path: str
    attached_at: float
    tx_buffer: bytearray = dataclasses.field(default_factory=bytearray)


class UARTBridge:
    def __init__(
        self,
        com: str,
        device_path: str,
        profile: UartProfile,
        wal: WalWriter,
        *,
        on_console_line: Callable[[str, str], None] | None = None,
        on_rx_data: Callable[[bytes], None] | None = None,
        on_bridge_down: Callable[[str], None] | None = None,
    ) -> None:
        self.com = com
        self.device_path = device_path
        self.profile = profile
        self.wal = wal
        self._on_console_line = on_console_line
        self._on_rx_data = on_rx_data
        self._on_bridge_down = on_bridge_down

        self._serial_fd: int | None = None
        self._primary_client_id: str | None = None
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._write_lock = threading.Lock()
        self._state_lock = threading.RLock()
        self._rx_lock = threading.Lock()
        self._rx_text = ""
        self._rx_max_chars = 131072
        self._clients: dict[str, ConsoleClient] = {}
        self._interactive_owner: str | None = None

    @property
    def vtty_path(self) -> str | None:
        with self._state_lock:
            if self._primary_client_id is None:
                return None
            client = self._clients.get(self._primary_client_id)
            return client.slave_path if client is not None else None

    def _set_nonblock(self, fd: int) -> None:
        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

    def _configure_serial(self, fd: int) -> None:
        attrs = termios.tcgetattr(fd)
        attrs[0] = 0
        attrs[1] = 0
        attrs[3] = 0

        cflag = termios.CREAD | termios.CLOCAL
        cflag |= termios.CS7 if self.profile.data_bits == 7 else termios.CS8

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
        attrs = termios.tcgetattr(fd)
        attrs[0] = 0
        attrs[1] = 0
        attrs[2] = termios.CREAD | termios.CLOCAL | termios.CS8
        attrs[3] = 0
        attrs[6][termios.VMIN] = 1
        attrs[6][termios.VTIME] = 0
        termios.tcsetattr(fd, termios.TCSANOW, attrs)

    def _create_console_client(self, label: str | None = None) -> ConsoleClient:
        master_fd, slave_fd = os.openpty()
        self._set_nonblock(master_fd)
        self._configure_pty_slave(slave_fd)
        client_id = uuid.uuid4().hex[:12]
        return ConsoleClient(
            client_id=client_id,
            label=(label or client_id).strip() or client_id,
            master_fd=master_fd,
            slave_fd=slave_fd,
            slave_path=os.ttyname(slave_fd),
            attached_at=time.time(),
        )

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return

        serial_fd = os.open(self.device_path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        self._configure_serial(serial_fd)
        self._set_nonblock(serial_fd)

        primary = self._create_console_client("primary")
        with self._state_lock:
            self._serial_fd = serial_fd
            self._clients = {primary.client_id: primary}
            self._primary_client_id = primary.client_id

        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, name=f"serialwrap-uart-{self.com}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive() and threading.current_thread() is not self._thread:
            self._thread.join(timeout=2.0)

        with self._state_lock:
            serial_fd = self._serial_fd
            clients = list(self._clients.values())
            self._serial_fd = None
            self._clients = {}
            self._primary_client_id = None
            self._interactive_owner = None

        if serial_fd is not None:
            try:
                os.close(serial_fd)
            except OSError:
                pass

        for client in clients:
            self._close_console_client(client)

    def _close_console_client(self, client: ConsoleClient) -> None:
        for fd in (client.master_fd, client.slave_fd):
            try:
                os.close(fd)
            except OSError:
                pass

    def _client_has_external_peer_locked(self, client: ConsoleClient) -> bool:
        self_pid = os.getpid()
        try:
            pids = os.listdir("/proc")
        except OSError:
            # Be conservative when procfs is unavailable.
            return True

        for pid_text in pids:
            if not pid_text.isdigit():
                continue
            pid = int(pid_text)
            if pid == self_pid:
                continue
            fd_dir = os.path.join("/proc", pid_text, "fd")
            try:
                fd_names = os.listdir(fd_dir)
            except OSError:
                continue
            for fd_name in fd_names:
                try:
                    target = os.readlink(os.path.join(fd_dir, fd_name))
                except OSError:
                    continue
                if target == client.slave_path:
                    return True
        return False

    def _drop_console_client(self, client_id: str) -> None:
        with self._state_lock:
            client = self._clients.pop(client_id, None)
            if client is None:
                return
            if self._primary_client_id == client_id:
                next_client = next(iter(self._clients.values()), None)
                self._primary_client_id = next_client.client_id if next_client is not None else None
            if self._interactive_owner == f"human:{client_id}":
                self._interactive_owner = None
        self._close_console_client(client)

    def _prune_stale_consoles_locked(self, *, now: float | None = None) -> list[ConsoleClient]:
        cutoff = time.time() if now is None else now
        stale: list[ConsoleClient] = []
        for client_id, client in list(self._clients.items()):
            if client_id == self._primary_client_id:
                continue
            if cutoff - client.attached_at < _STALE_CONSOLE_GRACE_S:
                continue
            if self._client_has_external_peer_locked(client):
                continue
            removed = self._clients.pop(client_id, None)
            if removed is None:
                continue
            if self._interactive_owner == f"human:{client_id}":
                self._interactive_owner = None
            stale.append(removed)
        return stale

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

    def _write_console_best_effort(self, fd: int, payload: bytes) -> None:
        try:
            os.write(fd, payload)
        except BlockingIOError:
            return
        except OSError as exc:
            if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                return
            raise

    def _append_rx_text(self, payload: bytes) -> None:
        text = payload.decode("utf-8", errors="replace")
        with self._rx_lock:
            self._rx_text += text
            if len(self._rx_text) > self._rx_max_chars:
                self._rx_text = self._rx_text[-self._rx_max_chars :]

    def _handle_serial_rx(self, data: bytes) -> None:
        self.wal.append(com=self.com, direction="RX", source="device", payload=data)
        self._append_rx_text(data)
        if self._on_rx_data is not None:
            self._on_rx_data(data)
        with self._state_lock:
            clients = list(self._clients.values())
        for client in clients:
            try:
                # Human consoles are best-effort views. Never let a slow or idle
                # PTY client stall the serial RX loop or block queued commands.
                self._write_console_best_effort(client.master_fd, data)
            except OSError:
                continue

    def _drain_line_buffer(self, client: ConsoleClient) -> list[str]:
        lines: list[str] = []
        while True:
            nl_positions = [pos for pos in (client.tx_buffer.find(b"\n"), client.tx_buffer.find(b"\r")) if pos >= 0]
            if not nl_positions:
                break
            pos = min(nl_positions)
            raw = bytes(client.tx_buffer[:pos])
            del client.tx_buffer[: pos + 1]
            while bytes(client.tx_buffer[:1]) in {b"\n", b"\r"}:
                del client.tx_buffer[:1]
            lines.append(raw.decode("utf-8", errors="replace"))
        return lines

    def _pop_last_console_char(self, buf: bytearray) -> bool:
        if not buf:
            return False
        idx = len(buf) - 1
        while idx > 0 and (buf[idx] & 0xC0) == 0x80:
            idx -= 1
        del buf[idx:]
        return True

    def _consume_console_input(self, client: ConsoleClient, data: bytes) -> tuple[list[str], bytes]:
        lines: list[str] = []
        echo = bytearray()
        last_terminator: int | None = None

        for b in data:
            if b in (0x08, 0x7F):
                last_terminator = None
                if self._pop_last_console_char(client.tx_buffer):
                    echo.extend(b"\b \b")
                continue

            if b in (0x0A, 0x0D):
                if last_terminator is not None and last_terminator != b and not client.tx_buffer:
                    last_terminator = None
                    continue
                lines.append(client.tx_buffer.decode("utf-8", errors="replace"))
                client.tx_buffer.clear()
                # Commit the local line visually without adding an extra blank
                # line before the target shell echoes the submitted command.
                echo.extend(b"\r")
                last_terminator = b
                continue

            last_terminator = None
            client.tx_buffer.append(b)
            if b == 0x09 or 0x20 <= b <= 0x7E or b >= 0x80:
                echo.append(b)

        return lines, bytes(echo)

    def _handle_console_rx(self, client: ConsoleClient, data: bytes) -> None:
        owner = None
        with self._state_lock:
            owner = self._interactive_owner
        if owner == f"human:{client.client_id}":
            self.send_bytes(data, source=f"human:{client.client_id}", cmd_id=None)
            return

        lines, echo = self._consume_console_input(client, data)
        if echo:
            try:
                self._write_console_best_effort(client.master_fd, echo)
            except OSError:
                pass
        if self._on_console_line is None:
            return
        for line in lines:
            self._on_console_line(client.client_id, line)

    def _loop(self) -> None:
        failure_reason: str | None = None
        while not self._stop_event.is_set():
            with self._state_lock:
                serial_fd = self._serial_fd
                clients_by_fd = {client.master_fd: client for client in self._clients.values()}

            if serial_fd is None:
                break

            read_fds = [serial_fd, *clients_by_fd.keys()]
            try:
                rlist, _, _ = select.select(read_fds, [], [], 0.2)
            except OSError as exc:
                failure_reason = f"SELECT:{exc.errno or type(exc).__name__}"
                break

            for fd in rlist:
                try:
                    data = os.read(fd, 8192)
                except BlockingIOError:
                    continue
                except OSError as exc:
                    if fd == serial_fd:
                        failure_reason = f"SERIAL_READ:{exc.errno or type(exc).__name__}"
                        self._stop_event.set()
                        break
                    client = clients_by_fd.get(fd)
                    if client is not None:
                        self._drop_console_client(client.client_id)
                    continue
                if not data:
                    if fd != serial_fd:
                        client = clients_by_fd.get(fd)
                        if client is not None:
                            self._drop_console_client(client.client_id)
                    continue
                if fd == serial_fd:
                    self._handle_serial_rx(data)
                    continue
                client = clients_by_fd.get(fd)
                if client is None:
                    continue
                self._handle_console_rx(client, data)
        if failure_reason and self._on_bridge_down is not None:
            threading.Thread(target=self._on_bridge_down, args=(failure_reason,), daemon=True).start()

    def send_bytes(self, payload: bytes, *, source: str, cmd_id: str | None = None, log: bool = True) -> None:
        with self._state_lock:
            serial_fd = self._serial_fd
        if serial_fd is None:
            raise RuntimeError("serial not ready")
        with self._write_lock:
            self._write_all(serial_fd, payload)
        if log:
            self.wal.append(com=self.com, direction="TX", source=source, payload=payload, cmd_id=cmd_id)

    def send_command(self, cmd: str, *, source: str, cmd_id: str | None = None) -> None:
        payload = cmd.encode("utf-8", errors="replace")
        if not payload.endswith(b"\n"):
            payload += b"\n"
        self.send_bytes(payload, source=source, cmd_id=cmd_id)

    def send_secret(self, secret: str) -> None:
        payload = secret.encode("utf-8", errors="replace")
        if not payload.endswith(b"\n"):
            payload += b"\n"
        self.send_bytes(payload, source="system:secret", cmd_id=None, log=False)

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

    def rx_snapshot_len(self) -> int:
        with self._rx_lock:
            return len(self._rx_text)

    def rx_text_from(self, from_offset: int) -> str:
        with self._rx_lock:
            return self._rx_text[from_offset:]

    def rx_tail(self, max_chars: int = 4096) -> str:
        with self._rx_lock:
            return self._rx_text[-max_chars:]

    def wait_for_regex_from(self, pattern: str, from_offset: int, timeout_s: float) -> bool:
        deadline = time.monotonic() + timeout_s
        regex = re.compile(pattern)
        while time.monotonic() < deadline:
            with self._rx_lock:
                snapshot = self._rx_text[from_offset:]
            if regex.search(snapshot):
                return True
            time.sleep(0.05)
        return False

    def attach_console(self, *, label: str | None = None) -> dict[str, Any]:
        client = self._create_console_client(label)
        stale: list[ConsoleClient] = []
        with self._state_lock:
            stale = self._prune_stale_consoles_locked(now=client.attached_at)
            self._clients[client.client_id] = client
            if self._primary_client_id is None:
                self._primary_client_id = client.client_id
        for row in stale:
            self._close_console_client(row)
        return {
            "client_id": client.client_id,
            "label": client.label,
            "vtty": client.slave_path,
        }

    def detach_console(self, client_id: str) -> bool:
        with self._state_lock:
            client = self._clients.pop(client_id, None)
            if client is None:
                return False
            if self._primary_client_id == client_id:
                next_client = next(iter(self._clients.values()), None)
                self._primary_client_id = next_client.client_id if next_client is not None else None
            if self._interactive_owner == f"human:{client_id}":
                self._interactive_owner = None
        self._close_console_client(client)
        return True

    def list_consoles(self) -> list[dict[str, Any]]:
        stale: list[ConsoleClient] = []
        with self._state_lock:
            stale = self._prune_stale_consoles_locked()
            owner = self._interactive_owner
            rows = [
                {
                    "client_id": client.client_id,
                    "label": client.label,
                    "vtty": client.slave_path,
                    "interactive_owner": owner == f"human:{client.client_id}",
                }
                for client in sorted(self._clients.values(), key=lambda row: (row.label, row.client_id))
            ]
        for row in stale:
            self._close_console_client(row)
        return rows

    def console_has_external_peer(self, client_id: str) -> bool:
        with self._state_lock:
            client = self._clients.get(client_id)
            if client is None:
                return False
            return self._client_has_external_peer_locked(client)

    def set_interactive_owner(self, owner: str | None) -> None:
        with self._state_lock:
            self._interactive_owner = owner

    def snapshot(self) -> dict[str, Any]:
        consoles = self.list_consoles()
        with self._state_lock:
            serial_fd = self._serial_fd
            primary_client_id = self._primary_client_id
            primary = None
            if primary_client_id is not None:
                client = self._clients.get(primary_client_id)
                if client is not None:
                    primary = client.slave_path
            interactive_owner = self._interactive_owner
        serial_alive = False
        if serial_fd is not None:
            try:
                os.fstat(serial_fd)
                serial_alive = True
            except OSError:
                serial_alive = False
        vtty_alive = bool(primary and os.path.exists(primary))
        return {
            "com": self.com,
            "device_path": self.device_path,
            "vtty": primary,
            "serial_alive": serial_alive,
            "vtty_alive": vtty_alive,
            "interactive_owner": interactive_owner,
            "consoles": consoles,
            "running": bool(self._thread and self._thread.is_alive()),
        }
