"""OD-09 凍結與候選長跑登記的契約失敗測試。"""

from __future__ import annotations

import ast
import copy
import hashlib
import json
import shlex
import subprocess
import sys
import textwrap
from dataclasses import asdict, replace
from pathlib import Path

import pytest

import fakes_od09


ROOT = Path(__file__).resolve().parents[1]
pytestmark = [pytest.mark.core_portable]


def _od09():
    from realhw.acceptance import od09_freeze

    return od09_freeze


def _gate():
    from realhw.acceptance import gate

    return gate


def _yaml():
    import yaml

    return yaml


def _load_yaml(path: Path):
    return _yaml().safe_load(path.read_text(encoding="utf-8"))


def _write_yaml(path: Path, value) -> None:
    path.write_text(_yaml().safe_dump(value, sort_keys=False, allow_unicode=True), encoding="utf-8")


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True), encoding="utf-8")


def _proposal_digest(proposal: dict) -> str:
    value = {key: item for key, item in proposal.items() if key != "proposal_sha256"}
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _keys(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from _keys(item)


def _refresh_index_item(env, item_id: str) -> None:
    index = _load_yaml(env.stage0_index)
    item = next(item for item in index["items"] if item["id"] == item_id)
    artifact = env.evidence_root / item["artifact_path"]
    item["sha256"] = hashlib.sha256(artifact.read_bytes()).hexdigest()
    _write_yaml(env.stage0_index, index)


def _sync_baseline_references(env, proposal: dict) -> None:
    current_sha = hashlib.sha256(env.acceptance.read_bytes()).hexdigest()
    for platform, gate_path in env.baseline_gates.items():
        gate_value = _read_json(gate_path)
        gate_value["acceptance"]["sha256"] = current_sha
        _write_json(gate_path, gate_value)
        item_id = _od09().BASELINE_ITEMS[platform]
        _refresh_index_item(env, item_id)
        proposal["baseline_artifacts"][platform]["release_gate_sha256"] = hashlib.sha256(
            gate_path.read_bytes()
        ).hexdigest()
    proposal["proposal_sha256"] = _proposal_digest(proposal)


def _new_env(tmp_path: Path, **kwargs):
    return fakes_od09.od09_env(tmp_path, **kwargs)


def _make_proposal(env):
    od09 = _od09()
    return od09.propose(env.linux_stats, env.windows_stats, baseline_gates=env.baseline_gates)


def _freeze(env, *, overrides=None, now=None, proposal=None):
    od09 = _od09()
    proposal = proposal or _make_proposal(env)
    report = od09.freeze(
        env.acceptance,
        proposal,
        decision_id="OD-09-F1",
        approved_by="owner",
        overrides=overrides or {},
        stage0_index=env.stage0_index,
        evidence_root=env.evidence_root,
        ledger_path=env.ledger,
        addenda_path=env.addenda,
        now=now or fakes_od09.ts(20, 9),
    )
    return report, proposal


def _assert_freeze_invalid(env, proposal, *, detail_path: str | None = None):
    od09 = _od09()
    acceptance_before = env.acceptance.read_bytes()
    addenda_before = env.addenda.read_bytes()
    with pytest.raises(od09.Od09Error) as error:
        od09.freeze(
            env.acceptance,
            proposal,
            decision_id="OD-09-F1",
            approved_by="owner",
            overrides={},
            stage0_index=env.stage0_index,
            evidence_root=env.evidence_root,
            ledger_path=env.ledger,
            addenda_path=env.addenda,
            now=fakes_od09.ts(20, 9),
        )
    assert error.value.code == "HARNESS_OD09_BASELINE_INVALID"
    if detail_path is not None:
        assert any(detail_path in item for item in error.value.detail)
    assert env.acceptance.read_bytes() == acceptance_before
    assert env.addenda.read_bytes() == addenda_before


@pytest.mark.process_rpc
def test_stage0_chain_without_later_phases(tmp_path):
    """R4-F2：端到端鏈只使用 P2 交付物。"""
    script = textwrap.dedent(f"""
        import importlib.abc, json, sys
        BLOCKED = ("tools.stage3", "sw_core")
        class _Block(importlib.abc.MetaPathFinder):
            def find_spec(self, name, path=None, target=None):
                if name.startswith(BLOCKED):
                    raise ImportError("later-phase module imported: " + name)
                return None
        sys.path.insert(0, {str(ROOT)!r}); sys.path.insert(0, {str(ROOT / "tests")!r})
        sys.meta_path.insert(0, _Block())
        from pathlib import Path
        import fakes_od09
        from realhw.acceptance import gate, od09_freeze
        env = fakes_od09.od09_env(Path({str(tmp_path)!r}))
        assert not (env.repo / "tests" / "acceptance" / "stage_exits.json").exists()
        assert not (env.repo / "docs" / "rust-refactor" / "evidence" / "index.json").exists()
        prop = od09_freeze.propose(env.linux_stats, env.windows_stats, baseline_gates=env.baseline_gates)
        od09_freeze.freeze(env.acceptance, prop, decision_id="OD-09-F1", approved_by="owner", overrides={{}},
                           stage0_index=env.stage0_index, evidence_root=env.evidence_root, ledger_path=env.ledger,
                           addenda_path=env.addenda, now=fakes_od09.ts(20, 9))
        od09_freeze.register_candidate_start(env.acceptance, item_id="S1B-L3-MIXED-48H-WINDOWS", platform="windows",
                                             run_id="s1b-1", started_at=fakes_od09.ts(21, 0), ledger_path=env.ledger)
        run = fakes_od09.longrun_case(Path({str(tmp_path)!r}) / "cand", platform="windows", started_at=fakes_od09.ts(21, 0))
        baseline = fakes_od09.rerun_baseline_gate(env, platform="windows")
        out = gate.evaluate(run.inputs(mode="candidate", acceptance_path=env.acceptance, baseline=baseline))
        res = {{c["id"]: c["result"] for c in out["checks"]}}
        print(json.dumps({{"res": res, "release": out["release_gate_result"],
                          "later": sorted(m for m in sys.modules if m.startswith(BLOCKED))}}))
    """)
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, result.stderr
    got = json.loads(result.stdout.strip().splitlines()[-1])
    assert got["later"] == []
    assert (
        got["res"]["resources"]
        == got["res"]["min_effective_load"]
        == got["res"]["comparison_uncertainty"]
        == "PASS"
    )
    assert got["release"] == "PASS"


@pytest.mark.memory_only
def test_od09_freeze_imports_only_stage0():
    path = ROOT / "realhw" / "acceptance" / "od09_freeze.py"
    source = path.read_text(encoding="utf-8")
    modules = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            modules.add(node.module)
    allowed_extra = {"yaml", "realhw", "realhw.acceptance", "realhw.acceptance.gate"}
    assert {module for module in modules if module.split(".")[0] not in sys.stdlib_module_names} <= allowed_extra, modules
    for banned in ("tools.stage3", "sw_core", "stage_exits.json", "evidence/index.json"):
        assert banned not in source


@pytest.mark.memory_only
def test_freeze_refused_after_candidate_registered(tmp_path):
    env = _new_env(tmp_path)
    env.ledger.write_text(
        '{"item_id":"S1B-L3-MIXED-48H-WINDOWS","platform":"windows","run_id":"x",'
        '"started_at":"2026-11-19T00:00:00+00:00"}\n',
        encoding="utf-8",
    )
    before = env.acceptance.read_bytes()
    addenda_before = env.addenda.read_bytes()
    proposal = _make_proposal(env)
    od09 = _od09()
    with pytest.raises(od09.Od09Error) as error:
        od09.freeze(
            env.acceptance,
            proposal,
            decision_id="OD-09-F1",
            approved_by="owner",
            overrides={},
            stage0_index=env.stage0_index,
            evidence_root=env.evidence_root,
            ledger_path=env.ledger,
            addenda_path=env.addenda,
            now=fakes_od09.ts(20, 9),
        )
    assert error.value.code == "HARNESS_OD09_TOO_LATE"
    assert env.acceptance.read_bytes() == before
    assert env.addenda.read_bytes() == addenda_before


@pytest.mark.memory_only
def test_register_candidate_refused_before_freeze(tmp_path):
    env = _new_env(tmp_path)
    od09 = _od09()
    with pytest.raises(od09.Od09Error) as error:
        od09.register_candidate_start(
            env.acceptance,
            item_id="S1B-L3-MIXED-48H-WINDOWS",
            platform="windows",
            run_id="s1b-1",
            started_at=fakes_od09.ts(21, 0),
            ledger_path=env.ledger,
        )
    assert error.value.code == "HARNESS_OD09_NOT_FROZEN"
    assert sorted(error.value.detail) == sorted(
        f"{key} not frozen" for key in od09.DEFERRED_KEYS
    )
    assert env.ledger.read_bytes() == b""


@pytest.mark.memory_only
def test_od09_env_baselines_present(tmp_path):
    env = _new_env(tmp_path)
    checker = fakes_od09.evidence_index_checker(env)
    od09 = _od09()
    for platform in od09.PLATFORMS:
        item, problems = checker.verify_item(
            env.stage0_index,
            od09.BASELINE_ITEMS[platform],
            root=env.evidence_root,
        )
        assert problems == []
        assert item["status"] == "present"


@pytest.mark.memory_only
def test_stage0_index_od09_item_ids():
    od09 = _od09()
    index_path = ROOT / "tests" / "acceptance" / "stage0_evidence_index.yaml"
    index = _load_yaml(index_path)
    items = {item["id"]: item for item in index["items"]}
    for item_id in (*od09.BASELINE_ITEMS.values(), od09.FREEZE_ITEM):
        assert items[item_id]["status"] == "blocked_owner"


@pytest.mark.memory_only
def test_baseline_stats_from_lr_mixed(tmp_path):
    env = _new_env(tmp_path)
    od09 = _od09()
    stats = od09.baseline_stats(
        env.linux_case_dir,
        platform="linux",
        release_manifest=env.linux_manifest,
    )
    workload = od09.workload(env.acceptance)
    assert stats.duration_s == workload["min_continuous_duration_s"]
    assert stats.hourly_done["COM0|agent:rhw0"]["hours"] == 48


@pytest.mark.memory_only
def test_spliced_baseline_rejected(tmp_path):
    env = _new_env(tmp_path)
    run = env.linux_run
    run.set_snapshots([
        row for row in run.snapshots
        if not 23 * 3600 <= row["t"] < 24 * 3600
    ])
    od09 = _od09()
    stats = od09.baseline_stats(
        env.linux_case_dir,
        platform="linux",
        release_manifest=env.linux_manifest,
    )
    problems = od09.validate_baseline(stats, acceptance_path=env.acceptance)
    assert any("snapshot gap" in problem for problem in problems)


@pytest.mark.memory_only
def test_short_baseline_rejected(tmp_path):
    env = _new_env(tmp_path)
    od09 = _od09()
    workload = od09.workload(env.acceptance)
    stats = replace(
        env.linux_stats,
        duration_s=workload["min_continuous_duration_s"] - 3600,
    )
    problems = od09.validate_baseline(stats, acceptance_path=env.acceptance)
    assert any("duration" in problem for problem in problems)


@pytest.mark.memory_only
def test_thresholds_read_from_workload(tmp_path):
    env = _new_env(tmp_path)
    acceptance = _load_yaml(env.acceptance)
    acceptance["workload"]["min_continuous_duration_s"] = 3600
    _write_yaml(env.acceptance, acceptance)
    stats = replace(env.linux_stats, duration_s=5400)
    problems = _od09().validate_baseline(stats, acceptance_path=env.acceptance)
    assert not any("duration" in problem for problem in problems)


@pytest.mark.memory_only
def test_rust_baseline_rejected(tmp_path):
    env = _new_env(tmp_path)
    stats = replace(env.linux_stats, daemon_implementation="rust")
    problems = _od09().validate_baseline(stats, acceptance_path=env.acceptance)
    assert any("daemon_implementation" in problem for problem in problems)


@pytest.mark.memory_only
def test_load_stop_rejected(tmp_path):
    env = _new_env(tmp_path)
    hourly_done = copy.deepcopy(env.linux_stats.hourly_done)
    hourly_done["COM0|agent:rhw0"]["min"] = 0
    stats = replace(env.linux_stats, hourly_done=hourly_done)
    problems = _od09().validate_baseline(stats, acceptance_path=env.acceptance)
    assert any("COM0|agent:rhw0" in problem for problem in problems)


@pytest.mark.memory_only
def test_proposal_formula_fixed(tmp_path):
    env = _new_env(tmp_path, linux_rss_peak=40000, linux_slope=10.0, interval=300)
    od09 = _od09()
    proposal = _make_proposal(env)
    thresholds = proposal["thresholds"]
    assert thresholds["resources"]["per_platform"]["linux"]["rss_peak_kb"] == 50000
    assert thresholds["resources"]["per_platform"]["linux"]["rss_slope_kb_per_h"] == 256
    assert thresholds["comparison_uncertainty"]["per_platform"]["linux"] == {
        "error_rate_abs_tolerance": 0.002,
        "throughput_ratio_tolerance": 0.02,
        "stuck_s_tolerance": 300,
        "max_reruns": 1,
    }
    assert thresholds["min_effective_load"]["per_platform"]["windows"]["max_idle_gap_s"] == 3600
    total = env.windows_stats.completed_total["COM0|agent:rhw0"]
    assert thresholds["min_effective_load"]["per_platform"]["windows"][
        "min_completed_per_board_source"
    ]["COM0|agent:rhw0"] == max(1, total // 2)
    proposal_keys = set(_keys(proposal))
    assert "tolerance" not in proposal_keys
    assert "rerun_rule" not in proposal_keys
    for platform, path in env.baseline_gates.items():
        assert proposal["baseline_artifacts"][platform]["release_gate_sha256"] == hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
    assert set(od09.DEFERRED_KEYS) == set(thresholds)


@pytest.mark.memory_only
def test_per_platform_fields_match_acceptance():
    od09 = _od09()
    acceptance = _load_yaml(ROOT / "realhw" / "acceptance" / "longrun_acceptance.yaml")
    for key, fields in od09.PER_PLATFORM_FIELDS.items():
        for platform in od09.PLATFORMS:
            assert set(fields) == set(acceptance["thresholds"][key]["per_platform"][platform])


@pytest.mark.memory_only
def test_real_acceptance_roundtrip_propose_freeze_check_gate(tmp_path):
    env = _new_env(tmp_path)
    od09 = _od09()
    before = _load_yaml(env.acceptance)
    _freeze(env)
    assert od09.check_frozen_before(env.acceptance, fakes_od09.ts(21, 0)) == []

    frozen = _load_yaml(env.acceptance)
    threshold = frozen["thresholds"]["resources"]["per_platform"]["linux"]["rss_peak_kb"]
    baseline = fakes_od09.rerun_baseline_gate(env, platform="linux")
    run = fakes_od09.longrun_case(
        tmp_path / "candidate-pass",
        platform="linux",
        started_at=fakes_od09.ts(21, 0),
        rss_peak_kb=threshold - 1,
    )
    gate = _gate()
    passed = gate.evaluate(run.inputs(
        mode="candidate",
        acceptance_path=env.acceptance,
        baseline=baseline,
    ))
    passed_checks = {item["id"]: item["result"] for item in passed["checks"]}
    assert passed_checks["resources"] == "PASS"
    assert passed_checks["min_effective_load"] == "PASS"
    assert passed_checks["comparison_uncertainty"] == "PASS"
    assert passed["release_gate_result"] == "PASS"

    run.set_snapshots([dict(row, rss_kb=threshold + 1) for row in run.snapshots])
    failed = gate.evaluate(run.inputs(
        mode="candidate",
        acceptance_path=env.acceptance,
        baseline=baseline,
    ))
    assert next(item for item in failed["checks"] if item["id"] == "resources")["result"] == "FAIL"
    assert failed["release_gate_result"] == "FAIL"

    after = _load_yaml(env.acceptance)
    assert "deferred" not in json.dumps(after)
    for key in od09.DEFERRED_KEYS:
        assert set(after["thresholds"][key]["per_platform"]) == set(before["thresholds"][key]["per_platform"])
        assert all(value is not None for platform in od09.PLATFORMS
                   for value in after["thresholds"][key]["per_platform"][platform].values())
        assert set(after["thresholds"][key]) == set(before["thresholds"][key]) | {
            "frozen_at", "decision_id", "approved_by", "overrides", "proposal_sha256", "baseline_evidence"
        }


@pytest.mark.memory_only
def test_freeze_writes_only_deferred_and_bumps_revision(tmp_path):
    env = _new_env(tmp_path)
    od09 = _od09()
    before = _load_yaml(env.acceptance)
    proposal = _make_proposal(env)
    _freeze(env, proposal=proposal)
    after = _load_yaml(env.acceptance)
    assert after["revision"] != before["revision"]
    for key in od09.DEFERRED_KEYS:
        frozen = after["thresholds"][key]
        assert frozen["status"] == "frozen"
        assert frozen["decision_id"] == "OD-09-F1"
        assert frozen["per_platform"] == proposal["thresholds"][key]["per_platform"]
        evidence = frozen["baseline_evidence"]
        assert {evidence[p]["item_id"] for p in od09.PLATFORMS} == set(od09.BASELINE_ITEMS.values())
        for platform in od09.PLATFORMS:
            indexed, problems = fakes_od09.evidence_index_checker(env).verify_item(
                env.stage0_index, od09.BASELINE_ITEMS[platform], root=env.evidence_root
            )
            assert problems == []
            assert evidence[platform]["sha256"] == indexed["sha256"]
    original_without_revision = {key: value for key, value in before.items() if key != "revision"}
    updated_without_revision = {key: value for key, value in after.items() if key != "revision"}
    for key in before["thresholds"]:
        if key not in od09.DEFERRED_KEYS:
            assert updated_without_revision["thresholds"][key] == original_without_revision["thresholds"][key]
    for key in before:
        if key not in {"revision", "thresholds"}:
            assert updated_without_revision[key] == original_without_revision[key]
    _gate().load_acceptance(env.acceptance)


@pytest.mark.memory_only
def test_freeze_records_overrides(tmp_path):
    env = _new_env(tmp_path)
    proposal = _make_proposal(env)
    windows_value = copy.deepcopy(proposal["thresholds"]["resources"]["per_platform"]["windows"])
    overrides = {"resources.per_platform.linux.rss_peak_kb": 60000}
    _freeze(env, overrides=overrides, proposal=proposal)
    frozen = _load_yaml(env.acceptance)["thresholds"]["resources"]
    assert frozen["per_platform"]["linux"]["rss_peak_kb"] == 60000
    assert frozen["per_platform"]["windows"] == windows_value
    assert frozen["overrides"] == overrides
    assert "resources.per_platform.linux.rss_peak_kb" in env.addenda.read_text(encoding="utf-8")


@pytest.mark.memory_only
def test_freeze_rejects_unknown_override_path(tmp_path):
    for index, overrides in enumerate((
        {"resources.rss_budget.linux": 1},
        {"comparison_uncertainty.per_platform.linux.tolerance": 1},
    )):
        env = _new_env(tmp_path / str(index))
        proposal = _make_proposal(env)
        before = env.acceptance.read_bytes()
        addenda_before = env.addenda.read_bytes()
        od09 = _od09()
        with pytest.raises(od09.Od09Error) as error:
            od09.freeze(
                env.acceptance, proposal, decision_id="OD-09-F1", approved_by="owner",
                overrides=overrides, stage0_index=env.stage0_index, evidence_root=env.evidence_root,
                ledger_path=env.ledger, addenda_path=env.addenda, now=fakes_od09.ts(20, 9),
            )
        assert error.value.code == "HARNESS_OD09_BASELINE_INVALID"
        assert env.acceptance.read_bytes() == before
        assert env.addenda.read_bytes() == addenda_before


@pytest.mark.memory_only
def test_freeze_rejects_field_set_mismatch(tmp_path):
    env = _new_env(tmp_path / "acceptance")
    proposal = _make_proposal(env)
    acceptance = _load_yaml(env.acceptance)
    del acceptance["thresholds"]["resources"]["per_platform"]["windows"]["rss_slope_kb_per_h"]
    _write_yaml(env.acceptance, acceptance)
    _sync_baseline_references(env, proposal)
    _assert_freeze_invalid(
        env,
        proposal,
        detail_path="thresholds.resources.per_platform.windows.rss_slope_kb_per_h",
    )

    env = _new_env(tmp_path / "proposal")
    proposal = _make_proposal(env)
    del proposal["thresholds"]["resources"]["per_platform"]["windows"]["rss_slope_kb_per_h"]
    proposal["proposal_sha256"] = _proposal_digest(proposal)
    _assert_freeze_invalid(
        env,
        proposal,
        detail_path="thresholds.resources.per_platform.windows.rss_slope_kb_per_h",
    )


@pytest.mark.memory_only
def test_freeze_requires_present_baselines_in_stage0_index(tmp_path):
    def variant(name: str, mutate):
        env = _new_env(tmp_path / name)
        proposal = _make_proposal(env)
        mutate(env, proposal)
        _assert_freeze_invalid(env, proposal)

    def blocked_owner(env, _proposal):
        index = _load_yaml(env.stage0_index)
        item = next(item for item in index["items"] if item["id"] == _od09().BASELINE_ITEMS["windows"])
        item["status"] = "blocked_owner"
        _write_yaml(env.stage0_index, index)

    def bad_index_sha(env, _proposal):
        index = _load_yaml(env.stage0_index)
        item = next(item for item in index["items"] if item["id"] == _od09().BASELINE_ITEMS["windows"])
        item["sha256"] = "0" * 64
        _write_yaml(env.stage0_index, index)

    def failed_baseline(env, _proposal):
        gate_path = env.baseline_gates["windows"]
        value = _read_json(gate_path)
        value["baseline_result"] = "FAIL"
        _write_json(gate_path, value)
        _refresh_index_item(env, _od09().BASELINE_ITEMS["windows"])

    def candidate_mode(env, _proposal):
        gate_path = env.baseline_gates["windows"]
        value = _read_json(gate_path)
        value["mode"] = "candidate"
        _write_json(gate_path, value)
        _refresh_index_item(env, _od09().BASELINE_ITEMS["windows"])

    def stale_acceptance_hash(env, _proposal):
        gate_path = env.baseline_gates["windows"]
        value = _read_json(gate_path)
        value["acceptance"]["sha256"] = "f" * 64
        _write_json(gate_path, value)
        _refresh_index_item(env, _od09().BASELINE_ITEMS["windows"])

    def late_baseline(env, _proposal):
        gate_path = env.baseline_gates["windows"]
        value = _read_json(gate_path)
        value["run"]["ended_at_utc"] = fakes_od09.ts(20, 9)
        _write_json(gate_path, value)
        _refresh_index_item(env, _od09().BASELINE_ITEMS["windows"])

    def proposal_gate_mismatch(_env, proposal):
        proposal["baseline_artifacts"]["windows"]["release_gate_sha256"] = "f" * 64
        proposal["proposal_sha256"] = _proposal_digest(proposal)

    for name, mutate in (
        ("blocked", blocked_owner),
        ("index-sha", bad_index_sha),
        ("gate-fail", failed_baseline),
        ("candidate-mode", candidate_mode),
        ("acceptance-sha", stale_acceptance_hash),
        ("late", late_baseline),
        ("proposal-gate-sha", proposal_gate_mismatch),
    ):
        variant(name, mutate)


@pytest.mark.memory_only
def test_refreeze_refused(tmp_path):
    env = _new_env(tmp_path)
    _freeze(env)
    proposal = _make_proposal(env)
    od09 = _od09()
    with pytest.raises(od09.Od09Error) as error:
        od09.freeze(
            env.acceptance, proposal, decision_id="OD-09-F2", approved_by="owner",
            overrides={}, stage0_index=env.stage0_index, evidence_root=env.evidence_root,
            ledger_path=env.ledger, addenda_path=env.addenda, now=fakes_od09.ts(20, 10),
        )
    assert error.value.code == "HARNESS_OD09_TOO_LATE"


@pytest.mark.memory_only
def test_freeze_refused_on_malformed_ledger_line(tmp_path):
    env = _new_env(tmp_path)
    proposal = _make_proposal(env)
    env.ledger.write_text("{bad\n", encoding="utf-8")
    od09 = _od09()
    with pytest.raises(od09.Od09Error) as error:
        od09.freeze(
            env.acceptance, proposal, decision_id="OD-09-F1", approved_by="owner",
            overrides={}, stage0_index=env.stage0_index, evidence_root=env.evidence_root,
            ledger_path=env.ledger, addenda_path=env.addenda, now=fakes_od09.ts(20, 9),
        )
    assert error.value.code == "HARNESS_OD09_TOO_LATE"


@pytest.mark.memory_only
def test_tampered_proposal_rejected(tmp_path):
    env = _new_env(tmp_path / "value")
    proposal = _make_proposal(env)
    proposal["thresholds"]["resources"]["per_platform"]["linux"]["rss_peak_kb"] += 1
    _assert_freeze_invalid(env, proposal)

    env = _new_env(tmp_path / "revision")
    proposal = _make_proposal(env)
    proposal["acceptance_revision"] = "wrong-revision"
    proposal["proposal_sha256"] = _proposal_digest(proposal)
    _assert_freeze_invalid(env, proposal)


@pytest.mark.memory_only
def test_check_frozen_before(tmp_path):
    env = _new_env(tmp_path)
    od09 = _od09()
    assert sorted(od09.check_frozen_before(env.acceptance, fakes_od09.ts(21, 0))) == sorted(
        f"{key} not frozen" for key in od09.DEFERRED_KEYS
    )
    _freeze(env)
    assert od09.check_frozen_before(env.acceptance, "2026-11-20T09:00:01+00:00") == []
    assert sorted(od09.check_frozen_before(env.acceptance, "2026-11-20T08:59:59+00:00")) == sorted(
        f"{key} frozen after run start" for key in od09.DEFERRED_KEYS
    )


@pytest.mark.memory_only
def test_check_frozen_before_rejects_null_values(tmp_path):
    env = _new_env(tmp_path)
    _freeze(env)
    acceptance = _load_yaml(env.acceptance)
    od09 = _od09()
    expected = []
    for key in od09.DEFERRED_KEYS:
        for field in od09.PER_PLATFORM_FIELDS[key]:
            acceptance["thresholds"][key]["per_platform"]["windows"][field] = None
            expected.append(f"{key}.per_platform.windows.{field} missing")
    _write_yaml(env.acceptance, acceptance)
    problems = od09.check_frozen_before(env.acceptance, fakes_od09.ts(21, 0))
    for problem in expected:
        assert problem in problems
    assert any(problem.startswith("acceptance schema invalid: ") for problem in problems)
    assert problems != []


@pytest.mark.memory_only
def test_register_candidate_after_freeze_appends(tmp_path):
    env = _new_env(tmp_path)
    _freeze(env)
    od09 = _od09()
    record = od09.register_candidate_start(
        env.acceptance,
        item_id="S1B-L3-MIXED-48H-WINDOWS",
        platform="windows",
        run_id="s1b-1",
        started_at=fakes_od09.ts(21, 0),
        ledger_path=env.ledger,
    )
    rows = [json.loads(line) for line in env.ledger.read_text(encoding="utf-8").splitlines()]
    acceptance = _load_yaml(env.acceptance)
    assert len(rows) == 1
    assert rows[0] == record
    assert record["acceptance_revision"] == acceptance["revision"]
    assert record["acceptance_sha256"] == hashlib.sha256(env.acceptance.read_bytes()).hexdigest()
    before = env.ledger.read_bytes()
    with pytest.raises(od09.Od09Error) as error:
        od09.register_candidate_start(
            env.acceptance,
            item_id="S1B-L3-MIXED-48H-WINDOWS",
            platform="windows",
            run_id="s1b-too-early",
            started_at=fakes_od09.ts(20, 8),
            ledger_path=env.ledger,
        )
    assert error.value.code == "HARNESS_OD09_NOT_FROZEN"
    assert env.ledger.read_bytes() == before


@pytest.mark.memory_only
def test_register_candidate_rejects_unknown_item_or_platform(tmp_path):
    env = _new_env(tmp_path)
    _freeze(env)
    od09 = _od09()
    for item_id, platform, run_id in (
        ("S0-L3-P0P1", "linux", "bad-item"),
        ("S1B-L3-MIXED-48H-WINDOWS", "linux", "bad-platform"),
    ):
        before = env.ledger.read_bytes()
        with pytest.raises(ValueError):
            od09.register_candidate_start(
                env.acceptance,
                item_id=item_id,
                platform=platform,
                run_id=run_id,
                started_at=fakes_od09.ts(21, 0),
                ledger_path=env.ledger,
            )
        assert env.ledger.read_bytes() == before

    od09.register_candidate_start(
        env.acceptance,
        item_id="S1B-L3-MIXED-48H-WINDOWS",
        platform="windows",
        run_id="duplicate",
        started_at=fakes_od09.ts(21, 0),
        ledger_path=env.ledger,
    )
    before = env.ledger.read_bytes()
    with pytest.raises(ValueError):
        od09.register_candidate_start(
            env.acceptance,
            item_id="S1B-L3-MIXED-48H-WINDOWS",
            platform="windows",
            run_id="duplicate",
            started_at=fakes_od09.ts(21, 1),
            ledger_path=env.ledger,
        )
    assert env.ledger.read_bytes() == before


@pytest.mark.memory_only
def test_addenda_append_only(tmp_path):
    first = _new_env(tmp_path / "first")
    second = _new_env(tmp_path / "second")
    second.addenda = first.addenda
    _freeze(first)
    prefix = first.addenda.read_bytes()
    _freeze(second)
    result = first.addenda.read_bytes()
    assert result.startswith(prefix)
    assert result[len(prefix):].count("OD-09 延後項凍結".encode("utf-8")) == 1


@pytest.mark.memory_only
def test_od09_cli_exit_codes(tmp_path, capsys):
    env = _new_env(tmp_path)
    od09 = _od09()
    assert od09.main([
        "check", "--acceptance", str(env.acceptance), "--run-started-at", fakes_od09.ts(21, 0)
    ]) == 1
    _freeze(env)
    assert od09.main([
        "check", "--acceptance", str(env.acceptance), "--run-started-at", fakes_od09.ts(21, 0)
    ]) == 0

    un_frozen = _new_env(tmp_path / "unfrozen")
    assert od09.main([
        "register-candidate", "--acceptance", str(un_frozen.acceptance),
        "--item", "S1B-L3-MIXED-48H-WINDOWS", "--platform", "windows",
        "--run-id", "blocked", "--started-at", fakes_od09.ts(21, 0),
        "--ledger", str(un_frozen.ledger),
    ]) == 5
    output = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert output["reason_code"] == "HARNESS_OD09_NOT_FROZEN"
    assert od09.main([
        "register-candidate", "--acceptance", str(env.acceptance),
        "--item", "unknown", "--platform", "windows", "--run-id", "unknown",
        "--started-at", fakes_od09.ts(21, 0), "--ledger", str(env.ledger),
    ]) == 2


@pytest.mark.memory_only
def test_od09_procedure_doc():
    path = ROOT / "docs" / "rust-refactor" / "procedures" / "od09-deferred-freeze.md"
    text = path.read_text(encoding="utf-8")
    assert "S1B-L3-MIXED-48H-WINDOWS" in text
    assert "Stage 0 出口" in text
    assert "register-candidate" in text
    assert "python3 -m tools.stage3 od09" not in text

    od09 = _od09()
    parser = od09.build_parser()
    in_bash = False
    commands = []
    fence = chr(96) * 3
    for line in text.splitlines():
        if line.strip().startswith(fence + "bash"):
            in_bash = True
            continue
        if in_bash and line.strip().startswith(fence):
            in_bash = False
            continue
        stripped = line.strip()
        if in_bash and stripped.startswith("python3 -m realhw.acceptance.od09_freeze "):
            words = shlex.split(stripped)
            parser.parse_args(words[3:])
            commands.append(words[3])
    assert set(commands) >= {"stats", "propose", "freeze", "check", "register-candidate"}


def _doc_bash_lines() -> list[str]:
    path = ROOT / "docs" / "rust-refactor" / "procedures" / "od09-deferred-freeze.md"
    fence = chr(96) * 3
    in_bash = False
    lines: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith(fence + "bash"):
            in_bash = True
            continue
        if in_bash and line.strip().startswith(fence):
            in_bash = False
            continue
        if in_bash:
            lines.append(line.strip())
    return lines


@pytest.mark.memory_only
def test_od09_procedure_doc_gate_baseline_reruns_carry_wal_dir():
    """文件中的 gate 基線重跑必須帶 --wal-dir（缺漏時安全檢查 INSUFFICIENT_EVIDENCE，見 Review）。"""
    baseline_lines = [
        line for line in _doc_bash_lines()
        if line.startswith("python3 -m realhw.acceptance.gate ") and "--mode baseline" in line
    ]
    assert len(baseline_lines) == 2
    assert all("--wal-dir" in line for line in baseline_lines)
    text = (ROOT / "docs" / "rust-refactor" / "procedures" / "od09-deferred-freeze.md").read_text(encoding="utf-8")
    assert "EVIDENCE_ROOT" in text and "check_evidence_index.py" in text


@pytest.mark.memory_only
def test_baseline_stats_accepts_non_grid_snapshot_times(tmp_path):
    """引擎快照非整點（首快照 t≈1 s、間隔 300.5 s、無末快照）時，時長與 gate 的連續時長一致，不得誤判不足。"""
    env = _new_env(tmp_path)
    od09 = _od09()
    run = env.linux_run
    rows = run.snapshots[:-1]
    run.set_snapshots([dict(row, t=1 + index * 300.5) for index, row in enumerate(rows)])
    stats = od09.baseline_stats(env.linux_case_dir, platform="linux", release_manifest=env.linux_manifest)
    assert stats.duration_s == od09.workload(env.acceptance)["min_continuous_duration_s"]
    assert od09.validate_baseline(stats, acceptance_path=env.acceptance) == []


@pytest.mark.memory_only
def test_baseline_stats_requires_single_start_and_stop(tmp_path):
    env = _new_env(tmp_path)
    od09 = _od09()
    env.linux_run.set_events([row for row in env.linux_run.events if row["kind"] != "stop"])
    with pytest.raises(ValueError, match="start"):
        od09.baseline_stats(env.linux_case_dir, platform="linux", release_manifest=env.linux_manifest)


@pytest.mark.memory_only
def test_propose_rejects_swapped_platform_labels(tmp_path):
    env = _new_env(tmp_path)
    od09 = _od09()
    with pytest.raises(ValueError, match="不得互換"):
        od09.propose(env.windows_stats, env.linux_stats, baseline_gates=env.baseline_gates)


@pytest.mark.memory_only
def test_propose_rejects_violating_stats(tmp_path):
    env = _new_env(tmp_path)
    od09 = _od09()
    stats = replace(env.linux_stats, daemon_implementation="rust")
    with pytest.raises(ValueError, match="拒絕提案"):
        od09.propose(stats, env.windows_stats, baseline_gates=env.baseline_gates)


@pytest.mark.memory_only
def test_propose_rejects_mismatched_baseline_gate(tmp_path):
    env = _new_env(tmp_path)
    od09 = _od09()
    gates = dict(env.baseline_gates)
    gates["windows"] = env.baseline_gates["linux"]
    with pytest.raises(ValueError, match="windows 基線 gate"):
        od09.propose(env.linux_stats, env.windows_stats, baseline_gates=gates)


@pytest.mark.memory_only
def test_stats_cli_writes_nothing_on_violation(tmp_path, capsys):
    env = _new_env(tmp_path)
    od09 = _od09()
    manifest = tmp_path / "rust-manifest.json"
    _write_json(manifest, {"components": {"daemon": {"implementation": "rust"}}})
    out = tmp_path / "stats-linux.json"
    code = od09.main([
        "stats", "--case-dir", str(env.linux_case_dir), "--platform", "linux",
        "--release-manifest", str(manifest), "--out", str(out),
    ])
    assert code == 1
    assert not out.exists()
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])["ok"] is False


@pytest.mark.memory_only
def test_propose_cli_rejects_violating_stats(tmp_path, capsys):
    env = _new_env(tmp_path)
    od09 = _od09()
    linux_stats = tmp_path / "linux-stats.json"
    _write_json(linux_stats, asdict(replace(env.linux_stats, daemon_implementation="rust")))
    windows_stats = tmp_path / "windows-stats.json"
    _write_json(windows_stats, asdict(env.windows_stats))
    code = od09.main([
        "propose", "--linux-stats", str(linux_stats), "--windows-stats", str(windows_stats),
        "--baseline-gate", f"linux={env.baseline_gates['linux']}",
        "--baseline-gate", f"windows={env.baseline_gates['windows']}",
        "--out", str(tmp_path / "proposal.json"),
    ])
    assert code == 1
    assert not (tmp_path / "proposal.json").exists()


@pytest.mark.memory_only
def test_freeze_rejects_missing_ledger(tmp_path):
    env = _new_env(tmp_path)
    proposal = _make_proposal(env)
    env.ledger.unlink()
    od09 = _od09()
    before = env.acceptance.read_bytes()
    with pytest.raises(ValueError, match="ledger 不存在"):
        od09.freeze(
            env.acceptance, proposal, decision_id="OD-09-F1", approved_by="owner", overrides={},
            stage0_index=env.stage0_index, evidence_root=env.evidence_root, ledger_path=env.ledger,
            addenda_path=env.addenda, now=fakes_od09.ts(20, 9),
        )
    assert env.acceptance.read_bytes() == before


@pytest.mark.memory_only
def test_freeze_treats_non_utf8_ledger_as_too_late(tmp_path):
    env = _new_env(tmp_path)
    proposal = _make_proposal(env)
    env.ledger.write_bytes(b"\xff\xfe\n")
    od09 = _od09()
    before = env.acceptance.read_bytes()
    with pytest.raises(od09.Od09Error) as error:
        od09.freeze(
            env.acceptance, proposal, decision_id="OD-09-F1", approved_by="owner", overrides={},
            stage0_index=env.stage0_index, evidence_root=env.evidence_root, ledger_path=env.ledger,
            addenda_path=env.addenda, now=fakes_od09.ts(20, 9),
        )
    assert error.value.code == "HARNESS_OD09_TOO_LATE"
    assert env.acceptance.read_bytes() == before


@pytest.mark.memory_only
def test_freeze_rejects_backdated_now(tmp_path):
    env = _new_env(tmp_path)
    proposal = _make_proposal(env)
    proposal["generated_at"] = fakes_od09.ts(20, 10)
    proposal["proposal_sha256"] = _proposal_digest(proposal)
    od09 = _od09()
    before = env.acceptance.read_bytes()
    with pytest.raises(od09.Od09Error) as error:
        od09.freeze(
            env.acceptance, proposal, decision_id="OD-09-F1", approved_by="owner", overrides={},
            stage0_index=env.stage0_index, evidence_root=env.evidence_root, ledger_path=env.ledger,
            addenda_path=env.addenda, now=fakes_od09.ts(20, 9),
        )
    assert error.value.code == "HARNESS_OD09_BASELINE_INVALID"
    assert any("回填" in item for item in error.value.detail)
    assert env.acceptance.read_bytes() == before


@pytest.mark.memory_only
def test_freeze_stores_utc_frozen_at(tmp_path):
    env = _new_env(tmp_path)
    _freeze(env, now="2026-11-20T10:00:00+01:00")
    frozen = _load_yaml(env.acceptance)["thresholds"]
    assert all(frozen[key]["frozen_at"] == "2026-11-20T09:00:00+00:00" for key in _od09().DEFERRED_KEYS)


@pytest.mark.memory_only
def test_freeze_preflight_rejects_unwritable_addenda_before_replace(tmp_path):
    env = _new_env(tmp_path)
    proposal = _make_proposal(env)
    before = env.acceptance.read_bytes()
    env.addenda.unlink()
    env.addenda.mkdir()
    od09 = _od09()
    with pytest.raises(OSError):
        od09.freeze(
            env.acceptance, proposal, decision_id="OD-09-F1", approved_by="owner", overrides={},
            stage0_index=env.stage0_index, evidence_root=env.evidence_root, ledger_path=env.ledger,
            addenda_path=env.addenda, now=fakes_od09.ts(20, 9),
        )
    assert env.acceptance.read_bytes() == before
    env.addenda.rmdir()
    env.addenda.write_text("| decision_id | 主題 | 日期 | 內容 |\n", encoding="utf-8")
    report, _ = _freeze(env, proposal=proposal)
    assert report["revision"] != "" and env.acceptance.read_bytes() != before


@pytest.mark.memory_only
def test_freeze_retries_after_addenda_failure(tmp_path, monkeypatch):
    """門檻檔已凍結但附加列失敗：同一提案重跑補寫附加列並回傳同一份報告，不得回報 TOO_LATE。"""
    env = _new_env(tmp_path)
    proposal = _make_proposal(env)
    od09 = _od09()
    real_append = od09._append_addenda
    calls = {"count": 0}

    def flaky_append(path, row):
        calls["count"] += 1
        if calls["count"] == 1:
            raise OSError("simulated addenda write failure")
        return real_append(path, row)

    monkeypatch.setattr(od09, "_append_addenda", flaky_append)
    options = dict(
        decision_id="OD-09-F1", approved_by="owner", overrides={},
        stage0_index=env.stage0_index, evidence_root=env.evidence_root, ledger_path=env.ledger,
        addenda_path=env.addenda, now=fakes_od09.ts(20, 9),
    )
    with pytest.raises(OSError):
        od09.freeze(env.acceptance, proposal, **options)
    frozen_bytes = env.acceptance.read_bytes()
    assert "OD-09 延後項凍結" not in env.addenda.read_text(encoding="utf-8")
    report = od09.freeze(env.acceptance, proposal, **options)
    assert env.acceptance.read_bytes() == frozen_bytes
    assert report["acceptance_sha256"] == hashlib.sha256(frozen_bytes).hexdigest()
    assert env.addenda.read_text(encoding="utf-8").count("OD-09 延後項凍結") == 1


@pytest.mark.memory_only
def test_addenda_row_escapes_pipe_characters(tmp_path):
    env = _new_env(tmp_path)
    proposal = _make_proposal(env)
    _od09().freeze(
        env.acceptance, proposal, decision_id="OD-09|F1", approved_by="a|b", overrides={},
        stage0_index=env.stage0_index, evidence_root=env.evidence_root, ledger_path=env.ledger,
        addenda_path=env.addenda, now=fakes_od09.ts(20, 9),
    )
    text = env.addenda.read_text(encoding="utf-8")
    assert r"| OD-09\|F1 | OD-09 延後項凍結 |" in text
    assert r"approved_by=a\|b" in text


@pytest.mark.memory_only
@pytest.mark.parametrize("field,value", [
    ("frozen_at", ""),
    ("decision_id", ""),
    ("approved_by", ""),
    ("proposal_sha256", "not-a-hash"),
    ("baseline_evidence", {}),
    ("overrides", {"resources.rss_budget.linux": 1}),
    ("baseline_evidence", {
        "linux": {"item_id": "S0-L3-BASELINE-WINDOWS-48H", "artifact_path": "x", "sha256": "0" * 64},
        "windows": {"item_id": "S0-L3-BASELINE-WINDOWS-48H", "artifact_path": "x", "sha256": "0" * 64},
    }),
])
def test_schema_rejects_forged_freeze_record(tmp_path, field, value):
    """凍結紀錄欄位不得以空值或格式錯誤的內容通過 gate 的 schema（單一寫入者需工具可核對）。"""
    env = _new_env(tmp_path)
    _freeze(env)
    acceptance = _load_yaml(env.acceptance)
    acceptance["thresholds"]["resources"][field] = value
    _write_yaml(env.acceptance, acceptance)
    with pytest.raises(ValueError, match="不符 schema"):
        _gate().load_acceptance(env.acceptance)


@pytest.mark.memory_only
def test_cli_missing_inputs_exit_2(tmp_path):
    env = _new_env(tmp_path)
    od09 = _od09()
    assert od09.main([
        "check", "--acceptance", str(tmp_path / "missing.yaml"), "--run-started-at", fakes_od09.ts(21, 0),
    ]) == 2
    assert od09.main([
        "freeze", "--acceptance", str(env.acceptance), "--proposal", str(tmp_path / "missing.json"),
        "--decision-id", "OD-09-F1", "--approved-by", "owner", "--stage0-index", str(env.stage0_index),
        "--evidence-root", str(env.evidence_root), "--ledger", str(env.ledger), "--addenda", str(env.addenda),
        "--out", str(tmp_path / "report.json"),
    ]) == 2


@pytest.mark.process_rpc
def test_register_candidate_concurrent_duplicate_run_id_rejected(tmp_path):
    """兩個程序同時以同一 run_id 登記：ledger 鎖須使恰有一筆成功，另一筆回報重複。"""
    env = _new_env(tmp_path)
    _freeze(env)
    go = tmp_path / "go"
    script = textwrap.dedent(f"""
        import sys, time
        from pathlib import Path
        sys.path.insert(0, {str(ROOT)!r})
        from realhw.acceptance import od09_freeze
        go = Path({str(go)!r})
        while not go.exists():
            time.sleep(0.01)
        try:
            od09_freeze.register_candidate_start(
                Path({str(env.acceptance)!r}), item_id="S1B-L3-MIXED-48H-WINDOWS", platform="windows",
                run_id="dup", started_at={fakes_od09.ts(21, 0)!r}, ledger_path=Path({str(env.ledger)!r}),
            )
            print("ok")
        except ValueError:
            print("duplicate")
    """)
    procs = [
        subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for _ in range(2)
    ]
    go.write_text("go", encoding="utf-8")
    outputs = []
    for proc in procs:
        stdout, stderr = proc.communicate(timeout=120)
        assert proc.returncode == 0, stderr
        outputs.append(stdout.strip())
    assert sorted(outputs) == ["duplicate", "ok"]
    assert len(env.ledger.read_text(encoding="utf-8").splitlines()) == 1
