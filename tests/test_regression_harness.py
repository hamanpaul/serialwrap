"""#155 regression plugin harness／core 純邏輯單測——不 import testpilot、不碰 live。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "regression"))

from serialwrap_regression import core, harness  # noqa: E402

CFG = {
    "boards": [
        {"com": "COM0", "alias": "dut-prpl", "serial": "S0", "platform": "prpl"},
        {"com": "COM1", "alias": "com1-brcm", "serial": "S1", "platform": "bcm"},
    ],
    "serialwrap_exe": "/opt/bin/serialwrap",
    "allow_destructive": False,
    "tmux_prefix": "swreg",
    "timeouts": {"ready_wait_s": 180, "boot_wait_s": 240, "cmd_timeout_s": 12},
}


def _mk(case_id: str, family: str = "F3", **kw) -> harness.Case:
    return harness.Case(
        id=case_id,
        family=family,
        title=case_id,
        run=lambda ctx: None,
        issues=kw.pop("issues", ("#94",)),
        **kw,
    )


@pytest.fixture(autouse=True)
def _clean_registry(monkeypatch):
    monkeypatch.setattr(harness, "REGISTRY", [])


def test_register_rejects_duplicate_id():
    harness.register(_mk("a"))
    with pytest.raises(ValueError, match="duplicate"):
        harness.register(_mk("a"))


def test_register_rejects_empty_issues():
    with pytest.raises(ValueError, match="issue"):
        harness.register(_mk("b", issues=()))


def test_register_rejects_unknown_family():
    with pytest.raises(ValueError, match="family"):
        harness.register(_mk("c", family="F99"))


def test_ordered_follows_family_order():
    harness.register(_mk("x-f9", family="F9"))
    harness.register(_mk("x-f3b", family="F3"))
    harness.register(_mk("x-f3a", family="F3"))
    harness.register(_mk("x-f1", family="F1"))
    got = [c.id for c in harness.ordered(list(harness.REGISTRY))]
    assert got == ["x-f3a", "x-f3b", "x-f1", "x-f9"]


def test_registry_disjoint_from_realhw():
    """兩 plugin 的 registry 不得互染（#155 spec：discover 互不撈到對方）。"""
    core.ensure_realhw_importable()
    import realhw.cases  # noqa: F401
    from realhw import harness as rh

    reg_ids = {c.id for c in core.load_registry()}
    realhw_ids = {c.id for c in rh.REGISTRY}
    assert reg_ids.isdisjoint(realhw_ids)


def test_case_dicts_carry_family_and_issues():
    harness.register(_mk("f3-x", family="F3", issues=("#94", "#16")))
    dicts = core.build_case_dicts(list(harness.REGISTRY), CFG)
    meta = dicts[0]["metadata"]
    assert meta["family"] == "F3"
    assert meta["issues"] == ["#94", "#16"]
    assert dicts[0]["topology"]["devices"]["COM0"]["platform"] == "prpl"


def test_runtime_skip_destructive_gated():
    got = core.runtime_skip({"destructive": True}, {}, None, allow_destructive=False)
    assert got is not None and got[0] == "destructive_gated"
    assert core.runtime_skip({"destructive": True}, {}, None, allow_destructive=True) is None


def test_runtime_skip_broken_by_and_requires():
    got = core.runtime_skip({"destructive": True}, {}, "f9-x", allow_destructive=True)
    assert got is not None and got[0] == "broken_by:f9-x"
    got = core.runtime_skip({"requires": ["two_boards"]}, {"two_boards": "board_down"}, None,
                            allow_destructive=False)
    assert got is not None and got[0] == "board_down"
    assert core.runtime_skip({}, {}, None, allow_destructive=False) is None


def test_failure_payload_fills_category():
    assert core.failure_payload({"verdict": "PASS"}) is None
    fail = core.failure_payload({"verdict": "FAIL", "reason": "x", "category": "",
                                 "reason_code": "r", "evidence": {}})
    assert fail is not None and fail["category"] == "test"
    skip = core.failure_payload({"verdict": "SKIP", "reason": "x", "category": "",
                                 "reason_code": "r", "evidence": {}})
    assert skip is not None and skip["category"] == "environment"


def test_filter_for_run_keeps_destructive_for_gate():
    """未點名時不預先剔除 destructive——由 runtime_skip 記 SKIP（gate 可見）。"""
    harness.register(_mk("f9-y", family="F9", destructive=True))
    dicts = core.build_case_dicts(list(harness.REGISTRY), CFG)
    assert [c["id"] for c in core.filter_for_run(dicts, set())] == ["f9-y"]
    assert core.filter_for_run(dicts, {"nope"}) == []


def test_load_testbed_defaults(tmp_path):
    example = tmp_path / "testbed.yaml.example"
    example.write_text("boards:\n  - com: COM0\n    serial: S0\n", encoding="utf-8")
    cfg = core.load_testbed(example)
    assert cfg["allow_destructive"] is False
    assert cfg["serialwrap_exe"].endswith("/.local/bin/serialwrap")
    assert not cfg["serialwrap_exe"].startswith("~")
    assert cfg["timeouts"]["ready_wait_s"] == 180
    real = tmp_path / "testbed.yaml"
    real.write_text("allow_destructive: true\nboards: []\n", encoding="utf-8")
    assert core.load_testbed(example)["allow_destructive"] is True
