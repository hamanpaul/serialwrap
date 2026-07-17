"""#122 realhw harness 純邏輯單測（不碰 live）。"""
from __future__ import annotations

import pytest

from realhw import harness


def _mk(id, tier="p0", destructive=False):
    return harness.Case(id=id, tier=tier, title=id, run=lambda ctx: harness.CaseResult("PASS"),
                        destructive=destructive)


def test_select_by_tier_excludes_longrun():
    reg = [_mk("a", "p0"), _mk("b", "p1"), _mk("c", "longrun")]
    got = harness.select_cases(reg, tiers=["p0", "p1"], only=None, skip=[])
    assert [c.id for c in got] == ["a", "b"]  # longrun 絕不被 p0/p1 隱含


def test_select_only_overrides_tier():
    reg = [_mk("a", "p0"), _mk("b", "p1")]
    got = harness.select_cases(reg, tiers=["p0"], only="b", skip=[])
    assert [c.id for c in got] == ["b"]


def test_select_skip():
    reg = [_mk("a", "p1"), _mk("b", "p1")]
    got = harness.select_cases(reg, tiers=["p1"], only=None, skip=["a"])
    assert [c.id for c in got] == ["b"]


def test_select_unknown_only_raises():
    with pytest.raises(harness.UnknownCaseError):
        harness.select_cases([_mk("a")], tiers=["p0"], only="nope", skip=[])


@pytest.mark.parametrize("s,secs", [("32h", 115200), ("45m", 2700), ("3600s", 3600), ("2h", 7200)])
def test_parse_duration(s, secs):
    assert harness.parse_duration(s) == secs


def test_parse_duration_rejects_garbage():
    with pytest.raises(ValueError):
        harness.parse_duration("soon")
