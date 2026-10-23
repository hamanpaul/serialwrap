"""P9-T7 A/B 判定的邊界測試（審查補強）。

既有紅燈測試 `tests/test_stage3_ab_verdict.py` 維持原樣（不改寫）；本檔只新增斷言，補齊以下未覆蓋的分支：
- L3 真實形狀的 `release-gate.v1`（`acceptance.revision` 為字串 `"2026-10-15.1"`）與列舉、`baseline_compatible` 結構驗證。
- L2 安全不變量案例不可由哨兵豁免，且 B 側 FAIL 須產生 `safety_violations`（即使 L2 不完整也判 FAIL）。
- 空必要案例集合不得視為完整；`date` 型哨兵到期時間（YAML 讀入的純日期）；B 側 XFAIL／XPASS 判定。
- `case_changes`、`load_registered_changes` 錯誤路徑、L3 模式／平台／JSON 結構錯誤、`combine` 的 diff=None 與未知狀態分支。
"""

from __future__ import annotations

import copy
import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from tools.stage3 import ab_verdict as av
from tools.stage3.reason_codes import HarnessError

pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]

_NOW = "2026-10-21T00:00:00+00:00"
_FUTURE = "2027-01-01T00:00:00+00:00"
_COMPARATOR = "HARNESS_COMPARATOR_ERROR"
_SUITE = "HARNESS_SUITE_INFRA_ERROR"


# ---------------------------------------------------------------------------
# 共用替身
# ---------------------------------------------------------------------------


def _result(case_id: str, verdict: str = "PASS", reason: str = "") -> dict:
    return {
        "id": case_id,
        "verdict": verdict,
        "reason": reason,
        "category": "functional",
        "reason_code": None,
        "duration_s": 0.1,
        "evidence": [],
    }


def _report(*results: dict) -> dict:
    return {"meta": {}, "results": list(results)}


def _sentinel_case(signature: str = "tail mismatch", expires=_FUTURE, invariants=None) -> dict:
    return {
        "expected": "XFAIL",
        "invariants": list(invariants or []),
        "sentinel": {"issue": "#159", "signature": signature, "expires": expires},
    }


def _compare(a_report, b_report, required, *, cases=None, registered=None, now=_NOW) -> dict:
    return av.compare_l2_reports(
        a_report,
        b_report,
        required_case_ids=set(required),
        cases=cases or {},
        registered_case_changes=registered or {},
        now=now,
    )


def _diff(**updates) -> dict:
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


def _gate_doc(
    mode: str,
    *,
    platform: str = "linux",
    revision="2026-10-15.1",
    engine: str = "PASS",
    baseline: str = "PASS",
    release: str | None = None,
    checks: list | None = None,
) -> dict:
    """依 `realhw/acceptance/gate.py` 的實際輸出形狀組出的 release-gate.v1 替身。"""
    is_candidate = mode == "candidate"
    if release is None:
        release = "PASS" if is_candidate else "NOT_APPLICABLE_BASELINE"
    if checks is None:
        checks = [
            {"id": "evidence_parse", "group": "evidence", "result": "PASS", "detail": {}},
            {
                "id": "baseline_compatible",
                "group": "evidence",
                "result": "PASS" if is_candidate else "NOT_APPLICABLE_BASELINE",
                "detail": {},
            },
            {"id": "safety_unauthorized_tx", "group": "safety", "result": "PASS", "detail": {}},
        ]
    return {
        "schema_version": "release-gate.v1",
        "created_at": "2026-10-21T00:00:00+00:00",
        "platform": platform,
        "mode": mode,
        "engine_result": {"verdict": engine, "metrics": {}},
        "release_gate_result": release,
        "baseline_result": None if is_candidate else baseline,
        "acceptance": {
            "path": "realhw/acceptance/longrun_acceptance.yaml",
            "revision": revision,
            "sha256": "c" * 64,
            "workload_contract_sha256": "a" * 64,
        },
        "checks": checks,
    }


def _write_json(path: Path, document) -> Path:
    if isinstance(document, str):
        path.write_text(document, encoding="utf-8")
    else:
        path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    return path


def _gate_pair(tmp_path: Path, a_doc: dict | None = None, b_doc: dict | None = None) -> tuple[Path, Path]:
    a_path = _write_json(tmp_path / "a-release-gate.json", a_doc if a_doc is not None else _gate_doc("baseline"))
    b_path = _write_json(tmp_path / "b-release-gate.json", b_doc if b_doc is not None else _gate_doc("candidate"))
    return a_path, b_path


def _comparator_error(exc_info) -> bool:
    return exc_info.value.code == _COMPARATOR


# ---------------------------------------------------------------------------
# L3：真實形狀（字串 revision）與列舉／結構驗證（#1、#6、#7）
# ---------------------------------------------------------------------------


def test_l3_real_shaped_string_revision_is_pass(tmp_path: Path):
    a_path, b_path = _gate_pair(tmp_path)
    diff = av.compare_l3(a_path, b_path)
    assert diff["acceptance_revision"] == "2026-10-15.1"
    assert diff["integrity"] == {"a": True, "b": True}
    assert av.combine("l3", a_status="PASS", b_status="PASS", diff=diff, registered={}) == ("PASS", None, [])


def test_l3_integer_revision_still_accepted(tmp_path: Path):
    a_path, b_path = _gate_pair(tmp_path, _gate_doc("baseline", revision=4), _gate_doc("candidate", revision=4))
    assert av.compare_l3(a_path, b_path)["acceptance_revision"] == 4


def test_l3_string_revision_mismatch_is_comparator_error(tmp_path: Path):
    a_path, b_path = _gate_pair(tmp_path, b_doc=_gate_doc("candidate", revision="2026-10-15.2"))
    with pytest.raises(HarnessError) as exc_info:
        av.compare_l3(a_path, b_path)
    assert _comparator_error(exc_info)


@pytest.mark.parametrize("revision", [True, "", None])
def test_l3_invalid_revision_is_comparator_error(tmp_path: Path, revision):
    a_path, b_path = _gate_pair(tmp_path, a_doc=_gate_doc("baseline", revision=revision))
    with pytest.raises(HarnessError) as exc_info:
        av.compare_l3(a_path, b_path)
    assert _comparator_error(exc_info)


@pytest.mark.parametrize("value", ["PASS", "FAIL", "INSUFFICIENT_EVIDENCE", "BLOCKED", "NOT_APPLICABLE_BASELINE"])
def test_l3_release_gate_result_enumeration_accepted(tmp_path: Path, value: str):
    a_path, b_path = _gate_pair(tmp_path, b_doc=_gate_doc("candidate", release=value))
    assert av.compare_l3(a_path, b_path)["b_release_gate_result"] == value


def test_l3_missing_release_gate_result_is_comparator_error_not_product_fail(tmp_path: Path):
    doc = _gate_doc("candidate")
    doc["release_gate_result"] = None
    a_path, b_path = _gate_pair(tmp_path, b_doc=doc)
    with pytest.raises(HarnessError) as exc_info:
        av.compare_l3(a_path, b_path)
    assert _comparator_error(exc_info)


def test_l3_unknown_baseline_result_is_comparator_error(tmp_path: Path):
    a_path, b_path = _gate_pair(tmp_path, a_doc=_gate_doc("baseline", baseline="WEIRD"))
    with pytest.raises(HarnessError) as exc_info:
        av.compare_l3(a_path, b_path)
    assert _comparator_error(exc_info)


def test_l3_duplicate_baseline_compatible_check_is_comparator_error(tmp_path: Path):
    checks = [
        {"id": "baseline_compatible", "group": "evidence", "result": "PASS", "detail": {}},
        {"id": "baseline_compatible", "group": "evidence", "result": "FAIL", "detail": {}},
    ]
    a_path, b_path = _gate_pair(tmp_path, b_doc=_gate_doc("candidate", checks=checks))
    with pytest.raises(HarnessError) as exc_info:
        av.compare_l3(a_path, b_path)
    assert _comparator_error(exc_info)


def test_l3_missing_baseline_compatible_check_is_comparator_error(tmp_path: Path):
    checks = [{"id": "evidence_parse", "group": "evidence", "result": "PASS", "detail": {}}]
    a_path, b_path = _gate_pair(tmp_path, b_doc=_gate_doc("candidate", checks=checks))
    with pytest.raises(HarnessError) as exc_info:
        av.compare_l3(a_path, b_path)
    assert _comparator_error(exc_info)


def test_l3_baseline_compatible_fail_is_suite_infra_error(tmp_path: Path):
    checks = [{"id": "baseline_compatible", "group": "evidence", "result": "FAIL", "detail": {}}]
    a_path, b_path = _gate_pair(tmp_path, b_doc=_gate_doc("candidate", checks=checks))
    diff = av.compare_l3(a_path, b_path)
    assert diff["b_baseline_compatible"] is False
    verdict, code, _notes = av.combine("l3", a_status="PASS", b_status="PASS", diff=diff, registered={})
    assert (verdict, code) == ("INFRA_ERROR", _SUITE)


def test_l3_safety_fail_extracted_with_side_prefix_and_fails(tmp_path: Path):
    a_checks = [
        {"id": "safety_unauthorized_tx", "group": "safety", "result": "FAIL", "detail": {}},
        {"id": "baseline_compatible", "group": "evidence", "result": "NOT_APPLICABLE_BASELINE", "detail": {}},
    ]
    b_checks = [
        {"id": "safety_double_holder", "group": "safety", "result": "FAIL", "detail": {}},
        {"id": "baseline_compatible", "group": "evidence", "result": "PASS", "detail": {}},
    ]
    a_path, b_path = _gate_pair(
        tmp_path,
        a_doc=_gate_doc("baseline", checks=a_checks),
        b_doc=_gate_doc("candidate", checks=b_checks),
    )
    diff = av.compare_l3(a_path, b_path)
    assert diff["safety_violations"] == ["A:safety_unauthorized_tx", "B:safety_double_holder"]
    verdict, code, notes = av.combine("l3", a_status="PASS", b_status="PASS", diff=diff, registered={})
    assert (verdict, code) == ("FAIL", None)
    assert "safety violation: A:safety_unauthorized_tx" in notes


def test_l3_insufficient_evidence_check_breaks_integrity(tmp_path: Path):
    checks = [
        {"id": "evidence_parse", "group": "evidence", "result": "INSUFFICIENT_EVIDENCE", "detail": {}},
        {"id": "baseline_compatible", "group": "evidence", "result": "PASS", "detail": {}},
    ]
    a_path, b_path = _gate_pair(tmp_path, b_doc=_gate_doc("candidate", checks=checks))
    diff = av.compare_l3(a_path, b_path)
    assert diff["integrity"] == {"a": True, "b": False}
    verdict, code, _notes = av.combine("l3", a_status="PASS", b_status="PASS", diff=diff, registered={})
    assert (verdict, code) == ("INFRA_ERROR", _COMPARATOR)


def test_l3_side_without_evidence_checks_has_no_integrity(tmp_path: Path):
    checks = [{"id": "safety_unauthorized_tx", "group": "safety", "result": "PASS", "detail": {}}]
    a_path, b_path = _gate_pair(tmp_path, a_doc=_gate_doc("baseline", checks=checks))
    assert av.compare_l3(a_path, b_path)["integrity"] == {"a": False, "b": True}


def test_l3_mode_mismatch_is_comparator_error(tmp_path: Path):
    a_path, b_path = _gate_pair(tmp_path, a_doc=_gate_doc("candidate"))
    with pytest.raises(HarnessError) as exc_info:
        av.compare_l3(a_path, b_path)
    assert _comparator_error(exc_info)


def test_l3_platform_mismatch_is_comparator_error(tmp_path: Path):
    a_path, b_path = _gate_pair(tmp_path, b_doc=_gate_doc("candidate", platform="windows"))
    with pytest.raises(HarnessError) as exc_info:
        av.compare_l3(a_path, b_path)
    assert _comparator_error(exc_info)


def test_l3_engine_result_without_verdict_is_comparator_error(tmp_path: Path):
    doc = _gate_doc("candidate")
    doc["engine_result"] = {}
    a_path, b_path = _gate_pair(tmp_path, b_doc=doc)
    with pytest.raises(HarnessError) as exc_info:
        av.compare_l3(a_path, b_path)
    assert _comparator_error(exc_info)


def test_l3_checks_not_a_list_is_comparator_error(tmp_path: Path):
    doc = _gate_doc("candidate")
    doc["checks"] = "not-a-list"
    a_path, b_path = _gate_pair(tmp_path, b_doc=doc)
    with pytest.raises(HarnessError) as exc_info:
        av.compare_l3(a_path, b_path)
    assert _comparator_error(exc_info)


@pytest.mark.parametrize("content", ["{not json", "[]"], ids=["malformed-json", "top-level-not-object"])
def test_l3_unreadable_gate_is_comparator_error(tmp_path: Path, content: str):
    a_path = _write_json(tmp_path / "a-release-gate.json", content)
    b_path = _write_json(tmp_path / "b-release-gate.json", _gate_doc("candidate"))
    with pytest.raises(HarnessError) as exc_info:
        av.compare_l3(a_path, b_path)
    assert _comparator_error(exc_info)


def test_l3_missing_gate_file_is_comparator_error(tmp_path: Path):
    b_path = _write_json(tmp_path / "b-release-gate.json", _gate_doc("candidate"))
    with pytest.raises(HarnessError) as exc_info:
        av.compare_l3(tmp_path / "missing-release-gate.json", b_path)
    assert _comparator_error(exc_info)


def test_combine_l3_baseline_side_not_pass_is_baseline_invalid():
    diff = _diff(
        a_engine_verdict="PASS",
        a_baseline_result="PASS",
        b_engine_verdict="PASS",
        b_release_gate_result="PASS",
        b_baseline_compatible=True,
        acceptance_revision="2026-10-15.1",
        safety_violations=[],
        integrity={"a": True, "b": True},
    )
    verdict, code, _notes = av.combine("l3", a_status="FAIL", b_status="PASS", diff=diff, registered={})
    assert (verdict, code) == ("BASELINE_INVALID", "HARNESS_BASELINE_INVALID")


# ---------------------------------------------------------------------------
# L2：安全不變量、空必要集合、哨兵到期、B 側 XFAIL／XPASS（#2、#3、#4、#5）
# ---------------------------------------------------------------------------


def test_l2_invariant_fail_is_safety_fail_even_when_report_incomplete():
    cases = {"f1-too-long-rejected": {"expected": "PASS", "invariants": ["zero_tx_on_reject"], "sentinel": None}}
    a_report = _report(_result("f1-too-long-rejected"), _result("other"))
    b_report = _report(_result("f1-too-long-rejected", "FAIL", "tx observed"), _result("other", "SKIP"))
    diff = _compare(a_report, b_report, {"f1-too-long-rejected", "other"}, cases=cases)
    assert diff["verdict"] == "INCOMPLETE"
    assert diff["safety_violations"] == ["B:f1-too-long-rejected:zero_tx_on_reject"]
    verdict, code, notes = av.combine("l2", a_status="PASS", b_status="PASS", diff=diff, registered={})
    assert (verdict, code) == ("FAIL", None)
    assert "safety violation: B:f1-too-long-rejected:zero_tx_on_reject" in notes


def test_l2_invariant_case_cannot_be_waived_by_sentinel():
    cases = {"f1": _sentinel_case("tail mismatch", invariants=["zero_tx_on_reject"])}
    diff = _compare(_report(_result("f1")), _report(_result("f1", "FAIL", "tail mismatch observed")), {"f1"}, cases=cases)
    assert diff["xfail_accepted"] == []
    assert diff["b_failures"] == ["f1"]
    assert diff["safety_violations"] == ["B:f1:zero_tx_on_reject"]
    assert av.combine("l2", a_status="PASS", b_status="PASS", diff=diff, registered={})[0] == "FAIL"


def test_l2_invariant_xfail_result_is_safety_fail():
    cases = {"f2": {"expected": "PASS", "invariants": ["no_auto_resend"], "sentinel": None}}
    diff = _compare(_report(_result("f2")), _report(_result("f2", "XFAIL", "")), {"f2"}, cases=cases)
    assert diff["safety_violations"] == ["B:f2:no_auto_resend"]
    assert diff["verdict"] == "FAIL"


def test_l2_malformed_invariants_is_comparator_error():
    cases = {"f1": {"expected": "PASS", "invariants": 5, "sentinel": None}}
    with pytest.raises(HarnessError) as exc_info:
        _compare(_report(_result("f1")), _report(_result("f1", "FAIL")), {"f1"}, cases=cases)
    assert _comparator_error(exc_info)


def test_l2_empty_required_set_is_incomplete_not_pass():
    diff = _compare(_report(_result("x")), _report(_result("x")), set())
    assert diff["verdict"] == "INCOMPLETE"
    assert diff["integrity"] == {"a": False, "b": False}
    verdict, code, _notes = av.combine("l2", a_status="PASS", b_status="PASS", diff=diff, registered={})
    assert (verdict, code) == ("INFRA_ERROR", _SUITE)


@pytest.mark.parametrize(
    "expires,expected_accepted",
    [
        ("2027-01-01", True),
        ("2027-01-01T00:00:00Z", True),
        ("2026-10-20", False),
        ("2026-10-21", False),
        ("2027-01-01T00:00:00", False),
        ("not-a-date", False),
        (date(2027, 1, 1), True),
        (date(2026, 10, 20), False),
        (datetime(2027, 1, 1, tzinfo=timezone.utc), True),
        (datetime(2027, 1, 1), False),
    ],
    ids=[
        "date-string-future",
        "z-suffix-future",
        "date-string-past",
        "date-string-today-is-expired",
        "naive-datetime-string",
        "unparseable",
        "date-object-future",
        "date-object-past",
        "aware-datetime-object-future",
        "naive-datetime-object",
    ],
)
def test_sentinel_expires_forms(expires, expected_accepted: bool):
    cases = {"x": _sentinel_case(expires=expires)}
    # 計畫第 3 條：XFAIL 案例兩側結果不同一律審查，因此哨兵接受路徑的 A 側須與 B 側一致（皆 FAIL）。
    diff = _compare(
        _report(_result("x", "FAIL", "tail mismatch")),
        _report(_result("x", "FAIL", "tail mismatch")),
        {"x"},
        cases=cases,
    )
    if expected_accepted:
        assert diff["xfail_accepted"] == ["x"]
        assert diff["expired_sentinels"] == []
        assert diff["verdict"] == "PASS"
    else:
        assert diff["expired_sentinels"] == ["x"]
        assert diff["xfail_accepted"] == []
        assert diff["verdict"] == "FAIL"


def test_b_xfail_on_expected_pass_case_is_failure():
    cases = {"p": {"expected": "PASS", "invariants": [], "sentinel": None}}
    diff = _compare(_report(_result("p")), _report(_result("p", "XFAIL", "")), {"p"}, cases=cases)
    assert diff["b_failures"] == ["p"]
    assert diff["verdict"] == "FAIL"


def test_b_xfail_with_valid_sentinel_is_accepted():
    cases = {"x": _sentinel_case("tail mismatch")}
    diff = _compare(
        _report(_result("x", "XFAIL", "tail mismatch observed")),
        _report(_result("x", "XFAIL", "tail mismatch observed")),
        {"x"},
        cases=cases,
    )
    assert diff["xfail_accepted"] == ["x"]
    assert diff["needs_review"] == []
    assert diff["verdict"] == "PASS"


def test_b_xpass_on_xfail_case_needs_review():
    cases = {"x": _sentinel_case()}
    diff = _compare(_report(_result("x", "FAIL")), _report(_result("x", "XPASS")), {"x"}, cases=cases)
    assert diff["needs_review"] == [{"case": "x", "a": "FAIL", "b": "XPASS"}]
    assert diff["verdict"] == "FAIL"


def test_xfail_case_sides_differ_with_b_pass_needs_review():
    cases = {"x": _sentinel_case()}
    diff = _compare(_report(_result("x", "XFAIL")), _report(_result("x", "PASS")), {"x"}, cases=cases)
    assert diff["needs_review"] == [{"case": "x", "a": "XFAIL", "b": "PASS"}]
    assert diff["verdict"] == "FAIL"


def test_b_xpass_on_non_xfail_case_needs_review():
    diff = _compare(_report(_result("y")), _report(_result("y", "XPASS")), {"y"})
    assert diff["needs_review"] == [{"case": "y", "a": "PASS", "b": "XPASS"}]


def test_l2_result_row_without_id_is_comparator_error():
    with pytest.raises(HarnessError) as exc_info:
        _compare({"results": [{"verdict": "PASS"}]}, _report(_result("x")), {"x"})
    assert _comparator_error(exc_info)


def test_l2_non_list_results_is_incomplete():
    diff = _compare({"results": "not-a-list"}, _report(_result("x")), {"x"})
    assert diff["verdict"] == "INCOMPLETE"


# ---------------------------------------------------------------------------
# combine：diff=None、未知狀態、未知層級、L2 與 L1.5 細節分支（#7）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("layer", ["l1.5", "l2", "l3"])
def test_combine_diff_none_is_comparator_error(layer: str):
    verdict, code, notes = av.combine(layer, a_status="PASS", b_status="PASS", diff=None, registered={})
    assert (verdict, code) == ("INFRA_ERROR", _COMPARATOR)
    assert notes


def test_combine_unknown_suite_status_is_suite_error():
    verdict, code, notes = av.combine("l1.5", a_status="WEIRD", b_status="PASS", diff=_diff(), registered={})
    assert (verdict, code) == ("INFRA_ERROR", _SUITE)
    assert "unknown suite status" in notes[0]


def test_combine_unknown_layer_raises_value_error():
    with pytest.raises(ValueError):
        av.combine("l9", a_status="PASS", b_status="PASS", diff=_diff(), registered={})


def test_combine_l2_unknown_verdict_is_comparator_error():
    verdict, code, _notes = av.combine(
        "l2", a_status="PASS", b_status="PASS", diff=_diff(verdict="WEIRD"), registered={}
    )
    assert (verdict, code) == ("INFRA_ERROR", _COMPARATOR)


def test_combine_l2_fail_with_integrity_false_is_comparator_error():
    diff = _diff(verdict="FAIL", integrity={"a": False, "b": True})
    verdict, code, _notes = av.combine("l2", a_status="PASS", b_status="PASS", diff=diff, registered={})
    assert (verdict, code) == ("INFRA_ERROR", _COMPARATOR)


def test_combine_l15_missing_integrity_is_comparator_error():
    diff = _diff()
    diff.pop("integrity")
    verdict, code, _notes = av.combine("l1.5", a_status="PASS", b_status="PASS", diff=diff, registered={})
    assert (verdict, code) == ("INFRA_ERROR", _COMPARATOR)


def test_combine_rust_only_absolute_failure_note():
    diff = _diff(
        differences=[
            {"case": "sc15-x", "path": "call[0].state", "kind": "value", "registered_change": "SC-15"}
        ],
        b_cases={"sc15-x": "FAIL"},
    )
    verdict, code, notes = av.combine(
        "l1.5", a_status="PASS", b_status="PASS", diff=diff, registered={"SC-15": "absolute_only"}
    )
    assert (verdict, code) == ("FAIL", None)
    assert notes == ["rust_only case failed absolute: sc15-x"]


def test_combine_unregistered_difference_note():
    diff = _diff(
        differences=[{"case": "x", "path": "call[0].ok", "kind": "value", "registered_change": None}]
    )
    verdict, code, notes = av.combine("l1.5", a_status="PASS", b_status="PASS", diff=diff, registered={})
    assert (verdict, code) == ("FAIL", None)
    assert notes == ["unregistered difference: x call[0].ok"]


# ---------------------------------------------------------------------------
# case_changes 與 load_registered_changes 錯誤路徑（#7）
# ---------------------------------------------------------------------------

_CHANGES_YAML = """schema_version: 1
revision: 4
changes:
  - id: "BC-01"
    scope: "both"
    ab_participation: "diff"
    case_ids: ["bc01-a", "bc01-b"]
  - id: "SC-15"
    scope: "rust_only"
    ab_participation: "absolute_only"
    case_ids: ["sc15-x"]
  - id: "SC-16"
    scope: "rust_only"
    ab_participation: "absolute_only"
"""


def _write_changes(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "contracts" / "behavior_changes.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_case_changes_maps_case_to_change(tmp_path: Path):
    path = _write_changes(tmp_path, _CHANGES_YAML)
    assert av.case_changes(path) == {"bc01-a": "BC-01", "bc01-b": "BC-01", "sc15-x": "SC-15"}


def test_case_changes_scope_filter(tmp_path: Path):
    path = _write_changes(tmp_path, _CHANGES_YAML)
    assert av.case_changes(path, scope="both") == {"bc01-a": "BC-01", "bc01-b": "BC-01"}
    assert av.case_changes(path, scope="rust_only") == {"sc15-x": "SC-15"}


def test_case_changes_duplicate_case_across_changes_is_error(tmp_path: Path):
    text = _CHANGES_YAML + (
        '  - id: "BC-02"\n'
        '    scope: "both"\n'
        '    ab_participation: "diff"\n'
        '    case_ids: ["bc01-a"]\n'
    )
    path = _write_changes(tmp_path, text)
    with pytest.raises(HarnessError) as exc_info:
        av.case_changes(path)
    assert _comparator_error(exc_info)


@pytest.mark.parametrize(
    "case_ids_line",
    ['    case_ids: "bc01-a"\n', "    case_ids: [1]\n"],
    ids=["case-ids-not-list", "case-id-not-string"],
)
def test_case_changes_malformed_case_ids_is_error(tmp_path: Path, case_ids_line: str):
    text = (
        'changes:\n'
        '  - id: "BC-01"\n'
        '    scope: "both"\n'
        '    ab_participation: "diff"\n'
        + case_ids_line
    )
    path = _write_changes(tmp_path, text)
    with pytest.raises(HarnessError) as exc_info:
        av.case_changes(path)
    assert _comparator_error(exc_info)


@pytest.mark.parametrize(
    "text",
    [
        "schema_version: 1\n",
        'changes: ["x"]\n',
        "changes: [\n",
        'changes:\n'
        '  - id: "BC-01"\n    scope: "both"\n    ab_participation: "diff"\n'
        '  - id: "BC-01"\n    scope: "both"\n    ab_participation: "diff"\n',
        'changes:\n  - id: "BC-01"\n    scope: "both"\n    ab_participation: "bogus"\n',
        'changes:\n  - id: "SC-15"\n    scope: "rust_only"\n    ab_participation: "diff"\n',
        'changes:\n  - scope: "both"\n    ab_participation: "diff"\n',
    ],
    ids=[
        "missing-changes",
        "entry-not-mapping",
        "yaml-syntax",
        "duplicate-id",
        "invalid-participation",
        "scope-participation-mismatch",
        "missing-id",
    ],
)
def test_load_registered_changes_rejects_malformed(tmp_path: Path, text: str):
    path = _write_changes(tmp_path, text)
    with pytest.raises(HarnessError) as exc_info:
        av.load_registered_changes(path)
    assert _comparator_error(exc_info)


def test_load_registered_changes_missing_file_is_error(tmp_path: Path):
    with pytest.raises(HarnessError) as exc_info:
        av.load_registered_changes(tmp_path / "missing-behavior-changes.yaml")
    assert _comparator_error(exc_info)


def test_compare_l3_does_not_mutate_gate_documents(tmp_path: Path):
    """讀取 gate 後不得改寫來源 JSON（只讀 JSON 的契約）。"""
    a_doc = _gate_doc("baseline")
    b_doc = _gate_doc("candidate")
    a_path, b_path = _gate_pair(tmp_path, a_doc, b_doc)
    before = copy.deepcopy((a_doc, b_doc))
    av.compare_l3(a_path, b_path)
    assert json.loads(a_path.read_text(encoding="utf-8")) == before[0]
    assert json.loads(b_path.read_text(encoding="utf-8")) == before[1]


def test_invariants_given_as_single_string_is_one_invariant():
    cases = {"f": {"expected": "PASS", "invariants": "zero_tx_on_reject", "sentinel": None}}
    diff = _compare(_report(_result("f")), _report(_result("f", "FAIL", "tx")), {"f"}, cases=cases)
    assert diff["safety_violations"] == ["B:f:zero_tx_on_reject"]


def test_sentinel_expires_of_unknown_type_is_expired():
    cases = {"x": _sentinel_case(expires=12345)}
    diff = _compare(_report(_result("x")), _report(_result("x", "FAIL", "tail mismatch")), {"x"}, cases=cases)
    assert diff["expired_sentinels"] == ["x"]
    assert diff["verdict"] == "FAIL"


def test_sentinel_not_a_mapping_is_not_waived():
    cases = {"x": {"expected": "XFAIL", "invariants": [], "sentinel": "tail mismatch"}}
    diff = _compare(_report(_result("x")), _report(_result("x", "FAIL", "tail mismatch")), {"x"}, cases=cases)
    assert diff["b_failures"] == ["x"]
    assert diff["xfail_accepted"] == []


def test_sentinel_without_signature_is_not_waived():
    sentinel = {"issue": "#159", "expires": _FUTURE}
    cases = {"x": {"expected": "XFAIL", "invariants": [], "sentinel": sentinel}}
    diff = _compare(_report(_result("x")), _report(_result("x", "FAIL", "tail mismatch")), {"x"}, cases=cases)
    assert diff["b_failures"] == ["x"]
    assert diff["xfail_accepted"] == []
