"""tests/test_suspend_resume_reentrant.py — #78 suspend/resume 可重入。

直接驅動真實 suspend_interactive/resume_interactive（bypass __init__，僅設狀態屬性），
驗證巢狀（重疊 agent 路徑）下 human raw ownership 不再被覆寫成 None。
"""
from __future__ import annotations

import threading

from sw_core.uart_io import UARTBridge


def _bridge(owner="human:X"):
    b = object.__new__(UARTBridge)
    b._state_lock = threading.RLock()
    b._interactive_owner = owner
    b._suspended_owner = None
    b._agent_active = False
    b._suspend_depth = 0
    b._deferred_buffers = {}
    return b


def test_single_suspend_resume_restores_owner():
    b = _bridge()
    b.suspend_interactive()
    assert b._interactive_owner is None and b._agent_active is True and b._suspend_depth == 1
    b.resume_interactive()
    assert b._interactive_owner == "human:X" and b._agent_active is False and b._suspend_depth == 0


def test_nested_double_suspend_resume_restores_owner():
    """#78 核心：兩條 agent 路徑重疊（double suspend）→ double resume 仍還原 owner（修前為 None）。"""
    b = _bridge()
    b.suspend_interactive()
    b.suspend_interactive()
    assert b._interactive_owner is None and b._suspend_depth == 2
    b.resume_interactive()
    assert b._interactive_owner is None and b._suspend_depth == 1  # 仍有未配對 suspend，維持 suspended
    b.resume_interactive()
    assert b._interactive_owner == "human:X" and b._agent_active is False and b._suspend_depth == 0


def test_extra_unbalanced_resume_is_noop():
    b = _bridge()
    b.suspend_interactive()
    b.resume_interactive()
    assert b._interactive_owner == "human:X"
    b.resume_interactive()  # 多餘 resume → no-op，不得再次「還原」成 None
    assert b._interactive_owner == "human:X" and b._suspend_depth == 0


def test_deferred_flushed_only_on_outermost_resume():
    b = _bridge()
    sent = []
    b.send_bytes = lambda data, source, cmd_id=None: sent.append((source, bytes(data)))
    b.suspend_interactive()
    b.suspend_interactive()
    b._deferred_buffers["c1"] = bytearray(b"hi")
    b.resume_interactive()
    assert sent == []  # 仍 suspended（depth=1），未 flush
    b.resume_interactive()
    assert sent == [("human:c1", b"hi")]  # 最外層 resume 才 flush deferred


def test_deep_nesting_balanced():
    b = _bridge()
    for _ in range(5):
        b.suspend_interactive()
    assert b._suspend_depth == 5 and b._interactive_owner is None
    for _ in range(5):
        b.resume_interactive()
    assert b._suspend_depth == 0 and b._interactive_owner == "human:X" and b._agent_active is False
