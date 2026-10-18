"""Stage 1A gate 判定器（scripts/rust/stage1a_gate.py）契約（P3-T18）。

測試以暫存樹重建閘門的輸入（位置與 repo 相同：tests/acceptance/cases.yaml、
cases.lock.json、l1_required.lock.json、tests/contract_blackbox/data/coverage_1a.json、
tests/acceptance/l1_judge.py），複製閘門腳本到暫存樹後執行，不讀寫真實 repo 的鎖檔與證據。

輸入格式（本檔為閘門契約的 oracle，實作須與之相容）：
- cases.yaml：`{"schema_version":1,"cases":[{"case_id","layer","cli_stage",…}]}`；layer ∈ L1|L1.5|L2|L3。
- cases.lock.json：`{"schema_version":1,"sets":{"cli_1A":[…排序的 case_id…]}}`。
- coverage_1a.json：本檔以 repo 內實際檔案為底、覆寫證據欄位（rows 的 stub／e2e／l15、
  invariants 的 cases、p3_extra）。
- ab-report：ab_compare.py 的輸出（`result`、`cases[{id,class,…}]`）。
- --l1-results <dir>：`<dir>/<platform>/<blackbox|cargo>/resource-report.json`（l1-results.v1）；
  blackbox 目錄另有同目錄 junit.xml。
- case id 慣例：`<檔名>::<函式名>`（與計畫各表一致）。

紅燈：stage1a_gate.py 為 stub（exit 2、不產生報告），本檔測試以 strict xfail 標示。
前置（P2 Task 13 `tests/acceptance/l1_judge.py`，唯一判定實作）：本 worktree 尚無此檔；
閘門實作依計畫須載入它，未合併前 L1 相關判定無法通過，屬待補前置，不在本檔以斷言掩蓋。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
GATE = ROOT / "scripts" / "rust" / "stage1a_gate.py"
ACC = ROOT / "tests" / "acceptance"
COVERAGE_SRC = ROOT / "tests" / "contract_blackbox" / "data" / "coverage_1a.json"
pytestmark = [
    pytest.mark.core_portable,
    pytest.mark.process_rpc,
    pytest.mark.xfail(strict=True, raises=(NotImplementedError, AssertionError), reason="P3-T18 紅燈"),
]

A1 = "test_bb_rpc_params.py::test_rpc_params_wire"  # L1 黑箱案例（有證據）
A2 = "test_bb_event.py::test_event_subcommands_wire_params"  # L1 黑箱案例（缺 ab-report 證據時用）
Z = "test_real_uart.py::test_board_smoke"  # L2 案例（實機，BLOCKED-OWNER）

BB1 = "tests/contract_blackbox/test_bb_rpc_params.py::test_rpc_params_wire"
BB2 = "tests/contract_blackbox/test_bb_event.py::test_event_subcommands_wire_params"
DELEG = "tests/contract_blackbox/test_bb_delegation.py::test_never_searches_path"
CARGO1 = "crates/sw-cli/tests/delegate_route.rs::routes_native_groups"

BB_LINUX = {"argv": ["tests/contract_blackbox", "-m", "core_portable or posix_only"],
            "paths": ["tests/contract_blackbox"], "markexpr": "core_portable or posix_only"}
BB_WIN = {"argv": ["tests/contract_blackbox", "-m", "core_portable or windows_native"],
          "paths": ["tests/contract_blackbox"], "markexpr": "core_portable or windows_native"}
CARGO_LINUX = {"argv": ["--workspace", "--all-features"], "paths": [], "markexpr": ""}
CARGO_WIN = {"argv": ["--package", "sw-proto", "--package", "sw-pal", "--package", "sw-cli"],
             "paths": [], "markexpr": ""}


def _l1_lock() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "sets": {
            "s1a_blackbox": {
                "linux": {"selector": BB_LINUX, "nodeids": sorted([BB1, BB2, DELEG])},
                "windows": {"selector": BB_WIN, "nodeids": sorted([BB1, BB2, DELEG])},
            },
            "s1a_delegation": {
                "linux": {"selector": {**BB_LINUX, "paths": ["tests/contract_blackbox/test_bb_delegation.py"]},
                          "nodeids": [DELEG]},
                "windows": {"selector": {**BB_WIN, "paths": ["tests/contract_blackbox/test_bb_delegation.py"]},
                            "nodeids": [DELEG]},
            },
            "s1a_cargo": {
                "linux": {"selector": CARGO_LINUX, "nodeids": [CARGO1]},
                "windows": {"selector": CARGO_WIN, "nodeids": [CARGO1]},
            },
        },
    }


def _report(platform: str, *, runner: str, argv: list[str], markexpr: str, nodeids: list[str],
            outcomes: dict[str, str] | None = None, exit_code: int = 0, finished: bool = True) -> dict[str, Any]:
    outcomes = outcomes or {n: "passed" for n in nodeids}
    return {
        "schema_version": 2,
        "format": "l1-results.v1",
        "runner": runner,
        "platform": platform,
        "argv": argv,
        "markexpr": markexpr,
        "collected": list(nodeids),
        "deselected": [],
        "results": [
            {"nodeid": n, "outcome": o, "skip_kind": ("other" if o == "skipped" else None),
             "phases": {"setup": "passed", "call": "passed", "teardown": "passed"}}
            for n, o in outcomes.items()
        ],
        "exit_code": exit_code,
        "finished": finished,
        "core_portable_platform_skips": [],
        "deployed_content": None,
    }


def _pytest_argv(sel: dict[str, Any]) -> list[str]:
    return ["/usr/lib/python3/dist-packages/pytest/__main__.py", *sel["argv"], "--junitxml=/x/junit.xml"]


def _default_l1_reports() -> dict[tuple[str, str], dict[str, Any]]:
    lock = _l1_lock()["sets"]
    reports: dict[tuple[str, str], dict[str, Any]] = {}
    for platform, sel_bb, sel_cargo in (("linux", BB_LINUX, CARGO_LINUX), ("windows", BB_WIN, CARGO_WIN)):
        nodes = lock["s1a_blackbox"][platform]["nodeids"]
        reports[(platform, "blackbox")] = _report(
            platform, runner="pytest", argv=_pytest_argv(sel_bb), markexpr=sel_bb["markexpr"], nodeids=nodes)
        reports[(platform, "cargo")] = _report(
            platform, runner="cargo", argv=list(sel_cargo["argv"]), markexpr="", nodeids=[CARGO1])
    return reports


def _synthetic_coverage() -> dict[str, Any]:
    """以 repo 內的實際列為底，把所有證據指向 A1（有 ab-report 證據的 L1 案例）。"""
    cov = json.loads(COVERAGE_SRC.read_text(encoding="utf-8"))
    for row in cov["rows"]:
        row["stub"] = [A1]
        row["e2e"] = [A1]
        row["l15"] = []
    for inv in cov["invariants"]:
        inv["cases"] = [A1]
        inv["l15"] = []
    cov["p3_extra"] = [A1]
    return cov


class GateTree:
    """暫存 repo 樹；預設為「只缺 L2／L3」的完整情境（應判 IMPLEMENTED）。"""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.cases: list[dict[str, Any]] = [
            {"case_id": A1, "layer": "L1", "cli_stage": "1A"},
            {"case_id": Z, "layer": "L2", "cli_stage": "1A"},
        ]
        self.lock: dict[str, Any] | None = {"schema_version": 1, "cases_revision": "test",
                                            "sets": {"cli_1A": sorted([A1, Z])}}
        self.coverage: dict[str, Any] | None = _synthetic_coverage()
        self.l1_lock = _l1_lock()
        self.l1_reports = _default_l1_reports()
        self.ab_report: dict[str, Any] | None = {
            "result": "pass",
            "errors": [],
            "cases": [{"id": A1, "class": "ok", "a": "pass", "b": "pass", "reason": ""}],
        }
        self.win_report: dict[str, Any] | None = {"exe": "bin/serialwrap.exe", "passed": 3, "failed": 0,
                                                  "ignored": 0, "exit": 0}
        self.windows_com: str | None = None
        self.l15_report: dict[str, Any] | None = None

    def write(self) -> None:
        (self.root / "scripts" / "rust").mkdir(parents=True, exist_ok=True)
        shutil.copy2(GATE, self.root / "scripts" / "rust" / "stage1a_gate.py")
        acc = self.root / "tests" / "acceptance"
        acc.mkdir(parents=True, exist_ok=True)
        if (ACC / "l1_judge.py").exists():  # P2 Task 13 唯一判定實作；未合併時閘門的 L1 判定無法執行
            shutil.copy2(ACC / "l1_judge.py", acc / "l1_judge.py")
        (acc / "cases.yaml").write_text(yaml.safe_dump({"schema_version": 1, "cases": self.cases}, allow_unicode=True),
                                        encoding="utf-8")
        if self.lock is not None:
            (acc / "cases.lock.json").write_text(json.dumps(self.lock, ensure_ascii=False), encoding="utf-8")
        (acc / "l1_required.lock.json").write_text(json.dumps(self.l1_lock, ensure_ascii=False), encoding="utf-8")
        if self.coverage is not None:
            cdir = self.root / "tests" / "contract_blackbox" / "data"
            cdir.mkdir(parents=True, exist_ok=True)
            (cdir / "coverage_1a.json").write_text(json.dumps(self.coverage, ensure_ascii=False), encoding="utf-8")
        out = self.root / "out"
        out.mkdir(parents=True, exist_ok=True)
        for (platform, kind), rep in self.l1_reports.items():
            d = out / "l1" / platform / kind
            d.mkdir(parents=True, exist_ok=True)
            (d / "resource-report.json").write_text(json.dumps(rep, ensure_ascii=False), encoding="utf-8")
            if kind == "blackbox":
                n = len(rep["results"])
                (d / "junit.xml").write_text(
                    f'<testsuites><testsuite tests="{n}" failures="0" errors="0" skipped="0"/></testsuites>',
                    encoding="utf-8")
        if self.ab_report is not None:
            (out / "ab-report.json").write_text(json.dumps(self.ab_report, ensure_ascii=False), encoding="utf-8")
        if self.win_report is not None:
            (out / "win-report.json").write_text(json.dumps(self.win_report, ensure_ascii=False), encoding="utf-8")
        if self.l15_report is not None:
            (out / "l15-report.json").write_text(json.dumps(self.l15_report, ensure_ascii=False), encoding="utf-8")
        if self.windows_com is not None:
            (out / "windows-com").mkdir(parents=True, exist_ok=True)

    def run(self) -> tuple[int, dict[str, Any]]:
        self.write()
        out = self.root / "out"
        argv = [sys.executable, str(self.root / "scripts" / "rust" / "stage1a_gate.py"),
                "--l1-results", str(out / "l1"), "--out", str(out / "gate.json")]
        if self.ab_report is not None:
            argv += ["--ab-report", str(out / "ab-report.json")]
        if self.win_report is not None:
            argv += ["--win-report", str(out / "win-report.json")]
        if self.l15_report is not None:
            argv += ["--l15-report", str(out / "l15-report.json")]
        if self.windows_com is not None:
            argv += ["--windows-com", str(out / "windows-com")]
        r = subprocess.run(argv, capture_output=True, text=True, check=False)
        assert (out / "gate.json").exists(), f"閘門未產生報告（rc={r.returncode}，stderr={r.stderr.strip()!r}）"
        return r.returncode, json.loads((out / "gate.json").read_text(encoding="utf-8"))


@pytest.fixture
def tree(tmp_path: Path) -> GateTree:
    return GateTree(tmp_path / "repo")


def _l1_row(rep: dict[str, Any], set_id: str, platform: str) -> dict[str, Any]:
    rows = [r for r in rep["l1"] if r["set_id"] == set_id and r["platform"] == platform]
    assert len(rows) == 1, (set_id, platform, rep.get("l1"))
    return rows[0]


def _case_row(rep: dict[str, Any], case_id: str) -> dict[str, Any]:
    rows = [r for r in rep["cases"] if r["id"] == case_id]
    assert len(rows) == 1, (case_id, rep["cases"])
    return rows[0]


def test_full_fixture_only_l2_blocked_is_implemented(tree):
    code, rep = tree.run()
    assert code == 0
    assert rep["overall"] == "IMPLEMENTED"
    assert _case_row(rep, Z)["status"] == "BLOCKED-OWNER"
    assert _case_row(rep, A1)["status"] in ("ok", "PASS")


def test_cli_1a_case_missing_from_ab_report_fails(tree):
    tree.cases.append({"case_id": A2, "layer": "L1", "cli_stage": "1A"})
    tree.lock["sets"]["cli_1A"] = sorted([A1, A2, Z])
    code, rep = tree.run()
    assert code != 0
    assert rep["overall"] == "FAIL"
    assert _case_row(rep, A2)["status"] == "evidence-insufficient"


def test_missing_lock_file_fails(tree):
    tree.lock = None
    code, rep = tree.run()
    assert code != 0
    assert rep["overall"] == "FAIL"


def test_coverage_data_plane_stub_only_fails(tree):
    for row in tree.coverage["rows"]:
        if row["command"] == "device list":
            row["e2e"] = []
            row["l15"] = []
    code, rep = tree.run()
    assert code != 0
    assert rep["overall"] == "FAIL"
    assert "device list" in json.dumps(rep["coverage"], ensure_ascii=False)


def test_invariant_case_missing_fails(tree):
    tree.coverage["invariants"][0]["cases"] = ["test_missing.py::test_nope"]
    code, rep = tree.run()
    assert code != 0
    assert rep["overall"] == "FAIL"
    assert "test_missing.py::test_nope" in json.dumps(rep["invariants"], ensure_ascii=False)


def test_windows_com_missing_is_blocked_owner_not_verified(tree):
    code, rep = tree.run()
    assert rep["windows_com"] == "BLOCKED-OWNER"
    assert rep["overall"] != "VERIFIED"


def test_win_report_missing_is_insufficient(tree):
    tree.win_report = None
    code, rep = tree.run()
    assert code != 0
    assert rep["overall"] == "FAIL"
    assert rep.get("windows_l1") == "evidence-insufficient"


def test_l1_single_passing_case_is_insufficient(tree):
    tree.l1_reports[("linux", "blackbox")] = _report(
        "linux", runner="pytest", argv=_pytest_argv(BB_LINUX), markexpr=BB_LINUX["markexpr"],
        nodeids=[BB1])
    code, rep = tree.run()
    row = _l1_row(rep, "s1a_blackbox", "linux")
    assert row["verdict"] == "INSUFFICIENT"
    assert sorted(row["missing"]) == sorted([BB2, DELEG])
    assert rep["overall"] == "FAIL"


def test_l1_required_skip_is_insufficient(tree):
    nodes = [BB1, BB2, DELEG]
    tree.l1_reports[("linux", "blackbox")] = _report(
        "linux", runner="pytest", argv=_pytest_argv(BB_LINUX), markexpr=BB_LINUX["markexpr"], nodeids=nodes,
        outcomes={BB1: "passed", BB2: "skipped", DELEG: "passed"})
    code, rep = tree.run()
    row = _l1_row(rep, "s1a_blackbox", "linux")
    assert any(x["nodeid"] == BB2 for x in row["skipped_required"])
    assert rep["overall"] == "FAIL"


def test_l1_runner_exit_nonzero_is_insufficient(tree):
    tree.l1_reports[("linux", "blackbox")] = _report(
        "linux", runner="pytest", argv=_pytest_argv(BB_LINUX), markexpr=BB_LINUX["markexpr"],
        nodeids=[BB1, BB2, DELEG], exit_code=1)
    code, rep = tree.run()
    row = _l1_row(rep, "s1a_blackbox", "linux")
    assert row["verdict"] == "INSUFFICIENT"
    assert any("runner exit" in r for r in row["reasons"])
    assert rep["overall"] == "FAIL"


def test_l1_results_missing_is_insufficient(tree):
    tree.l1_reports.pop(("windows", "cargo"))
    code, rep = tree.run()
    row = _l1_row(rep, "s1a_cargo", "windows")
    assert row["verdict"] == "evidence-insufficient"
    assert rep["overall"] == "FAIL"


def test_l1_full_sets_pass(tree):
    code, rep = tree.run()
    assert rep["overall"] == "IMPLEMENTED"
    rows = rep["l1"]
    assert len(rows) == 6
    assert all(r["verdict"] == "PASS" for r in rows)


def test_l1_cargo_argv_not_matching_selector_invalid(tree):
    tree.l1_reports[("linux", "cargo")] = _report(
        "linux", runner="cargo", argv=["--workspace"], markexpr="", nodeids=[CARGO1])
    code, rep = tree.run()
    row = _l1_row(rep, "s1a_cargo", "linux")
    assert row["verdict"] == "evidence-insufficient"
    assert any("selector mismatch" in r for r in row["reasons"])
    assert rep["overall"] == "FAIL"


def test_l1_delegation_judged_from_blackbox_report(tree):
    tree.l1_reports.pop(("linux", "cargo"))
    code, rep = tree.run()
    assert _l1_row(rep, "s1a_cargo", "linux")["verdict"] == "evidence-insufficient"
    assert _l1_row(rep, "s1a_delegation", "linux")["verdict"] == "PASS"
    assert rep["overall"] == "FAIL"
