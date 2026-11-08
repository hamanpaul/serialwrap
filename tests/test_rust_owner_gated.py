"""擁有者機台登記掃描（`scripts/rust/owner_gated.py`）的 deselected 缺口回歸（Stage 1 exit G3-owner-gated-deselection）。

缺口（sol ccf29ce，Medium）：owner-gated 節點若同時列於 `collected` 與 `deselected`、且 `results` 沒有對應列，
舊版 `scan_cargo_report` 仍把它視為「存在」，`owner_gated_skipped` 與 `absent_expected` 皆不列它；
最小報告即可讓 `l1_judge` 判 PASS，未執行的擁有者機台必要能力因而靜默消失。

本檔的斷言：
- scan 層：此類節點必須列入 `owner_gated_skipped`（擁有者待補），登記列另列入 `absent_expected`（收集失效）。
- 閘門層：以子行程執行 `stage1a_gate.py` 與 `check_p4_cases.py --check`，輸入為暫存樹、暫存 lock 與合成報告；
  斷言該列不判 PASS，且列名可見於 reasons 或 owner 清單。
- 不改 `l1_judge.py`、凍結鎖與 selector；不讀寫真實 repo 的鎖檔與證據（check 與 gate 皆在暫存樹執行）。
"""
from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = [
    pytest.mark.core_portable,
    pytest.mark.process_rpc,
]

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts" / "rust"
ACC = ROOT / "tests" / "acceptance"
OWNER_PY = SCRIPTS / "owner_gated.py"
GATE = SCRIPTS / "stage1a_gate.py"
CHECK = SCRIPTS / "check_p4_cases.py"
STAGE1A_TESTS = ROOT / "tests" / "test_rust_stage1a_gate.py"

ARGV = ["--workspace"]


def _nid(crate: str, name: str) -> str:
    """cargo nodeid 形式 `cargo:<crate>::<target>::<name>`；target 段與掃描無關，統一用 `t`。"""
    return f"cargo:{crate}::t::{name}"


PLAIN = _nid("sw-cli", "routes_native_groups")  # 一般（非擁有者）測試
GUI = _nid("sw-daemon", "cap_windows_native__launcher_gui_subsystem")  # 登記列（OWNER_GATED，linux 會編譯）
GUI_KEY = "sw-daemon::cap_windows_native__launcher_gui_subsystem"  # absent_expected 的 `crate::name` 格式
UNLISTED = _nid("sw-pal", "cap_new_unlisted_probe")  # 不在登記表，但以 cap_ 開頭（仍屬擁有者機台相依）


def _load(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _owner() -> Any:
    return _load("owner_gated_deselect_under_test", OWNER_PY)


def _owner_nids(platform: str = "linux") -> list[str]:
    """本平台、selector `--workspace` 下所有登記列的 nodeid（與 owner_gated.expected_names 同範圍）。"""
    return [_nid(crate, name) for crate, name in _owner().expected_names(platform, ARGV)]


def _scan_report(*, collected: list[str], deselected: list[str] = (), results: dict[str, str] | None = None) -> dict[str, Any]:
    return {
        "runner": "cargo",
        "argv": list(ARGV),
        "collected": list(collected),
        "deselected": list(deselected),
        "results": [{"nodeid": nid, "outcome": out} for nid, out in (results or {}).items()],
    }


def _l1_cargo_report(*, collected: list[str], deselected: list[str] = (), results: dict[str, str]) -> dict[str, Any]:
    """完整通過 P2 schema 的 cargo `l1-results.v1` 報告（缺欄即 INVALID，會遮蔽本檔要測的規則）。"""
    rows = []
    for nid, outcome in results.items():
        rows.append({
            "nodeid": nid,
            "outcome": outcome,
            "skip_kind": ("capability" if outcome == "skipped" else None),
            "phases": {"setup": "passed",
                       "call": "skipped" if outcome == "skipped" else "passed",
                       "teardown": "passed"},
        })
    return {
        "schema_version": 2, "format": "l1-results.v1", "runner": "cargo", "platform": "linux",
        "argv": list(ARGV), "markexpr": "", "counts": {"all": {"passed": 0, "failed": 0, "skipped": 0}},
        "resources": {}, "uart_kind": "native", "not_collected_platform": [],
        "core_portable_platform_skips": [], "collected": list(collected), "deselected": list(deselected),
        "results": rows, "exit_code": 0, "finished": True, "deployed_content": None,
    }


# ---------------------------------------------------------------------------
# scan 層：deselected 且無結果的擁有者節點不得靜默消失
# ---------------------------------------------------------------------------

def test_deselected_registry_row_without_result_is_owner_pending_and_absent():
    """登記列同時在 collected 與 deselected、results 無對應列 → 列為 owner 待補，且登記缺席（收集失效）。"""
    report = _scan_report(collected=[PLAIN, GUI], deselected=[GUI], results={PLAIN: "passed"})
    scan = _owner().scan_cargo_report(report, "linux")
    assert GUI in scan["owner_gated_skipped"], scan
    assert GUI_KEY in scan["absent_expected"], scan


def test_deselected_unlisted_cap_row_without_result_is_owner_pending():
    """不在登記表的 cap_ 列同時在 collected 與 deselected、無結果 → 必須列為 owner 待補（不得靜默）。"""
    report = _scan_report(collected=[PLAIN, UNLISTED], deselected=[UNLISTED], results={PLAIN: "passed"})
    scan = _owner().scan_cargo_report(report, "linux")
    assert UNLISTED in scan["owner_gated_skipped"], scan
    assert UNLISTED not in scan["absent_expected"], scan


@pytest.mark.parametrize("nodeid", [GUI, UNLISTED], ids=["登記列", "未登記cap列"])
def test_deselected_owner_row_is_never_silent(nodeid):
    """任何 owner-gated 節點若只以 deselected 存在，兩份清單至少其一必須列出它。"""
    report = _scan_report(collected=[PLAIN, nodeid], deselected=[nodeid], results={PLAIN: "passed"})
    scan = _owner().scan_cargo_report(report, "linux")
    name = nodeid.rsplit("::", 1)[-1]
    listed_as_owner = nodeid in scan["owner_gated_skipped"]
    listed_as_absent = any(entry.endswith("::" + name) for entry in scan["absent_expected"])
    assert listed_as_owner or listed_as_absent, scan


def test_scan_exposes_deselected_owner_rows_explicitly():
    """`owner_gated_deselected` 單列出 deselected 且無結果的 owner 節點，供報告與人工核對。"""
    report = _scan_report(collected=[PLAIN, GUI, UNLISTED], deselected=[GUI, UNLISTED], results={PLAIN: "passed"})
    scan = _owner().scan_cargo_report(report, "linux")
    assert scan["owner_gated_deselected"] == sorted([GUI, UNLISTED]), scan


def test_collected_and_passed_registry_row_is_not_flagged():
    """回歸：正常收集且通過的登記列不得被誤列為缺失或待補。"""
    report = _scan_report(collected=[PLAIN, GUI], results={PLAIN: "passed", GUI: "passed"})
    scan = _owner().scan_cargo_report(report, "linux")
    assert GUI_KEY not in scan["absent_expected"], scan
    assert GUI not in scan["owner_gated_skipped"], scan
    assert scan["owner_gated_deselected"] == [], scan


def test_skipped_owner_row_with_result_is_still_listed():
    """回歸：有 skipped 結果列的擁有者節點仍照舊列為 owner 待補。"""
    report = _scan_report(collected=[PLAIN, GUI], results={PLAIN: "passed", GUI: "skipped"})
    scan = _owner().scan_cargo_report(report, "linux")
    assert scan["owner_gated_skipped"] == [GUI], scan


def test_deselected_plain_row_is_not_an_owner_row():
    """回歸：被 deselected 的一般測試不屬擁有者登記，不得出現在 owner 清單。"""
    plain2 = _nid("sw-cli", "some_plain_case")
    report = _scan_report(collected=[PLAIN, plain2], deselected=[plain2], results={PLAIN: "passed"})
    scan = _owner().scan_cargo_report(report, "linux")
    assert scan["owner_gated_skipped"] == [], scan
    assert scan["owner_gated_deselected"] == [], scan


# ---------------------------------------------------------------------------
# 閘門層（子行程）：stage1a_gate.py 與 check_p4_cases.py --check 不得判 PASS
# ---------------------------------------------------------------------------

def _stage1a_fixture() -> Any:
    """重用 test_rust_stage1a_gate.py 的暫存樹（GateTree）與報告欄位，不另寫一份閘門輸入。"""
    return _load("test_rust_stage1a_gate_fixture_reuse", STAGE1A_TESTS)


def _deselect_in_stage1a_report(report: dict[str, Any], nodeid: str) -> None:
    """把 nodeid 改為「collected 與 deselected、results 無列」（與 scan 層的缺口情境相同）。"""
    report["results"] = [row for row in report["results"] if row["nodeid"] != nodeid]
    if nodeid not in report["collected"]:
        report["collected"].append(nodeid)
    report["deselected"] = [nodeid]


def test_stage1a_gate_deselected_registry_row_is_evidence_insufficient(tmp_path: Path) -> None:
    """stage1a_gate：s1a_cargo linux 的登記列 deselected 且無結果 → evidence-insufficient，整體 FAIL（非 PASS）。"""
    fx = _stage1a_fixture()
    tree = fx.GateTree(tmp_path / "repo")
    report = fx._cargo_report("linux", dict(fx.LINUX_OWNER_OUTCOMES))
    _deselect_in_stage1a_report(report, fx.GUI)
    tree.l1_reports[("linux", "cargo")] = report
    _code, rep = tree.run()
    row = fx._l1_row(rep, "s1a_cargo", "linux")
    assert row["verdict"] != "PASS", row
    assert row["verdict"] == "evidence-insufficient", row
    assert "launcher_gui_subsystem" in json.dumps(row["reasons"], ensure_ascii=False), row["reasons"]
    assert rep["overall"] == "FAIL", rep["overall"]


def test_stage1a_gate_deselected_unlisted_cap_row_is_blocked_owner(tmp_path: Path) -> None:
    """stage1a_gate：未登記的 cap_ 列 deselected 且無結果 → BLOCKED-OWNER 並列名（非 PASS、非靜默）。"""
    fx = _stage1a_fixture()
    tree = fx.GateTree(tmp_path / "repo")
    report = fx._cargo_report("linux", dict(fx.LINUX_OWNER_OUTCOMES))
    unlisted = "cargo:sw-pal::exec_delegate::cap_new_unlisted_probe"
    _deselect_in_stage1a_report(report, unlisted)
    tree.l1_reports[("linux", "cargo")] = report
    _code, rep = tree.run()
    row = fx._l1_row(rep, "s1a_cargo", "linux")
    assert row["verdict"] == "BLOCKED-OWNER", row
    assert unlisted in json.dumps(rep["owner_gated"], ensure_ascii=False), rep["owner_gated"]
    assert rep["overall"] == "IMPLEMENTED", rep["overall"]


def _stage_check_tree(root: Path, l1_lock: dict[str, Any]) -> Path:
    """建立最小暫存樹供 check_p4_cases.py 以子行程執行；p4 映射表為空，只剩 L1 判定與 owner 掃描在運作。"""
    scripts = root / "scripts" / "rust"
    scripts.mkdir(parents=True)
    shutil.copy2(CHECK, scripts / "check_p4_cases.py")
    shutil.copy2(OWNER_PY, scripts / "owner_gated.py")
    acc = root / "tests" / "acceptance"
    acc.mkdir(parents=True)
    shutil.copy2(ACC / "l1_judge.py", acc / "l1_judge.py")  # P2 唯一判定實作（原檔，不改）
    shutil.copy2(ACC / "check_evidence_index.py", acc / "check_evidence_index.py")
    (acc / "p4_case_map.yaml").write_text(yaml.safe_dump({"cases": []}, allow_unicode=True), encoding="utf-8")
    (acc / "p4_required.lock.json").write_text(
        json.dumps({"schema_version": 1, "frozen_commit": "staged", "cases": [], "error_codes_at_freeze": []}),
        encoding="utf-8",
    )
    (acc / "l1_required.lock.json").write_text(json.dumps(l1_lock, ensure_ascii=False), encoding="utf-8")
    (root / "contracts").mkdir()
    (root / "contracts" / "error_codes.yaml").write_text(yaml.safe_dump({"codes": []}), encoding="utf-8")
    return scripts / "check_p4_cases.py"


def _check_p4_run(tmp_path: Path, cargo_report: dict[str, Any]) -> tuple[subprocess.CompletedProcess, dict[str, Any], dict[str, Any]]:
    lock = {"schema_version": 1, "sets": {
        "s1b_20groups": {p: {"selector": {"argv": ["x", p], "paths": [], "markexpr": ""},
                             "nodeids": ["tests/x.py::g"]} for p in ("linux", "windows")},
        "s1b_cargo": {p: {"selector": {"argv": list(ARGV), "paths": [], "markexpr": ""},
                          "nodeids": [PLAIN]} for p in ("linux", "windows")},
    }}
    repo = tmp_path / "repo"
    script = _stage_check_tree(repo, lock)
    evidence = tmp_path / "evidence"
    l1 = evidence / "l1"
    (l1 / "linux" / "cargo").mkdir(parents=True)
    (l1 / "linux" / "cargo" / "resource-report.json").write_text(json.dumps(cargo_report), encoding="utf-8")
    summary = evidence / "summary.json"
    proc = subprocess.run(
        [sys.executable, str(script), "--check", "--evidence-dir", str(evidence),
         "--l1-results", str(l1), "--summary-out", str(summary)],
        capture_output=True, text=True, check=False,
    )
    assert summary.is_file(), (proc.returncode, proc.stderr)
    doc = json.loads(summary.read_text(encoding="utf-8"))
    row = next(r for r in doc["l1"]["sets"] if r["set"] == "s1b_cargo" and r["platform"] == "linux")
    return proc, doc, row


def test_check_p4_deselected_registry_row_is_not_pass(tmp_path: Path) -> None:
    """check_p4_cases --check：登記列 deselected 且無結果 → s1b_cargo linux 為 INSUFFICIENT（非 PASS、列名可見）。"""
    others = {nid: "passed" for nid in _owner_nids("linux") if nid != GUI}
    report = _l1_cargo_report(collected=[PLAIN, *_owner_nids("linux")], deselected=[GUI],
                              results={PLAIN: "passed", **others})
    proc, doc, row = _check_p4_run(tmp_path, report)
    assert row["verdict"] == "INSUFFICIENT", row
    assert "launcher_gui_subsystem" in json.dumps(row["reasons"], ensure_ascii=False), row["reasons"]
    assert doc["overall"] != "PASS", doc["overall"]
    assert proc.returncode != 0, proc.stdout


def test_check_p4_deselected_unlisted_cap_row_is_blocked_owner(tmp_path: Path) -> None:
    """check_p4_cases --check：未登記 cap_ 列 deselected 且無結果 → s1b_cargo linux 為 BLOCKED-OWNER 並列名。"""
    owner_rows = {nid: "passed" for nid in _owner_nids("linux")}
    report = _l1_cargo_report(collected=[PLAIN, *owner_rows, UNLISTED], deselected=[UNLISTED],
                              results={PLAIN: "passed", **owner_rows})
    proc, doc, row = _check_p4_run(tmp_path, report)
    assert row["verdict"] == "BLOCKED-OWNER", row
    assert UNLISTED in row["owner_gated"], row
    assert doc["overall"] != "PASS", doc["overall"]
