"""Slice 2.2 console 黑箱小工具（stdlib only；07 Task 11 Produces）。"""
from __future__ import annotations

import base64
import os
import select
import time


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
