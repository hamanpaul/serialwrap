"""OD-75：Python daemon 基線已知缺陷不擋 Stage 1 出口，證據維持 FAIL。

登記檔（tests/acceptance/python_baseline_defects.json）與清單 B 段對得上時，
符合平台、狀態與訊息特徵的失敗列入「基線已知缺陷（不擋）」；不符合的照擋。
A、B 兩輪同一 nodeid 都失敗且未登記者標「疑似基線問題，待分類」並照擋。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import scripts.rust.l15_exit_driver as l15_exit_driver
from tests.test_rust_ab_compare import run as run_ab
from tests.test_rust_stage1_exit_driver import (
    _passing_windows_scenarios_report,
    _windows_l15_evidence,
    _windows_l15_report,
)
from tests.test_rust_stage1a_gate import (
    PYT_MANIFEST,
    PYT_MKBUNDLE,
    PYT_FULL_SEL,
    A1,
    Z,
    GateTree,
    _case_row,
    _l1_row,
    _pytest_argv,
    _report,
)

pytestmark = [
    pytest.mark.core_portable,
    pytest.mark.process_rpc,
]

MARKER = "BASELINE_DEFECT_MARKER"
L15_ID = "l15-baseline-demo"
AB_ID = "tests.contract_blackbox.test_bb_event::test_event_subcommands_wire_params"
AB_CASE = "test_bb_event.py::test_event_subcommands_wire_params"
AB_NODEID = "tests/contract_blackbox/test_bb_event.py::test_event_subcommands_wire_params"
KNOWN = "基線已知缺陷（不擋）"
PENDING = "疑似基線問題，待分類"


def _ledger(path: Path, ids: list[str]) -> None:
    rows = "\n".join(f"| {item} | 測試用根因 |" for item in ids)
    path.write_text(
        "# ledger\n\n## B. 測試用清單\n\n| id | 說明 |\n|---|---|\n"
        f"{rows}\n\n## C. 其他\n",
        encoding="utf-8",
    )


def _registry(path: Path, defects: list[dict]) -> None:
    path.write_text(
        json.dumps({"schema_version": "python-baseline-defects.v1", "defects": defects}, ensure_ascii=False),
        encoding="utf-8",
    )


def _use(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, defects: list[dict], ids: list[str] | None = None) -> None:
    ledger_ids = ids if ids is not None else sorted({row["defect_id"] for row in defects}) or ["B1"]
    _registry(tmp_path / "defects.json", defects)
    _ledger(tmp_path / "ledger.md", ledger_ids)
    monkeypatch.setenv("SERIALWRAP_PYTHON_BASELINE_DEFECTS", str(tmp_path / "defects.json"))
    monkeypatch.setenv("SERIALWRAP_STAGE1_LEDGER", str(tmp_path / "ledger.md"))


def _pytest_entry(**overrides: object) -> dict:
    row = {
        "defect_id": "B1",
        "nodeid": PYT_MKBUNDLE,
        "platform": "linux",
        "allowed_statuses": ["failed"],
        "message_regex": re.escape(MARKER),
        "root_cause": "Python daemon 基線已知缺陷",
        "od75_basis": "OD-75 已登記缺陷不擋出口",
    }
    row.update(overrides)
    return row


def _fail_pytest_full(tree: GateTree, message: str = MARKER) -> None:
    outcomes = {PYT_MKBUNDLE: "failed", PYT_MANIFEST: "passed"}
    tree.l1_reports[("linux", "pytest-full")] = _report(
        "linux", runner="pytest", argv=_pytest_argv(PYT_FULL_SEL), markexpr="",
        nodeids=sorted(outcomes), outcomes=outcomes, exit_code=1,
    )
    cases = []
    for nid, outcome in outcomes.items():
        path, *rest = nid.partition("[")[0].split("::")
        cls = ".".join([path.removesuffix(".py").replace("/", "."), *rest[:-1]])
        name = rest[-1] + nid[len(nid.partition("[")[0]):]
        body = f'<failure message="{message}"/>' if outcome == "failed" else ""
        cases.append(f'<testcase classname="{cls}" name="{name}">{body}</testcase>')
    attrs = 'tests="2" failures="1" errors="0" skipped="0"'
    tree.pytest_junit_xml = f"<testsuites {attrs}><testsuite {attrs}>{''.join(cases)}</testsuite></testsuites>"


def _ab_case(**overrides: object) -> dict:
    row = {
        "id": AB_ID,
        "case": AB_CASE,
        "class": "baseline-fail",
        "a": "fail",
        "b": "fail",
        "reason": MARKER,
        "a_message": MARKER,
        "b_message": MARKER,
    }
    row.update(overrides)
    return row


def _with_ab(tree: GateTree, extra: dict) -> None:
    tree.ab_report = {
        "result": "fail",
        "errors": [],
        "identity": {"a": {"platform": "linux"}},
        "cases": [
            {"id": A1, "class": "ok", "a": "pass", "b": "pass", "reason": ""},
            extra,
        ],
    }


def _sections(rep: dict) -> tuple[list, list]:
    assert "baseline_known_defects" in rep, "gate 報告須另列基線已知缺陷"
    assert "baseline_pending_classification" in rep, "gate 報告須另列待分類"
    return rep["baseline_known_defects"], rep["baseline_pending_classification"]


def test_registered_pytest_defect_is_listed_and_does_not_block(monkeypatch, tmp_path):
    """已登記且訊息符合：L1 判決維持 FAIL，整體不擋，並列入基線已知缺陷。"""
    _use(monkeypatch, tmp_path, [_pytest_entry()])
    tree = GateTree(tmp_path / "repo")
    _fail_pytest_full(tree)
    code, rep = tree.run()
    known, pending = _sections(rep)
    assert code == 0
    assert rep["overall"] == "IMPLEMENTED"
    assert pending == []
    assert _l1_row(rep, "s1_pytest_full", "linux")["verdict"] == "FAIL"
    assert known and known[0]["classification"] == KNOWN and known[0]["blocking"] is False
    assert known[0]["defect_id"] == "B1"
    assert "pytest JUnit 報告含失敗或錯誤" not in rep["errors"]


def test_unregistered_pytest_failure_still_blocks(monkeypatch, tmp_path):
    _use(monkeypatch, tmp_path, [])
    tree = GateTree(tmp_path / "repo")
    _fail_pytest_full(tree)
    _code, rep = tree.run()
    known, pending = _sections(rep)
    assert rep["overall"] == "FAIL"
    assert known == []
    assert pending == []


def test_only_b_failure_blocks_even_when_message_matches_registry(monkeypatch, tmp_path):
    """只有 B 輪失敗是回歸，不是基線缺陷；即使訊息符合登記也照擋、不列入不擋。"""
    _use(monkeypatch, tmp_path, [_pytest_entry()])
    tree = GateTree(tmp_path / "repo")
    _fail_pytest_full(tree)
    extra = _ab_case(
        id=PYT_MKBUNDLE,
        case="test_rust_mkbundle.py::test_mkbundle_respects_cargo_target_dir_override",
        a="pass",
        b="fail",
        a_message="",
        b_message=MARKER,
        reason=MARKER,
    )
    extra["class"] = "regression"
    _with_ab(tree, extra)
    _code, rep = tree.run()
    known, pending = _sections(rep)
    assert rep["overall"] == "FAIL"
    assert all(row.get("id") != PYT_MKBUNDLE for row in known)
    assert pending == []


def test_signature_mismatch_blocks(monkeypatch, tmp_path):
    _use(monkeypatch, tmp_path, [_pytest_entry(message_regex="OTHER_SIGNATURE")])
    tree = GateTree(tmp_path / "repo")
    _fail_pytest_full(tree, message=MARKER)
    _code, rep = tree.run()
    known, pending = _sections(rep)
    assert rep["overall"] == "FAIL"
    assert known == []
    assert pending == []


def test_both_rounds_failing_unregistered_is_pending_and_blocks(monkeypatch, tmp_path):
    _use(monkeypatch, tmp_path, [])
    tree = GateTree(tmp_path / "repo")
    _with_ab(tree, _ab_case(a_message="A " + MARKER, b_message="B " + MARKER, reason="B " + MARKER))
    _code, rep = tree.run()
    known, pending = _sections(rep)
    assert rep["overall"] == "FAIL"
    assert known == []
    assert len(pending) == 1
    assert pending[0]["classification"] == PENDING
    assert pending[0]["blocking"] is True
    assert pending[0]["a_message"] == "A " + MARKER
    assert pending[0]["b_message"] == "B " + MARKER


def test_both_rounds_failing_registered_does_not_block(monkeypatch, tmp_path):
    _use(monkeypatch, tmp_path, [_pytest_entry(
        nodeid=AB_NODEID,
        allowed_statuses=["fail"],
        message_regex=re.escape(MARKER),
    )])
    tree = GateTree(tmp_path / "repo")
    _with_ab(tree, _ab_case())
    code, rep = tree.run()
    known, pending = _sections(rep)
    assert code == 0
    assert rep["overall"] == "IMPLEMENTED"
    assert pending == []
    assert any(row["classification"] == KNOWN and row["blocking"] is False for row in known)


def test_corrupt_registry_fails_the_gate(monkeypatch, tmp_path):
    (tmp_path / "defects.json").write_text("{", encoding="utf-8")
    monkeypatch.setenv("SERIALWRAP_PYTHON_BASELINE_DEFECTS", str(tmp_path / "defects.json"))
    monkeypatch.setenv("SERIALWRAP_STAGE1_LEDGER", str(tmp_path / "ledger.md"))
    tree = GateTree(tmp_path / "repo")
    _code, rep = tree.run()
    assert rep["overall"] == "FAIL"
    assert any("登記無效" in item for item in rep["errors"])


def test_registry_schema_rejects_missing_field_bad_regex_and_unknown_id(tmp_path):
    try:
        from scripts.rust.python_baseline_defects import BaselineDefectRegistryError, load_registry
    except ImportError:
        pytest.fail("尚未提供 python_baseline_defects")
    ledger = tmp_path / "ledger.md"
    _ledger(ledger, ["B1"])
    missing = tmp_path / "missing.json"
    _registry(missing, [{"defect_id": "B1", "nodeid": PYT_MKBUNDLE}])
    with pytest.raises(BaselineDefectRegistryError, match="platform"):
        load_registry(missing, ledger_path=ledger)
    bad = tmp_path / "bad.json"
    _registry(bad, [_pytest_entry(message_regex="(")])
    with pytest.raises(BaselineDefectRegistryError):
        load_registry(bad, ledger_path=ledger)
    unknown = tmp_path / "unknown.json"
    _registry(unknown, [_pytest_entry(defect_id="B9")])
    with pytest.raises(BaselineDefectRegistryError, match="B9"):
        load_registry(unknown, ledger_path=ledger)


def test_registered_l15_failure_stays_fail_and_does_not_block(monkeypatch, tmp_path):
    _use(monkeypatch, tmp_path, [{
        "defect_id": "B1",
        "l15_case_id": L15_ID,
        "platform": "windows",
        "allowed_statuses": ["FAIL"],
        "message_regex": re.escape(MARKER),
        "root_cause": "Python daemon 基線已知缺陷",
        "od75_basis": "OD-75 已登記缺陷不擋出口",
    }])
    tree = GateTree(tmp_path / "repo")
    tree.cases.append({"case_id": L15_ID, "layer": "L1.5", "cli_stage": "1A", "platforms": ["linux", "windows"]})
    tree.lock["sets"]["cli_1A"] = sorted([A1, Z, L15_ID])
    tree.l15_reports = [
        ("linux", {"results": [{
            "case_id": L15_ID, "verdict": "PASS", "gate_eligible": True,
            "message": "OK", "harness_diagnostics": [],
        }]}),
        ("windows", {"results": [{
            "case_id": L15_ID, "verdict": "FAIL", "gate_eligible": False,
            "message": MARKER, "harness_diagnostics": [],
        }]}),
    ]
    code, rep = tree.run()
    known, _pending = _sections(rep)
    assert _case_row(rep, L15_ID)["status"] == "FAIL"
    assert code == 0
    assert rep["overall"] == "IMPLEMENTED"
    assert any(row["kind"] == "l15" and row["classification"] == KNOWN and row["blocking"] is False for row in known)


def _baseline_l15_gate(case_id: str, platform: str) -> dict:
    evidence = [
        {"platform": "linux", "verdict": "PASS", "gate_eligible": True, "message": "OK", "harness_diagnostics": []},
        {"platform": "windows", "verdict": "PASS", "gate_eligible": True, "message": "OK", "harness_diagnostics": []},
    ]
    for row in evidence:
        if row["platform"] == platform:
            row["verdict"] = "FAIL"
            row["gate_eligible"] = False
            row["message"] = MARKER
    return {
        "overall": "IMPLEMENTED",
        "errors": [],
        "cases": [{"id": case_id, "layer": "L1.5", "status": "FAIL", "evidence": evidence}],
    }


def test_exit_driver_suppresses_registered_windows_l15_failure(monkeypatch, tmp_path):
    _use(monkeypatch, tmp_path, [{
        "defect_id": "B1",
        "l15_case_id": "brcm-01-login-flow",
        "platform": "windows",
        "allowed_statuses": ["FAIL"],
        "message_regex": re.escape(MARKER),
        "root_cause": "Python daemon 基線已知缺陷",
        "od75_basis": "OD-75 已登記缺陷不擋出口",
    }])
    gate = _baseline_l15_gate("brcm-01-login-flow", "windows")
    scenarios = _passing_windows_scenarios_report()
    basic = _windows_l15_report(
        [{"case_id": "brcm-01-login-flow", "verdict": "FAIL", "message": MARKER}],
        report_subdir="report-basic",
    )
    unresolved, suppressed = l15_exit_driver.unresolved_steps(
        "win_l15_func_test 1\nstage1a_gate 0\n", gate, _windows_l15_evidence(scenarios, basic),
    )
    assert unresolved == []
    assert suppressed == [{"step": "win_l15_func_test", "rc": 1}]


def test_exit_driver_suppresses_registered_linux_l15_producer(monkeypatch, tmp_path):
    _use(monkeypatch, tmp_path, [{
        "defect_id": "B1",
        "l15_case_id": "brcm-01-login-flow",
        "platform": "linux",
        "allowed_statuses": ["FAIL"],
        "message_regex": re.escape(MARKER),
        "root_cause": "Python daemon 基線已知缺陷",
        "od75_basis": "OD-75 已登記缺陷不擋出口",
    }])
    unresolved, suppressed = l15_exit_driver.unresolved_steps(
        "l15_linux_bridge_scenarios 1\nstage1a_gate 0\n",
        _baseline_l15_gate("brcm-01-login-flow", "linux"),
    )
    assert unresolved == []
    assert suppressed == [{"step": "l15_linux_bridge_scenarios", "rc": 1}]


def test_rejudge_without_driver_log_is_not_the_same_judgment(tmp_path, capsys):
    code = l15_exit_driver.main(["--rejudge", str(tmp_path)])
    payload = json.loads(capsys.readouterr().out)
    assert code == 1
    assert payload["same_judgment"] is False


def test_ab_compare_keeps_both_side_messages(tmp_path):
    code, rep = run_ab(tmp_path, [("t.mod::c1", "fail")], [("t.mod::c1", "fail")])
    assert code == 1
    assert rep["cases"][0]["a_message"] == "x"
    assert rep["cases"][0]["b_message"] == "x"
