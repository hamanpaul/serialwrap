"""Slice 2.2 console 黑箱小工具（stdlib only；07 Task 11 Produces）。"""
from __future__ import annotations

import base64
import os
import select
import socket
import time
from collections.abc import Mapping


def open_vtty(path: str) -> int:
    return os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)


def read_for(fd: int, seconds: float) -> bytes:
    out = bytearray()
    deadline = time.monotonic() + seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return bytes(out)
        ready, _, _ = select.select([fd], [], [], remaining)
        if not ready:
            continue
        try:
            chunk = os.read(fd, 65536)
        except BlockingIOError:
            continue
        except OSError:
            return bytes(out)
        if not chunk:
            return bytes(out)
        out += chunk


def read_until(fd: int, needle: bytes, timeout_s: float) -> bytes:
    out = bytearray()
    deadline = time.monotonic() + timeout_s
    while needle not in out and time.monotonic() < deadline:
        out += read_for(fd, 0.05)
    return bytes(out)


def telnet_connect(endpoint: str) -> socket.socket:
    """連 `"127.0.0.1:<port>"` 的 Telnet console（非阻塞讀取用 `sock_read_for`）。"""
    host, port = endpoint.rsplit(":", 1)
    sock = socket.create_connection((host, int(port)), timeout=3.0)
    sock.setblocking(False)
    return sock


def console_kind(attach_resp: Mapping[str, object]) -> str:
    """依 attach 回應判定 console 使用 TCP 或 PTY。"""
    transport = attach_resp.get("transport")
    if transport == "tcp":
        return "tcp"
    if transport == "pty" or (transport is None and attach_resp.get("vtty")):
        return "pty"
    raise AssertionError(f"未知 console transport：{attach_resp!r}")


def open_console(attach_resp: Mapping[str, object]) -> socket.socket | int:
    """依 attach 回應連入 console；Windows 使用 TCP socket，POSIX 使用 PTY。"""
    if console_kind(attach_resp) == "tcp":
        endpoint = attach_resp.get("endpoint")
        if not isinstance(endpoint, str):
            raise AssertionError(f"TCP console 缺少 endpoint：{attach_resp!r}")
        return telnet_connect(endpoint)
    vtty = attach_resp.get("vtty")
    if not isinstance(vtty, str):
        raise AssertionError(f"PTY console 缺少 vtty：{attach_resp!r}")
    return open_vtty(vtty)


def write_console(client: socket.socket | int, data: bytes) -> None:
    if isinstance(client, socket.socket):
        client.sendall(data)
    else:
        os.write(client, data)


def close_console(client: socket.socket | int) -> None:
    if isinstance(client, socket.socket):
        client.close()
    else:
        os.close(client)


def sock_read_for(sock: socket.socket, seconds: float) -> bytes:
    out = bytearray()
    deadline = time.monotonic() + seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return bytes(out)
        ready, _, _ = select.select([sock], [], [], remaining)
        if not ready:
            continue
        try:
            chunk = sock.recv(65536)
        except BlockingIOError:
            continue
        except OSError:
            return bytes(out)
        if not chunk:
            return bytes(out)
        out += chunk


def sock_read_until(sock: socket.socket, needle: bytes, timeout_s: float, buf: bytearray | None = None) -> bytes:
    out = buf if buf is not None else bytearray()
    deadline = time.monotonic() + timeout_s
    while needle not in out and time.monotonic() < deadline:
        out += sock_read_for(sock, 0.05)
    return bytes(out)


def b64_payload(record: dict) -> bytes:
    return base64.b64decode(record.get("payload_b64") or "")


def tx_payload(wal: list[dict], source: str) -> bytes:
    rows = sorted((r for r in wal if r.get("dir") == "TX" and r.get("source") == source), key=lambda r: r["seq"])
    return b"".join(b64_payload(r) for r in rows)


def wait_rx_contains(target, needle: bytes, timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if needle in target.written(0.05):
            return True
        time.sleep(0.05)
    return False


def wait_for(cond, timeout_s: float, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(interval)
    return bool(cond())
