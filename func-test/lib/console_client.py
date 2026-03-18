"""Console PTY 模擬。

模擬 human console 的 attach / write / read / detach 操作，
透過 CLI RPC 與 daemon 通訊。
"""
from __future__ import annotations

import os
import select
import time
from dataclasses import dataclass
from typing import Any

from .cli_client import cli_run


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
    if handle is not None:
        handle.close_fd()
    return cli_run(
        ["session", "console-detach", "--selector", selector, "--client-id", client_id],
        socket_path=socket_path,
        env=env,
    )


def console_write(handle: ConsoleHandle, data: str, *, delay_ms: int = 0) -> None:
    """寫入資料到 console PTY。"""
    if handle.fd is None:
        handle.open_fd()
    assert handle.fd is not None
    if delay_ms > 0:
        for ch in data:
            os.write(handle.fd, ch.encode("utf-8"))
            time.sleep(delay_ms / 1000.0)
    else:
        os.write(handle.fd, data.encode("utf-8"))


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
