"""#122 preflight 判定邏輯單測——吃注入的檢查結果，不碰 live。"""
from __future__ import annotations

from realhw import preflight


def _checks(**over):
    base = dict(git_behind=0, doctor_ok=True, boards_ready=["COM0", "COM1"],
                boards_expected=["COM0", "COM1"], tools_missing=[],
                leaked_daemons=[], other_pytest=False, state_polluted=False)
    base.update(over)
    return preflight.Checks(**base)


def test_all_green_passes():
    ok, problems = preflight.evaluate(_checks())
    assert ok and problems == []


def test_missing_tool_fails():
    ok, problems = preflight.evaluate(_checks(tools_missing=["tmux"]))
    assert not ok and any("tmux" in p for p in problems)


def test_other_pytest_fails_with_mutex_reason():
    ok, problems = preflight.evaluate(_checks(other_pytest=True))
    assert not ok and any("live guard" in p for p in problems)


def test_boards_not_ready_fails():
    ok, problems = preflight.evaluate(_checks(boards_ready=["COM0"]))
    assert not ok and any("COM1" in p for p in problems)


def test_git_behind_warns_but_passes():
    ok, problems = preflight.evaluate(_checks(git_behind=3))
    assert ok and any("落後" in p for p in problems)  # 警告仍列出但不擋
