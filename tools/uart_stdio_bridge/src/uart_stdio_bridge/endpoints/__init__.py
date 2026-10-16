"""外部 bridge 的 POSIX PTY 與 Windows pySerial 端點選擇器。"""

from __future__ import annotations

import pathlib
from typing import Any

from .base import Endpoint, EndpointError, EndpointWriteUncertain, TRANSPORT_MODES


def open_endpoint(spec: dict[str, Any]) -> Endpoint:
    """依宣告式 spec 建立並開啟 adapter；平台專屬模組只在選用時載入。"""
    if not isinstance(spec, dict):
        raise ValueError("endpoint spec 必須是 dict")

    kind = spec.get("kind")
    if kind == "posix-pty":
        from .posix_pty import PosixPtyEndpoint

        by_id_link = spec.get("by_id_link")
        if not isinstance(by_id_link, (str, pathlib.Path)):
            raise ValueError("posix-pty spec 必須提供 by_id_link")
        endpoint = PosixPtyEndpoint(
            pathlib.Path(by_id_link),
            peer_gate=spec.get("peer_gate", True),
        )
        endpoint.open()
        return endpoint
    if kind == "win-serial":
        from .win_serial import WinSerialEndpoint

        com = spec.get("com")
        if not isinstance(com, str) or not com:
            raise ValueError("win-serial spec 必須提供 com")
        endpoint = WinSerialEndpoint(
            com,
            baud=spec.get("baud", 115200),
            transport_mode=spec.get("transport_mode", "virtual_com0com"),
            allow_peer_close=spec.get("allow_peer_close", False),
        )
        endpoint.open()
        return endpoint
    raise ValueError(f"不支援的 endpoint kind：{kind!r}")


__all__ = [
    "TRANSPORT_MODES",
    "Endpoint",
    "EndpointError",
    "EndpointWriteUncertain",
    "open_endpoint",
]
