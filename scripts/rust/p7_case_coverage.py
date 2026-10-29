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
- source_ref 為 `crates/<pkg>/tests/<target>.rs::<fn>` 的案例（任何 suite，windows-native 除外）：取 gate 的 cargo L1 報告
  （`--cargo-l1`，`l1-results.v1`）中 nodeid `cargo:<pkg>::<target>::<fn>` 恰一列 passed。
- `windows-native` → BLOCKED-OWNER B-06；`realhw` → B-07；`regression` 依 family：F9 → B-02、F10 → B-04、F11 → B-05；
  先前切片 L2 回歸 F1～F6、F8、F12 依 08 BLOCKED-OWNER 總表 B-09～B-16（rulings/p7.md 2026-10-29 SPEC-GAP #4 (i)）；
  總表沒有 B 編號的 family → 缺漏。
- 先前切片（daemon_slice 2.1／2.2）的 contract-blackbox／func-test／l15-scenario 案例不在本 gate 重跑（同裁決 (ii)），
  一律以對應切片 gate 的逐案證據判定：2.1 取 slice21 gate 的 run 目錄（`--slice21-run`：`report.json`＋
  `case_verdicts.json`，`{case_id: PASS|FAIL|EVIDENCE_MISSING}`），2.2 取 s22 gate（`scripts/rust/s22_gate_check.py`）
  的輸出 JSON（`--slice22-gate`，`cases[].status`）；該案在匯入證據中須為 PASS，缺檔、缺列或非 PASS 皆為缺漏（不得記
  PASS）。本 gate 若另有同案的證據（凍結的 l15-slice21-b 節點、cargo L1 報告中出現的同名 nodeid），也必須通過（取最差）。
  匯入證據須綁定本 gate（審查 P7-38c150c-3）：來源 commit（slice21 `report.json` 的 `git_revision`、s22 輸出的
  `identity.git_revision`）等於本 gate `results.json`（`--p7-results`）的 `git_head`，且受測 daemon sha256（slice21
  `identity.daemon_sha256`、s22 `identity.daemon_sha256`）等於本 gate 快照的 serialwrapd sha256；任一不符、缺身分欄位或
  缺 `--p7-results` → 該切片的匯入證據不採用（案例全為缺漏）。採用與否及來源記於輸出的 `prior_sources`。

用法：python3 scripts/rust/p7_case_coverage.py --ab-dir <run>/ab --out <run>/coverage.json
      [--cargo-l1 <l1-results.json>] [--p7-results <run>/results.json] [--slice21-run <slice21 gate run 目錄>]
      [--slice22-gate <s22-gate.json>]
只用 stdlib＋PyYAML；不 import sw_core。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
OWNER_BY_SUITE = {"windows-native": "B-06", "realhw": "B-07"}
OWNER_BY_REGRESSION_FAMILY = {"F9": "B-02", "F10": "B-04", "F11": "B-05",
                              # 先前切片 L2 回歸（08 BLOCKED-OWNER 總表 B-09～B-16；rulings/p7.md SPEC-GAP #4 (i)）
                              "F1": "B-09", "F2": "B-10", "F3": "B-11", "F4": "B-12", "F5": "B-13", "F6": "B-14",
                              "F8": "B-15", "F12": "B-16"}
PRIOR_SLICES = ("2.1", "2.2")
PRIOR_SUITES = ("contract-blackbox", "func-test", "l15-scenario")


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


def _cargo_nodeid(ref: str) -> str | None:
    """`crates/sw-core/tests/event_store.rs::fn` → `cargo:sw-core::event_store::fn`；非 crate 測試 → None。"""
    m = re.match(r"^crates/([^/]+)/tests/([^/]+)\.rs::([A-Za-z0-9_]+)$", ref)
    return f"cargo:{m.group(1)}::{m.group(2)}::{m.group(3)}" if m else None


def _l15_slice21_nodes(lock: dict, cid: str) -> list[str]:
    return [n for n in _frozen(lock, "p7_gate:l15-slice21-b")
            if n.split("::", 1)[-1].startswith("test_case[") and (f"-{cid}]" in n or f"[{cid}-" in n) and "rust" in n]


PRIOR_SOURCE = {"2.1": "slice21 gate case_verdicts", "2.2": "s22 gate cases"}


def _judge_prior(case: dict, slice_: str, junit, lock: dict, cargo: dict[str, list[str]] | None,
                 prior: dict[str, dict[str, str] | str | None]) -> tuple[str, str]:
    """先前切片案例：匯入對應切片 gate 的逐案證據須為 PASS；本 gate 另有同案證據時也須通過（取最差）。"""
    cid, src = case["case_id"], PRIOR_SOURCE[slice_]
    verdicts = prior.get(slice_)
    if not isinstance(verdicts, dict):
        why = verdicts if isinstance(verdicts, str) else "未提供或無法解析"
        return "GAP", f"{src} 不採用：{why}（先前切片逐案證據缺，EVIDENCE_MISSING）"
    got = verdicts.get(cid)
    if got != "PASS":
        return "GAP", f"{src}：{got or '缺列'}"
    nid = _cargo_nodeid(str(case.get("source_ref") or ""))
    if nid is not None and cargo is not None and nid in cargo:
        why = _once(cargo, [nid], "passed")
        if why:
            return "GAP", f"{src} PASS，但本 gate cargo：{why}"
    if case.get("suite") == "l15-scenario" and slice_ == "2.1":
        nodes = _l15_slice21_nodes(lock, cid)
        why = _once(junit("l15-slice21-b"), nodes, "passed") if nodes else None
        if why:
            return "GAP", f"{src} PASS，但本 gate l15：{why}"
    return "PASS", f"{src} PASS"


def judge_case(case: dict, ab: Path, rust_only: set[str], cache: dict, lock: dict | None = None,
               cargo: dict[str, list[str]] | None = None,
               prior: dict[str, dict[str, str] | str | None] | None = None) -> tuple[str, str]:
    lock = lock or {}
    def junit(slug: str):
        if slug not in cache:
            cache[slug] = _junit(ab / slug / "junit.xml")
        return cache[slug]

    suite, cid = case.get("suite"), case["case_id"]
    if suite in OWNER_BY_SUITE:
        return "BLOCKED-OWNER", OWNER_BY_SUITE[suite]
    slice_ = str(case.get("daemon_slice"))
    if suite in PRIOR_SUITES and slice_ in PRIOR_SLICES:
        return _judge_prior(case, slice_, junit, lock, cargo, prior or {})
    nid = _cargo_nodeid(str(case.get("source_ref") or ""))
    if nid is not None:
        why = _once(cargo, [nid], "passed")
        return ("GAP", f"cargo：{why}") if why else ("PASS", f"cargo {nid}")
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
        if suite == "func-test" and (ab / f"func-{cid}").is_dir():
            found = next(iter(sorted((ab / f"func-{cid}" / "report").rglob("comparison.json"))), None)
            try:
                rc = int((ab / f"func-{cid}" / "rc").read_text(encoding="utf-8").strip())
                verdict = json.loads(found.read_text(encoding="utf-8")).get("verdict") if found else None
            except (OSError, ValueError):
                rc, verdict = None, None
            return ("PASS", "func-test comparison PASS") if (verdict == "PASS" and rc == 0) else (
                "GAP", f"func-test verdict {verdict} rc={rc}")
        if slice_ != "2.3":
            return "GAP", f"daemon_slice {slice_} 無承擔來源"
        slug, nodes = "l15-slice23", [n for n in _frozen(lock, "p7_gate:l15-slice23-b") if n.endswith(f"_rust[{cid}]")]
        why = _once(junit(slug), nodes, "passed")
        return ("GAP", f"l15：{why}") if why else ("PASS", f"l15 {len(nodes)} 節點")
    return "GAP", f"未知 suite {suite}"


def _cargo_results(path: Path | None) -> dict[str, list[str]] | None:
    try:
        rep = json.loads(Path(path).read_text(encoding="utf-8")) if path else None
    except (OSError, ValueError):
        return None
    if not isinstance(rep, dict):
        return None
    out: dict[str, list[str]] = {}
    for r in rep.get("results") or []:
        o = r.get("outcome")
        out.setdefault(str(r.get("nodeid")), []).append("passed" if o == "passed" else "failed" if o in ("failed", "error") else "skipped")
    return out


def _load_json(path: Path | None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8")) if path else None
    except (OSError, ValueError):
        return None


def _p7_identity(p7_results: Path | None) -> tuple[str | None, str | None]:
    """本 gate 的 (git_head, 受測 serialwrapd sha256)；取不到即 None。"""
    doc = _load_json(p7_results)
    if not isinstance(doc, dict):
        return None, None
    arts = doc.get("artifacts") if isinstance(doc.get("artifacts"), dict) else {}
    daemon = [v for k, v in arts.items() if Path(str(k)).name in ("serialwrapd", "serialwrapd.exe")]
    head = doc.get("git_head") if isinstance(doc.get("git_head"), str) else None
    return head or None, (daemon[0] if len(daemon) == 1 and isinstance(daemon[0], str) else None)


def _bind(label: str, rev, daemon_sha, p7: tuple[str | None, str | None]) -> str | None:
    """匯入證據的來源綁定（審查 P7-38c150c-3）：回傳不採用的原因，或 None（採用）。"""
    head, p7_daemon = p7
    if not head or not p7_daemon:
        return "缺本 gate results.json 的 git_head／serialwrapd sha256（--p7-results），無法綁定來源"
    if not isinstance(rev, str) or not rev or not isinstance(daemon_sha, str) or not daemon_sha:
        return f"{label} 缺來源身分（git_revision／daemon_sha256）"
    if rev != head:
        return f"{label} git_revision {rev[:12]} 與本 gate HEAD {head[:12]} 不符"
    if daemon_sha != p7_daemon:
        return f"{label} daemon sha256 {daemon_sha[:12]} 與本 gate 快照 {p7_daemon[:12]} 不符"
    return None


def _prior_verdicts(slice21_run: Path | None, slice22: Path | None,
                    p7_results: Path | None = None) -> tuple[dict[str, dict[str, str] | str | None], dict]:
    """先前切片 gate 的逐案證據與來源：2.1＝slice21 gate run 目錄（`report.json`＋`case_verdicts.json`）、2.2＝s22 gate
    輸出（`cases[]`＋`identity`）。未提供、讀不到、格式不符 → None；來源綁定不成立 → 原因字串（該切片案例一律缺漏）。"""
    p7 = _p7_identity(p7_results)
    out: dict[str, dict[str, str] | str | None] = {"2.1": None, "2.2": None}
    sources: dict[str, dict] = {}
    if slice21_run is not None:
        rep = _load_json(Path(slice21_run) / "report.json")
        s21 = _load_json(Path(slice21_run) / "case_verdicts.json")
        ident = rep.get("identity") if isinstance(rep, dict) and isinstance(rep.get("identity"), dict) else {}
        rev = rep.get("git_revision") if isinstance(rep, dict) else None
        why = (_bind("slice21 gate report.json", rev, ident.get("daemon_sha256"), p7) if isinstance(rep, dict)
               else "slice21 gate run 目錄缺 report.json（無來源身分）")
        if why is None and not (isinstance(s21, dict)
                                and all(isinstance(k, str) and isinstance(v, str) for k, v in s21.items())):
            why = "case_verdicts.json 缺或格式不符"
        out["2.1"] = why if why else dict(s21)
        sources["2.1"] = {"path": str(slice21_run), "run_id": rep.get("run_id") if isinstance(rep, dict) else None,
                          "git_revision": rev, "daemon_sha256": ident.get("daemon_sha256"),
                          "accepted": why is None, "reason": why}
    if slice22 is not None:
        doc = _load_json(slice22)
        rows = doc.get("cases") if isinstance(doc, dict) else None
        ident = doc.get("identity") if isinstance(doc, dict) and isinstance(doc.get("identity"), dict) else {}
        why = _bind("s22 gate 輸出", ident.get("git_revision"), ident.get("daemon_sha256"), p7)
        if why is None and not (isinstance(rows, list)
                                and all(isinstance(r, dict) and isinstance(r.get("case_id"), str) for r in rows)):
            why = "s22 gate 輸出缺 cases[] 或格式不符"
        if why is None:
            seen: dict[str, str] = {}
            for r in rows:
                cid = r["case_id"]
                seen[cid] = "DUPLICATE" if cid in seen else str(r.get("status"))
            out["2.2"] = seen
        else:
            out["2.2"] = why
        sources["2.2"] = {"path": str(slice22), "git_revision": ident.get("git_revision"),
                          "daemon_sha256": ident.get("daemon_sha256"), "accepted": why is None, "reason": why}
    return out, sources


def build(ab: Path, cases_yaml: Path, cases_lock: Path, bc_yaml: Path, l1_lock: Path | None = None,
          cargo_l1: Path | None = None, slice21_run: Path | None = None,
          slice22_gate: Path | None = None, p7_results: Path | None = None) -> dict:
    cargo = _cargo_results(cargo_l1)
    prior, prior_sources = _prior_verdicts(slice21_run, slice22_gate, p7_results)
    lock = json.loads(cases_lock.read_text(encoding="utf-8"))
    l1 = json.loads(Path(l1_lock or ROOT / "tests/acceptance/l1_required.lock.json").read_text(encoding="utf-8"))
    cases = {c["case_id"]: c for c in yaml.safe_load(cases_yaml.read_text(encoding="utf-8"))["cases"]}
    rust_only = _rust_only(bc_yaml)
    cache: dict = {}
    rows = []
    for cid in sorted(lock["sets"]["slice_2.3"]):
        case = cases.get(cid)
        status, detail = ("GAP", "cases.yaml 無此案例") if case is None else judge_case(case, ab, rust_only, cache, l1, cargo, prior)
        rows.append({"case_id": cid, "status": status, "detail": detail})
    gaps = [r for r in rows if r["status"] not in ("PASS", "BLOCKED-OWNER")]
    return {"set": "slice_2.3", "total": len(rows), "pass": sum(r["status"] == "PASS" for r in rows),
            "blocked_owner": sum(r["status"] == "BLOCKED-OWNER" for r in rows), "gaps": len(gaps), "rows": rows,
            "prior_sources": prior_sources}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--ab-dir", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--cases", type=Path, default=ROOT / "tests/acceptance/cases.yaml")
    ap.add_argument("--cases-lock", type=Path, default=ROOT / "tests/acceptance/cases.lock.json")
    ap.add_argument("--behavior-changes", type=Path, default=ROOT / "contracts/behavior_changes.yaml")
    ap.add_argument("--l1-lock", type=Path, default=ROOT / "tests/acceptance/l1_required.lock.json")
    ap.add_argument("--cargo-l1", type=Path, help="gate 的 cargo L1 報告（crates/ 來源案例）")
    ap.add_argument("--p7-results", type=Path, help="本 gate 的 results.json（匯入證據的 HEAD／daemon 綁定）")
    ap.add_argument("--slice21-run", type=Path, help="slice21 gate run 目錄（report.json＋case_verdicts.json；2.1 先前切片案例）")
    ap.add_argument("--slice22-gate", type=Path, help="s22 gate（s22_gate_check.py）的輸出 JSON，須含 identity（2.2 先前切片案例）")
    args = ap.parse_args(argv)
    report = build(args.ab_dir, args.cases, args.cases_lock, args.behavior_changes, args.l1_lock, args.cargo_l1,
                   args.slice21_run, args.slice22_gate, args.p7_results)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"slice_2.3 coverage：{report['pass']} PASS、{report['blocked_owner']} BLOCKED-OWNER、{report['gaps']} 缺漏"
          f"（共 {report['total']}）")
    for r in report["rows"]:
        if r["status"] not in ("PASS", "BLOCKED-OWNER"):
            print(f"  GAP {r['case_id']}：{r['detail']}")
    return 1 if report["gaps"] else 0


if __name__ == "__main__":
    sys.exit(main())
