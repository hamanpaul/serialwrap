#!/usr/bin/env python3
"""A/B 比較器（P3-T18）。

比對 A 輪（Python 基線）與 B 輪（Rust CLI）的黑箱測試 JUnit 輸出與身分資訊，
依計畫 §Task 18 的分類表進行逐案分類與整體 gate 判定。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any


def _compute_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


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


def _parse_junit(path: Path) -> tuple[dict[str, dict[str, str]], list[str]]:
    cases: dict[str, dict[str, str]] = {}
    errors: list[str] = []
    seen: set[str] = set()

    if not path.is_file() or path.stat().st_size == 0:
        errors.append(f"empty-report: {path.name} 為空或不存在")
        return cases, errors

    try:
        tree = ET.parse(path)
        root = tree.getroot()
    except Exception as e:
        errors.append(f"unparseable: {path.name}: {e}")
        return cases, errors

    testcases = root.findall(".//testcase")
    if not testcases:
        errors.append(f"empty-report: {path.name} 不含任何 testcase")
        return cases, errors

    for tc in testcases:
        cls = tc.attrib.get("classname", "")
        name = tc.attrib.get("name", "")
        cid = f"{cls}::{name}" if cls else name
        if cid in seen:
            errors.append(f"duplicate-id: {path.name} 包含重複 case_id: {cid}")
        seen.add(cid)

        failures = tc.findall("failure")
        errors_elems = tc.findall("error")
        skipped = tc.findall("skipped")

        outcome = "pass"
        message = ""
        if failures:
            outcome = "fail"
            message = failures[0].attrib.get("message") or failures[0].text or ""
        elif errors_elems:
            outcome = "error"
            message = errors_elems[0].attrib.get("message") or errors_elems[0].text or ""
        elif skipped:
            sk = skipped[0]
            sk_type = sk.attrib.get("type", "")
            message = sk.attrib.get("message") or sk.text or ""
            if "xfail" in sk_type:
                outcome = "xfail"
            else:
                outcome = "skip"

        cases[cid] = {
            "outcome": outcome,
            "message": message,
            "case": _normalize_case_id(cid),
        }

    return cases, errors


def _validate_identity(
    side: str, id_data: dict[str, Any], case_ids: list[str]
) -> list[str]:
    errors: list[str] = []
    if not isinstance(id_data, dict):
        return [f"identity-{side} 必須為 JSON 物件 (dict)"]

    required_str_fields = [
        "cli_path",
        "cli_sha256",
        "cli_implementation",
        "daemon_version",
        "daemon_git_revision",
        "daemon_tree_sha256",
        "daemon_runtime_sha256",
        "contract_revision",
        "tool_revision",
        "platform",
        "config_digest",
        "dut_identity",
    ]

    for f in required_str_fields:
        val = id_data.get(f)
        if not isinstance(val, str) or not val.strip():
            errors.append(f"identity-{side} 缺少必要欄位或為空: '{f}'")

    evidence_files = id_data.get("evidence_files")
    if not isinstance(evidence_files, list):
        errors.append(f"identity-{side} 的 'evidence_files' 必須為 list")
    else:
        for item in evidence_files:
            if isinstance(item, dict):
                p_str = item.get("path")
                if not p_str or not isinstance(p_str, str):
                    errors.append(f"identity-{side} 的 evidence_files 條目缺少 'path'")
                    continue
                p = Path(p_str)
                if not p.is_file():
                    errors.append(f"identity-{side} 的 evidence 檔案不存在: {p_str}")
                else:
                    exp_sha = item.get("sha256")
                    if exp_sha:
                        act_sha = _compute_sha256(p)
                        if act_sha != exp_sha:
                            errors.append(
                                f"identity-{side} 的 evidence 檔案 {p_str} sha256 ({act_sha}) 與記錄值 ({exp_sha}) 不符"
                            )
            elif isinstance(item, str):
                p = Path(item)
                if not p.is_file():
                    errors.append(f"identity-{side} 的 evidence 檔案不存在: {item}")

    cli_path_str = id_data.get("cli_path")
    if isinstance(cli_path_str, str) and cli_path_str:
        cli_p = Path(cli_path_str)
        if not cli_p.is_file():
            errors.append(f"identity-{side} 的 cli_path 檔案不存在: {cli_path_str}")
        else:
            computed_cli_sha = _compute_sha256(cli_p)
            given_cli_sha = id_data.get("cli_sha256", "")
            if given_cli_sha and computed_cli_sha != given_cli_sha:
                errors.append(
                    f"identity-{side} 的 cli_sha256 ({given_cli_sha}) 與重新計算值 ({computed_cli_sha}) 不符"
                )

    expected_cases_sha = (
        hashlib.sha256("\n".join(sorted(case_ids)).encode("utf-8")).hexdigest()
        if case_ids
        else ""
    )
    given_cases_sha = id_data.get("case_list_sha256")
    if given_cases_sha:
        if given_cases_sha != expected_cases_sha:
            errors.append(
                f"identity-{side} 的 case_list_sha256 ({given_cases_sha}) 與實際 testcase 清單 sha256 ({expected_cases_sha}) 不符"
            )
    else:
        id_data["case_list_sha256"] = expected_cases_sha

    return errors



def compare_ab(
    junit_a_path: Path,
    junit_b_path: Path,
    a_label: str,
    b_label: str,
    id_a_path: Path,
    id_b_path: Path,
    applicability_path: Path,
    out_path: Path,
    required_path: Path | None = None,
) -> int:
    errors: list[str] = []

    # 1. 載入 identity
    try:
        id_a: dict[str, Any] = json.loads(id_a_path.read_text(encoding="utf-8"))
    except Exception as e:
        id_a = {}
        errors.append(f"無法讀取 identity-a ({id_a_path}): {e}")

    try:
        id_b: dict[str, Any] = json.loads(id_b_path.read_text(encoding="utf-8"))
    except Exception as e:
        id_b = {}
        errors.append(f"無法讀取 identity-b ({id_b_path}): {e}")

    # 2. 載入 applicability
    try:
        applicability: dict[str, Any] = json.loads(
            applicability_path.read_text(encoding="utf-8")
        )
    except Exception as e:
        applicability = {}
        errors.append(f"無法讀取 applicability ({applicability_path}): {e}")

    # 3. 解析 junit
    cases_a, errs_a = _parse_junit(junit_a_path)
    cases_b, errs_b = _parse_junit(junit_b_path)
    errors.extend(errs_a)
    errors.extend(errs_b)

    # 4. 驗證 identity
    errors.extend(_validate_identity("a", id_a, list(cases_a.keys())))
    errors.extend(_validate_identity("b", id_b, list(cases_b.keys())))

    # 跨邊斷言
    if id_a.get("cli_implementation") != "python":
        errors.append("identity-a 的 cli_implementation 必須為 'python'")
    if id_b.get("cli_implementation") != "rust":
        errors.append("identity-b 的 cli_implementation 必須為 'rust'")
    if id_a.get("cli_sha256") and id_a.get("cli_sha256") == id_b.get("cli_sha256"):
        errors.append("A 與 B 的 cli_sha256 不得相同")
    if id_a.get("daemon_tree_sha256") != id_b.get("daemon_tree_sha256"):
        errors.append("A 與 B 的 daemon_tree_sha256 必須相同")
    if id_a.get("platform") != id_b.get("platform"):
        errors.append(f"A 與 B 的 platform 必須相同 ({id_a.get('platform')} != {id_b.get('platform')})")

    # 5. 處理 required 清單
    required_ids: set[str] = set()
    if required_path:
        if not required_path.is_file():
            errors.append(f"required 清單檔案不存在或無法讀取: {required_path}")
        else:
            try:
                req_text = required_path.read_text(encoding="utf-8")
                required_ids = {
                    line.strip() for line in req_text.splitlines() if line.strip()
                }
            except Exception as e:
                errors.append(f"無法讀取 required 清單 ({required_path}): {e}")

    # 建立 normalized case 索引以便支援參數化案例與模組級別比對
    case_to_cids_a: dict[str, list[str]] = {}
    for cid, info in cases_a.items():
        c_norm = info.get("case", cid)
        case_to_cids_a.setdefault(c_norm, []).append(cid)

    case_to_cids_b: dict[str, list[str]] = {}
    for cid, info in cases_b.items():
        c_norm = info.get("case", cid)
        case_to_cids_b.setdefault(c_norm, []).append(cid)

    # 6. 逐案分類
    all_case_ids = set(cases_a.keys()) | set(cases_b.keys())
    # 檢查 required_ids 是否有完全不在 A 與 B 出現者
    for req_id in required_ids:
        req_norm = _normalize_case_id(req_id)
        found_a = req_id in cases_a or req_id in case_to_cids_a or req_norm in case_to_cids_a
        found_b = req_id in cases_b or req_id in case_to_cids_b or req_norm in case_to_cids_b
        if not found_a and not found_b:
            all_case_ids.add(req_id)

    sorted_case_ids = sorted(all_case_ids)
    case_rows: list[dict[str, Any]] = []

    for cid in sorted_case_ids:
        in_a = cid in cases_a
        in_b = cid in cases_b

        if not in_a and not in_b:
            cls = "required-missing"
            out_a = None
            out_b = None
            reason = "case missing from both sides"
            case_name = _normalize_case_id(cid)
        elif not in_a or not in_b:
            cls = "case-set-mismatch"
            out_a = cases_a[cid]["outcome"] if in_a else None
            out_b = cases_b[cid]["outcome"] if in_b else None
            reason = f"case present only in {'a' if in_a else 'b'}"
            case_name = cases_a[cid].get("case", "") if in_a else cases_b[cid].get("case", "")
        else:
            out_a = cases_a[cid]["outcome"]
            out_b = cases_b[cid]["outcome"]
            msg_a = cases_a[cid]["message"]
            msg_b = cases_b[cid]["message"]
            reason = msg_b or msg_a or ""
            case_name = cases_a[cid].get("case") or cases_b[cid].get("case") or _normalize_case_id(cid)

            if out_a == "xfail" or out_b == "xfail":
                cls = "xfail-unapproved"
            elif out_a in ("fail", "error"):
                cls = "baseline-fail"
            elif out_a == "pass" and out_b in ("fail", "error"):
                cls = "regression"
            elif out_a == "pass" and out_b == "pass":
                cls = "ok"
                reason = ""
            elif out_a == "pass" and out_b == "skip":
                cls = "evidence-insufficient"
            elif out_a == "skip" and msg_a == "rust-only contract":
                if out_b == "pass":
                    cls = "rust-only-ok"
                    reason = ""
                elif out_b in ("fail", "error"):
                    cls = "regression"
                else:
                    cls = "evidence-insufficient"
            elif out_a == "skip" and out_b == "skip":
                if msg_a == msg_b and msg_a in applicability:
                    app = applicability[msg_a]
                    target = app.get("applies_to", "")
                    plat = id_a.get("platform") or id_b.get("platform") or ""
                    matches = False
                    if target == "windows" and plat in ("windows", "win32"):
                        matches = True
                    elif target in ("posix", "linux") and plat in (
                        "linux",
                        "posix",
                        "darwin",
                    ):
                        matches = True
                    elif target == plat:
                        matches = True

                    if matches:
                        cls = "not-applicable"
                    else:
                        cls = "evidence-insufficient"
                else:
                    cls = "evidence-insufficient"
            else:
                cls = "evidence-insufficient"

        case_rows.append(
            {
                "id": cid,
                "case": case_name,
                "class": cls,
                "a": out_a,
                "b": out_b,
                "reason": reason,
            }
        )

    # 7. 統計與整體結果
    counts: dict[str, int] = {}
    for r in case_rows:
        counts[r["class"]] = counts.get(r["class"], 0) + 1

    # 每 case 聚合判定
    per_case: dict[str, str] = {}
    for r in case_rows:
        c_name = r.get("case", r["id"])
        c_cls = r["class"]
        prev = per_case.get(c_name)
        if prev is None:
            per_case[c_name] = c_cls
        elif prev != c_cls:
            if c_cls in ("regression", "baseline-fail", "required-missing", "case-set-mismatch", "xfail-unapproved", "evidence-insufficient"):
                per_case[c_name] = c_cls

    has_failures = any(
        r["class"] not in ("ok", "rust-only-ok", "not-applicable") for r in case_rows
    )
    result = "pass" if (not errors and not has_failures and bool(case_rows)) else "fail"

    envelope = {
        "a_label": a_label,
        "b_label": b_label,
        "identity": {
            "a": id_a,
            "b": id_b,
        },
        "counts": counts,
        "per_case": per_case,
        "cases": case_rows,
        "result": result,
        "errors": errors,
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(envelope, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    return 0 if result == "pass" else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="A/B 比較器")
    parser.add_argument("--a", required=True, type=Path, help="A 輪 JUnit XML 路徑")
    parser.add_argument("--b", required=True, type=Path, help="B 輪 JUnit XML 路徑")
    parser.add_argument("--a-label", required=True, help="A 輪標籤")
    parser.add_argument("--b-label", required=True, help="B 輪標籤")
    parser.add_argument("--out", required=True, type=Path, help="輸出報告 JSON 路徑")
    parser.add_argument("--identity-a", required=True, type=Path, help="A 輪身分 JSON")
    parser.add_argument("--identity-b", required=True, type=Path, help="B 輪身分 JSON")
    parser.add_argument(
        "--applicability", required=True, type=Path, help="適用性設定 JSON"
    )
    parser.add_argument(
        "--required", type=Path, default=None, help="必要案例清單檔案"
    )

    args = parser.parse_args(argv)
    return compare_ab(
        junit_a_path=args.a,
        junit_b_path=args.b,
        a_label=args.a_label,
        b_label=args.b_label,
        id_a_path=args.identity_a,
        id_b_path=args.identity_b,
        applicability_path=args.applicability,
        out_path=args.out,
        required_path=args.required,
    )


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
