"""Root 驗證：FLASHING 不得被 raw deferred gate 改成延後回放。"""
from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest import mock

from sw_core.config import UartProfile
from sw_core.uart_io import UARTBridge
from sw_core.wal import WalWriter


def test_flashing_after_raw_snapshot_drops_input_instead_of_deferring(tmp_path: Path) -> None:
    bridge = UARTBridge(
        "COM0", "/dev/serial/by-id/fake", UartProfile(), WalWriter(wal_dir=str(tmp_path))
    )
    attached = bridge.attach_console(label="flash-priority")
    client_id = attached["client_id"]
    bridge.set_interactive_owner(f"human:{client_id}")
    original_send = bridge.send_bytes

    def enter_flash_before_write(payload: bytes, **kwargs: Any) -> None:
        bridge._set_interactive_admission(False)
        bridge.set_flash_mode(True)
        original_send(payload, **kwargs)

    try:
        with mock.patch.object(bridge, "send_bytes", side_effect=enter_flash_before_write):
            bridge._handle_console_rx(bridge._clients[client_id], b"must-not-replay")
        assert bridge._deferred_buffers == {}
        assert bridge._set_interactive_admission(True) == []
        assert not hasattr(bridge._console_raw_context, "token")
    finally:
        bridge.set_flash_mode(False)
        bridge.stop()
