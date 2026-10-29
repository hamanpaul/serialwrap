"""P7 Task 18（08-slice23-login-events.md）：`p7_ab_report.py` 的先前切片必要回歸判定與
`p7_case_coverage.py` 的 slice_2.3 涵蓋判定（合成 fixture，不讀「最新報告」）。

- 回歸階段：junit 任一 failure → FAIL；skip、無 testcase、rc 非 0 卻無失敗、缺 junit → INCOMPLETE；`blocked` 標記 →
  BLOCKED-UPSTREAM；皆不得 PASS。func-test 階段：comparison verdict PASS 且 rc 0 才 PASS。
- 涵蓋：contract-blackbox 案例需 B 全 passed 且 A passed（rust_only SC 的 A skip 例外）；windows-native／realhw／
  F9／F10／F11 regression → BLOCKED-OWNER＋B 編號；其他 regression family（無 B 編號）與 2.2 案例 → 缺漏。
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


@pytest.mark.parametrize("rows,rc,want", [
    ({"tests/a.py::t1": "passed", "tests/a.py::t2": "passed"}, 0, "PASS"),
    ({"tests/a.py::t1": "passed", "tests/a.py::t2": "failed"}, 1, "FAIL"),
    ({"tests/a.py::t1": "passed", "tests/a.py::t2": "skipped"}, 0, "INCOMPLETE"),
    ({}, 0, "INCOMPLETE"),
    ({"tests/a.py::t1": "passed"}, 2, "INCOMPLETE"),
    (None, 0, "INCOMPLETE"),
])
def test_regression_phase_status(tmp_path, rows, rc, want):
    assert ab.judge_phase(_phase(tmp_path, "slice21-b", rows, rc))[0] == want


def test_regression_phase_blocked_upstream_is_not_pass(tmp_path):
    d = _phase(tmp_path, "slice22-b", None, None)
    (d / "blocked").write_text("BLOCKED-UPSTREAM：P6", encoding="utf-8")
    assert ab.judge_phase(d)[0] == "BLOCKED-UPSTREAM"


@pytest.mark.parametrize("verdict,rc,want", [("PASS", 0, "PASS"), ("FAIL", 1, "FAIL"), ("PASS", 3, "INCOMPLETE"),
                                             (None, 3, "INCOMPLETE"), (None, 1, "FAIL")])
def test_func_phase_status(tmp_path, verdict, rc, want):
    d = tmp_path / "func-x"
    (d / "report" / "x").mkdir(parents=True)
    (d / "rc").write_text(str(rc), encoding="utf-8")
    if verdict is not None:
        (d / "report" / "x" / "comparison.json").write_text(json.dumps({"verdict": verdict}), encoding="utf-8")
    assert ab.judge_func(d)[0] == want


def _report_args(tmp: pathlib.Path, regression_dir: pathlib.Path | None) -> list[str]:
    """A/B 本體全 PASS 的最小輸入（合成 collect-only＝空案例集合不影響回歸段）。"""
    cases = tmp / "cases.yaml"
    cases.write_text("cases: []\n", encoding="utf-8")
    lock = tmp / "lock.json"
    lock.write_text(json.dumps({"sets": {"slice_2.3": []}}), encoding="utf-8")
    ja, jb, col = tmp / "a.xml", tmp / "b.xml", tmp / "col.txt"
    _junit(ja, {"tests/x.py::t": "passed"})
    _junit(jb, {"tests/x.py::t": "passed"})
    col.write_text("tests/x.py::t\n", encoding="utf-8")
    args = ["--run-id", "r", "--junit-a", str(ja), "--junit-b", str(jb), "--collected", str(col), "--rc-a", "0",
            "--rc-b", "0", "--cases", str(cases), "--cases-lock", str(lock), "--out", str(tmp / "out.md")]
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


def _cov_fixture(tmp: pathlib.Path, cases: list[dict]) -> tuple[pathlib.Path, pathlib.Path, pathlib.Path]:
    cy, lk, bc = tmp / "cases.yaml", tmp / "lock.json", tmp / "bc.yaml"
    import yaml
    cy.write_text(yaml.safe_dump({"cases": cases}), encoding="utf-8")
    lk.write_text(json.dumps({"sets": {"slice_2.3": [c["case_id"] for c in cases]}}), encoding="utf-8")
    bc.write_text(yaml.safe_dump({"changes": [{"id": "SC-30", "scope": "rust_only"}]}), encoding="utf-8")
    return cy, lk, bc


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
    assert got["f2-x"][0] == "GAP" and got["l15-ctx"][0] == "GAP"
    assert rep["gaps"] == 2
    # B 輪任一節點非 passed、或 A 輪非登記 skip → 缺漏
    _junit(ab_dir / "p7-b" / "junit.xml", {ref + "[p1]": "passed", ref + "[p2]": "failed", sc30: "passed"})
    _junit(ab_dir / "p7-a" / "junit.xml", {ref + "[p1]": "passed", ref + "[p2]": "passed", sc30: "passed"})
    rep = cov.build(ab_dir, *_cov_fixture(tmp_path, cases))
    got = {r["case_id"]: r["status"] for r in rep["rows"]}
    assert got["p7-a"] == "GAP" and got["p7-sc30"] == "PASS"
