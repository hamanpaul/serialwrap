"""P7 Task 18（08-slice23-login-events.md）：`p7_ab_report.py` 的先前切片必要回歸判定與
`p7_case_coverage.py` 的 slice_2.3 涵蓋判定（合成 fixture，不讀「最新報告」）。

- 回歸階段：以預先凍結的必要集合逐節點核對——junit 任一 failure → FAIL；凍結節點缺漏／重複／skip、含凍結集合以外
  的節點（替代節點）、無凍結集合、rc 非 0 卻無失敗、缺 junit → INCOMPLETE；`blocked` 標記 → BLOCKED-UPSTREAM；皆不得
  PASS（審查 P7-cd7acf0-1）。func-test 階段：comparison verdict PASS 且 rc 0 才 PASS。
- 涵蓋：contract-blackbox 案例的必要節點取自凍結 A／B 集合，各恰一次 passed（rust_only SC 的 A 輪為 skip）；
  windows-native／realhw／F1～F6、F8～F12 regression → BLOCKED-OWNER＋B 編號；總表沒有 B 編號的 family → 缺漏；
  2.1／2.2 先前切片案例以匯入的 slice21／s22 gate 逐案證據判定（缺證據 → 缺漏）。
"""
from __future__ import annotations

import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts/rust"))
import p7_ab_report as ab  # noqa: E402
import p7_case_coverage as cov  # noqa: E402

pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]


def _junit(path: pathlib.Path, rows: dict[str, str]) -> None:
    """rows：{nodeid: passed|failed|skipped}。"""
    import xml.sax.saxutils as su
    out = []
    for nid, outcome in rows.items():
        file, name = nid.split("::", 1)
        body = {"failed": '<failure message="boom"/>', "skipped": '<skipped message="why"/>'}.get(outcome, "")
        out.append(f'<testcase classname="{su.escape(file[:-3].replace("/", "."))}" name="{su.escape(name)}">{body}</testcase>')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('<?xml version="1.0"?><testsuites><testsuite name="pytest">' + "".join(out)
                    + "</testsuite></testsuites>", encoding="utf-8")


def _phase(tmp: pathlib.Path, slug: str, rows: dict[str, str] | None, rc: int | None = 0) -> pathlib.Path:
    d = tmp / slug
    d.mkdir(parents=True, exist_ok=True)
    if rows is not None:
        _junit(d / "junit.xml", rows)
    if rc is not None:
        (d / "rc").write_text(f"{rc}\n", encoding="utf-8")
    return d


FROZEN = ["tests/a.py::t1", "tests/a.py::t2[shell]", "tests/a.py::t2[prpl]"]
ALL_PASS = {n: "passed" for n in FROZEN}


@pytest.mark.parametrize("rows,rc,frozen,want", [
    (ALL_PASS, 0, FROZEN, "PASS"),
    ({**ALL_PASS, "tests/a.py::t1": "failed"}, 1, FROZEN, "FAIL"),
    ({**ALL_PASS, "tests/a.py::t1": "skipped"}, 0, FROZEN, "INCOMPLETE"),
    ({}, 0, FROZEN, "INCOMPLETE"),
    (ALL_PASS, 2, FROZEN, "INCOMPLETE"),
    (None, 0, FROZEN, "INCOMPLETE"),
    # 審查 P7-cd7acf0-1 重播：只含無關節點、rc 0 → 不得 PASS
    ({"tests/unrelated.py::test_ok": "passed"}, 0, FROZEN, "INCOMPLETE"),
    # 參數化只跑 [shell]（缺 [prpl]）→ 不得 PASS
    ({"tests/a.py::t1": "passed", "tests/a.py::t2[shell]": "passed"}, 0, FROZEN, "INCOMPLETE"),
    # 凍結節點全過但另含替代／額外節點 → 不得 PASS
    ({**ALL_PASS, "tests/a.py::t3": "passed"}, 0, FROZEN, "INCOMPLETE"),
    # 鎖中無凍結集合 → 不得 PASS
    (ALL_PASS, 0, None, "INCOMPLETE"),
], ids=[
    # 前六個 id 沿用已凍結的 nodeid（審查 P7-f9e90a9-2：不得因改參數而讓凍結節點消失）
    "rows0-0-PASS", "rows1-1-FAIL", "rows2-0-INCOMPLETE", "rows3-0-INCOMPLETE", "rows4-2-INCOMPLETE",
    "None-0-INCOMPLETE",
    "unrelated-only", "param-missing", "extra-node", "no-frozen-set",
])
def test_regression_phase_status(tmp_path, rows, rc, frozen, want):
    assert ab.judge_phase(_phase(tmp_path, "slice21-b", rows, rc), frozen)[0] == want


def test_regression_phase_duplicate_node_is_not_pass(tmp_path):
    d = _phase(tmp_path, "slice21-b", ALL_PASS)
    xml = (d / "junit.xml").read_text(encoding="utf-8")
    dup = '<testcase classname="tests.a" name="t1"></testcase>'
    (d / "junit.xml").write_text(xml.replace("</testsuite>", dup + "</testsuite>"), encoding="utf-8")
    assert ab.judge_phase(d, FROZEN)[0] == "INCOMPLETE"


def test_regression_phase_blocked_upstream_is_not_pass(tmp_path):
    d = _phase(tmp_path, "slice22-b", None, None)
    (d / "blocked").write_text("BLOCKED-UPSTREAM：P6", encoding="utf-8")
    assert ab.judge_phase(d, FROZEN)[0] == "BLOCKED-UPSTREAM"


@pytest.mark.parametrize("verdict,rc,want", [("PASS", 0, "PASS"), ("FAIL", 1, "FAIL"), ("PASS", 3, "INCOMPLETE"),
                                             (None, 3, "INCOMPLETE"), (None, 1, "FAIL")])
def test_func_phase_status(tmp_path, verdict, rc, want):
    d = tmp_path / "func-x"
    (d / "report" / "x").mkdir(parents=True)
    (d / "rc").write_text(str(rc), encoding="utf-8")
    if verdict is not None:
        (d / "report" / "x" / "comparison.json").write_text(json.dumps({"verdict": verdict}), encoding="utf-8")
    assert ab.judge_func(d)[0] == want


def _l1_lock(tmp: pathlib.Path, sets: dict[str, list[str]]) -> pathlib.Path:
    lock = tmp / "l1.json"
    lock.write_text(json.dumps({"sets": {k: {"linux": {"nodeids": v}} for k, v in sets.items()}}), encoding="utf-8")
    return lock


def _report_args(tmp: pathlib.Path, regression_dir: pathlib.Path | None) -> list[str]:
    """A/B 本體全 PASS 的最小輸入（合成 collect-only＝空案例集合不影響回歸段）；凍結集合以合成 L1 鎖提供。"""
    cases = tmp / "cases.yaml"
    cases.write_text("cases: []\n", encoding="utf-8")
    lock = tmp / "lock.json"
    lock.write_text(json.dumps({"sets": {"slice_2.3": []}}), encoding="utf-8")
    ja, jb, col = tmp / "a.xml", tmp / "b.xml", tmp / "col.txt"
    _junit(ja, {"tests/x.py::t": "passed"})
    _junit(jb, {"tests/x.py::t": "passed"})
    col.write_text("tests/x.py::t\n", encoding="utf-8")
    args = ["--run-id", "r", "--junit-a", str(ja), "--junit-b", str(jb), "--collected", str(col), "--rc-a", "0",
            "--rc-b", "0", "--cases", str(cases), "--cases-lock", str(lock), "--out", str(tmp / "out.md"),
            "--l1-lock", str(_l1_lock(tmp, {ab.SET_A: ["tests/x.py::t"], ab.SET_B: ["tests/x.py::t"],
                                            **{s: ["tests/r.py::ok"] for s in ab.PHASE_SETS.values()}}))]
    if regression_dir is not None:
        args += ["--regression-dir", str(regression_dir)]
    return args


def test_report_exit_code_follows_regression_phases(tmp_path):
    assert ab.main(_report_args(tmp_path, None)) == 0
    reg = tmp_path / "run"
    for slug, _ in ab.REGRESSION_PHASES:
        _phase(reg, slug, {"tests/r.py::ok": "passed"})
    for c in ab.FUNC_CASES:
        d = reg / f"func-{c}"
        (d / "report").mkdir(parents=True)
        (d / "rc").write_text("0", encoding="utf-8")
        (d / "report" / "comparison.json").write_text(json.dumps({"verdict": "PASS"}), encoding="utf-8")
    assert ab.main(_report_args(tmp_path, reg)) == 0
    md = (tmp_path / "out.md").read_text(encoding="utf-8")
    assert "## 先前切片必要回歸（Rust artifact）" in md and "結論：PASS" in md
    _phase(reg, "slice21-b", {"tests/r.py::ok": "passed", "tests/r.py::bad": "failed"}, 1)
    assert ab.main(_report_args(tmp_path, reg)) == 1
    _phase(reg, "slice21-b", {"tests/r.py::ok": "passed"})
    (reg / "slice22-b" / "blocked").write_text("BLOCKED-UPSTREAM", encoding="utf-8")
    assert ab.main(_report_args(tmp_path, reg)) == 3


def _cov_fixture(tmp: pathlib.Path, cases: list[dict]) -> tuple[pathlib.Path, ...]:
    cy, lk, bc = tmp / "cases.yaml", tmp / "lock.json", tmp / "bc.yaml"
    import yaml
    cy.write_text(yaml.safe_dump({"cases": cases}), encoding="utf-8")
    lk.write_text(json.dumps({"sets": {"slice_2.3": [c["case_id"] for c in cases]}}), encoding="utf-8")
    bc.write_text(yaml.safe_dump({"changes": [{"id": "SC-30", "scope": "rust_only"}]}), encoding="utf-8")
    ref = "tests/contract_blackbox/test_p7_x.py::test_a"
    sc30 = "tests/contract_blackbox/test_p7_x.py::test_sc30"
    b = [ref + "[p1]", ref + "[p2]", sc30]
    l1 = _l1_lock(tmp, {"p7_gate:contract-blackbox-p7-b": b, "p7_gate:contract-blackbox-p7-a": b[:2]})
    return cy, lk, bc, l1


def test_coverage_requires_pass_or_numbered_owner_block(tmp_path):
    ref = "tests/contract_blackbox/test_p7_x.py::test_a"
    sc30 = "tests/contract_blackbox/test_p7_x.py::test_sc30"
    cases = [
        {"case_id": "p7-a", "suite": "contract-blackbox", "source_ref": ref, "behavior_changes": []},
        {"case_id": "p7-sc30", "suite": "contract-blackbox", "source_ref": sc30, "behavior_changes": ["SC-30"]},
        {"case_id": "win-x", "suite": "windows-native"},
        {"case_id": "p1-x", "suite": "realhw"},
        {"case_id": "f9-x", "suite": "regression", "family": "F9"},
        {"case_id": "f2-x", "suite": "regression", "family": "F2"},
        {"case_id": "f7-x", "suite": "regression", "family": "F7"},
        {"case_id": "l15-ctx", "suite": "l15-scenario", "daemon_slice": "2.2"},
    ]
    ab_dir = tmp_path / "ab"
    _junit(ab_dir / "p7-b" / "junit.xml", {ref + "[p1]": "passed", ref + "[p2]": "passed", sc30: "passed"})
    _junit(ab_dir / "p7-a" / "junit.xml", {ref + "[p1]": "passed", ref + "[p2]": "passed", sc30: "skipped"})
    rep = cov.build(ab_dir, *_cov_fixture(tmp_path, cases))
    got = {r["case_id"]: (r["status"], r["detail"]) for r in rep["rows"]}
    assert got["p7-a"][0] == "PASS" and got["p7-sc30"][0] == "PASS"
    assert got["win-x"] == ("BLOCKED-OWNER", "B-06") and got["p1-x"] == ("BLOCKED-OWNER", "B-07")
    assert got["f9-x"] == ("BLOCKED-OWNER", "B-02")
    # rulings/p7.md SPEC-GAP #4 (i)：先前切片 L2 回歸依 08 總表 B-09～B-16；總表沒有 B 編號的 family 仍為缺漏
    assert got["f2-x"] == ("BLOCKED-OWNER", "B-10")
    assert got["f7-x"][0] == "GAP" and got["l15-ctx"][0] == "GAP"
    assert rep["gaps"] == 2
    # B 輪任一節點非 passed、或 A 輪非登記 skip → 缺漏
    _junit(ab_dir / "p7-b" / "junit.xml", {ref + "[p1]": "passed", ref + "[p2]": "failed", sc30: "passed"})
    _junit(ab_dir / "p7-a" / "junit.xml", {ref + "[p1]": "passed", ref + "[p2]": "passed", sc30: "passed"})
    rep = cov.build(ab_dir, *_cov_fixture(tmp_path, cases))
    got = {r["case_id"]: r["status"] for r in rep["rows"]}
    assert got["p7-a"] == "GAP" and got["p7-sc30"] == "PASS"
    # 審查 P7-cd7acf0-1 重播：A／B 都只有 [p1]（缺凍結的 [p2]）→ 缺漏；重複節點 → 缺漏
    _junit(ab_dir / "p7-b" / "junit.xml", {ref + "[p1]": "passed", sc30: "passed"})
    _junit(ab_dir / "p7-a" / "junit.xml", {ref + "[p1]": "passed", sc30: "skipped"})
    rep = cov.build(ab_dir, *_cov_fixture(tmp_path, cases))
    assert {r["case_id"]: r["status"] for r in rep["rows"]}["p7-a"] == "GAP"
    xml = '<testcase classname="tests.contract_blackbox.test_p7_x" name="test_a[p1]"></testcase>'
    _junit(ab_dir / "p7-b" / "junit.xml", {ref + "[p1]": "passed", ref + "[p2]": "passed", sc30: "passed"})
    bx = ab_dir / "p7-b" / "junit.xml"
    bx.write_text(bx.read_text(encoding="utf-8").replace("</testsuite>", xml + "</testsuite>"), encoding="utf-8")
    _junit(ab_dir / "p7-a" / "junit.xml", {ref + "[p1]": "passed", ref + "[p2]": "passed", sc30: "skipped"})
    rep = cov.build(ab_dir, *_cov_fixture(tmp_path, cases))
    assert {r["case_id"]: r["status"] for r in rep["rows"]}["p7-a"] == "GAP"


def test_external_evidence_path_is_locatable(tmp_path, monkeypatch):
    """審查 P7-7bb9cfd-1：repo 外的證據不得只記 basename——`--run-dir` 內記 `$RUN_DIR/<相對>`，其外記完整路徑
    （去掉開頭 `/`，不留主機絕對路徑前綴，R-21）。"""
    run = tmp_path / "p7-run-1"
    (run / "sub").mkdir(parents=True)
    monkeypatch.setattr(ab, "RUN_DIR", run)
    assert ab._rel(run / "sub" / "a.xml") == "$RUN_DIR/sub/a.xml"
    other = tmp_path / "elsewhere" / "a.xml"
    assert ab._rel(other) == "$EXTERNAL/" + str(other.resolve()).lstrip("/")
    assert ab._rel(ROOT / "scripts/rust/p7_ab_report.py") == "scripts/rust/p7_ab_report.py"


def test_coverage_crate_sourced_case_uses_cargo_l1(tmp_path):
    """source_ref 在 crates/ 的案例以 gate 的 cargo L1 報告判定：恰一列 passed 才 PASS；缺報告、缺列、失敗皆缺漏。"""
    cases = [{"case_id": "df18-x", "suite": "contract-blackbox",
              "source_ref": "crates/sw-core/tests/event_store.rs::upsert_overwrites_existing_df18"}]
    nid = "cargo:sw-core::event_store::upsert_overwrites_existing_df18"
    fx = _cov_fixture(tmp_path, cases)
    rep_path = tmp_path / "cargo.json"
    for results, want in (([{"nodeid": nid, "outcome": "passed"}], "PASS"),
                          ([{"nodeid": nid, "outcome": "failed"}], "GAP"),
                          ([{"nodeid": nid, "outcome": "passed"}, {"nodeid": nid, "outcome": "passed"}], "GAP"),
                          ([], "GAP")):
        rep_path.write_text(json.dumps({"results": results}), encoding="utf-8")
        rep = cov.build(tmp_path / "ab", *fx, cargo_l1=rep_path)
        assert rep["rows"][0]["status"] == want, (results, rep["rows"][0])
    assert cov.build(tmp_path / "ab", *fx)["rows"][0]["status"] == "GAP"     # 未提供 cargo 報告


def test_report_header_run_dir_is_restorable(tmp_path):
    """審查 P7-7bb9cfd-1（第三輪）：repo 外 run 的表頭須記下可還原的 run_dir 基準（不得為 `$RUN_DIR/.`）。"""
    run = tmp_path / "ext-run"
    run.mkdir()
    args = _report_args(tmp_path, None) + ["--run-dir", str(run)]
    ab.main(args)
    md = (tmp_path / "out.md").read_text(encoding="utf-8")
    assert f"- run_dir: `$EXTERNAL/{str(run.resolve()).lstrip('/')}`" in md, md.splitlines()[:16]
    assert "$RUN_DIR/." not in md


def test_coverage_b_numbers_for_prior_slice_regression_families(tmp_path):
    """rulings/p7.md SPEC-GAP #4 (i)：F1～F6、F8、F12 依 08 BLOCKED-OWNER 總表 B-09～B-16 記 BLOCKED-OWNER。"""
    want = {"F1": "B-09", "F2": "B-10", "F3": "B-11", "F4": "B-12", "F5": "B-13", "F6": "B-14", "F8": "B-15", "F12": "B-16"}
    cases = [{"case_id": f"{fam.lower()}-x", "suite": "regression", "family": fam} for fam in want]
    rep = cov.build(tmp_path / "ab", *_cov_fixture(tmp_path, cases))
    assert {r["case_id"]: (r["status"], r["detail"]) for r in rep["rows"]} == {
        f"{fam.lower()}-x": ("BLOCKED-OWNER", bid) for fam, bid in want.items()}
    plan = (ROOT / "docs/rust-refactor/plans/08-slice23-login-events.md").read_text(encoding="utf-8")
    for fam, bid in want.items():
        assert f"| {bid} | L2 {fam} " in plan, (fam, bid)       # 編號須登記於 08 BLOCKED-OWNER 總表


def test_coverage_prior_slice_cases_use_imported_slice_gate_evidence(tmp_path):
    """rulings/p7.md SPEC-GAP #4 (ii)：2.1／2.2 先前切片案例不在本 gate 重跑，以 slice21 gate `case_verdicts.json`
    與 s22 gate 輸出逐案判定——須為 PASS；缺檔、缺列、非 PASS、重複列皆缺漏。本 gate 另有同案證據時也須通過。"""
    l15 = "tests/acceptance/l15/test_slice21_l15.py::test_case"
    cases = [
        {"case_id": "sm-01", "suite": "func-test", "daemon_slice": "2.1"},
        {"case_id": "l15-a", "suite": "l15-scenario", "daemon_slice": "2.1"},
        {"case_id": "l15-b", "suite": "l15-scenario", "daemon_slice": "2.1"},
        {"case_id": "bc02-x", "suite": "contract-blackbox", "daemon_slice": "2.2",
         "source_ref": "crates/sw-core/tests/deferred_bc02.rs::overflow_x"},
        {"case_id": "co-01", "suite": "func-test", "daemon_slice": "2.2"},
    ]
    cy, lk, bc, l1 = _cov_fixture(tmp_path, cases)
    lock = json.loads(l1.read_text(encoding="utf-8"))
    lock["sets"]["p7_gate:l15-slice21-b"] = {"linux": {"selector": {"argv": [], "markexpr": "", "paths": []},
                                                        "nodeids": [f"{l15}[rust-s21-l15-b]"]}}
    l1.write_text(json.dumps(lock), encoding="utf-8")
    ab_dir = tmp_path / "ab"
    _junit(ab_dir / "l15-slice21-b" / "junit.xml", {f"{l15}[rust-s21-l15-b]": "failed"})
    # 未提供匯入證據 → 全數缺漏（即使本 gate 自己的 l15 節點存在）
    rep = cov.build(ab_dir, cy, lk, bc, l1)
    assert {r["status"] for r in rep["rows"]} == {"GAP"} and rep["gaps"] == 5, rep["rows"]
    s21 = tmp_path / "case_verdicts.json"
    s21.write_text(json.dumps({"sm-01": "PASS", "l15-a": "EVIDENCE_MISSING", "l15-b": "PASS"}), encoding="utf-8")
    s22 = tmp_path / "s22.json"
    s22.write_text(json.dumps({"cases": [{"case_id": "bc02-x", "status": "PASS"}, {"case_id": "co-01", "status": "PASS"},
                                         {"case_id": "co-01", "status": "PASS"}], "result": "INSUFFICIENT"}), encoding="utf-8")
    cargo = tmp_path / "cargo.json"
    cargo.write_text(json.dumps({"results": [{"nodeid": "cargo:sw-core::deferred_bc02::overflow_x", "outcome": "failed"}]}),
                     encoding="utf-8")
    rep = cov.build(ab_dir, cy, lk, bc, l1, cargo_l1=cargo, slice21_verdicts=s21, slice22_gate=s22)
    got = {r["case_id"]: r["status"] for r in rep["rows"]}
    assert got == {"sm-01": "PASS", "l15-a": "GAP",          # 匯入非 PASS
                   "l15-b": "GAP",                           # 匯入 PASS 但本 gate 的同案 l15 節點 failed（取最差）
                   "bc02-x": "GAP",                          # 匯入 PASS 但本 gate 的 cargo 同名 nodeid failed
                   "co-01": "GAP"}, rep["rows"]              # s22 輸出同案重複列
    _junit(ab_dir / "l15-slice21-b" / "junit.xml", {f"{l15}[rust-s21-l15-b]": "passed"})
    cargo.write_text(json.dumps({"results": []}), encoding="utf-8")   # 本 gate 無同案 cargo 證據 → 只看匯入
    s22.write_text(json.dumps({"cases": [{"case_id": "bc02-x", "status": "PASS"}, {"case_id": "co-01", "status": "BLOCKED-OWNER"}]}),
                   encoding="utf-8")
    rep = cov.build(ab_dir, cy, lk, bc, l1, cargo_l1=cargo, slice21_verdicts=s21, slice22_gate=s22)
    got = {r["case_id"]: r["status"] for r in rep["rows"]}
    assert got == {"sm-01": "PASS", "l15-a": "GAP", "l15-b": "PASS", "bc02-x": "PASS", "co-01": "GAP"}, rep["rows"]
