"""T7：A/B 判定的紅燈契約。"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]

import fakes_ab_diff
from tools.stage3.ab_diff import diff_l15
import tools.stage3.ab_verdict as ab_verdict
from tools.stage3.reason_codes import HarnessError


ROOT = Path(__file__).resolve().parents[1]
RULES_PATH = ROOT / "contracts" / "ab_normalization.yaml"
BEHAVIOR_CHANGES_PATH = ROOT / "contracts" / "behavior_changes.yaml"
_NOW = "2026-11-27T00:00:00+00:00"


def _empty_diff(**updates: Any) -> dict[str, Any]:
    diff: dict[str, Any] = {
        "ok": True,
        "integrity": {"a": True, "b": True},
        "integrity_problems": {"a": [], "b": []},
        "safety_violations": [],
        "differences": [],
        "a_cases": {},
        "b_cases": {},
        "baseline_failures": [],
        "unverified_failures": [],
        "product_failures": [],
        "case_maps": [],
    }
    diff.update(updates)
    return diff


def _failure(side: str = "b", case: str = "confirmed-case") -> dict[str, str]:
    return {
        "side": side,
        "case": case,
        "source_ref": f"{case}/bridge/1/comparison.json#/invariants/0",
        "reason": "confirmed product failure",
    }


def _longrun_diff(**updates: Any) -> dict[str, Any]:
    diff = {
        "kind": "longrun",
        "a_mode": "baseline",
        "b_mode": "candidate",
        "a_engine_verdict": "PASS",
        "a_baseline_result": "PASS",
        "b_engine_verdict": "PASS",
        "b_release_gate_result": "PASS",
        "b_baseline_compatible": True,
        "acceptance_revision": "2026-11-21.1",
        "safety_violations": [],
        "integrity": {"a": True, "b": True},
        "product_failures": [],
    }
    diff.update(updates)
    return diff


def _l2_result(
    case_id: str,
    verdict: str = "PASS",
    reason: str = "",
    *,
    evidence: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "id": case_id,
        "verdict": verdict,
        "reason": reason,
        "category": "functional",
        "reason_code": None,
        "duration_s": 0.1,
        "evidence": list(evidence or []),
    }


def _l2_report(*results: dict[str, Any], run_status: str | None = None) -> dict[str, Any]:
    meta = {} if run_status is None else {"run_status": run_status}
    return {"meta": meta, "results": list(results)}


def _compare_l2(
    a_report: dict[str, Any],
    b_report: dict[str, Any],
    required_case_ids: set[str],
    *,
    cases: dict[str, dict[str, Any]] | None = None,
    registered: dict[str, str] | None = None,
    report_ref: str = "testpilot/report.json",
) -> dict[str, Any]:
    return ab_verdict.compare_l2_reports(
        a_report,
        b_report,
        required_case_ids=set(required_case_ids),
        cases=cases or {},
        registered_case_changes=registered or {},
        now=_NOW,
        a_report_ref=report_ref,
        b_report_ref=report_ref,
    )


def _cases_diff(comparison: dict[str, Any]) -> dict[str, Any]:
    return {"kind": "cases", **comparison}


def _write_gate(
    path: Path,
    *,
    mode: str,
    platform: str = "linux",
    revision: str = "2026-11-21.1",
    engine_verdict: str = "PASS",
    baseline_result: str | None = None,
    release_gate_result: str | None = None,
    baseline_compatible: str | None = None,
    evidence_results: tuple[str, ...] = ("PASS",),
    safety_results: tuple[tuple[str, str], ...] = (),
) -> Path:
    checks = [
        {"id": f"evidence-{index}", "group": "evidence", "status": "evaluated", "result": result, "detail": {}}
        for index, result in enumerate(evidence_results)
    ]
    checks.append({
        "id": "engine_verdict", "group": "engine", "status": "evaluated",
        "result": engine_verdict, "detail": {"verdict": engine_verdict},
    })
    checks.append({
        "id": "baseline_compatible", "group": "evidence", "status": "evaluated",
        "result": baseline_compatible or ("NOT_APPLICABLE_BASELINE" if mode == "baseline" else "PASS"),
        "detail": {},
    })
    checks.extend(
        {"id": check_id, "group": "safety", "status": "evaluated", "result": result, "detail": {}}
        for check_id, result in safety_results
    )
    check_results = {check["result"] for check in checks}
    summarized_result = (
        "FAIL" if "FAIL" in check_results
        else "INSUFFICIENT_EVIDENCE" if "INSUFFICIENT_EVIDENCE" in check_results
        else "BLOCKED" if "BLOCKED" in check_results
        else "PASS" if mode == "candidate"
        else "NOT_APPLICABLE_BASELINE"
    )
    gate = {
        "schema_version": "release-gate.v1",
        "created_at": "2026-11-27T00:00:00+00:00",
        "platform": platform,
        "mode": mode,
        "engine_result": {"verdict": engine_verdict, "metrics": {"samples": 1}},
        "release_gate_result": release_gate_result or summarized_result,
        "baseline_result": baseline_result or ("FAIL" if engine_verdict == "FAIL" else "PASS") if mode == "baseline" else baseline_result,
        "acceptance": {
            "path": "acceptance.yaml",
            "revision": revision,
            "sha256": "a" * 64,
            "git_blob_sha1": "b" * 40,
            "workload_contract_sha256": "c" * 64,
            "effective_content": {},
        },
        "run": {"cli_identity": {"resolved": "<bundle>/bin/serialwrap", "sha256": "b" * 64}},
        "checks": checks,
        "counts": {},
        "stuck": {},
        "walscan": {},
        "evidence_hashes": {},
        "excl_evidence_hashes": {},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((json.dumps(gate, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"))
    return path


def _write_l15_pair(
    tmp_path: Path,
    *,
    case_id: str,
    required: set[str],
    runner: str = "bridge",
    a_kwargs: dict[str, Any] | None = None,
    b_kwargs: dict[str, Any] | None = None,
) -> tuple[Path, Path, dict[str, Any]]:
    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    fakes_ab_diff.write_run(a_dir, case_id, side="a", runner=runner, **(a_kwargs or {}))
    fakes_ab_diff.write_run(b_dir, case_id, side="b", runner=runner, **(b_kwargs or {}))
    diff = diff_l15(
        a_dir,
        b_dir,
        registered={},
        case_changes={},
        required_case_ids=set(required),
        rules_path=RULES_PATH,
    )
    return a_dir, b_dir, diff


def _source_ref_exists(report_dir: Path, source_ref: str) -> bool:
    relative = source_ref.split("#", 1)[0]
    path = Path(relative)
    return not path.is_absolute() and (report_dir / path).is_file()


@pytest.mark.parametrize(
    "row,layer,a_status,b_status,diff,registered,expected",
    [
        pytest.param(
            "safety-violations",
            "l1.5",
            "INFRA_ERROR",
            "INFRA_ERROR",
            _empty_diff(integrity={"a": False, "b": False}, safety_violations=["B:c1:zero_tx_on_reject"]),
            {},
            ("FAIL", None),
            id="safety-violations",
        ),
        pytest.param(
            "confirmed-product-failure",
            "l1.5",
            "INFRA_ERROR",
            "PASS",
            _empty_diff(
                integrity={"a": True, "b": False},
                integrity_problems={"a": [], "b": ["missing required case: other-required"]},
                b_cases={"confirmed-case": "FAIL"},
                product_failures=[_failure()],
            ),
            {},
            ("FAIL", None),
            id="confirmed-product-failure",
        ),
        pytest.param(
            "baseline-suite-infra",
            "l1.5",
            "INFRA_ERROR",
            "PASS",
            _empty_diff(),
            {},
            ("INFRA_ERROR", "HARNESS_SUITE_INFRA_ERROR"),
            id="baseline-suite-infra",
        ),
        pytest.param(
            "candidate-suite-infra",
            "l1.5",
            "PASS",
            "INFRA_ERROR",
            _empty_diff(),
            {},
            ("INFRA_ERROR", "HARNESS_SUITE_INFRA_ERROR"),
            id="candidate-suite-infra",
        ),
        pytest.param(
            "baseline-comparator-integrity",
            "l1.5",
            "PASS",
            "PASS",
            _empty_diff(integrity={"a": False, "b": True}),
            {},
            ("INFRA_ERROR", "HARNESS_COMPARATOR_ERROR"),
            id="baseline-comparator-integrity",
        ),
        pytest.param(
            "candidate-comparator-integrity",
            "l1.5",
            "PASS",
            "PASS",
            _empty_diff(integrity={"a": True, "b": False}),
            {},
            ("INFRA_ERROR", "HARNESS_COMPARATOR_ERROR"),
            id="candidate-comparator-integrity",
        ),
        pytest.param(
            "l15-status-fail-without-case-evidence",
            "l1.5",
            "PASS",
            "FAIL",
            _empty_diff(a_cases={"c1": "PASS"}, b_cases={"c1": "PASS"}),
            {},
            ("INFRA_ERROR", "HARNESS_SUITE_INFRA_ERROR"),
            id="l15-status-fail-without-case-evidence",
        ),
        pytest.param(
            "l2-pass-ignores-run-status",
            "l2",
            "FAIL",
            "FAIL",
            _empty_diff(verdict="PASS", integrity={"a": True, "b": True}),
            {},
            ("PASS", None),
            id="l2-pass-ignores-run-status",
        ),
        pytest.param(
            "l2-fail",
            "l2",
            "PASS",
            "PASS",
            _empty_diff(
                verdict="FAIL",
                integrity={"a": True, "b": True},
                b_failures=["confirmed-case"],
                b_cases={"confirmed-case": "FAIL"},
                product_failures=[_failure()],
            ),
            {},
            ("FAIL", None),
            id="l2-fail",
        ),
        pytest.param(
            "l2-incomplete",
            "l2",
            "PASS",
            "PASS",
            _empty_diff(verdict="INCOMPLETE", integrity={"a": True, "b": False}),
            {},
            ("INFRA_ERROR", "HARNESS_SUITE_INFRA_ERROR"),
            id="l2-incomplete",
        ),
        pytest.param(
            "l3-cases-pass",
            "l3",
            "FAIL",
            "FAIL",
            _empty_diff(kind="cases", verdict="PASS", integrity={"a": True, "b": True}),
            {},
            ("PASS", None),
            id="l3-cases-pass",
        ),
        pytest.param(
            "l3-cases-fail",
            "l3",
            "PASS",
            "PASS",
            _empty_diff(
                kind="cases",
                verdict="FAIL",
                integrity={"a": True, "b": True},
                b_failures=["confirmed-case"],
                b_cases={"confirmed-case": "FAIL"},
                product_failures=[_failure()],
            ),
            {},
            ("FAIL", None),
            id="l3-cases-fail",
        ),
        pytest.param(
            "l3-cases-incomplete",
            "l3",
            "PASS",
            "PASS",
            _empty_diff(kind="cases", verdict="INCOMPLETE", integrity={"a": True, "b": False}),
            {},
            ("INFRA_ERROR", "HARNESS_SUITE_INFRA_ERROR"),
            id="l3-cases-incomplete",
        ),
        pytest.param(
            "l15-both-fail-with-evidence",
            "l1.5",
            "FAIL",
            "FAIL",
            _empty_diff(
                a_cases={"baseline-case": "FAIL"},
                b_cases={"confirmed-case": "FAIL"},
                baseline_failures=[_failure("a", "baseline-case")],
                product_failures=[_failure()],
            ),
            {},
            ("FAIL", None),
            id="l15-both-fail-with-evidence",
        ),
        pytest.param(
            "l15-baseline-fail-candidate-pass",
            "l1.5",
            "FAIL",
            "PASS",
            _empty_diff(
                a_cases={"baseline-case": "FAIL"},
                b_cases={"baseline-case": "PASS"},
                baseline_failures=[_failure("a", "baseline-case")],
            ),
            {},
            ("BASELINE_INVALID", "HARNESS_BASELINE_INVALID"),
            id="l15-baseline-fail-candidate-pass",
        ),
        pytest.param(
            "l15-candidate-fail-with-evidence",
            "l1.5",
            "PASS",
            "FAIL",
            _empty_diff(b_cases={"confirmed-case": "FAIL"}, product_failures=[_failure()]),
            {},
            ("FAIL", None),
            id="l15-candidate-fail-with-evidence",
        ),
        pytest.param(
            "l15-rust-only-absolute-pass",
            "l1.5",
            "PASS",
            "PASS",
            _empty_diff(
                differences=[{"case": "sc15-x", "path": "call[0].state", "kind": "value", "registered_change": "SC-15", "verified": True, "source_refs": {"a": "a/call.json#L1", "b": "b/call.json#L1"}}],
                b_cases={"sc15-x": "PASS"},
            ),
            {"SC-15": "absolute_only"},
            ("PASS", None),
            id="l15-rust-only-absolute-pass",
        ),
        pytest.param(
            "l15-unregistered-difference",
            "l1.5",
            "PASS",
            "PASS",
            _empty_diff(
                differences=[{"case": "unregistered-case", "path": "call[0].ok", "kind": "value", "registered_change": None, "verified": True, "source_refs": {"a": "a/call.json#L1", "b": "b/call.json#L1"}}]
            ),
            {},
            ("FAIL", None),
            id="l15-unregistered-difference",
        ),
        pytest.param(
            "l15-unknown-change",
            "l1.5",
            "PASS",
            "PASS",
            _empty_diff(
                differences=[{"case": "unknown-change-case", "path": "call[0].ok", "kind": "rc", "registered_change": "BC-99", "verified": True, "source_refs": {"a": "a/call.json#L1", "b": "b/call.json#L1"}}]
            ),
            {},
            ("FAIL", None),
            id="l15-unknown-change",
        ),
        pytest.param(
            "l15-both-scope-difference",
            "l1.5",
            "PASS",
            "PASS",
            _empty_diff(
                differences=[{"case": "bc01-x", "path": "call[0].ok", "kind": "verdict", "registered_change": "BC-01", "verified": True, "source_refs": {"a": "a/call.json#L1", "b": "b/call.json#L1"}}]
            ),
            {"BC-01": "diff"},
            ("FAIL", None),
            id="l15-both-scope-difference",
        ),
        pytest.param(
            "l3-baseline-engine-fail",
            "l3",
            "PASS",
            "PASS",
            _longrun_diff(a_engine_verdict="FAIL"),
            {},
            ("BASELINE_INVALID", "HARNESS_BASELINE_INVALID"),
            id="l3-baseline-engine-fail",
        ),
        pytest.param(
            "l3-baseline-result-fail",
            "l3",
            "PASS",
            "PASS",
            _longrun_diff(a_baseline_result="FAIL"),
            {},
            ("BASELINE_INVALID", "HARNESS_BASELINE_INVALID"),
            id="l3-baseline-result-fail",
        ),
        pytest.param(
            "l3-candidate-engine-fail-with-evidence",
            "l3",
            "PASS",
            "PASS",
            _longrun_diff(b_engine_verdict="FAIL", product_failures=[_failure()]),
            {},
            ("FAIL", None),
            id="l3-candidate-engine-fail-with-evidence",
        ),
        pytest.param(
            "l3-unknown-candidate-engine-verdict",
            "l3",
            "PASS",
            "PASS",
            _longrun_diff(b_engine_verdict="INSUFFICIENT_EVIDENCE"),
            {},
            ("INFRA_ERROR", "HARNESS_SUITE_INFRA_ERROR"),
            id="l3-unknown-candidate-engine-verdict",
        ),
        pytest.param(
            "l3-suite-engine-inconsistent",
            "l3",
            "PASS",
            "FAIL",
            _longrun_diff(),
            {},
            ("INFRA_ERROR", "HARNESS_SUITE_INFRA_ERROR"),
            id="l3-suite-engine-inconsistent",
        ),
        pytest.param(
            "l3-od09-blocked",
            "l3",
            "PASS",
            "PASS",
            _longrun_diff(b_release_gate_result="BLOCKED"),
            {},
            ("INFRA_ERROR", "HARNESS_OD09_NOT_FROZEN"),
            id="l3-od09-blocked",
        ),
        pytest.param(
            "l3-insufficient-evidence",
            "l3",
            "PASS",
            "PASS",
            _longrun_diff(b_release_gate_result="INSUFFICIENT_EVIDENCE"),
            {},
            ("INFRA_ERROR", "HARNESS_SUITE_INFRA_ERROR"),
            id="l3-insufficient-evidence",
        ),
        pytest.param(
            "l3-baseline-incompatible",
            "l3",
            "PASS",
            "PASS",
            _longrun_diff(b_baseline_compatible=False),
            {},
            ("INFRA_ERROR", "HARNESS_SUITE_INFRA_ERROR"),
            id="l3-baseline-incompatible",
        ),
        pytest.param(
            "l3-release-gate-fail-with-evidence",
            "l3",
            "PASS",
            "PASS",
            _longrun_diff(b_release_gate_result="FAIL", product_failures=[_failure()]),
            {},
            ("FAIL", None),
            id="l3-release-gate-fail-with-evidence",
        ),
        pytest.param(
            "l3-unknown-release-gate-result",
            "l3",
            "PASS",
            "PASS",
            _longrun_diff(b_release_gate_result="NOT_APPLICABLE_BASELINE"),
            {},
            ("INFRA_ERROR", "HARNESS_SUITE_INFRA_ERROR"),
            id="l3-unknown-release-gate-result",
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
    verdict, reason_code, _notes = ab_verdict.combine(
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
    verdict, code, notes = ab_verdict.combine("l1.5", a_status="FAIL", b_status="FAIL", diff=diff, registered={})
    assert (verdict, code) == ("FAIL", None) and "zero_tx_on_reject" in " ".join(notes)


def test_rust_only_difference_skipped_but_absolute_must_pass():
    diff = {
        "ok": False,
        "integrity": {"a": True, "b": True},
        "safety_violations": [],
        "differences": [{"case": "sc15-x", "path": "call[0].state", "kind": "value", "registered_change": "SC-15", "verified": True, "source_refs": {"a": "a/call.json#L1", "b": "b/call.json#L1"}}],
        "a_cases": {"sc15-x": "PASS"},
        "b_cases": {"sc15-x": "PASS"},
    }
    assert ab_verdict.combine(
        "l1.5", a_status="PASS", b_status="PASS", diff=diff, registered={"SC-15": "absolute_only"}
    )[0] == "PASS"
    diff["b_cases"]["sc15-x"] = "FAIL"
    assert ab_verdict.combine(
        "l1.5", a_status="PASS", b_status="PASS", diff=diff, registered={"SC-15": "absolute_only"}
    )[0] == "FAIL"


def test_both_scope_difference_is_fail():
    diff = {
        "ok": False,
        "integrity": {"a": True, "b": True},
        "safety_violations": [],
        "differences": [{"case": "bc01-x", "path": "call[0].ok", "kind": "value", "registered_change": "BC-01", "verified": True, "source_refs": {"a": "a/call.json#L1", "b": "b/call.json#L1"}}],
        "a_cases": {"bc01-x": "PASS"},
        "b_cases": {"bc01-x": "PASS"},
    }
    verdict, _reason_code, notes = ab_verdict.combine(
        "l1.5", a_status="PASS", b_status="PASS", diff=diff, registered={"BC-01": "diff"}
    )
    assert verdict == "FAIL"
    assert notes[0] == "both-scope change differs: BC-01 bc01-x"
    assert len(notes) == 2 and "b/call.json#L1" in notes[1]


@pytest.mark.parametrize("kind", ["value", "rc", "verdict", "argv", "call_count"])
def test_registered_difference_kinds_follow_change_participation(kind: str):
    diff = _empty_diff(
        differences=[
            {"case": "rust-case", "path": "call[0]", "kind": kind, "registered_change": "SC-15", "verified": True, "source_refs": {"a": "rust-case/a.json#L1", "b": "rust-case/b.json#L1"}}
        ],
        b_cases={"rust-case": "PASS"},
    )
    assert ab_verdict.combine(
        "l1.5", a_status="PASS", b_status="PASS", diff=diff, registered={"SC-15": "absolute_only"}
    )[0] == "PASS"
    assert ab_verdict.combine(
        "l1.5", a_status="PASS", b_status="PASS", diff=diff, registered={"SC-15": "diff"}
    )[0] == "FAIL"


def test_unverified_failures_are_infra_and_remain_in_notes():
    source_ref = "unsafe-case/bridge/1/comparison.json#/invariants/0"
    diff = _empty_diff(
        integrity={"a": True, "b": False},
        integrity_problems={"a": [], "b": ["unverified failure source"]},
        unverified_failures=[
            {"side": "b", "case": "unsafe-case", "source_ref": source_ref, "reason": "FAIL", "problems": ["identity unavailable"]}
        ],
    )
    verdict, code, notes = ab_verdict.combine(
        "l1.5", a_status="PASS", b_status="PASS", diff=diff, registered={}
    )
    assert (verdict, code) == ("INFRA_ERROR", "HARNESS_COMPARATOR_ERROR")
    assert "unsafe-case" in " ".join(notes) and source_ref in " ".join(notes)


def test_l2_empty_report_incomplete():
    result = _compare_l2(
        _l2_report(_l2_result("p0-doctor")),
        _l2_report(),
        {"p0-doctor"},
    )
    assert result["verdict"] == "INCOMPLETE"
    assert result["integrity"]["b"] is False


def test_l2_missing_required_case_incomplete():
    result = _compare_l2(
        _l2_report(_l2_result("required-id")),
        _l2_report(_l2_result("other-id")),
        {"required-id"},
    )
    assert result["verdict"] == "INCOMPLETE" and result["missing"]["b"] == ["required-id"]


def test_l2_duplicate_or_unknown_verdict_incomplete():
    a_report = _l2_report(_l2_result("required-id"))
    duplicate = _compare_l2(
        a_report,
        _l2_report(_l2_result("required-id"), _l2_result("required-id")),
        {"required-id"},
    )
    unknown = _compare_l2(
        a_report,
        _l2_report(_l2_result("required-id", verdict="WEIRD")),
        {"required-id"},
    )
    assert duplicate["verdict"] == "INCOMPLETE" and duplicate["duplicates"]["b"] == ["required-id"]
    assert unknown["verdict"] == "INCOMPLETE" and unknown["unknown_verdicts"]["b"] == ["required-id"]


def test_l2_collects_all_integrity_problems_without_early_return():
    required = {"missing-id", "duplicate-id", "unknown-id", "skip-id"}
    a_report = _l2_report(*(_l2_result(case_id) for case_id in sorted(required)))
    b_report = _l2_report(
        _l2_result("duplicate-id"),
        _l2_result("duplicate-id"),
        _l2_result("unknown-id", verdict="WEIRD"),
        _l2_result("skip-id", verdict="SKIP"),
    )
    result = _compare_l2(a_report, b_report, required)
    assert result["verdict"] == "INCOMPLETE"
    assert result["missing"]["b"] == ["missing-id"]
    assert result["duplicates"]["b"] == ["duplicate-id"]
    assert result["unknown_verdicts"]["b"] == ["unknown-id"]
    assert result["required_skips"]["b"] == ["skip-id"]
    assert result["integrity"]["b"] is False


@pytest.mark.parametrize("verdict", ["SKIP", "ERROR"])
def test_l2_required_skip_incomplete(verdict: str):
    result = _compare_l2(
        _l2_report(_l2_result("p0-doctor")),
        _l2_report(_l2_result("p0-doctor", verdict=verdict)),
        {"p0-doctor"},
    )
    assert result["verdict"] == "INCOMPLETE" and result["required_skips"]["b"] == ["p0-doctor"]


def test_l15_status_fail_without_case_evidence_is_infra(tmp_path: Path):
    case_id = "reported-pass"
    _a_dir, _b_dir, diff = _write_l15_pair(tmp_path, case_id=case_id, required={case_id})
    verdict, reason_code, notes = ab_verdict.combine(
        "l1.5", a_status="PASS", b_status="FAIL", diff=diff, registered={}
    )
    assert (verdict, reason_code) == ("INFRA_ERROR", "HARNESS_SUITE_INFRA_ERROR")
    assert "inconsistent" in " ".join(notes) or "原始報告" in " ".join(notes)


@pytest.mark.parametrize("layer", ["l1.5", "l2", "l3-cases", "l3-longrun"])
def test_confirmed_fail_wins_over_incomplete_and_infra(tmp_path: Path, layer: str):
    required = {"confirmed-case", "missing-required"}
    if layer == "l1.5":
        a_dir = tmp_path / "a"
        b_dir = tmp_path / "b"
        fakes_ab_diff.write_run(a_dir, "confirmed-case", side="a")
        fakes_ab_diff.write_run(b_dir, "confirmed-case", side="b", verdict="FAIL")
        fakes_ab_diff.write_run(a_dir, "missing-required", side="a")
        diff = diff_l15(
            a_dir,
            b_dir,
            registered={},
            case_changes={},
            required_case_ids=required,
            rules_path=RULES_PATH,
        )
        combine_layer = "l1.5"
        a_status, b_status = "PASS", "INFRA_ERROR"
    elif layer in {"l2", "l3-cases"}:
        evidence = [{"source_ref": "testpilot/report.json#results[0]"}]
        a_report = _l2_report(_l2_result("confirmed-case"), run_status="PASS")
        b_report = _l2_report(
            _l2_result("confirmed-case", verdict="FAIL", reason="confirmed target failure", evidence=evidence),
            run_status="INFRA_ERROR",
        )
        diff = _compare_l2(a_report, b_report, required)
        combine_layer = "l2" if layer == "l2" else "l3"
        if layer == "l3-cases":
            diff = _cases_diff(diff)
        a_status, b_status = "PASS", "INFRA_ERROR"
    else:
        diff = _longrun_diff(
            integrity={"a": False, "b": False},
            product_failures=[_failure()],
            integrity_problems={"a": ["A gate unavailable"], "b": ["missing required observation"]},
        )
        combine_layer = "l3"
        a_status, b_status = "INFRA_ERROR", "PASS"

    verdict, _reason_code, notes = ab_verdict.combine(
        combine_layer, a_status=a_status, b_status=b_status, diff=diff, registered={}
    )
    assert verdict == "FAIL"
    if layer in {"l1.5", "l2", "l3-cases"}:
        assert diff["integrity"]["b"] is False
        assert any(row["side"] == "b" and row["case"] == "confirmed-case" for row in diff["product_failures"])
        assert "missing-required" in " ".join(notes)
    else:
        assert diff["product_failures"][0]["source_ref"]

    without_confirmed_fail = copy.deepcopy(diff)
    without_confirmed_fail["product_failures"] = []
    if layer == "l1.5":
        without_confirmed_fail["differences"] = []
    if "b_failures" in without_confirmed_fail:
        without_confirmed_fail["b_failures"] = []
        without_confirmed_fail["expired_sentinels"] = []
        without_confirmed_fail["needs_review"] = []
        without_confirmed_fail["safety_violations"] = []
        without_confirmed_fail["verdict"] = "INCOMPLETE"
    if layer == "l3-longrun":
        without_confirmed_fail["b_engine_verdict"] = "PASS"
        without_confirmed_fail["b_release_gate_result"] = "INSUFFICIENT_EVIDENCE"
    control, _control_code, _control_notes = ab_verdict.combine(
        combine_layer, a_status=a_status, b_status=b_status, diff=without_confirmed_fail, registered={}
    )
    assert control == "INFRA_ERROR"


def test_l2_baseline_red_case_not_run_level_fail():
    a_report = _l2_report(
        _l2_result("required-id"),
        _l2_result("optional-red", verdict="FAIL", reason="baseline target failure"),
        run_status="FAIL",
    )
    b_report = _l2_report(_l2_result("required-id"), _l2_result("optional-red"))
    unregistered = _compare_l2(a_report, b_report, {"required-id"})
    assert unregistered["verdict"] == "FAIL"
    assert unregistered["needs_review"] == [{"case": "optional-red", "a": "FAIL", "b": "PASS"}]
    assert ab_verdict.combine("l2", a_status="FAIL", b_status="PASS", diff=unregistered, registered={})[0] == "FAIL"

    registered = _compare_l2(
        a_report,
        b_report,
        {"required-id"},
        registered={"optional-red": "BC-01"},
    )
    assert registered["verdict"] == "PASS"
    assert ab_verdict.combine("l2", a_status="FAIL", b_status="PASS", diff=registered, registered={})[0] == "PASS"


def test_l2_xfail_requires_sentinel_and_signature():
    xfail_id = "xfail-case"
    xfail_case = {
        "expected": "XFAIL",
        "sentinel": {
            "issue": "#159",
            "signature": "tail mismatch",
            "expires": "2027-01-01T00:00:00+00:00",
        },
    }
    both_fail = _l2_report(
        _l2_result(xfail_id, verdict="FAIL", reason="tail mismatch in baseline"),
    )
    accepted = _compare_l2(
        both_fail,
        _l2_report(_l2_result(xfail_id, verdict="FAIL", reason="tail mismatch observed")),
        {xfail_id},
        cases={xfail_id: xfail_case},
    )
    assert accepted["xfail_accepted"] == [xfail_id] and accepted["verdict"] == "PASS"

    xfail_changed = _compare_l2(
        _l2_report(_l2_result(xfail_id)),
        _l2_report(_l2_result(xfail_id, verdict="FAIL", reason="tail mismatch observed")),
        {xfail_id},
        cases={xfail_id: xfail_case},
    )
    assert xfail_changed["xfail_accepted"] == [xfail_id]
    assert xfail_changed["needs_review"] == [{"case": xfail_id, "a": "PASS", "b": "FAIL"}]
    assert xfail_changed["verdict"] == "FAIL"

    expired_case = copy.deepcopy(xfail_case)
    expired_case["sentinel"]["expires"] = _NOW
    expired = _compare_l2(
        both_fail,
        _l2_report(_l2_result(xfail_id, verdict="FAIL", reason="tail mismatch observed")),
        {xfail_id},
        cases={xfail_id: expired_case},
    )
    assert expired["expired_sentinels"] == [xfail_id] and expired["verdict"] == "FAIL"

    missing_signature = _compare_l2(
        both_fail,
        _l2_report(_l2_result(xfail_id, verdict="FAIL", reason="different failure")),
        {xfail_id},
        cases={xfail_id: xfail_case},
    )
    assert missing_signature["b_failures"] == [xfail_id]

    unsafe_xfail_case = copy.deepcopy(xfail_case)
    unsafe_xfail_case["invariants"] = ["zero_tx_on_reject"]
    unsafe_xfail = _compare_l2(
        both_fail,
        _l2_report(_l2_result(xfail_id, verdict="FAIL", reason="tail mismatch observed")),
        {xfail_id},
        cases={xfail_id: unsafe_xfail_case},
    )
    assert unsafe_xfail["xfail_accepted"] == []
    assert unsafe_xfail["safety_violations"] == []
    assert unsafe_xfail["b_failures"] == [xfail_id]

    ordinary_id = "f4-background-result-tail-consistent"
    ordinary = _compare_l2(
        _l2_report(_l2_result(ordinary_id)),
        _l2_report(_l2_result(ordinary_id, verdict="FAIL", reason="tail mismatch")),
        {ordinary_id},
        cases={ordinary_id: {"expected": "PASS", "sentinel": None}},
    )
    assert ordinary["b_failures"] == [ordinary_id] and ordinary["verdict"] == "FAIL"


def test_l2_product_failure_with_invariant_coverage_and_missing_required_case():
    case_id = "ordinary-failure-case"
    required = {case_id, "missing-required"}
    cases = {case_id: {"invariants": ["zero_tx_on_reject"], "expected": "PASS", "sentinel": None}}
    a_report = _l2_report(_l2_result(case_id))
    b_report = _l2_report(
        _l2_result(
            case_id,
            verdict="FAIL",
            reason="functional assertion failed",
            evidence=[{"source_ref": "testpilot/report.json#results[0]"}],
        )
    )
    result = _compare_l2(a_report, b_report, required, cases=cases)
    assert result["verdict"] == "FAIL" and result["integrity"]["b"] is False
    assert result["safety_violations"] == []
    assert result["product_failures"][0]["case"] == case_id


def test_l3_reads_gate_json_only(tmp_path: Path):
    a_path = _write_gate(tmp_path / "a" / "release-gate.json", mode="baseline")
    b_path = _write_gate(tmp_path / "b" / "release-gate.json", mode="candidate")
    result = ab_verdict.compare_l3(a_path, b_path)
    assert result["a_mode"] == "baseline" and result["b_mode"] == "candidate"
    assert result["a_engine_verdict"] == "PASS" and result["b_engine_verdict"] == "PASS"
    assert result["a_baseline_result"] == "PASS" and result["b_release_gate_result"] == "PASS"
    assert result["b_baseline_compatible"] is True and result["acceptance_revision"] == "2026-11-21.1"
    assert result["integrity"] == {"a": True, "b": True}
    assert result["safety_violations"] == []


def test_l3_b_engine_fail_with_gate_pass(tmp_path: Path):
    a_path = _write_gate(tmp_path / "a" / "release-gate.json", mode="baseline")
    b_path = _write_gate(
        tmp_path / "b" / "release-gate.json",
        mode="candidate",
        engine_verdict="FAIL",
        release_gate_result="PASS",
    )
    diff = ab_verdict.compare_l3(a_path, b_path)
    assert any(row["side"] == "b" and row["source_ref"] for row in diff["product_failures"])
    verdict, _reason_code, _notes = ab_verdict.combine("l3", a_status="PASS", b_status="PASS", diff=diff, registered={})
    assert verdict == "FAIL"


def test_l3_release_gate_fail_is_preserved_as_product_failure(tmp_path: Path):
    a_path = _write_gate(tmp_path / "a" / "release-gate.json", mode="baseline")
    b_path = _write_gate(
        tmp_path / "b" / "release-gate.json",
        mode="candidate",
        release_gate_result="FAIL",
    )
    diff = ab_verdict.compare_l3(a_path, b_path)
    assert any(row["side"] == "b" and "release-gate.json" in row["source_ref"] for row in diff["product_failures"])
    assert ab_verdict.combine("l3", a_status="PASS", b_status="PASS", diff=diff, registered={})[0] == "FAIL"


def test_l3_baseline_engine_fail(tmp_path: Path):
    a_path = _write_gate(tmp_path / "a" / "release-gate.json", mode="baseline", engine_verdict="FAIL")
    b_path = _write_gate(tmp_path / "b" / "release-gate.json", mode="candidate")
    diff = ab_verdict.compare_l3(a_path, b_path)
    verdict, reason_code, _notes = ab_verdict.combine("l3", a_status="PASS", b_status="PASS", diff=diff, registered={})
    assert (verdict, reason_code) == ("BASELINE_INVALID", "HARNESS_BASELINE_INVALID")
    assert diff["product_failures"] == []


def test_l3_revision_mismatch_error(tmp_path: Path):
    a_path = _write_gate(tmp_path / "a" / "release-gate.json", mode="baseline", revision="2026-11-21.1")
    b_path = _write_gate(tmp_path / "b" / "release-gate.json", mode="candidate", revision="2026-11-22.1")
    with pytest.raises(HarnessError) as exc_info:
        ab_verdict.compare_l3(a_path, b_path)
    assert exc_info.value.code == "HARNESS_COMPARATOR_ERROR"


@pytest.mark.parametrize(
    "a_overrides,b_overrides",
    [
        pytest.param({"mode": "candidate"}, {}, id="a-mode"),
        pytest.param({}, {"mode": "baseline"}, id="b-mode"),
        pytest.param({"platform": "linux"}, {"platform": "windows"}, id="platform"),
    ],
)
def test_l3_invalid_gate_mode_or_platform_is_comparator_error(tmp_path: Path, a_overrides, b_overrides):
    a_args = {"mode": "baseline", **a_overrides}
    b_args = {"mode": "candidate", **b_overrides}
    a_path = _write_gate(tmp_path / "a" / "release-gate.json", **a_args)
    b_path = _write_gate(tmp_path / "b" / "release-gate.json", **b_args)
    with pytest.raises(HarnessError) as exc_info:
        ab_verdict.compare_l3(a_path, b_path)
    assert exc_info.value.code == "HARNESS_COMPARATOR_ERROR"


def test_l3_preserves_safety_failures_from_both_sides(tmp_path: Path):
    a_path = _write_gate(
        tmp_path / "a" / "release-gate.json",
        mode="baseline",
        safety_results=(("safety_unauthorized_tx", "FAIL"),),
    )
    b_path = _write_gate(
        tmp_path / "b" / "release-gate.json",
        mode="candidate",
        safety_results=(("safety_cross_context_replay", "FAIL"),),
    )
    diff = ab_verdict.compare_l3(a_path, b_path)
    assert diff["safety_violations"] == ["A:safety_unauthorized_tx", "B:safety_cross_context_replay"]
    assert ab_verdict.combine("l3", a_status="INFRA_ERROR", b_status="PASS", diff=diff, registered={})[0] == "FAIL"


def test_l3_evidence_integrity_rejects_insufficient_evidence(tmp_path: Path):
    a_path = _write_gate(tmp_path / "a" / "release-gate.json", mode="baseline")
    b_path = _write_gate(
        tmp_path / "b" / "release-gate.json",
        mode="candidate",
        evidence_results=("PASS", "INSUFFICIENT_EVIDENCE"),
    )
    diff = ab_verdict.compare_l3(a_path, b_path)
    assert diff["integrity"] == {"a": True, "b": False}


def test_l3_requires_one_baseline_compatible_check(tmp_path: Path):
    a_path = _write_gate(tmp_path / "a" / "release-gate.json", mode="baseline")
    b_path = _write_gate(tmp_path / "b" / "release-gate.json", mode="candidate")
    gate = json.loads(b_path.read_bytes().decode("utf-8"))
    gate["checks"].append(copy.deepcopy(gate["checks"][-1]))
    b_path.write_bytes((json.dumps(gate, sort_keys=True) + "\n").encode("utf-8"))
    with pytest.raises(HarnessError) as exc_info:
        ab_verdict.compare_l3(a_path, b_path)
    assert exc_info.value.code == "HARNESS_COMPARATOR_ERROR"


def test_l3_missing_engine_verdict_is_comparator_error(tmp_path: Path):
    a_path = _write_gate(tmp_path / "a" / "release-gate.json", mode="baseline")
    b_path = _write_gate(tmp_path / "b" / "release-gate.json", mode="candidate")
    gate = json.loads(b_path.read_bytes().decode("utf-8"))
    del gate["engine_result"]["verdict"]
    b_path.write_bytes((json.dumps(gate, sort_keys=True) + "\n").encode("utf-8"))
    with pytest.raises(HarnessError) as exc_info:
        ab_verdict.compare_l3(a_path, b_path)
    assert exc_info.value.code == "HARNESS_COMPARATOR_ERROR"


def test_l3_baseline_compatible_check_is_read_without_recalculation(tmp_path: Path):
    a_path = _write_gate(tmp_path / "a" / "release-gate.json", mode="baseline")
    b_path = _write_gate(
        tmp_path / "b" / "release-gate.json",
        mode="candidate",
        baseline_compatible="INSUFFICIENT_EVIDENCE",
    )
    diff = ab_verdict.compare_l3(a_path, b_path)
    assert diff["b_baseline_compatible"] is False
    assert ab_verdict.combine("l3", a_status="PASS", b_status="PASS", diff=diff, registered={})[:2] == (
        "INFRA_ERROR",
        "HARNESS_SUITE_INFRA_ERROR",
    )


def test_l3_nonlongrun_cases_all_pass_without_release_gate(tmp_path: Path, monkeypatch):
    missing_gate = tmp_path / "release-gate.json"
    assert not missing_gate.exists()

    def unexpected_longrun_read(*_args, **_kwargs):
        pytest.fail("非長跑 L3 不得讀取 release-gate.json")

    monkeypatch.setattr(ab_verdict, "compare_l3", unexpected_longrun_read)
    required = {"remote-case", "native-case", "required-case"}
    a_report = _l2_report(*(_l2_result(case_id) for case_id in sorted(required)))
    b_report = _l2_report(*(_l2_result(case_id) for case_id in sorted(required)))
    diff = _cases_diff(_compare_l2(a_report, b_report, required))
    assert ab_verdict.combine("l3", a_status="PASS", b_status="PASS", diff=diff, registered={})[:2] == ("PASS", None)
    assert not missing_gate.exists()


@pytest.mark.parametrize("missing_case", ["remote-case", "native-case"])
def test_l3_nonlongrun_missing_remote_or_native_is_incomplete(missing_case: str):
    required = {"remote-case", "native-case", "required-case"}
    a_report = _l2_report(*(_l2_result(case_id) for case_id in sorted(required)))
    b_report = _l2_report(*(_l2_result(case_id) for case_id in sorted(required - {missing_case})))
    diff = _cases_diff(_compare_l2(a_report, b_report, required))
    assert diff["verdict"] == "INCOMPLETE" and diff["missing"]["b"] == [missing_case]
    assert ab_verdict.combine("l3", a_status="PASS", b_status="PASS", diff=diff, registered={})[:2] == (
        "INFRA_ERROR",
        "HARNESS_SUITE_INFRA_ERROR",
    )


def test_l3_nonlongrun_required_skip_is_incomplete():
    required = {"remote-case", "native-case", "required-case"}
    a_report = _l2_report(*(_l2_result(case_id) for case_id in sorted(required)))
    b_report = _l2_report(
        _l2_result("remote-case"),
        _l2_result("native-case"),
        _l2_result("required-case", verdict="SKIP"),
    )
    diff = _cases_diff(_compare_l2(a_report, b_report, required))
    assert diff["verdict"] == "INCOMPLETE" and diff["required_skips"]["b"] == ["required-case"]
    assert ab_verdict.combine("l3", a_status="PASS", b_status="PASS", diff=diff, registered={})[0] == "INFRA_ERROR"


def test_l3_nonlongrun_unregistered_difference_fails():
    required = {"required-case"}
    a_report = _l2_report(_l2_result("required-case"), _l2_result("unregistered-change", verdict="FAIL"))
    b_report = _l2_report(_l2_result("required-case"), _l2_result("unregistered-change", verdict="PASS"))
    diff = _cases_diff(_compare_l2(a_report, b_report, required))
    assert diff["verdict"] == "FAIL"
    assert diff["needs_review"] == [{"case": "unregistered-change", "a": "FAIL", "b": "PASS"}]
    assert ab_verdict.combine("l3", a_status="PASS", b_status="PASS", diff=diff, registered={})[0] == "FAIL"


def test_load_registered_changes_maps_scope(tmp_path: Path):
    changes_path = tmp_path / "contracts" / "behavior_changes.yaml"
    changes_path.parent.mkdir(parents=True)
    changes_path.write_bytes(
        (
            'schema_version: 1\nrevision: 4\nchanges:\n'
            '  - id: "BC-01"\n    scope: "both"\n    ab_participation: "diff"\n'
            '    case_ids: ["bc01-case"]\n'
            '  - id: "SC-15"\n    scope: "rust_only"\n    ab_participation: "absolute_only"\n'
            '    case_ids: ["sc15-case"]\n'
        ).encode("utf-8")
    )
    assert ab_verdict.load_registered_changes(changes_path) == {"BC-01": "diff", "SC-15": "absolute_only"}
    assert ab_verdict.case_changes(changes_path) == {"bc01-case": "BC-01", "sc15-case": "SC-15"}


def test_case_changes_includes_rust_only_bc06_from_repository():
    mapping = ab_verdict.case_changes(BEHAVIOR_CHANGES_PATH)
    assert mapping["bc06-shutdown-self-test-flush"] == "BC-06"


def test_t7b_rust_only_difference_is_skipped_only_when_absolute_passes(tmp_path: Path):
    case_id = "bc06-shutdown-self-test-flush"
    registered = ab_verdict.load_registered_changes(BEHAVIOR_CHANGES_PATH)
    case_changes = ab_verdict.case_changes(BEHAVIOR_CHANGES_PATH)
    calls_a = [{"argv": ["daemon", "status"], "rc": 0, "response": {"ok": True, "state": "python"}}]
    calls_b = [{"argv": ["daemon", "status"], "rc": 0, "response": {"ok": True, "state": "rust"}}]

    a_dir = tmp_path / "pass" / "a"
    b_dir = tmp_path / "pass" / "b"
    fakes_ab_diff.write_run(a_dir, case_id, side="a", calls=calls_a)
    fakes_ab_diff.write_run(b_dir, case_id, side="b", calls=calls_b)
    passing_diff = diff_l15(
        a_dir,
        b_dir,
        registered=registered,
        case_changes=case_changes,
        required_case_ids={case_id},
        rules_path=RULES_PATH,
    )
    difference = next(item for item in passing_diff["differences"] if item["case"] == case_id)
    assert difference["registered_change"] == "BC-06"
    assert ab_verdict.combine(
        "l1.5", a_status="PASS", b_status="PASS", diff=passing_diff, registered=registered
    )[0] == "PASS"

    failed_a = tmp_path / "fail" / "a"
    failed_b = tmp_path / "fail" / "b"
    fakes_ab_diff.write_run(failed_a, case_id, side="a", calls=calls_a)
    fakes_ab_diff.write_run(failed_b, case_id, side="b", calls=calls_b, verdict="FAIL")
    failing_diff = diff_l15(
        failed_a,
        failed_b,
        registered=registered,
        case_changes=case_changes,
        required_case_ids={case_id},
        rules_path=RULES_PATH,
    )
    assert any(item["registered_change"] == "BC-06" for item in failing_diff["differences"])
    assert any(item["case"] == case_id and item["side"] == "b" for item in failing_diff["product_failures"])
    assert ab_verdict.combine(
        "l1.5", a_status="PASS", b_status="FAIL", diff=failing_diff, registered=registered
    )[0] == "FAIL"


def test_t7b_baseline_failure_stays_out_of_product_failures(tmp_path: Path):
    case_id = "baseline-only-fail"
    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    calls = [{"argv": ["daemon", "status"], "rc": 0, "response": {"ok": True, "state": "same"}}]
    fakes_ab_diff.write_run(
        a_dir, case_id, side="a", verdict="FAIL", reason="baseline observation failed", calls=calls,
    )
    fakes_ab_diff.write_run(b_dir, case_id, side="b", calls=calls)
    diff = diff_l15(
        a_dir,
        b_dir,
        registered={},
        case_changes={},
        required_case_ids={case_id},
        rules_path=RULES_PATH,
    )
    assert diff["baseline_failures"] == [
        {
            "side": "a",
            "case": case_id,
            "source_ref": f"{case_id}/bridge/1/comparison.json",
            "reason": "baseline observation failed",
        }
    ]
    assert diff["product_failures"] == []
    assert [(row["kind"], row["path"]) for row in diff["differences"]] == [
        ("verdict", "comparison.verdict"),
    ]
    assert ab_verdict.combine("l1.5", a_status="FAIL", b_status="PASS", diff=diff, registered={})[:2] == (
        "BASELINE_INVALID",
        "HARNESS_BASELINE_INVALID",
    )


def test_t7b_identical_reports_combine_pass_and_keep_case_maps(tmp_path: Path):
    case_id = "identical-l15-case"
    a_dir, b_dir, diff = _write_l15_pair(
        tmp_path, case_id=case_id, required={case_id}, runner="contract-blackbox"
    )
    assert diff["ok"] is True and diff["differences"] == []
    assert {item["side"] for item in diff["case_maps"]} == {"a", "b"}
    assert all(item["path"] == "tests/acceptance/cases.yaml" for item in diff["case_maps"])
    assert ab_verdict.combine("l1.5", a_status="PASS", b_status="PASS", diff=diff, registered={})[:2] == (
        "PASS",
        None,
    )
    assert a_dir != b_dir


def test_t7b_confirmed_fail_and_missing_required_case_still_fail(tmp_path: Path):
    failed_case = "confirmed-fail"
    missing_case = "missing-required"
    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    fakes_ab_diff.write_run(a_dir, failed_case, side="a")
    fakes_ab_diff.write_run(b_dir, failed_case, side="b", verdict="FAIL")
    fakes_ab_diff.write_run(a_dir, missing_case, side="a")
    diff = diff_l15(
        a_dir,
        b_dir,
        registered={},
        case_changes={},
        required_case_ids={failed_case, missing_case},
        rules_path=RULES_PATH,
    )
    failure = next(item for item in diff["product_failures"] if item["case"] == failed_case)
    assert failure["side"] == "b" and _source_ref_exists(b_dir, failure["source_ref"])
    assert diff["integrity"]["b"] is False
    assert any("missing required case: missing-required" in issue for issue in diff["integrity_problems"]["b"])
    verdict, _reason_code, notes = ab_verdict.combine(
        "l1.5", a_status="PASS", b_status="INFRA_ERROR", diff=diff, registered={}
    )
    assert verdict == "FAIL" and "missing required case: missing-required" in " ".join(notes)


def test_t7b_only_unverified_failure_is_infra_not_product_fail(tmp_path: Path):
    case_id = "unverified-fail"
    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    fakes_ab_diff.write_run(a_dir, case_id, side="a")
    fakes_ab_diff.write_run(b_dir, case_id, side="b", verdict="FAIL", identity_missing=["cli.sha256"])
    diff = diff_l15(
        a_dir,
        b_dir,
        registered={},
        case_changes={},
        required_case_ids={case_id},
        rules_path=RULES_PATH,
    )
    assert diff["unverified_failures"]
    assert diff["product_failures"] == []
    assert diff["integrity"]["b"] is False
    verdict, reason_code, notes = ab_verdict.combine(
        "l1.5", a_status="PASS", b_status="PASS", diff=diff, registered={}
    )
    assert (verdict, reason_code) == ("INFRA_ERROR", "HARNESS_COMPARATOR_ERROR")
    assert case_id in " ".join(notes)


@pytest.mark.parametrize(
    "a_calls,b_calls,expected_kind,expected_path",
    [
        pytest.param(
            [{"argv": ["daemon", "status"], "rc": 0, "response": {"ok": True}}],
            [{"argv": ["session", "list"], "rc": 0, "response": {"ok": True}}],
            "argv",
            "call[0].argv",
            id="argv",
        ),
        pytest.param(
            [{"argv": ["daemon", "status"], "rc": 0, "response": {"ok": True}}],
            [
                {"argv": ["daemon", "status"], "rc": 0, "response": {"ok": True}},
                {"argv": ["session", "list"], "rc": 0, "response": {"ok": True}},
            ],
            "call_count",
            "call[1]",
            id="call-count",
        ),
    ],
)
def test_t7b_argv_and_call_count_differences_fail_when_unregistered(
    tmp_path: Path,
    a_calls: list[dict[str, Any]],
    b_calls: list[dict[str, Any]],
    expected_kind: str,
    expected_path: str,
):
    case_id = "new-difference-kind"
    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    fakes_ab_diff.write_run(a_dir, case_id, side="a", runner="contract-blackbox", calls=a_calls)
    fakes_ab_diff.write_run(b_dir, case_id, side="b", runner="contract-blackbox", calls=b_calls)
    diff = diff_l15(
        a_dir,
        b_dir,
        registered={},
        case_changes={},
        required_case_ids={case_id},
        rules_path=RULES_PATH,
    )
    difference = next(item for item in diff["differences"] if item["kind"] == expected_kind)
    assert difference["path"] == expected_path
    if expected_kind == "argv":
        assert _source_ref_exists(a_dir, difference["source_refs"]["a"])
        assert _source_ref_exists(b_dir, difference["source_refs"]["b"])
    else:
        assert difference["source_refs"]["a"] is None
        assert difference["source_refs"]["b"] is not None
    assert {item["side"] for item in diff["case_maps"]} == {"a", "b"}
    verdict, _reason_code, notes = ab_verdict.combine(
        "l1.5", a_status="PASS", b_status="PASS", diff=diff, registered={}
    )
    assert verdict == "FAIL"
    assert notes[0] == f"unregistered difference: {case_id} {expected_path}"
    assert len(notes) == 2 and difference["source_refs"]["b"] in notes[1]


def _write_l15_unregistered_difference(tmp_path: Path, *, bad_observation_hash: bool = False):
    case_id = "r1-01-unregistered"
    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    calls_a = [{"argv": ["daemon", "status"], "rc": 0, "response": {"ok": True, "state": "a"}}]
    calls_b = [{"argv": ["daemon", "status"], "rc": 0, "response": {"ok": True, "state": "b"}}]
    fakes_ab_diff.write_run(a_dir, case_id, side="a", calls=calls_a)
    fakes_ab_diff.write_run(b_dir, case_id, side="b", calls=calls_b)
    if bad_observation_hash:
        manifest_path = b_dir / case_id / "bridge" / "1" / "manifest.json"
        manifest = fakes_ab_diff.read_json(manifest_path)
        manifest["evidence"]["cli_log"]["sha256"] = "0" * 64
        fakes_ab_diff.write_json(manifest_path, manifest)
    diff = diff_l15(
        a_dir,
        b_dir,
        registered={},
        case_changes={},
        required_case_ids={case_id, "another-required"},
        rules_path=RULES_PATH,
    )
    return case_id, a_dir, b_dir, diff


def test_r1_01_verified_difference_wins_over_missing_required_case(tmp_path: Path):
    case_id, _a_dir, _b_dir, diff = _write_l15_unregistered_difference(tmp_path)
    difference = next(row for row in diff["differences"] if row["case"] == case_id)
    assert difference["verified"] is True
    assert diff["integrity"] == {"a": False, "b": False}

    verdict, reason_code, notes = ab_verdict.combine(
        "l1.5", a_status="PASS", b_status="PASS", diff=diff, registered={}
    )

    assert (verdict, reason_code) == ("FAIL", None)
    assert any("unregistered difference: r1-01-unregistered" in note for note in notes)
    assert difference["source_refs"]["b"] in " ".join(notes)
    assert "another-required" in " ".join(notes)


def test_r1_01_verified_difference_wins_over_suite_infra(tmp_path: Path):
    case_id, _a_dir, _b_dir, diff = _write_l15_unregistered_difference(tmp_path)
    difference = next(row for row in diff["differences"] if row["case"] == case_id)
    assert difference["verified"] is True

    verdict, reason_code, notes = ab_verdict.combine(
        "l1.5", a_status="PASS", b_status="INFRA_ERROR", diff=diff, registered={}
    )

    assert (verdict, reason_code) == ("FAIL", None)
    assert difference["source_refs"]["b"] in " ".join(notes)
    assert "B suite status: INFRA_ERROR" in notes
    assert "suite reported INFRA_ERROR" in notes


def test_r1_01_unverified_difference_is_infra_and_kept_in_notes(tmp_path: Path):
    case_id, _a_dir, _b_dir, diff = _write_l15_unregistered_difference(tmp_path, bad_observation_hash=True)
    difference = next(row for row in diff["differences"] if row["case"] == case_id)
    assert difference["verified"] is False

    verdict, reason_code, notes = ab_verdict.combine(
        "l1.5", a_status="PASS", b_status="PASS", diff=diff, registered={}
    )

    assert (verdict, reason_code) == ("INFRA_ERROR", "HARNESS_COMPARATOR_ERROR")
    assert case_id in " ".join(notes) and difference["source_refs"]["b"] in " ".join(notes)


def test_r1_01_unverified_difference_in_baseline_failure_case_is_infra():
    case_id = "baseline-unverified-difference"
    diff = _empty_diff(
        differences=[{
            "case": case_id,
            "path": "call[0].state",
            "kind": "value",
            "verified": False,
            "source_refs": {"a": "a/call.json#L1", "b": "b/call.json#L1"},
        }],
        baseline_failures=[_failure(side="a", case=case_id)],
        a_cases={case_id: "FAIL"},
        b_cases={case_id: "PASS"},
    )

    verdict, reason_code, notes = ab_verdict.combine(
        "l1.5", a_status="PASS", b_status="PASS", diff=diff, registered={}
    )

    assert (verdict, reason_code) == ("INFRA_ERROR", "HARNESS_COMPARATOR_ERROR")
    assert case_id in " ".join(notes)


def test_r1_01_verified_difference_in_baseline_failure_case_is_product_fail():
    case_id = "baseline-verified-difference"
    source_ref = "b/call.json#L1"
    diff = _empty_diff(
        differences=[{
            "case": case_id,
            "path": "call[0].state",
            "kind": "value",
            "verified": True,
            "source_refs": {"a": "a/call.json#L1", "b": source_ref},
        }],
        baseline_failures=[_failure(side="a", case=case_id)],
        a_cases={case_id: "FAIL"},
        b_cases={case_id: "PASS"},
    )

    verdict, reason_code, notes = ab_verdict.combine(
        "l1.5", a_status="FAIL", b_status="PASS", diff=diff, registered={}
    )

    assert (verdict, reason_code) == ("FAIL", None)
    assert any(source_ref in note for note in notes)
    assert any("baseline failure A" in note for note in notes)


@pytest.mark.parametrize("layer", ["l2", "l3-cases"])
def test_r1_02_suite_infra_without_case_failure_is_infra(layer: str):
    diff = _empty_diff(kind="cases", verdict="PASS", integrity={"a": True, "b": True})

    verdict, reason_code, notes = ab_verdict.combine(
        "l2" if layer == "l2" else "l3",
        a_status="INFRA_ERROR",
        b_status="PASS",
        diff=diff,
        registered={},
    )

    assert (verdict, reason_code) == ("INFRA_ERROR", "HARNESS_SUITE_INFRA_ERROR")
    assert "A suite status: INFRA_ERROR" in notes


@pytest.mark.parametrize("layer", ["l2", "l3-cases"])
def test_r1_02_confirmed_failure_precedes_suite_infra(layer: str):
    source_ref = "testpilot/report.json#/results/1"
    diff = _empty_diff(
        kind="cases",
        verdict="FAIL",
        integrity={"a": True, "b": True},
        product_failures=[{**_failure(), "source_ref": source_ref}],
    )

    verdict, reason_code, notes = ab_verdict.combine(
        "l2" if layer == "l2" else "l3",
        a_status="PASS",
        b_status="INFRA_ERROR",
        diff=diff,
        registered={},
    )

    assert (verdict, reason_code) == ("FAIL", None)
    assert source_ref in " ".join(notes)
    assert "B suite status: INFRA_ERROR" in notes


def test_r1_03_l2_does_not_infer_safety_from_cases_yaml_invariants():
    result = _compare_l2(
        _l2_report(_l2_result("ordinary-failure")),
        _l2_report(_l2_result("ordinary-failure", verdict="FAIL", reason="functional assertion")),
        {"ordinary-failure"},
        cases={"ordinary-failure": {"invariants": ["zero_tx_on_reject"]}},
    )

    assert result["safety_violations"] == []
    assert result["product_failures"][0]["side"] == "b"


def test_r1_03_l3_safety_failure_keeps_side_and_source(tmp_path: Path):
    a_path = _write_gate(
        tmp_path / "a" / "release-gate.json",
        mode="baseline",
        safety_results=(("safety_unauthorized_tx", "FAIL"),),
    )
    b_path = _write_gate(tmp_path / "b" / "release-gate.json", mode="candidate")

    diff = ab_verdict.compare_l3(a_path, b_path)

    assert diff["safety_violations"] == ["A:safety_unauthorized_tx"]
    assert diff["safety_failure_sources"] == [
        {
            "side": "a",
            "case": "safety_unauthorized_tx",
            "source_ref": "release-gate.json#/checks/3",
            "reason": "safety check FAIL",
        }
    ]
    assert ab_verdict.combine("l3", a_status="PASS", b_status="PASS", diff=diff, registered={})[0] == "FAIL"


@pytest.mark.parametrize(
    "a_mode,a_verdict,b_verdict",
    [
        pytest.param("missing", "PASS", "PASS", id="a-missing"),
        pytest.param("unknown", "PASS", "PASS", id="a-unknown"),
        pytest.param("skip", "PASS", "XFAIL", id="a-skip-b-xfail"),
        pytest.param("error", "PASS", "PASS", id="a-error"),
    ],
)
def test_r1_04_xfail_needs_two_valid_observations(a_mode: str, a_verdict: str, b_verdict: str):
    case_id = "xfail-needs-two-observations"
    a_results = [] if a_mode == "missing" else [_l2_result(case_id, verdict={"unknown": "WEIRD", "skip": "SKIP", "error": "ERROR"}.get(a_mode, a_verdict))]
    b_reason = "known signature" if b_verdict == "XFAIL" else ""
    cases = {
        case_id: {
            "expected": "XFAIL",
            "invariants": [],
            "sentinel": {"issue": "#159", "signature": "known signature", "expires": "2027-01-01T00:00:00+00:00"},
        }
    }

    result = _compare_l2(
        _l2_report(*a_results),
        _l2_report(_l2_result(case_id, verdict=b_verdict, reason=b_reason)),
        {case_id},
        cases=cases,
    )

    assert result["verdict"] == "INCOMPLETE"
    assert result["needs_review"] == []
    assert result["product_failures"] == []


def test_r1_04_xfail_difference_with_two_valid_observations_needs_review():
    case_id = "xfail-valid-difference"
    cases = {case_id: {"expected": "XFAIL", "invariants": [], "sentinel": None}}

    result = _compare_l2(
        _l2_report(_l2_result(case_id, verdict="XFAIL")),
        _l2_report(_l2_result(case_id, verdict="PASS")),
        {case_id},
        cases=cases,
    )

    assert result["verdict"] == "FAIL"
    assert result["needs_review"] == [{"case": case_id, "a": "XFAIL", "b": "PASS"}]
    assert result["product_failures"][0]["source_ref"] == "testpilot/report.json#/results/0"


@pytest.mark.parametrize(
    "problem",
    [
        "missing-schema-version",
        "wrong-schema-version",
        "missing-top-level-field",
        "missing-check-group",
        "null-check-id",
        "missing-check-result",
        "null-check-result",
        "unknown-check-result",
    ],
)
def test_r1_05_invalid_p2_gate_shape_is_integrity_error(tmp_path: Path, problem: str):
    a_path = _write_gate(tmp_path / "a" / "release-gate.json", mode="baseline")
    b_path = _write_gate(tmp_path / "b" / "release-gate.json", mode="candidate")
    gate = json.loads(b_path.read_bytes().decode("utf-8"))
    if problem == "missing-schema-version":
        del gate["schema_version"]
    elif problem == "wrong-schema-version":
        gate["schema_version"] = "release-gate.v0"
    elif problem == "missing-top-level-field":
        del gate["run"]
    elif problem == "missing-check-group":
        del gate["checks"][0]["group"]
    elif problem == "null-check-id":
        gate["checks"][0]["id"] = None
    elif problem == "missing-check-result":
        del gate["checks"][0]["result"]
    elif problem == "null-check-result":
        gate["checks"][0]["result"] = None
    else:
        gate["checks"][0]["result"] = "UNRECOGNIZED"
    b_path.write_bytes((json.dumps(gate, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"))

    diff = ab_verdict.compare_l3(a_path, b_path)

    assert diff["integrity"]["b"] is False
    assert diff["integrity_problems"]["b"]
    assert ab_verdict.combine("l3", a_status="PASS", b_status="PASS", diff=diff, registered={})[:2] == (
        "INFRA_ERROR",
        "HARNESS_COMPARATOR_ERROR",
    )


def test_r1_05_valid_candidate_failure_survives_other_side_gate_integrity_error(tmp_path: Path):
    a_path = _write_gate(tmp_path / "a" / "release-gate.json", mode="baseline")
    gate_a = json.loads(a_path.read_bytes().decode("utf-8"))
    gate_a["checks"][0]["result"] = None
    a_path.write_bytes((json.dumps(gate_a, sort_keys=True) + "\n").encode("utf-8"))
    b_path = _write_gate(
        tmp_path / "b" / "release-gate.json",
        mode="candidate",
        engine_verdict="FAIL",
    )

    diff = ab_verdict.compare_l3(a_path, b_path)

    assert diff["integrity"]["a"] is False
    assert any(row["side"] == "b" for row in diff["product_failures"])
    assert ab_verdict.combine("l3", a_status="PASS", b_status="PASS", diff=diff, registered={})[0] == "FAIL"


@pytest.mark.parametrize("b_mode", ["baseline", "missing"])
def test_r1_06_invalid_candidate_mode_does_not_confirm_b_engine_failure(tmp_path: Path, b_mode: str):
    a_path = _write_gate(tmp_path / "a" / "release-gate.json", mode="baseline")
    b_path = _write_gate(
        tmp_path / "b" / "release-gate.json",
        mode="baseline" if b_mode == "baseline" else "candidate",
        engine_verdict="FAIL",
    )
    if b_mode == "missing":
        gate = json.loads(b_path.read_bytes().decode("utf-8"))
        del gate["mode"]
        b_path.write_bytes((json.dumps(gate, sort_keys=True) + "\n").encode("utf-8"))

    with pytest.raises(HarnessError) as exc_info:
        ab_verdict.compare_l3(a_path, b_path)

    assert exc_info.value.code == "HARNESS_COMPARATOR_ERROR"


def test_r1_06_other_side_safety_failure_survives_invalid_candidate_mode(tmp_path: Path):
    a_path = _write_gate(
        tmp_path / "a" / "release-gate.json",
        mode="baseline",
        safety_results=(("safety_unauthorized_tx", "FAIL"),),
    )
    b_path = _write_gate(
        tmp_path / "b" / "release-gate.json",
        mode="candidate",
        engine_verdict="FAIL",
    )
    gate_b = json.loads(b_path.read_bytes().decode("utf-8"))
    del gate_b["mode"]
    b_path.write_bytes((json.dumps(gate_b, sort_keys=True) + "\n").encode("utf-8"))

    diff = ab_verdict.compare_l3(a_path, b_path)

    assert diff["product_failures"] == []
    assert diff["safety_violations"] == ["A:safety_unauthorized_tx"]
    assert ab_verdict.combine("l3", a_status="PASS", b_status="PASS", diff=diff, registered={})[0] == "FAIL"


@pytest.mark.parametrize(
    "mismatch,update",
    [
        pytest.param("revision", {"revision": "2026-11-22.1"}, id="revision"),
        pytest.param("platform", {"platform": "windows"}, id="platform"),
    ],
)
def test_r1_06_verified_candidate_failure_survives_comparison_mismatch(
    tmp_path: Path, mismatch: str, update: dict[str, Any],
):
    a_path = _write_gate(tmp_path / "a" / "release-gate.json", mode="baseline")
    b_path = _write_gate(
        tmp_path / "b" / "release-gate.json",
        mode="candidate",
        engine_verdict="FAIL",
        **update,
    )

    diff = ab_verdict.compare_l3(a_path, b_path)
    verdict, reason_code, notes = ab_verdict.combine(
        "l3", a_status="PASS", b_status="PASS", diff=diff, registered={}
    )

    assert (verdict, reason_code) == ("FAIL", None)
    assert any(row["side"] == "b" for row in diff["product_failures"])
    assert mismatch in " ".join(notes)


@pytest.mark.parametrize(
    "report_folder,report_ref,layer",
    [
        pytest.param("testpilot", "testpilot/report.json", "l2", id="l2-testpilot"),
        pytest.param("realhw", "realhw/report.json", "l3-cases", id="l3-realhw"),
    ],
)
def test_r1_07_result_source_ref_reads_failed_row_from_report_dir(
    tmp_path: Path, report_folder: str, report_ref: str, layer: str,
):
    a_dir = tmp_path / "baseline"
    b_dir = tmp_path / "candidate"
    for report_dir, verdict in ((a_dir, "PASS"), (b_dir, "FAIL")):
        report_path = report_dir / report_folder / "report.json"
        report_path.parent.mkdir(parents=True)
        payload = _l2_report(
            _l2_result(
                "failed-case",
                verdict=verdict,
                reason="functional failure" if verdict == "FAIL" else "",
                evidence=[{"source_ref": "evidence/trace.json#/events/0"}] if verdict == "FAIL" else None,
            )
        )
        report_path.write_bytes((json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"))
    a_report = json.loads((a_dir / report_ref).read_bytes().decode("utf-8"))
    b_report = json.loads((b_dir / report_ref).read_bytes().decode("utf-8"))

    diff = ab_verdict.compare_l2_reports(
        a_report,
        b_report,
        required_case_ids={"failed-case"},
        cases={},
        registered_case_changes={},
        now=_NOW,
        a_report_ref=report_ref,
        b_report_ref=report_ref,
    )
    if layer == "l3-cases":
        diff = _cases_diff(diff)
    failure = diff["product_failures"][0]
    relative, fragment = failure["source_ref"].split("#", 1)
    source_report = json.loads((b_dir / relative).read_bytes().decode("utf-8"))
    result_index = int(fragment.rsplit("/", 1)[1])

    assert failure["source_ref"] == f"{report_ref}#/results/0"
    assert source_report["results"][result_index]["id"] == "failed-case"
    assert source_report["results"][result_index]["verdict"] == "FAIL"
    assert failure["evidence_refs"] == ["evidence/trace.json#/events/0"]


def test_r2_verified_cli_difference_survives_unverified_invariant_and_is_fail(tmp_path: Path):
    case_id = "r2-cli-diff-with-unverified-invariant"
    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    calls_a = [{"argv": ["daemon", "status"], "rc": 0, "response": {"ok": True, "state": "a"}}]
    calls_b = [{"argv": ["daemon", "status"], "rc": 0, "response": {"ok": True, "state": "b"}}]
    fakes_ab_diff.write_run(a_dir, case_id, side="a", calls=calls_a)
    fakes_ab_diff.write_run(
        b_dir, case_id, side="b", calls=calls_b,
        invariants=[{"id": None, "result": "FAIL"}],
    )
    diff = diff_l15(
        a_dir, b_dir, registered={}, case_changes={}, required_case_ids={case_id}, rules_path=RULES_PATH,
    )

    difference = next(row for row in diff["differences"] if row["kind"] == "value")
    assert difference["verified"] is True
    assert diff["unverified_failures"]
    verdict, reason_code, notes = ab_verdict.combine(
        "l1.5", a_status="PASS", b_status="PASS", diff=diff, registered={},
    )

    assert (verdict, reason_code) == ("FAIL", None)
    assert difference["source_refs"]["b"] in " ".join(notes)
    assert "unverified failure" in " ".join(notes)


def test_r2_verified_unregistered_difference_has_priority_over_baseline_failure(tmp_path: Path):
    case_id = "r2-product-diff-and-baseline-failure"
    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    calls_a = [{"argv": ["daemon", "status"], "rc": 0, "response": {"ok": True, "state": "a"}}]
    calls_b = [{"argv": ["daemon", "status"], "rc": 0, "response": {"ok": True, "state": "b"}}]
    fakes_ab_diff.write_run(a_dir, case_id, side="a", verdict="FAIL", calls=calls_a)
    fakes_ab_diff.write_run(b_dir, case_id, side="b", calls=calls_b)
    diff = diff_l15(
        a_dir, b_dir, registered={}, case_changes={}, required_case_ids={case_id}, rules_path=RULES_PATH,
    )

    difference = next(row for row in diff["differences"] if row["kind"] == "value")
    assert difference["verified"] is True
    assert diff["baseline_failures"]
    verdict, reason_code, notes = ab_verdict.combine(
        "l1.5", a_status="PASS", b_status="PASS", diff=diff, registered={},
    )

    assert (verdict, reason_code) == ("FAIL", None)
    assert difference["source_refs"]["b"] in " ".join(notes)


@pytest.mark.parametrize("kind", ["value", "argv", "call_count"])
def test_r2_collect_product_failures_references_actual_side_report(
    tmp_path: Path, kind: str,
):
    case_id = f"r2-source-{kind}"
    a_dir = tmp_path / kind / "a"
    b_dir = tmp_path / kind / "b"
    base_call = {"argv": ["daemon", "status"], "rc": 0, "response": {"ok": True, "state": "same"}}
    if kind == "value":
        calls_a = [base_call]
        calls_b = [{**base_call, "response": {"ok": True, "state": "changed"}}]
    elif kind == "argv":
        calls_a = [base_call]
        calls_b = [{**base_call, "argv": ["session", "list"]}]
    else:
        calls_a = [base_call, {"argv": ["session", "list"], "rc": 0, "response": {"ok": True}}]
        calls_b = [base_call]
    fakes_ab_diff.write_run(a_dir, case_id, side="a", calls=calls_a)
    fakes_ab_diff.write_run(b_dir, case_id, side="b", calls=calls_b)
    diff = diff_l15(
        a_dir, b_dir, registered={}, case_changes={}, required_case_ids={case_id}, rules_path=RULES_PATH,
    )

    failures = ab_verdict.collect_product_failures("l1.5", diff=diff, registered={})
    assert len(failures) == 1
    failure = failures[0]
    expected_side = "a" if kind == "call_count" else "b"
    assert set(failure) == {"side", "case", "source_ref", "reason", "source_refs", "kind", "path"}
    assert failure["side"] == expected_side
    assert failure["kind"] == kind
    assert failure["source_refs"] == next(row for row in diff["differences"] if row["kind"] == kind)["source_refs"]
    assert failure["path"] == next(row for row in diff["differences"] if row["kind"] == kind)["path"]
    for side, report_dir in (("a", a_dir), ("b", b_dir)):
        source_ref = failure["source_refs"][side]
        if source_ref is None:
            continue
        assert _source_ref_exists(report_dir, source_ref)
        relative, locator = source_ref.split("#", 1)
        line_number = int(locator.removeprefix("L"))
        source_file = report_dir / relative
        source_row = json.loads(source_file.read_bytes().splitlines()[line_number - 1].decode("utf-8"))
        assert isinstance(source_row.get("argv"), list)
    assert _source_ref_exists(a_dir if expected_side == "a" else b_dir, failure["source_ref"])

    verdict, reason_code, notes = ab_verdict.combine(
        "l1.5", a_status="PASS", b_status="PASS", diff=diff, registered={},
    )
    assert (verdict, reason_code) == ("FAIL", None)
    assert failure["source_ref"] in " ".join(notes)
    expected_label = "difference source A" if expected_side == "a" else "difference source B"
    assert expected_label in " ".join(notes)


@pytest.mark.parametrize("evidence_result", ["BLOCKED", "FAIL"])
def test_r2_nonpass_evidence_check_with_pass_summary_breaks_gate_integrity(
    tmp_path: Path, evidence_result: str,
):
    a_path = _write_gate(tmp_path / "a" / "release-gate.json", mode="baseline")
    b_path = _write_gate(
        tmp_path / "b" / "release-gate.json",
        mode="candidate",
        evidence_results=(evidence_result,),
        release_gate_result="PASS",
    )

    diff = ab_verdict.compare_l3(a_path, b_path)
    verdict, reason_code, _notes = ab_verdict.combine(
        "l3", a_status="PASS", b_status="PASS", diff=diff, registered={},
    )

    assert diff["integrity"]["b"] is False
    assert (verdict, reason_code) == ("INFRA_ERROR", "HARNESS_COMPARATOR_ERROR")


def test_r2_consistent_gate_results_and_complete_evidence_are_intact(tmp_path: Path):
    a_path = _write_gate(tmp_path / "a" / "release-gate.json", mode="baseline")
    b_path = _write_gate(tmp_path / "b" / "release-gate.json", mode="candidate")

    diff = ab_verdict.compare_l3(a_path, b_path)

    assert json.loads(a_path.read_bytes().decode("utf-8"))["release_gate_result"] == "NOT_APPLICABLE_BASELINE"
    assert diff["b_release_gate_result"] == "PASS"
    assert diff["integrity"] == {"a": True, "b": True}


def test_r2_gate_integrity_error_preserves_product_and_safety_failures(tmp_path: Path):
    a_path = _write_gate(tmp_path / "a" / "release-gate.json", mode="baseline")
    b_path = _write_gate(
        tmp_path / "b" / "release-gate.json",
        mode="candidate",
        engine_verdict="FAIL",
        evidence_results=("BLOCKED",),
        safety_results=(("safety_unauthorized_tx", "FAIL"),),
        release_gate_result="PASS",
    )

    diff = ab_verdict.compare_l3(a_path, b_path)
    verdict, reason_code, _notes = ab_verdict.combine(
        "l3", a_status="PASS", b_status="PASS", diff=diff, registered={},
    )

    assert diff["integrity"]["b"] is False
    assert any(row["side"] == "b" for row in diff["product_failures"])
    assert diff["safety_violations"] == ["B:safety_unauthorized_tx"]
    assert (verdict, reason_code) == ("FAIL", None)
