#!/usr/bin/env python3
"""Slice 2.2 出口判定（07 Task 13，R5-F3）：逐集合 L1 完整性＋逐案 AUTOMATED 證據。

用法：
  python3 scripts/rust/s22_gate_check.py --l1-results <dir> --map tests/acceptance/slice22_evidence_map.yaml \
      [--lock tests/acceptance/l1_required.lock.json] [--cases-lock tests/acceptance/cases.lock.json] \
      [--l15-results <json>] [--parity-doc docs/rust-refactor/plans/parity-amendments-2026-10-26.md] \
      [--prereqs-script scripts/rust/check_p6_prereqs.py] --out <json>

`<dir>/<section>/<platform>/resource-report.json`（`l1-results.v1`）；`<section>` 為 `s22_gate:<section>` 的後段。
- 每個 `s22_gate:*` 集合／平台以 P2 `tests/acceptance/l1_judge.judge`（依路徑載入，本檔不另寫判定規則）判定；
  報告不存在 → INSUFFICIENT。
- 對照表每個 `AUTOMATED` 條目的每個 `.py`／`.rs` 測試須在任一平台的報告中恰一列 `passed`（`.rs` 轉為
  `cargo:<crate>::<檔名 stem>::<函式>`）；有 failed 列 → FAIL；缺列、重複或非 passed → INSUFFICIENT。
  其他來源（func-test、scenario）須在 `--l15-results`（`s22-l15-results.v1`：`{"format","runs":[{"ref","outcome",
  "exit_code"}]}`）中恰一列 `outcome == "passed"` 且 `exit_code == 0`；未提供或不符 → INSUFFICIENT（審查
  3317987_d6e3b3c-opus High #1：函式／腳本存在不等於已執行且通過）。
- 必要集合（`REQUIRED_SECTIONS`，以計畫 07 Task 13 為準）由本檔固定：lock 缺任一集合／平台 → INSUFFICIENT；
  對照表 `cases` 的鍵集合須等於 `cases.lock.json` 凍結的 `slice_2.2`，否則 INSUFFICIENT（同報告 Medium #2）。
- 增補 #180：以全表模式執行 `check_p6_prereqs.py --strict`，exit 非 0 → INSUFFICIENT。
- 增補 #187：`parity-amendments-2026-10-26.md` 中承接 Phase 為 P6 的承接編號逐列輸出於 `parity_rows`；狀態取對照表
  `parity_rows["#N"].status`（`PASS` 須附 `tests` 並同 AUTOMATED 規則判定；`BLOCKED-OWNER` 須附 `procedure`；
  `OWNER-DECISION-PENDING` 或未登記 → INSUFFICIENT，result 不得為 PASS）。
- `BLOCKED-OWNER` 條目回報為 BLOCKED-OWNER（不得標 PASS，也不改變 `result`）；`PENDING` → INSUFFICIENT；
  `NOT_APPLICABLE` 略過。
輸出 `{"sections":[{set_id,platform,verdict,reasons}],"cases":[{case_id,status,missing_tests,...}],"result"}`；
exit 0 只在 `result == "PASS"`。只用 stdlib＋PyYAML，不得匯入 sw_core。
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import pathlib
import re
import subprocess
import sys
from typing import Any

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
L1_JUDGE = ROOT / "tests" / "acceptance" / "l1_judge.py"
DEFAULT_LOCK = ROOT / "tests" / "acceptance" / "l1_required.lock.json"
DEFAULT_CASES_LOCK = ROOT / "tests" / "acceptance" / "cases.lock.json"
DEFAULT_PARITY_DOC = ROOT / "docs" / "rust-refactor" / "plans" / "parity-amendments-2026-10-26.md"
DEFAULT_PREREQS = ROOT / "scripts" / "rust" / "check_p6_prereqs.py"
SET_PREFIX = "s22_gate:"
# 07 Task 13（R5-F3、增補 #187）：出口必要集合與平台；不得由 lock 內容推導（lock 少登記時會無聲縮小分母）。
REQUIRED_SECTIONS: dict[str, tuple[str, ...]] = {
    "s22_gate:cargo_workspace": ("linux",),
    "s22_gate:console_conformance_pty": ("linux",),
    "s22_gate:blackbox_rust": ("linux",),
    "s22_gate:win_test": ("windows",),
    "s22_gate:py_parity": ("linux",),
}
L15_FORMAT = "s22-l15-results.v1"
PARITY_STATUSES = ("PASS", "BLOCKED-OWNER", "OWNER-DECISION-PENDING")
_RANK = {"PASS": 0, "INSUFFICIENT": 1, "FAIL": 2, "INVALID": 3}


def _load_judge():
    spec = importlib.util.spec_from_file_location("l1_judge", L1_JUDGE)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _worst(verdicts: list[str]) -> str:
    return max(verdicts, key=lambda v: _RANK.get(v, 3)) if verdicts else "PASS"


def to_nodeid(ref: str) -> str | None:
    """對照表的 `<檔案>::<函式>` → L1 nodeid；非 `.py`／`.rs` 回 None。"""
    path, _, name = ref.partition("::")
    if path.endswith(".py"):
        return ref
    if path.endswith(".rs"):
        parts = pathlib.PurePosixPath(path).parts
        if len(parts) >= 2 and parts[0] == "crates":
            return f"cargo:{parts[1]}::{pathlib.PurePosixPath(path).stem}::{name}"
    return None


def judge_sections(results_dir: pathlib.Path, lock: dict[str, Any], judge) -> tuple[list[dict], dict[str, list[dict]]]:
    sections: list[dict] = []
    rows_by_nodeid: dict[str, list[dict]] = {}
    sets = lock.get("sets", {})
    for set_id, platforms in sorted(REQUIRED_SECTIONS.items()):
        for platform in platforms:
            if platform not in (sets.get(set_id) or {}):
                sections.append({"set_id": set_id, "platform": platform, "verdict": "INSUFFICIENT",
                                 "reasons": ["必要集合未登記於 L1 鎖（REQUIRED_SECTIONS）"]})
    for set_id in sorted(k for k in sets if k.startswith(SET_PREFIX)):
        for platform, spec in sorted(lock["sets"][set_id].items()):
            section = set_id[len(SET_PREFIX):]
            path = results_dir / section / platform / "resource-report.json"
            if not path.is_file():
                sections.append({"set_id": set_id, "platform": platform, "verdict": "INSUFFICIENT",
                                 "reasons": [f"report missing：{path}"]})
                continue
            try:
                report = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                sections.append({"set_id": set_id, "platform": platform, "verdict": "INVALID",
                                 "reasons": [f"report unreadable：{exc}"]})
                continue
            verdict = judge.judge(report, list(spec.get("nodeids") or []), platform=platform,
                                  selector=spec.get("selector"))
            sections.append({"set_id": set_id, "platform": platform, "verdict": verdict["verdict"],
                             "reasons": list(verdict.get("reasons") or [])})
            for row in report.get("results") or []:
                if isinstance(row, dict):
                    rows_by_nodeid.setdefault(str(row.get("nodeid")), []).append({**row, "_platform": platform})
    return sections, rows_by_nodeid


def _l15_passed(l15: dict[str, list[dict]] | None, ref: str) -> bool:
    runs = (l15 or {}).get(ref, [])
    return len(runs) == 1 and runs[0].get("outcome") == "passed" and runs[0].get("exit_code") == 0


def judge_tests(tests: list[str], rows_by_nodeid: dict[str, list[dict]],
                l15: dict[str, list[dict]] | None) -> tuple[str, dict[str, list[str]]]:
    """一組測試參照 → (PASS|FAIL|INSUFFICIENT, {missing, failed, unchecked})。"""
    missing: list[str] = []
    failed: list[str] = []
    unchecked: list[str] = []
    for ref in tests:
        nid = to_nodeid(ref)
        if nid is None:
            runs = (l15 or {}).get(ref, [])
            if any(r.get("outcome") in ("failed", "error") for r in runs):
                failed.append(ref)
            elif not _l15_passed(l15, ref):
                unchecked.append(ref)
            continue
        rows = rows_by_nodeid.get(nid, [])
        if any(r.get("outcome") in ("failed", "error") for r in rows):
            failed.append(ref)
            continue
        per_platform: dict[str, list[dict]] = {}
        for r in rows:
            per_platform.setdefault(r["_platform"], []).append(r)
        if not any(len(rs) == 1 and rs[0].get("outcome") == "passed" for rs in per_platform.values()):
            missing.append(ref)
    if not tests:
        status = "INSUFFICIENT"
    elif failed:
        status = "FAIL"
    elif missing or unchecked:
        status = "INSUFFICIENT"
    else:
        status = "PASS"
    return status, {"missing": missing, "failed": failed, "unchecked": unchecked}


def judge_cases(emap: dict[str, Any], rows_by_nodeid: dict[str, list[dict]],
                l15: dict[str, list[dict]] | None = None) -> list[dict]:
    out: list[dict] = []
    for cid, entry in sorted((emap.get("cases") or {}).items()):
        status = (entry or {}).get("status")
        if status == "NOT_APPLICABLE":
            out.append({"case_id": cid, "status": "NOT_APPLICABLE", "missing_tests": []})
            continue
        if status == "BLOCKED-OWNER":
            out.append({"case_id": cid, "status": "BLOCKED-OWNER", "missing_tests": [],
                        "blocked_item": entry.get("blocked_item")})
            continue
        if status != "AUTOMATED":
            out.append({"case_id": cid, "status": "INSUFFICIENT", "missing_tests": [],
                        "reasons": [f"status {status!r} 不是 AUTOMATED／BLOCKED-OWNER／NOT_APPLICABLE"]})
            continue
        tests = list(entry.get("tests") or [])
        case_status, detail = judge_tests(tests, rows_by_nodeid, l15)
        missing, failed, unchecked = detail["missing"], detail["failed"], detail["unchecked"]
        row = {"case_id": cid, "status": case_status, "missing_tests": missing}
        if failed:
            row["failed_tests"] = failed
        if unchecked:
            row["unchecked_tests"] = unchecked
        out.append(row)
    return out


def load_l15(path: pathlib.Path | None) -> tuple[dict[str, list[dict]] | None, list[str]]:
    """`--l15-results` → {ref: [run…]}；未提供回 None（所有非 nodeid 參照皆 INSUFFICIENT）。"""
    if path is None:
        return None, []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {}, [f"l15 results unreadable：{exc}"]
    if not isinstance(data, dict) or data.get("format") != L15_FORMAT:
        return {}, [f"l15 results format 不是 {L15_FORMAT}"]
    out: dict[str, list[dict]] = {}
    for run in data.get("runs") or []:
        if isinstance(run, dict) and isinstance(run.get("ref"), str):
            out.setdefault(run["ref"], []).append(run)
    return out, []


def map_coverage(emap: dict[str, Any], cases_lock: dict[str, Any]) -> dict[str, Any]:
    """對照表鍵集合須等於凍結 `slice_2.2`（不得縮小或擴張分母）。"""
    frozen = set((cases_lock.get("sets") or {}).get("slice_2.2") or [])
    keys = set((emap.get("cases") or {}).keys())
    verdict = "PASS" if frozen and keys == frozen else "INSUFFICIENT"
    return {"verdict": verdict, "missing_cases": sorted(frozen - keys), "extra_cases": sorted(keys - frozen),
            "frozen_count": len(frozen)}


def p6_parity_numbers(text: str) -> list[str]:
    """`parity-amendments-2026-10-26.md` 中承接 Phase 為 P6 的承接編號（§1 第 5 欄、§2 第 3 欄、§3 P6 小節）。"""
    nums: set[int] = set()
    section = None
    for line in text.splitlines():
        if line.startswith("## §1"):
            section = "s1"
        elif line.startswith("## §2"):
            section = "s2"
        elif line.startswith("### `feature/rust-refactor-p6`"):
            section = "s3p6"
        elif line.startswith("## ") or line.startswith("### "):
            section = None if not line.startswith("### §2.") else section
        if not line.startswith("|") or line.startswith("|---"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if section == "s1" and len(cells) >= 5 and cells[0].isdigit() and "P6" in cells[4]:
            nums.add(int(cells[0]))
        elif section == "s2" and len(cells) >= 4 and "P6" in cells[2]:
            nums.update(int(n) for n in re.findall(r"#(\d+)", cells[3]))
        elif section == "s3p6" and cells and cells[0].isdigit():
            nums.add(int(cells[0]))
    return [f"#{n}" for n in sorted(nums)]


def judge_parity(numbers: list[str], emap: dict[str, Any], rows_by_nodeid: dict[str, list[dict]],
                 l15: dict[str, list[dict]] | None) -> list[dict]:
    rows = emap.get("parity_rows") or {}
    out: list[dict] = []
    for num in numbers:
        entry = rows.get(num) or {}
        status = entry.get("status")
        row: dict[str, Any] = {"row": num, "status": status or "OWNER-DECISION-PENDING"}
        if status == "PASS":
            verdict, detail = judge_tests(list(entry.get("tests") or []), rows_by_nodeid, l15)
            row.update({"verdict": verdict, "missing_tests": detail["missing"] + detail["unchecked"]})
            if detail["failed"]:
                row["failed_tests"] = detail["failed"]
        elif status == "BLOCKED-OWNER":
            proc = entry.get("procedure")
            row["verdict"] = "BLOCKED-OWNER" if proc and (ROOT / str(proc)).is_file() else "INSUFFICIENT"
        else:
            row["verdict"] = "INSUFFICIENT"
            if status not in PARITY_STATUSES:
                row["reasons"] = [f"status {status!r} 未登記或不在 {PARITY_STATUSES}"]
        out.append(row)
    return out


def run_prereqs(script: pathlib.Path) -> dict[str, Any]:
    """增補 #180：全表模式（`--strict`），不得以 `--max-task` 局部檢查充當出口。"""
    try:
        proc = subprocess.run([sys.executable, str(script), "--strict"], capture_output=True, text=True,
                              timeout=120, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"verdict": "INSUFFICIENT", "exit_code": None, "reasons": [str(exc)]}
    tail = (proc.stdout or "").strip().splitlines()[-1:] or [""]
    return {"verdict": "PASS" if proc.returncode == 0 else "INSUFFICIENT", "exit_code": proc.returncode,
            "reasons": tail}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--l1-results", required=True, type=pathlib.Path)
    ap.add_argument("--map", required=True, type=pathlib.Path)
    ap.add_argument("--lock", type=pathlib.Path, default=DEFAULT_LOCK)
    ap.add_argument("--cases-lock", type=pathlib.Path, default=DEFAULT_CASES_LOCK)
    ap.add_argument("--l15-results", type=pathlib.Path, default=None)
    ap.add_argument("--parity-doc", type=pathlib.Path, default=DEFAULT_PARITY_DOC)
    ap.add_argument("--prereqs-script", type=pathlib.Path, default=DEFAULT_PREREQS)
    ap.add_argument("--out", required=True, type=pathlib.Path)
    args = ap.parse_args(argv)

    lock = json.loads(args.lock.read_text(encoding="utf-8"))
    emap = yaml.safe_load(args.map.read_text(encoding="utf-8")) or {}
    cases_lock = json.loads(args.cases_lock.read_text(encoding="utf-8"))
    l15, l15_reasons = load_l15(args.l15_results)
    sections, rows = judge_sections(args.l1_results, lock, _load_judge())
    cases = judge_cases(emap, rows, l15)
    coverage = map_coverage(emap, cases_lock)
    parity = judge_parity(p6_parity_numbers(args.parity_doc.read_text(encoding="utf-8")), emap, rows, l15)
    prereqs = run_prereqs(args.prereqs_script)
    case_verdicts = [c["status"] for c in cases if c["status"] in _RANK]
    parity_verdicts = [r["verdict"] for r in parity if r["verdict"] in _RANK]
    extra = [coverage["verdict"], prereqs["verdict"]] + (["INSUFFICIENT"] if l15_reasons else [])
    result = _worst([s["verdict"] for s in sections] + case_verdicts + parity_verdicts + extra)
    if result == "INVALID":
        result = "FAIL"
    payload = {"sections": sections, "cases": cases, "map_coverage": coverage, "parity_rows": parity,
               "prereqs": prereqs, "l15_reasons": l15_reasons, "result": result}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"[s22_gate_check] result={result} → {args.out}")
    return 0 if result == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
