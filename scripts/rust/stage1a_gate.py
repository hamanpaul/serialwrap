#!/usr/bin/env python3
"""Stage 1A gate 判定器（P3-T18）。

彙整 A/B 比較報告、Windows 測試報告、L1.5 功能測試報告與 L1 完整性報告，
對照 cases.lock.json、l1_required.lock.json 與 coverage_1a.json，
判定 Stage 1A 是否滿足 IMPLEMENTED 或 VERIFIED 出口門檻。
"""
from __future__ import annotations

import argparse
import base64
import importlib.util
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import yaml


def _normalize_case_id(cid: str) -> str:
    """從 JUnit case_id 提取模組與函式名 (例如 test_bb_x.py::test_y)。"""
    if "::" in cid:
        cls, name = cid.split("::", 1)
    else:
        cls, name = "", cid

    name_clean = name.split("[")[0].strip()

    parts = cls.split(".")
    mod_part = None
    for p in parts:
        if p.startswith("test_"):
            mod_part = p
            break
    if mod_part:
        return f"{mod_part}.py::{name_clean}"

    if cls:
        if cls.endswith(".py"):
            return f"{cls}::{name_clean}"
        return f"{cls}::{name_clean}"
    return name_clean


def _l15_case_rows(
    case_id: str,
    meta: dict[str, Any],
    reports: list[tuple[str, dict[str, Any]]],
) -> list[dict[str, Any]]:
    platforms = meta.get("platforms")
    allowed_platforms = set(platforms) if isinstance(platforms, list) and platforms else None
    rows: list[dict[str, Any]] = []
    for platform, report in reports:
        if allowed_platforms is not None and platform not in allowed_platforms:
            continue
        results = report.get("results")
        if isinstance(results, list):
            rows.extend(
                row for row in results
                if isinstance(row, dict) and row.get("case_id") == case_id
            )
    return rows


def _l15_case_status(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "evidence-insufficient"
    if any(row.get("verdict") != "PASS" for row in rows):
        return "FAIL"
    if any(row.get("gate_eligible") is not True for row in rows):
        return "evidence-insufficient"
    return "PASS"


def _is_rust_pytest_case(case_id: str) -> bool:
    path, separator, _ = case_id.partition("::")
    return bool(separator) and Path(path).name.startswith("test_rust_") and path.endswith(".py")


def _pytest_junit_cases(path: Path | None, errors: list[str]) -> dict[str, list[str]]:
    cases: dict[str, list[str]] = {}
    if path is None:
        return cases
    if not path.is_file():
        errors.append(f"缺少 pytest JUnit 報告: {path}")
        return cases
    try:
        root = ET.parse(path).getroot()
    except Exception as e:
        errors.append(f"讀取 pytest JUnit 報告失敗: {e}")
        return cases

    report_failed = False
    for suite in root.iter("testsuite"):
        try:
            if int(suite.attrib.get("failures", 0)) or int(suite.attrib.get("errors", 0)):
                errors.append("pytest JUnit 報告含失敗或錯誤")
                report_failed = True
                break
        except (TypeError, ValueError):
            errors.append("pytest JUnit 報告的失敗計數無效")
            report_failed = True
            break

    testcases = list(root.iter("testcase"))
    if any(testcase.find("failure") is not None or testcase.find("error") is not None for testcase in testcases):
        report_failed = True

    for testcase in testcases:
        nodeid = testcase.get("nodeid")
        if nodeid:
            case_id = _normalize_case_id(nodeid)
            if case_id.startswith("tests/"):
                case_id = case_id[len("tests/"):]
        else:
            case_id = _normalize_case_id(f"{testcase.get('classname', '')}::{testcase.get('name', '')}")
        if not case_id:
            continue
        if report_failed or testcase.find("failure") is not None or testcase.find("error") is not None:
            outcome = "failed"
        elif testcase.find("skipped") is not None:
            outcome = "skipped"
        else:
            outcome = "passed"
        cases.setdefault(case_id, []).append(outcome)
    return cases


WINDOWS_COM_FORMAT = "windows-com-evidence.v1"
WINDOWS_COM_PAIR = "COM20:COM21"
WINDOWS_COM_P4_NODE = "tests/contract_blackbox/test_cli_mgmt_daemon.py::test_autostart_l15_tx_once"
WINDOWS_COM_SEQUENCE = (
    "python-daemon-start", "session-list-ready", "cmd-submit", "cmd-status-poll",
    "device-release", "session-list-released", "device-attach", "session-list-ready",
)


def _dedupe_consecutive(names: list[str]) -> list[str]:
    out: list[str] = []
    for name in names:
        if not out or out[-1] != name:
            out.append(name)
    return out


def _read_json_dict(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _windows_com_verdict(evidence_dir: Path) -> tuple[str, list[str]]:
    """只接受 `<evidence_dir>/windows-com.json`（與 win-blackbox.ps1 的 OutDir 同層）並逐項驗證。

    不採信任何 `verdict`／`windows_com` 頂層欄位以外的後門（`report.json`、`com-selftest.json` 不再參與判定）。
    回傳 (狀態, 原因清單)；狀態為 PASS、FAIL（doc 明示失敗）或 BLOCKED-OWNER（證據缺漏或不符）。
    """
    doc = _read_json_dict(evidence_dir / "windows-com.json")
    if doc is None:
        return "BLOCKED-OWNER", ["缺少或無法解析 windows-com.json"]
    if doc.get("windows_com") == "FAIL":
        return "FAIL", ["windows-com.json 明示 FAIL"]
    reasons: list[str] = []
    if doc.get("format") != WINDOWS_COM_FORMAT:
        reasons.append(f"format 不符：{doc.get('format')!r}")
    if doc.get("com_pair") != WINDOWS_COM_PAIR:
        reasons.append(f"com_pair 不符：{doc.get('com_pair')!r}")
    if doc.get("windows_com") != "PASS":
        reasons.append(f"windows_com 非 PASS：{doc.get('windows_com')!r}")
    p4 = doc.get("p4_case")
    if not (isinstance(p4, dict) and p4.get("nodeid") == WINDOWS_COM_P4_NODE and p4.get("outcome") == "passed"):
        reasons.append(f"p4_case 非凍結案例 passed：{p4!r}")
    bridge_stop = doc.get("bridge_stop")
    if not (isinstance(bridge_stop, dict) and bridge_stop.get("status") == "PASS"):
        reasons.append("bridge_stop 非 PASS")

    com_dir = evidence_dir / "windows-com"
    # S0 自測：以 OutDir 內相對路徑讀取，不採信 JSON 內的 Windows 絕對路徑。
    selftest = _read_json_dict(com_dir / "bridge-selftest" / "selftest-report.json")
    if selftest is None or selftest.get("verdict") != "PASS":
        reasons.append("bridge-selftest/selftest-report.json 缺漏或 verdict 非 PASS")

    # 序列：步驟依序為凍結清單，允許結尾的 daemon-stop（finally 清理）；所有含 returncode 的步驟必須為 0。
    sequence = _read_json_dict(com_dir / "sequence-result.json")
    if sequence is None or sequence.get("result") != "PASS":
        reasons.append("sequence-result.json 缺漏或 result 非 PASS")
    elif isinstance(sequence.get("steps"), list):
        steps = [s for s in sequence["steps"] if isinstance(s, dict)]
        deduped = _dedupe_consecutive([str(s.get("name")) for s in steps])
        expected = list(WINDOWS_COM_SEQUENCE)
        tail = deduped[len(expected):]
        if deduped[:len(expected)] != expected or tail not in ([], ["daemon-stop"]):
            reasons.append(f"sequence 步驟順序不符：{deduped}")
        bad = [s.get("name") for s in steps if "returncode" in s and s.get("returncode") != 0]
        if bad:
            reasons.append(f"sequence 步驟 returncode 非 0：{bad}")
    else:
        reasons.append("sequence-result.json 缺少 steps")

    # wire：DUT 端必須有 dut_stdin_write 的 TX，內容含 `echo hi`。
    wire = com_dir / "bridge" / "wire.ndjson"
    found_tx = False
    if wire.is_file():
        for line in wire.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if not isinstance(row, dict) or row.get("dir") != "TX" or row.get("observation") != "dut_stdin_write":
                continue
            try:
                payload = base64.b64decode(str(row.get("payload_b64", "")), validate=True)
            except (ValueError, TypeError):
                continue
            if b"echo hi" in payload:
                found_tx = True
                break
    if not found_tx:
        reasons.append("wire.ndjson 缺少 dut_stdin_write TX（echo hi）或檔案不存在")

    return ("PASS" if not reasons else "BLOCKED-OWNER"), reasons


def judge_stage1a(
    ab_report_path: Path | None,
    l1_results_dir: Path,
    out_path: Path,
    win_report_path: Path | None = None,
    l15_report_inputs: list[tuple[str, Path]] | None = None,
    pytest_junit_path: Path | None = None,
    windows_com_dir: Path | None = None,
    repo_root: Path | None = None,
) -> int:
    if repo_root is None:
        repo_root = Path(__file__).resolve().parents[2]

    acc_dir = repo_root / "tests" / "acceptance"
    cases_yaml_path = acc_dir / "cases.yaml"
    cases_lock_path = acc_dir / "cases.lock.json"
    l1_lock_path = acc_dir / "l1_required.lock.json"
    coverage_path = repo_root / "tests" / "contract_blackbox" / "data" / "coverage_1a.json"

    errors: list[str] = []

    # 1. 分母（cases.lock.json 與 coverage_1a.json）
    if not cases_lock_path.is_file():
        errors.append(f"cases.lock.json 不存在: {cases_lock_path}")
        cli_1a_cases: list[str] = []
    else:
        try:
            lock_data = json.loads(cases_lock_path.read_text(encoding="utf-8"))
            cli_1a_cases = lock_data.get("sets", {}).get("cli_1A", [])
            if not isinstance(cli_1a_cases, list) or not cli_1a_cases:
                errors.append("cases.lock.json 缺少 sets.cli_1A 或清單為空")
        except Exception as e:
            errors.append(f"讀取 cases.lock.json 失敗: {e}")
            cli_1a_cases = []

    if not coverage_path.is_file():
        errors.append(f"coverage_1a.json 不存在: {coverage_path}")
        cov_data: dict[str, Any] | None = None
        p3_extra: list[str] = []
    else:
        try:
            cov_data = json.loads(coverage_path.read_text(encoding="utf-8"))
            p3_extra = cov_data.get("p3_extra", []) if isinstance(cov_data, dict) else []
        except Exception as e:
            errors.append(f"讀取 coverage_1a.json 失敗: {e}")
            cov_data = None
            p3_extra = []

    required_cases = sorted(set(cli_1a_cases) | set(p3_extra))
    if not required_cases:
        errors.append("Stage 1A 必要案例集合為空，證據不足")

    # 2. cases.yaml 元資料
    case_meta: dict[str, dict[str, Any]] = {}
    if cases_yaml_path.is_file():
        try:
            cases_doc = yaml.safe_load(cases_yaml_path.read_text(encoding="utf-8")) or {}
            c_list = cases_doc.get("cases", []) if isinstance(cases_doc, dict) else []
            for c in c_list:
                if isinstance(c, dict) and "case_id" in c:
                    case_meta[c["case_id"]] = c
        except Exception as e:
            errors.append(f"讀取 cases.yaml 失敗: {e}")

    # 3. ab-report
    ab_cases: dict[str, dict[str, Any]] = {}
    ab_cases_by_norm: dict[str, list[dict[str, Any]]] = {}
    ab_result = "fail"
    if ab_report_path and ab_report_path.is_file():
        try:
            ab_data = json.loads(ab_report_path.read_text(encoding="utf-8"))
            ab_result = ab_data.get("result", "fail")
            for c in ab_data.get("cases", []):
                if isinstance(c, dict) and "id" in c:
                    ab_cases[c["id"]] = c
                    norm = c.get("case") or _normalize_case_id(c["id"])
                    ab_cases_by_norm.setdefault(norm, []).append(c)
        except Exception as e:
            errors.append(f"讀取 ab-report 失敗: {e}")
    else:
        errors.append("缺少 ab-report")

    # 4. L1.5 報告依平台標記；缺檔或格式錯誤不提供可通過的案例證據。
    l15_reports: list[tuple[str, dict[str, Any]]] = []
    for platform, report_path in l15_report_inputs or []:
        if not report_path.is_file():
            errors.append(f"缺少 {platform} l15-report: {report_path}")
            continue
        try:
            report_data = json.loads(report_path.read_text(encoding="utf-8"))
            if not isinstance(report_data, dict):
                errors.append(f"{platform} l15-report 格式錯誤: {report_path}")
                continue
            l15_reports.append((platform, report_data))
        except Exception as e:
            errors.append(f"讀取 {platform} l15-report 失敗: {e}")

    pytest_cases = _pytest_junit_cases(pytest_junit_path, errors)

    # 5. Windows COM：--windows-com 指向 OutDir；逐項驗證 windows-com.json 與其引用的子證據。
    windows_com_reasons: list[str] = []
    if windows_com_dir and windows_com_dir.is_dir():
        windows_com, windows_com_reasons = _windows_com_verdict(windows_com_dir)
    else:
        windows_com = "BLOCKED-OWNER"
        windows_com_reasons = ["未提供 Windows COM 證據目錄（--windows-com）"]

    # 6. Windows L1
    if win_report_path and win_report_path.is_file():
        try:
            win_data = json.loads(win_report_path.read_text(encoding="utf-8"))
            if not isinstance(win_data, dict):
                windows_l1 = "evidence-insufficient"
            elif "result" in win_data:
                # win-blackbox.sh 格式
                imports_ok = win_data.get("imports", {}).get("ok") if isinstance(win_data.get("imports"), dict) else True
                startup_ok = win_data.get("startup_100", {}).get("ok") if isinstance(win_data.get("startup_100"), dict) else True
                if win_data.get("result") == "pass" and imports_ok is not False and startup_ok is not False:
                    windows_l1 = "PASS"
                elif win_data.get("result") == "fail":
                    windows_l1 = "FAIL"
                else:
                    windows_l1 = "evidence-insufficient"
            elif "overall" in win_data:
                # win-test.sh 格式
                if win_data.get("overall") == "pass" and win_data.get("l1_verdict") == "PASS":
                    windows_l1 = "PASS"
                elif win_data.get("overall") == "fail" or win_data.get("l1_verdict") == "FAIL":
                    windows_l1 = "FAIL"
                else:
                    windows_l1 = "evidence-insufficient"
            elif "exit" in win_data and "failed" in win_data:
                # Oracle fixture 格式 (test_rust_stage1a_gate.py)
                if win_data.get("exit") == 0 and win_data.get("failed") == 0:
                    windows_l1 = "PASS"
                else:
                    windows_l1 = "FAIL"
            else:
                windows_l1 = "evidence-insufficient"
        except Exception as e:
            windows_l1 = "evidence-insufficient"
            errors.append(f"讀取 win-report 失敗: {e}")
    else:
        windows_l1 = "evidence-insufficient"

    # 7. 逐 case 對帳
    case_rows: list[dict[str, Any]] = []
    for cid in required_cases:
        meta = case_meta.get(cid, {})
        layer = meta.get("layer", "L1")
        evidence = None

        if layer == "L1":
            source_ref = meta.get("source_ref")
            prefix = "tests/contract_blackbox/"
            if isinstance(source_ref, str) and source_ref.startswith(prefix):
                source_case = source_ref[len(prefix):]
                matching_rows = ab_cases_by_norm.get(source_case, [])
                if not matching_rows:
                    status = "evidence-insufficient"
                elif all(r.get("class") in ("ok", "rust-only-ok") for r in matching_rows):
                    status = "ok"
                    evidence = matching_rows
                else:
                    failing = [r for r in matching_rows if r.get("class") not in ("ok", "rust-only-ok")]
                    status = failing[0].get("class", "regression") if failing else "evidence-insufficient"
                    evidence = matching_rows
            elif cid in ab_cases:
                abc = ab_cases[cid]
                status = abc.get("class", "evidence-insufficient")
                evidence = abc
            elif cid in ab_cases_by_norm:
                matching_rows = ab_cases_by_norm[cid]
                if all(r.get("class") in ("ok", "rust-only-ok") for r in matching_rows):
                    status = "ok"
                    evidence = matching_rows
                else:
                    failing = [r for r in matching_rows if r.get("class") not in ("ok", "rust-only-ok")]
                    status = failing[0].get("class", "regression") if failing else "evidence-insufficient"
                    evidence = matching_rows
            else:
                status = "evidence-insufficient"
        elif layer == "L1.5":
            case_l15 = _l15_case_rows(cid, meta, l15_reports)
            status = _l15_case_status(case_l15)
            evidence = case_l15 or None
        elif layer in ("L2", "L3"):
            has_l15_docker_row = any(
                report.get("evidence_layer") == "L1.5-docker"
                and _l15_case_rows(cid, meta, [(platform, report)])
                for platform, report in l15_reports
            )
            if has_l15_docker_row:
                status = "evidence-insufficient"
            else:
                status = "BLOCKED-OWNER"
        else:
            status = "evidence-insufficient"

        case_rows.append(
            {
                "id": cid,
                "layer": layer,
                "status": status,
                "evidence": evidence,
            }
        )

    # 8. coverage_1a.json 檢查
    coverage_rows: list[dict[str, Any]] = []
    coverage_ok = True
    if not cov_data or not isinstance(cov_data, dict) or not cov_data.get("rows") or not cov_data.get("invariants"):
        errors.append("coverage_1a.json 為空或缺少必要 rows/invariants")
        coverage_ok = False
    else:
        c_rows = cov_data.get("rows", [])
        if len(c_rows) < 55:
            errors.append(f"coverage_1a.json rows 數量不足: {len(c_rows)} < 55")
            coverage_ok = False

        for row in c_rows:
            cmd = row.get("command", "")
            is_data_plane = row.get("data_plane", False)
            stubs = row.get("stub", [])
            e2e = row.get("e2e", [])
            l15 = row.get("l15", [])

            row_status = "ok"
            if not stubs:
                row_status = "evidence-insufficient"
                coverage_ok = False
            else:
                for sc in stubs:
                    sc_ok = (sc in ab_cases and ab_cases[sc].get("class") in ("ok", "rust-only-ok")) or \
                            (sc in ab_cases_by_norm and all(r.get("class") in ("ok", "rust-only-ok") for r in ab_cases_by_norm[sc]))
                    if not sc_ok:
                        row_status = "evidence-insufficient"
                        coverage_ok = False
                        break

            if is_data_plane:
                has_e2e_or_l15 = False
                for ec in e2e:
                    if (ec in ab_cases and ab_cases[ec].get("class") in ("ok", "rust-only-ok")) or \
                       (ec in ab_cases_by_norm and all(r.get("class") in ("ok", "rust-only-ok") for r in ab_cases_by_norm[ec])):
                        has_e2e_or_l15 = True
                        break
                if not has_e2e_or_l15:
                    for lc in l15:
                        l15_meta = case_meta.get(lc, {})
                        if _l15_case_status(_l15_case_rows(lc, l15_meta, l15_reports)) == "PASS":
                            has_e2e_or_l15 = True
                            break

                if not has_e2e_or_l15:
                    row_status = "evidence-insufficient"
                    coverage_ok = False

            coverage_rows.append(
                {
                    "id": row.get("id"),
                    "command": cmd,
                    "status": row_status,
                    "stub": stubs,
                    "e2e": e2e,
                    "l15": l15,
                }
            )

    # 9. 安全不變量檢查
    cargo_pass_nodes = set()
    linux_cargo_rep = l1_results_dir / "linux" / "cargo" / "resource-report.json"
    if linux_cargo_rep.is_file():
        try:
            c_doc = json.loads(linux_cargo_rep.read_text(encoding="utf-8"))
            for r in c_doc.get("results", []):
                if isinstance(r, dict) and r.get("outcome") == "passed":
                    cargo_pass_nodes.add(r.get("nodeid", ""))
        except Exception:
            pass

    invariant_rows: list[dict[str, Any]] = []
    invariants_ok = True
    if cov_data:
        inv_list = cov_data.get("invariants", [])
        if len(inv_list) < 5:
            errors.append(f"安全不變量數量不足: {len(inv_list)} < 5")
            invariants_ok = False

        has_rust_pytest_cases = any(
            _is_rust_pytest_case(case_id)
            for inv in inv_list
            for case_id in inv.get("cases", [])
        )
        if has_rust_pytest_cases and pytest_junit_path is None:
            errors.append("安全不變量含 test_rust_ pytest 案例，但缺少 pytest JUnit 報告")

        for inv in inv_list:
            inv_id = inv.get("id")
            title = inv.get("title")
            inv_cases = inv.get("cases", [])
            missing_or_failed = []

            if not inv_cases:
                missing_or_failed.append("<empty-cases>")

            for c in inv_cases:
                passed = False
                if _is_rust_pytest_case(c):
                    outcomes = pytest_cases.get(_normalize_case_id(c), [])
                    passed = bool(outcomes) and all(outcome == "passed" for outcome in outcomes)
                elif (c in ab_cases and ab_cases[c].get("class") in ("ok", "rust-only-ok")) or \
                   (c in ab_cases_by_norm and all(r.get("class") in ("ok", "rust-only-ok") for r in ab_cases_by_norm[c])):
                    passed = True
                elif any(node.endswith(c) or ("::" in c and node.endswith(c.split("::")[-1])) for node in cargo_pass_nodes):
                    passed = True
                elif _l15_case_status(_l15_case_rows(c, case_meta.get(c, {}), l15_reports)) == "PASS":
                    passed = True
                if not passed:
                    missing_or_failed.append(c)

            if missing_or_failed:
                inv_status = "FAIL"
                invariants_ok = False
            else:
                inv_status = "PASS"

            invariant_rows.append(
                {
                    "id": inv_id,
                    "title": title,
                    "status": inv_status,
                    "cases": inv_cases,
                    "missing_or_failed": missing_or_failed,
                }
            )

    # 10. L1 完整性檢查（R5-F3）
    l1_rows: list[dict[str, Any]] = []
    l1_ok = True

    if not l1_lock_path.is_file():
        errors.append(f"l1_required.lock.json 不存在: {l1_lock_path}")
        l1_ok = False
    else:
        try:
            l1_lock = json.loads(l1_lock_path.read_text(encoding="utf-8"))
        except Exception as e:
            errors.append(f"讀取 l1_required.lock.json 失敗: {e}")
            l1_lock = {}
            l1_ok = False

        if l1_ok:
            judge_mod = None
            judge_script = acc_dir / "l1_judge.py"
            if judge_script.is_file():
                try:
                    spec = importlib.util.spec_from_file_location("l1_judge", judge_script)
                    if spec and spec.loader:
                        judge_mod = importlib.util.module_from_spec(spec)
                        spec.loader.exec_module(judge_mod)
                except Exception as e:
                    errors.append(f"載入 l1_judge.py 失敗: {e}")
                    judge_mod = None

            check_targets = [
                ("s1a_blackbox", "linux", "blackbox"),
                ("s1a_blackbox", "windows", "blackbox"),
                ("s1a_delegation", "linux", "blackbox"),
                ("s1a_delegation", "windows", "blackbox"),
                ("s1a_cargo", "linux", "cargo"),
                ("s1a_cargo", "windows", "cargo"),
            ]

            for set_id, platform, subfolder in check_targets:
                set_info = (
                    l1_lock.get("sets", {}).get(set_id, {}).get(platform)
                )
                if not set_info:
                    l1_rows.append(
                        {
                            "set_id": set_id,
                            "platform": platform,
                            "verdict": "evidence-insufficient",
                            "reasons": [f"set {set_id} / {platform} not in lock"],
                            "missing": [],
                            "skipped_required": [],
                            "duplicates": [],
                        }
                    )
                    l1_ok = False
                    continue

                rep_file = l1_results_dir / platform / subfolder / "resource-report.json"
                if not rep_file.is_file():
                    l1_rows.append(
                        {
                            "set_id": set_id,
                            "platform": platform,
                            "verdict": "evidence-insufficient",
                            "reasons": [f"report file missing: {rep_file}"],
                            "missing": [],
                            "skipped_required": [],
                            "duplicates": [],
                        }
                    )
                    l1_ok = False
                    continue

                try:
                    rep = json.loads(rep_file.read_text(encoding="utf-8"))
                except Exception as e:
                    l1_rows.append(
                        {
                            "set_id": set_id,
                            "platform": platform,
                            "verdict": "evidence-insufficient",
                            "reasons": [f"report file unparseable: {e}"],
                            "missing": [],
                            "skipped_required": [],
                            "duplicates": [],
                        }
                    )
                    l1_ok = False
                    continue

                required_nodes = list(set_info.get("nodeids", []))
                lock_sel = set_info.get("selector", {})

                if judge_mod and hasattr(judge_mod, "judge"):
                    junit_dict = None
                    if subfolder == "blackbox":
                        junit_file = l1_results_dir / platform / subfolder / "junit.xml"
                        if junit_file.is_file():
                            try:
                                root = ET.parse(junit_file).getroot()
                                ts = root.find(".//testsuite")
                                if ts is None:
                                    ts = root
                                junit_dict = {
                                    "tests": int(ts.attrib.get("tests", 0)),
                                    "failures": int(ts.attrib.get("failures", 0)),
                                    "errors": int(ts.attrib.get("errors", 0)),
                                    "skipped": int(ts.attrib.get("skipped", 0)),
                                }
                            except Exception:
                                pass
                    judged = judge_mod.judge(rep, required_nodes, platform=platform, junit=junit_dict, selector=lock_sel)
                    v_raw = judged.get("verdict", "INVALID")
                    if v_raw == "PASS":
                        verdict = "PASS"
                    elif v_raw == "FAIL":
                        verdict = "FAIL"
                    elif v_raw == "INSUFFICIENT":
                        verdict = "INSUFFICIENT"
                    else:
                        verdict = "evidence-insufficient"
                    reasons = list(judged.get("reasons", []))
                    missing = list(judged.get("missing", []))
                    skipped_required = list(judged.get("skipped_required", []))
                    duplicates = list(judged.get("duplicates", []))
                else:
                    # 計畫 R5-F3：L1 判定一律依路徑載入 P2 Task 13 的 l1_judge.py（唯一判定實作），
                    # 不得自寫判定代替；缺檔或無 judge() 時該列記證據不足，不得轉 PASS。
                    verdict = "evidence-insufficient"
                    reasons = [f"l1_judge.py 缺少或無 judge()（P2 Task 13 前置缺口）: {judge_script}"]
                    missing = []
                    skipped_required = []
                    duplicates = []

                if verdict != "PASS":
                    l1_ok = False

                l1_rows.append(
                    {
                        "set_id": set_id,
                        "platform": platform,
                        "verdict": verdict,
                        "reasons": reasons,
                        "missing": missing,
                        "skipped_required": skipped_required,
                        "duplicates": duplicates,
                    }
                )

    # 11. 整體判定
    has_case_fail = any(
        r["status"] not in ("ok", "rust-only-ok", "PASS", "BLOCKED-OWNER", "not-applicable") for r in case_rows
    )
    if (
        errors
        or has_case_fail
        or not coverage_ok
        or not invariants_ok
        or not l1_ok
        or windows_l1 != "PASS"
        or ab_result != "pass"
        or windows_com == "FAIL"
    ):
        overall = "FAIL"
    else:
        if windows_com == "BLOCKED-OWNER" or any(
            r["status"] == "BLOCKED-OWNER" for r in case_rows
        ):
            overall = "IMPLEMENTED"
        else:
            overall = "VERIFIED"

    envelope = {
        "required": required_cases,
        "cases": case_rows,
        "invariants": invariant_rows,
        "coverage": coverage_rows,
        "l1": l1_rows,
        "windows_com": windows_com,
        "windows_com_reasons": windows_com_reasons,
        "windows_l1": windows_l1,
        "overall": overall,
        "errors": errors,
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(envelope, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    return 0 if overall in ("IMPLEMENTED", "VERIFIED") else 1


def main(argv: list[str] | None = None) -> int:
    def parse_l15_report(value: str) -> tuple[str, Path]:
        for platform in ("linux", "windows"):
            prefix = f"{platform}="
            if value.startswith(prefix):
                path = value[len(prefix):]
                if not path:
                    raise argparse.ArgumentTypeError("L1.5 報告路徑不可為空")
                return platform, Path(path)
        if not value:
            raise argparse.ArgumentTypeError("L1.5 報告路徑不可為空")
        return "linux", Path(value)

    parser = argparse.ArgumentParser(description="Stage 1A gate 判定器")
    parser.add_argument("--ab-report", type=Path, default=None, help="A/B 比較報告 JSON")
    parser.add_argument(
        "--l1-results", required=True, type=Path, help="L1 結果目錄 (含各平台 blackbox 與 cargo)"
    )
    parser.add_argument("--out", required=True, type=Path, help="輸出 gate 報告 JSON 路徑")
    parser.add_argument(
        "--win-report", type=Path, default=None, help="Windows 測試報告 JSON"
    )
    parser.add_argument(
        "--l15-report", action="append", type=parse_l15_report, default=[],
        help="L1.5 功能測試報告；未帶平台前綴時視為 linux，也可指定 linux=<path> 或 windows=<path>；可重複指定",
    )
    parser.add_argument(
        "--pytest-junit-report", type=Path, default=None,
        help="SW_REQUIRE_CARGO=1 完整 pytest 執行的 JUnit XML",
    )
    parser.add_argument(
        "--windows-com", type=Path, default=None,
        help="Windows COM 證據 OutDir（須含 windows-com.json；win-blackbox.ps1 的輸出根目錄）",
    )
    parser.add_argument(
        "--repo", type=Path, default=None, help="Repo 根目錄（預設自動推算）"
    )

    args = parser.parse_args(argv)
    return judge_stage1a(
        ab_report_path=args.ab_report,
        l1_results_dir=args.l1_results,
        out_path=args.out,
        win_report_path=args.win_report,
        l15_report_inputs=args.l15_report,
        pytest_junit_path=args.pytest_junit_report,
        windows_com_dir=args.windows_com,
        repo_root=args.repo,
    )


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
