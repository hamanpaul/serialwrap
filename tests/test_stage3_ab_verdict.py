from __future__ import annotations

import copy
import importlib
import json
from pathlib import Path

import pytest


pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]

_NOW = "2026-10-21T00:00:00+00:00"


def _ab_verdict():
    return importlib.import_module("tools.stage3.ab_verdict")


def _empty_diff(**updates):
    diff = {
        "ok": True,
        "integrity": {"a": True, "b": True},
        "safety_violations": [],
        "differences": [],
        "a_cases": {},
        "b_cases": {},
    }
    diff.update(updates)
    return diff


def _l3_diff(**updates):
    diff = {
        "a_mode": "baseline",
        "b_mode": "candidate",
        "a_engine_verdict": "PASS",
        "a_baseline_result": "PASS",
        "b_engine_verdict": "PASS",
        "b_release_gate_result": "PASS",
        "b_baseline_compatible": True,
        "acceptance_revision": 4,
        "safety_violations": [],
        "integrity": {"a": True, "b": True},
    }
    diff.update(updates)
    return diff


@pytest.mark.parametrize(
    "row,layer,a_status,b_status,diff,registered,expected",
    [
        pytest.param(
            "safety-violations",
            "l1.5",
            "PASS",
            "PASS",
            _empty_diff(
                integrity={"a": False, "b": True},
                safety_violations=["A:c1:zero_tx_on_reject"],
            ),
            {},
            ("FAIL", None),
            id="safety-violations",
        ),
        pytest.param(
            "suite-infra-error",
            "l1.5",
            "INFRA_ERROR",
            "PASS",
            _empty_diff(),
            {},
            ("INFRA_ERROR", "HARNESS_SUITE_INFRA_ERROR"),
            id="suite-infra-error",
        ),
        pytest.param(
            "candidate-suite-infra-error",
            "l1.5",
            "PASS",
            "INFRA_ERROR",
            _empty_diff(),
            {},
            ("INFRA_ERROR", "HARNESS_SUITE_INFRA_ERROR"),
            id="candidate-suite-infra-error",
        ),
        pytest.param(
            "comparator-integrity-error",
            "l1.5",
            "PASS",
            "PASS",
            _empty_diff(integrity={"a": True, "b": False}),
            {},
            ("INFRA_ERROR", "HARNESS_COMPARATOR_ERROR"),
            id="comparator-integrity-error",
        ),
        pytest.param(
            "baseline-comparator-integrity-error",
            "l1.5",
            "PASS",
            "PASS",
            _empty_diff(integrity={"a": False, "b": True}),
            {},
            ("INFRA_ERROR", "HARNESS_COMPARATOR_ERROR"),
            id="baseline-comparator-integrity-error",
        ),
        pytest.param(
            "l2-pass",
            "l2",
            "FAIL",
            "FAIL",
            _empty_diff(verdict="PASS"),
            {},
            ("PASS", None),
            id="l2-pass",
        ),
        pytest.param(
            "l2-fail",
            "l2",
            "PASS",
            "PASS",
            _empty_diff(verdict="FAIL"),
            {},
            ("FAIL", None),
            id="l2-fail",
        ),
        pytest.param(
            "l2-incomplete",
            "l2",
            "PASS",
            "PASS",
            _empty_diff(verdict="INCOMPLETE"),
            {},
            ("INFRA_ERROR", "HARNESS_SUITE_INFRA_ERROR"),
            id="l2-incomplete",
        ),
        pytest.param(
            "l15-both-fail",
            "l1.5",
            "FAIL",
            "FAIL",
            _empty_diff(),
            {},
            ("FAIL", None),
            id="l15-both-fail",
        ),
        pytest.param(
            "l15-baseline-fail-candidate-pass",
            "l1.5",
            "FAIL",
            "PASS",
            _empty_diff(),
            {},
            ("BASELINE_INVALID", "HARNESS_BASELINE_INVALID"),
            id="l15-baseline-fail-candidate-pass",
        ),
        pytest.param(
            "l15-candidate-fail",
            "l1.5",
            "PASS",
            "FAIL",
            _empty_diff(),
            {},
            ("FAIL", None),
            id="l15-candidate-fail",
        ),
        pytest.param(
            "l15-rust-only-absolute-pass",
            "l1.5",
            "PASS",
            "PASS",
            _empty_diff(
                differences=[
                    {
                        "case": "sc15-x",
                        "path": "call[0].state",
                        "kind": "value",
                        "registered_change": "SC-15",
                    }
                ],
                b_cases={"sc15-x": "PASS"},
            ),
            {"SC-15": "absolute_only"},
            ("PASS", None),
            id="l15-rust-only-absolute-pass",
        ),
        pytest.param(
            "l15-no-registered-change",
            "l1.5",
            "PASS",
            "PASS",
            _empty_diff(
                differences=[
                    {"case": "x", "path": "call[0].ok", "kind": "value", "registered_change": None}
                ]
            ),
            {},
            ("FAIL", None),
            id="l15-no-registered-change",
        ),
        pytest.param(
            "l15-unknown-registered-change",
            "l1.5",
            "PASS",
            "PASS",
            _empty_diff(
                differences=[
                    {"case": "x", "path": "call[0].ok", "kind": "value", "registered_change": "BC-99"}
                ]
            ),
            {},
            ("FAIL", None),
            id="l15-unknown-registered-change",
        ),
        pytest.param(
            "l15-both-scope-change-differs",
            "l1.5",
            "PASS",
            "PASS",
            _empty_diff(
                differences=[
                    {"case": "bc01-x", "path": "call[0].ok", "kind": "value", "registered_change": "BC-01"}
                ]
            ),
            {"BC-01": "diff"},
            ("FAIL", None),
            id="l15-both-scope-change-differs",
        ),
        pytest.param(
            "l3-baseline-engine-fail",
            "l3",
            "PASS",
            "PASS",
            _l3_diff(a_engine_verdict="FAIL"),
            {},
            ("BASELINE_INVALID", "HARNESS_BASELINE_INVALID"),
            id="l3-baseline-engine-fail",
        ),
        pytest.param(
            "l3-baseline-result-fail",
            "l3",
            "PASS",
            "PASS",
            _l3_diff(a_baseline_result="FAIL"),
            {},
            ("BASELINE_INVALID", "HARNESS_BASELINE_INVALID"),
            id="l3-baseline-result-fail",
        ),
        pytest.param(
            "l3-candidate-engine-fail",
            "l3",
            "PASS",
            "PASS",
            _l3_diff(b_engine_verdict="FAIL"),
            {},
            ("FAIL", None),
            id="l3-candidate-engine-fail",
        ),
        pytest.param(
            "l3-suite-engine-inconsistent",
            "l3",
            "PASS",
            "FAIL",
            _l3_diff(),
            {},
            ("INFRA_ERROR", "HARNESS_SUITE_INFRA_ERROR"),
            id="l3-suite-engine-inconsistent",
        ),
        pytest.param(
            "l3-od09-blocked",
            "l3",
            "PASS",
            "PASS",
            _l3_diff(b_release_gate_result="BLOCKED"),
            {},
            ("INFRA_ERROR", "HARNESS_OD09_NOT_FROZEN"),
            id="l3-od09-blocked",
        ),
        pytest.param(
            "l3-insufficient-evidence",
            "l3",
            "PASS",
            "PASS",
            _l3_diff(b_release_gate_result="INSUFFICIENT_EVIDENCE"),
            {},
            ("INFRA_ERROR", "HARNESS_SUITE_INFRA_ERROR"),
            id="l3-insufficient-evidence",
        ),
        pytest.param(
            "l3-baseline-incompatible",
            "l3",
            "PASS",
            "PASS",
            _l3_diff(b_baseline_compatible=False),
            {},
            ("INFRA_ERROR", "HARNESS_SUITE_INFRA_ERROR"),
            id="l3-baseline-incompatible",
        ),
        pytest.param(
            "l3-release-gate-fail",
            "l3",
            "PASS",
            "PASS",
            _l3_diff(b_release_gate_result="FAIL"),
            {},
            ("FAIL", None),
            id="l3-release-gate-fail",
        ),
        pytest.param(
            "fallback-pass",
            "l1.5",
            "PASS",
            "PASS",
            _empty_diff(),
            {},
            ("PASS", None),
            id="fallback-pass",
        ),
    ],
)
def test_combine(row, layer, a_status, b_status, diff, registered, expected):
    verdict, reason_code, _notes = _ab_verdict().combine(
        layer,
        a_status=a_status,
        b_status=b_status,
        diff=copy.deepcopy(diff),
        registered=registered,
    )
    assert (verdict, reason_code) == expected


def test_python_and_rust_both_violate_safety_is_fail():
    diff = {
        "ok": False,
        "integrity": {"a": True, "b": True},
        "safety_violations": ["A:c1:zero_tx_on_reject", "B:c1:zero_tx_on_reject"],
        "differences": [],
        "a_cases": {},
        "b_cases": {},
    }
    verdict, code, notes = _ab_verdict().combine(
        "l1.5", a_status="FAIL", b_status="FAIL", diff=diff, registered={}
    )
    assert (verdict, code) == ("FAIL", None) and "zero_tx_on_reject" in " ".join(notes)


def test_rust_only_difference_skipped_but_absolute_must_pass():
    diff = {
        "ok": False,
        "integrity": {"a": True, "b": True},
        "safety_violations": [],
        "differences": [
            {"case": "sc15-x", "path": "call[0].state", "kind": "value", "registered_change": "SC-15"}
        ],
        "a_cases": {"sc15-x": "PASS"},
        "b_cases": {"sc15-x": "PASS"},
    }
    assert (
        _ab_verdict().combine(
            "l1.5", a_status="PASS", b_status="PASS", diff=diff, registered={"SC-15": "absolute_only"}
        )[0]
        == "PASS"
    )
    diff["b_cases"]["sc15-x"] = "FAIL"
    assert (
        _ab_verdict().combine(
            "l1.5", a_status="PASS", b_status="PASS", diff=diff, registered={"SC-15": "absolute_only"}
        )[0]
        == "FAIL"
    )


def test_both_scope_difference_is_fail():
    diff = {
        "ok": False,
        "integrity": {"a": True, "b": True},
        "safety_violations": [],
        "differences": [
            {"case": "bc01-x", "path": "call[0].ok", "kind": "value", "registered_change": "BC-01"}
        ],
        "a_cases": {"bc01-x": "PASS"},
        "b_cases": {"bc01-x": "PASS"},
    }
    verdict, _reason_code, notes = _ab_verdict().combine(
        "l1.5", a_status="PASS", b_status="PASS", diff=diff, registered={"BC-01": "diff"}
    )
    assert verdict == "FAIL" and notes == ["both-scope change differs: BC-01 bc01-x"]


def _l2_result(case_id: str, verdict: str = "PASS", reason: str = "") -> dict:
    return {
        "id": case_id,
        "verdict": verdict,
        "reason": reason,
        "category": "functional",
        "reason_code": None,
        "duration_s": 0.1,
        "evidence": [],
    }


def _l2_report(*results: dict, run_status: str | None = None) -> dict:
    meta = {} if run_status is None else {"run_status": run_status}
    return {"meta": meta, "results": list(results)}


def _compare_l2(ab_verdict, a_report, b_report, required_case_ids, *, cases=None, registered=None, now=_NOW):
    return ab_verdict.compare_l2_reports(
        a_report,
        b_report,
        required_case_ids=set(required_case_ids),
        cases=cases or {},
        registered_case_changes=registered or {},
        now=now,
    )


def test_l2_empty_report_incomplete():
    ab_verdict = _ab_verdict()
    result = _compare_l2(
        ab_verdict,
        _l2_report(_l2_result("p0-doctor")),
        _l2_report(),
        {"p0-doctor"},
    )
    assert result["verdict"] == "INCOMPLETE"


def test_l2_missing_required_case_incomplete():
    ab_verdict = _ab_verdict()
    result = _compare_l2(
        ab_verdict,
        _l2_report(_l2_result("required-id")),
        _l2_report(_l2_result("other-id")),
        {"required-id"},
    )
    assert result["verdict"] == "INCOMPLETE" and result["missing"]["b"] == ["required-id"]


def test_l2_duplicate_or_unknown_verdict_incomplete():
    ab_verdict = _ab_verdict()
    a_report = _l2_report(_l2_result("required-id"))
    duplicate_result = _compare_l2(
        ab_verdict,
        a_report,
        _l2_report(_l2_result("required-id"), _l2_result("required-id")),
        {"required-id"},
    )
    unknown_verdict_result = _compare_l2(
        ab_verdict,
        a_report,
        _l2_report(_l2_result("required-id", verdict="WEIRD")),
        {"required-id"},
    )
    assert duplicate_result["verdict"] == "INCOMPLETE"
    assert unknown_verdict_result["verdict"] == "INCOMPLETE"


def test_l2_required_skip_incomplete():
    ab_verdict = _ab_verdict()
    result = _compare_l2(
        ab_verdict,
        _l2_report(_l2_result("p0-doctor")),
        _l2_report(_l2_result("p0-doctor", verdict="SKIP")),
        {"p0-doctor"},
    )
    assert result["verdict"] == "INCOMPLETE"


def test_l2_baseline_red_case_not_run_level_fail():
    ab_verdict = _ab_verdict()
    a_report = _l2_report(
        _l2_result("required-id"),
        _l2_result("optional-red", verdict="FAIL"),
        run_status="FAIL",
    )
    b_report = _l2_report(_l2_result("required-id"), _l2_result("optional-red"))
    unregistered = _compare_l2(ab_verdict, a_report, b_report, {"required-id"})
    assert unregistered["verdict"] == "FAIL"
    assert unregistered["needs_review"] == [{"case": "optional-red", "a": "FAIL", "b": "PASS"}]
    assert (
        ab_verdict.combine(
            "l2", a_status="FAIL", b_status="PASS", diff=unregistered, registered={}
        )[0]
        == "FAIL"
    )

    registered = _compare_l2(
        ab_verdict,
        a_report,
        b_report,
        {"required-id"},
        registered={"optional-red": "BC-01"},
    )
    assert registered["verdict"] == "PASS"
    assert (
        ab_verdict.combine(
            "l2", a_status="FAIL", b_status="PASS", diff=registered, registered={}
        )[0]
        == "PASS"
    )


def test_l2_xfail_requires_sentinel_and_signature():
    ab_verdict = _ab_verdict()
    xfail_id = "xfail-case"
    xfail_case = {
        "expected": "XFAIL",
        "sentinel": {
            "issue": "#159",
            "signature": "tail mismatch",
            "expires": "2027-01-01T00:00:00+00:00",
        },
    }
    a_report = _l2_report(_l2_result(xfail_id))
    accepted = _compare_l2(
        ab_verdict,
        a_report,
        _l2_report(_l2_result(xfail_id, verdict="FAIL", reason="tail mismatch observed")),
        {xfail_id},
        cases={xfail_id: xfail_case},
    )
    assert accepted["xfail_accepted"] == [xfail_id]
    assert accepted["needs_review"] == [{"case": xfail_id, "a": "PASS", "b": "FAIL"}]
    assert accepted["verdict"] == "FAIL"
    assert ab_verdict.combine(
        "l2", a_status="PASS", b_status="FAIL", diff=accepted, registered={}
    )[0] == "FAIL"

    expired_case = copy.deepcopy(xfail_case)
    expired_case["sentinel"]["expires"] = "2026-10-20T00:00:00+00:00"
    expired = _compare_l2(
        ab_verdict,
        a_report,
        _l2_report(_l2_result(xfail_id, verdict="FAIL", reason="tail mismatch observed")),
        {xfail_id},
        cases={xfail_id: expired_case},
    )
    assert expired["expired_sentinels"] == [xfail_id] and expired["verdict"] == "FAIL"

    f4_id = "f4-background-result-tail-consistent"
    ordinary_failure = _compare_l2(
        ab_verdict,
        _l2_report(_l2_result(f4_id)),
        _l2_report(_l2_result(f4_id, verdict="FAIL", reason="tail mismatch")),
        {f4_id},
        cases={f4_id: {"expected": "PASS", "sentinel": None}},
    )
    assert ordinary_failure["b_failures"] == [f4_id]
    assert ordinary_failure["verdict"] == "FAIL"


def _write_gate(
    path: Path,
    *,
    mode: str,
    platform: str = "linux",
    revision: int = 4,
    engine_verdict: str = "PASS",
    baseline_result: str = "PASS",
    release_gate_result: str = "PASS",
    baseline_compatible: str = "PASS",
) -> Path:
    gate = {
        "schema_version": 1,
        "format": "release-gate.v1",
        "platform": platform,
        "mode": mode,
        "engine_result": {"verdict": engine_verdict, "metrics": {"samples": 1}},
        "release_gate_result": release_gate_result,
        "baseline_result": baseline_result,
        "acceptance": {"revision": revision, "workload_contract_sha256": "a" * 64},
        "run": {"cli_identity": {"resolved": "/bundle/bin/serialwrap", "sha256": "b" * 64}},
        "checks": [
            {"id": "evidence-ready", "group": "evidence", "result": "PASS", "detail": {}},
            {
                "id": "baseline_compatible",
                "group": "evidence",
                "result": baseline_compatible,
                "detail": {},
            },
        ],
    }
    path.write_text(json.dumps(gate, ensure_ascii=False), encoding="utf-8")
    return path


def test_l3_reads_gate_json_only(tmp_path: Path):
    a_path = _write_gate(tmp_path / "a-release-gate.json", mode="baseline")
    b_path = _write_gate(tmp_path / "b-release-gate.json", mode="candidate")
    result = _ab_verdict().compare_l3(a_path, b_path)
    assert result["a_mode"] == "baseline" and result["b_mode"] == "candidate"
    assert result["a_engine_verdict"] == "PASS" and result["b_engine_verdict"] == "PASS"
    assert result["a_baseline_result"] == "PASS" and result["b_release_gate_result"] == "PASS"
    assert result["b_baseline_compatible"] is True and result["acceptance_revision"] == 4
    assert result["integrity"] == {"a": True, "b": True}
    assert result["safety_violations"] == []


def test_l3_b_engine_fail_with_gate_pass(tmp_path: Path):
    a_path = _write_gate(tmp_path / "a-release-gate.json", mode="baseline")
    b_path = _write_gate(
        tmp_path / "b-release-gate.json",
        mode="candidate",
        engine_verdict="FAIL",
        release_gate_result="PASS",
    )
    diff = _ab_verdict().compare_l3(a_path, b_path)
    verdict, _reason_code, _notes = _ab_verdict().combine(
        "l3", a_status="PASS", b_status="PASS", diff=diff, registered={}
    )
    assert verdict == "FAIL"


def test_l3_baseline_engine_fail(tmp_path: Path):
    a_path = _write_gate(tmp_path / "a-release-gate.json", mode="baseline", engine_verdict="FAIL")
    b_path = _write_gate(tmp_path / "b-release-gate.json", mode="candidate")
    diff = _ab_verdict().compare_l3(a_path, b_path)
    verdict, reason_code, _notes = _ab_verdict().combine(
        "l3", a_status="PASS", b_status="PASS", diff=diff, registered={}
    )
    assert (verdict, reason_code) == ("BASELINE_INVALID", "HARNESS_BASELINE_INVALID")


def test_l3_revision_mismatch_error(tmp_path: Path):
    ab_verdict = _ab_verdict()
    a_path = _write_gate(tmp_path / "a-release-gate.json", mode="baseline", revision=4)
    b_path = _write_gate(tmp_path / "b-release-gate.json", mode="candidate", revision=5)
    harness_error = importlib.import_module("tools.stage3.reason_codes").HarnessError
    with pytest.raises(harness_error) as exc_info:
        ab_verdict.compare_l3(a_path, b_path)
    assert exc_info.value.code == "HARNESS_COMPARATOR_ERROR"


def test_load_registered_changes_maps_scope(tmp_path: Path):
    changes_path = tmp_path / "contracts" / "behavior_changes.yaml"
    changes_path.parent.mkdir()
    changes_path.write_text(
        "schema_version: 1\n"
        "revision: 4\n"
        "changes:\n"
        "  - id: \"BC-01\"\n"
        "    scope: \"both\"\n"
        "    ab_participation: \"diff\"\n"
        "  - id: \"SC-15\"\n"
        "    scope: \"rust_only\"\n"
        "    ab_participation: \"absolute_only\"\n",
        encoding="utf-8",
    )
    assert _ab_verdict().load_registered_changes(changes_path) == {
        "BC-01": "diff",
        "SC-15": "absolute_only",
    }
