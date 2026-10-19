#!/usr/bin/env python3
"""Stage 1A gate 判定器（P3-T18）。

彙整 A/B 比較報告、Windows 測試報告、L1.5 功能測試報告與 L1 完整性報告，
對照 cases.lock.json、l1_required.lock.json 與 coverage_1a.json，
判定 Stage 1A 是否滿足 IMPLEMENTED 或 VERIFIED 出口門檻。
"""
from __future__ import annotations

import argparse
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


def _normalize_argv(argv: list[str]) -> list[str]:
    """去除路徑參數 (--junitxml=..., --out ...) 用於比對 selector.argv。"""
    out = []
    skip_next = False
    for arg in argv:
        if skip_next:
            skip_next = False
            continue
        if arg in ("--junitxml", "--out", "--report"):
            skip_next = True
            continue
        if arg.startswith("--junitxml=") or arg.startswith("--out=") or arg.startswith("--report="):
            continue
        out.append(arg)
    return out


def judge_stage1a(
    ab_report_path: Path | None,
    l1_results_dir: Path,
    out_path: Path,
    win_report_path: Path | None = None,
    l15_report_path: Path | None = None,
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

    # 4. l15-report
    l15_data: dict[str, Any] | None = None
    if l15_report_path and l15_report_path.is_file():
        try:
            l15_data = json.loads(l15_report_path.read_text(encoding="utf-8"))
        except Exception as e:
            errors.append(f"讀取 l15-report 失敗: {e}")

    # 5. Windows COM
    windows_com = "BLOCKED-OWNER"
    if windows_com_dir and windows_com_dir.is_dir():
        has_pass = False
        for report_name in ("report.json", "windows-com.json", "com-selftest.json"):
            rf = windows_com_dir / report_name
            if rf.is_file():
                try:
                    rep_doc = json.loads(rf.read_text(encoding="utf-8"))
                    if rep_doc.get("windows_com") == "PASS" or rep_doc.get("verdict") == "PASS":
                        has_pass = True
                        break
                except Exception:
                    pass
        if has_pass:
            windows_com = "PASS"
        else:
            windows_com = "BLOCKED-OWNER"
    else:
        windows_com = "BLOCKED-OWNER"

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
            if cid in ab_cases:
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
            if l15_data and isinstance(l15_data, dict):
                l15_results = l15_data.get("results")
                if isinstance(l15_results, list):
                    case_l15 = [r for r in l15_results if isinstance(r, dict) and r.get("case_id") == cid]
                    if not case_l15:
                        status = "evidence-insufficient"
                    elif any(r.get("verdict") != "PASS" for r in case_l15):
                        status = "FAIL"
                        evidence = case_l15
                    elif any(r.get("gate_eligible") is not True for r in case_l15):
                        status = "evidence-insufficient"
                        evidence = case_l15
                    else:
                        status = "PASS"
                        evidence = case_l15
                elif l15_data.get("pass") and l15_data.get("gate_eligible"):
                    status = "PASS"
                    evidence = l15_data
                else:
                    status = "evidence-insufficient"
            else:
                status = "evidence-insufficient"
        elif layer in ("L2", "L3"):
            if l15_data and isinstance(l15_data, dict) and l15_data.get("evidence_layer") == "L1.5-docker":
                l15_results = l15_data.get("results") or []
                if any(isinstance(r, dict) and r.get("case_id") == cid for r in l15_results):
                    status = "evidence-insufficient"
                else:
                    status = "BLOCKED-OWNER"
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
                if not has_e2e_or_l15 and l15_data and isinstance(l15_data, dict):
                    l15_results = l15_data.get("results")
                    if isinstance(l15_results, list):
                        for lc in l15:
                            if any(isinstance(r, dict) and r.get("case_id") == lc and r.get("verdict") == "PASS" and r.get("gate_eligible") is True for r in l15_results):
                                has_e2e_or_l15 = True
                                break
                    elif l15 and l15_data.get("pass") and l15_data.get("gate_eligible"):
                        has_e2e_or_l15 = True

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

        for inv in inv_list:
            inv_id = inv.get("id")
            title = inv.get("title")
            inv_cases = inv.get("cases", [])
            missing_or_failed = []

            if not inv_cases:
                missing_or_failed.append("<empty-cases>")

            for c in inv_cases:
                passed = False
                if (c in ab_cases and ab_cases[c].get("class") in ("ok", "rust-only-ok")) or \
                   (c in ab_cases_by_norm and all(r.get("class") in ("ok", "rust-only-ok") for r in ab_cases_by_norm[c])):
                    passed = True
                elif any(node.endswith(c) or ("::" in c and node.endswith(c.split("::")[-1])) for node in cargo_pass_nodes):
                    passed = True
                elif l15_data and isinstance(l15_data, dict):
                    l15_results = l15_data.get("results")
                    if isinstance(l15_results, list):
                        if any(isinstance(r, dict) and r.get("case_id") == c and r.get("verdict") == "PASS" for r in l15_results):
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
                    reasons: list[str] = []
                    rep_format = rep.get("format", "")
                    rep_platform = rep.get("platform", "")
                    if rep_format != "l1-results.v1":
                        reasons.append(f"format invalid: {rep_format}")
                    if rep_platform != platform:
                        reasons.append(f"platform mismatch: {rep_platform} != {platform}")

                    runner = rep.get("runner", "")
                    exit_code = rep.get("exit_code", 0)
                    finished = rep.get("finished", False)

                    lock_argv = lock_sel.get("argv", [])
                    rep_argv = rep.get("argv", [])

                    if runner == "cargo":
                        if _normalize_argv(rep_argv) != _normalize_argv(lock_argv):
                            reasons.append(f"selector mismatch: {rep_argv} != {lock_argv}")
                    elif runner == "pytest":
                        norm_lock = _normalize_argv(lock_argv)
                        norm_rep = _normalize_argv(rep_argv)
                        it = iter(norm_rep)
                        if not all(arg in it for arg in norm_lock):
                            reasons.append(f"selector mismatch: {lock_argv} not in {rep_argv}")

                    rep_results = rep.get("results", [])
                    rep_collected = rep.get("collected", [])

                    node_counts: dict[str, int] = {}
                    for r in rep_results:
                        n = r.get("nodeid", "")
                        node_counts[n] = node_counts.get(n, 0) + 1
                    for n in rep_collected:
                        node_counts[n] = max(node_counts.get(n, 0), rep_collected.count(n))
                    duplicates = [n for n, c in node_counts.items() if c > 1]
                    if duplicates:
                        reasons.append("duplicate nodeid")

                    if subfolder == "blackbox":
                        junit_file = l1_results_dir / platform / subfolder / "junit.xml"
                        if junit_file.is_file():
                            try:
                                root = ET.parse(junit_file).getroot()
                                ts = root.find(".//testsuite")
                                if ts is None:
                                    ts = root
                                j_tests = int(ts.attrib.get("tests", 0))
                                j_fails = int(ts.attrib.get("failures", 0))
                                j_errs = int(ts.attrib.get("errors", 0))
                                j_skips = int(ts.attrib.get("skipped", 0))

                                r_fails = sum(1 for r in rep_results if r.get("outcome") in ("failed", "error"))
                                r_skips = sum(1 for r in rep_results if r.get("outcome") in ("skipped", "ignored", "xfail", "xfailed"))
                                if j_tests != len(rep_results) or (j_fails + j_errs) != r_fails or j_skips != r_skips:
                                    reasons.append("junit/l1-results mismatch")
                            except Exception as e:
                                reasons.append(f"junit parse error: {e}")

                    results_by_node = {r.get("nodeid"): r for r in rep_results}
                    missing = sorted(set(required_nodes) - set(results_by_node.keys()))

                    skipped_required = [
                        r
                        for r in rep_results
                        if r.get("nodeid") in set(required_nodes)
                        and r.get("outcome") in ("skipped", "ignored", "xfail", "xfailed")
                    ]
                    failed = [
                        r for r in rep_results if r.get("outcome") in ("failed", "error", "xpassed")
                    ]

                    if not finished:
                        reasons.append("runner did not finish")
                    if exit_code != 0 and not failed:
                        reasons.append(f"runner exit {exit_code} != 0")

                    not_passed = [
                        r for r in rep_results if r.get("nodeid") in set(required_nodes) and r.get("outcome") != "passed"
                    ]

                    if any("selector mismatch" in r for r in reasons) or any("format invalid" in r for r in reasons) or any("platform mismatch" in r for r in reasons) or any("junit" in r for r in reasons):
                        verdict = "evidence-insufficient"
                    elif failed:
                        verdict = "FAIL"
                    elif missing or skipped_required or duplicates or reasons or not_passed:
                        verdict = "INSUFFICIENT"
                    else:
                        verdict = "PASS"

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
        "--l15-report", type=Path, default=None, help="L1.5 功能測試報告 JSON"
    )
    parser.add_argument(
        "--windows-com", type=Path, default=None, help="Windows COM 驗證目錄"
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
        l15_report_path=args.l15_report,
        windows_com_dir=args.windows_com,
        repo_root=args.repo,
    )


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
