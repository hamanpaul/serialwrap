"""S1X-a-1：`mode=interactive` 命令必須以 `interactive:<lease_id>` permit 送出。

Stage 1 出口審查 A 指出 `_execute_command_inner` 的 interactive 分支以 `cmd_id` 當
permit op_id，與 `_open_interactive_locked` 發佈的 lease owner（`interactive:<id>`）不符，
`_tx_reject_reason` 必回 `lease`：UART 零寫入、回 `SEND_FAILED`，且剛開的 lease 遺留，
下一個命令得到 `SESSION_INTERACTIVE_BUSY`。0.3.2 會回 ok:true 並寫出命令。

本檔以真 `UARTBridge`＋`FakePort` 送非空 interactive 命令釘住 0.3.2 行為，並涵蓋
human line console（第二個以後的 console）經 `_human_console_mode` 判為 interactive 的路徑，
以及送出被拒時須關閉剛開的 lease。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from stage0_fakes import FakePort, make_bridge, make_manager, make_profile, wal_records

pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]

BY_ID = "/dev/serial/by-id/X"
GENERATION = 4


def _ready(tmp_path: Path, port: FakePort) -> tuple[Any, Any, Any]:
    """建立已 attach 為 READY 的 session（同 test_stage0_tx_permit::_ready 的最小版本）。"""
    from sw_core.device_watcher import DeviceInfo

    mgr = make_manager(tmp_path, [make_profile(by_id=BY_ID)])
    s = mgr.get_session("COM0")
    assert s is not None
    bridge, _wal = make_bridge(
        tmp_path,
        port=port,
        wal=mgr._wal,
        on_rx_data=lambda data, wal_seq=None, sid=s.session_id: mgr._on_bridge_rx(
            sid, data, wal_seq
        ),
    )
    mgr._devices[BY_ID] = DeviceInfo(by_id=BY_ID, real_path="fake-uart")
    real_snapshot = bridge.snapshot

    def snapshot_as_attached() -> dict[str, Any]:
        snapshot = real_snapshot()
        snapshot.update(running=True, serial_alive=True, vtty_alive=True, vtty="fake-vtty")
        return snapshot

    bridge.snapshot = snapshot_as_attached
    with mgr._lock:
        s.bridge = bridge
        s.state = "READY"
        s.bridge_generation = GENERATION
    bridge.set_tx_gate(generation=GENERATION, quiet=False, lease_owner=None)
    return mgr, s, bridge


def _tx_payloads(mgr: Any) -> list[bytes]:
    import base64

    return [
        base64.b64decode(r["payload_b64"])
        for r in wal_records(mgr._wal)
        if r.get("dir") == "TX"
    ]


def test_agent_interactive_command_writes_bytes_and_returns_ok(tmp_path: Path) -> None:
    """agent `cmd submit --mode interactive "top"`：0.3.2 回 ok:true 並寫出 `top\\n`。"""
    port = FakePort()
    mgr, s, _bridge = _ready(tmp_path, port)

    result = mgr.execute_command(s.session_id, "top", "agent:test", "cmd-i1", mode="interactive")

    assert result["ok"] is True, result
    assert result["execution_mode"] == "interactive"
    assert result["status"] == "interactive"
    lease_id = result["interactive_session_id"]
    assert lease_id and s.interactive_session_id == lease_id
    assert b"".join(port.writes) == b"top\n"
    assert b"".join(_tx_payloads(mgr)) == b"top\n"


def test_interactive_lease_usable_after_interactive_command(tmp_path: Path) -> None:
    """interactive 命令開出的 lease 可續用，關閉後下一個命令不會拿到 SESSION_INTERACTIVE_BUSY。"""
    port = FakePort()
    mgr, s, _bridge = _ready(tmp_path, port)

    result = mgr.execute_command(s.session_id, "top", "agent:test", "cmd-i2", mode="interactive")
    assert result["ok"] is True, result
    lease_id = result["interactive_session_id"]

    sent = mgr.interactive_send(lease_id, data="q", encoding="plain")
    assert sent["ok"] is True, sent
    assert b"".join(port.writes) == b"top\nq"

    closed = mgr.interactive_close(lease_id)
    assert closed["ok"] is True, closed
    nxt = mgr.execute_command(
        s.session_id, "top", "agent:test", "cmd-i3", mode="interactive"
    )
    assert nxt.get("error_code") != "SESSION_INTERACTIVE_BUSY", nxt
    assert nxt["ok"] is True, nxt


def test_human_line_console_interactive_program_reaches_uart(tmp_path: Path) -> None:
    """human line console 輸入 `vim` 經 `_human_console_mode` 判為 interactive，須送達 UART。"""
    from sw_core.service import _human_console_mode

    assert _human_console_mode("vim /tmp/x") == "interactive"
    port = FakePort()
    mgr, s, _bridge = _ready(tmp_path, port)

    result = mgr.execute_command(
        s.session_id,
        "vim /tmp/x",
        "human:c2",
        "cmd-h1",
        mode=_human_console_mode("vim /tmp/x"),
        trusted_human=True,
    )

    assert result["ok"] is True, result
    assert result["execution_mode"] == "interactive"
    assert b"".join(port.writes) == b"vim /tmp/x\n"


def test_rejected_interactive_send_closes_opened_lease(tmp_path: Path) -> None:
    """送出被拒（零 TX）時須關閉剛開的 lease：之後的命令不得被遺留 lease 卡成 BUSY。"""
    port = FakePort()
    mgr, s, bridge = _ready(tmp_path, port)
    bridge._tx_frozen = True  # 模擬 drain fence：提交點以 draining 拒絕

    result = mgr.execute_command(s.session_id, "top", "agent:test", "cmd-r1", mode="interactive")

    assert result["ok"] is False, result
    assert port.writes == []
    assert s.interactive_session_id is None
    assert getattr(bridge, "_gate_lease_owner", None) is None

    bridge._tx_frozen = False
    nxt = mgr.execute_command(s.session_id, "top", "agent:test", "cmd-r2", mode="interactive")
    assert nxt.get("error_code") != "SESSION_INTERACTIVE_BUSY", nxt
    assert nxt["ok"] is True, nxt
    assert b"".join(port.writes) == b"top\n"


def test_rejected_send_does_not_close_lease_replaced_meanwhile(tmp_path: Path) -> None:
    """送出被拒時，若 session 的 lease 已不是本命令所開（期間被收回／更換），不得誤關現行 lease。"""
    port = FakePort()
    mgr, s, bridge = _ready(tmp_path, port)
    original_send = mgr._send_command_with_permit

    def swap_then_reject(*args: Any, **kwargs: Any) -> Any:
        with mgr._lock:
            s.interactive_session_id = "other-lease"
        bridge._tx_frozen = True
        return original_send(*args, **kwargs)

    mgr._send_command_with_permit = swap_then_reject  # type: ignore[method-assign]

    result = mgr.execute_command(s.session_id, "top", "agent:test", "cmd-x1", mode="interactive")

    assert result["ok"] is False, result
    assert s.interactive_session_id == "other-lease"
