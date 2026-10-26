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

# OD-17：mkbundle ignored cargo 列的替換登記（Stage 1 pytest 全套，Linux）。
PYT_MKBUNDLE = "tests/test_rust_mkbundle.py::test_mkbundle_respects_cargo_target_dir_override"
PYT_MANIFEST = "tests/test_rust_release_manifest_schema.py::test_manifest_schema_accepts_new_optional_fields"
PYT_FULL_SEL = {"argv": ["-q", "tests/", "-p", "junit_provenance"], "paths": ["tests/"], "markexpr": ""}


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
            "s1_pytest_full": {
                "linux": {"selector": PYT_FULL_SEL, "nodeids": sorted([PYT_MKBUNDLE, PYT_MANIFEST])},
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
            {"nodeid": n, "outcome": o,
             "skip_kind": (("capability" if n.rsplit("::", 1)[-1].startswith("cap_") else "other")
                           if o == "skipped" else None),
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


def _owner_nodeids(platform: str, argv: list[str]) -> list[str]:
    """selector 涵蓋且本平台會編譯進來的擁有者機台登記列（與 owner_gated.expected_names 同範圍）。"""
    sys.path.insert(0, str(ROOT / "scripts" / "rust"))
    try:
        import owner_gated
    finally:
        sys.path.pop(0)
    return [f"cargo:{crate}::t::{name}" for crate, name in owner_gated.expected_names(platform, argv)]


def _default_l1_reports() -> dict[tuple[str, str], dict[str, Any]]:
    lock = _l1_lock()["sets"]
    reports: dict[tuple[str, str], dict[str, Any]] = {}
    for platform, sel_bb, sel_cargo in (("linux", BB_LINUX, CARGO_LINUX), ("windows", BB_WIN, CARGO_WIN)):
        nodes = lock["s1a_blackbox"][platform]["nodeids"]
        reports[(platform, "blackbox")] = _report(
            platform, runner="pytest", argv=_pytest_argv(sel_bb), markexpr=sel_bb["markexpr"], nodeids=nodes)
        owner = _owner_nodeids(platform, list(sel_cargo["argv"]))
        reports[(platform, "cargo")] = _report(
            platform, runner="cargo", argv=list(sel_cargo["argv"]), markexpr="", nodeids=[CARGO1, *owner])
    reports[("linux", "pytest-full")] = _report(
        "linux", runner="pytest", argv=_pytest_argv(PYT_FULL_SEL), markexpr="",
        nodeids=sorted([PYT_MKBUNDLE, PYT_MANIFEST]))
    return reports


def _pytest_full_junit_xml(outcomes: dict[str, str] | None = None, *, subtests: int = 0) -> str:
    """與 s1_pytest_full 報告對應的 pytest JUnit（passed／failed／skipped 依 outcomes；預設兩筆皆 passed）。

    subtests：模擬 pytest 9 的 subtests——testsuite 的 tests 屬性多計這些數量，但不產生對應的 testcase 元素。
    """
    outcomes = outcomes or {PYT_MKBUNDLE: "passed", PYT_MANIFEST: "passed"}
    failed = sum(1 for o in outcomes.values() if o == "failed")
    skipped = sum(1 for o in outcomes.values() if o == "skipped")
    cases = []
    for nid, outcome in outcomes.items():
        classname, name = nid.split("::")
        cls = "tests." + classname.replace("/", ".").removesuffix(".py")
        body = {"failed": '<failure message="x"/>', "skipped": '<skipped message="x"/>'}.get(outcome, "")
        cases.append(f'<testcase classname="{cls}" name="{name}">{body}</testcase>')
    n = len(outcomes) + subtests
    attrs = f'tests="{n}" failures="{failed}" errors="0" skipped="{skipped}"'
    return f"<testsuites {attrs}><testsuite {attrs}>{''.join(cases)}</testsuite></testsuites>"


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


_PROVENANCE_ARGV = ["-q", "tests/", "-p", "junit_provenance", "--junitxml=/x/j.xml"]

_PROVENANCE_EFFECTIVE: dict[str, Any] = {
    "keyword": "", "markexpr": "", "deselect": [], "ignore": [], "ignore_glob": [],
    "maxfail": 0, "collectonly": False, "lf": False, "ff": False,
}


def _init_git_head(root: Path) -> str:
    """建立含一個 commit 的暫存 git repo，回傳 HEAD（gate 以 repo_root 的 HEAD 作為受測 revision）。"""
    root.mkdir(parents=True, exist_ok=True)
    git = ["git", "-c", "user.name=gate-test", "-c", "user.email=gate-test@invalid", "-c", "commit.gpgsign=false"]
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "init"], cwd=root, check=True)
    res = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True)
    return res.stdout.strip()


def _with_properties(xml: str, props: dict[str, str] | None) -> str:
    """把 pytest `record_testsuite_property` 形式的 `<properties>` 插入第一個 testsuite 開頭標籤之後。"""
    if props is None:
        return xml
    from html import escape
    block = "<properties>" + "".join(
        f'<property name="{escape(k)}" value="{escape(v)}"/>' for k, v in props.items()
    ) + "</properties>"
    start = xml.index("<testsuite ")
    end = xml.index(">", start) + 1
    return xml[:end] + block + xml[end:]


class GateTree:
    """暫存 repo 樹；預設為「只缺 L2／L3」的完整情境（應判 IMPLEMENTED）。"""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.head: str | None = None
        # None：依樹的 HEAD 與 PASS 所需的 provenance 自動帶入；dict：照原樣寫入（負向測試用）。
        self.junit_props: dict[str, str] | None = None
        # None：不提供 spike3 證據目錄；dict：寫入 <case_id>.json 並以 --spike3-evidence 傳入。
        self.spike3: dict[str, dict[str, Any]] | None = None
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
        # OD-15：Windows COM 為可執行 gate，預設為完整通過的證據（與「只缺 L2／L3」情境一致）；None＝不傳 --windows-com。
        self.windows_com: str | None = "valid"
        self.l15_reports: list[tuple[str, dict[str, Any]]] = []
        # 預設與 s1_pytest_full 報告同一次執行的 JUnit（provenance 由 write() 自動帶入）。
        self.pytest_junit_xml: str | None = _pytest_full_junit_xml()
        self.blackbox_junit_xml: str | None = None

    def default_junit_props(self) -> dict[str, str]:
        return {"HEAD": self.head or "", "SW_REQUIRE_CARGO": "1", "git_clean": "1",
                "effective": json.dumps(_PROVENANCE_EFFECTIVE, sort_keys=True), "argv": json.dumps(_PROVENANCE_ARGV)}

    def write(self) -> None:
        if self.head is None:
            self.head = _init_git_head(self.root)
        (self.root / "scripts" / "rust").mkdir(parents=True, exist_ok=True)
        shutil.copy2(GATE, self.root / "scripts" / "rust" / "stage1a_gate.py")
        shutil.copy2(ROOT / "scripts" / "rust" / "owner_gated.py", self.root / "scripts" / "rust" / "owner_gated.py")
        (self.root / "scripts" / "stage0").mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / "scripts" / "stage0" / "spike3_evidence.schema.json",
                     self.root / "scripts" / "stage0" / "spike3_evidence.schema.json")
        acc = self.root / "tests" / "acceptance"
        acc.mkdir(parents=True, exist_ok=True)
        if (ACC / "l1_judge.py").exists():  # P2 Task 13 唯一判定實作；未合併時閘門的 L1 判定無法執行
            shutil.copy2(ACC / "l1_judge.py", acc / "l1_judge.py")
        # junit 計數的唯一實作（P2 check_evidence_index._junit_counts）；閘門依路徑載入，缺檔即 fail-closed。
        shutil.copy2(ACC / "check_evidence_index.py", acc / "check_evidence_index.py")
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
            if kind == "cargo" and "provenance" not in rep:
                rep["provenance"] = {
                    "schema": "cargo-l1-provenance.v1",
                    "git": {
                        "head": self.head,
                        "clean": True,
                        "end": {"head": self.head, "clean": True},
                    },
                }
            d = out / "l1" / platform / kind
            d.mkdir(parents=True, exist_ok=True)
            (d / "resource-report.json").write_text(json.dumps(rep, ensure_ascii=False), encoding="utf-8")
            if kind == "blackbox":
                # 計數以 testcase 元素為準（P2 慣例）：每個結果列對應一個 testcase；屬性與元素數一致。
                n = len(rep["results"])
                cases = "".join(f'<testcase classname="tests.contract_blackbox" name="case{i}"/>' for i in range(n))
                (d / "junit.xml").write_text(
                    self.blackbox_junit_xml or
                    f'<testsuites><testsuite tests="{n}" failures="0" errors="0" skipped="0">{cases}</testsuite></testsuites>',
                    encoding="utf-8")
        if self.ab_report is not None:
            (out / "ab-report.json").write_text(json.dumps(self.ab_report, ensure_ascii=False), encoding="utf-8")
        if self.win_report is not None:
            (out / "win-report.json").write_text(json.dumps(self.win_report, ensure_ascii=False), encoding="utf-8")
        for index, (_, report) in enumerate(self.l15_reports):
            # F7：fixture 預設為完整報告（complete=true）；不完整報告的判定由 test_incomplete_l15_report_is_rejected 覆蓋。
            doc = {"complete": True, **report}
            (out / f"l15-report-{index}.json").write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        if self.pytest_junit_xml is not None:
            props = self.junit_props if self.junit_props is not None else self.default_junit_props()
            (out / "pytest-junit.xml").write_text(_with_properties(self.pytest_junit_xml, props), encoding="utf-8")
        if self.windows_com is not None:
            _write_windows_com_fixture(out, self.windows_com)
        if self.spike3 is not None:
            (out / "spike3").mkdir(parents=True, exist_ok=True)
            for cid, doc in self.spike3.items():
                (out / "spike3" / f"{cid}.json").write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")

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
        if self.spike3 is not None:
            argv += ["--spike3-evidence", str(out / "spike3")]
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
    no-wire（缺 dut_stdin_write TX）、bad-order（序列步驟對調）、
    absent（OutDir 內不寫任何 windows-com.json）、explicit-fail（doc 明示 windows_com: FAIL）。
    """
    if mode == "forged":
        for name in ("windows-com.json", "report.json", "com-selftest.json"):
            (out / name).write_text(json.dumps({"verdict": "PASS", "windows_com": "PASS"}), encoding="utf-8")
        return
    if mode == "absent":
        return
    if mode == "explicit-fail":
        (out / "windows-com.json").write_text(json.dumps({"windows_com": "FAIL"}), encoding="utf-8")
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
    # 依真機 win-blackbox.ps1 的佈局：sequence.json（頂層 result／steps）＋ sequence-result.json（wrapper 的 status／report）。
    sequence_doc = {
        "format": "windows-com-sequence.v1", "result": "PASS", "com_pair": "COM20:COM21",
        "serialwrap_com": "COM20", "bridge_com": "COM21", "steps": steps,
    }
    (com / "sequence.json").write_text(json.dumps(sequence_doc), encoding="utf-8")
    (com / "sequence-result.json").write_text(
        json.dumps({"status": "PASS", "exit": 0, "report": sequence_doc}), encoding="utf-8"
    )
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
def test_windows_com_invalid_evidence_is_insufficient_and_fails(tree, mode):
    """OD-15：不合格的 Windows COM 證據為證據不足（非 BLOCKED-OWNER），整體 FAIL。"""
    tree.windows_com = mode
    code, rep = tree.run()
    assert rep["windows_com"] == "evidence-insufficient", rep["windows_com_reasons"]
    assert rep["overall"] == "FAIL"
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


@pytest.mark.parametrize("mode", ["none", "absent"])
def test_windows_com_missing_is_evidence_insufficient_and_fails(tree, mode):
    """OD-15：Windows COM 為可執行 gate。未帶 --windows-com（none）或目錄內無 windows-com.json（absent）
    皆為證據不足（不得 BLOCKED-OWNER、不得 IMPLEMENTED），整體 FAIL。"""
    tree.windows_com = None if mode == "none" else "absent"
    code, rep = tree.run()
    assert rep["windows_com"] == "evidence-insufficient", rep["windows_com_reasons"]
    assert rep["windows_com_reasons"]
    assert rep["overall"] == "FAIL"
    assert code != 0


def test_windows_com_explicit_fail_stays_fail(tree):
    """明示 FAIL 維持 FAIL（不降為證據不足）。"""
    tree.windows_com = "explicit-fail"
    code, rep = tree.run()
    assert rep["windows_com"] == "FAIL"
    assert rep["overall"] == "FAIL"
    assert code != 0


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
    # 六列（s1a_blackbox／s1a_delegation／s1a_cargo × 平台）＋ s1_pytest_full（linux，OD-17 替換登記）
    assert len(rows) == 7
    assert all(r["verdict"] == "PASS" for r in rows)


def test_l1_blackbox_inconsistent_suite_junit_is_not_pass(tree):
    """黑箱 JUnit 宣告失敗數高於 testcase 層時，不能因非 strict 集合而忽略解析錯誤。"""
    tree.blackbox_junit_xml = (
        '<testsuites><testsuite tests="3" failures="1" errors="0" skipped="0">'
        '<testcase classname="tests.contract_blackbox" name="case0"/>'
        '<testcase classname="tests.contract_blackbox" name="case1"/>'
        '<testcase classname="tests.contract_blackbox" name="case2"/>'
        '</testsuite></testsuites>'
    )
    code, rep = tree.run()
    row = _l1_row(rep, "s1a_blackbox", "linux")
    assert row["verdict"] == "evidence-insufficient", row
    assert any("JUnit" in reason and "failures=1" in reason for reason in row["reasons"]), row["reasons"]
    assert rep["overall"] == "FAIL"


def _pytest_full_row(rep: dict[str, Any]) -> dict[str, Any]:
    return _l1_row(rep, "s1_pytest_full", "linux")


def test_pytest_full_missing_report_is_evidence_insufficient(tree):
    tree.l1_reports.pop(("linux", "pytest-full"))
    code, rep = tree.run()
    row = _pytest_full_row(rep)
    assert row["verdict"] == "evidence-insufficient", row
    assert rep["overall"] == "FAIL"


def test_pytest_full_required_skip_is_not_pass(tree):
    outcomes = {PYT_MKBUNDLE: "skipped", PYT_MANIFEST: "passed"}
    tree.l1_reports[("linux", "pytest-full")] = _report(
        "linux", runner="pytest", argv=_pytest_argv(PYT_FULL_SEL), markexpr="",
        nodeids=sorted([PYT_MKBUNDLE, PYT_MANIFEST]), outcomes=outcomes)
    tree.pytest_junit_xml = _pytest_full_junit_xml(outcomes)
    code, rep = tree.run()
    row = _pytest_full_row(rep)
    assert row["verdict"] == "evidence-insufficient", row
    assert any(x["nodeid"] == PYT_MKBUNDLE for x in row["skipped_required"]), row
    assert rep["overall"] == "FAIL"


def test_pytest_full_selector_mismatch_is_not_pass(tree):
    tree.l1_reports[("linux", "pytest-full")] = _report(
        "linux", runner="pytest", argv=_pytest_argv({"argv": ["-q", "tests/test_rust_mkbundle.py"]}),
        markexpr="", nodeids=sorted([PYT_MKBUNDLE, PYT_MANIFEST]))
    code, rep = tree.run()
    row = _pytest_full_row(rep)
    assert row["verdict"] == "evidence-insufficient", row
    assert any("selector mismatch" in r for r in row["reasons"]), row["reasons"]
    assert rep["overall"] == "FAIL"


def test_pytest_full_without_junit_is_not_pass(tree):
    tree.pytest_junit_xml = None
    code, rep = tree.run()
    row = _pytest_full_row(rep)
    assert row["verdict"] == "evidence-insufficient", row
    assert rep["overall"] == "FAIL"


def test_pytest_full_failed_required_is_fail(tree):
    outcomes = {PYT_MKBUNDLE: "failed", PYT_MANIFEST: "passed"}
    tree.l1_reports[("linux", "pytest-full")] = _report(
        "linux", runner="pytest", argv=_pytest_argv(PYT_FULL_SEL), markexpr="",
        nodeids=sorted([PYT_MKBUNDLE, PYT_MANIFEST]), outcomes=outcomes)
    tree.pytest_junit_xml = _pytest_full_junit_xml(outcomes)
    code, rep = tree.run()
    row = _pytest_full_row(rep)
    assert row["verdict"] == "FAIL", row
    assert rep["overall"] == "FAIL"


def test_pytest_full_junit_with_matching_totals_but_missing_required_nodeid_is_not_pass(tree):
    """計數相同的過期 JUnit（必要 nodeid 缺席、以他案補足筆數）不得與報告湊成 PASS（逐列對帳）。"""
    outcomes = {PYT_MKBUNDLE: "passed", "tests/test_unrelated.py::test_unrelated": "passed"}
    tree.pytest_junit_xml = _pytest_full_junit_xml(outcomes)
    code, rep = tree.run()
    row = _pytest_full_row(rep)
    assert row["verdict"] == "evidence-insufficient", row
    assert any(PYT_MANIFEST in r for r in row["reasons"]), row["reasons"]
    assert rep["overall"] == "FAIL"


def test_pytest_full_junit_required_nodeid_not_passed_is_not_pass(tree):
    """必要 nodeid 在 JUnit 中為 skipped → 不得 PASS（由 judge 的 totals 對帳先行攔下，逐列對帳亦不放行）。"""
    outcomes = {PYT_MKBUNDLE: "passed", PYT_MANIFEST: "skipped"}
    tree.pytest_junit_xml = _pytest_full_junit_xml(outcomes)
    code, rep = tree.run()
    row = _pytest_full_row(rep)
    assert row["verdict"] == "evidence-insufficient", row
    assert row["reasons"], row
    assert rep["overall"] == "FAIL"


def test_pytest_full_set_missing_from_lock_is_evidence_insufficient(tree):
    tree.l1_lock["sets"].pop("s1_pytest_full")
    code, rep = tree.run()
    row = _pytest_full_row(rep)
    assert row["verdict"] == "evidence-insufficient", row
    assert rep["overall"] == "FAIL"


def test_pytest_full_complete_evidence_is_pass(tree):
    code, rep = tree.run()
    row = _pytest_full_row(rep)
    assert row["verdict"] == "PASS", row
    assert rep["overall"] == "IMPLEMENTED"


PYT_SUBTEST_NODE = "tests/test_rust_subtests_fixture.py::test_parametrized_with_subtests"


def _pytest_full_three_nodes(tree) -> list[str]:
    """s1_pytest_full 凍結 3 個 nodeid（含一個以 subtests 執行的節點），報告同為 3 列。"""
    nodes = sorted([PYT_MKBUNDLE, PYT_MANIFEST, PYT_SUBTEST_NODE])
    tree.l1_lock["sets"]["s1_pytest_full"]["linux"]["nodeids"] = nodes
    tree.l1_reports[("linux", "pytest-full")] = _report(
        "linux", runner="pytest", argv=_pytest_argv(PYT_FULL_SEL), markexpr="", nodeids=nodes)
    return nodes


def test_pytest_full_subtests_in_tests_attr_are_not_junit_mismatch(tree):
    """G4-junit-subtest-count-mismatch：testsuite tests=6、testcase 元素 3 個（subtests 計入屬性、不產生 testcase），
    報告 3 列 → 計數以 testcase 元素為準（P2 check_evidence_index 慣例），不得出現 junit mismatch，逐列 PASS。"""
    nodes = _pytest_full_three_nodes(tree)
    tree.pytest_junit_xml = _pytest_full_junit_xml({n: "passed" for n in nodes}, subtests=3)
    code, rep = tree.run()
    row = _pytest_full_row(rep)
    assert not any("junit/l1-results mismatch" in r for r in row["reasons"]), row["reasons"]
    assert row["verdict"] == "PASS", row
    assert rep["overall"] == "IMPLEMENTED"


def test_pytest_full_testcase_count_mismatch_is_still_evidence_insufficient(tree):
    """真正不一致不得因改用 testcase 計數而放行：屬性 tests=3（與報告 3 列相符）但 testcase 元素僅 2 個 → 判證據不足。"""
    _pytest_full_three_nodes(tree)
    tree.pytest_junit_xml = _pytest_full_junit_xml({PYT_MKBUNDLE: "passed", PYT_MANIFEST: "passed"}, subtests=1)
    code, rep = tree.run()
    row = _pytest_full_row(rep)
    assert row["verdict"] == "evidence-insufficient", row
    assert any("junit/l1-results mismatch" in r for r in row["reasons"]), row["reasons"]
    assert rep["overall"] == "FAIL"


def _p2_junit_counter():
    """依路徑載入 P2 `check_evidence_index._junit_counts`（與閘門同一實作）。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location("p2_check_evidence_index_for_test", ACC / "check_evidence_index.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module._junit_counts


def test_junit_totals_counts_testcase_elements_not_suite_attributes(tmp_path):
    """黑箱與 pytest-full 共用的計數：tests 屬性含 subtests（6）但 testcase 僅 3 個 → 合計以 testcase 為準。"""
    gate = _load_gate_module()
    path = tmp_path / "junit.xml"
    path.write_text(
        '<testsuites><testsuite tests="6" failures="0" errors="0" skipped="1">'
        '<testcase classname="tests.a" name="t1"/>'
        '<testcase classname="tests.a" name="t2"><failure message="x"/></testcase>'
        '<testcase classname="tests.a" name="t3"><skipped message="x"/></testcase>'
        '</testsuite></testsuites>', encoding="utf-8")
    assert gate._junit_totals(path, _p2_junit_counter()) == {"tests": 3, "failures": 1, "errors": 0, "skipped": 1}


def test_junit_totals_without_counter_or_file_is_none(tmp_path):
    """計數函式未載入（P2 缺席）或檔案缺漏／不可解析 → None（閘門 fail-closed，不得以屬性放行）。"""
    gate = _load_gate_module()
    junit = tmp_path / "junit.xml"
    junit.write_text('<testsuites><testsuite tests="1" failures="0" errors="0" skipped="0"/></testsuites>', encoding="utf-8")
    assert gate._junit_totals(junit, None) is None
    assert gate._junit_totals(tmp_path / "missing.xml", _p2_junit_counter()) is None
    bad = tmp_path / "bad.xml"
    bad.write_text("<testsuites>", encoding="utf-8")
    assert gate._junit_totals(bad, _p2_junit_counter()) is None


def test_l1_cargo_argv_not_matching_selector_invalid(tree):
    tree.l1_reports[("linux", "cargo")] = _report(
        "linux", runner="cargo", argv=["--workspace"], markexpr="", nodeids=[CARGO1])
    code, rep = tree.run()
    row = _l1_row(rep, "s1a_cargo", "linux")
    assert row["verdict"] == "evidence-insufficient"
    assert any("selector mismatch" in r for r in row["reasons"])
    assert rep["overall"] == "FAIL"


def test_l1_cargo_provenance_head_and_clean_tree_are_required(tree):
    """Stage 1A cargo 報告必須綁定受測 HEAD，且執行前後工作樹皆乾淨。"""
    tree.write()
    report = tree.l1_reports[("linux", "cargo")]
    for mutation in (
        lambda git: git.__setitem__("head", "0" * 40),
        lambda git: git.__setitem__("clean", False),
        lambda git: git["end"].__setitem__("head", "0" * 40),
        lambda git: git["end"].__setitem__("clean", False),
    ):
        report["provenance"] = {
            "schema": "cargo-l1-provenance.v1",
            "git": {"head": tree.head, "clean": True,
                    "end": {"head": tree.head, "clean": True}},
        }
        mutation(report["provenance"]["git"])
        _, gate_report = tree.run()
        row = _l1_row(gate_report, "s1a_cargo", "linux")
        assert row["verdict"] == "evidence-insufficient", row
        assert gate_report["overall"] == "FAIL"


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
    # 宣告 linux+windows 的案例須兩平台各有列（Linux 列不得替代 Windows）；本測試重點仍是跨多份報告找列。
    tree.l15_reports = [
        ("linux", {"results": [{"case_id": first, "verdict": "PASS", "gate_eligible": True}]}),
        ("linux", {"results": [{"case_id": second, "verdict": "PASS", "gate_eligible": True}]}),
        ("windows", {"results": [
            {"case_id": first, "verdict": "PASS", "gate_eligible": True},
            {"case_id": second, "verdict": "PASS", "gate_eligible": True},
        ]}),
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


def test_l15_case_declared_on_windows_is_not_satisfied_by_linux_rows(tree):
    """宣告 linux+windows 的 L1.5 案例：只有 Linux 列（Windows 報告缺席）→ evidence-insufficient，不得 PASS。"""
    cid = "l15-needs-windows-producer"
    tree.cases.append({"case_id": cid, "layer": "L1.5", "platforms": ["linux", "windows"]})
    tree.lock["sets"]["cli_1A"] = sorted([A1, cid, Z])
    tree.l15_reports = [
        ("linux", {"results": [{"case_id": cid, "verdict": "PASS", "gate_eligible": True}]}),
    ]

    _, rep = tree.run()

    assert _case_row(rep, cid)["status"] == "evidence-insufficient"


def test_l15_status_requires_each_declared_platform_row():
    """單元層：platforms 宣告時每個平台都要有列；未宣告時行為不變（既有 PASS 判定保留）。"""
    gate = _load_gate_module()
    lin = {"case_id": "c", "verdict": "PASS", "gate_eligible": True, "platform": "linux"}
    win = {"case_id": "c", "verdict": "PASS", "gate_eligible": True, "platform": "windows"}
    assert gate._l15_case_status([dict(lin)], ["linux", "windows"]) == "evidence-insufficient"
    assert gate._l15_case_status([dict(lin), dict(win)], ["linux", "windows"]) == "PASS"
    assert gate._l15_case_status([dict(lin)]) == "PASS"


def test_unprefixed_l15_report_path_is_linux_compatible(tree):
    cid = "l15-legacy-linux-report"
    tree.cases.append({"case_id": cid, "layer": "L1.5", "platforms": ["linux"]})
    tree.lock["sets"]["cli_1A"] = sorted([A1, cid, Z])
    tree.l15_reports = [
        ("linux", {"results": [{"case_id": cid, "verdict": "PASS", "gate_eligible": True}]})
    ]

    _, rep = tree.run(legacy_l15_report=True)

    assert _case_row(rep, cid)["status"] == "PASS"


def test_incomplete_l15_report_is_rejected(tree):
    """F7：每案中途寫出的部分報告（complete=false）不得作為 L1.5 證據；gate 須判證據不足並記錄錯誤。"""
    cid = "l15-incomplete-report"
    tree.cases.append({"case_id": cid, "layer": "L1.5", "platforms": ["linux"]})
    tree.lock["sets"]["cli_1A"] = sorted([A1, cid, Z])
    tree.l15_reports = [
        ("linux", {"complete": False, "results": [{"case_id": cid, "verdict": "PASS", "gate_eligible": True}]}),
    ]

    rc, rep = tree.run()

    assert rc != 0
    assert _case_row(rep, cid)["status"] != "PASS"
    assert any("complete" in str(e) for e in rep["errors"])


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
    tree.pytest_junit_xml = None  # 不提供 --pytest-junit-report（預設 fixture 會帶入 s1_pytest_full 的 JUnit）

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



# ---------------------------------------------------------------------------
# S1X-gate-accounting：擁有者機台相依列（cap_*／登記列）不得靜默消失（BLOCKED-OWNER）；
# pytest JUnit 必須帶 provenance（HEAD＝受測 revision、SW_REQUIRE_CARGO=1、argv 涵蓋 tests/）；
# spike3 證據依 schema 與 git_head 判定，未知 EDR 不得 PASS。
# ---------------------------------------------------------------------------

LINUX_OWNER_OUTCOMES = {
    "cargo:sw-cli::mgmt_layout::windows_switch_running_exe_rename": "passed",
    "cargo:sw-daemon::launcher::cap_windows_native__launcher_gui_subsystem": "passed",
    "cargo:sw-daemon::launcher::cap_windows_native__launcher_parent_kill_kills_child": "passed",
    "cargo:sw-pal::exec_delegate::cap_interactive_desktop__exec_or_wait_ctrl_c": "passed",
    "cargo:sw-pal::service_ctl::cap_supervisor_runtime__systemd_hold_blocks_restart_on_failure": "passed",
}
GUI = "cargo:sw-daemon::launcher::cap_windows_native__launcher_gui_subsystem"


def _cargo_report(platform: str, owner: dict[str, str], *, omit: tuple[str, ...] = ()) -> dict[str, Any]:
    """cargo 報告：CARGO1 必為 passed；owner 為登記列的結果；omit 內的 nodeid 整列自報告中移除（不收集）。"""
    argv = list(CARGO_LINUX["argv"]) if platform == "linux" else list(CARGO_WIN["argv"])
    outcomes = {CARGO1: "passed", **owner}
    for nid in omit:
        outcomes.pop(nid, None)
    return _report(platform, runner="cargo", argv=argv, markexpr="", nodeids=list(outcomes), outcomes=outcomes)


def test_owner_gated_row_absent_from_lock_is_blocked_owner_not_verified(tree):
    """鎖內不含 cap 列、報告以 skipped 呈現 → 列為 BLOCKED-OWNER，整體不得 VERIFIED（不得因不在鎖內而消失）。"""
    owner = dict(LINUX_OWNER_OUTCOMES, **{GUI: "skipped"})
    tree.l1_reports[("linux", "cargo")] = _cargo_report("linux", owner)
    code, rep = tree.run()
    row = _l1_row(rep, "s1a_cargo", "linux")
    assert row["verdict"] == "BLOCKED-OWNER", row
    assert GUI in json.dumps(rep["owner_gated"], ensure_ascii=False)
    assert rep["overall"] == "IMPLEMENTED", rep["overall"]


def test_owner_gated_expected_row_absent_from_report_is_insufficient(tree):
    """登記列在 selector 涵蓋範圍內且本平台會編譯，卻在報告中完全缺席 → INSUFFICIENT，整體 FAIL。"""
    tree.l1_reports[("linux", "cargo")] = _cargo_report("linux", LINUX_OWNER_OUTCOMES, omit=(GUI,))
    code, rep = tree.run()
    row = _l1_row(rep, "s1a_cargo", "linux")
    assert row["verdict"] == "evidence-insufficient", row
    assert any("launcher_gui_subsystem" in r for r in row["reasons"]), row["reasons"]
    assert rep["overall"] == "FAIL"


def test_owner_gated_locked_skip_maps_to_blocked_owner_not_fail(tree):
    """過渡期：cap 列仍在鎖內且為 skipped → judge 的 INSUFFICIENT 僅由擁有者列造成時，列為 BLOCKED-OWNER（非 PASS）。"""
    tree.l1_lock["sets"]["s1a_cargo"]["linux"]["nodeids"] = sorted([CARGO1, GUI])
    tree.l1_reports[("linux", "cargo")] = _cargo_report("linux", dict(LINUX_OWNER_OUTCOMES, **{GUI: "skipped"}))
    code, rep = tree.run()
    row = _l1_row(rep, "s1a_cargo", "linux")
    assert row["verdict"] == "BLOCKED-OWNER", row
    assert rep["overall"] == "IMPLEMENTED", rep["overall"]


def test_owner_gated_skip_is_not_hidden_by_non_owner_gap(tree):
    """必要的非擁有者案例 CARGO1 缺席，同時 cap 列 skipped → 仍 FAIL（擁有者列不得掩蓋其他缺口）。"""
    tree.l1_lock["sets"]["s1a_cargo"]["linux"]["nodeids"] = sorted([CARGO1, GUI])
    report = _cargo_report("linux", dict(LINUX_OWNER_OUTCOMES, **{GUI: "skipped"}), omit=(CARGO1,))
    tree.l1_reports[("linux", "cargo")] = report
    code, rep = tree.run()
    row = _l1_row(rep, "s1a_cargo", "linux")
    assert row["verdict"] != "BLOCKED-OWNER", row
    assert rep["overall"] == "FAIL"


def _junit_case_xml(case_id: str, *, failure: bool = False) -> str:
    classname, name = case_id.split("::")
    cls = classname.replace("/", ".").removesuffix(".py")
    body = '<failure message="x"/>' if failure else ""
    n = 1 if failure else 0
    return (
        f'<testsuites tests="1" failures="{n}" errors="0" skipped="0">'
        f'<testsuite tests="1" failures="{n}" errors="0" skipped="0">'
        f'<testcase classname="tests.{cls}" name="{name}">{body}</testcase>'
        "</testsuite></testsuites>"
    )


_PROVENANCE_CASE = "test_rust_ab_compare.py::test_classification"


def _provenance_invariant(tree: GateTree, props: dict[str, str] | None) -> dict[str, Any]:
    """以單一案例的 pytest JUnit 驅動安全不變量；props 為 None 時自動帶入正確 provenance。"""
    if tree.head is None:
        tree.head = _init_git_head(tree.root)
    _set_evidence_insufficient_invariant_cases(tree, [_PROVENANCE_CASE])
    tree.pytest_junit_xml = _junit_case_xml(_PROVENANCE_CASE)
    tree.junit_props = props
    _, rep = tree.run()
    return next(x for x in rep["invariants"] if x["id"] == "evidence-insufficient-never-pass")


def test_pytest_junit_with_full_provenance_is_accepted(tree):
    inv = _provenance_invariant(tree, None)
    assert inv["status"] == "PASS", inv


def test_pytest_junit_without_provenance_is_rejected(tree):
    inv = _provenance_invariant(tree, {})
    assert inv["status"] == "FAIL", inv


def test_pytest_junit_head_mismatch_is_rejected(tree):
    _provenance_invariant(tree, None)  # 先取得 tree.head
    inv = _provenance_invariant(tree, {**tree.default_junit_props(), "HEAD": "0" * 40})
    assert inv["status"] == "FAIL"
    assert inv["missing_or_failed"] == [_PROVENANCE_CASE]


def _eff(**changes: Any) -> str:
    return json.dumps({**_PROVENANCE_EFFECTIVE, **changes}, sort_keys=True)


@pytest.mark.parametrize("override", [
    {"SW_REQUIRE_CARGO": "0"},
    {"SW_REQUIRE_CARGO": None},
    {"HEAD": None},
    {"argv": json.dumps(["-q", "tests/test_rust_stage1a_gate.py", "-p", "junit_provenance"])},
    {"argv": json.dumps(["-q", "tests/", "-k", "rust"])},
    {"argv": json.dumps(["-q", "tests/", "-m", "core_portable"])},
    {"argv": json.dumps(["-q", "tests/", "--deselect", "tests/x.py::y"])},
    {"argv": "not-json"},
    # 髒樹：同 HEAD 但工作樹有未提交／未追蹤變更，不得作為正式證據
    {"git_clean": "0"},
    {"git_clean": None},
    # 生效設定縮小範圍（argv 看不出，例如 addopts／PYTEST_ADDOPTS 或合併短旗標 -xq）
    {"effective": _eff(keyword="rust")},
    {"effective": _eff(markexpr="core_portable")},
    {"effective": _eff(deselect=["tests/x.py::y"])},
    {"effective": _eff(ignore=["tests/acceptance"])},
    {"effective": _eff(maxfail=1)},
    {"effective": _eff(collectonly=True)},
    {"effective": "not-json"},
    {"effective": None},
], ids=["require-cargo-0", "require-cargo-missing", "head-missing", "argv-not-tests-tree", "argv-k",
        "argv-m", "argv-deselect", "argv-unparseable", "dirty-tree", "git-clean-missing",
        "effective-keyword-via-addopts", "effective-markexpr", "effective-deselect", "effective-ignore",
        "effective-maxfail-via-xq", "effective-collectonly", "effective-unparseable", "effective-missing"])
def test_pytest_junit_provenance_violations_rejected(tree, override):
    _provenance_invariant(tree, None)  # 先取得 tree.head
    base = tree.default_junit_props()
    props = {k: v for k, v in {**base, **override}.items() if v is not None}
    inv = _provenance_invariant(tree, props)
    assert inv["status"] == "FAIL", (override, inv)


SP = "win-wal-av-rotate"  # spike3 產物 scripts/stage0/spike3_win_wal.py 的 case_id（L1.5、cli_1A）
SP_CASES = (
    "win-wal-av-rotate",
    "win-wal-av-unlink",
    "win-wal-av-reader-handle",
    "win-wal-f12-progress",
)


def _enable_spike3_case(tree: GateTree, case_id: str = SP, *, mutate=None, **doc_kwargs: Any) -> dict[str, Any]:
    """啟用一個 Spike 3 案例（L1.5、cli_1A）；證據的 git_head 預設為受測 HEAD。"""
    tree.head = _init_git_head(tree.root)
    tree.cases.append({"case_id": case_id, "layer": "L1.5", "cli_stage": "1A"})
    tree.lock["sets"]["cli_1A"] = sorted([A1, Z, case_id])
    doc = _spike3_doc(tree.head, case_id=case_id, **doc_kwargs)
    if mutate is not None:
        mutate(doc)
    tree.spike3 = {case_id: doc}
    return doc


def _enable_spike3(tree: GateTree, *, mutate=None, **doc_kwargs: Any) -> dict[str, Any]:
    """維持既有 rotate 測試 helper 的呼叫介面。"""
    return _enable_spike3_case(tree, SP, mutate=mutate, **doc_kwargs)


def _spike3_observations(case_id: str) -> dict[str, Any]:
    """各 Spike 3 案例的有效觀察替身，避免只用自報 verdict 走過 gate。"""
    append_count = {
        "win-wal-av-rotate": 400,
        "win-wal-av-unlink": 11,
        "win-wal-av-reader-handle": 401,
        "win-wal-f12-progress": 403,
    }[case_id]
    common = {"append_count": append_count, "append_latency_ms": [1.0] * append_count}
    if case_id == "win-wal-av-rotate":
        return {
            **common,
            "archives": 1, "records": 400, "continuity_meta_count": 0,
            "expected_records": 400, "first_seq": 1, "last_seq": 400,
            "seq_continuous": True, "rotate_retry": 1, "deferred_delete_pending": 0,
            "interference_observed": True,
            "drain": {"tick_interval_s": 1.0, "ticks": 1, "drained": True},
        }
    if case_id == "win-wal-av-unlink":
        return {
            **common,
            "unlink_attempts": 2, "sharing_violation_attempts": 1,
            "interference_observed": True, "unlinked": True, "append_attempts_until_meta": 1,
            "meta_count": 1, "loss_flag": True, "range_known": True,
            "potentially_lost_seq_range": [1, 2],
        }
    if case_id == "win-wal-av-reader-handle":
        return {
            **common,
            "rotation_failed_seen": True, "appended": 401, "records": 401,
            "continuity_meta_count": 0, "first_seq": 1, "last_seq": 401,
            "seq_continuous": True, "write_failures": 0, "deferred_delete_pending": 0,
            "interference_observed": True,
            "drain": {"tick_interval_s": 1.0, "ticks": 1, "drained": True},
        }
    if case_id == "win-wal-f12-progress":
        return {
            **common,
            "archives": 1, "records": 403, "continuity_meta_count": 0,
            "first_seq": 1, "last_seq": 403, "seq_continuous": True,
            "rotate_retry": 1, "deferred_delete_pending": 0,
            "pre_rotate": {"ok": True, "reason_code": "none", "diagnostics": {}},
            "post_rotate": {"ok": True, "reason_code": "none", "diagnostics": {}},
            "crc_errors": [], "interference_observed": True,
            "drain": {"tick_interval_s": 1.0, "ticks": 1, "drained": True},
        }
    raise AssertionError(f"unknown Spike 3 case: {case_id}")


def _spike3_doc(git_head: str, *, verdict: str = "PASS", product: str = "Vendor EDR",
                status: str = "enabled", case_id: str = SP, drop_integrity: bool = False,
                drop_av: bool = False, unchanged: bool = True, launch_refused: bool = False,
                oplock: str = "granted", grabs: Any = 3, launch_error: str = "none",
                subjects: Any = None, sha_after: str | None = None) -> dict[str, Any]:
    """spike3 證據替身（OD-19 形態）：edr 為選填資訊欄；av_sim 與 binary_integrity 為必填。

    oplock 預設 granted 僅為替身，用來走通 PASS 路徑（gate 只接受 granted／denied／unavailable）；
    真實值必須由原生 Windows 執行產生，不得以替身代替證據。
    """
    if subjects is None:
        subjects = [{"path": "python.exe", "exists_before": True, "exists_after": True,
                     "sha256_before": "c" * 64, "sha256_after": sha_after or "c" * 64, "unchanged": unchanged}]
    doc: dict[str, Any] = {
        "case_id": case_id,
        "run_at": "2026-10-22T00:00:00Z",
        "env": {"os": "Windows 10"},
        "artifact": {"binary": "sw_core (Python 基線)", "revision": "a" * 40, "tree_sha256": "b" * 64,
                     "git_head": git_head, "git_clean": True},
        "steps": [{"op": "case", "ts": "2026-10-22T00:00:01Z", "result": "ok"}],
        "observations": _spike3_observations(case_id),
        "av_sim": {"mode": "share_no_delete", "share_mode": "FILE_SHARE_READ|FILE_SHARE_WRITE",
                   "hold_ms_range": [10, 500], "seed": 20261023, "oplock": "not_requested",
                   "launch_error": launch_error, "grabs": grabs, "open_errors": 0, "helper_exit": 0,
                   "variants": {"oplock": {
                       "mode": "oplock",
                       "share_mode": "FILE_SHARE_READ|FILE_SHARE_WRITE|FILE_SHARE_DELETE",
                       "hold_ms_range": [10, 500], "seed": 20261023, "oplock": oplock,
                       "launch_error": "none", "grabs": 1, "open_errors": 0, "helper_exit": 0,
                   }}},
        "binary_integrity": {
            "subjects": subjects,
            "unchanged": unchanged, "launch_refused": launch_refused, "launch_error": "none",
        },
        "edr": {"product": product, "status": status, "interceptions": "unknown"},
        "verdict": verdict,
        "fallback": "none",
    }
    if drop_integrity:
        del doc["binary_integrity"]
    if drop_av:
        del doc["av_sim"]
    return doc


def test_spike3_dirty_tree_evidence_is_insufficient_never_pass(tree):
    """spike3 證據產出於髒樹（artifact.git_clean 非 true）即使 HEAD 相符也不得 PASS。"""
    _enable_spike3(tree, mutate=lambda doc: doc["artifact"].__setitem__("git_clean", False))
    code, rep = tree.run()
    assert _case_row(rep, SP)["status"] != "PASS", _case_row(rep, SP)


def test_spike3_pass_with_known_edr_is_pass(tree):
    """EDR 欄位不影響判定；有效延遲樣本仍因 owner 未裁決上限而 BLOCKED-OWNER。"""
    _enable_spike3(tree)
    code, rep = tree.run()
    assert _case_row(rep, SP)["status"] == "BLOCKED-OWNER", _case_row(rep, SP)
    assert any("PENDING-OD-S1X-AVSIM-LATENCY" in reason for reason in _case_row(rep, SP)["evidence"]["reasons"])
    assert not any("EDR" in reason or "edr" in reason for reason in _case_row(rep, SP)["evidence"]["reasons"])
    assert rep["overall"] == "IMPLEMENTED"
    assert code == 0


@pytest.mark.parametrize("bound_mode,expected", [("above", "PASS"), ("below", "FAIL")])
def test_spike3_latency_bound_pass_and_fail_paths(tmp_path, monkeypatch, bound_mode, expected):
    """owner 設定上限後，最大 append 樣本分別走 PASS 與超限 FAIL 分支。"""
    gate = _load_gate_module()
    head = "a" * 40
    doc = _spike3_doc(head)
    samples = doc["observations"]["append_latency_ms"]
    observed_max = max(samples)
    bound = observed_max + 1.0 if bound_mode == "above" else observed_max / 2.0
    evidence_dir = tmp_path / "spike3"
    evidence_dir.mkdir()
    (evidence_dir / f"{SP}.json").write_text(json.dumps(doc), encoding="utf-8")
    monkeypatch.setattr(gate, "OD19_AVSIM_WRITE_LATENCY_BOUND_MS", bound)

    status, reasons = gate._spike3_status(
        SP, evidence_dir, ROOT / "scripts" / "stage0" / "spike3_evidence.schema.json", head,
    )

    assert status == expected, reasons
    if expected == "FAIL":
        assert any("超過 owner 上限" in reason for reason in reasons)


def test_spike3_unknown_edr_is_blocked_owner_never_pass(tree):
    """OD-19 (3)：未知 EDR 不影響結果；此列只因尚無 owner latency 上限而 BLOCKED-OWNER。"""
    _enable_spike3(tree, product="unknown", status="unknown")
    code, rep = tree.run()
    assert _case_row(rep, SP)["status"] == "BLOCKED-OWNER", _case_row(rep, SP)
    assert any("PENDING-OD-S1X-AVSIM-LATENCY" in reason for reason in _case_row(rep, SP)["evidence"]["reasons"])
    assert not any("EDR" in reason or "edr" in reason for reason in _case_row(rep, SP)["evidence"]["reasons"])
    assert rep["overall"] == "IMPLEMENTED"
    assert code == 0


def test_spike3_unknown_edr_is_info_only_and_does_not_block_pass(tree):
    """保留已登記 nodeid；未知 EDR 不參與判定，上限未裁決仍明確 BLOCKED-OWNER。"""
    _enable_spike3(tree, product="unknown", status="unknown")
    code, rep = tree.run()
    row = _case_row(rep, SP)
    assert row["status"] == "BLOCKED-OWNER", row
    assert any("PENDING-OD-S1X-AVSIM-LATENCY" in reason for reason in row["evidence"]["reasons"])
    assert not any("EDR" in reason or "edr" in reason for reason in row["evidence"]["reasons"])
    assert rep["overall"] == "IMPLEMENTED"
    assert code == 0


def test_spike3_missing_append_latency_measurements_is_insufficient(tree):
    """OD-19 (2)：缺少實際 append 延遲樣本時，不得用其他 WAL 結果代替延遲證據。"""
    _enable_spike3(tree, mutate=lambda doc: doc["observations"].pop("append_latency_ms"))
    code, rep = tree.run()
    assert _case_row(rep, SP)["status"] == "evidence-insufficient", _case_row(rep, SP)
    assert any("append 延遲" in reason or "schema" in reason for reason in _case_row(rep, SP)["evidence"]["reasons"])
    assert rep["overall"] == "FAIL"


def test_spike3_owner_latency_bound_not_decided_cannot_pass(tree):
    """OD-19 (2)：有量測值但 owner 尚未核准上限時，延遲驗收仍須保持 BLOCKED-OWNER。"""
    def set_valid_latency_samples(doc: dict[str, Any]) -> None:
        doc["observations"]["append_latency_ms"] = [2.0, *([1.0] * 399)]

    _enable_spike3(tree, mutate=set_valid_latency_samples)
    code, rep = tree.run()
    assert _case_row(rep, SP)["status"] == "BLOCKED-OWNER", _case_row(rep, SP)
    assert any("PENDING-OD-S1X-AVSIM-LATENCY" in reason for reason in _case_row(rep, SP)["evidence"]["reasons"])
    assert rep["overall"] == "IMPLEMENTED"


def test_spike3_invalid_append_latency_measurement_is_insufficient(tree):
    """OD-19 (2)：非數值、負數或非有限值不能充當 append 延遲證據。"""
    _enable_spike3(tree, mutate=lambda doc: doc["observations"].update({"append_latency_ms": [1.0, "fast"]}))
    code, rep = tree.run()
    assert _case_row(rep, SP)["status"] == "evidence-insufficient", _case_row(rep, SP)
    assert rep["overall"] == "FAIL"


def test_spike3_nonzero_helper_exit_is_insufficient_never_owner_pending(tree):
    """helper 有非零退出碼時證據不足；不可只靠先前 grabs 和自報 PASS 進入 owner pending。"""
    _enable_spike3(tree, mutate=lambda doc: doc["av_sim"].__setitem__("helper_exit", 1))
    code, rep = tree.run()
    row = _case_row(rep, SP)
    assert row["status"] == "evidence-insufficient", row
    assert any("helper" in reason.lower() for reason in row["evidence"]["reasons"])
    assert rep["overall"] == "FAIL"


@pytest.mark.parametrize(
    ("case_id", "field"),
    [
        ("win-wal-av-rotate", "seq_continuous"),
        ("win-wal-av-unlink", "meta_count"),
        ("win-wal-av-unlink", "sharing_violation_attempts"),
        ("win-wal-av-unlink", "interference_observed"),
        ("win-wal-av-reader-handle", "write_failures"),
        ("win-wal-f12-progress", "post_rotate"),
        ("win-wal-f12-progress", "rotate_retry"),
        ("win-wal-f12-progress", "records"),
    ],
)
def test_spike3_each_case_requires_its_own_observations(tree, case_id: str, field: str):
    """四個 Spike 3 case 均須提交能支撐 PASS 的案例專屬觀察，不得以 verdict 欄位代替。"""
    def remove_required_observation(doc: dict[str, Any]) -> None:
        doc["observations"].pop(field)

    _enable_spike3_case(tree, case_id, mutate=remove_required_observation)
    code, rep = tree.run()
    row = _case_row(rep, case_id)
    assert row["status"] == "evidence-insufficient", row
    assert rep["overall"] == "FAIL"


def test_spike3_rotate_counts_continuity_meta_without_calling_it_data_loss(tree):
    """tick 完成延後輪替時會新增連續性 META；仍須保留 400 筆資料 append 與完整 seq。"""
    def add_continuity_meta(doc: dict[str, Any]) -> None:
        obs = doc["observations"]
        obs.update({"records": 401, "continuity_meta_count": 1, "last_seq": 401})

    _enable_spike3(tree, mutate=add_continuity_meta)
    code, rep = tree.run()
    assert _case_row(rep, SP)["status"] == "BLOCKED-OWNER", _case_row(rep, SP)
    assert rep["overall"] == "IMPLEMENTED"
    assert code == 0


def test_spike3_f12_requires_proven_rotate_and_complete_sequence(tree):
    """F12 marker 成功不能替代跨輪替資料完整性與實際輪替干擾證據。"""
    def remove_rotation_evidence(doc: dict[str, Any]) -> None:
        obs = doc["observations"]
        obs.pop("rotate_retry")
        obs["last_seq"] = 402

    _enable_spike3_case(tree, "win-wal-f12-progress", mutate=remove_rotation_evidence)
    code, rep = tree.run()
    assert _case_row(rep, "win-wal-f12-progress")["status"] == "evidence-insufficient"
    assert rep["overall"] == "FAIL"
    assert code == 1


def test_spike3_unlink_without_observed_sharing_violation_is_insufficient(tree):
    """外部 unlink 未遇到 FILE_SHARE_DELETE 衝突時，即使 helper 抓到檔案也不能算干擾驗收。"""
    def no_sharing_violation(doc: dict[str, Any]) -> None:
        doc["observations"].update({"sharing_violation_attempts": 0, "interference_observed": False})

    _enable_spike3_case(tree, "win-wal-av-unlink", mutate=no_sharing_violation)
    code, rep = tree.run()
    assert _case_row(rep, "win-wal-av-unlink")["status"] == "evidence-insufficient"
    assert rep["overall"] == "FAIL"
    assert code == 1


def test_spike3_binary_changed_is_insufficient_never_pass(tree):
    """OD-19 (3)：測試前後執行檔 sha256 不一致即不得 PASS。"""
    _enable_spike3(tree, unchanged=False)
    code, rep = tree.run()
    assert _case_row(rep, SP)["status"] == "evidence-insufficient", _case_row(rep, SP)
    assert rep["overall"] == "FAIL"


def test_spike3_launch_refused_is_insufficient_never_pass(tree):
    """OD-19 (3)：helper 或執行檔啟動被拒（launch_refused）即不得 PASS。"""
    _enable_spike3(tree, launch_refused=True)
    code, rep = tree.run()
    assert _case_row(rep, SP)["status"] == "evidence-insufficient", _case_row(rep, SP)
    assert rep["overall"] == "FAIL"


def test_spike3_zero_grabs_is_insufficient_never_pass(tree):
    """OD-19 (2)：模擬防毒未實際佔用任何檔案（grabs=0）即不得 PASS（僅有自報 verdict 不足）。"""
    _enable_spike3(tree, grabs=0)
    code, rep = tree.run()
    assert _case_row(rep, SP)["status"] == "evidence-insufficient", _case_row(rep, SP)
    assert rep["overall"] == "FAIL"


def test_spike3_empty_subjects_is_insufficient_never_pass(tree):
    """OD-19 (3)：binary_integrity.subjects 為空＝無前後比對證據，不得以 unchanged=true 過閘（schema minItems）。"""
    _enable_spike3(tree, subjects=[])
    code, rep = tree.run()
    assert _case_row(rep, SP)["status"] == "evidence-insufficient", _case_row(rep, SP)
    assert rep["overall"] == "FAIL"


def test_spike3_sha_mismatch_with_unchanged_true_is_insufficient(tree):
    """OD-19 (3)：sha256 前後不一致但自報 unchanged=true，gate 逐 subject 重算判定，仍不得 PASS。"""
    subjects = [{"path": "python.exe", "exists_before": True, "exists_after": True,
                 "sha256_before": "c" * 64, "sha256_after": "d" * 64, "unchanged": True}]
    _enable_spike3(tree, subjects=subjects, sha_after="d" * 64)
    code, rep = tree.run()
    assert _case_row(rep, SP)["status"] == "evidence-insufficient", _case_row(rep, SP)
    assert rep["overall"] == "FAIL"


def test_spike3_oplock_not_implemented_is_insufficient_never_pass(tree):
    """OD-19 (2)：oplock 變體為驗收必要項；未實作（not_implemented）即標示 oplock_not_covered，不得 PASS。"""
    _enable_spike3(tree, mutate=lambda doc: doc["av_sim"]["variants"]["oplock"].__setitem__("oplock", "not_implemented"))
    code, rep = tree.run()
    assert _case_row(rep, SP)["status"] == "evidence-insufficient", _case_row(rep, SP)
    assert rep["overall"] == "FAIL"


def test_spike3_oplock_helper_cannot_replace_no_delete_injection(tree):
    """OD-19 (2)：允許 delete 的 oplock helper 不得取代主案例 no-delete 干擾證據。"""
    _enable_spike3(tree, mutate=lambda doc: doc["av_sim"].update(
        {"mode": "oplock", "share_mode": "FILE_SHARE_READ|FILE_SHARE_WRITE|FILE_SHARE_DELETE"}
    ))
    code, rep = tree.run()
    assert _case_row(rep, SP)["status"] == "evidence-insufficient", _case_row(rep, SP)
    assert rep["overall"] == "FAIL"


def test_spike3_helper_launch_error_is_insufficient_never_pass(tree):
    """OD-19 (2)：模擬防毒 helper 啟動失敗（launch_error 非 none）不得 PASS。"""
    _enable_spike3(tree, launch_error="helper_spawn_failed")
    code, rep = tree.run()
    assert _case_row(rep, SP)["status"] == "evidence-insufficient", _case_row(rep, SP)
    assert rep["overall"] == "FAIL"


def test_spike3_missing_av_sim_parameters_is_insufficient(tree):
    """OD-19 (2)：av_sim 參數為必填；缺漏即不合格（schema 驗證失敗）。"""
    _enable_spike3(tree, drop_av=True)
    code, rep = tree.run()
    assert _case_row(rep, SP)["status"] == "evidence-insufficient", _case_row(rep, SP)
    assert rep["overall"] == "FAIL"


def test_spike3_git_head_mismatch_is_insufficient(tree):
    _enable_spike3(tree, mutate=lambda d: d["artifact"].__setitem__("git_head", "0" * 40))
    code, rep = tree.run()
    assert _case_row(rep, SP)["status"] == "evidence-insufficient"
    assert rep["overall"] == "FAIL"


def test_spike3_missing_evidence_file_is_insufficient(tree):
    _enable_spike3(tree)
    tree.spike3 = {}
    code, rep = tree.run()
    assert _case_row(rep, SP)["status"] == "evidence-insufficient"
    assert rep["overall"] == "FAIL"


def test_spike3_schema_invalid_is_insufficient(tree):
    _enable_spike3(tree, drop_integrity=True)
    code, rep = tree.run()
    assert _case_row(rep, SP)["status"] == "evidence-insufficient"
    assert rep["overall"] == "FAIL"


def test_spike3_verdict_fail_is_fail(tree):
    _enable_spike3(tree, verdict="FAIL")
    code, rep = tree.run()
    assert _case_row(rep, SP)["status"] == "FAIL"
    assert rep["overall"] == "FAIL"


def test_spike3_insufficient_verdict_is_not_pass(tree):
    _enable_spike3(tree, verdict="INSUFFICIENT_EVIDENCE")
    code, rep = tree.run()
    assert _case_row(rep, SP)["status"] == "evidence-insufficient"
    assert rep["overall"] == "FAIL"


def test_junit_provenance_plugin_records_head_require_cargo_and_argv(tmp_path):
    """scripts/rust/junit_provenance.py 寫出的 properties 須能被 gate 的 provenance 檢查接受（HEAD／SW_REQUIRE_CARGO／argv）。"""
    import os
    import xml.etree.ElementTree as ET

    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_probe.py").write_text("def test_probe():\n    assert True\n", encoding="utf-8")
    junit = tmp_path / "junit.xml"
    env = {**os.environ, "PYTHONPATH": str(ROOT / "scripts" / "rust"), "SW_REQUIRE_CARGO": "1"}
    res = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests", "-p", "junit_provenance",
         f"--junitxml={junit}"],
        cwd=tmp_path, env=env, capture_output=True, text=True, check=False,
    )
    assert res.returncode == 0, res.stdout + res.stderr
    root = ET.parse(junit).getroot()
    props = {p.get("name"): p.get("value") for p in root.iter("property")}
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True,
                          check=True).stdout.strip()
    assert props["HEAD"] == head
    assert props["SW_REQUIRE_CARGO"] == "1"
    argv = json.loads(props["argv"])
    assert "tests" in argv and "junit_provenance" in " ".join(argv)
    assert "--junitxml" in " ".join(argv)


# ---- L15 gate accounting（計畫 04 §5c）：not_applicable 缺口與 verdict 對應 ----

def _load_gate_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location("stage1a_gate_under_test", GATE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_NA_LINUX_ROW = {
    "verdict": "INSUFFICIENT_EVIDENCE", "gate_eligible": False, "na_only_gap": True,
    "not_applicable_steps": [{"step": 8, "action": "cli"}], "platform": "linux",
}
_WIN_PASS_ROW = {"verdict": "PASS", "gate_eligible": True, "not_applicable_steps": [], "platform": "windows"}


def test_l15_linux_na_only_plus_windows_pass_is_pass():
    gate = _load_gate_module()
    assert gate._l15_case_status([dict(_NA_LINUX_ROW), dict(_WIN_PASS_ROW)]) == "PASS"


def test_l15_linux_na_only_alone_is_evidence_insufficient():
    gate = _load_gate_module()
    assert gate._l15_case_status([dict(_NA_LINUX_ROW)]) == "evidence-insufficient"


def test_l15_insufficient_row_is_evidence_insufficient_not_fail():
    gate = _load_gate_module()
    row = {"verdict": "INSUFFICIENT_EVIDENCE", "gate_eligible": False, "na_only_gap": False, "not_applicable_steps": []}
    assert gate._l15_case_status([row, dict(_WIN_PASS_ROW)]) == "evidence-insufficient"


def test_l15_real_fail_still_fails_even_with_na_gap():
    gate = _load_gate_module()
    fail = {"verdict": "FAIL", "gate_eligible": False, "na_only_gap": False, "not_applicable_steps": []}
    assert gate._l15_case_status([dict(_NA_LINUX_ROW), fail]) == "FAIL"
    assert gate._l15_case_status([dict(_NA_LINUX_ROW), dict(_WIN_PASS_ROW), fail]) == "FAIL"


def test_l15_na_flag_without_structured_steps_does_not_exempt_row():
    gate = _load_gate_module()
    forged = {"verdict": "INSUFFICIENT_EVIDENCE", "gate_eligible": False, "na_only_gap": True, "not_applicable_steps": []}
    assert gate._l15_case_status([forged, dict(_WIN_PASS_ROW)]) == "evidence-insufficient"
