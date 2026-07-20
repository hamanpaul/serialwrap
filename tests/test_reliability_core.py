"""Phase 2 plugin core（serialwrap_reliability.core）純邏輯單測——不 import testpilot、不碰 live。"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "reliability"))

from serialwrap_reliability import core  # noqa: E402


CFG = {
    "boards": [
        {"com": "COM0", "alias": "dut-prpl", "serial": "S0", "busid": "8-1", "platform": "prpl"},
        {"com": "COM1", "alias": "sta-prpl", "serial": "S1", "busid": "8-2", "platform": "brcm"},
    ],
    "tmux_prefix": "realhw",
    "usbipd_exe": "/mnt/c/x/usbipd.exe",
    "timeouts": {"ready_wait_s": 180, "reboot_wait_s": 300, "human_active_window_s": 60},
    "longrun": {"snapshot_interval_s": 300, "agent_workers": 4},
    "duration_s": 900,
}


def _mk_case(id: str, tier: str = "p0", destructive: bool = False,
             requires: tuple[str, ...] = (), hints: tuple[str, ...] = ()):
    core.ensure_realhw_importable()
    from realhw import harness

    return harness.Case(
        id=id,
        tier=tier,
        title=f"title-{id}",
        run=lambda ctx: harness.CaseResult("PASS"),
        destructive=destructive,
        requires=tuple(requires),
        hints=tuple(hints),
    )


def test_repo_root_locates_worktree():
    assert core.REPO_ROOT == REPO_ROOT
    assert (core.REPO_ROOT / "realhw" / "harness.py").is_file()


def test_resolve_repo_root_rejects_non_editable_layout(tmp_path):
    fake = tmp_path / "venv" / "lib" / "python3.12" / "site-packages" / "serialwrap_reliability" / "core.py"
    fake.parent.mkdir(parents=True)
    fake.write_text("", encoding="utf-8")
    with pytest.raises(RuntimeError, match="editable 安裝"):
        core.resolve_repo_root(fake)


def test_ensure_realhw_importable_idempotent():
    got = core.ensure_realhw_importable()
    assert got == REPO_ROOT
    import realhw  # noqa: F401

    before = list(sys.path)
    core.ensure_realhw_importable()
    assert sys.path.count(str(REPO_ROOT)) == before.count(str(REPO_ROOT))


def test_load_registry_populated_unique():
    registry = core.load_registry()
    ids = [case.id for case in registry]
    assert len(ids) == len(set(ids))
    assert len(ids) >= 29
    assert any(case.id == "p0-doctor" for case in registry)


def test_core_modules_do_not_import_testpilot():
    pkg = REPO_ROOT / "reliability" / "serialwrap_reliability"
    for name in ("__init__.py", "core.py"):
        text = (pkg / name).read_text(encoding="utf-8")
        bad = [line for line in text.splitlines() if re.match(r"\s*(import|from)\s+testpilot", line)]
        assert not bad, f"{name} 不得 import testpilot：{bad}"


@pytest.mark.parametrize("duration_s,interval_s,n", [(900, 300, 3), (60, 300, 1), (0, 300, 1)])
def test_synth_longrun_steps_count(duration_s, interval_s, n):
    steps = core.synth_longrun_steps(duration_s, interval_s)
    assert len(steps) == n
    assert steps[0]["id"] == "checkpoint-001"
    for step in steps:
        assert {"id", "action", "target"} <= set(step)
        assert step["action"] == "longrun_checkpoint"
        assert step["target"] == "bench"


def test_case_to_dict_single_step_schema():
    got = core.case_to_dict(_mk_case("p0-x", requires=("two_boards",), hints=("h1",)), CFG)
    assert {"id", "name", "topology", "steps", "pass_criteria", "metadata"} <= set(got)
    assert set(got["topology"]["devices"]) == {"COM0", "COM1"}
    assert got["steps"] == [{"id": "exec", "action": "run_case", "target": "bench"}]
    assert got["pass_criteria"] == ["realhw_case_verdict"]
    assert got["metadata"] == {
        "tier": "p0",
        "destructive": False,
        "requires": ["two_boards"],
        "hints": ["h1"],
    }


def test_case_to_dict_longrun_synthesizes_checkpoints():
    got = core.case_to_dict(_mk_case("lr-mixed", tier="longrun"), CFG)
    assert [step["id"] for step in got["steps"]] == [
        "checkpoint-001",
        "checkpoint-002",
        "checkpoint-003",
    ]


def test_build_case_dicts_preserves_registry_order():
    got = core.build_case_dicts([_mk_case("a"), _mk_case("b"), _mk_case("c")], CFG)
    assert [case["id"] for case in got] == ["a", "b", "c"]


def test_filter_for_run_default_excludes_destructive():
    cases = core.build_case_dicts(
        [_mk_case("a"), _mk_case("b", destructive=True), _mk_case("c")], CFG
    )
    got = core.filter_for_run(cases, set())
    assert [case["id"] for case in got] == ["a", "c"]


def test_filter_for_run_explicit_id_includes_destructive():
    cases = core.build_case_dicts([_mk_case("a"), _mk_case("b", destructive=True)], CFG)
    got = core.filter_for_run(cases, {"b"})
    assert [case["id"] for case in got] == ["b"]
