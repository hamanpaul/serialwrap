#!/usr/bin/env python3
"""P4 必要案例集合的凍結與完成度檢查（計畫 Task 1 §0.4、§11.3）。

- `--freeze`：凍結 `tests/acceptance/p4_case_map.yaml` 的必要案例集合，寫入 `tests/acceptance/p4_required.lock.json`。
  只允許在 lock 不存在時執行一次；既有 lock 一律拒絕覆寫。
- `--freeze --add-only`：既有 lock 只能「追加」新的必要案例。既有案例若消失、降級為非必要、凍結欄位變動、
  承接節點被移除或 `error_codes` 變動，一律 exit 1。
- `--check`：對**目前 map 中所有必要案例**（不只 lock 內者）檢查：
  1. 凍結欄位（`FROZEN_FIELDS`）的 sha256 與 lock 相符；凍結時的承接節點集合 ⊆ 現在的節點集合；
  2. 只綁 `p4_case_placeholders` 占位節點的案例不得作為證據；
  3. 證據檔 `target/p4-evidence/<case_id>.json` 存在，且 `records` 每筆含 `case_id`、`round`、`platform`、`commit`、
     `artifact_sha256`、`result`；
  4. 只有 `commit == 受測 HEAD` 的紀錄會計入；`FAIL` → 失敗；`SKIP` 僅在 round 非 `rust` 且 round 或 platform 已於
     `skip_registered` 登記時接受，其餘（含 rust 輪 SKIP）一律失敗；
  5. 案例每個 `platforms` 項目都需要一筆 `round=rust`、`result=PASS`、受測 HEAD、且 `artifact_path` 指向的實際檔案
     sha256 與 `artifact_sha256` 相符的 PASS 紀錄；
  6. `contracts/error_codes.yaml` 的碼集合與凍結時相同（本 Phase 不得新增錯誤碼）。
  任一項失敗 → exit 1（「證據不足不得轉為 PASS」，不變量 `insufficient_evidence_not_pass`）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CASE_MAP = REPO_ROOT / "tests" / "acceptance" / "p4_case_map.yaml"
LOCK = REPO_ROOT / "tests" / "acceptance" / "p4_required.lock.json"
ERROR_CODES = REPO_ROOT / "contracts" / "error_codes.yaml"
EVIDENCE_DIR = REPO_ROOT / "target" / "p4-evidence"

ROUNDS = ("rust", "python")
PLATFORMS = ("linux", "windows")
RESULTS = ("PASS", "FAIL", "SKIP")
NECESSITIES = ("required", "optional")
# 凍結欄位：這些欄位變動即視為案例被改寫（tests 另以「凍結節點 ⊆ 現節點」檢查）。
FROZEN_FIELDS = ("case_id", "necessity", "invariants", "layer", "platforms", "capabilities", "artifact", "skip_registered")
RECORD_KEYS = ("case_id", "round", "platform", "commit", "artifact_sha256", "result")
PLACEHOLDER_PREFIX = "sw-cli::p4_case_placeholders::"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _canonical(obj: object) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_cases() -> list[dict[str, Any]]:
    data = yaml.safe_load(CASE_MAP.read_text(encoding="utf-8")) or {}
    return list(data.get("cases", []))


def error_codes() -> list[str]:
    data = yaml.safe_load(ERROR_CODES.read_text(encoding="utf-8")) or {}
    return sorted(entry["code"] for entry in data.get("codes", []) if isinstance(entry, dict) and "code" in entry)


def head_commit() -> str:
    res = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True, check=False)
    return res.stdout.strip() or "unknown"


def frozen_digest(case: dict[str, Any]) -> str:
    return _sha256_text(_canonical({key: case.get(key) for key in FROZEN_FIELDS}))


def validate_map(cases: list[dict[str, Any]]) -> list[str]:
    """映射表自身的結構檢查（與 schema 互補，供 --freeze 與 --check 共用）。"""
    problems: list[str] = []
    seen: set[str] = set()
    for case in cases:
        cid = str(case.get("case_id", "<缺 case_id>"))
        if cid in seen:
            problems.append(f"{cid}: case_id 重複")
        seen.add(cid)
        if case.get("necessity") not in NECESSITIES:
            problems.append(f"{cid}: necessity 必須為 {NECESSITIES}")
        for plat in case.get("platforms", []):
            if plat not in PLATFORMS:
                problems.append(f"{cid}: 未知平台 {plat!r}")
        if not case.get("platforms"):
            problems.append(f"{cid}: platforms 不得為空")
        if not case.get("tests"):
            problems.append(f"{cid}: tests 不得為空")
        for key in (case.get("skip_registered") or {}):
            if key not in ROUNDS and key not in PLATFORMS:
                problems.append(f"{cid}: skip_registered 鍵 {key!r} 必須是 round 或 platform")
    return problems


def _entry(case: dict[str, Any]) -> dict[str, Any]:
    return {
        "case_id": case["case_id"],
        "sha256": frozen_digest(case),
        "tests": sorted(case.get("tests", [])),
    }


def _write_lock(lock: dict[str, Any]) -> None:
    LOCK.write_text(json.dumps(lock, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _set_digest(entries: list[dict[str, Any]]) -> str:
    return _sha256_text(_canonical([[e["case_id"], e["sha256"]] for e in entries]))


def freeze(add_only: bool) -> int:
    cases = load_cases()
    problems = validate_map(cases)
    if problems:
        print("\n".join(problems), file=sys.stderr)
        print("映射表結構錯誤，拒絕凍結", file=sys.stderr)
        return 1
    required = [c for c in cases if c.get("necessity") == "required"]
    if not required:
        print("沒有必要案例，拒絕凍結", file=sys.stderr)
        return 1
    codes = error_codes()

    if not LOCK.exists():
        if add_only:
            print("--add-only 需要既有 lock；請先執行 --freeze", file=sys.stderr)
            return 1
        entries = [_entry(c) for c in sorted(required, key=lambda c: c["case_id"])]
        _write_lock(
            {
                "schema_version": 2,
                "frozen_commit": head_commit(),
                "amended_commits": [],
                "case_set_sha256": _set_digest(entries),
                "cases": entries,
                "error_codes_at_freeze": codes,
            }
        )
        print(f"凍結 {len(entries)} 筆必要案例 → {LOCK}")
        return 0

    if not add_only:
        print(
            f"{LOCK} 已存在，拒絕覆寫（凍結只能一次）；新增必要案例請使用 --freeze --add-only",
            file=sys.stderr,
        )
        return 1

    lock = json.loads(LOCK.read_text(encoding="utf-8"))
    current = {c["case_id"]: c for c in cases}
    problems = []
    for entry in lock.get("cases", []):
        cid = entry["case_id"]
        case = current.get(cid)
        if case is None:
            problems.append(f"{cid}: 既有凍結案例已被刪除")
            continue
        if case.get("necessity") != "required":
            problems.append(f"{cid}: 既有凍結案例不得降級為非必要")
        if frozen_digest(case) != entry["sha256"]:
            problems.append(f"{cid}: 凍結欄位變動")
        gone = sorted(set(entry.get("tests", [])) - set(case.get("tests", [])))
        if gone:
            problems.append(f"{cid}: 凍結時的承接節點被移除：{gone}")
    if sorted(codes) != sorted(lock.get("error_codes_at_freeze", [])):
        problems.append("error_codes 與凍結時不同（本 Phase 不得新增錯誤碼）")
    if problems:
        print("\n".join(problems), file=sys.stderr)
        print("--add-only 拒絕：既有凍結內容不可被刪除或改寫", file=sys.stderr)
        return 1

    existing = {e["case_id"] for e in lock.get("cases", [])}
    added = [_entry(c) for c in sorted(required, key=lambda c: c["case_id"]) if c["case_id"] not in existing]
    entries = sorted(lock.get("cases", []) + added, key=lambda e: e["case_id"])
    lock["cases"] = entries
    lock["case_set_sha256"] = _set_digest(entries)
    head = head_commit()
    if head not in lock.get("amended_commits", []):
        lock.setdefault("amended_commits", []).append(head)
    _write_lock(lock)
    print(f"追加 {len(added)} 筆必要案例 → {LOCK}（凍結 commit 維持 {lock.get('frozen_commit')}）")
    return 0


def _artifact_problem(record: dict[str, Any]) -> str | None:
    raw = str(record.get("artifact_path") or "")
    if not raw:
        return "PASS 紀錄缺 artifact_path"
    path = Path(raw)
    if not path.is_absolute():
        path = REPO_ROOT / path
    if not path.is_file():
        return f"artifact 不存在：{path}"
    actual = _sha256_bytes(path.read_bytes())
    if actual != record["artifact_sha256"]:
        return f"artifact_sha256 與實際檔案不符（實際 {actual}）"
    return None


def _record_problem(record: Any, case_id: str) -> str | None:
    if not isinstance(record, dict):
        return "紀錄不是物件"
    missing = [key for key in RECORD_KEYS if key not in record]
    if missing:
        return f"缺少欄位 {missing}"
    if record["case_id"] != case_id:
        return "紀錄 case_id 與檔名不符"
    if record["round"] not in ROUNDS:
        return f"round 必須為 {ROUNDS}"
    if record["platform"] not in PLATFORMS:
        return f"platform 必須為 {PLATFORMS}"
    if record["result"] not in RESULTS:
        return f"result 必須為 {RESULTS}"
    if not isinstance(record["commit"], str) or not record["commit"]:
        return "commit 不得為空"
    if record["result"] == "PASS":
        sha = record["artifact_sha256"]
        if not isinstance(sha, str) or not SHA256_RE.match(sha):
            return "PASS 紀錄的 artifact_sha256 必須為 64 位小寫 hex"
    if record["result"] == "SKIP" and not str(record.get("reason") or "").strip():
        return "SKIP 紀錄必須附 reason"
    return None


def check_case_evidence(case: dict[str, Any], head: str, evidence_dir: Path) -> list[str]:
    cid = case["case_id"]
    problems: list[str] = []
    if all(str(t).startswith(PLACEHOLDER_PREFIX) for t in case.get("tests", [])):
        problems.append(f"{cid}: 只綁占位節點（{PLACEHOLDER_PREFIX}*），不得作為證據")
    path = evidence_dir / f"{cid}.json"
    if not path.exists():
        return problems + [f"{cid}: 缺少證據檔 {path}"]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return problems + [f"{cid}: 證據檔無法解析：{exc}"]
    records = data.get("records") if isinstance(data, dict) else None
    if not isinstance(records, list):
        return problems + [f"{cid}: 證據檔須含 records 陣列"]

    skips = case.get("skip_registered") or {}
    stale = 0
    current: list[dict[str, Any]] = []
    for idx, record in enumerate(records):
        bad = _record_problem(record, cid)
        if bad:
            problems.append(f"{cid}#{idx}: {bad}")
            continue
        if record["commit"] != head:
            stale += 1
            continue
        if record["result"] == "FAIL":
            problems.append(f"{cid}: {record['platform']}／{record['round']} 證據 FAIL")
            continue
        if record["result"] == "SKIP":
            if record["round"] == "rust":
                problems.append(f"{cid}: rust 輪 SKIP 不得放行（必要案例 SKIP 必為 0）")
            elif record["round"] not in skips and record["platform"] not in skips:
                problems.append(f"{cid}: 未登記的 SKIP（{record['platform']}／{record['round']}）")
            continue
        artifact_bad = _artifact_problem(record)
        if artifact_bad:
            problems.append(f"{cid}: {record['platform']}／{record['round']} {artifact_bad}")
            continue
        current.append(record)

    for plat in case.get("platforms", []):
        ok = [r for r in current if r["round"] == "rust" and r["platform"] == plat and r["result"] == "PASS"]
        if not ok:
            detail = f"（另有 {stale} 筆證據 commit 非受測 HEAD，不計入）" if stale else ""
            problems.append(f"{cid}: 缺少 {plat} 的 rust 輪 PASS（受測 HEAD、artifact 已驗）{detail}")
    return problems


def check() -> int:
    if not LOCK.exists():
        print("缺少 p4_required.lock.json（須於紅燈 commit 前執行 --freeze）", file=sys.stderr)
        return 1
    lock = json.loads(LOCK.read_text(encoding="utf-8"))
    cases = load_cases()
    failures = validate_map(cases)
    current = {c["case_id"]: c for c in cases}
    head = head_commit()
    if head == "unknown":
        failures.append("無法取得受測 HEAD commit")

    frozen_ids = set()
    for entry in lock.get("cases", []):
        cid = entry["case_id"]
        frozen_ids.add(cid)
        case = current.get(cid)
        if case is None:
            failures.append(f"{cid}: 凍結集合中的案例已被刪除")
            continue
        if case.get("necessity") != "required":
            failures.append(f"{cid}: 凍結案例不得改為非必要")
        if frozen_digest(case) != entry["sha256"]:
            failures.append(f"{cid}: 凍結欄位與凍結時不同")
        gone = sorted(set(entry.get("tests", [])) - set(case.get("tests", [])))
        if gone:
            failures.append(f"{cid}: 凍結時的承接節點已不存在於映射表：{gone}")

    for cid, case in current.items():
        if case.get("necessity") != "required":
            continue
        if cid not in frozen_ids:
            failures.append(f"{cid}: 必要案例未凍結（請以 --freeze --add-only 登記）")
        failures.extend(check_case_evidence(case, head, EVIDENCE_DIR))

    if sorted(error_codes()) != sorted(lock.get("error_codes_at_freeze", [])):
        failures.append("error_codes 與凍結時不同（本 Phase 不得新增錯誤碼）")

    for line in failures:
        print(line, file=sys.stderr)
    if failures:
        print(f"FAIL：{len(failures)} 項", file=sys.stderr)
        return 1
    print(f"PASS：{len(frozen_ids)} 筆凍結案例與目前全部必要案例的證據齊全")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="P4 必要案例凍結與完成度檢查")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--freeze", action="store_true", help="寫入 p4_required.lock.json（既有 lock 不得覆寫）")
    group.add_argument("--check", action="store_true", help="檢查必要案例的證據與碼集合")
    parser.add_argument("--add-only", action="store_true", help="與 --freeze 併用：只追加新的必要案例")
    args = parser.parse_args()
    if args.add_only and not args.freeze:
        parser.error("--add-only 只能與 --freeze 併用")
    return freeze(add_only=args.add_only) if args.freeze else check()


if __name__ == "__main__":
    sys.exit(main())
