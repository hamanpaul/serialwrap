#!/usr/bin/env python3
"""Slice 2.3（P7）出口 gate 的 `cases.lock slice_2.3 coverage` 列（08-slice23-login-events.md Task 19 Step 3）。

逐一核對 `tests/acceptance/cases.lock.json` 的 `slice_2.3` 集合：每個 `case_id` 在本次 gate run 的結果中須為 PASS，
或為 BLOCKED-OWNER 且附 B 編號；其餘（FAIL、未執行、BLOCKED-UPSTREAM、證據不足、找不到承擔來源）一律記為缺漏，
任何缺漏 → 結束碼 1（FAIL）。

案例 → 結果來源（依 `cases.yaml` 的 suite／source_ref／family；只讀 run 目錄內的證據，不讀「最新報告」）：
- 必要節點一律取自 L1 鎖（`--l1-lock`）**預先凍結**的 `p7_gate:*` 集合，不採信當輪 junit 出現哪些節點；每個必要
  節點須在 junit 恰出現一次（缺漏、重複皆為缺漏；審查 P7-cd7acf0-1）。
- `contract-blackbox`：凍結 `p7_gate:contract-blackbox-p7-b` 中 nodeid 為 `source_ref` 或 `source_ref[參數]` 的節點須在
  `<ab>/p7-b/junit.xml` 各恰一次 passed；A 輪取 `-p7-a` 的同名節點各恰一次 passed（rust_only SC 案例：A 集合不含、
  A 輪 junit 中 B 的節點須各恰一次 skip 或 passed）。
- `l15-scenario`／`func-test`：daemon_slice 2.1 的 l15 情境取 `<ab>/l15-slice21-b/junit.xml` 的
  `test_case[<id>-rust-…]`；2.3 的取 `<ab>/l15-slice23/junit.xml` 的 `test_*_rust[<id>]`；func-test 的
  brcm-01-login-flow／ev-01-spawn-handler 另認 `<ab>/func-<id>/` 的 comparison.json。2.2 案例（P6 human console）
  與找不到節點者為缺漏。
- `windows-native` → BLOCKED-OWNER B-06；`realhw` → B-07；`regression` 依 family：F9 → B-02、F10 → B-04、F11 → B-05；
  其他 family（先前切片 L2 回歸）在 08 BLOCKED-OWNER 總表無 B 編號 → 缺漏（SPEC-GAP，交編排者編號）。

用法：python3 scripts/rust/p7_case_coverage.py --ab-dir <run>/ab --out <run>/coverage.json
只用 stdlib＋PyYAML；不 import sw_core。
"""
from __future__ import annotations

import argparse
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
OWNER_BY_SUITE = {"windows-native": "B-06", "realhw": "B-07"}
OWNER_BY_REGRESSION_FAMILY = {"F9": "B-02", "F10": "B-04", "F11": "B-05"}


def _junit(path: Path) -> dict[str, list[str]] | None:
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError):
        return None
    out: dict[str, list[str]] = {}
    for tc in root.iter("testcase"):
        nodeid = (tc.get("classname") or "").replace(".", "/") + ".py::" + (tc.get("name") or "")
        outcome = "passed"
        for child in tc:
            if child.tag in ("failure", "error"):
                outcome = "failed"
            elif child.tag == "skipped":
                outcome = "skipped"
        out.setdefault(nodeid, []).append(outcome)
    return out


def _match(frozen: list[str], ref: str) -> list[str]:
    return [n for n in frozen if n == ref or n.startswith(ref + "[")]


def _once(res: dict[str, list[str]] | None, nodes: list[str], want: str | tuple[str, ...]) -> str | None:
    """每個必要節點在 junit 恰一次且結果屬於 `want`；回傳失敗原因或 None。"""
    want = (want,) if isinstance(want, str) else want
    if not nodes:
        return "凍結集合無節點"
    if res is None:
        return "junit 缺檔或不可解析"
    for n in nodes:
        got = res.get(n) or []
        if len(got) != 1:
            return f"{'缺' if not got else '重複'}節點 {n.split('::', 1)[-1]}"
        if got[0] not in want:
            return f"{n.split('::', 1)[-1]} 為 {got[0]}"
    return None


def _frozen(lock: dict, set_id: str) -> list[str]:
    try:
        return list(lock["sets"][set_id]["linux"]["nodeids"])
    except (KeyError, TypeError):
        return []


def _rust_only(bc_yaml: Path) -> set[str]:
    data = yaml.safe_load(bc_yaml.read_text(encoding="utf-8"))
    return {c["id"] for c in data["changes"] if c.get("scope") == "rust_only"}


def judge_case(case: dict, ab: Path, rust_only: set[str], cache: dict, lock: dict | None = None) -> tuple[str, str]:
    lock = lock or {}
    def junit(slug: str):
        if slug not in cache:
            cache[slug] = _junit(ab / slug / "junit.xml")
        return cache[slug]

    suite, cid = case.get("suite"), case["case_id"]
    if suite in OWNER_BY_SUITE:
        return "BLOCKED-OWNER", OWNER_BY_SUITE[suite]
    if suite == "regression":
        bid = OWNER_BY_REGRESSION_FAMILY.get(str(case.get("family")))
        return ("BLOCKED-OWNER", bid) if bid else ("GAP", f"regression family {case.get('family')} 無 B 編號")
    if suite == "contract-blackbox":
        ref = str(case.get("source_ref") or "")
        b_nodes = _match(_frozen(lock, "p7_gate:contract-blackbox-p7-b"), ref)
        a_nodes = _match(_frozen(lock, "p7_gate:contract-blackbox-p7-a"), ref)
        why = _once(junit("p7-b"), b_nodes, "passed")
        if why:
            return "GAP", f"B 輪：{why}"
        if set(case.get("behavior_changes") or []) & rust_only and not a_nodes:
            why = _once(junit("p7-a"), b_nodes, ("skipped", "passed"))
        elif sorted(a_nodes) != sorted(b_nodes):
            why = "凍結 A／B 集合節點不一致"
        else:
            why = _once(junit("p7-a"), a_nodes, "passed")
        return ("GAP", f"A 輪：{why}") if why else ("PASS", f"B {len(b_nodes)}／A {len(a_nodes) or len(b_nodes)} 節點")
    if suite in ("l15-scenario", "func-test"):
        slice_ = str(case.get("daemon_slice"))
        if suite == "func-test" and (ab / f"func-{cid}").is_dir():
            found = next(iter(sorted((ab / f"func-{cid}" / "report").rglob("comparison.json"))), None)
            try:
                rc = int((ab / f"func-{cid}" / "rc").read_text(encoding="utf-8").strip())
                verdict = json.loads(found.read_text(encoding="utf-8")).get("verdict") if found else None
            except (OSError, ValueError):
                rc, verdict = None, None
            return ("PASS", "func-test comparison PASS") if (verdict == "PASS" and rc == 0) else (
                "GAP", f"func-test verdict {verdict} rc={rc}")
        if slice_ == "2.1":
            slug, nodes = "l15-slice21-b", [n for n in _frozen(lock, "p7_gate:l15-slice21-b")
                                             if n.split("::", 1)[-1].startswith("test_case[")
                                             and (f"-{cid}]" in n or f"[{cid}-" in n) and "rust" in n]
        elif slice_ == "2.3":
            slug, nodes = "l15-slice23", [n for n in _frozen(lock, "p7_gate:l15-slice23-b") if n.endswith(f"_rust[{cid}]")]
        else:
            return "GAP", f"daemon_slice {slice_}（P6 Slice 2.2，BLOCKED-UPSTREAM）"
        why = _once(junit(slug), nodes, "passed")
        return ("GAP", f"l15：{why}") if why else ("PASS", f"l15 {len(nodes)} 節點")
    return "GAP", f"未知 suite {suite}"


def build(ab: Path, cases_yaml: Path, cases_lock: Path, bc_yaml: Path, l1_lock: Path | None = None) -> dict:
    lock = json.loads(cases_lock.read_text(encoding="utf-8"))
    l1 = json.loads(Path(l1_lock or ROOT / "tests/acceptance/l1_required.lock.json").read_text(encoding="utf-8"))
    cases = {c["case_id"]: c for c in yaml.safe_load(cases_yaml.read_text(encoding="utf-8"))["cases"]}
    rust_only = _rust_only(bc_yaml)
    cache: dict = {}
    rows = []
    for cid in sorted(lock["sets"]["slice_2.3"]):
        case = cases.get(cid)
        status, detail = ("GAP", "cases.yaml 無此案例") if case is None else judge_case(case, ab, rust_only, cache, l1)
        rows.append({"case_id": cid, "status": status, "detail": detail})
    gaps = [r for r in rows if r["status"] not in ("PASS", "BLOCKED-OWNER")]
    return {"set": "slice_2.3", "total": len(rows), "pass": sum(r["status"] == "PASS" for r in rows),
            "blocked_owner": sum(r["status"] == "BLOCKED-OWNER" for r in rows), "gaps": len(gaps), "rows": rows}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--ab-dir", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--cases", type=Path, default=ROOT / "tests/acceptance/cases.yaml")
    ap.add_argument("--cases-lock", type=Path, default=ROOT / "tests/acceptance/cases.lock.json")
    ap.add_argument("--behavior-changes", type=Path, default=ROOT / "contracts/behavior_changes.yaml")
    ap.add_argument("--l1-lock", type=Path, default=ROOT / "tests/acceptance/l1_required.lock.json")
    args = ap.parse_args(argv)
    report = build(args.ab_dir, args.cases, args.cases_lock, args.behavior_changes, args.l1_lock)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"slice_2.3 coverage：{report['pass']} PASS、{report['blocked_owner']} BLOCKED-OWNER、{report['gaps']} 缺漏"
          f"（共 {report['total']}）")
    for r in report["rows"]:
        if r["status"] not in ("PASS", "BLOCKED-OWNER"):
            print(f"  GAP {r['case_id']}：{r['detail']}")
    return 1 if report["gaps"] else 0


if __name__ == "__main__":
    sys.exit(main())
