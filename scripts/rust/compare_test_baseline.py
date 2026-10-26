#!/usr/bin/env python3
"""cargo test 結果與上游基線逐 test binary 比對（07 Task 1 Step 4；parity-amendments-2026-10-26 #179，P6 審查 F1）。

P5 基線本身有骨架紅燈（`s21_todo!`），「06 既有測試全部仍綠燈」恆不成立，gate 形同空洞。改為：
同一個 cargo 指令在基線（P5）與待測（P6）各跑一次，逐 test binary、逐測試比對結果。

判定（預設）：
  - 基線 `ok` 而待測非 `ok`（FAILED、ignored 或消失）→ REGRESSION，exit 1。
  - 基線有、待測沒有的 binary 或測試（不論基線結果）→ MISSING，exit 1（不得以刪測試縮小分母）。
  - 基線非 `ok` 而待測 `ok` → 改善，只列出。
  - 只在待測出現的測試（P6 新增，含 TDD 紅燈）→ 只列出，由各 Task 的 Step 判定。
  - `--strict`：任何狀態變化（含改善）都 exit 1。
  - 任一邊解析不到任何測試（例如漏了 `--features test-fakes` 整檔被編掉）→ exit 2。

用法：
  python3 scripts/rust/compare_test_baseline.py run --dir <worktree> --out <log>
      # 在 <worktree> 執行 `cargo test --workspace --features test-fakes --no-fail-fast`，輸出存到 <log>
  python3 scripts/rust/compare_test_baseline.py compare <基線 log> <待測 log> [--strict] [--json <報告>]

stdlib only；比對只讀 cargo 的人類可讀輸出（`Running …`／`Doc-tests …`／`test <名> ... <結果>`）。
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import subprocess
import sys

CARGO_ARGS = ["test", "--workspace", "--features", "test-fakes", "--no-fail-fast"]

_RUNNING = re.compile(r"^\s*Running (?P<src>.+?) \((?:.*[/\\])?(?P<bin>[^/\\()]+?)(?:-[0-9a-f]{16})?(?:\.exe)?\)\s*$")
_DOCTESTS = re.compile(r"^\s*Doc-tests (?P<crate>\S+)\s*$")
_RESULT = re.compile(r"^test (?P<name>.+?) \.\.\. (?P<status>ok|FAILED|ignored)\b")

Results = dict[str, dict[str, str]]


def parse_cargo_log(text: str) -> Results:
    """cargo test 輸出 → {binary 鍵: {測試名: ok|FAILED|ignored}}。

    binary 鍵為 `<來源> (<binary 名，去掉 hash>)`；doc-test 為 `doc-tests <crate>`。
    同一 binary 內重複出現的測試名以最後一次為準（cargo 不會重複輸出）。
    """
    out: Results = {}
    current: str | None = None
    for line in text.splitlines():
        m = _RUNNING.match(line)
        if m:
            current = f"{m.group('src')} ({m.group('bin')})"
            out.setdefault(current, {})
            continue
        m = _DOCTESTS.match(line)
        if m:
            current = f"doc-tests {m.group('crate')}"
            out.setdefault(current, {})
            continue
        m = _RESULT.match(line)
        if m and current is not None:
            out[current][m.group("name")] = m.group("status")
    return out


def compare(base: Results, head: Results, *, strict: bool = False) -> dict:
    regressions: list[tuple[str, str, str, str]] = []
    missing: list[tuple[str, str]] = []
    improvements: list[tuple[str, str, str, str]] = []
    changed: list[tuple[str, str, str, str]] = []
    head_only: list[tuple[str, str, str]] = []
    for binary in sorted(base):
        if binary not in head:
            missing.append((binary, "*"))
            continue
        for name in sorted(base[binary]):
            before = base[binary][name]
            after = head[binary].get(name)
            if after is None:
                missing.append((binary, name))
            elif before == "ok" and after != "ok":
                regressions.append((binary, name, before, after))
            elif before != "ok" and after == "ok":
                improvements.append((binary, name, before, after))
            elif before != after:
                changed.append((binary, name, before, after))
    for binary in sorted(head):
        for name in sorted(head[binary]):
            if name not in base.get(binary, {}):
                head_only.append((binary, name, head[binary][name]))
    ok = not regressions and not missing and not (strict and (improvements or changed))
    return {
        "ok": ok,
        "regressions": regressions,
        "missing": missing,
        "improvements": improvements,
        "changed": changed,
        "head_only": head_only,
    }


def _count(results: Results) -> int:
    return sum(len(v) for v in results.values())


def _cmd_run(args: argparse.Namespace) -> int:
    env = dict(os.environ)
    env.setdefault("CARGO_TARGET_DIR", str(pathlib.Path(args.dir) / "target"))
    with open(args.out, "w", encoding="utf-8", newline="\n") as fh:
        proc = subprocess.run(["cargo", *CARGO_ARGS], cwd=args.dir, env=env, stdout=fh, stderr=subprocess.STDOUT,
                              check=False)
    n = _count(parse_cargo_log(pathlib.Path(args.out).read_text(encoding="utf-8", errors="replace")))
    print(f"[compare_test_baseline] {args.dir}：cargo rc={proc.returncode}，解析 {n} 個測試 → {args.out}")
    return 0 if n > 0 else 2


def _cmd_compare(args: argparse.Namespace) -> int:
    base = parse_cargo_log(pathlib.Path(args.base).read_text(encoding="utf-8", errors="replace"))
    head = parse_cargo_log(pathlib.Path(args.head).read_text(encoding="utf-8", errors="replace"))
    if _count(base) == 0 or _count(head) == 0:
        print(f"[compare_test_baseline] FAIL：解析到的測試數 base={_count(base)} head={_count(head)}"
              "（是否漏了 --features test-fakes 或編譯失敗？）")
        return 2
    report = compare(base, head, strict=args.strict)
    for b, n, x, y in report["regressions"]:
        print(f"REGRESSION {b}::{n} {x}→{y}")
    for b, n in report["missing"]:
        print(f"MISSING {b}::{n}")
    for b, n, x, y in report["changed"]:
        print(f"CHANGED {b}::{n} {x}→{y}")
    for b, n, x, y in report["improvements"]:
        print(f"IMPROVED {b}::{n} {x}→{y}")
    head_red = [t for t in report["head_only"] if t[2] != "ok"]
    print(f"[compare_test_baseline] base {_count(base)} 個、head {_count(head)} 個；"
          f"回歸 {len(report['regressions'])}、消失 {len(report['missing'])}、改善 {len(report['improvements'])}、"
          f"head 新增 {len(report['head_only'])}（其中非 ok {len(head_red)}）")
    if args.json:
        pathlib.Path(args.json).write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print("[compare_test_baseline] " + ("OK：與基線逐 binary 結果相同" if report["ok"] else "FAIL"))
    return 0 if report["ok"] else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="在指定 worktree 執行 cargo test 並存 log")
    run.add_argument("--dir", required=True)
    run.add_argument("--out", required=True)
    cmp_ = sub.add_parser("compare", help="比對兩份 cargo test log")
    cmp_.add_argument("base")
    cmp_.add_argument("head")
    cmp_.add_argument("--strict", action="store_true", help="任何狀態變化（含改善）皆視為失敗")
    cmp_.add_argument("--json", default=None, help="另寫出 JSON 報告")
    args = ap.parse_args(argv)
    return _cmd_run(args) if args.cmd == "run" else _cmd_compare(args)


if __name__ == "__main__":
    sys.exit(main())
