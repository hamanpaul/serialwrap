"""P6 Task 13（R5-F3）：`scripts/rust/s22_gate_check.py` 的 Slice 2.2 出口判定。

合成 fixture（不讀最新報告）：`<dir>/<section>/<platform>/resource-report.json`（`l1-results.v1`）＋對照表＋L1 鎖。
判定規則：每個 `s22_gate:*` 集合以 P2 `tests/acceptance/l1_judge.judge` 判定；每個 `AUTOMATED` 條目的每個測試
須在報告中恰一列 `passed`；函式存在不等於已執行且通過。
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "rust" / "s22_gate_check.py"

pytestmark = [pytest.mark.core_portable, pytest.mark.process_rpc]

CARGO_ARGV = ["--workspace", "--features", "test-fakes"]
PTY_ARGV = ["--package", "sw-pal", "--features", "test-fakes", "--test", "console_conformance", "--", "--ignored", "cap_posix_pty__"]
BB_ARGV = ["-q", "tests/contract_blackbox/slice22"]
WIN_ARGV = ["--features", "test-fakes"]
PY_ARGV = ["-q", "tests/test_s22_py_parity.py"]

RAW_OWNER = "tests/contract_blackbox/slice22/test_console_pty.py::test_first_console_raw_second_line_buffered"
PARTIAL = "cargo:sw-core::human_tx_lane::human_raw_partial_not_resent"
PTY_CAP = "cargo:sw-pal::console_conformance::cap_posix_pty__mux_mixed_pty_and_tcp"
WIN_CORE = "cargo:sw-core::console_hub::close_all_on_release"
WIN_CAP = "cargo:sw-pal::console_conformance::cap_windows_com__console_com0com_open"
PY_PARITY = "tests/test_s22_py_parity.py::test_parity_row"
FUNC_REF = "func-test/runner.py -c co-01-line-buffering"
# 合成 parity-amendments：§1 一列（P6）、§1 一列（P5，不計）、§3 P6 小節一列。
PARITY_DOC = """# fixture
## §1 總表
| # | 稽核來源 | 項目 | 判定 | 承接 Phase/Task | 修改的計畫位置 | 新增測試／入鎖要求 |
|---|---|---|---|---|---|---|
| 35 | A | x | ALIGN | P6-T7 | 07 | t |
| 3 | A | y | ALIGN | P5-T19 | 06 | t |
## §2 稽核 B
| 稽核 ID | 判定 | 承接 Phase/Task | 承接編號 | 修改的計畫位置 |
|---|---|---|---|---|
| E-15 | ADD | P6-T8 | #109 | 07 |
## §3 已實作追加修正清單
### `feature/rust-refactor-p6`（Slice 2.2；P6 審查）
| # | 審查 | 嚴重度 | 問題 | 承接（07） |
|---|---|---|---|---|
| 179 | F1 | Medium | gate | 07 Task 1 |
### `feature/rust-refactor-p7`（Slice 2.3）
| # | 審查 | 嚴重度 | 問題 | 承接 |
|---|---|---|---|---|
| 200 | F1 | Medium | p7 | 08 |
"""


def _row(nodeid: str, outcome: str = "passed") -> dict:
    if outcome == "passed":
        phases = {"setup": "passed", "call": "passed", "teardown": "passed"}
    elif outcome in ("failed", "error"):
        phases = {"setup": "passed", "call": "failed", "teardown": None}
    else:
        phases = {"setup": "skipped", "call": None, "teardown": None}
    skip_kind = "capability" if outcome == "skipped" else None
    return {"nodeid": nodeid, "outcome": outcome, "skip_kind": skip_kind, "phases": phases}


def _report(platform: str, runner: str, argv: list[str], rows: list[dict], *, exit_code: int = 0, finished: bool = True) -> dict:
    return {
        "schema_version": 2, "format": "l1-results.v1", "runner": runner, "platform": platform,
        # pytest 報告的 argv[0] 為程式入口（l1_judge `_report_argv` 會去掉）
        "argv": (["/x/pytest/__main__.py"] if runner == "pytest" else []) + list(argv), "markexpr": "", "counts": {}, "resources": {}, "uart_kind": "native",
        "not_collected_platform": [], "core_portable_platform_skips": [],
        "collected": [r["nodeid"] for r in rows], "deselected": [], "results": rows,
        "exit_code": exit_code, "finished": finished, "deployed_content": None,
    }


def _lock(win_extra: tuple[str, ...] = ()) -> dict:
    def spec(argv, ids):
        return {"selector": {"argv": list(argv), "paths": [], "markexpr": ""}, "nodeids": sorted(ids)}

    return {"schema_version": 1, "sets": {
        "s22_gate:cargo_workspace": {"linux": spec(CARGO_ARGV, [PARTIAL])},
        "s22_gate:console_conformance_pty": {"linux": spec(PTY_ARGV, [PTY_CAP])},
        "s22_gate:blackbox_rust": {"linux": spec(BB_ARGV, [RAW_OWNER])},
        "s22_gate:win_test": {"windows": spec(WIN_ARGV, [WIN_CORE, *win_extra])},
        "s22_gate:py_parity": {"linux": spec(PY_ARGV, [PY_PARITY])},
    }}


def _emap() -> dict:
    return {"schema_version": 1, "frozen_lock_sha1": "0" * 40, "cases": {
        "f5-second-console-linebuffer": {"status": "AUTOMATED", "tests": [RAW_OWNER]},
        "sc15-console-ring-drop-oldest": {"status": "AUTOMATED",
                                          "tests": ["crates/sw-core/tests/human_tx_lane.rs::human_raw_partial_not_resent"]},
        "p1-con-fanout": {"status": "BLOCKED-OWNER", "procedure": "docs/x.md", "evidence_schema": ["a.json"], "blocked_item": 5},
        "win-telnet-raw-owner": {"status": "NOT_APPLICABLE", "reason": "fixture"},
    }, "parity_rows": {
        "#35": {"status": "PASS", "tests": [PY_PARITY]},
        "#109": {"status": "BLOCKED-OWNER", "procedure": "docs/rust-refactor/procedures/windows-telnet-frontends.md"},
        "#179": {"status": "PASS", "tests": ["crates/sw-core/tests/human_tx_lane.rs::human_raw_partial_not_resent"]},
    }}


def _good_reports() -> dict[tuple[str, str], dict]:
    return {
        ("cargo_workspace", "linux"): _report("linux", "cargo", CARGO_ARGV, [_row(PARTIAL)]),
        ("console_conformance_pty", "linux"): _report("linux", "cargo", PTY_ARGV, [_row(PTY_CAP)]),
        ("blackbox_rust", "linux"): _report("linux", "pytest", BB_ARGV, [_row(RAW_OWNER)]),
        ("win_test", "windows"): _report("windows", "cargo", WIN_ARGV, [_row(WIN_CORE), _row(WIN_CAP, "skipped")]),
        ("py_parity", "linux"): _report("linux", "pytest", PY_ARGV, [_row(PY_PARITY)]),
    }


def _run(tmp_path: Path, reports: dict, lock: dict | None = None, emap: dict | None = None, *,
         cases_lock: dict | None = None, l15: dict | None = None, prereqs_rc: int = 0,
         parity_doc: str = PARITY_DOC) -> tuple[int, dict]:
    res = tmp_path / "l1"
    for (section, platform), rep in reports.items():
        out = res / section / platform / "resource-report.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(rep), encoding="utf-8")
    lock_path = tmp_path / "lock.json"
    lock_path.write_text(json.dumps(lock or _lock()), encoding="utf-8")
    map_path = tmp_path / "map.yaml"
    map_path.write_text(yaml.safe_dump(emap or _emap(), allow_unicode=True), encoding="utf-8")
    emap_cases = (emap or _emap())["cases"]
    cases_lock_path = tmp_path / "cases.lock.json"
    cases_lock_path.write_text(json.dumps(cases_lock or {"sets": {"slice_2.2": sorted(emap_cases)}}), encoding="utf-8")
    parity_path = tmp_path / "parity.md"
    parity_path.write_text(parity_doc, encoding="utf-8")
    prereqs = tmp_path / "prereqs.py"   # check_p6_prereqs.py --strict 的替身（只看 exit code）
    prereqs.write_text(f"import sys\nassert sys.argv[1:] == ['--strict']\nsys.exit({prereqs_rc})\n", encoding="utf-8")
    extra = []
    if l15 is not None:
        l15_path = tmp_path / "l15.json"
        l15_path.write_text(json.dumps(l15), encoding="utf-8")
        extra = ["--l15-results", str(l15_path)]
    out_path = tmp_path / "gate.json"
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--l1-results", str(res), "--map", str(map_path), "--lock", str(lock_path),
         "--cases-lock", str(cases_lock_path), "--parity-doc", str(parity_path), "--prereqs-script", str(prereqs),
         *extra, "--out", str(out_path)],
        capture_output=True, text=True, timeout=60,
    )
    assert out_path.is_file(), proc.stderr
    return proc.returncode, json.loads(out_path.read_text(encoding="utf-8"))


def _case(out: dict, cid: str) -> dict:
    return next(c for c in out["cases"] if c["case_id"] == cid)


def _section(out: dict, set_id: str, platform: str) -> dict:
    return next(s for s in out["sections"] if s["set_id"] == set_id and s["platform"] == platform)


def test_existing_function_not_executed_is_insufficient(tmp_path: Path) -> None:
    reports = _good_reports()
    reports[("blackbox_rust", "linux")] = _report("linux", "pytest", BB_ARGV, [])
    lock = _lock()
    lock["sets"]["s22_gate:blackbox_rust"]["linux"]["nodeids"] = []
    rc, out = _run(tmp_path, reports, lock=lock)
    case = _case(out, "f5-second-console-linebuffer")
    assert case["status"] == "INSUFFICIENT" and RAW_OWNER in case["missing_tests"], case
    assert out["result"] == "INSUFFICIENT" and rc == 1


def test_cargo_nonzero_count_is_not_enough(tmp_path: Path) -> None:
    reports = _good_reports()
    rows = [_row(f"cargo:sw-core::filler::t{i}") for i in range(200)]
    reports[("cargo_workspace", "linux")] = _report("linux", "cargo", CARGO_ARGV, rows)
    rc, out = _run(tmp_path, reports)
    assert _section(out, "s22_gate:cargo_workspace", "linux")["verdict"] == "INSUFFICIENT", out
    assert out["result"] == "INSUFFICIENT" and rc == 1


def test_required_ignored_or_duplicate_insufficient(tmp_path: Path) -> None:
    reports = _good_reports()
    reports[("console_conformance_pty", "linux")] = _report("linux", "cargo", PTY_ARGV, [_row(PTY_CAP, "skipped")])
    rc, out = _run(tmp_path / "a", reports)
    assert _section(out, "s22_gate:console_conformance_pty", "linux")["verdict"] == "INSUFFICIENT"
    assert rc == 1
    reports = _good_reports()
    reports[("console_conformance_pty", "linux")] = _report("linux", "cargo", PTY_ARGV, [_row(PTY_CAP), _row(PTY_CAP)])
    rc, out = _run(tmp_path / "b", reports)
    assert _section(out, "s22_gate:console_conformance_pty", "linux")["verdict"] == "INSUFFICIENT"
    reports = _good_reports()
    reports[("console_conformance_pty", "linux")] = _report(
        "linux", "cargo", PTY_ARGV, [_row(PTY_CAP, "failed")], exit_code=101
    )
    rc, out = _run(tmp_path / "c", reports)
    assert _section(out, "s22_gate:console_conformance_pty", "linux")["verdict"] == "FAIL"
    assert out["result"] == "FAIL" and rc == 1


def test_runner_exit_nonzero_insufficient(tmp_path: Path) -> None:
    for i, kwargs in enumerate(({"exit_code": 101}, {"finished": False})):
        reports = _good_reports()
        reports[("cargo_workspace", "linux")] = _report("linux", "cargo", CARGO_ARGV, [_row(PARTIAL)], **kwargs)
        rc, out = _run(tmp_path / str(i), reports)
        assert _section(out, "s22_gate:cargo_workspace", "linux")["verdict"] == "INSUFFICIENT", kwargs
        assert out["result"] == "INSUFFICIENT" and rc == 1


def test_cap_windows_com_absent_is_blocked_not_pass(tmp_path: Path) -> None:
    rc, out = _run(tmp_path / "a", _good_reports())
    assert _section(out, "s22_gate:win_test", "windows")["verdict"] == "PASS"
    assert rc == 0
    rc, out = _run(tmp_path / "b", _good_reports(), lock=_lock(win_extra=(WIN_CAP,)))
    assert _section(out, "s22_gate:win_test", "windows")["verdict"] == "INSUFFICIENT", "不得以 ignored 充數"
    assert rc == 1


def test_all_present_pass(tmp_path: Path) -> None:
    rc, out = _run(tmp_path, _good_reports())
    assert out["result"] == "PASS" and rc == 0, out
    assert _case(out, "f5-second-console-linebuffer")["status"] == "PASS"
    assert _case(out, "sc15-console-ring-drop-oldest")["status"] == "PASS"
    assert _case(out, "p1-con-fanout")["status"] == "BLOCKED-OWNER", "BLOCKED-OWNER 不得標 PASS"
    assert all(s["verdict"] == "PASS" for s in out["sections"])


def test_missing_report_and_pending_entry_insufficient(tmp_path: Path) -> None:
    reports = _good_reports()
    del reports[("win_test", "windows")]
    emap = _emap()
    emap["cases"]["bc02-normal-replay-53"] = {"status": "PENDING"}
    rc, out = _run(tmp_path, reports, emap=emap)
    assert _section(out, "s22_gate:win_test", "windows")["verdict"] == "INSUFFICIENT"
    assert _case(out, "bc02-normal-replay-53")["status"] == "INSUFFICIENT"
    assert out["result"] == "INSUFFICIENT" and rc == 1


def test_unchecked_only_case_is_insufficient(tmp_path: Path) -> None:
    """審查 3317987_d6e3b3c-opus High #1：只含 func-test／scenario 參照的 AUTOMATED 條目沒有 L1.5 執行證據時不得 PASS。"""
    emap = _emap()
    emap["cases"]["co-01-line-buffering"] = {"status": "AUTOMATED", "tests": [FUNC_REF]}
    rc, out = _run(tmp_path / "a", _good_reports(), emap=emap)
    case = _case(out, "co-01-line-buffering")
    assert case["status"] == "INSUFFICIENT" and case["unchecked_tests"] == [FUNC_REF], case
    assert out["result"] == "INSUFFICIENT" and rc == 1
    passed = {"format": "s22-l15-results.v1", "runs": [{"ref": FUNC_REF, "outcome": "passed", "exit_code": 0}]}
    rc, out = _run(tmp_path / "b", _good_reports(), emap=emap, l15=passed)
    assert _case(out, "co-01-line-buffering")["status"] == "PASS" and rc == 0, out
    failed = {"format": "s22-l15-results.v1", "runs": [{"ref": FUNC_REF, "outcome": "failed", "exit_code": 1}]}
    rc, out = _run(tmp_path / "c", _good_reports(), emap=emap, l15=failed)
    assert _case(out, "co-01-line-buffering")["status"] == "FAIL" and out["result"] == "FAIL" and rc == 1
    dup = {"format": "s22-l15-results.v1", "runs": [passed["runs"][0], passed["runs"][0]]}
    rc, out = _run(tmp_path / "d", _good_reports(), emap=emap, l15=dup)
    assert _case(out, "co-01-line-buffering")["status"] == "INSUFFICIENT" and rc == 1


def test_missing_required_sets_insufficient(tmp_path: Path) -> None:
    """審查 Medium #2：lock 缺任一必要 `s22_gate:*` 集合（例：win_test、blackbox_rust、py_parity）→ INSUFFICIENT。"""
    for i, missing in enumerate(("s22_gate:win_test", "s22_gate:blackbox_rust", "s22_gate:py_parity")):
        lock = _lock()
        del lock["sets"][missing]
        rc, out = _run(tmp_path / str(i), _good_reports(), lock=lock)
        assert any(s["set_id"] == missing and s["verdict"] == "INSUFFICIENT" for s in out["sections"]), (missing, out)
        assert out["result"] == "INSUFFICIENT" and rc == 1


def test_map_must_cover_frozen_slice22(tmp_path: Path) -> None:
    """審查 Medium #2：對照表鍵集合須等於凍結 slice_2.2；`cases: {}` 或缺鍵不得 PASS。"""
    emap = _emap()
    rc, out = _run(tmp_path / "a", _good_reports(), emap=emap,
                   cases_lock={"sets": {"slice_2.2": sorted(emap["cases"]) + ["extra-frozen-case"]}})
    assert out["map_coverage"]["missing_cases"] == ["extra-frozen-case"] and out["result"] == "INSUFFICIENT" and rc == 1
    empty = dict(_emap(), cases={})
    rc, out = _run(tmp_path / "b", _good_reports(), emap=empty, cases_lock={"sets": {"slice_2.2": ["x"]}})
    assert out["result"] == "INSUFFICIENT" and rc == 1


def test_prereqs_strict_failure_insufficient(tmp_path: Path) -> None:
    """增補 #180：`check_p6_prereqs.py --strict` 非 0 → INSUFFICIENT。"""
    rc, out = _run(tmp_path, _good_reports(), prereqs_rc=1)
    assert out["prereqs"]["verdict"] == "INSUFFICIENT" and out["prereqs"]["exit_code"] == 1
    assert out["result"] == "INSUFFICIENT" and rc == 1


def test_parity_rows_listed_and_pending_not_pass(tmp_path: Path) -> None:
    """增補 #187：承接 Phase 為 P6 的承接編號逐列輸出；未登記或 OWNER-DECISION-PENDING → result 不得為 PASS。"""
    rc, out = _run(tmp_path / "a", _good_reports())
    assert [r["row"] for r in out["parity_rows"]] == ["#35", "#109", "#179"], out["parity_rows"]
    assert {r["row"]: r["verdict"] for r in out["parity_rows"]} == {"#35": "PASS", "#109": "BLOCKED-OWNER", "#179": "PASS"}
    emap = _emap()
    emap["parity_rows"]["#109"] = {"status": "OWNER-DECISION-PENDING"}
    rc, out = _run(tmp_path / "b", _good_reports(), emap=emap)
    assert out["result"] == "INSUFFICIENT" and rc == 1
    emap = _emap()
    del emap["parity_rows"]["#179"]
    rc, out = _run(tmp_path / "c", _good_reports(), emap=emap)
    row = next(r for r in out["parity_rows"] if r["row"] == "#179")
    assert row["status"] == "OWNER-DECISION-PENDING" and row["verdict"] == "INSUFFICIENT" and rc == 1


def test_module_importable_without_sw_core() -> None:
    spec = importlib.util.spec_from_file_location("s22_gate_check", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert callable(mod.main)
    src = SCRIPT.read_text(encoding="utf-8")
    assert "import sw_core" not in src and "from sw_core" not in src, "只用 stdlib＋PyYAML，判定委派 l1_judge"


def test_overlapping_sections_same_nodeid_once_each_is_pass(tmp_path: Path) -> None:
    """實跑（第 4 輪 r4）：`console_conformance_pty` 的 selector 會執行整個 `console_conformance` binary，其非 cap 測試
    同時出現在 `cargo_workspace` 報告；兩個集合各恰一列 `passed` 時不得因跨集合合計兩列而判證據不足。
    單一報告內重複仍為證據不足（不放寬「每份報告恰一列」）。"""
    shared = "cargo:sw-pal::console_conformance::tcp_accept_sets_nodelay_and_label_shape"
    emap = _emap()
    emap["cases"]["f5-second-console-linebuffer"]["tests"] = [
        RAW_OWNER, "crates/sw-pal/tests/console_conformance.rs::tcp_accept_sets_nodelay_and_label_shape"]
    lock = _lock()
    lock["sets"]["s22_gate:cargo_workspace"]["linux"]["nodeids"] = sorted([PARTIAL, shared])
    reports = _good_reports()
    reports[("cargo_workspace", "linux")] = _report("linux", "cargo", CARGO_ARGV, [_row(PARTIAL), _row(shared)])
    reports[("console_conformance_pty", "linux")] = _report("linux", "cargo", PTY_ARGV, [_row(PTY_CAP), _row(shared)])
    rc, out = _run(tmp_path / "a", reports, lock=lock, emap=emap)
    assert _case(out, "f5-second-console-linebuffer")["status"] == "PASS", out
    reports[("cargo_workspace", "linux")] = _report("linux", "cargo", CARGO_ARGV, [_row(PARTIAL), _row(shared), _row(shared)])
    reports[("console_conformance_pty", "linux")] = _report("linux", "cargo", PTY_ARGV, [_row(PTY_CAP)])
    rc, out = _run(tmp_path / "b", reports, lock=lock, emap=emap)
    case = _case(out, "f5-second-console-linebuffer")
    assert case["status"] == "INSUFFICIENT", case
    reports[("cargo_workspace", "linux")] = _report("linux", "cargo", CARGO_ARGV, [_row(PARTIAL), _row(shared)])
    reports[("console_conformance_pty", "linux")] = _report(
        "linux", "cargo", PTY_ARGV, [_row(PTY_CAP), _row(shared, "failed")], exit_code=101)
    rc, out = _run(tmp_path / "c", reports, lock=lock, emap=emap)
    assert _case(out, "f5-second-console-linebuffer")["status"] == "FAIL", out


def test_duplicate_within_one_report_not_masked_by_other_report_pass(tmp_path: Path) -> None:
    """OD-54（sol P6-c6ddda1-1）：跨集合皆 passed、無 failed → PASS（cd80448 追認），但同一份報告（集合×平台）內部
    重複仍判 INSUFFICIENT，不得被另一份報告的單列 passed 遮蔽；逐案與 parity_rows 皆同。"""
    shared = "cargo:sw-pal::console_conformance::tcp_accept_sets_nodelay_and_label_shape"
    emap = _emap()
    emap["cases"]["f5-second-console-linebuffer"]["tests"] = [
        RAW_OWNER, "crates/sw-pal/tests/console_conformance.rs::tcp_accept_sets_nodelay_and_label_shape"]
    lock = _lock()
    lock["sets"]["s22_gate:cargo_workspace"]["linux"]["nodeids"] = sorted([PARTIAL, shared])
    reports = _good_reports()
    # cargo_workspace 內部重複兩列 passed、console_conformance_pty 單列 passed
    reports[("cargo_workspace", "linux")] = _report("linux", "cargo", CARGO_ARGV, [_row(PARTIAL), _row(shared), _row(shared)])
    reports[("console_conformance_pty", "linux")] = _report("linux", "cargo", PTY_ARGV, [_row(PTY_CAP), _row(shared)])
    rc, out = _run(tmp_path / "a", reports, lock=lock, emap=emap)
    case = _case(out, "f5-second-console-linebuffer")
    assert case["status"] == "INSUFFICIENT", case
    assert "crates/sw-pal/tests/console_conformance.rs::tcp_accept_sets_nodelay_and_label_shape" in case["missing_tests"]
    # 反向：重複出現在 console_conformance_pty、cargo_workspace 單列 passed
    reports[("cargo_workspace", "linux")] = _report("linux", "cargo", CARGO_ARGV, [_row(PARTIAL), _row(shared)])
    reports[("console_conformance_pty", "linux")] = _report("linux", "cargo", PTY_ARGV, [_row(PTY_CAP), _row(shared), _row(shared)])
    rc, out = _run(tmp_path / "b", reports, lock=lock, emap=emap)
    assert _case(out, "f5-second-console-linebuffer")["status"] == "INSUFFICIENT", out
    # failed 仍優先於重複
    reports[("console_conformance_pty", "linux")] = _report(
        "linux", "cargo", PTY_ARGV, [_row(PTY_CAP), _row(shared), _row(shared, "failed")], exit_code=101)
    rc, out = _run(tmp_path / "c", reports, lock=lock, emap=emap)
    assert _case(out, "f5-second-console-linebuffer")["status"] == "FAIL", out


def test_judge_tests_duplicate_in_one_report_is_insufficient_unit() -> None:
    """OD-54 判定器單元：`judge_tests` 直接以 rows 判定（不經檔案），確認同報告重複不被他報告 passed 遮蔽。"""
    spec = importlib.util.spec_from_file_location("s22_gate_check", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    nid = "cargo:sw-core::m::t"
    ref = "crates/sw-core/tests/m.rs::t"
    dup = [{"nodeid": nid, "outcome": "passed", "_platform": "linux", "_section": "s22_gate:a"},
           {"nodeid": nid, "outcome": "passed", "_platform": "linux", "_section": "s22_gate:a"},
           {"nodeid": nid, "outcome": "passed", "_platform": "linux", "_section": "s22_gate:b"}]
    assert mod.judge_tests([ref], {nid: dup}, None)[0] == "INSUFFICIENT"
    assert mod.judge_tests([ref], {nid: dup[1:]}, None)[0] == "PASS"
    assert mod.judge_tests([ref], {nid: dup[:1]}, None)[0] == "PASS"
