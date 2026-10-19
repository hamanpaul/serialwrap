"""Console PTY 模擬。

模擬 human console 的 attach / write / read / detach 操作，
透過 CLI RPC 與 daemon 通訊。
"""
from __future__ import annotations

import os
import pathlib
import select
import socket
import sys
import time
from dataclasses import dataclass
from typing import Any

from .cli_client import cli_run

_ROOT_DIR = pathlib.Path(__file__).resolve().parents[2]
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))
from realhw.telnet_client import TelnetClientCodec

_TELNET_READ_QUIET_S = 0.5


@dataclass
class ConsoleHandle:
    """代表一個已 attach 的 console。"""
    client_id: str
    vtty: str
    label: str
    fd: int | None = None  # 開啟 vtty 後的 file descriptor

    def open_fd(self) -> None:
        if self.fd is None:
            self.fd = os.open(self.vtty, os.O_RDWR | os.O_NONBLOCK)

    def close_fd(self) -> None:
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None


def attach_console(
    selector: str,
    label: str,
    *,
    socket_path: str,
    env: dict[str, str],
) -> dict[str, Any]:
    """透過 CLI attach console，回傳包含 ConsoleHandle 的結果。"""
    resp = cli_run(
        ["session", "console-attach", "--selector", selector, "--label", label],
        socket_path=socket_path,
        env=env,
    )
    if resp.get("ok"):
        handle = ConsoleHandle(
            client_id=resp["client_id"],
            vtty=resp["vtty"],
            label=label,
        )
        handle.open_fd()
        resp["_handle"] = handle
    return resp


def detach_console(
    selector: str,
    client_id: str,
    *,
    socket_path: str,
    env: dict[str, str],
    handle: ConsoleHandle | None = None,
) -> dict[str, Any]:
    """透過 CLI detach console。"""
    response = cli_run(
        ["session", "console-detach", "--selector", selector, "--client-id", client_id],
        socket_path=socket_path,
        env=env,
    )
    if handle is not None:
        handle.close_fd()
    return response


def console_write(handle: ConsoleHandle, data: str | bytes, *, delay_ms: int = 0) -> None:
    """寫入資料到 console PTY。"""
    if handle.fd is None:
        handle.open_fd()
    assert handle.fd is not None
    payload = data.encode("utf-8") if isinstance(data, str) else bytes(data)
    if delay_ms > 0:
        for byte in payload:
            os.write(handle.fd, bytes((byte,)))
            time.sleep(delay_ms / 1000.0)
    else:
        os.write(handle.fd, payload)


def console_read(
    handle: ConsoleHandle,
    *,
    timeout_s: float = 5.0,
    max_bytes: int = 65536,
) -> str:
    """從 console PTY 讀取所有可用資料。"""
    if handle.fd is None:
        handle.open_fd()
    assert handle.fd is not None

    collected = b""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        remaining = max(0.01, deadline - time.monotonic())
        try:
            rlist, _, _ = select.select([handle.fd], [], [], min(remaining, 0.1))
        except (OSError, ValueError):
            break
        if handle.fd in rlist:
            try:
                chunk = os.read(handle.fd, max_bytes)
                if chunk:
                    collected += chunk
                    continue
            except BlockingIOError:
                pass
            except OSError:
                break
        # 短暫沒資料，若已有收集就結束
        if collected and handle.fd not in rlist:
            break
    return collected.decode("utf-8", errors="replace")


class TelnetConsole:
    """以 socket-backed Telnet client 連接 Windows daemon console。"""

    def __init__(self, endpoint: str, *, timeout_s: float = 5.0) -> None:
        address = endpoint.removeprefix("tcp://")
        host, separator, port_text = address.rpartition(":")
        if not separator or not host or not port_text.isdigit():
            raise ValueError(f"無效的 Telnet endpoint：{endpoint}")
        self.timeout_s = float(timeout_s)
        self._socket = socket.create_connection((host, int(port_text)), timeout=self.timeout_s)
        self._socket.settimeout(self.timeout_s)
        self._codec = TelnetClientCodec()
        self._buffer = bytearray()
        self._closed = False
        self.client_id = ""
        self.vtty = endpoint
        self.label = ""

    def _receive(self, timeout_s: float) -> bool:
        if self._closed:
            return False
        readable, _, _ = select.select([self._socket], [], [], max(0.0, timeout_s))
        if not readable:
            return False
        try:
            chunk = self._socket.recv(65536)
        except BlockingIOError:
            return False
        if not chunk:
            self.close()
            return False
        payload, reply = self._codec.feed(chunk)
        if reply:
            self._socket.sendall(reply)
        self._buffer.extend(payload)
        return True

    def write(self, data: bytes, *, delay_ms: int = 0) -> None:
        """先處理已到達的 option greeting，再傳送跳脫後的應用資料。"""
        if self._closed:
            raise OSError("Telnet console 已關閉")
        self._receive(min(0.1, self.timeout_s))
        encoded = self._codec.encode(data)
        if delay_ms > 0:
            for index, byte in enumerate(data):
                self._socket.sendall(self._codec.encode(bytes((byte,))))
                if index + 1 < len(data):
                    time.sleep(delay_ms / 1000.0)
        elif encoded:
            self._socket.sendall(encoded)

    def read(self, timeout_s: float, *, max_bytes: int = 65536) -> bytes:
        """回傳已移除協商指令與 subnegotiation 的資料。"""
        if max_bytes <= 0:
            return b""
        deadline = time.monotonic() + max(0.0, timeout_s)
        collected = bytearray(self._buffer[:max_bytes])
        del self._buffer[:len(collected)]
        while len(collected) < max_bytes and not self._closed:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            wait_s = min(remaining, _TELNET_READ_QUIET_S) if collected else remaining
            received = self._receive(wait_s)
            available = max_bytes - len(collected)
            if self._buffer:
                collected.extend(self._buffer[:available])
                del self._buffer[:available]
            if len(collected) >= max_bytes:
                break
            if not received and collected:
                break
        return bytes(collected)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self._socket.close()

    def close_fd(self) -> None:
        """提供與 POSIX ConsoleHandle 相同的回收介面。"""
        self.close()
