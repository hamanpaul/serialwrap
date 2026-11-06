#!/usr/bin/env python3
"""豁免 gate 已核准的 Windows L1.5 擁有者延後列，以及 OD-75 已登記的基線缺陷。

擁有者延後列（OD-30～OD-32）仍須為 BLOCKED-OWNER。基線缺陷的證據維持 FAIL，
不改判 PASS、SKIP 或 BLOCKED-OWNER。`--rejudge` 用既有證據重跑 gate，不重跑測試。
"""

from __future__ import annotations

import json
import importlib.util
import os
import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any
import xml.etree.ElementTree as ET

import yaml

if __package__:
    from scripts.rust import python_baseline_defects
else:
    import python_baseline_defects


_REPO_ROOT = Path(__file__).resolve().parents[2]
_L15_PRODUCER_STEPS = frozenset({
    "win_l15_func_test",
    "l15_linux_bridge",
    "l15_linux_bridge_scenarios",
    "l15_linux_1b_bridge",
    "l15_linux_1b_scenarios",
})
_JUDGMENT_IGNORE = frozenset({"baseline_known_defects", "baseline_pending_classification"})
_LINUX_L15_REPORTS = {
    "l15_linux_bridge": ("func-test", False),
    "l15_linux_bridge_scenarios": ("func-test-scenarios", True),
    "l15_linux_1b_bridge": ("p4/l15-linux", False),
    "l15_linux_1b_scenarios": ("p4/l15-linux-scenarios", True),
}
_PYTEST_PRODUCER_REPORTS = {
    "pytest_full_junit": "l1/linux/pytest-full",
    "suite_20groups_linux": "p4/l1/linux/20groups",
    "bbproc_multiagent_coexist": "bbproc-multiagent-coexist",
    "bbproc_1b": "bbproc-1b",
    "ab_blackbox_release": "l1/linux/blackbox",
}

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


def _declared_repeat(case_id: str, *, scenarios: bool) -> int | None:
    """讀取 runner 實際使用的案例 repeat；檔案或宣告不合法時 fail-closed。"""
    if not case_id or Path(case_id).name != case_id:
        return None
    kind = "scenarios" if scenarios else "cases"
    path = _REPO_ROOT / "func-test" / kind / f"{case_id}.yaml"
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError):
        return None
    if not isinstance(raw, dict):
        return None
    meta = raw.get("meta") or {}
    if not isinstance(meta, dict) or meta.get("name", path.stem) != case_id:
        return None
    repeat = meta.get("repeat", 1)
    if isinstance(repeat, bool) or not isinstance(repeat, int) or repeat < 1:
        return None
    return repeat


def _row_na_only(row: dict[str, Any]) -> bool:
    return (
        row.get("verdict") == "INSUFFICIENT_EVIDENCE"
        and row.get("na_only_gap") is True
        and bool(row.get("not_applicable_steps"))
    )


def _baseline_case(case: dict[str, Any], registry: python_baseline_defects.Registry) -> bool:
    """每一條非 PASS、非 not_applicable 的證據列都符合登記，且至少有一條。"""
    evidence = case.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        return False
    saw = False
    for row in evidence:
        if not isinstance(row, dict):
            return False
        if row.get("verdict") == "PASS" or _row_na_only(row):
            continue
        saw = True
        if registry.match_l15(
            case.get("id"), row.get("platform"), row.get("verdict"),
            row.get("message"), row.get("harness_diagnostics"),
        ) is None:
            return False
    return saw


def _gate_deferred_case_ids(
    gate: dict[str, Any], registry: python_baseline_defects.Registry,
) -> set[str] | None:
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
        evidence = case.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            return None
        if case.get("status") == "BLOCKED-OWNER" and case.get("id") in _DEFERRED_CASES:
            windows = [row for row in evidence if isinstance(row, dict) and row.get("platform") == "windows"]
            linux = [row for row in evidence if isinstance(row, dict) and row.get("platform") == "linux"]
            if not windows or not linux:
                return None
            if not all(row.get("verdict") == "PASS" and row.get("gate_eligible") is True for row in linux):
                return None
            if not all(_is_deferred_result({**row, "case_id": case["id"]}) for row in windows):
                return None
            deferred_ids.add(case["id"])
            continue
        if _baseline_case(case, registry):
            deferred_ids.add(case["id"])
            continue
        return None
    return deferred_ids or None


def _windows_explained_ids(
    gate: dict[str, Any], registry: python_baseline_defects.Registry,
) -> set[str] | None:
    """Windows 非 PASS 列都是擁有者延後或已登記基線缺陷時，回傳那些案例 id。"""
    cases = gate.get("cases")
    if not isinstance(cases, list):
        return None
    explained: set[str] = set()
    for case in cases:
        if not isinstance(case, dict) or case.get("layer") != "L1.5":
            continue
        evidence = case.get("evidence")
        if not isinstance(evidence, list):
            return None
        windows = [
            row for row in evidence
            if isinstance(row, dict) and row.get("platform") == "windows" and row.get("verdict") != "PASS"
        ]
        if not windows:
            continue
        for row in windows:
            payload = {**row, "case_id": case.get("id")}
            if _is_deferred_result(payload):
                continue
            if registry.match_l15(
                case.get("id"), "windows", row.get("verdict"), row.get("message"), row.get("harness_diagnostics"),
            ) is None:
                return None
        explained.add(str(case.get("id")))
    return explained


def _read_json(path: Path) -> dict[str, Any]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"報告不是 object：{path}")
    return raw


def _linux_baseline_explained(
    step: str, evidence_dir: Path | None, registry: python_baseline_defects.Registry,
) -> bool:
    """逐步核對完整報告、案例宣告與所有失敗，不能用其他 producer 的已知缺陷放行。"""
    if evidence_dir is None:
        return False
    folder, scenarios = _LINUX_L15_REPORTS[step]
    report = _read_json(evidence_dir / folder / "func-test-report.json")
    results = report.get("results")
    if report.get("schema_version") != "func-test-report.v1" or report.get("complete") is not True:
        return False
    if not isinstance(results, list) or not results or not all(
        isinstance(row, dict) and isinstance(row.get("case_id"), str) and row["case_id"]
        and row.get("mode") == "bridge" and type(row.get("run_index")) is int
        and isinstance(row.get("message"), str) and isinstance(row.get("harness_diagnostics"), list)
        for row in results
    ):
        return False
    case_ids = {row["case_id"] for row in results}
    if report.get("independent_cases") != len(case_ids):
        return False
    for case_id in case_ids:
        repeat = _declared_repeat(case_id, scenarios=scenarios)
        indexes = sorted(row["run_index"] for row in results if row["case_id"] == case_id)
        if repeat is None or indexes != list(range(repeat)):
            return False
    verdicts = [row.get("verdict") for row in results]
    expected = {"runs": len(results), **{
        key: verdicts.count(verdict) for key, verdict in
        (("pass", "PASS"), ("fail", "FAIL"), ("insufficient", "INSUFFICIENT_EVIDENCE"), ("skip", "SKIP"))
    }}
    if sum(expected[k] for k in ("pass", "fail", "insufficient", "skip")) != len(results):
        return False
    modes = report.get("modes")
    if (not isinstance(modes, dict) or not set(modes) <= {"bridge", "pty"}
            or modes.get("bridge") != expected
            or ("pty" in modes and modes["pty"] != dict.fromkeys(expected, 0))):
        return False
    nonpass = [r for r in results if r.get("verdict") != "PASS" and not _row_na_only(r)]
    return bool(nonpass) and all(
        registry.match_l15(
            row["case_id"], "linux", row.get("verdict"), row.get("message"), row.get("harness_diagnostics"),
        ) is not None for row in nonpass
    )


def _gate_module():
    if __package__:
        from scripts.rust import stage1a_gate
    else:
        import stage1a_gate
    return stage1a_gate


def _load_acceptance_module(name: str):
    path = _REPO_ROOT / "tests" / "acceptance" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ValueError(f"無法載入 {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _approved_node(
    gate: dict[str, Any], registry: python_baseline_defects.Registry, kind: str,
    nodeid: str, platform: str, status: str, message: str,
) -> bool:
    entry = registry.match_node(nodeid, platform, status, message)
    return entry is not None and any(
        isinstance(row, dict) and row.get("kind") == kind and row.get("blocking") is False
        and row.get("defect_id") == entry.defect_id and row.get("platform") == platform
        and isinstance(row.get("id"), str) and python_baseline_defects.nodeids_equal(row["id"], nodeid)
        for row in gate.get("baseline_known_defects", [])
    )


def _ab_report_path(evidence_dir: Path) -> Path:
    log = evidence_dir / "driver.log"
    if log.is_file():
        args = _gate_args_from_log(log) or []
        if "--ab-report" in args:
            return Path(args[args.index("--ab-report") + 1])
    return evidence_dir / "ab-report.json"


def _node_producer_explained(
    step: str, evidence_dir: Path | None, gate: dict[str, Any], registry: python_baseline_defects.Registry,
) -> bool:
    if evidence_dir is None:
        return False
    stage_gate = _gate_module()
    if step == "ab_blackbox_release":
        report = _read_json(_ab_report_path(evidence_dir))
        rows = report.get("cases")
        if not isinstance(rows, list) or not rows or not all(isinstance(r, dict) for r in rows):
            return False
        if len({r.get("id") for r in rows}) != len(rows):
            return False
        platform = report.get("identity", {}).get("a", {}).get("platform")
        if platform != "linux" or report.get("errors") != [] or report.get("result") != "fail":
            return False
        nonpass = [r for r in rows if r.get("class") not in stage_gate._AB_NON_BLOCKING]
        if not nonpass:
            return False
        for row in nonpass:
            entry_a = stage_gate._match_ab_entry(registry, row, platform, row.get("a"), stage_gate._ab_side_message(row, "a"))
            entry_b = stage_gate._match_ab_entry(registry, row, platform, row.get("b"), stage_gate._ab_side_message(row, "b"))
            if row.get("a") not in stage_gate._AB_FAIL or row.get("b") not in stage_gate._AB_FAIL:
                return False
            if entry_a is None or entry_a is not entry_b or not _approved_node(
                gate, registry, "ab", row.get("id"), platform, row["b"], stage_gate._ab_side_message(row, "b"),
            ):
                return False
        return True

    folder = evidence_dir / _PYTEST_PRODUCER_REPORTS[step]
    junit = evidence_dir / "pytest-full.xml" if step == "pytest_full_junit" else folder / "junit.xml"
    report = _read_json(folder / "resource-report.json")
    if report.get("runner") != "pytest" or report.get("exit_code") != 1:
        return False
    root = ET.parse(junit).getroot()
    if not stage_gate._junit_failures_accounted(root):
        return False
    counter = _load_acceptance_module("check_evidence_index")._junit_counts
    judged = _load_acceptance_module("l1_judge").judge(
        report, report.get("collected") or [], platform="linux", junit=counter(junit),
    )
    if judged.get("verdict") != "FAIL" or any(
        not str(reason).startswith("failed:") for reason in judged.get("reasons", [])
    ):
        return False
    failures = stage_gate._junit_failure_rows(junit)
    if not failures:
        return False
    by_key = {stage_gate._pytest_exact_key(n): n for n in judged.get("failed", [])}
    if len(by_key) != len(failures) or {r[0] for r in failures} != set(by_key):
        return False
    # 非失敗列也逐列對帳；相同總數不能掩蓋錯列或重複列。
    junit_cases: dict[str, list[str]] = {}
    for tc in root.iter("testcase"):
        key = (stage_gate._pytest_exact_key(tc.get("nodeid")) if tc.get("nodeid") else
               stage_gate._pytest_exact_key_from_junit(tc.get("classname", ""), tc.get("name", "")))
        outcome = ("failed" if tc.find("failure") is not None or tc.find("error") is not None else
                   "skipped" if tc.find("skipped") is not None else "passed")
        junit_cases.setdefault(key, []).append(outcome)
    expected = {stage_gate._pytest_exact_key(r["nodeid"]): r["outcome"] for r in report["results"]}
    if set(junit_cases) != set(expected) or any(
        outcomes != ["failed" if expected[key] == "error" else expected[key]]
        for key, outcomes in junit_cases.items()
    ):
        return False
    return all(_approved_node(gate, registry, "pytest", by_key[key], "linux", status, message)
               for key, status, message in failures)


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
        and row.get("mode") == "bridge"
        and isinstance(row.get("run_index"), int) and not isinstance(row.get("run_index"), bool)
        and row["run_index"] >= 0
        and isinstance(row.get("message"), str)
        and isinstance(row.get("harness_diagnostics"), list)
        and isinstance(row.get("evidence_dir"), str)
        for row in results
    ):
        return False, [], 0
    case_ids = [row["case_id"] for row in results]
    unique_case_ids = set(case_ids)
    run_keys = [(row["case_id"], row["mode"], row["run_index"]) for row in results]
    if len(set(run_keys)) != len(run_keys) or report.get("independent_cases") != len(unique_case_ids):
        return False, [], 0
    for case_id in unique_case_ids:
        expected_repeat = _declared_repeat(case_id, scenarios=scenarios)
        indexes = sorted(row["run_index"] for row in results if row["case_id"] == case_id)
        if expected_repeat is None or indexes != list(range(expected_repeat)):
            return False, [], 0
    report_subdir = "report" if scenarios else "report-basic"
    for row in results:
        evidence_dir = row["evidence_dir"].replace("\\", "/").rstrip("/")
        expected_suffix = (
            f"/sw-rust-test/{run_id}/{report_subdir}/{row['case_id']}/bridge/{row['run_index']}"
        )
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


def _windows_l15_evidence_allows_defer(
    gate: dict[str, Any],
    evidence: Any,
    gate_ids: set[str],
    registry: python_baseline_defects.Registry,
) -> bool:
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
        baseline = registry.match_l15(
            row.get("case_id"), "windows", row.get("verdict"), row.get("message"), row.get("harness_diagnostics"),
        ) is not None
        if not _is_deferred_result(row) and not baseline:
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
    *, evidence_dir: Path | None = None,
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
    registry = python_baseline_defects.load_for_gate(_REPO_ROOT)
    gate_ids = _gate_deferred_case_ids(gate, registry)
    windows_ids = _windows_explained_ids(gate, registry)

    def _explained(row: dict[str, Any]) -> bool:
        if row["rc"] != 1:
            return False
        if row["step"] == "win_l15_func_test":
            return (
                gate_ids is not None and windows_ids is not None
                and bool(windows_ids)
                and _windows_l15_evidence_allows_defer(gate, windows_l15_evidence, windows_ids, registry)
            )
        if row["step"] in _LINUX_L15_REPORTS:
            return _linux_baseline_explained(row["step"], evidence_dir, registry)
        if row["step"] in _PYTEST_PRODUCER_REPORTS:
            return _node_producer_explained(row["step"], evidence_dir, gate, registry)
        return False

    if (
        not malformed
        and rows
        and gate.get("overall") in {"IMPLEMENTED", "VERIFIED"} and gate.get("errors") == []
        and rc_text.splitlines().count("stage1a_gate 0") == 1
    ):
        remaining = []
        for row in rows:
            try:
                explained = _explained(row)
            except (OSError, ValueError, TypeError, KeyError, AttributeError, ET.ParseError):
                explained = False
            (suppressed if explained else remaining).append(row)
        # OD-30～OD-32 既有全有或全無語意保留；其他不明錯誤不能被延後列掩蓋。
        if any(r["step"] == "win_l15_func_test" for r in suppressed) and remaining:
            rows = [r for r in rows if r not in suppressed or r["step"] == "win_l15_func_test"]
            suppressed = [r for r in suppressed if r["step"] != "win_l15_func_test"]
        else:
            rows = remaining
    return malformed + rows, suppressed


def parse_failed_steps(summary_text: str) -> int:
    """解析出口摘要。不是合法 JSON、不是 object，或 failed_steps 不是非負整數時拒絕。"""
    try:
        summary = json.loads(summary_text)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("RC_SUMMARY 不是合法 JSON") from exc
    if not isinstance(summary, dict) or "failed_steps" not in summary:
        raise ValueError("RC_SUMMARY 缺少 failed_steps")
    failed_steps = summary["failed_steps"]
    # bool 是 int 的子類別，但不是步驟計數。
    if isinstance(failed_steps, bool) or not isinstance(failed_steps, int) or failed_steps < 0:
        raise ValueError("RC_SUMMARY 的 failed_steps 必須是非負整數")
    return failed_steps


def _judgment_core(document: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in document.items() if key not in _JUDGMENT_IGNORE}


def _gate_args_from_log(path: Path) -> list[str] | None:
    for line in path.read_text(encoding="utf-8").splitlines():
        marker = "GATE_ARGS="
        position = line.find(marker)
        if position < 0:
            continue
        raw = line[position + len(marker):].strip()
        return shlex.split(raw) if raw else None
    return None


def _replace_out(args: list[str], out: Path) -> list[str]:
    updated: list[str] = []
    replaced = False
    expect_path = False
    for arg in args:
        if expect_path:
            updated.append(str(out))
            expect_path = False
            replaced = True
            continue
        updated.append(arg)
        if arg == "--out":
            expect_path = True
    if expect_path:
        raise ValueError("GATE_ARGS 的 --out 缺少路徑")
    if not replaced:
        updated.extend(["--out", str(out)])
    return updated


def _emit_rejudge(payload: dict[str, Any], *, assert_same: bool = False) -> int:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    if "error" in payload:
        return 1
    return 0 if payload.get("same_judgment" if assert_same else "exit_passed") is True else 1


def _rejudge(argv: list[str]) -> int:
    """重判兩層出口；預設行程碼反映新出口，另可要求判定與存檔一致。"""
    assert_same = "--assert-same-judgment" in argv
    argv = [arg for arg in argv if arg != "--assert-same-judgment"]
    if len(argv) != 1 or not argv[0]:
        return _emit_rejudge({
            "same_judgment": False,
            "error": "用法：l15_exit_driver.py --rejudge <evidence_dir> [--assert-same-judgment]",
        })
    evidence = Path(argv[0])
    log_path = evidence / "driver.log"
    head_path = evidence / "HEAD"
    saved_path = evidence / "stage1a-gate.json"
    if not log_path.is_file() or not head_path.is_file() or not saved_path.is_file():
        return _emit_rejudge({
            "same_judgment": False,
            "error": "證據目錄缺少 driver.log、HEAD 或 stage1a-gate.json",
        })
    head = head_path.read_text(encoding="utf-8").strip()
    if re.fullmatch(r"[0-9a-f]{40}", head) is None:
        return _emit_rejudge({"same_judgment": False, "error": "HEAD 不是 40 碼十六進位"})
    gate_args = _gate_args_from_log(log_path)
    if gate_args is None:
        return _emit_rejudge({"same_judgment": False, "error": "driver.log 沒有 GATE_ARGS"})
    try:
        original = json.loads(saved_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return _emit_rejudge({"same_judgment": False, "error": f"無法讀取存檔 gate：{exc}"})
    if not isinstance(original, dict):
        return _emit_rejudge({"same_judgment": False, "error": "存檔 gate 不是 object"})
    descriptor, name = tempfile.mkstemp(prefix="stage1a-rejudge-", suffix=".json")
    os.close(descriptor)
    temporary = Path(name)
    try:
        command = [
            sys.executable,
            str(_REPO_ROOT / "scripts" / "rust" / "stage1a_gate.py"),
            *_replace_out(gate_args, temporary),
            "--expected-head",
            head,
        ]
        proc = subprocess.run(command, capture_output=True, text=True, check=False)
        if not temporary.is_file() or temporary.stat().st_size == 0:
            return _emit_rejudge({
                "same_judgment": False,
                "error": proc.stderr.strip() or "重判未產生 gate 報告",
                "rejudge_rc": proc.returncode,
            })
        fresh = json.loads(temporary.read_text(encoding="utf-8"))
        if not isinstance(fresh, dict):
            return _emit_rejudge({
                "same_judgment": False,
                "error": "重判 gate 不是 object",
                "rejudge_rc": proc.returncode,
            })
        same = _judgment_core(original) == _judgment_core(fresh)
        rc_lines = (evidence / "rc.txt").read_text(encoding="utf-8").splitlines()
        gate_steps = [line for line in rc_lines if line.split()[:1] == ["stage1a_gate"]]
        if len(gate_steps) != 1 or len(gate_steps[0].split()) != 2:
            raise ValueError("rc.txt 須恰有一筆 stage1a_gate 退出碼")
        int(gate_steps[0].split()[1])
        if rc_lines and rc_lines[-1].strip() == "DONE":
            rc_lines.pop()
        rc_text = "\n".join(f"stage1a_gate {proc.returncode}" if line == gate_steps[0] else line
                            for line in rc_lines)
        # DONE 是 driver 結束標記，不是測試步驟；原始檔不改寫。
        windows = None
        if any(line.split() == ["win_l15_func_test", "1"] for line in rc_lines):
            win_root = evidence / "func-test-windows"
            windows = {"scenarios": _read_json(win_root / "func-test-report.json"),
                       "basic": _read_json(win_root / "basic" / "func-test-report.json"),
                       "metadata": _read_json(win_root / "win-func-test-meta.json")}
        unresolved, suppressed = unresolved_steps(rc_text, fresh, windows, evidence_dir=evidence)
        payload = {
            "same_judgment": same,
            "saved_overall": original.get("overall"),
            "rejudge_overall": fresh.get("overall"),
            "rejudge_rc": proc.returncode,
            "baseline_known_defects": fresh.get("baseline_known_defects", []),
            "baseline_pending_classification": fresh.get("baseline_pending_classification", []),
            "failed_steps": len(unresolved), "unresolved": unresolved, "suppressed": suppressed,
            "exit_passed": proc.returncode == 0 and not unresolved and fresh.get("overall") in {"IMPLEMENTED", "VERIFIED"},
        }
        return _emit_rejudge(payload, assert_same=assert_same)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        return _emit_rejudge({"same_judgment": False, "error": str(exc)})
    finally:
        temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "--rejudge":
        return _rejudge(args[1:])
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
        unresolved, suppressed = unresolved_steps(rc_text, gate, evidence, evidence_dir=Path(args[0]).parent)
        summary = {"failed_steps": len(unresolved), "unresolved": unresolved, "suppressed": suppressed}
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        summary = {"failed_steps": 1, "unresolved": [{"step": "exit_driver_summary", "error": str(exc)}],
                   "suppressed": []}
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
