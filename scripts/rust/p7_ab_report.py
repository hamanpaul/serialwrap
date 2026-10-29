#!/usr/bin/env python3
"""Slice 2.3（P7）黑箱 A/B 執行紀錄產生器（08-slice23-login-events.md Task 18 Step 3；規格 §15.9.1）。

輸入兩輪 pytest junit（A＝Python 基線、B＝Rust CLI＋daemon），依 `tests/acceptance/cases.yaml` 中 suite 為
`contract-blackbox`、`source_ref` 指向 `tests/contract_blackbox/test_p7_*.py` 的案例逐列判定：
- 必要節點＝`--collected`（同一 commit 的 `pytest --collect-only -q`）中屬於該案例的完整 nodeid（含 `[參數]`）；每個
  節點須在 junit 恰出現一次，缺漏或重複 → INCOMPLETE（審查 P7-c5f0fc9-5）。
- PASS：所有必要節點皆通過。
- FAIL：任一 failure／error。
- SKIP＋原因：全數 skip，且僅限 cases.yaml 已登記的平台限定——A 輪 skip 只在案例的 behavior_changes 含
  `scope: rust_only` 的 SC（如 SC-30）或原因為 `baseline_variant_re2` 時接受；B 輪 skip 一律不接受。
- INCOMPLETE：junit 缺檔／不可解析，或該案例無任何 testcase（證據不足不得 PASS，§11.3-6）。
- 整份 junit 的任何 failure／error（含未映射到案例的節點）→ FAIL；pytest 結束碼（`--rc-a`／`--rc-b`）缺或非 0／1、
  或為 1 卻無失敗節點 → INCOMPLETE（審查 P7-c5f0fc9-4）。
先前切片必要回歸（`--regression-dir`，`run_p7_ab.sh` 的 run 目錄；§11.5／§11.6）逐階段判定：
- pytest 階段（`REGRESSION_PHASES`）：目錄或 junit 缺／不可解析 → INCOMPLETE；`blocked` 標記檔 → BLOCKED-UPSTREAM；
  任一 failure／error → FAIL；無 testcase、任一 skip、或 rc 非 0 卻無失敗節點 → INCOMPLETE；其餘 PASS。
- func-test 階段（`FUNC_CASES`）：`comparison.json` verdict PASS 且 rc 0 → PASS；verdict FAIL 或 rc 1 → FAIL；
  缺 comparison.json 或其他 → INCOMPLETE（證據不足不得 PASS）。
結束碼：全列 A∈{PASS,可接受 SKIP} 且 B＝PASS（且回歸各階段 PASS）→ 0；有 FAIL → 1；否則（含 INCOMPLETE、
BLOCKED-UPSTREAM）→ 3；參數錯誤 → 2。
只用 stdlib＋PyYAML；不 import sw_core。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import platform as _platform
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
PREFIX = "tests/contract_blackbox/test_p7_"
# 先前切片必要回歸的 pytest 階段（run 目錄下的子目錄名 → 說明）。
REGRESSION_PHASES: tuple[tuple[str, str], ...] = (
    ("slice21-b", "contract_blackbox slice21（Rust）"),
    ("slice22-b", "contract_blackbox slice22（Rust；P6 human console）"),
    ("l15-slice21-b", "l15 slice21（Rust 參數＋凍結清單）"),
    ("l15-slice23", "l15 slice23（Rust；s23 情境含 Python 基線）"),
)
FUNC_CASES: tuple[str, ...] = ("brcm-01-login-flow", "ev-01-spawn-handler")


def _sha256_file(p: Path | None) -> str | None:
    try:
        return hashlib.sha256(Path(p).read_bytes()).hexdigest() if p else None
    except OSError:
        return None


def _git(*args: str) -> str | None:
    try:
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def _rel(p: Path | None) -> str | None:
    """證據路徑以 repo 相對路徑呈現（R-21：不得寫入主機絕對路徑）。"""
    if p is None:
        return None
    try:
        return str(Path(p).resolve().relative_to(ROOT))
    except ValueError:
        return Path(p).name


def load_cases(cases_yaml: Path) -> list[dict]:
    data = yaml.safe_load(cases_yaml.read_text(encoding="utf-8"))
    out = []
    for c in data["cases"]:
        ref = str(c.get("source_ref") or "")
        if c.get("suite") == "contract-blackbox" and ref.startswith(PREFIX) and "::" in ref:
            out.append(c)
    return out


def rust_only_sc(bc_yaml: Path) -> set[str]:
    data = yaml.safe_load(bc_yaml.read_text(encoding="utf-8"))
    return {c["id"] for c in data["changes"] if c.get("scope") == "rust_only"}


def parse_junit(path: Path | None) -> dict[str, list[tuple[str, str]]] | None:
    """回傳 {完整 nodeid（含 `[參數]`）: [(outcome, message)]}；缺檔或不可解析 → None。"""
    if path is None:
        return None
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError):
        return None
    out: dict[str, list[tuple[str, str]]] = {}
    for tc in root.iter("testcase"):
        nodeid = (tc.get("classname") or "").replace(".", "/") + ".py::" + (tc.get("name") or "")
        outcome, msg = "passed", ""
        for child in tc:
            if child.tag in ("failure", "error"):
                outcome, msg = "failed", child.get("message") or ""
            elif child.tag == "skipped":
                outcome, msg = "skipped", child.get("message") or ""
        out.setdefault(nodeid, []).append((outcome, msg))
    return out


def load_collected(path: Path | None) -> list[str] | None:
    """`pytest --collect-only -q` 的 nodeid 清單（必要節點的凍結來源，含參數化）；缺檔 → None。"""
    if path is None:
        return None
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    return [ln.strip() for ln in lines if "::" in ln and not ln.startswith(" ")]


def case_nodes(case: dict, collected: list[str]) -> list[str]:
    ref = case["source_ref"]
    return [n for n in collected if n == ref or n.startswith(ref + "[")]


def judge_side(results, case: dict, side: str, rust_only: set[str], collected: list[str] | None) -> tuple[str, str]:
    if results is None:
        return "INCOMPLETE", "junit 缺檔或不可解析"
    if collected is None:
        return "INCOMPLETE", "缺 collect-only 清單（必要節點無法核對）"
    nodes = case_nodes(case, collected)
    if not nodes:
        return "INCOMPLETE", "無 testcase（collect-only 未收集到）"
    rows: list[tuple[str, str]] = []
    for n in nodes:
        got = results.get(n) or []
        if len(got) != 1:
            return "INCOMPLETE", f"{'缺' if not got else '重複'}節點：{n.split('::', 1)[1]}"
        rows.extend(got)
    if any(o == "failed" for o, _ in rows):
        return "FAIL", next(m for o, m in rows if o == "failed")[:200]
    if all(o == "passed" for o, _ in rows):
        return "PASS", f"{len(rows)} testcase"
    if all(o == "skipped" for o, _ in rows):
        reason = rows[0][1]
        allowed = side == "A" and (
            bool(set(case.get("behavior_changes") or []) & rust_only) or "baseline_variant_re2" in reason
        )
        return ("SKIP", reason[:200]) if allowed else ("INCOMPLETE", f"不允許的 SKIP：{reason[:200]}")
    return "INCOMPLETE", "部分 skip"


def _read_rc(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def judge_phase(d: Path) -> tuple[str, str]:
    """pytest 回歸階段：見模組說明。"""
    blocked = d / "blocked"
    if blocked.is_file():
        return "BLOCKED-UPSTREAM", blocked.read_text(encoding="utf-8").strip()[:200]
    res = parse_junit(d / "junit.xml")
    if res is None:
        return "INCOMPLETE", "junit 缺檔或不可解析"
    rc = _read_rc(d / "rc")
    failed = sorted(n for n, rows in res.items() if any(o == "failed" for o, _ in rows))
    if failed:
        return "FAIL", ", ".join(x.split("::", 1)[1] for x in failed)[:400]
    if not res:
        return "INCOMPLETE", "無 testcase"
    skipped = sorted(n for n, rows in res.items() if any(o == "skipped" for o, _ in rows))
    if skipped:
        return "INCOMPLETE", f"skip {len(skipped)} 筆：" + ", ".join(x.split("::", 1)[1] for x in skipped)[:300]
    if rc != 0:
        return "INCOMPLETE", f"rc={rc}"
    return "PASS", f"{len(res)} testcase"


def judge_func(d: Path) -> tuple[str, str]:
    """func-test 回歸階段：comparison.json 的 verdict 與 runner 結束碼。"""
    rc = _read_rc(d / "rc")
    found = next(iter(sorted((d / "report").rglob("comparison.json"))), None) if (d / "report").is_dir() else None
    if found is None:
        return ("FAIL", f"rc={rc}，無 comparison.json") if rc == 1 else ("INCOMPLETE", f"無 comparison.json（rc={rc}）")
    try:
        verdict = json.loads(found.read_text(encoding="utf-8")).get("verdict")
    except (OSError, ValueError):
        return "INCOMPLETE", "comparison.json 不可解析"
    if verdict == "PASS" and rc == 0:
        return "PASS", f"verdict PASS（{_rel(found)} sha256 {_sha256_file(found)}）"
    if verdict == "FAIL" or rc == 1:
        return "FAIL", f"verdict {verdict}、rc={rc}"
    return "INCOMPLETE", f"verdict {verdict}、rc={rc}"


def build(args) -> tuple[str, int]:
    cases = load_cases(args.cases)
    rust_only = rust_only_sc(args.behavior_changes)
    ja, jb = parse_junit(args.junit_a), parse_junit(args.junit_b)
    collected = load_collected(args.collected)
    lock = json.loads(args.cases_lock.read_text(encoding="utf-8"))
    s23 = lock["sets"]["slice_2.3"]
    head = [
        "# P7 黑箱 A/B 執行紀錄",
        "",
        f"- run_id: `{args.run_id}`",
        f"- git_head: `{_git('rev-parse', 'HEAD')}`",
        f"- contracts_revision: `{_git('rev-parse', 'HEAD:contracts')}`",
        f"- cases_yaml_sha256: `{_sha256_file(args.cases)}`",
        f"- slice_2.3_set_sha256: `{hashlib.sha256(json.dumps(sorted(s23), separators=(',', ':')).encode()).hexdigest()}`",
        f"- tools_revision: contract_blackbox `{_git('rev-parse', 'HEAD:tests/contract_blackbox')}`、"
        f"uart_stdio_bridge `{_git('rev-parse', 'HEAD:tools/uart_stdio_bridge')}`",
        f"- rust_cli_sha256: `{_sha256_file(args.rust_cli)}`",
        f"- rust_daemon_sha256: `{_sha256_file(args.rust_daemon)}`",
        f"- platform: `{_platform.system().lower()}`；transport: `faketarget_pty`",
        f"- evidence A: `{_rel(args.junit_a)}` sha256 `{_sha256_file(args.junit_a)}`",
        f"- evidence B: `{_rel(args.junit_b)}` sha256 `{_sha256_file(args.junit_b)}`",
        "",
        "| case_id | A | B | 說明 |",
        "|---|---|---|---|",
    ]
    rc = 0
    any_fail = any_incomplete = False
    for c in cases:
        a, ad = judge_side(ja, c, "A", rust_only, collected)
        b, bd = judge_side(jb, c, "B", rust_only, collected)
        any_fail |= "FAIL" in (a, b)
        any_incomplete |= "INCOMPLETE" in (a, b) or b == "SKIP"
        note = "；".join(x for x in (f"A：{ad}" if a != "PASS" else "", f"B：{bd}" if b != "PASS" else "") if x)
        head.append(f"| {c['case_id']} | {a} | {b} | {note.replace('|', '/')} |")
    # 整份 junit 的 failure／error 一律阻擋（含未映射到案例的節點，審查 P7-c5f0fc9-4）；pytest 結束碼非 0 不得 PASS。
    for side, res, prc in (("A", ja, args.rc_a), ("B", jb, args.rc_b)):
        failed = sorted(n for n, rows in (res or {}).items() if any(o == "failed" for o, _ in rows))
        if failed:
            any_fail = True
            head.append(f"| （{side} 輪失敗節點） | FAIL | | {', '.join(x.split('::', 1)[1] for x in failed)[:500]} |")
        if prc is None or prc not in (0, 1):
            any_incomplete = True
            head.append(f"| （{side} 輪 pytest 結束碼） | INCOMPLETE | | rc={prc} |")
        elif prc == 1 and not failed:
            any_incomplete = True
            head.append(f"| （{side} 輪 pytest 結束碼） | INCOMPLETE | | rc=1 但 junit 無失敗節點 |")
    if args.regression_dir is not None:
        head += ["", "## 先前切片必要回歸（Rust artifact）", "", "| 階段 | 結果 | 說明 | evidence sha256 |", "|---|---|---|---|"]
        rows = [(slug, title, judge_phase(args.regression_dir / slug), args.regression_dir / slug / "junit.xml")
                for slug, title in REGRESSION_PHASES]
        rows += [(f"func-{c}", f"func-test {c}（bridge，Rust）", judge_func(args.regression_dir / f"func-{c}"),
                  args.regression_dir / f"func-{c}" / "log") for c in FUNC_CASES]
        for slug, title, (st, detail), ev in rows:
            any_fail |= st == "FAIL"
            any_incomplete |= st != "PASS"
            head.append(f"| {title} | {st} | {detail.replace('|', '/')} | `{_rel(ev)}` {_sha256_file(ev)} |")
    rc = 1 if any_fail else (3 if any_incomplete else 0)
    head += ["", f"結論：{ {0: 'PASS', 1: 'FAIL', 3: 'INCOMPLETE'}[rc] }", ""]
    return "\n".join(head), rc


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--junit-a", type=Path)
    ap.add_argument("--junit-b", type=Path)
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--collected", type=Path, help="pytest --collect-only -q 輸出（必要節點）")
    ap.add_argument("--rc-a", type=int)
    ap.add_argument("--rc-b", type=int)
    ap.add_argument("--regression-dir", type=Path, help="run_p7_ab.sh 的 run 目錄（先前切片必要回歸各階段）")
    ap.add_argument("--out", type=Path)
    ap.add_argument("--rust-cli", type=Path)
    ap.add_argument("--rust-daemon", type=Path)
    ap.add_argument("--cases", type=Path, default=ROOT / "tests/acceptance/cases.yaml")
    ap.add_argument("--cases-lock", type=Path, default=ROOT / "tests/acceptance/cases.lock.json")
    ap.add_argument("--behavior-changes", type=Path, default=ROOT / "contracts/behavior_changes.yaml")
    try:
        args = ap.parse_args(argv)
    except SystemExit:
        return 2
    md, rc = build(args)
    if args.out:
        args.out.write_text(md, encoding="utf-8", newline="\n")
    else:
        sys.stdout.write(md)
    return rc


if __name__ == "__main__":
    sys.exit(main())
