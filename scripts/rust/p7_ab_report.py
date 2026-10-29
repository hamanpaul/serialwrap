#!/usr/bin/env python3
"""Slice 2.3（P7）黑箱 A/B 執行紀錄產生器（08-slice23-login-events.md Task 18 Step 3；規格 §15.9.1）。

輸入兩輪 pytest junit（A＝Python 基線、B＝Rust CLI＋daemon），依 `tests/acceptance/cases.yaml` 中 suite 為
`contract-blackbox`、`source_ref` 指向 `tests/contract_blackbox/test_p7_*.py` 的案例逐列判定：
- PASS：該 test function 的所有 testcase（含 parametrize）皆通過。
- FAIL：任一 failure／error。
- SKIP＋原因：全數 skip，且僅限 cases.yaml 已登記的平台限定——A 輪 skip 只在案例的 behavior_changes 含
  `scope: rust_only` 的 SC（如 SC-30）或原因為 `baseline_variant_re2` 時接受；B 輪 skip 一律不接受。
- INCOMPLETE：junit 缺檔／不可解析，或該案例無任何 testcase（證據不足不得 PASS，§11.3-6）。
結束碼：全列 A∈{PASS,可接受 SKIP} 且 B＝PASS → 0；有 FAIL → 1；否則（含 INCOMPLETE）→ 3；參數錯誤 → 2。
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


def parse_junit(path: Path | None) -> dict[tuple[str, str], list[tuple[str, str]]] | None:
    """回傳 {(file, function): [(outcome, message)]}；缺檔或不可解析 → None。"""
    if path is None:
        return None
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError):
        return None
    out: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for tc in root.iter("testcase"):
        cls = tc.get("classname") or ""
        name = tc.get("name") or ""
        file = cls.replace(".", "/") + ".py"
        func = name.split("[", 1)[0]
        outcome, msg = "passed", ""
        for child in tc:
            if child.tag in ("failure", "error"):
                outcome, msg = "failed", child.get("message") or ""
            elif child.tag == "skipped":
                outcome, msg = "skipped", child.get("message") or ""
        out.setdefault((file, func), []).append((outcome, msg))
    return out


def judge_side(results, case: dict, side: str, rust_only: set[str]) -> tuple[str, str]:
    if results is None:
        return "INCOMPLETE", "junit 缺檔或不可解析"
    file, func = case["source_ref"].split("::", 1)
    rows = results.get((file, func))
    if not rows:
        return "INCOMPLETE", "無 testcase"
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


def build(args) -> tuple[str, int]:
    cases = load_cases(args.cases)
    rust_only = rust_only_sc(args.behavior_changes)
    ja, jb = parse_junit(args.junit_a), parse_junit(args.junit_b)
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
        f"- evidence A: `{args.junit_a}` sha256 `{_sha256_file(args.junit_a)}`",
        f"- evidence B: `{args.junit_b}` sha256 `{_sha256_file(args.junit_b)}`",
        "",
        "| case_id | A | B | 說明 |",
        "|---|---|---|---|",
    ]
    rc = 0
    any_fail = any_incomplete = False
    for c in cases:
        a, ad = judge_side(ja, c, "A", rust_only)
        b, bd = judge_side(jb, c, "B", rust_only)
        any_fail |= "FAIL" in (a, b)
        any_incomplete |= "INCOMPLETE" in (a, b) or b == "SKIP"
        note = "；".join(x for x in (f"A：{ad}" if a != "PASS" else "", f"B：{bd}" if b != "PASS" else "") if x)
        head.append(f"| {c['case_id']} | {a} | {b} | {note.replace('|', '/')} |")
    rc = 1 if any_fail else (3 if any_incomplete else 0)
    head += ["", f"結論：{ {0: 'PASS', 1: 'FAIL', 3: 'INCOMPLETE'}[rc] }", ""]
    return "\n".join(head), rc


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--junit-a", type=Path)
    ap.add_argument("--junit-b", type=Path)
    ap.add_argument("--run-id", required=True)
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
