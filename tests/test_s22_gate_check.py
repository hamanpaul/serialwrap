"""P6 Task 13（R5-F3）：`scripts/rust/s22_gate_check.py` 的 Slice 2.2 出口判定。

合成 fixture（不讀最新報告）：`<dir>/<section>/<platform>/resource-report.json`（`l1-results.v1`）＋對照表＋L1 鎖。
判定規則：每個 `s22_gate:*` 集合以 P2 `tests/acceptance/l1_judge.judge` 判定；每個 `AUTOMATED` 條目的每個測試
須在報告中恰一列 `passed`；函式存在不等於已執行且通過。
"""
from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import os
import pathlib
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "rust" / "s22_gate_check.py"

pytestmark = [pytest.mark.process_rpc]

CARGO_ARGV = ["--workspace", "--features", "test-fakes"]
PTY_ARGV = ["--package", "sw-pal", "--features", "test-fakes", "--test", "console_conformance", "--", "--ignored", "cap_posix_pty__"]
BB_ARGV = ["-q", "tests/contract_blackbox/slice22"]
WIN_ARGV = ["--features", "test-fakes"]
WIN_COM0COM_ARGV = ["--package", "sw-pal", "--features", "test-fakes", "--test", "console_conformance",
                    "--", "--ignored", "cap_windows_com__"]
PY_ARGV = ["-q", "tests/test_s22_py_parity.py"]

RAW_OWNER = "tests/contract_blackbox/slice22/test_console_pty.py::test_first_console_raw_second_line_buffered"
PARTIAL = "cargo:sw-core::human_tx_lane::human_raw_partial_not_resent"
PTY_CAP = "cargo:sw-pal::console_conformance::cap_posix_pty__mux_mixed_pty_and_tcp"
WIN_CORE = "cargo:sw-core::console_hub::close_all_on_release"
WIN_CAP = "cargo:sw-pal::console_conformance::cap_windows_com__console_com0com_open"
WIN_COM0COM_CAPS = [
    "cargo:sw-pal::console_conformance::cap_windows_com__console_com0com_roundtrip",
    "cargo:sw-pal::console_conformance::cap_windows_com__console_com0com_exclusive",
    "cargo:sw-pal::console_conformance::cap_windows_com__console_com0com_no_modem_forward",
]
WIN_DAEMON_CONSOLE_ARGV = [
    "--package", "sw-daemon", "--features", "test-fakes", "--test", "windows_console_native",
    "--", "--ignored", "cap_windows_com__",
]
WIN_DAEMON_CONSOLE_TESTS = [
    "cargo:sw-daemon::windows_console_native::telnet_hub_end_to_end_with_fake_serial",
    "cargo:sw-daemon::windows_console_native::telnet_second_client_line_mode",
    "cargo:sw-daemon::windows_console_native::telnet_listener_ephemeral_per_bridge",
    "cargo:sw-daemon::windows_console_native::telnet_slow_client_bounded",
    "cargo:sw-daemon::windows_console_native::cap_windows_com__serial_console_end_to_end",
]
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
    report = {
        "schema_version": 2, "format": "l1-results.v1", "runner": runner, "platform": platform,
        # pytest 報告的 argv[0] 為程式入口（l1_judge `_report_argv` 會去掉）
        "argv": (["/x/pytest/__main__.py"] if runner == "pytest" else []) + list(argv), "markexpr": "", "counts": {}, "resources": {}, "uart_kind": "native",
        "not_collected_platform": [], "core_portable_platform_skips": [],
        "collected": [r["nodeid"] for r in rows], "deselected": [], "results": rows,
        "exit_code": exit_code, "finished": finished, "deployed_content": None,
    }
    if platform == "windows":
        head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
        report["provenance"] = {"git": {"head": head, "clean": True,
                                         "end": {"head": head, "clean": True}}}
    return report


def _lock(win_extra: tuple[str, ...] = ()) -> dict:
    def spec(argv, ids):
        return {"selector": {"argv": list(argv), "paths": [], "markexpr": ""}, "nodeids": sorted(ids)}

    return {"schema_version": 1, "sets": {
        "s22_gate:cargo_workspace": {"linux": spec(CARGO_ARGV, [PARTIAL])},
        "s22_gate:console_conformance_pty": {"linux": spec(PTY_ARGV, [PTY_CAP])},
        "s22_gate:blackbox_rust": {"linux": spec(BB_ARGV, [RAW_OWNER])},
        "s22_gate:win_test": {"windows": spec(WIN_ARGV, [WIN_CORE, *win_extra])},
        "s22_gate:windows_cap_windows_com": {"windows": spec(WIN_COM0COM_ARGV, WIN_COM0COM_CAPS)},
        "s22_gate:windows_cap_windows_com_daemon": {
            "windows": spec(WIN_DAEMON_CONSOLE_ARGV, WIN_DAEMON_CONSOLE_TESTS),
        },
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
        ("windows_cap_windows_com", "windows"): _report(
            "windows", "cargo", WIN_COM0COM_ARGV, [_row(nodeid) for nodeid in WIN_COM0COM_CAPS]
        ),
        ("windows_cap_windows_com_daemon", "windows"): _report(
            "windows", "cargo", WIN_DAEMON_CONSOLE_ARGV,
            [_row(nodeid) for nodeid in WIN_DAEMON_CONSOLE_TESTS],
        ),
        ("py_parity", "linux"): _report("linux", "pytest", PY_ARGV, [_row(PY_PARITY)]),
    }


def _run(tmp_path: Path, reports: dict, lock: dict | None = None, emap: dict | None = None, *,
         cases_lock: dict | None = None, l15: dict | None = None, prereqs_rc: int = 0,
         parity_doc: str = PARITY_DOC, daemon_path: Path | None = None,
         build_linux_exit: int | None = None, built_daemon_sha256: str | None = None,
         bridge_selftest_report: Path | None = None,
         dry_run: bool = False) -> tuple[int, dict]:
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
    identity_args = []
    if daemon_path is not None:
        identity_args += ["--daemon-path", str(daemon_path)]
    if build_linux_exit is not None:
        identity_args += ["--build-linux-exit", str(build_linux_exit)]
    if built_daemon_sha256 is not None:
        identity_args += ["--built-daemon-sha256", built_daemon_sha256]
    if bridge_selftest_report is not None:
        identity_args += ["--bridge-selftest-report", str(bridge_selftest_report)]
    if dry_run:
        identity_args += ["--dry-run"]
    out_path = tmp_path / "gate.json"
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--l1-results", str(res), "--map", str(map_path), "--lock", str(lock_path),
         "--cases-lock", str(cases_lock_path), "--parity-doc", str(parity_path), "--prereqs-script", str(prereqs),
         *extra, *identity_args, "--out", str(out_path)],
        capture_output=True, text=True, timeout=60,
    )
    assert out_path.is_file(), proc.stderr
    return proc.returncode, json.loads(out_path.read_text(encoding="utf-8"))


def _case(out: dict, cid: str) -> dict:
    return next(c for c in out["cases"] if c["case_id"] == cid)


def _section(out: dict, set_id: str, platform: str) -> dict:
    return next(s for s in out["sections"] if s["set_id"] == set_id and s["platform"] == platform)


@pytest.mark.core_portable
def test_existing_function_not_executed_is_insufficient(tmp_path: Path) -> None:
    reports = _good_reports()
    reports[("blackbox_rust", "linux")] = _report("linux", "pytest", BB_ARGV, [])
    lock = _lock()
    lock["sets"]["s22_gate:blackbox_rust"]["linux"]["nodeids"] = []
    rc, out = _run(tmp_path, reports, lock=lock)
    case = _case(out, "f5-second-console-linebuffer")
    assert case["status"] == "INSUFFICIENT" and RAW_OWNER in case["missing_tests"], case
    assert out["result"] == "INSUFFICIENT" and rc == 1


@pytest.mark.core_portable
def test_cargo_nonzero_count_is_not_enough(tmp_path: Path) -> None:
    reports = _good_reports()
    rows = [_row(f"cargo:sw-core::filler::t{i}") for i in range(200)]
    reports[("cargo_workspace", "linux")] = _report("linux", "cargo", CARGO_ARGV, rows)
    rc, out = _run(tmp_path, reports)
    assert _section(out, "s22_gate:cargo_workspace", "linux")["verdict"] == "INSUFFICIENT", out
    assert out["result"] == "INSUFFICIENT" and rc == 1


@pytest.mark.core_portable
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


@pytest.mark.core_portable
def test_runner_exit_nonzero_insufficient(tmp_path: Path) -> None:
    for i, kwargs in enumerate(({"exit_code": 101}, {"finished": False})):
        reports = _good_reports()
        reports[("cargo_workspace", "linux")] = _report("linux", "cargo", CARGO_ARGV, [_row(PARTIAL)], **kwargs)
        rc, out = _run(tmp_path / str(i), reports)
        assert _section(out, "s22_gate:cargo_workspace", "linux")["verdict"] == "INSUFFICIENT", kwargs
        assert out["result"] == "INSUFFICIENT" and rc == 1


@pytest.mark.core_portable
def test_cap_windows_com_absent_is_blocked_not_pass(tmp_path: Path) -> None:
    rc, out = _run(tmp_path / "a", _good_reports())
    assert _section(out, "s22_gate:win_test", "windows")["verdict"] == "PASS"
    assert rc == 0
    rc, out = _run(tmp_path / "b", _good_reports(), lock=_lock(win_extra=(WIN_CAP,)))
    assert _section(out, "s22_gate:win_test", "windows")["verdict"] == "INSUFFICIENT", "不得以 ignored 充數"
    assert rc == 1


@pytest.mark.core_portable
def test_windows_com_console_cap_requires_native_evidence(tmp_path: Path) -> None:
    reports = _good_reports()
    del reports[("windows_cap_windows_com", "windows")]
    rc, out = _run(tmp_path, reports)
    section = _section(out, "s22_gate:windows_cap_windows_com", "windows")
    assert section["verdict"] == "EVIDENCE_MISSING", section
    assert out["result"] == "INSUFFICIENT" and rc == 1

    windows_nodes = {
        "win_test": WIN_CORE,
        "windows_cap_windows_com": WIN_COM0COM_CAPS[0],
        "windows_cap_windows_com_daemon": WIN_DAEMON_CONSOLE_TESTS[0],
    }
    for set_name, nodeid in windows_nodes.items():
        reports = _good_reports()
        reports[(set_name, "windows")]["provenance"]["git"]["head"] = "f" * 40
        emap = _emap()
        emap["cases"][f"stale-windows-{set_name}"] = {"status": "AUTOMATED", "tests": [nodeid]}
        rc, out = _run(tmp_path / f"stale-{set_name}", reports, emap=emap)
        section = _section(out, f"s22_gate:{set_name}", "windows")
        assert section["verdict"] == "EVIDENCE_MISSING", section
        assert any("provenance.git.head" in reason for reason in section["reasons"]), section
        assert _case(out, f"stale-windows-{set_name}")["status"] == "INSUFFICIENT", out
        assert out["result"] == "INSUFFICIENT" and rc == 1


@pytest.mark.core_portable
def test_windows_daemon_console_cap_requires_native_evidence(tmp_path: Path) -> None:
    reports = _good_reports()
    del reports[("windows_cap_windows_com_daemon", "windows")]
    rc, out = _run(tmp_path, reports)
    section = _section(out, "s22_gate:windows_cap_windows_com_daemon", "windows")
    assert section["verdict"] == "EVIDENCE_MISSING", section
    assert out["result"] == "INSUFFICIENT" and rc == 1


@pytest.mark.core_portable
def test_all_present_pass(tmp_path: Path) -> None:
    rc, out = _run(tmp_path, _good_reports())
    assert out["result"] == "PASS" and rc == 0, out
    assert _case(out, "f5-second-console-linebuffer")["status"] == "PASS"
    assert _case(out, "sc15-console-ring-drop-oldest")["status"] == "PASS"
    assert _case(out, "p1-con-fanout")["status"] == "BLOCKED-OWNER", "BLOCKED-OWNER 不得標 PASS"
    assert all(s["verdict"] == "PASS" for s in out["sections"])


@pytest.mark.core_portable
def test_od87_breaker_state_is_reported_without_blocking_s22(tmp_path: Path) -> None:
    rc, out = _run(tmp_path, _good_reports())
    assert rc == 0 and out["result"] == "PASS", out
    assert out["breaker"]["actual_verdict"] == "INSUFFICIENT", out["breaker"]
    assert out["breaker"]["assessment"]["insufficient_evidence"] is True, out["breaker"]
    assert out["breaker"]["gate_verdict"] == "OD-87_EXEMPT", out["breaker"]
    assert out["breaker"]["exemption"]["decision"] == "OD-87", out["breaker"]


@pytest.mark.core_portable
def test_s22_checker_does_not_modify_tracked_breaker_inputs(tmp_path: Path) -> None:
    tracked_inputs = [
        ROOT / "tests/acceptance/breaker/slice_2.2.json",
        ROOT / "tests/acceptance/breaker/slice_2.2.snapshots.ndjson",
    ]
    before = {path: path.read_bytes() for path in tracked_inputs}
    rc, out = _run(tmp_path, _good_reports())
    assert rc == 0 and out["result"] == "PASS", out
    assert {path: path.read_bytes() for path in tracked_inputs} == before
    assert (tmp_path / "gate.json").is_file()

    driver = (ROOT / "scripts/rust/s22_gate.sh").read_text(encoding="utf-8")
    checker = SCRIPT.read_text(encoding="utf-8")
    assert 'OUT_ROOT="$ROOT/target/s22-gate"' in driver
    assert 'logs_dir = run_dir / "logs"' in driver
    assert 'L1_DIR="$RUN_DIR/l1-results"' in driver
    assert 'l15_path = run_dir / "l15-results.json"' in driver
    assert 'report = run_dir / "s22-gate.json"' in driver
    assert checker.count(".write_text(") == 1 and "args.out.write_text(" in checker


@pytest.mark.core_portable
def test_missing_report_and_pending_entry_insufficient(tmp_path: Path) -> None:
    reports = _good_reports()
    del reports[("win_test", "windows")]
    emap = _emap()
    emap["cases"]["bc02-normal-replay-53"] = {"status": "PENDING"}
    rc, out = _run(tmp_path, reports, emap=emap)
    assert _section(out, "s22_gate:win_test", "windows")["verdict"] == "EVIDENCE_MISSING"
    assert _case(out, "bc02-normal-replay-53")["status"] == "INSUFFICIENT"
    assert out["result"] == "INSUFFICIENT" and rc == 1


@pytest.mark.core_portable
def test_s22_gate_driver_dry_run_emits_incomplete_json(tmp_path: Path) -> None:
    run = ROOT / "target" / "s2-gfx" / "tests" / tmp_path.parent.name / tmp_path.name / "s22"
    proc = subprocess.run(["bash", "scripts/rust/s22_gate.sh", "--dry-run", "--run-id", "dry", "--out", str(run)],
                          cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    report_path = run / "dry" / "s22-gate.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["dry_run"] is True
    assert report["result"] == "INSUFFICIENT"
    assert _section(report, "s22_gate:win_test", "windows")["verdict"] == "EVIDENCE_MISSING"
    shutil.rmtree(run, ignore_errors=True)


@pytest.mark.core_portable
def test_unchecked_only_case_is_insufficient(tmp_path: Path) -> None:
    """審查 3317987_d6e3b3c-opus High #1：只含 func-test／scenario 參照的 AUTOMATED 條目沒有 L1.5 執行證據時不得 PASS。"""
    emap = _emap()
    emap["cases"]["co-01-line-buffering"] = {"status": "AUTOMATED", "tests": [FUNC_REF]}
    rc, out = _run(tmp_path / "a", _good_reports(), emap=emap)
    case = _case(out, "co-01-line-buffering")
    assert case["status"] == "INSUFFICIENT" and case["unchecked_tests"] == [FUNC_REF], case
    assert out["result"] == "INSUFFICIENT" and rc == 1
    driver = (ROOT / "scripts/rust/s22_gate.sh").read_text(encoding="utf-8")
    assert 'env["SERIALWRAP_DAEMON_EXE"] = str(path)' in driver
    assert '"daemon_sha256"' in driver and '"implementation"' in driver

    def run_row(directory: Path, implementation: str, outcome: str = "passed", *, missing: bool = False) -> dict:
        artifact = directory / f"{implementation}-daemon"
        if not missing:
            artifact, digest = _write_l15_artifact(artifact, implementation)
        else:
            artifact.parent.mkdir(parents=True, exist_ok=True)
            digest = "0" * 64
        return {"ref": FUNC_REF, "implementation": implementation, "outcome": outcome,
                "exit_code": 0 if outcome == "passed" else 1,
                "daemon_path": str(artifact), "daemon_sha256": digest}

    passed = {"format": "s22-l15-results.v1", "runs": [
        run_row(tmp_path / "daemons", "python"), run_row(tmp_path / "daemons", "rust")
    ]}
    rust_run = next(row for row in passed["runs"] if row["implementation"] == "rust")
    candidate_path = Path(rust_run["daemon_path"])
    rc, out = _run(tmp_path / "b", _good_reports(), emap=emap, l15=passed,
                   daemon_path=candidate_path, build_linux_exit=0,
                   built_daemon_sha256=rust_run["daemon_sha256"])
    assert _case(out, "co-01-line-buffering")["status"] == "PASS" and rc == 0, out
    failed = {"format": "s22-l15-results.v1", "runs": [
        run_row(tmp_path / "failed-daemons", "python"),
        run_row(tmp_path / "failed-daemons", "rust", "failed"),
    ]}
    rc, out = _run(tmp_path / "c", _good_reports(), emap=emap, l15=failed)
    assert _case(out, "co-01-line-buffering")["status"] == "FAIL" and out["result"] == "FAIL" and rc == 1
    bad_rust = {"format": "s22-l15-results.v1", "runs": [
        run_row(tmp_path / "bad-daemons", "python"),
        run_row(tmp_path / "bad-daemons", "rust", missing=True),
    ]}
    rc, out = _run(tmp_path / "bad-rust", _good_reports(), emap=emap, l15=bad_rust)
    assert _case(out, "co-01-line-buffering")["status"] != "PASS" and rc == 1, out
    dup = {"format": "s22-l15-results.v1", "runs": [passed["runs"][0], passed["runs"][0]]}
    rc, out = _run(tmp_path / "d", _good_reports(), emap=emap, l15=dup)
    assert _case(out, "co-01-line-buffering")["status"] == "INSUFFICIENT" and rc == 1


def _write_l15_artifact(path: Path, kind: str) -> tuple[Path, str]:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = (b"#!/bin/sh\nexec python3 ./serialwrapd.py \"$@\"\n" if kind == "python"
               else b"\x7fELF rust-daemon fixture")
    path.write_bytes(content)
    return path, hashlib.sha256(content).hexdigest()


def _l15_run(ref: str, implementation: str, path: Path, digest: str) -> dict:
    return {"ref": ref, "implementation": implementation, "outcome": "passed", "exit_code": 0,
            "daemon_path": str(path), "daemon_sha256": digest}


@pytest.mark.core_portable
def test_l15_rejects_two_python_launchers_labeled_python_and_rust(tmp_path: Path) -> None:
    """L1.5 身分取自實際 artifact；兩份不同路徑的 Python wrapper 不能偽裝成 Python＋Rust。"""
    emap = _emap()
    emap["cases"]["co-01-line-buffering"] = {"status": "AUTOMATED", "tests": [FUNC_REF]}
    py_path, py_sha = _write_l15_artifact(tmp_path / "python" / "serialwrapd", "python")
    rust_path, rust_sha = _write_l15_artifact(tmp_path / "rust-copy" / "serialwrapd", "python")
    l15 = {"format": "s22-l15-results.v1", "runs": [
        _l15_run(FUNC_REF, "python", py_path, py_sha),
        _l15_run(FUNC_REF, "rust", rust_path, rust_sha),
    ]}

    rc, out = _run(tmp_path / "run", _good_reports(), emap=emap, l15=l15)
    case = _case(out, "co-01-line-buffering")
    assert case["status"] == "INSUFFICIENT" and rc == 1, case
    assert any("實際 artifact 身分" in reason for reason in out["l15_reasons"]), out["l15_reasons"]


@pytest.mark.core_portable
def test_l15_rust_artifact_must_match_reported_candidate(tmp_path: Path) -> None:
    """L1.5 的 Rust 證據必須綁定 --daemon-path 指定的候選 artifact。"""
    emap = _emap()
    emap["cases"]["co-01-line-buffering"] = {"status": "AUTOMATED", "tests": [FUNC_REF]}
    py_path, py_sha = _write_l15_artifact(tmp_path / "python" / "serialwrapd", "python")
    candidate, candidate_sha = _write_l15_artifact(tmp_path / "candidate" / "serialwrapd", "rust")
    stale, stale_sha = _write_l15_artifact(tmp_path / "old-target" / "serialwrapd", "rust")
    l15 = {"format": "s22-l15-results.v1", "runs": [
        _l15_run(FUNC_REF, "python", py_path, py_sha),
        _l15_run(FUNC_REF, "rust", stale, stale_sha),
    ]}

    rc, out = _run(tmp_path / "run", _good_reports(), emap=emap, l15=l15,
                   daemon_path=candidate, build_linux_exit=0, built_daemon_sha256=candidate_sha)
    case = _case(out, "co-01-line-buffering")
    assert case["status"] == "INSUFFICIENT" and rc == 1, case
    assert any("--daemon-path 候選不同" in reason for reason in out["l15_reasons"]), out["l15_reasons"]


@pytest.mark.core_portable
def test_l15_rust_requires_successful_candidate_build_identity(tmp_path: Path) -> None:
    """缺 build exit 或建置 SHA 時，不能把 Rust L1.5 列當成候選版通過證據。"""
    emap = _emap()
    emap["cases"]["co-01-line-buffering"] = {"status": "AUTOMATED", "tests": [FUNC_REF]}
    py_path, py_sha = _write_l15_artifact(tmp_path / "python" / "serialwrapd", "python")
    candidate, candidate_sha = _write_l15_artifact(tmp_path / "candidate" / "serialwrapd", "rust")
    l15 = {"format": "s22-l15-results.v1", "runs": [
        _l15_run(FUNC_REF, "python", py_path, py_sha),
        _l15_run(FUNC_REF, "rust", candidate, candidate_sha),
    ]}

    rc, out = _run(tmp_path / "run", _good_reports(), emap=emap, l15=l15, daemon_path=candidate)
    case = _case(out, "co-01-line-buffering")
    assert case["status"] == "INSUFFICIENT" and rc == 1, case
    assert any("建置" in reason and "L1.5 Rust" in reason for reason in out["l15_reasons"]), out["l15_reasons"]


@pytest.mark.core_portable
def test_missing_required_sets_insufficient(tmp_path: Path) -> None:
    """審查 Medium #2：lock 缺任一必要 `s22_gate:*` 集合（例：win_test、blackbox_rust、py_parity）→ INSUFFICIENT。"""
    for i, missing in enumerate(("s22_gate:win_test", "s22_gate:blackbox_rust", "s22_gate:py_parity")):
        lock = _lock()
        del lock["sets"][missing]
        rc, out = _run(tmp_path / str(i), _good_reports(), lock=lock)
        expected = "EVIDENCE_MISSING" if missing == "s22_gate:win_test" else "INSUFFICIENT"
        assert any(s["set_id"] == missing and s["verdict"] == expected for s in out["sections"]), (missing, out)
        assert out["result"] == "INSUFFICIENT" and rc == 1


@pytest.mark.core_portable
def test_map_must_cover_frozen_slice22(tmp_path: Path) -> None:
    """審查 Medium #2：對照表鍵集合須等於凍結 slice_2.2；`cases: {}` 或缺鍵不得 PASS。"""
    emap = _emap()
    rc, out = _run(tmp_path / "a", _good_reports(), emap=emap,
                   cases_lock={"sets": {"slice_2.2": sorted(emap["cases"]) + ["extra-frozen-case"]}})
    assert out["map_coverage"]["missing_cases"] == ["extra-frozen-case"] and out["result"] == "INSUFFICIENT" and rc == 1
    empty = dict(_emap(), cases={})
    rc, out = _run(tmp_path / "b", _good_reports(), emap=empty, cases_lock={"sets": {"slice_2.2": ["x"]}})
    assert out["result"] == "INSUFFICIENT" and rc == 1


@pytest.mark.core_portable
def test_prereqs_strict_failure_insufficient(tmp_path: Path) -> None:
    """增補 #180：`check_p6_prereqs.py --strict` 非 0 → INSUFFICIENT。"""
    rc, out = _run(tmp_path, _good_reports(), prereqs_rc=1)
    assert out["prereqs"]["verdict"] == "INSUFFICIENT" and out["prereqs"]["exit_code"] == 1
    assert out["result"] == "INSUFFICIENT" and rc == 1


@pytest.mark.core_portable
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


@pytest.mark.core_portable
def test_module_importable_without_sw_core() -> None:
    spec = importlib.util.spec_from_file_location("s22_gate_check", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert callable(mod.main)
    src = SCRIPT.read_text(encoding="utf-8")
    assert "import sw_core" not in src and "from sw_core" not in src, "只用 stdlib＋PyYAML，判定委派 l1_judge"


@pytest.mark.core_portable
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


@pytest.mark.core_portable
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


@pytest.mark.core_portable
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


def _process_report(red: dict | None = None, green: dict | None = None, heading: str = "## #185 T0 單獨紅燈") -> str:
    red = {"commit": "ae33e28", "command": "python3 -m pytest -q tests/a.py", "exit_code": 1, "passed": 9, "failed": 4,
           "failed_tests": ["tests/a.py::test_x"]} if red is None else red
    green = {"commit": "5ade76c", "command": "python3 -m pytest -q tests/a.py", "exit_code": 0, "passed": 13,
             "failed": 0} if green is None else green
    block = json.dumps({"red": red, "green": green}, ensure_ascii=False, indent=1)
    return f"# 出口報告\n\n{heading}\n\n說明文字：紅燈、綠燈。\n\n```json process-evidence\n{block}\n```\n\n## 其他\n"


@pytest.mark.core_portable
def test_process_evidence_row_requires_structured_red_and_green(tmp_path: Path) -> None:
    """rulings/p6.md OWNER-BATCH #4（#185 證據形式）＋sol P6-b64dad9-1：流程項以出口報告 `## #185` 段落內的
    `json process-evidence` 區塊為證據（不以說明文字關鍵字判定）。紅燈須為核准的 commit、非零退出、failed>0 且列出失敗測試；
    綠燈須為另一 commit、同一命令、退出碼 0、failed==0、passed>0。缺區塊、缺綠燈、綠燈失敗、缺檔、非逐名核准者皆 INSUFFICIENT。"""
    doc = PARITY_DOC.replace("| 179 | F1 | Medium | gate | 07 Task 1 |",
                             "| 179 | F1 | Medium | gate | 07 Task 1 |\n| 185 | F3 | Medium | T0 紅燈 | 07 Task 0 |")
    report = tmp_path / "exit-report.md"
    emap = _emap()
    emap["parity_rows"]["#185"] = {"status": "PROCESS-EVIDENCE", "report": str(report), "commit": "ae33e28"}

    def verdict(text: str, sub: str) -> str:
        report.write_text(text, encoding="utf-8")
        _rc, out = _run(tmp_path / sub, _good_reports(), emap=emap, parity_doc=doc)
        return next(r for r in out["parity_rows"] if r["row"] == "#185")["verdict"]

    assert verdict(_process_report(), "ok") == "PASS"
    good_green = {"commit": "5ade76c", "command": "python3 -m pytest -q tests/a.py", "exit_code": 0, "passed": 13, "failed": 0}
    bad = {
        "no_block": "# r\n\n## #185 T0\n\nT0 commit `ae33e28` 紅燈 4 failed；終態綠燈 13 passed。\n",
        "no_green": _process_report(green={}),
        "green_failed": _process_report(green={**good_green, "passed": 0, "failed": 13, "exit_code": 1}),
        "green_nonzero_exit": _process_report(green={**good_green, "exit_code": 2}),
        "green_other_command": _process_report(green={**good_green, "command": "python3 -m pytest -q tests/b.py"}),
        "green_same_commit": _process_report(green={**good_green, "commit": "ae33e28"}),
        "red_other_commit": _process_report(red={"commit": "deadbee", "command": good_green["command"], "exit_code": 1,
                                                 "passed": 9, "failed": 4, "failed_tests": ["tests/a.py::test_x"]}),
        "red_not_red": _process_report(red={"commit": "ae33e28", "command": good_green["command"], "exit_code": 0,
                                            "passed": 13, "failed": 0, "failed_tests": []}),
        "red_no_tests": _process_report(red={"commit": "ae33e28", "command": good_green["command"], "exit_code": 1,
                                             "passed": 9, "failed": 4, "failed_tests": []}),
        "wrong_section": _process_report(heading="## #186 其他"),
        "bad_json": _process_report().replace('"red"', "'red'"),
    }
    for name, text in bad.items():
        assert verdict(text, name) == "INSUFFICIENT", name
    report.write_text(_process_report(), encoding="utf-8")
    emap["parity_rows"]["#185"]["report"] = str(tmp_path / "missing.md")
    _rc, out = _run(tmp_path / "missing", _good_reports(), emap=emap, parity_doc=doc)
    assert next(r for r in out["parity_rows"] if r["row"] == "#185")["verdict"] == "INSUFFICIENT"
    # 非逐名核准的承接編號不得以 PROCESS-EVIDENCE 充當證據
    emap = _emap()
    emap["parity_rows"]["#179"] = {"status": "PROCESS-EVIDENCE", "report": str(report), "commit": "ae33e28"}
    rc, out = _run(tmp_path / "d", _good_reports(), emap=emap)
    row = next(r for r in out["parity_rows"] if r["row"] == "#179")
    assert row["verdict"] == "INSUFFICIENT" and rc == 1, row


def _gate_module():
    spec = importlib.util.spec_from_file_location("s22_gate_check", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.core_portable
def test_derive_caps_from_selector_unit() -> None:
    """rulings/p6.md「OD-53 selector 記錄 caps」：不改鎖 schema，由現有 selector 推導收集器 `--caps`。
    test-binary 參數（`--` 之後）恰為 `--ignored cap_<cap>__…` → `--caps <cap>…`；不含 ignored 篩選 → 不需 caps；
    其餘帶 ignored 意圖者（`--include-ignored`、`--ignored` 無篩選、混有非 cap 篩選或其他旗標、`--` 之前）推導不出（None）。"""
    mod = _gate_module()
    assert mod.derive_caps({"argv": PTY_ARGV}) == ["posix_pty"]
    assert mod.derive_caps({"argv": ["-p", "x", "--", "--ignored", "cap_posix_pty__", "cap_windows_com__"]}) == [
        "posix_pty", "windows_com"]
    assert mod.derive_caps({"argv": [*CARGO_ARGV, "--", "--skip", "cap_"]}) == []
    assert mod.derive_caps({"argv": CARGO_ARGV}) == []
    assert mod.derive_caps({"argv": BB_ARGV}) == []
    for tail in (["--ignored"], ["--include-ignored"], ["--include-ignored", "cap_posix_pty__"],
                 ["--ignored", "cap_posix_pty__", "other_filter"], ["--ignored", "cap_posix_pty"],
                 ["--ignored", "--skip", "x", "cap_posix_pty__"], ["--ignored", "cap_posix_pty__", "--exact"]):
        assert mod.derive_caps({"argv": ["-p", "x", "--", *tail]}) is None, tail
    assert mod.derive_caps({"argv": ["--ignored", "cap_posix_pty__"]}) is None, "`--` 之前的 --ignored 無法以 --caps 重現"


@pytest.mark.core_portable
def test_real_lock_s22_cargo_sets_caps_derivable() -> None:
    """現行 L1 鎖的每個 `s22_gate:*` 集合皆可推導：`console_conformance_pty` → `posix_pty`，其餘不需 caps。"""
    mod = _gate_module()
    lock = json.loads((ROOT / "tests" / "acceptance" / "l1_required.lock.json").read_text(encoding="utf-8"))
    derived = {(sid, plat): mod.derive_caps(spec["selector"])
               for sid, plats in lock["sets"].items() if sid.startswith("s22_gate:") for plat, spec in plats.items()}
    assert derived[("s22_gate:console_conformance_pty", "linux")] == ["posix_pty"], derived
    assert derived[("s22_gate:cargo_workspace", "linux")] == [], derived
    assert derived[("s22_gate:windows_cap_windows_com_daemon", "windows")] == ["windows_com"], derived
    assert all(v is not None for v in derived.values()), derived


@pytest.mark.core_portable
def test_underivable_caps_selector_is_evidence_missing(tmp_path: Path) -> None:
    """推導不出 `--caps` 的集合（例：計畫 07 Task 13 Step 4 原文 `--include-ignored cap_posix_pty__`，收集器不轉交）→
    該集合 EVIDENCE_MISSING、result 不得 PASS，即使報告各列皆 passed、argv 與 selector 相符；該報告的 passed 列不得
    充當逐案證據，failed 列仍判 FAIL。可推導時輸出推導的 caps 與收集命令（`cargo_l1_results.py --caps <cap>`）。"""
    bad_argv = [*PTY_ARGV[:-2], "--include-ignored", "cap_posix_pty__"]
    lock = _lock()
    lock["sets"]["s22_gate:console_conformance_pty"]["linux"]["selector"]["argv"] = bad_argv
    cap_ref = "crates/sw-pal/tests/console_conformance.rs::cap_posix_pty__mux_mixed_pty_and_tcp"
    emap = _emap()
    emap["cases"]["sc28-cap-mux"] = {"status": "AUTOMATED", "tests": [cap_ref]}
    reports = _good_reports()
    reports[("console_conformance_pty", "linux")] = _report("linux", "cargo", bad_argv, [_row(PTY_CAP)])
    rc, out = _run(tmp_path / "a", reports, lock=lock, emap=emap)
    sec = _section(out, "s22_gate:console_conformance_pty", "linux")
    assert sec["verdict"] == "EVIDENCE_MISSING" and sec["caps"] is None and sec["collect_argv"] is None, sec
    case = _case(out, "sc28-cap-mux")
    assert case["status"] == "INSUFFICIENT" and cap_ref in case["missing_tests"], case
    assert out["result"] == "INSUFFICIENT" and rc == 1

    reports[("console_conformance_pty", "linux")] = _report(
        "linux", "cargo", bad_argv, [_row(PTY_CAP, "failed")], exit_code=101)
    rc, out = _run(tmp_path / "b", reports, lock=lock, emap=emap)
    assert _case(out, "sc28-cap-mux")["status"] == "FAIL" and out["result"] == "FAIL" and rc == 1, out

    rc, out = _run(tmp_path / "c", _good_reports(), emap=emap)
    sec = _section(out, "s22_gate:console_conformance_pty", "linux")
    assert sec["verdict"] == "PASS" and sec["caps"] == ["posix_pty"], sec
    argv = sec["collect_argv"]
    assert argv[:2] == ["python3", "scripts/rust/cargo_l1_results.py"] and argv[argv.index("--caps") + 1] == "posix_pty"
    assert argv[argv.index("--") + 1:] == PTY_ARGV, argv
    work = _section(out, "s22_gate:cargo_workspace", "linux")
    assert work["caps"] == [] and "--caps" not in work["collect_argv"], work
    assert _section(out, "s22_gate:blackbox_rust", "linux")["collect_argv"] is None, "pytest 集合不由 cargo 收集器收集"
    assert _case(out, "sc28-cap-mux")["status"] == "PASS" and out["result"] == "PASS" and rc == 0, out


@pytest.mark.core_portable
def test_plan_lists_derived_cargo_collect_commands(tmp_path: Path) -> None:
    """`--plan`：只依 L1 鎖輸出各 cargo `s22_gate:*` 集合的收集命令（前置收集由 gate 推導，不手打 `--caps`）；
    推導不出者 `collect_argv` 為 null、exit 1。Windows 集合由 win-test.sh 原生收集，只列 caps。"""
    def plan(lock: dict, sub: str) -> tuple[int, list[dict]]:
        lock_path = tmp_path / f"{sub}.json"
        lock_path.write_text(json.dumps(lock), encoding="utf-8")
        proc = subprocess.run([sys.executable, str(SCRIPT), "--plan", "--l1-results", str(tmp_path / sub),
                               "--lock", str(lock_path)], capture_output=True, text=True, timeout=60)
        return proc.returncode, json.loads(proc.stdout)

    rc, entries = plan(_lock(), "ok")
    by_set = {(e["set_id"], e["platform"]): e for e in entries}
    assert set(by_set) == {("s22_gate:cargo_workspace", "linux"), ("s22_gate:console_conformance_pty", "linux"),
                           ("s22_gate:win_test", "windows"),
                           ("s22_gate:windows_cap_windows_com", "windows"),
                           ("s22_gate:windows_cap_windows_com_daemon", "windows")}, entries
    cap_entry = by_set[("s22_gate:console_conformance_pty", "linux")]
    assert cap_entry["caps"] == ["posix_pty"] and "--caps" in cap_entry["collect_argv"]
    out_arg = cap_entry["collect_argv"][cap_entry["collect_argv"].index("--out") + 1]
    assert out_arg == str(tmp_path / "ok" / "console_conformance_pty" / "linux" / "resource-report.json")
    assert by_set[("s22_gate:win_test", "windows")]["collect_argv"] is None
    assert by_set[("s22_gate:windows_cap_windows_com", "windows")]["caps"] == ["windows_com"]
    assert by_set[("s22_gate:windows_cap_windows_com", "windows")]["collect_argv"] is None
    daemon_cap = by_set[("s22_gate:windows_cap_windows_com_daemon", "windows")]
    assert daemon_cap["caps"] == ["windows_com"] and daemon_cap["collect_argv"] is None
    assert rc == 0
    bad = _lock()
    bad["sets"]["s22_gate:console_conformance_pty"]["linux"]["selector"]["argv"] = [*PTY_ARGV[:-2], "--include-ignored"]
    rc, entries = plan(bad, "bad")
    entry = next(e for e in entries if e["set_id"] == "s22_gate:console_conformance_pty")
    assert entry["caps"] is None and entry["collect_argv"] is None and rc == 1, entries


@pytest.mark.core_portable
def test_gate_output_records_identity_without_changing_verdict(tmp_path: Path) -> None:
    """S22 身分供 P7 綁定先前切片證據；缺少產物時自檢列出缺漏，但不可改寫既有 gate 判定。"""
    rc, out = _run(tmp_path, _good_reports())

    assert out["result"] == "PASS" and rc == 0, out
    assert out["dry_run"] is False
    assert out["identity"]["git_revision"] == subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    assert out["identity"]["daemon_sha256"] is None
    assert "identity.daemon_sha256" in out["identity_problems"]["missing"]


@pytest.mark.core_portable
def test_identity_self_check_requires_matching_tested_and_release_daemon_sha(tmp_path: Path) -> None:
    """缺來源身分或實際受測 artifact 與獨立 release build SHA 不符時，identity 自檢必須明確拒絕。"""
    mod = _gate_module()
    missing, mismatch = mod.identity_problems(None, None)
    assert "identity 缺失" in missing and not mismatch

    daemon = tmp_path / "serialwrapd"
    daemon.write_bytes(b"release daemon artifact")
    actual_sha = hashlib.sha256(daemon.read_bytes()).hexdigest()
    good = {
        "git_revision": "a" * 40,
        "daemon_sha256": actual_sha,
        "build": {"linux_exit": 0, "daemon_sha256_at_build": actual_sha},
    }
    missing, mismatch = mod.identity_problems(good, daemon)
    assert not missing and not mismatch

    wrong_build = {**good, "build": {"linux_exit": 0, "daemon_sha256_at_build": "e" * 64}}
    missing, mismatch = mod.identity_problems(wrong_build, daemon)
    assert not missing and any("建置當下" in reason for reason in mismatch), mismatch

    wrong_artifact = {**good, "daemon_sha256": "f" * 64}
    missing, mismatch = mod.identity_problems(wrong_artifact, daemon)
    assert not missing and any("實際 sha256" in reason for reason in mismatch), mismatch


@pytest.mark.core_portable
def test_gate_output_binds_release_sha_and_dry_run_cannot_supply_tested_sha(tmp_path: Path) -> None:
    """受測 daemon hash 與 build 快照分開輸出；dry-run 有標記且不提供可被 P7 採用的 daemon SHA。"""
    daemon = tmp_path / "release" / "serialwrapd"
    daemon.parent.mkdir()
    daemon.write_bytes(b"release daemon")
    sha = hashlib.sha256(daemon.read_bytes()).hexdigest()

    rc, out = _run(tmp_path / "run", _good_reports(), daemon_path=daemon, build_linux_exit=0,
                   built_daemon_sha256=sha)
    assert out["result"] == "PASS" and rc == 0, out
    assert out["dry_run"] is False
    assert out["identity"]["daemon_sha256"] == sha
    assert out["identity"]["build"]["daemon_sha256_at_build"] == sha
    assert out["identity"]["build"]["command"] == [
        "cargo", "build", "--release", "-p", "sw-daemon", "-p", "sw-cli",
        "--target", "x86_64-unknown-linux-gnu",
    ]
    assert out["identity_problems"] == {"missing": [], "mismatch": []}

    rc, out = _run(tmp_path / "mismatch", _good_reports(), daemon_path=daemon, build_linux_exit=0,
                   built_daemon_sha256="e" * 64)
    assert out["result"] == "PASS" and rc == 0, out
    assert out["identity"]["daemon_sha256"] is None, "SHA 不符時 P7 不得拿到可採用的來源 SHA"
    assert any("建置當下" in reason for reason in out["identity_problems"]["mismatch"]), out

    rc, out = _run(tmp_path / "dry", _good_reports(), daemon_path=daemon, build_linux_exit=0,
                   built_daemon_sha256=sha, dry_run=True)
    assert out["result"] == "PASS" and rc == 0, out
    assert out["dry_run"] is True
    assert out["identity"]["daemon_sha256"] is None
    assert "identity.daemon_sha256" in out["identity_problems"]["missing"]


@pytest.mark.core_portable
def test_plan_passes_actual_release_build_exit_to_identity() -> None:
    """OD-93／s22-17：Task 13 Step 4 以共用 Stage 2 release artifact 作為候選來源，不重建裸 daemon。"""
    plan = (ROOT / "docs" / "rust-refactor" / "plans" / "07-slice22-console.md").read_text(encoding="utf-8")

    assert 'SHARED_EVIDENCE="target/s2-shared-evidence/$RUN_ID"' in plan
    assert 'scripts/rust/gate_shared.sh --run-id "$RUN_ID"' in plan
    assert 'scripts/rust/s22_gate.sh --shared-evidence "$SHARED_EVIDENCE" --run-id "$RUN_ID"' in plan
    assert "cargo build --release -p sw-daemon -p sw-cli" not in plan


def _shared_evidence_module():
    path = ROOT / "scripts/rust/gate_shared_evidence.py"
    spec = importlib.util.spec_from_file_location("gate_shared_evidence_s22_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _s22_stale_report(tmp_path: Path, section: str, platform: str) -> tuple[Path, dict, dict]:
    lock = json.loads((ROOT / "tests/acceptance/l1_required.lock.json").read_text(encoding="utf-8"))
    set_id = f"s22_gate:{section}"
    locked = lock["sets"][set_id][platform]
    report = _report(platform, "cargo", locked["selector"]["argv"],
                     [_row(nodeid) for nodeid in locked["nodeids"]])
    path = tmp_path / "stale-l1" / section / platform / "resource-report.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(report), encoding="utf-8")

    judge_spec = importlib.util.spec_from_file_location("s22_l1_judge", ROOT / "tests/acceptance/l1_judge.py")
    assert judge_spec is not None and judge_spec.loader is not None
    judge = importlib.util.module_from_spec(judge_spec)
    judge_spec.loader.exec_module(judge)
    judged = judge.judge(report, locked["nodeids"], platform=platform, selector=locked["selector"])
    assert judged["verdict"] == "PASS", judged
    return path, lock, report


def _shared_s22_import_fixture(tmp_path: Path, section: str, *, bad_selector: bool = False,
                               bad_sha: bool = False) -> Path:
    shared_api = _shared_evidence_module()
    shared = tmp_path / "shared-evidence"
    artifact_sources = {}
    for name in ("debug_cli", "debug_daemon", "release_cli", "release_daemon"):
        path = tmp_path / "candidate-artifacts" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"candidate:{name}".encode())
        artifact_sources[name] = path
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    shared_api.init_manifest(shared, artifact_sources, head=head, clean=True)

    set_id = f"s22_gate:{section}"
    lock = json.loads((ROOT / "tests/acceptance/l1_required.lock.json").read_text(encoding="utf-8"))
    locked = lock["sets"][set_id]["linux" if section == "cargo_workspace" else "windows"]
    report = _report("linux" if section == "cargo_workspace" else "windows", "cargo",
                     locked["selector"]["argv"], [_row(nodeid) for nodeid in locked["nodeids"]])
    if section != "cargo_workspace":
        head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
        report["provenance"] = {
            "schema": "cargo-l1-provenance.v1", "platform": "windows",
            "build_target": "x86_64-pc-windows-gnu",
            "git": {"head": head, "clean": True, "end": {"head": head, "clean": True}},
        }
    report_source = tmp_path / "reports" / "l1-results.json"
    report_source.parent.mkdir()
    report_source.write_text(json.dumps(report), encoding="utf-8")

    if section == "cargo_workspace":
        log = tmp_path / "reports" / "cargo.log"
        log.write_text("cargo tests passed\n", encoding="utf-8")
        argv = [*locked["selector"]["argv"], "--selector-mismatch"] if bad_selector else locked["selector"]["argv"]
        shared_api.record_section(
            shared, name="cargo_fakes_filtered", argv=argv, exit_code=0,
            files={
                "s22_gate/cargo_workspace/linux/resource-report.json": report_source,
                "logs/cargo-fakes-filtered.log": log,
            }, required_sets=[set_id], platform="linux",
        )
    else:
        windows_root = tmp_path / "windows-l1"
        source = windows_root / quote(set_id, safe="") / "l1-results.json"
        source.parent.mkdir(parents=True)
        source.write_text(json.dumps(report), encoding="utf-8")
        recorded = shared_api.record_windows_l1(
            shared, source_root=windows_root, required_set=set_id, current_head=head,
            worktree_clean=True,
        )
        assert recorded["status"] == "PASS", recorded
        if bad_sha:
            manifest_path = shared / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            rel = manifest["sections"][f"windows_l1:{set_id}"]["files"]["l1-results.json"]["path"]
            (shared / rel).write_text("tampered after SHA256 registration", encoding="utf-8")
    return shared


@pytest.mark.core_portable
@pytest.mark.parametrize(
    ("section", "platform", "bad_selector", "bad_sha"),
    [
        ("cargo_workspace", "linux", True, False),
        ("windows_cap_windows_com", "windows", False, True),
    ],
)
def test_rejected_shared_evidence_cannot_fall_back_to_stale_green_l1(
    tmp_path: Path, section: str, platform: str, bad_selector: bool, bad_sha: bool,
) -> None:
    """OD-93：selector／SHA 被拒時，舊的全綠 L1 報告仍不得讓該集合 PASS。"""
    stale_path, _lock, stale_report = _s22_stale_report(tmp_path, section, platform)
    stale_sha = hashlib.sha256(stale_path.read_bytes()).hexdigest()
    shared = _shared_s22_import_fixture(
        tmp_path, section, bad_selector=bad_selector, bad_sha=bad_sha,
    )
    out = ROOT / "target" / "s2-gfx" / "tests" / tmp_path.parent.name / tmp_path.name / "rejected-shared"
    out.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [
            "bash", "scripts/rust/s22_gate.sh", "--run-id", "reject", "--out", str(out),
            "--l1-results-dir", str(stale_path.parents[2]), "--shared-evidence", str(shared),
            "--windows-dir", "", "--selftest-report", "", "--daemon-path", "",
        ],
        cwd=ROOT, env={**os.environ, "SERIALWRAP_L15_PY_DAEMON": str(tmp_path / "missing-py-serialwrapd")},
        capture_output=True, text=True, timeout=180,
    )
    assert proc.returncode != 0, proc.stdout + proc.stderr
    report_path = out / "reject" / "s22-gate.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    judged = _section(report, f"s22_gate:{section}", platform)
    assert judged["verdict"] == "EVIDENCE_MISSING", judged
    for section_name in ("console_conformance_pty", "blackbox_rust"):
        assert _section(report, f"s22_gate:{section_name}", "linux")["verdict"] == "EVIDENCE_MISSING"
    assert not (out / "reject" / "logs" / "console_conformance_pty.log").exists()
    assert not (out / "reject" / "logs" / "blackbox_rust.log").exists()
    assert stale_path.is_file() and hashlib.sha256(stale_path.read_bytes()).hexdigest() == stale_sha
    assert "EVIDENCE_MISSING" in proc.stdout + proc.stderr
    shutil.rmtree(out, ignore_errors=True)


@pytest.mark.core_portable
def test_s22_gate_records_bridge_selftest_report_path(tmp_path: Path):
    """s22-05：報告需可追溯 bridge 自測輸入，而非只記錄 L1.5 結果。"""
    driver = (ROOT / "scripts/rust/s22_gate.sh").read_text(encoding="utf-8")
    checker = (ROOT / "scripts/rust/s22_gate_check.py").read_text(encoding="utf-8")
    assert "work/p7-ab/bridge-selftest/selftest-report.json" in driver
    assert "uart_stdio_bridge selftest --endpoint posix-pty" in driver
    assert 'sys.executable, "-m", "uart_stdio_bridge", "selftest", "--endpoint", "posix-pty"' in driver
    assert "evaluate_bridge_eligibility" in driver
    assert '"outcome": "failed" if report_exists else "unchecked"' in driver
    assert "--bridge-selftest-report" in driver
    assert '"bridge_selftest_report"' in checker
    report_path = tmp_path / "shared/work/p7-ab/bridge-selftest/selftest-report.json"
    _rc, report = _run(tmp_path / "run", _good_reports(), bridge_selftest_report=report_path)
    assert report["l15_inputs"]["bridge_selftest_report"] == str(report_path)


@pytest.mark.core_portable
def test_s22_driver_runs_plan_derived_console_and_rust_blackbox() -> None:
    """s22-01：兩個缺漏集合須由 driver 實際產生，argv 以 --plan／凍結 selector 為準。"""
    driver = (ROOT / "scripts/rust/s22_gate.sh").read_text(encoding="utf-8")
    lock = json.loads((ROOT / "tests/acceptance/l1_required.lock.json").read_text(encoding="utf-8"))
    selector = lock["sets"]["s22_gate:blackbox_rust"]["linux"]["selector"]["argv"]

    assert '"--plan"' in driver and '"s22_gate:console_conformance_pty"' in driver
    assert 'run_logged("console_conformance_pty",' in driver
    assert 'set_id = "s22_gate:blackbox_rust"\n    selector = lock["sets"][set_id]["linux"]["selector"]' in driver
    assert 'run_logged("blackbox_rust",' in driver
    assert '"SERIALWRAP_AB_LABEL"] = "rust"' in driver
    assert '"SERIALWRAP_CLI_EXE"] = str(cli_path)' in driver
    assert '"SERIALWRAP_DAEMON_EXE"] = str(daemon_path)' in driver
    l15_driver = driver.split("def l15_rows() -> list[dict]:", 1)[1].split("\nif not dry_run:", 1)[0]
    assert 'env.pop("SERIALWRAP_CLI_EXE", None)' in l15_driver, "不可讓外層 CLI 覆寫 Python 基線"
    assert 'env["SERIALWRAP_CLI_EXE"] = str(cli_path)' in l15_driver, "Rust L1.5 必須使用共用 release CLI"
    assert '[sys.executable, "-m", "pytest", *selector["argv"], f"--junitxml={junit}"]' in driver
    assert selector == ["-q", "tests/contract_blackbox/slice21", "tests/contract_blackbox/slice22"]


@pytest.mark.core_portable
def test_s22_shared_import_includes_win_test_and_ignores_windows_dir() -> None:
    """s22-03：shared win_test 必須與兩組 Windows 能力列由同一 manifest 匯入。"""
    driver = (ROOT / "scripts/rust/s22_gate.sh").read_text(encoding="utf-8")
    assert 'for section in win_test windows_cap_windows_com windows_cap_windows_com_daemon; do' in driver
    assert '"windows_l1:$set_id"' in driver
    assert 'if shared_evidence or windows_dir is None:\n        return' in driver


@pytest.mark.core_portable
def test_s22_shared_build_identity_records_imported_stage2_bundle_and_cli_sha(tmp_path: Path) -> None:
    """s22-15／s22-17：共用 release CLI／daemon 的 SHA 與 bundle 來源要寫入報告。"""
    mod = _gate_module()
    cli = tmp_path / "release" / "serialwrap"
    daemon = tmp_path / "release" / "serialwrapd"
    cli.parent.mkdir(parents=True)
    cli.write_bytes(b"shared stage2 release cli")
    daemon.write_bytes(b"shared stage2 release daemon")
    daemon_sha = hashlib.sha256(daemon.read_bytes()).hexdigest()

    bundle_path = tmp_path / "candidate-bundle"
    bundle_identity = {"path": "candidate-bundle", "tree_sha256": "f" * 64, "git_revision": "a" * 40}
    identity = mod.build_identity(
        cli_path=cli,
        daemon_path=daemon,
        build_linux_exit=0,
        built_daemon_sha256=daemon_sha,
        shared_release_bundle=True,
        shared_release_info={"status": "PASS", "bundle_path": str(bundle_path), "identity": bundle_identity},
    )

    assert identity["cli_sha256"] == hashlib.sha256(cli.read_bytes()).hexdigest()
    assert identity["daemon_sha256"] == daemon_sha
    assert identity["build"]["command"] == [
        "bash", "scripts/rust/mkbundle.sh", "--release-bundle", "--stage", "2", "--profile", "release",
        "--out", str(bundle_path),
    ]
    assert identity["build"]["source"]["bundle_path"] == str(bundle_path)
    assert identity["build"]["source"]["bundle_identity"] == bundle_identity
    assert identity["build"]["source"]["imported_cli_path"] == str(cli)
    assert identity["build"]["source"]["imported_daemon_path"] == str(daemon)
    driver = (ROOT / "scripts/rust/s22_gate.sh").read_text(encoding="utf-8")
    assert "CLI SHA256" in driver and "daemon SHA256" in driver
    assert '"--cli-path"' in driver and '"--shared-release-bundle"' in driver
    assert '"--shared-release-info"' in driver
    assert "checked = check_candidate_bundle(shared)" in driver
    assert "recorded != expected or actual != expected" in driver
    assert '"bundle_path": checked["path"]' in driver
    assert 'str(run_dir / "logs/shared-release-bundle.json")' in driver


@pytest.mark.core_portable
def test_s22_driver_rejects_nonshared_formal_execution_before_running_python(tmp_path: Path) -> None:
    """s22-01：正式 driver 不得以無共用 release artifact 的非共用模式執行。"""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    invoked = tmp_path / "python3-called"
    fake_python = fake_bin / "python3"
    fake_python.write_text(
        "#!/bin/sh\nprintf called > \"$S22_TEST_PYTHON_MARKER\"\nexit 91\n",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    out = ROOT / "target" / "s2-gfx" / "tests" / tmp_path.parent.name / tmp_path.name / "nonshared"
    out.parent.mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ,
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "S22_TEST_PYTHON_MARKER": str(invoked),
        "SERIALWRAP_L15_RUST_DAEMON": "",
    }
    proc = subprocess.run(
        ["bash", "scripts/rust/s22_gate.sh", "--run-id", "no-shared", "--out", str(out)],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "非共用模式" in proc.stderr
    assert not invoked.exists(), "拒絕前不應執行任何 Python 測試或 checker"
    assert not (out / "no-shared" / "s22-gate.json").exists()
    shutil.rmtree(out, ignore_errors=True)


def _s22_driver_functions() -> dict[str, object]:
    driver = (ROOT / "scripts/rust/s22_gate.sh").read_text(encoding="utf-8")
    source = driver.rsplit("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
    module = ast.parse(source)
    wanted = {"run_logged", "l15_rows"}
    nodes = [node for node in module.body if isinstance(node, ast.FunctionDef) and node.name in wanted]
    assert {node.name for node in nodes} == wanted
    namespace: dict[str, object] = {
        "hashlib": hashlib,
        "os": os,
        "pathlib": pathlib,
        "shlex": shlex,
        "sys": sys,
    }
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "<s22_gate.py>", "exec"), namespace)
    return namespace


def _collect_s22_l15_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    emap: dict,
    *,
    python_daemon: bool = True,
    rust_daemon: bool = True,
) -> tuple[list[dict], list[dict], dict[str, Path | None]]:
    from scripts.rust.l15_artifact import l15_artifact_problem

    root = tmp_path.resolve()
    python_path = root / "tests/contract_blackbox/bin/py-serialwrapd"
    rust_path = root / "shared-artifacts/release/serialwrapd"
    cli_path = root / "shared-artifacts/release/serialwrap"
    if python_daemon:
        python_path.parent.mkdir(parents=True, exist_ok=True)
        python_path.write_text(
            "#!/bin/sh\nexec python3 ./serialwrapd.py \"$@\"\n", encoding="utf-8"
        )
    if rust_daemon:
        rust_path.parent.mkdir(parents=True, exist_ok=True)
        rust_path.write_bytes(b"\x7fELF s22 rust daemon fixture")
        cli_path.write_bytes(b"s22 shared release cli fixture")
        monkeypatch.setenv("SERIALWRAP_L15_RUST_DAEMON", str(rust_path))
    else:
        monkeypatch.delenv("SERIALWRAP_L15_RUST_DAEMON", raising=False)
    monkeypatch.delenv("SERIALWRAP_L15_PY_DAEMON", raising=False)
    monkeypatch.setenv("SERIALWRAP_CLI_EXE", str(root / "outer-python-cli"))

    calls: list[dict] = []
    namespace = _s22_driver_functions()
    namespace.update({
        "l15_artifact_problem": l15_artifact_problem,
        "root": root,
        "emap": emap,
        "selftest_report": None,
        "daemon_path": rust_path if rust_daemon else None,
        "build_exit": 0 if rust_daemon else None,
        "built_sha": hashlib.sha256(rust_path.read_bytes()).hexdigest() if rust_daemon else None,
        "cli_path": cli_path if rust_daemon else None,
        "selftest_ready": True,
        "selftest_eligibility": "PASS",
        "run_dir": root / "run",
        "logs_dir": root / "run/logs",
    })
    namespace["logs_dir"].mkdir(parents=True, exist_ok=True)

    def record_run(label, argv, env, *, preamble=None, timeout=None):
        calls.append({"label": label, "argv": list(argv), "env": dict(env),
                      "preamble": preamble, "timeout": timeout})
        return 0

    namespace["run_logged"] = record_run
    rows = namespace["l15_rows"]()
    return rows, calls, {
        "root": root,
        "python": python_path,
        "rust": rust_path if rust_daemon else None,
        "cli": cli_path if rust_daemon else None,
    }


@pytest.mark.core_portable
def test_s22_l15_rows_use_cases_and_parity_refs_default_python_and_shared_rust_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """s22-02／s22-11／s22-14：來源聯集、兩輪 CLI、每輪報告目錄與 900 秒逾時。"""
    case_ref = "func-test/runner.py --case co-01-line-buffering"
    parity_ref = "func-test/runner.py --case co-02-line-backspace"
    emap = {
        "cases": {"co-01-line-buffering": {"tests": [case_ref]}},
        "parity_rows": {"#107": {"tests": [parity_ref]}},
    }
    rows, calls, paths = _collect_s22_l15_rows(tmp_path, monkeypatch, emap)

    assert {(row["ref"], row["implementation"]) for row in rows} == {
        (ref, implementation)
        for ref in (case_ref, parity_ref)
        for implementation in ("python", "rust")
    }
    assert all(row["outcome"] == "passed" for row in rows), rows
    assert len(calls) == 4
    for call in calls:
        label = call["label"]
        argv = call["argv"]
        assert call["timeout"] == 900, call
        assert argv[argv.index("--report-dir") + 1] == str(paths["root"] / "run/l15" / label), call
        implementation = label.rsplit("-", 1)[1]
        if implementation == "python":
            assert call["env"].get("SERIALWRAP_DAEMON_EXE") == str(paths["python"])
            assert "SERIALWRAP_CLI_EXE" not in call["env"], "Python 輪不得沿用外層 Rust CLI"
        else:
            assert call["env"].get("SERIALWRAP_DAEMON_EXE") == str(paths["rust"])
            assert call["env"].get("SERIALWRAP_CLI_EXE") == str(paths["cli"]), call


@pytest.mark.core_portable
@pytest.mark.parametrize("missing_implementation", ["python", "rust"])
def test_s22_l15_missing_daemon_is_unchecked_and_cannot_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing_implementation: str,
) -> None:
    """s22-04／s22-12：任一 daemon 缺席只記 unchecked，仍不能令 L1.5 判 PASS。"""
    ref = "func-test/runner.py --case co-01-line-buffering"
    emap = {"cases": {"co-01-line-buffering": {"tests": [ref]}}, "parity_rows": {}}
    rows, calls, _paths = _collect_s22_l15_rows(
        tmp_path, monkeypatch, emap,
        python_daemon=missing_implementation != "python",
        rust_daemon=missing_implementation != "rust",
    )
    missing = next(row for row in rows if row["implementation"] == missing_implementation)
    available = next(row for row in rows if row["implementation"] != missing_implementation)
    assert missing["outcome"] == "unchecked" and missing["exit_code"] is None, missing
    assert available["outcome"] == "passed", available
    checker = _gate_module()
    assert not checker._l15_passed({ref: rows}, ref), "單邊缺 daemon 不得成為 PASS"
    assert len(calls) == 1, calls


@pytest.mark.core_portable
@pytest.mark.parametrize("out_kind", ["outside-repo", "inside-repo-outside-target"])
def test_s22_driver_rejects_outside_target_before_creating_run_dir(
    tmp_path: Path, out_kind: str,
) -> None:
    """s22-13：輸出只能位於本 worktree 的 target/，拒絕必須先於任何寫入。"""
    run_id = f"reject-{out_kind}-{tmp_path.name}"
    out = tmp_path / "outside" if out_kind == "outside-repo" else ROOT / "docs"
    run_dir = out / run_id
    try:
        proc = subprocess.run(
            ["bash", "scripts/rust/s22_gate.sh", "--dry-run", "--run-id", run_id, "--out", str(out)],
            cwd=ROOT, capture_output=True, text=True, timeout=30,
        )
        assert proc.returncode == 2, proc.stdout + proc.stderr
        assert "--out" in proc.stderr and "target/" in proc.stderr, proc.stderr
        assert not run_dir.exists(), "錯誤 --out 不得先建立任何輸出"
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)


@pytest.mark.core_portable
def test_s22_driver_exports_parsed_rust_daemon_and_ignores_outer_common_override(
    tmp_path: Path,
) -> None:
    """s22-06／s22-12：child env 只使用解析後路徑，共用模式不繼承外層 artifact。"""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    capture = tmp_path / "daemon-env.txt"
    fake_python = fake_bin / "python3"
    fake_python.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "${SERIALWRAP_L15_RUST_DAEMON-<unset>}" >> "$S22_TEST_DAEMON_CAPTURE"\n'
        f"exec {shlex.quote(sys.executable)} \"$@\"\n",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    base_env = {
        **os.environ,
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "S22_TEST_DAEMON_CAPTURE": str(capture),
        "SERIALWRAP_L15_RUST_DAEMON": "/outer/not-shared/serialwrapd",
    }

    def run_driver(run_id: str, *extra: str) -> None:
        out = ROOT / "target" / "s2-gfx" / "tests" / tmp_path.parent.name / tmp_path.name / run_id
        out.parent.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(
            ["bash", "scripts/rust/s22_gate.sh", "--dry-run", "--run-id", run_id,
             "--out", str(out), *extra],
            cwd=ROOT, env=base_env, capture_output=True, text=True, timeout=60,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        shutil.rmtree(out, ignore_errors=True)

    explicit = str(tmp_path / "from-cli" / "serialwrapd")
    run_driver("daemon-from-cli", "--daemon-path", explicit)
    assert capture.read_text(encoding="utf-8").splitlines()[-1] == explicit

    run_driver("common-ignores-outer", "--shared-evidence", str(tmp_path / "unused-shared"))
    assert capture.read_text(encoding="utf-8").splitlines()[-1] == "", "共用模式不可繼承外層 Rust daemon"


@pytest.mark.core_portable
def test_s22_task13_step4_runs_shared_evidence_before_gate() -> None:
    """s22-07：文件命令以同一 RUN_ID 產生並消費 shared evidence。"""
    plan = (ROOT / "docs/rust-refactor/plans/07-slice22-console.md").read_text(encoding="utf-8")
    shared = plan.index('scripts/rust/gate_shared.sh --run-id "$RUN_ID"')
    gate = plan.index('scripts/rust/s22_gate.sh --shared-evidence "$SHARED_EVIDENCE" --run-id "$RUN_ID"')
    assert shared < gate
    assert 'SHARED_EVIDENCE="target/s2-shared-evidence/$RUN_ID"' in plan


@pytest.mark.core_portable
def test_s22_run_logged_turns_timeout_into_failed_exit(tmp_path: Path) -> None:
    """逾時仍是執行失敗，不得因 runner 卡住而產生可通過列。"""
    namespace = _s22_driver_functions()
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()

    def timeout_run(argv, **kwargs):
        assert kwargs.get("timeout") == 900
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    namespace.update({
        "logs_dir": logs_dir,
        "root": tmp_path,
        "subprocess": type("FakeSubprocess", (), {
            "run": staticmethod(timeout_run), "TimeoutExpired": subprocess.TimeoutExpired,
            "STDOUT": subprocess.STDOUT
        }),
    })
    rc = namespace["run_logged"]("l15-test", ["runner"], {}, timeout=900)
    assert rc == 124
    assert "900" in (logs_dir / "l15-test.log").read_text(encoding="utf-8")
