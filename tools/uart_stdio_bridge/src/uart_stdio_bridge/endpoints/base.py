"""外部 bridge 端點 adapter 的共用介面與錯誤型別。"""

from __future__ import annotations

from typing import Any, Protocol

TRANSPORT_MODES = ("virtual_pty", "virtual_com0com", "physical_usb_uart")


class EndpointError(OSError):
    """端點開啟或 I/O 操作失敗。"""


class EndpointWriteUncertain(EndpointError):
    """寫入逾時且作業系統未提供確切接受長度。"""

    def __init__(self, requested_len: int, detail: str) -> None:
        self.requested_len = requested_len
        self.detail = detail
        super().__init__(f"HARNESS_WRITE_UNCERTAIN: {detail}")


class Endpoint(Protocol):
    """POSIX PTY 與 Windows pySerial adapter 共用的最小介面。

    `disconnect()` 後、`close()` 前的傳輸空窗會在各自 timeout 內回傳空資料
    或 0；從未 `open()` 或已 `close()` 的端點則 raise `EndpointError`。
    """

    kind: str
    transport_mode: str
    generation: int

    def open(self) -> None: ...

    def read(self, max_bytes: int, timeout_s: float) -> bytes: ...

    def write_some(self, data: bytes, timeout_s: float) -> int: ...

    def peer_open(self) -> bool | None: ...

    def capabilities(self) -> dict[str, bool]: ...

    def disconnect(self, *, remove_link: bool) -> None: ...

    def reconnect(self) -> int: ...

    def link_remove(self) -> None: ...

    def link_add(self) -> None: ...

    def close(self) -> None: ...

    def describe(self) -> dict[str, Any]: ...


__all__ = ["TRANSPORT_MODES", "EndpointError", "EndpointWriteUncertain", "Endpoint"]
