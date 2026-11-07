"""S1M-B1：取消只屬於原命令，不得撤銷已完成 TX 後仍存活的 interactive lease。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from stage0_fakes import FakePort
from test_s1x_a1_interactive_permit import _ready, _tx_payloads

pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]


def test_cancel_after_interactive_tx_before_executor_return_preserves_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TX 已完成但 foreground operation 尚未收尾時取消，後續兩次 send 仍須送達。"""
    port = FakePort()
    mgr, session, _bridge = _ready(tmp_path, port)
    cmd_id = "cmd-b1-late-cancel"
    execute_inner = mgr._execute_command_inner

    def cancel_after_tx(*args: Any, **kwargs: Any) -> dict[str, Any]:
        result = execute_inner(*args, **kwargs)
        assert result["ok"] is True, result
        assert b"".join(port.writes) == b"top\n"
        assert session._foreground_operation is not None
        assert session._foreground_operation.cmd_id == cmd_id
        assert session.interactive_session_id == result["interactive_session_id"]
        mgr.revoke_tx_op(cmd_id)
        return result

    monkeypatch.setattr(mgr, "_execute_command_inner", cancel_after_tx)
    result = mgr.execute_command(
        session.session_id, "top", "agent:test", cmd_id, mode="interactive",
    )

    assert result["ok"] is True, result
    lease_id = result["interactive_session_id"]
    assert session.interactive_session_id == lease_id
    assert session._foreground_operation is None
    for data in ("q", "x"):
        sent = mgr.interactive_send(lease_id, data=data, encoding="plain")
        assert sent["ok"] is True, sent
    assert b"".join(port.writes) == b"top\nqx"
    assert b"".join(_tx_payloads(mgr)) == b"top\nqx"
    assert mgr.interactive_close(lease_id)["ok"] is True


@pytest.mark.parametrize("cancel_point", ["before_lease", "before_commit"])
def test_cancel_before_interactive_tx_still_blocks_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancel_point: str,
) -> None:
    """只撤銷命令 op 仍須擋下開 lease 前與 fence 提交前的取消，零 TX 且不遺留 lease。"""
    port = FakePort()
    mgr, session, bridge = _ready(tmp_path, port)
    cmd_id = "cmd-b1-early-cancel"
    if cancel_point == "before_lease":
        execute_inner = mgr._execute_command_inner

        def cancel_before_lease(*args: Any, **kwargs: Any) -> dict[str, Any]:
            assert session.interactive_session_id is None
            mgr.revoke_tx_op(cmd_id)
            return execute_inner(*args, **kwargs)

        monkeypatch.setattr(mgr, "_execute_command_inner", cancel_before_lease)
    else:
        bridge._after_tx_authorize = lambda: mgr.revoke_tx_op(cmd_id)

    result = mgr.execute_command(
        session.session_id, "top", "agent:test", cmd_id, mode="interactive",
    )

    assert result["ok"] is False, result
    assert result["error_code"] == "FLUSHED_BY_RECOVERY", result
    assert port.writes == []
    assert _tx_payloads(mgr) == []
    assert session.interactive_session_id is None
    assert session._foreground_operation is None
