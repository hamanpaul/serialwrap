"""OD-75 第二輪：精確識別、原始 producer 證據與重判出口回歸。"""
from __future__ import annotations

import copy
import json
from types import SimpleNamespace
from pathlib import Path

import pytest

from scripts.rust import l15_exit_driver as driver
from scripts.rust import python_baseline_defects as defects
from scripts.rust import stage1a_gate as gate
from tests.test_python_baseline_defects import (
    AB_CASE, AB_ID, AB_NODEID, MARKER, _ab_case, _baseline_l15_gate,
    _fail_pytest_full, _pytest_entry, _use, _with_ab,
)
from tests.test_rust_stage1a_gate import (
    COVERAGE_SRC, GateTree, PYT_MANIFEST, PYT_MKBUNDLE,
    PYT_FULL_SEL, _l1_row, _pytest_argv, _report,
)

pytestmark = [pytest.mark.core_portable, pytest.mark.process_rpc]


def test_driver_preserves_each_non_gate_producer_report():
    source = (driver._REPO_ROOT / "scripts/rust/stage1_exit_driver.sh").read_text()
    for folder in ("bbproc-multiagent-coexist", "bbproc-1b"):
        assert f'SW_RESOURCE_REPORT="$EV/{folder}/resource-report.json"' in source
        assert f'--junitxml="$EV/{folder}/junit.xml"' in source
    assert 'step baseline_evidence_snapshot snapshot_p4_evidence "$P4" "$EV/p4"' in source



def _ab_index(rows):
    return {r["id"]: r for r in rows}, {AB_CASE: rows}


def test_parameterized_b_only_failure_cannot_exempt_pytest(monkeypatch, tmp_path):
    node = AB_NODEID + "[a]"
    _use(monkeypatch, tmp_path, [_pytest_entry(nodeid=node, allowed_statuses=["failed", "fail"])])
    registry = defects.load_for_gate(driver._REPO_ROOT)
    rows = [_ab_case(id=AB_ID + "[a]", a="pass", b="fail", **{"class": "regression"}),
            _ab_case(id=AB_ID + "[b]", a="pass", b="pass", **{"class": "ok"})]
    exact, normalized = _ab_index(rows)
    assert gate._find_ab_row(node, exact, normalized) is rows[0]
    assert not gate._ab_allows_pytest_exemption(registry, registry.entries[0], node, exact, normalized, "linux")


def test_ambiguous_ab_candidate_cannot_be_treated_as_absent(monkeypatch, tmp_path):
    _use(monkeypatch, tmp_path, [_pytest_entry(nodeid=AB_NODEID + "[c]")])
    registry = defects.load_for_gate(driver._REPO_ROOT)
    rows = [_ab_case(id=AB_ID + "[a]"), _ab_case(id=AB_ID + "[b]")]
    exact, normalized = _ab_index(rows)
    assert not gate._ab_allows_pytest_exemption(
        registry, registry.entries[0], AB_NODEID + "[c]", exact, normalized, "linux",
    )


def test_parameter_exemption_does_not_cover_sibling_or_function(monkeypatch, tmp_path):
    _use(monkeypatch, tmp_path, [_pytest_entry(nodeid=AB_NODEID + "[a]", allowed_statuses=["fail"])])
    tree = GateTree(tmp_path / "repo")
    tree.cases.append({"case_id": AB_CASE, "layer": "L1", "cli_stage": "1A"})
    tree.lock["sets"]["cli_1A"].append(AB_CASE)
    _with_ab(tree, _ab_case(id=AB_ID + "[a]"))
    tree.ab_report["cases"].append(_ab_case(id=AB_ID + "[b]", a="pass", **{"class": "regression"}))
    _, rep = tree.run()
    assert rep["overall"] == "FAIL"
    registry = defects.load_for_gate(driver._REPO_ROOT)
    exact, _ = _ab_index(tree.ab_report["cases"][1:])
    exempt, _, _, _ = gate._classify_baseline_ab(exact, "fail", [], "linux", registry)
    assert exempt == {AB_ID + "[a]"}


def test_unparameterized_registration_cannot_match_parameter_via_case_alias(monkeypatch, tmp_path):
    _use(monkeypatch, tmp_path, [_pytest_entry(nodeid=AB_NODEID, allowed_statuses=["fail"])])
    registry = defects.load_for_gate(driver._REPO_ROOT)
    assert gate._match_ab_entry(registry, _ab_case(id=AB_ID + "[a]"), "linux", "fail", MARKER) is None


def test_real_slugged_l1_case_uses_all_ab_evidence(monkeypatch, tmp_path):
    _use(monkeypatch, tmp_path, [_pytest_entry(nodeid=AB_NODEID, allowed_statuses=["fail"])])
    tree = GateTree(tmp_path / "repo")
    slug = "bb-session-reaches-ready"
    tree.cases.append({"case_id": slug, "layer": "L1", "cli_stage": "1A", "source_ref": AB_NODEID})
    tree.lock["sets"]["cli_1A"].append(slug)
    _with_ab(tree, _ab_case())
    code, rep = tree.run()
    assert code == 0, rep
    assert next(r for r in rep["cases"] if r["id"] == slug)["status"] == "baseline-fail"


@pytest.mark.parametrize("node,observed", [
    ("tests/test_human_agent_coexist.py::TestHumanAgentCoexist::test_coexist",
     "tests.test_human_agent_coexist.TestHumanAgentCoexist::test_coexist"),
    ("tests/pkg/test_x.py::Outer::Inner::fn[a::b.c]", "tests.pkg.test_x.Outer.Inner::fn[a::b.c]"),
])
def test_class_nodeid_matches_junit_without_losing_parameters(node, observed):
    assert defects.nodeids_equal(node, observed)
    assert not defects.nodeids_equal(node, observed + "[other]")


def test_nodeid_does_not_merge_same_filename_in_different_directories():
    assert not defects.nodeids_equal("tests/a/test_x.py::fn[p]", "tests.b.test_x::fn[p]")


def test_registered_failure_does_not_poison_real_safety_invariant(monkeypatch, tmp_path):
    _use(monkeypatch, tmp_path, [_pytest_entry()])
    tree = GateTree(tmp_path / "repo")
    _fail_pytest_full(tree)
    inv = next(i for i in json.loads(COVERAGE_SRC.read_text())["invariants"]
               if i["id"] == "evidence-insufficient-never-pass")
    tree.coverage["invariants"][0] = inv
    extra = "".join(f'<testcase classname="tests.{n.split("::")[0][:-3]}" name="{n.split("::")[1]}"/>'
                    for n in inv["cases"])
    tree.pytest_junit_xml = tree.pytest_junit_xml.replace("</testsuite>", extra + "</testsuite>")
    tree.pytest_junit_xml = tree.pytest_junit_xml.replace('tests="2"', f'tests="{2 + len(inv["cases"])}"')
    # 額外的 invariant 列也出現在同一次 resource report，維持 totals 對帳。
    outcomes = {PYT_MKBUNDLE: "failed", PYT_MANIFEST: "passed", **{"tests/" + n: "passed" for n in inv["cases"]}}
    tree.l1_reports[("linux", "pytest-full")] = _report(
        "linux", runner="pytest", argv=_pytest_argv(PYT_FULL_SEL), markexpr="",
        nodeids=list(outcomes), outcomes=outcomes, exit_code=1,
    )
    code, rep = tree.run()
    assert code == 0, rep
    assert next(r for r in rep["invariants"] if r["id"] == inv["id"])["status"] == "PASS"


def test_registered_failure_still_reconciles_passing_required_node(monkeypatch, tmp_path):
    _use(monkeypatch, tmp_path, [_pytest_entry()])
    tree = GateTree(tmp_path / "repo")
    _fail_pytest_full(tree)
    tree.pytest_junit_xml = tree.pytest_junit_xml.replace(
        'name="test_manifest_schema_accepts_new_optional_fields"', 'name="unrelated_passing_node"',
    )
    _, rep = tree.run()
    assert rep["overall"] == "FAIL"
    assert any("逐列對帳" in r for r in _l1_row(rep, "s1_pytest_full", "linux")["reasons"])


def test_registered_failures_in_multiple_suites_remove_one_blanket_error(monkeypatch, tmp_path):
    _use(monkeypatch, tmp_path, [_pytest_entry(), _pytest_entry(nodeid=PYT_MANIFEST)])
    tree = GateTree(tmp_path / "repo")
    outcomes = {PYT_MKBUNDLE: "failed", PYT_MANIFEST: "failed"}
    tree.l1_reports[("linux", "pytest-full")] = _report(
        "linux", runner="pytest", argv=_pytest_argv(PYT_FULL_SEL), markexpr="",
        nodeids=list(outcomes), outcomes=outcomes, exit_code=1,
    )
    suites = []
    for node in outcomes:
        cls, name = gate._pytest_exact_key(node).split("::")
        suites.append(f'<testsuite tests="1" failures="1" errors="0" skipped="0">'
                      f'<testcase classname="{cls}" name="{name}"><failure message="{MARKER}"/>'
                      '</testcase></testsuite>')
    tree.pytest_junit_xml = '<testsuites>' + ''.join(suites) + '</testsuites>'
    code, rep = tree.run()
    assert code == 0, rep["errors"]
    assert gate._PYTEST_JUNIT_FAILURE not in rep["errors"]
    assert _l1_row(rep, "s1_pytest_full", "linux")["verdict"] == "FAIL"


@pytest.mark.parametrize("damage", ["b-only", "count-mismatch"])
def test_blanket_error_requires_ab_agreement_and_exact_failure_counts(monkeypatch, tmp_path, damage):
    _use(monkeypatch, tmp_path, [_pytest_entry()])
    tree = GateTree(tmp_path / "repo")
    _fail_pytest_full(tree)
    if damage == "b-only":
        _with_ab(tree, _ab_case(id=PYT_MKBUNDLE, a="pass", **{"class": "regression"}))
    else:
        tree.pytest_junit_xml = tree.pytest_junit_xml.replace('failures="1"', 'failures="2"')
    _, rep = tree.run()
    assert gate._PYTEST_JUNIT_FAILURE in rep["errors"]


LINUX_REPORTS = {
    "l15_linux_bridge": ("func-test", False),
    "l15_linux_bridge_scenarios": ("func-test-scenarios", True),
    "l15_linux_1b_bridge": ("p4/l15-linux", False),
    "l15_linux_1b_scenarios": ("p4/l15-linux-scenarios", True),
}


def _l15_entry():
    row = _pytest_entry(allowed_statuses=["FAIL"])
    row.pop("nodeid")
    return {**row, "l15_case_id": "known-case"}


def _linux_report():
    return {"schema_version": "func-test-report.v1", "complete": True, "independent_cases": 1,
            "modes": {"bridge": {"runs": 1, "pass": 0, "fail": 1, "skip": 0, "insufficient": 0},
                      "pty": {"runs": 0, "pass": 0, "fail": 0, "skip": 0, "insufficient": 0}},
            "results": [{"case_id": "known-case", "mode": "bridge", "run_index": 0, "verdict": "FAIL",
                         "message": MARKER, "harness_diagnostics": []}]}


@pytest.mark.parametrize("step", list(LINUX_REPORTS))
@pytest.mark.parametrize("damage", [None, "missing", "incomplete", "undeclared", "other-failure", "wrong-repeat"])
def test_linux_step_requires_its_own_complete_registered_results(monkeypatch, tmp_path, step, damage):
    _use(monkeypatch, tmp_path, [_l15_entry()])
    folder, scenarios = LINUX_REPORTS[step]
    monkeypatch.setattr(driver, "_declared_repeat", lambda cid, *, scenarios: 1 if cid == "known-case" else None)
    ev = tmp_path / "evidence"
    path = ev / folder / "func-test-report.json"
    path.parent.mkdir(parents=True)
    report = _linux_report()
    if damage == "incomplete":
        report["complete"] = False
    if damage == "undeclared":
        report["results"][0]["case_id"] = "unknown-case"
    if damage == "other-failure":
        report["results"].append({**report["results"][0], "case_id": "unknown-case"})
    if damage == "wrong-repeat":
        report["results"][0]["run_index"] = 2
    if damage != "missing":
        path.write_text(json.dumps(report))
    approved = _baseline_l15_gate("known-case", "linux")
    unresolved, suppressed = driver.unresolved_steps(
        f"{step} 1\nstage1a_gate 0\n", approved, evidence_dir=ev,
    )
    assert (not unresolved) is (damage is None)
    assert bool(suppressed) is (damage is None)


def test_linux_producer_registration_does_not_require_stage1a_case(monkeypatch, tmp_path):
    """Stage 1B producer 的缺陷也須由自己的報告判定，不依賴 Stage 1A 的 L1.5 分母。"""
    _use(monkeypatch, tmp_path, [_l15_entry()])
    monkeypatch.setattr(driver, "_declared_repeat", lambda cid, *, scenarios: 1)
    path = tmp_path / "p4/l15-linux/func-test-report.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(_linux_report()))
    unresolved, suppressed = driver.unresolved_steps(
        "l15_linux_1b_bridge 1\nstage1a_gate 0\n", {"overall": "IMPLEMENTED", "errors": [], "cases": []},
        evidence_dir=tmp_path,
    )
    assert unresolved == []
    assert suppressed == [{"step": "l15_linux_1b_bridge", "rc": 1}]


def _pytest_evidence(ev, folder="l1/linux/pytest-full", *, extra_failure=False):
    path = ev / folder
    path.mkdir(parents=True, exist_ok=True)
    outcomes = {PYT_MKBUNDLE: "failed", PYT_MANIFEST: "failed" if extra_failure else "passed"}
    report = _report("linux", runner="pytest", argv=_pytest_argv(PYT_FULL_SEL), markexpr="",
                     nodeids=list(outcomes), outcomes=outcomes, exit_code=1)
    (path / "resource-report.json").write_text(json.dumps(report))
    rows = "".join(f'<testcase nodeid="{n}">' +
                   (f'<failure message="{MARKER}"/>' if o == "failed" else "") + '</testcase>'
                   for n, o in outcomes.items())
    xml = f'<testsuites><testsuite failures="{1 + int(extra_failure)}" errors="0">{rows}</testsuite></testsuites>'
    (path / "junit.xml").write_text(xml)
    (ev / "pytest-full.xml").write_text(xml)


@pytest.mark.parametrize("step,folder,kind", [
    ("pytest_full_junit", "l1/linux/pytest-full", "pytest"),
    ("suite_20groups_linux", "p4/l1/linux/20groups", "pytest"),
    ("bbproc_multiagent_coexist", "bbproc-multiagent-coexist", "pytest"),
    ("bbproc_1b", "bbproc-1b", "pytest"),
    ("ab_blackbox_release", "l1/linux/blackbox", "ab"),
])
@pytest.mark.parametrize("extra_failure", [False, True])
def test_nodeid_producer_requires_all_raw_failures_approved(monkeypatch, tmp_path, step, folder, kind, extra_failure):
    _use(monkeypatch, tmp_path, [_pytest_entry(allowed_statuses=["fail", "failed"])])
    ev = tmp_path / "evidence"
    _pytest_evidence(ev, folder, extra_failure=extra_failure)
    ab = _ab_case(id=PYT_MKBUNDLE)
    (ev / "ab-report.json").write_text(json.dumps({"result": "fail", "errors": [],
        "identity": {"a": {"platform": "linux"}}, "cases": [ab] + ([_ab_case(id=PYT_MANIFEST)] if extra_failure else [])}))
    approved = {"overall": "IMPLEMENTED", "errors": [], "cases": [], "baseline_known_defects": [
        {"kind": kind, "id": PYT_MKBUNDLE, "platform": "linux", "status": "fail" if kind == "ab" else "failed",
         "message": MARKER, "defect_id": "B1", "blocking": False},
    ]}
    unresolved, suppressed = driver.unresolved_steps(f"{step} 1\nstage1a_gate 0\n", approved, evidence_dir=ev)
    assert (not unresolved) is (not extra_failure)
    assert bool(suppressed) is (not extra_failure)


@pytest.mark.parametrize("fresh_ok,assert_same,expected", [(True, False, 0), (False, False, 1), (False, True, 0)])
def test_rejudge_returns_new_exit_and_classifications(monkeypatch, tmp_path, capsys, fresh_ok, assert_same, expected):
    _use(monkeypatch, tmp_path, [_pytest_entry()])
    ev = tmp_path / "evidence"
    _pytest_evidence(ev)
    (ev / "driver.log").write_text("GATE_ARGS=--l1-results " + str(ev / "l1") + "\n")
    (ev / "HEAD").write_text("a" * 40)
    original = {"overall": "FAIL", "errors": [], "cases": []}
    (ev / "stage1a-gate.json").write_text(json.dumps(original))
    (ev / "rc.txt").write_text("pytest_full_junit 1\nstage1a_gate 1\n")
    fresh = copy.deepcopy(original)
    if fresh_ok:
        fresh.update(overall="IMPLEMENTED", baseline_pending_classification=[], baseline_known_defects=[{
            "kind": "pytest", "id": PYT_MKBUNDLE, "platform": "linux", "status": "failed",
            "message": MARKER, "defect_id": "B1", "blocking": False,
        }])
    def run(command, **kwargs):
        Path(command[command.index("--out") + 1]).write_text(json.dumps(fresh))
        return SimpleNamespace(returncode=0 if fresh_ok else 1, stderr="")
    monkeypatch.setattr(driver.subprocess, "run", run)
    before = {p: p.read_bytes() for p in ev.rglob("*") if p.is_file()}
    code = driver.main(["--rejudge", str(ev)] + (["--assert-same-judgment"] if assert_same else []))
    payload = json.loads(capsys.readouterr().out)
    assert code == expected
    assert payload["same_judgment"] is (not fresh_ok)
    assert payload["failed_steps"] == (0 if fresh_ok else 2)
    assert payload["baseline_known_defects"] == fresh.get("baseline_known_defects", [])
    assert payload["baseline_pending_classification"] == []
    assert {p: p.read_bytes() for p in ev.rglob("*") if p.is_file()} == before


def test_rejudge_after_registration_uses_existing_gate_evidence(monkeypatch, tmp_path, capsys):
    """真正執行合成樹的 gate；登記後由 FAIL 轉為不擋，兩層出口一併重判。"""
    import shlex

    _use(monkeypatch, tmp_path, [])
    tree = GateTree(tmp_path / "repo")
    _fail_pytest_full(tree)
    code, original = tree.run()
    assert code == 1
    ev = tree.root / "out"
    (ev / "stage1a-gate.json").write_text(json.dumps(original))
    (ev / "pytest-full.xml").write_bytes((ev / "pytest-junit.xml").read_bytes())
    args = ["--l1-results", str(ev / "l1"), "--pytest-junit-report", str(ev / "pytest-full.xml"),
            "--ab-report", str(ev / "ab-report.json"), "--win-report", str(ev / "win-report.json"),
            "--windows-com", str(ev)]
    (ev / "driver.log").write_text("GATE_ARGS=" + shlex.join(args))
    (ev / "HEAD").write_text(tree.head)
    (ev / "rc.txt").write_text("pytest_full_junit 1\nstage1a_gate 1\nDONE\n")
    _use(monkeypatch, tmp_path, [_pytest_entry()])
    monkeypatch.setattr(driver, "_REPO_ROOT", tree.root)
    assert driver.main(["--rejudge", str(ev)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["rejudge_overall"] == "IMPLEMENTED"
    assert result["same_judgment"] is False
    assert result["failed_steps"] == 0
    assert result["suppressed"] == [{"step": "pytest_full_junit", "rc": 1}]
    assert result["baseline_known_defects"][0]["id"] == PYT_MKBUNDLE
    assert json.loads((ev / "stage1a-gate.json").read_text())["overall"] == "FAIL"
