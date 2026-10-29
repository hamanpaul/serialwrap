#!/usr/bin/env python3
"""Slice 2.3（P7）出口 gate 報告產生器與驗證器（08-slice23-login-events.md Task 19）。

`p7_gate.sh` 只負責「執行並記錄」：每個子程序的 argv、rc、時間、log（落檔＋sha256）、測試計數與 evidence 寫入
`<run>/results.json`；列狀態一律由本檔計算（gate 腳本本身不判定）。

狀態規則（R5-F3）：
- 測試列（`TEST_ROWS`）PASS ⇔ rc==0、`required_set` 非 null、`l1_results`（P2 `l1-results.v1`）經 P2
  `tests/acceptance/l1_judge.judge` 判定 PASS（每個凍結 nodeid 恰一列 passed、無重複、無 skip／未執行、exit 0、
  finished）、`test_counts` 與 l1 彙總相符、所有 evidence 檔存在且 sha256 相符；l1_judge FAIL 或任一列 failed → FAIL；
  INSUFFICIENT／INVALID／缺 l1_results／缺 required_set → INCOMPLETE；`required_set` 必須等於列名對應的
  `p7_gate:<slug>`（不得以較小集合替代），lock 的 selector 一併交給 judge 核對。
- 非測試列 PASS ⇔ rc==0 且至少一項 evidence、每項皆有有效 sha256 且相符；rc!=0 → FAIL；evidence 不符 → INCOMPLETE。
- 能力缺失（`caps_missing`）→ 該列 BLOCKED-OWNER＋B 編號。
- 擁有者項目 B-01～B-08：只有 `owner_evidence[B-xx]` 的檔案存在且 sha256 相符時 PASS，否則 BLOCKED-OWNER。
- `REQUIRED_ROWS` 缺列 → INCOMPLETE（未執行）。

用法：
  python3 scripts/rust/p7_gate_report.py init   --results-dir <run> --run-id <id> [--caps-missing c …] [--artifact p …]
  python3 scripts/rust/p7_gate_report.py record --results-dir <run> --name <列名> --rc <n> --log <p> --argv-json <j>
                                                --started-at <t> --ended-at <t> [--l1-results <p>] [--platform linux]
                                                [--evidence <p> …]       # 只記錄（log／evidence 落 sha256、測試計數取自 l1）
  python3 scripts/rust/p7_gate_report.py build  --results-dir <run> [--l1-lock <lock>] [--out <md>]
  python3 scripts/rust/p7_gate_report.py verify --results-dir <run> --md <md>     # 不一致清單非空 → exit 1
只用 stdlib；不 import sw_core。
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_L1_LOCK = ROOT / "tests/acceptance/l1_required.lock.json"

REQUIRED_ROWS: tuple[str, ...] = (
    "check_cfg_boundary",
    "cargo test --workspace",
    "win-test.sh (non-COM)",
    "win-test.sh --caps windows_com",
    "contract_blackbox p7 A",
    "contract_blackbox p7 B",
    "contract_blackbox slice21 B",
    "contract_blackbox slice22 B",
    "l15 slice21 B",
    "l15 slice23 B",
    "func-test brcm-01-login-flow",
    "func-test ev-01-spawn-handler",
    "regen_lock --check",
    "cases.lock slice_2.3 coverage",
    "python3 -m pytest -q tests/",
    "policy_check",
)
TEST_ROWS: frozenset[str] = frozenset({
    "cargo test --workspace",
    "win-test.sh (non-COM)",
    "win-test.sh --caps windows_com",
    "contract_blackbox p7 A",
    "contract_blackbox p7 B",
    "contract_blackbox slice21 B",
    "contract_blackbox slice22 B",
    "l15 slice21 B",
    "l15 slice23 B",
    "python3 -m pytest -q tests/",
})
# 能力缺失 → BLOCKED-OWNER 的對應（列名 → (cap, B 編號)）。
CAP_ROWS: Mapping[str, tuple[str, str]] = {
    "win-test.sh (non-COM)": ("windows_native", "B-06"),
    "win-test.sh --caps windows_com": ("windows_com", "B-06"),
}
OWNER_ITEMS: Mapping[str, str] = {
    "B-01": "四平台實機登入（bcm、prpl、op3 shell、smart4R console unlock）",
    "B-02": "L2 F9 六案（destructive）",
    "B-03": "真實 USB／usbip read-endpoint stall",
    "B-04": "L2 F10 兩案（destructive）",
    "B-05": "L2 F11 兩案",
    "B-06": "原生 Windows COM handle 證據（OD-08、OD-15）",
    "B-07": "L3 realhw P0＋登入／Recovery／事件 P1 與 remote",
    "B-08": "測試團隊 Windows 工作站 event handler",
}
# parity-amendments-2026-10-26 #188：P7 承接列的必要 L1 集合（Task 0 起登記；py_parity_* 為後續 Task 增列）。
PARITY_SETS: tuple[str, ...] = ("p7_gate:py_parity", "p7_gate:py_parity_recover", "p7_gate:py_parity_attach")


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
# verify 必須核對的受測 artifact（Rust CLI 與 daemon；路徑為 repo 相對，檔名可帶 `.exe`）：缺任一 → 驗證失敗
#（審查 P7-c5f0fc9-3：`artifacts:{}` 不得通過）。
REQUIRED_ARTIFACTS: tuple[str, ...] = ("serialwrap", "serialwrapd")


def slug(name: str) -> str:
    """列名 → 必要集合 slug（`cargo test --workspace` → `cargo-test-workspace`）。"""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def expected_set(name: str) -> str:
    """列名對應的必要集合固定為 `p7_gate:<slug>`，不得以其他集合替代（審查 P7-c5f0fc9-1）。"""
    return f"p7_gate:{slug(name)}"


def current_head() -> str | None:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True,
                              check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def _load_judge():
    spec = importlib.util.spec_from_file_location("p7_l1_judge", ROOT / "tests/acceptance/l1_judge.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _resolve(base: Path, p: str) -> Path:
    q = Path(p)
    return q if q.is_absolute() else base / q


def _evidence_errors(base: Path, evidence: Any, *, required: bool) -> list[str]:
    """每項證據須有路徑與有效 sha256 且相符；`required` 時不得為空（審查 P7-c5f0fc9-2）。"""
    items = list(evidence or [])
    if required and not items:
        return ["缺必要證據"]
    errs: list[str] = []
    for item in items:
        if not isinstance(item, Mapping) or not isinstance(item.get("path"), str):
            errs.append(f"evidence 格式錯誤：{item!r}")
            continue
        want = item.get("sha256")
        if not (isinstance(want, str) and _SHA256_RE.match(want)):
            errs.append(f"evidence 缺有效 sha256：{item['path']}")
            continue
        got = _sha256(_resolve(base, item["path"]))
        if got is None:
            errs.append(f"evidence 缺檔：{item['path']}")
        elif got != want:
            errs.append(f"evidence sha256 不符：{item['path']}")
    return errs


def _test_row_status(base: Path, row: Mapping[str, Any], lock: Mapping[str, Any] | None, judge) -> tuple[str, str]:
    counts = row.get("test_counts") or {}
    rc = row.get("rc")
    sid = row.get("required_set")
    l1_path = row.get("l1_results")
    if counts.get("failed"):
        # 任一列 failed 即 FAIL（不論是否附 l1_results；OD-15 COM 列依實跑判定）。
        return "FAIL", f"failed={counts.get('failed')} rc={rc}"
    if not sid:
        return "INCOMPLETE", "缺 required_set"
    if sid != expected_set(row.get("name", "")):
        return "INCOMPLETE", f"required_set {sid} 與列名對應的 {expected_set(row.get('name', ''))} 不符"
    if not l1_path:
        return "INCOMPLETE", "缺 l1_results"
    try:
        report = json.loads(_resolve(base, l1_path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return "INCOMPLETE", f"l1_results 不可讀：{exc}"
    platform = row.get("platform") or report.get("platform") or "linux"
    try:
        required = judge.required_for(lock or {}, sid, platform)
        selector = lock["sets"][sid][platform].get("selector")
    except (KeyError, TypeError):
        return "INCOMPLETE", f"鎖中無 {sid}.{platform}"
    verdict = judge.judge(report, required, platform=platform, selector=selector)
    if verdict["verdict"] == "FAIL":
        return "FAIL", "; ".join(verdict.get("reasons") or [f"failed={counts.get('failed')}"])[:300]
    if verdict["verdict"] != "PASS":
        detail = verdict.get("reasons") or []
        missing = verdict.get("missing") or []
        text = "; ".join(str(x) for x in detail)
        if missing:
            text = f"missing {len(missing)}；" + text
        return "INCOMPLETE", text[:300] or verdict["verdict"]
    agg = verdict.get("counts") or {}
    if counts and (counts.get("passed") != agg.get("passed") or counts.get("failed", 0) != agg.get("failed", 0)):
        return "INCOMPLETE", f"test_counts 與 l1 彙總不符：{counts} vs {agg}"
    if rc != 0:
        return "INCOMPLETE", f"rc={rc}"
    ev = _evidence_errors(base, row.get("evidence"), required=False)
    if ev:
        return "INCOMPLETE", "; ".join(ev)
    return "PASS", f"{sid} {len(required)} 筆"


def _row_status(base: Path, row: Mapping[str, Any], caps_missing: set[str], lock, judge) -> tuple[str, str]:
    name = row.get("name", "")
    cap = CAP_ROWS.get(name)
    if cap is not None and cap[0] in caps_missing:
        return "BLOCKED-OWNER", cap[1]
    if name in TEST_ROWS:
        return _test_row_status(base, row, lock, judge)
    if row.get("rc") != 0:
        return "FAIL", f"rc={row.get('rc')}"
    ev = _evidence_errors(base, row.get("evidence"), required=True)
    if ev:
        return "INCOMPLETE", "; ".join(ev)
    return "PASS", ""


def _owner_status(base: Path, owner_evidence: Mapping[str, Any], bid: str) -> tuple[str, str]:
    ev = owner_evidence.get(bid)
    if not ev:
        return "BLOCKED-OWNER", "證據未提供"
    errs = _evidence_errors(base, ev, required=True)
    if errs:
        return "BLOCKED-OWNER", "; ".join(errs)
    return "PASS", "擁有者證據已核對"


def build_report(results_dir: Path, *, l1_lock: Path | None = None) -> str:
    results_dir = Path(results_dir)
    data = json.loads((results_dir / "results.json").read_text(encoding="utf-8"))
    lock_path = Path(l1_lock) if l1_lock is not None else DEFAULT_L1_LOCK
    try:
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        lock = None
    judge = _load_judge()
    caps_missing = set(data.get("caps_missing") or [])
    rows = {r.get("name"): r for r in data.get("rows") or [] if isinstance(r, Mapping)}
    lines = [
        "# Slice 2.3（P7）出口 gate 報告",
        "",
        f"- run_id: `{data.get('run_id')}`",
        f"- git_head: `{data.get('git_head')}`",
        f"- worktree_clean: `{data.get('worktree_clean')}`",
        f"- caps_missing: `{sorted(caps_missing)}`",
        f"- l1_lock_sha256: `{_sha256(lock_path)}`",
        "",
        "## artifacts",
        "",
        "| artifact | sha256 |",
        "|---|---|",
    ]
    for name, digest in sorted((data.get("artifacts") or {}).items()):
        lines.append(f"| {name} | {digest} |")
    lines += ["", "## 列狀態", "", "| 列 | 狀態 | 說明 | log_sha256 |", "|---|---|---|---|"]
    statuses: dict[str, str] = {}
    names = list(REQUIRED_ROWS) + [n for n in rows if n not in REQUIRED_ROWS]
    for name in names:
        row = rows.get(name)
        if row is None:
            status, detail = "INCOMPLETE", "未執行"
            log_sha = ""
        else:
            status, detail = _row_status(results_dir, row, caps_missing, lock, judge)
            log_sha = row.get("log_sha256") or ""
        statuses[name] = status
        lines.append(f"| {name} | {status} | {detail.replace('|', '/')} | {log_sha} |")
    lines += ["", "## 擁有者項目", "", "| 項目 | 狀態 | 說明 |", "|---|---|---|"]
    owner_evidence = data.get("owner_evidence") or {}
    for bid, title in OWNER_ITEMS.items():
        status, detail = _owner_status(results_dir, owner_evidence, bid)
        lines.append(f"| {bid} | {status} | {title}；{detail} |")
    lines += ["", "## parity_amendments", "", "| 必要集合 | 平台 | nodeid 數 |", "|---|---|---|"]
    for sid in PARITY_SETS:
        for plat in ("linux", "windows"):
            n = len(((lock or {}).get("sets", {}).get(sid, {}).get(plat) or {}).get("nodeids") or [])
            if n:
                lines.append(f"| {sid} | {plat} | {n} |")
    ok = all(s in ("PASS", "BLOCKED-OWNER") for s in statuses.values())
    lines += ["", f"結論：{'PASS（含 BLOCKED-OWNER）' if ok else 'NOT-PASS'}", ""]
    return "\n".join(lines)


def verify_report(md_path: Path, results_dir: Path, *, l1_lock: Path | None = None) -> list[str]:
    """重算報告並逐行比對；另核對 log sha256 與 results.json 的 git_head／run_id。回傳不一致清單。"""
    results_dir = Path(results_dir)
    problems: list[str] = []
    try:
        data = json.loads((results_dir / "results.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [f"results.json 不可讀：{exc}"]
    text = Path(md_path).read_text(encoding="utf-8")
    if f"run_id: `{data.get('run_id')}`" not in text:
        problems.append("run_id 不符")
    if f"git_head: `{data.get('git_head')}`" not in text:
        problems.append("git_head 不符")
    head = current_head()
    if not data.get("git_head") or data.get("git_head") != head:
        problems.append(f"results.json git_head {data.get('git_head')} 與目前 HEAD {head} 不符")
    names = {Path(p).name.removesuffix(".exe") for p in (data.get("artifacts") or {})}
    for need in REQUIRED_ARTIFACTS:
        if need not in names:
            problems.append(f"缺必要 artifact：{need}")
    for path, want in (data.get("artifacts") or {}).items():
        got = _sha256(_resolve(ROOT, path))
        if got is None or got != want:
            problems.append(f"artifact sha256 不符或缺檔：{path}")
    for row in data.get("rows") or []:
        lp, want = row.get("log_path"), row.get("log_sha256")
        if lp and want and _sha256(_resolve(results_dir, lp)) != want:
            problems.append(f"log sha256 不符：{row.get('name')}")
    expect = build_report(results_dir, l1_lock=l1_lock).splitlines()
    got = text.splitlines()
    if expect != got:
        diff = [f"- {e!r} / + {g!r}" for e, g in zip(expect, got) if e != g][:10]
        problems.append("列狀態重算不符：" + "; ".join(diff) if diff else "行數不符")
    return problems


def _rel_to(base: Path, p: Path) -> str:
    try:
        return str(Path(p).resolve().relative_to(Path(base).resolve()))
    except ValueError:
        return str(p)


def init_results(results_dir: Path, run_id: str, *, caps_missing: list[str], artifacts: list[Path]) -> dict[str, Any]:
    """`p7_gate.sh` 起點：寫 results.json 的身分欄位（HEAD、工作樹是否乾淨、artifact sha256，路徑為 repo 相對）。"""
    status = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True)
    data = {
        "run_id": run_id,
        "git_head": current_head(),
        "worktree_clean": status.returncode == 0 and not status.stdout.strip(),
        "caps_missing": sorted(set(caps_missing)),
        "artifacts": {_rel_to(ROOT, a): _sha256(a) for a in artifacts},
        "rows": [],
        "owner_evidence": {},
    }
    Path(results_dir).mkdir(parents=True, exist_ok=True)
    (Path(results_dir) / "results.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return data


def record_row(results_dir: Path, *, name: str, rc: int | None, log: Path, argv: list[str], started_at: str,
               ended_at: str, l1_results: Path | None = None, platform: str = "linux",
               evidence: list[Path] = ()) -> dict[str, Any]:
    """只記錄、不判定：同名列覆寫；測試列附 `required_set`（列名對應集合）與 l1 的測試計數。"""
    results_dir = Path(results_dir)
    path = results_dir / "results.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    row: dict[str, Any] = {
        "name": name, "argv": argv, "rc": rc, "started_at": started_at, "ended_at": ended_at,
        "log_path": _rel_to(results_dir, log), "log_sha256": _sha256(Path(log)), "platform": platform,
        "evidence": [{"path": _rel_to(results_dir, e), "sha256": _sha256(Path(e))} for e in evidence],
    }
    if name in TEST_ROWS:
        row["required_set"] = expected_set(name)
        counts = {"passed": 0, "failed": 0, "ignored": 0}
        if l1_results is not None and Path(l1_results).is_file():
            row["l1_results"] = _rel_to(results_dir, l1_results)
            try:
                rep = json.loads(Path(l1_results).read_text(encoding="utf-8"))
            except ValueError:
                rep = {}
            for r in rep.get("results") or []:
                o = r.get("outcome")
                key = "passed" if o == "passed" else "failed" if o in ("failed", "error") else "ignored"
                counts[key] += 1
        row["test_counts"] = counts
    data["rows"] = [r for r in data.get("rows") or [] if r.get("name") != name] + [row]
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return row


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("init")
    i.add_argument("--results-dir", required=True, type=Path)
    i.add_argument("--run-id", required=True)
    i.add_argument("--caps-missing", nargs="*", default=[])
    i.add_argument("--artifact", nargs="*", default=[], type=Path)
    r = sub.add_parser("record")
    r.add_argument("--results-dir", required=True, type=Path)
    r.add_argument("--name", required=True)
    r.add_argument("--rc", type=int)
    r.add_argument("--log", required=True, type=Path)
    r.add_argument("--argv-json", required=True)
    r.add_argument("--started-at", required=True)
    r.add_argument("--ended-at", required=True)
    r.add_argument("--l1-results", type=Path)
    r.add_argument("--platform", default="linux")
    r.add_argument("--evidence", nargs="*", default=[], type=Path)
    b = sub.add_parser("build")
    b.add_argument("--results-dir", required=True, type=Path)
    b.add_argument("--l1-lock", type=Path)
    b.add_argument("--out", type=Path)
    v = sub.add_parser("verify")
    v.add_argument("--results-dir", required=True, type=Path)
    v.add_argument("--md", required=True, type=Path)
    v.add_argument("--l1-lock", type=Path)
    args = ap.parse_args(argv)
    if args.cmd == "init":
        init_results(args.results_dir, args.run_id, caps_missing=args.caps_missing, artifacts=args.artifact)
        return 0
    if args.cmd == "record":
        record_row(args.results_dir, name=args.name, rc=args.rc, log=args.log, argv=json.loads(args.argv_json),
                   started_at=args.started_at, ended_at=args.ended_at, l1_results=args.l1_results,
                   platform=args.platform, evidence=args.evidence)
        return 0
    if args.cmd == "build":
        md = build_report(args.results_dir, l1_lock=args.l1_lock)
        if args.out:
            args.out.write_text(md, encoding="utf-8", newline="\n")
        else:
            sys.stdout.write(md)
        return 0
    problems = verify_report(args.md, args.results_dir, l1_lock=args.l1_lock)
    for p in problems:
        print(p, file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
