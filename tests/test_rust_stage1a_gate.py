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
- --l15-report <path>|<platform>=<path>：可重複指定；未帶前綴時視為 linux。
- --pytest-junit-report <path>：安全不變量引用的 test_rust_ pytest 節點必須在此 JUnit 中通過。
- case id 慣例：`<檔名>::<函式名>`（與計畫各表一致）。

本檔直接執行 `stage1a_gate.py`，以暫存樹驗證 CLI 參數、證據判定與 fail-closed 行為；L1 集合由 P2 Task 13 的 `tests/acceptance/l1_judge.py` 判定。
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
    tally = {"passed": 0, "failed": 0, "skipped": 0}
    for outcome in outcomes.values():
        if outcome in tally:
            tally[outcome] += 1
        elif outcome == "xfailed":
            tally["skipped"] += 1
        elif outcome == "xpassed":
            tally["failed"] += 1
    return {
        "schema_version": 2,
        "format": "l1-results.v1",
        "runner": runner,
        "platform": platform,
        "argv": argv,
        "markexpr": markexpr,
        "counts": {"all": tally},
        "resources": {},
        "uart_kind": "native",
        "not_collected_platform": [],
        "collected": list(nodeids),
        "deselected": [],
        "results": [
            {"nodeid": n, "outcome": o, "skip_kind": ("other" if o == "skipped" else None),
             "phases": {"setup": "passed", "call": ("skipped" if o in ("skipped", "xfailed") else
                                                           "failed" if o in ("failed", "error", "xpassed") else "passed"),
                        "teardown": "passed"}}
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
        self.l15_reports: list[tuple[str, dict[str, Any]]] = []
        self.pytest_junit_xml: str | None = None

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
        for index, (_, report) in enumerate(self.l15_reports):
            (out / f"l15-report-{index}.json").write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
        if self.pytest_junit_xml is not None:
            (out / "pytest-junit.xml").write_text(self.pytest_junit_xml, encoding="utf-8")
        if self.windows_com is not None:
            _write_windows_com_fixture(out, self.windows_com)

    def run(self, *, legacy_l15_report: bool = False) -> tuple[int, dict[str, Any]]:
        self.write()
        out = self.root / "out"
        argv = [sys.executable, str(self.root / "scripts" / "rust" / "stage1a_gate.py"),
                "--l1-results", str(out / "l1"), "--out", str(out / "gate.json")]
        if self.ab_report is not None:
            argv += ["--ab-report", str(out / "ab-report.json")]
        if self.win_report is not None:
            argv += ["--win-report", str(out / "win-report.json")]
        for index, (platform, _) in enumerate(self.l15_reports):
            report_path = str(out / f"l15-report-{index}.json")
            argv += ["--l15-report", report_path if legacy_l15_report else f"{platform}={report_path}"]
        if self.pytest_junit_xml is not None:
            argv += ["--pytest-junit-report", str(out / "pytest-junit.xml")]
        if self.windows_com is not None:
            argv += ["--windows-com", str(out)]
        r = subprocess.run(argv, capture_output=True, text=True, check=False)
        assert (out / "gate.json").exists(), f"閘門未產生報告（rc={r.returncode}，stderr={r.stderr.strip()!r}）"
        return r.returncode, json.loads((out / "gate.json").read_text(encoding="utf-8"))


@pytest.fixture
def tree(tmp_path: Path) -> GateTree:
    return GateTree(tmp_path / "repo")


_WIN_COM_P4 = "tests/contract_blackbox/test_cli_mgmt_daemon.py::test_autostart_l15_tx_once"
_WIN_COM_SEQ = ["python-daemon-start", "session-list-ready", "cmd-submit", "cmd-status-poll",
                "device-release", "session-list-released", "device-attach", "session-list-ready", "daemon-stop"]


def _write_windows_com_fixture(out: Path, mode: str) -> None:
    """依 win-blackbox.ps1 的 OutDir 佈局寫入 Windows COM 證據。

    mode：valid（完整通過）、forged（僅有 verdict／windows_com 後門欄位）、
    no-wire（缺 dut_stdin_write TX）、bad-order（序列步驟對調）。
    """
    if mode == "forged":
        for name in ("windows-com.json", "report.json", "com-selftest.json"):
            (out / name).write_text(json.dumps({"verdict": "PASS", "windows_com": "PASS"}), encoding="utf-8")
        return
    com = out / "windows-com"
    (com / "bridge-selftest").mkdir(parents=True, exist_ok=True)
    (com / "bridge").mkdir(parents=True, exist_ok=True)
    (com / "bridge-selftest" / "selftest-report.json").write_text(json.dumps({"verdict": "PASS"}), encoding="utf-8")
    names = list(_WIN_COM_SEQ)
    if mode == "bad-order":
        names[2], names[3] = names[3], names[2]
    steps = [{"name": "python-daemon-start", "pid": 1}] + [
        {"name": n, "returncode": 0} for n in names[1:]
    ]
    (com / "sequence-result.json").write_text(json.dumps({"result": "PASS", "steps": steps}), encoding="utf-8")
    import base64
    rows = [{"dir": "RX", "observation": "dut_stdout_read", "payload_b64": base64.b64encode(b"ok\n").decode()}]
    if mode != "no-wire":
        rows.append({"dir": "TX", "observation": "dut_stdin_write",
                     "payload_b64": base64.b64encode(b"echo hi\n").decode()})
    (com / "bridge" / "wire.ndjson").write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    doc = {
        "format": "windows-com-evidence.v1", "windows_com": "PASS", "com_pair": "COM20:COM21",
        "selftest": {"status": "PASS"}, "sequence": {"status": "PASS"},
        "bridge_stop": {"status": "PASS"},
        "p4_case": {"nodeid": _WIN_COM_P4, "outcome": "passed"},
    }
    (out / "windows-com.json").write_text(json.dumps(doc), encoding="utf-8")


def test_windows_com_valid_fixture_passes(tree):
    tree.windows_com = "valid"
    code, rep = tree.run()
    assert rep["windows_com"] == "PASS", rep["windows_com_reasons"]


@pytest.mark.parametrize("mode", ["forged", "no-wire", "bad-order"])
def test_windows_com_invalid_evidence_is_not_pass(tree, mode):
    tree.windows_com = mode
    code, rep = tree.run()
    assert rep["windows_com"] != "PASS"
    assert rep["overall"] != "VERIFIED"
    assert rep["windows_com_reasons"]


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


def test_slugged_contract_case_uses_source_ref(tree):
    cid = "bb-session-reaches-ready"
    source_ref = "tests/contract_blackbox/test_bb_daemon_e2e.py::test_session_reaches_ready"
    tree.cases.append({"case_id": cid, "layer": "L1", "cli_stage": "1A", "source_ref": source_ref})
    tree.lock["sets"]["cli_1A"] = sorted([A1, cid, Z])
    tree.ab_report["cases"].append({
        "id": "test_bb_daemon_e2e.py::test_session_reaches_ready",
        "case": "test_bb_daemon_e2e.py::test_session_reaches_ready",
        "class": "ok",
    })

    _, rep = tree.run()

    assert _case_row(rep, cid)["status"] == "ok"


def test_slugged_contract_case_rejects_any_bad_source_ref_row(tree):
    cid = "bb-session-reaches-ready"
    source_ref = "tests/contract_blackbox/test_bb_daemon_e2e.py::test_session_reaches_ready"
    tree.cases.append({"case_id": cid, "layer": "L1", "cli_stage": "1A", "source_ref": source_ref})
    tree.lock["sets"]["cli_1A"] = sorted([A1, cid, Z])
    for class_name in ("ok", "regression"):
        tree.ab_report["cases"].append({
            "id": f"report-{class_name}",
            "case": "test_bb_daemon_e2e.py::test_session_reaches_ready",
            "class": class_name,
        })

    _, rep = tree.run()

    assert _case_row(rep, cid)["status"] == "regression"


def test_slugged_contract_case_does_not_fall_back_when_source_ref_is_absent(tree):
    cid = "bb-session-reaches-ready"
    tree.cases.append({
        "case_id": cid,
        "layer": "L1",
        "cli_stage": "1A",
        "source_ref": "tests/contract_blackbox/test_bb_daemon_e2e.py::test_missing_case",
    })
    tree.lock["sets"]["cli_1A"] = sorted([A1, cid, Z])
    tree.ab_report["cases"].append({"id": cid, "class": "ok"})

    _, rep = tree.run()

    assert _case_row(rep, cid)["status"] == "evidence-insufficient"


def test_l15_cases_are_found_across_repeated_reports(tree):
    first = "l15-first-scenario"
    second = "l15-second-scenario"
    tree.cases.extend([
        {"case_id": first, "layer": "L1.5", "platforms": ["linux", "windows"]},
        {"case_id": second, "layer": "L1.5", "platforms": ["linux", "windows"]},
    ])
    tree.lock["sets"]["cli_1A"] = sorted([A1, first, second, Z])
    tree.l15_reports = [
        ("linux", {"results": [{"case_id": first, "verdict": "PASS", "gate_eligible": True}]}),
        ("linux", {"results": [{"case_id": second, "verdict": "PASS", "gate_eligible": True}]}),
    ]

    _, rep = tree.run()

    assert _case_row(rep, first)["status"] == "PASS"
    assert _case_row(rep, second)["status"] == "PASS"


def test_l15_case_requires_all_matching_rows_to_be_gate_eligible(tree):
    cid = "l15-eligible-everywhere"
    tree.cases.append({"case_id": cid, "layer": "L1.5", "platforms": ["linux", "windows"]})
    tree.lock["sets"]["cli_1A"] = sorted([A1, cid, Z])
    tree.l15_reports = [
        ("linux", {"results": [{"case_id": cid, "verdict": "PASS", "gate_eligible": False}]}),
        ("windows", {"results": [{"case_id": cid, "verdict": "PASS", "gate_eligible": True}]}),
    ]

    _, rep = tree.run()

    assert _case_row(rep, cid)["status"] == "evidence-insufficient"


def test_l15_case_fails_if_any_matching_row_fails(tree):
    cid = "l15-fails-anywhere"
    tree.cases.append({"case_id": cid, "layer": "L1.5", "platforms": ["linux", "windows"]})
    tree.lock["sets"]["cli_1A"] = sorted([A1, cid, Z])
    tree.l15_reports = [
        ("linux", {"results": [{"case_id": cid, "verdict": "FAIL", "gate_eligible": True}]}),
        ("windows", {"results": [{"case_id": cid, "verdict": "PASS", "gate_eligible": True}]}),
    ]

    _, rep = tree.run()

    assert _case_row(rep, cid)["status"] == "FAIL"


def test_windows_l15_case_cannot_use_linux_evidence(tree):
    cid = "win-com-open-exclusive"
    tree.cases.append({"case_id": cid, "layer": "L1.5", "platforms": ["windows"]})
    tree.lock["sets"]["cli_1A"] = sorted([A1, cid, Z])
    tree.l15_reports = [
        ("linux", {"results": [{"case_id": cid, "verdict": "PASS", "gate_eligible": True}]}),
    ]

    _, rep = tree.run()

    assert _case_row(rep, cid)["status"] == "evidence-insufficient"

    tree.l15_reports.append(
        ("windows", {"results": [{"case_id": cid, "verdict": "PASS", "gate_eligible": True}]})
    )
    _, rep = tree.run()

    assert _case_row(rep, cid)["status"] == "PASS"


def test_unprefixed_l15_report_path_is_linux_compatible(tree):
    cid = "l15-legacy-linux-report"
    tree.cases.append({"case_id": cid, "layer": "L1.5", "platforms": ["linux"]})
    tree.lock["sets"]["cli_1A"] = sorted([A1, cid, Z])
    tree.l15_reports = [
        ("linux", {"results": [{"case_id": cid, "verdict": "PASS", "gate_eligible": True}]})
    ]

    _, rep = tree.run(legacy_l15_report=True)

    assert _case_row(rep, cid)["status"] == "PASS"


def _set_evidence_insufficient_invariant_cases(tree, cases: list[str]) -> None:
    invariant = next(x for x in tree.coverage["invariants"] if x["id"] == "evidence-insufficient-never-pass")
    invariant["cases"] = cases


def test_evidence_insufficient_invariant_accepts_passing_pytest_junit(tree):
    cases = [
        "test_rust_ab_compare.py::test_classification",
        "test_rust_stage1a_gate.py::test_l1_single_passing_case_is_insufficient",
    ]
    _set_evidence_insufficient_invariant_cases(tree, cases)
    tree.pytest_junit_xml = (
        '<testsuites tests="2" failures="0" errors="0" skipped="0">'
        '<testsuite tests="2" failures="0" errors="0" skipped="0">'
        '<testcase classname="tests.test_rust_ab_compare" name="test_classification" />'
        '<testcase classname="tests.test_rust_stage1a_gate" '
        'name="test_l1_single_passing_case_is_insufficient" />'
        '</testsuite></testsuites>'
    )

    _, rep = tree.run()

    invariant = next(x for x in rep["invariants"] if x["id"] == "evidence-insufficient-never-pass")
    assert invariant["status"] == "PASS"
    assert invariant["missing_or_failed"] == []


def test_evidence_insufficient_invariant_fails_without_pytest_junit(tree):
    cases = ["test_rust_ab_compare.py::test_classification"]
    _set_evidence_insufficient_invariant_cases(tree, cases)

    _, rep = tree.run()

    invariant = next(x for x in rep["invariants"] if x["id"] == "evidence-insufficient-never-pass")
    assert invariant["status"] == "FAIL"
    assert invariant["missing_or_failed"] == cases
    assert rep["overall"] == "FAIL"


def test_evidence_insufficient_invariant_fails_on_pytest_junit_failure(tree):
    case = "test_rust_ab_compare.py::test_classification"
    _set_evidence_insufficient_invariant_cases(tree, [case])
    tree.pytest_junit_xml = (
        '<testsuites tests="1" failures="1" errors="0" skipped="0">'
        '<testsuite tests="1" failures="1" errors="0" skipped="0">'
        '<testcase classname="tests.test_rust_ab_compare" name="test_classification">'
        '<failure message="assertion failed" />'
        '</testcase></testsuite></testsuites>'
    )

    _, rep = tree.run()

    invariant = next(x for x in rep["invariants"] if x["id"] == "evidence-insufficient-never-pass")
    assert invariant["status"] == "FAIL"
    assert invariant["missing_or_failed"] == [case]


def test_evidence_insufficient_invariant_fails_when_junit_suite_has_other_error(tree):
    case = "test_rust_ab_compare.py::test_classification"
    _set_evidence_insufficient_invariant_cases(tree, [case])
    tree.pytest_junit_xml = (
        '<testsuites tests="2" failures="0" errors="1" skipped="0">'
        '<testsuite tests="2" failures="0" errors="1" skipped="0">'
        '<testcase classname="tests.test_rust_ab_compare" name="test_classification" />'
        '<testcase classname="tests.test_unrelated" name="test_other_case">'
        '<error message="fixture setup failed" />'
        '</testcase></testsuite></testsuites>'
    )

    _, rep = tree.run()

    invariant = next(x for x in rep["invariants"] if x["id"] == "evidence-insufficient-never-pass")
    assert invariant["status"] == "FAIL"
    assert invariant["missing_or_failed"] == [case]
