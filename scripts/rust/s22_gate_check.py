#!/usr/bin/env python3
"""Slice 2.2 出口判定（07 Task 13，R5-F3）：逐集合 L1 完整性＋逐案 AUTOMATED 證據。

用法：
  python3 scripts/rust/s22_gate_check.py --l1-results <dir> --map tests/acceptance/slice22_evidence_map.yaml \
      [--lock tests/acceptance/l1_required.lock.json] --out <json>

`<dir>/<section>/<platform>/resource-report.json`（`l1-results.v1`）；`<section>` 為 `s22_gate:<section>` 的後段。
- 每個 `s22_gate:*` 集合／平台以 P2 `tests/acceptance/l1_judge.judge`（依路徑載入，本檔不另寫判定規則）判定；
  報告不存在 → INSUFFICIENT。
- 對照表每個 `AUTOMATED` 條目的每個 `.py`／`.rs` 測試須在任一平台的報告中恰一列 `passed`（`.rs` 轉為
  `cargo:<crate>::<檔名 stem>::<函式>`）；有 failed 列 → FAIL；缺列、重複或非 passed → INSUFFICIENT。
  其他來源（func-test、scenario）由 L1.5 報告判定，本檔不計（列在 `unchecked_tests`）。
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
import sys
from typing import Any

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
L1_JUDGE = ROOT / "tests" / "acceptance" / "l1_judge.py"
DEFAULT_LOCK = ROOT / "tests" / "acceptance" / "l1_required.lock.json"
SET_PREFIX = "s22_gate:"
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
    for set_id in sorted(k for k in lock.get("sets", {}) if k.startswith(SET_PREFIX)):
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


def judge_cases(emap: dict[str, Any], rows_by_nodeid: dict[str, list[dict]]) -> list[dict]:
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
        missing: list[str] = []
        failed: list[str] = []
        unchecked: list[str] = []
        for ref in tests:
            nid = to_nodeid(ref)
            if nid is None:
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
            case_status = "INSUFFICIENT"
        elif failed:
            case_status = "FAIL"
        elif missing:
            case_status = "INSUFFICIENT"
        else:
            case_status = "PASS"
        row = {"case_id": cid, "status": case_status, "missing_tests": missing}
        if failed:
            row["failed_tests"] = failed
        if unchecked:
            row["unchecked_tests"] = unchecked
        out.append(row)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--l1-results", required=True, type=pathlib.Path)
    ap.add_argument("--map", required=True, type=pathlib.Path)
    ap.add_argument("--lock", type=pathlib.Path, default=DEFAULT_LOCK)
    ap.add_argument("--out", required=True, type=pathlib.Path)
    args = ap.parse_args(argv)

    lock = json.loads(args.lock.read_text(encoding="utf-8"))
    emap = yaml.safe_load(args.map.read_text(encoding="utf-8")) or {}
    sections, rows = judge_sections(args.l1_results, lock, _load_judge())
    cases = judge_cases(emap, rows)
    case_verdicts = [c["status"] for c in cases if c["status"] in _RANK]
    result = _worst([s["verdict"] for s in sections] + case_verdicts)
    if result == "INVALID":
        result = "FAIL"
    payload = {"sections": sections, "cases": cases, "result": result}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"[s22_gate_check] result={result} → {args.out}")
    return 0 if result == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
