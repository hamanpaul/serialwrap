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

pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]

CARGO_ARGV = ["--workspace", "--features", "test-fakes"]
PTY_ARGV = ["--package", "sw-pal", "--features", "test-fakes", "--test", "console_conformance", "--", "--ignored", "cap_posix_pty__"]
BB_ARGV = ["-q", "tests/contract_blackbox/slice22"]
WIN_ARGV = ["--features", "test-fakes"]

RAW_OWNER = "tests/contract_blackbox/slice22/test_console_pty.py::test_first_console_raw_second_line_buffered"
PARTIAL = "cargo:sw-core::human_tx_lane::human_raw_partial_not_resent"
PTY_CAP = "cargo:sw-pal::console_conformance::cap_posix_pty__mux_mixed_pty_and_tcp"
WIN_CORE = "cargo:sw-core::console_hub::close_all_on_release"
WIN_CAP = "cargo:sw-pal::console_conformance::cap_windows_com__console_com0com_open"


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
    }}


def _emap() -> dict:
    return {"schema_version": 1, "frozen_lock_sha1": "0" * 40, "cases": {
        "f5-second-console-linebuffer": {"status": "AUTOMATED", "tests": [RAW_OWNER]},
        "sc15-console-ring-drop-oldest": {"status": "AUTOMATED",
                                          "tests": ["crates/sw-core/tests/human_tx_lane.rs::human_raw_partial_not_resent"]},
        "p1-con-fanout": {"status": "BLOCKED-OWNER", "procedure": "docs/x.md", "evidence_schema": ["a.json"], "blocked_item": 5},
        "win-telnet-raw-owner": {"status": "NOT_APPLICABLE", "reason": "fixture"},
    }}


def _good_reports() -> dict[tuple[str, str], dict]:
    return {
        ("cargo_workspace", "linux"): _report("linux", "cargo", CARGO_ARGV, [_row(PARTIAL)]),
        ("console_conformance_pty", "linux"): _report("linux", "cargo", PTY_ARGV, [_row(PTY_CAP)]),
        ("blackbox_rust", "linux"): _report("linux", "pytest", BB_ARGV, [_row(RAW_OWNER)]),
        ("win_test", "windows"): _report("windows", "cargo", WIN_ARGV, [_row(WIN_CORE), _row(WIN_CAP, "skipped")]),
    }


def _run(tmp_path: Path, reports: dict, lock: dict | None = None, emap: dict | None = None) -> tuple[int, dict]:
    res = tmp_path / "l1"
    for (section, platform), rep in reports.items():
        out = res / section / platform / "resource-report.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(rep), encoding="utf-8")
    lock_path = tmp_path / "lock.json"
    lock_path.write_text(json.dumps(lock or _lock()), encoding="utf-8")
    map_path = tmp_path / "map.yaml"
    map_path.write_text(yaml.safe_dump(emap or _emap(), allow_unicode=True), encoding="utf-8")
    out_path = tmp_path / "gate.json"
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--l1-results", str(res), "--map", str(map_path), "--lock", str(lock_path),
         "--out", str(out_path)],
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


def test_module_importable_without_sw_core() -> None:
    spec = importlib.util.spec_from_file_location("s22_gate_check", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert callable(mod.main)
    src = SCRIPT.read_text(encoding="utf-8")
    assert "import sw_core" not in src and "from sw_core" not in src, "只用 stdlib＋PyYAML，判定委派 l1_judge"
