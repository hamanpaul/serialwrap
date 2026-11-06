"""S1-6485e90-1：interactive permit 換成 `interactive:<lease_id>` 後，原命令的取消仍須擋下 TX。

sol 增量審查（62ffec7..6485e90）指出：`9cb3c4c` 只在送出前於鎖外讀一次原 `cmd_id` 的
撤銷集合。若取消在該檢查之後、提交點之前才落入撤銷帳本，命令以新的 lease permit 送出，
提交點不檢查原 `cmd_id`，取消後仍寫出 bytes。修正後 interactive permit 帶著原命令的取消
身分，由 TX fence 內的提交點同時檢查原 `cmd_id` 與 lease op 的撤銷狀態。

本檔以 bridge 的 `_after_tx_authorize` 測試鉤子（pre-check 通過後、fence 提交前）讓取消
恰好落在該窗口，重現審查所述交錯。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from stage0_fakes import FakePort
from test_s1x_a1_interactive_permit import _ready, _tx_payloads

pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]


def test_cancel_after_precheck_blocks_interactive_tx(tmp_path: Path) -> None:
    """取消落在 pre-check 與 fence 提交之間：零 TX、FLUSHED_BY_RECOVERY，並關閉本命令的 lease。"""
    port = FakePort()
    mgr, s, bridge = _ready(tmp_path, port)
    cmd_id = "cmd-race"
    bridge._after_tx_authorize = lambda: bridge.revoke_ops([cmd_id])

    result = mgr.execute_command(s.session_id, "top", "agent:test", cmd_id, mode="interactive")

    assert result["ok"] is False, result
    assert result["error_code"] == "FLUSHED_BY_RECOVERY", result
    assert port.writes == []
    assert _tx_payloads(mgr) == []
    assert s.interactive_session_id is None


def test_lease_op_cancel_identity_does_not_leak_to_later_sends(tmp_path: Path) -> None:
    """取消身分只綁本命令：原 cmd_id 被撤銷不影響之後以同一 lease 的 interactive_send。"""
    port = FakePort()
    mgr, s, bridge = _ready(tmp_path, port)

    result = mgr.execute_command(s.session_id, "top", "agent:test", "cmd-ok", mode="interactive")
    assert result["ok"] is True, result
    bridge.revoke_ops(["cmd-ok"])

    sent = mgr.interactive_send(result["interactive_session_id"], data="q", encoding="plain")
    assert sent["ok"] is True, sent
    assert b"".join(port.writes) == b"top\nq"
