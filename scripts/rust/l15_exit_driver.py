#!/usr/bin/env python3
"""只豁免 gate 已核准的 Windows L1.5 擁有者延後列之 producer exit 1。"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any


_DEFERRED_CASES = {
    "l15-transport-disconnect": (
        {"FAIL", "INSUFFICIENT_EVIDENCE"},
        re.compile(r".*'transport_disconnect' 失敗: HARNESS_CAPABILITY_UNAVAILABLE.*", re.DOTALL),
    ),
    "ev-01-spawn-handler": (
        {"FAIL"},
        re.compile(r"步驟 \d+: 等待檔案逾時 [0-9.]+s: [^\r\n]*[/\\]ev-fired"),
    ),
    "sm-04-device-unplug": (
        {"SKIP"},
        re.compile(r"cap_missing:by_id_link"),
    ),
}


def _is_deferred_result(row: dict[str, Any]) -> bool:
    case_id = row.get("case_id")
    entry = _DEFERRED_CASES.get(case_id)
    if entry is None or row.get("verdict") not in entry[0]:
        return False
    if not isinstance(row.get("message"), str) or entry[1].fullmatch(row["message"]) is None:
        return False
    diagnostics = row.get("harness_diagnostics")
    return isinstance(diagnostics, list) and not any(
        isinstance(item, dict) and item.get("kind") == "harness_stop_failed" for item in diagnostics
    )


def _gate_deferred_case_ids(gate: dict[str, Any]) -> set[str] | None:
    if gate.get("overall") not in {"IMPLEMENTED", "VERIFIED"} or gate.get("errors") != []:
        return None
    cases = gate.get("cases")
    if not isinstance(cases, list):
        return None
    deferred_ids: set[str] = set()
    for case in cases:
        if not isinstance(case, dict):
            return None
        if case.get("layer") != "L1.5" or case.get("status") == "PASS":
            continue
        if case.get("status") != "BLOCKED-OWNER" or case.get("id") not in _DEFERRED_CASES:
            return None
        evidence = case.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            return None
        windows = [row for row in evidence if isinstance(row, dict) and row.get("platform") == "windows"]
        linux = [row for row in evidence if isinstance(row, dict) and row.get("platform") == "linux"]
        if not windows or not linux:
            return None
        if not all(row.get("verdict") == "PASS" and row.get("gate_eligible") is True for row in linux):
            return None
        if not all(_is_deferred_result({**row, "case_id": case["id"]}) for row in windows):
            return None
        deferred_ids.add(case["id"])
    return deferred_ids or None


def _report_results(
    report: Any,
    run: Any,
    expected_report: str,
    *,
    scenarios: bool,
    run_id: str,
) -> tuple[bool, list[dict[str, Any]], int]:
    if not isinstance(report, dict) or report.get("schema_version") != "func-test-report.v1":
        return False, [], 0
    if report.get("complete") is not True:
        return False, [], 0
    results = report.get("results")
    if not isinstance(results, list) or not results:
        return False, [], 0
    if not all(
        isinstance(row, dict)
        and isinstance(row.get("case_id"), str) and row["case_id"]
        and row.get("mode") == "bridge" and row.get("run_index") == 0
        and isinstance(row.get("message"), str)
        and isinstance(row.get("harness_diagnostics"), list)
        and isinstance(row.get("evidence_dir"), str)
        for row in results
    ):
        return False, [], 0
    case_ids = [row["case_id"] for row in results]
    if len(set(case_ids)) != len(case_ids) or report.get("independent_cases") != len(case_ids):
        return False, [], 0
    report_subdir = "report" if scenarios else "report-basic"
    for row in results:
        evidence_dir = row["evidence_dir"].replace("\\", "/").rstrip("/")
        expected_suffix = f"/sw-rust-test/{run_id}/{report_subdir}/{row['case_id']}/bridge/0"
        if not evidence_dir.lower().endswith(expected_suffix.lower()):
            return False, [], 0
    verdicts = [row.get("verdict") for row in results]
    mode_summary = (report.get("modes") or {}).get("bridge") if isinstance(report.get("modes"), dict) else None
    if not isinstance(mode_summary, dict):
        return False, [], 0
    expected_summary = {
        "runs": len(results),
        "pass": verdicts.count("PASS"),
        "fail": verdicts.count("FAIL"),
        "insufficient": verdicts.count("INSUFFICIENT_EVIDENCE"),
        "skip": verdicts.count("SKIP"),
    }
    if any(mode_summary.get(key) != value for key, value in expected_summary.items()):
        return False, [], 0
    # 未知 verdict 不得因沒有進入任何摘要欄位而被算作 runner 成功。
    if sum(expected_summary[key] for key in ("pass", "fail", "insufficient", "skip")) != len(results):
        return False, [], 0

    nonpass = [row for row in results if row.get("verdict") != "PASS"]
    exit_code = int(bool(nonpass))
    if not isinstance(run, dict) or run.get("report") != expected_report or run.get("runner_exit") != exit_code:
        return False, [], 0
    argv = run.get("runner_argv")
    if not isinstance(argv, list) or not all(isinstance(value, str) for value in argv):
        return False, [], 0
    if argv[:3] != ["-3.12", "func-test/runner.py", "--mode"] or argv[3:4] != ["bridge"]:
        return False, [], 0
    if ("--scenarios" in argv) is not scenarios or "--repeat" in argv:
        return False, [], 0
    for flag, expected in (("--daemon-com", "COM20"), ("--bridge-com", "COM21"),
                           ("--transport-mode", "virtual_com0com")):
        try:
            pos = argv.index(flag)
        except ValueError:
            return False, [], 0
        if argv[pos + 1:pos + 2] != [expected]:
            return False, [], 0
    try:
        report_dir = argv[argv.index("--report-dir") + 1]
    except (ValueError, IndexError):
        return False, [], 0
    normalized_dir = report_dir.replace("\\", "/").rstrip("/")
    expected_suffix = f"/sw-rust-test/{run_id}/{report_subdir}"
    if not normalized_dir.endswith(expected_suffix):
        return False, [], 0
    return True, nonpass, exit_code


def _windows_l15_evidence_allows_defer(gate: dict[str, Any], evidence: Any, gate_ids: set[str]) -> bool:
    if not isinstance(evidence, dict):
        return False
    metadata = evidence.get("metadata")
    if not isinstance(metadata, dict) or metadata.get("schema_version") != "win-func-test-meta.v2":
        return False
    if metadata.get("git_clean") is not True or not re.fullmatch(r"[0-9a-f]{40}", str(metadata.get("git_head", ""))):
        return False
    if not re.fullmatch(r"[0-9a-f]{40}", str(metadata.get("git_tree", ""))):
        return False
    if not re.fullmatch(r"[0-9a-f]{64}", str(metadata.get("cli_exe_sha256", ""))):
        return False
    if not re.fullmatch(r"[0-9a-f]{64}", str(metadata.get("selftest_report_sha256", ""))):
        return False
    if not re.fullmatch(r"[0-9a-f]{64}", str(metadata.get("tool_tree_sha256", ""))):
        return False
    if not isinstance(metadata.get("run_id"), str) or not metadata["run_id"]:
        return False
    run_id = metadata["run_id"]
    if metadata.get("com_pair") != {"daemon": "COM20", "bridge": "COM21"}:
        return False
    if metadata.get("transport_mode") != "virtual_com0com" or not isinstance(metadata.get("runner_timeout_s"), int):
        return False
    if metadata["runner_timeout_s"] <= 0:
        return False
    runs = metadata.get("runs")
    if not isinstance(runs, dict) or set(runs) != {"scenarios", "basic"}:
        return False
    scenario_run = runs.get("scenarios")
    basic_run = runs.get("basic")
    if not isinstance(scenario_run, dict) or not isinstance(basic_run, dict):
        return False
    if metadata.get("runner_argv") != scenario_run.get("runner_argv"):
        return False

    valid_scenarios, scenario_nonpass, scenario_exit = _report_results(
        evidence.get("scenarios"), scenario_run, "func-test-report.json", scenarios=True, run_id=run_id,
    )
    valid_basic, basic_nonpass, basic_exit = _report_results(
        evidence.get("basic"), basic_run, "basic/func-test-report.json", scenarios=False, run_id=run_id,
    )
    if not valid_scenarios or not valid_basic:
        return False
    if type(metadata.get("runner_exit")) is not int or metadata["runner_exit"] != scenario_exit:
        return False
    if scenario_run.get("runner_exit") != scenario_exit or basic_run.get("runner_exit") != basic_exit:
        return False
    if not isinstance(scenario_run.get("runner_argv"), list) or not isinstance(basic_run.get("runner_argv"), list):
        return False

    nonpass = scenario_nonpass + basic_nonpass
    if not nonpass or {row["case_id"] for row in nonpass} != gate_ids:
        return False
    if len({row["case_id"] for row in nonpass}) != len(nonpass):
        return False
    wrapper_exit = scenario_exit if scenario_exit != 0 else basic_exit
    if wrapper_exit != 1:
        return False

    gate_cases = [case for case in gate["cases"] if isinstance(case, dict) and case.get("layer") == "L1.5"]
    for row in nonpass:
        if not _is_deferred_result(row):
            return False
        matching_cases = [case for case in gate_cases if case.get("id") == row["case_id"]]
        if len(matching_cases) != 1:
            return False
        windows = [item for item in matching_cases[0].get("evidence", [])
                   if isinstance(item, dict) and item.get("platform") == "windows"]
        if len(windows) != 1 or windows[0].get("verdict") != row.get("verdict") \
                or windows[0].get("message") != row.get("message"):
            return False
    return True


def unresolved_steps(
    rc_text: str, gate: dict[str, Any], windows_l15_evidence: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """回傳未豁免的非零步驟與明確豁免項；格式錯誤一律 fail-closed。"""
    rows: list[dict[str, Any]] = []
    malformed: list[dict[str, Any]] = []
    for line_number, raw in enumerate(rc_text.splitlines(), start=1):
        if not raw.strip():
            continue
        parts = raw.split()
        if len(parts) != 2:
            malformed.append({"line": line_number, "raw": raw})
            continue
        name, value = parts
        try:
            code = int(value, 10)
        except ValueError:
            malformed.append({"line": line_number, "raw": raw})
            continue
        if code != 0:
            rows.append({"step": name, "rc": code})

    suppressed: list[dict[str, Any]] = []
    if windows_l15_evidence is None:
        injected = gate.get("_windows_l15_evidence")
        windows_l15_evidence = injected if isinstance(injected, dict) else None
    gate_ids = _gate_deferred_case_ids(gate)
    if (
        not malformed
        and len(rows) == 1
        and rows[0] == {"step": "win_l15_func_test", "rc": 1}
        and rc_text.splitlines().count("stage1a_gate 0") == 1
        and gate_ids is not None
        and _windows_l15_evidence_allows_defer(gate, windows_l15_evidence, gate_ids)
    ):
        suppressed = rows
        rows = []
    return malformed + rows, suppressed


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        if len(args) != 5:
            raise ValueError(
                "用法：l15_exit_driver.py <rc.txt> <stage1a-gate.json> "
                "<windows-scenarios-report.json> <windows-basic-report.json> <windows-meta.json>"
            )
        rc_text = Path(args[0]).read_text(encoding="utf-8")
        gate = json.loads(Path(args[1]).read_text(encoding="utf-8"))
        if not isinstance(gate, dict):
            raise ValueError("stage1a gate 報告不是 object")
        evidence = {
            "scenarios": json.loads(Path(args[2]).read_text(encoding="utf-8")),
            "basic": json.loads(Path(args[3]).read_text(encoding="utf-8")),
            "metadata": json.loads(Path(args[4]).read_text(encoding="utf-8")),
        }
        unresolved, suppressed = unresolved_steps(rc_text, gate, evidence)
        summary = {"failed_steps": len(unresolved), "unresolved": unresolved, "suppressed": suppressed}
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        summary = {"failed_steps": 1, "unresolved": [{"step": "exit_driver_summary", "error": str(exc)}],
                   "suppressed": []}
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
