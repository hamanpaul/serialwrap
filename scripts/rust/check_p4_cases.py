#!/usr/bin/env python3
"""P4 必要案例集合的凍結與完成度檢查（計畫 Task 1 §0.4、§11.3）。

- `--freeze`：凍結 `tests/acceptance/p4_case_map.yaml` 的必要案例集合，寫入 `tests/acceptance/p4_required.lock.json`。
  只允許在 lock 不存在時執行一次；既有 lock 一律拒絕覆寫。
- `--freeze --add-only`：既有 lock 只能「追加」新的必要案例。既有案例若消失、降級為非必要、凍結欄位變動或承接節點被移除，一律 exit 1；
  `error_codes` 新增只可用 `--allow-error-code-addition CODE` 逐碼核准，既有凍結碼不得移除。占位節點（`sw-cli::p4_case_placeholders::`）不計入「承接節點被移除」。
- `--check`：對**目前 map 中所有必要案例**（不只 lock 內者）檢查：
  1. 凍結欄位（`FROZEN_FIELDS`）的 sha256 與 lock 相符；凍結時的承接節點集合（排除占位節點）⊆ 現在的節點集合；
  2. 只綁 `p4_case_placeholders` 占位節點的案例不得作為證據；
  3. 證據檔 `<evidence-dir>/<case_id>.json` 存在，且 `records` 每筆含 `case_id`、`round`、`platform`、`commit`、
     `artifact_sha256`、`result`；
  4. 只有 `commit == 受測 HEAD` 的紀錄會計入；`FAIL` → 失敗；`SKIP` 僅在 round 非 `rust` 且 round 或 platform 已於
     `skip_registered` 登記時接受，其餘（含 rust 輪 SKIP）一律失敗；
  5. 案例每個 `platforms` 項目都需要一筆 `round=rust`、`result=PASS`、受測 HEAD、且 `artifact_path` 指向的實際檔案
     sha256 與 `artifact_sha256` 相符的 PASS 紀錄；
  6. `contracts/error_codes.yaml` 的碼集合與凍結時相同（本 Phase 不得新增錯誤碼）；
  7. 證據層級（OD-15）：L2／L3 案例的每筆證據必須以 P2 `evidence_labels.validate_report_layer` 取得正向 `evidence_layer`
     （缺標即拒收），並以 `assert_not_real_board` 拒絕 `L1.5-docker`／`host-throwaway`（`not_real_board`）；
  8. L1 完整性（R5-F3）：`--l1-results <dir>`（預設 `<evidence-dir>/l1`）；對 `s1b_20groups`、`s1b_cargo` 的每個平台，
     **唯一判定實作為 P2 `tests/acceptance/l1_judge.py`**（依路徑以 importlib 載入，本檔不另寫判定規則）；
     verdict 非 `PASS` 一律失敗。P2 模組缺席即 `BLOCKED-PREREQ`（L1 證據不足，exit 1），不得放行。
  9. S1X 擁有者機台相依列（`owner_gated.py`）：`s1b_cargo` 的 cargo 報告中 `cap_*`／登記列為 skipped → BLOCKED-OWNER（列出，exit 2 類，不因不在鎖內而消失）；登記列於 selector 涵蓋範圍內缺席 → INSUFFICIENT。
- 平台適用性（G4）：受測 HEAD 且帶 `test_id` 的紀錄，若節點屬 cargo／contract_blackbox pytest 範圍，須其 platform
  在 `platform_applicability` 算出的適用集合內（cargo 依 s1b_cargo 鎖；pytest 依該平台 s1b_20groups selector 的 markexpr 收集）；
  不適用或兩平台皆不適用者不計入／FAIL。與 register_p4_evidence、ab_blackbox_1b 共用同一實作。
- 判定結果：任一硬性失敗 → exit 1（「證據不足不得轉為 PASS」，不變量 `insufficient_evidence_not_pass`）。
  若唯一的缺口是 `evidence_status: BLOCKED-OWNER` 的 L2／L3／manual 案例證據（擁有者機台待補） → exit 2（BLOCKED-OWNER）。
  全部通過 → exit 0。失敗列同時輸出到 stdout 與 stderr 的摘要行。
- `--check` 同時寫 `<evidence-dir>/summary.json`：逐案的 證據檔、受測 HEAD commit、通過紀錄的 `artifact_sha256`；
  L1 段為各 set／平台的 P2 判定結果與理由。
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from types import ModuleType
from typing import Any

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import owner_gated  # noqa: E402  擁有者機台相依 cargo 列的共用登記（S1X）

REPO_ROOT = Path(__file__).resolve().parents[2]
CASE_MAP = REPO_ROOT / "tests" / "acceptance" / "p4_case_map.yaml"
LOCK = REPO_ROOT / "tests" / "acceptance" / "p4_required.lock.json"
ERROR_CODES = REPO_ROOT / "contracts" / "error_codes.yaml"
EVIDENCE_DIR = REPO_ROOT / "target" / "p4-evidence"
L1_LOCK = REPO_ROOT / "tests" / "acceptance" / "l1_required.lock.json"
L1_JUDGE = REPO_ROOT / "tests" / "acceptance" / "l1_judge.py"
EVIDENCE_LABELS = REPO_ROOT / "tests" / "acceptance" / "evidence_labels.py"
# P4 必須完整判定的 L1 集合（R5-F3）→ 報告子目錄名稱。
P4_L1_SETS = {"s1b_20groups": "20groups", "s1b_cargo": "cargo"}
# cargo 集合的報告無 junit 交叉核對（`cargo_l1_results.py` 產生）。
JUNIT_OPTIONAL_SETS = frozenset({"s1b_cargo"})
ROUNDS = ("rust", "python")
PLATFORMS = ("linux", "windows")
RESULTS = ("PASS", "FAIL", "SKIP")
NECESSITIES = ("required", "optional")
BLOCKED_OWNER = "BLOCKED-OWNER"
BLOCKED_OWNER_LAYERS = ("L2", "L3", "manual")
REAL_BOARD_LAYERS = ("L2", "L3")
DOCKER_EVIDENCE_LAYERS = ("L1.5-docker", "host-throwaway")
# 凍結欄位：這些欄位變動即視為案例被改寫（evidence_status 為狀態欄，不凍結）。
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


def _load_module(name: str, path: Path) -> ModuleType | None:
    """依路徑載入 P2 模組；檔案缺席回傳 None（呼叫端轉為 BLOCKED-PREREQ，fail-closed）。"""
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_l1_judge() -> ModuleType | None:
    return _load_module("l1_judge", L1_JUDGE)


def load_evidence_labels() -> ModuleType | None:
    return _load_module("evidence_labels", EVIDENCE_LABELS)


def is_placeholder(node: str) -> bool:
    return str(node).startswith(PLACEHOLDER_PREFIX)


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
        status = case.get("evidence_status")
        if status is not None:
            if status != BLOCKED_OWNER:
                problems.append(f"{cid}: evidence_status 只允許 {BLOCKED_OWNER!r}")
            if case.get("layer") not in BLOCKED_OWNER_LAYERS:
                problems.append(f"{cid}: BLOCKED-OWNER 只適用於 layer ∈ {BLOCKED_OWNER_LAYERS}")
    return problems


def _entry(case: dict[str, Any]) -> dict[str, Any]:
    return {
        "case_id": case["case_id"],
        "sha256": frozen_digest(case),
        "tests": sorted(case.get("tests", [])),
    }


def _gone_nodes(entry: dict[str, Any], case: dict[str, Any]) -> list[str]:
    """凍結時的承接節點中、現映射表已不存在者（占位節點不計，其已由真測試取代）。"""
    return sorted(
        node
        for node in set(entry.get("tests", [])) - set(case.get("tests", []))
        if not is_placeholder(node)
    )


def _write_lock(lock: dict[str, Any]) -> None:
    LOCK.write_text(json.dumps(lock, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _set_digest(entries: list[dict[str, Any]]) -> str:
    return _sha256_text(_canonical([[e["case_id"], e["sha256"]] for e in entries]))


def freeze(add_only: bool, allowed_error_additions: list[str] | None = None) -> int:
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
        gone = _gone_nodes(entry, case)
        if gone:
            problems.append(f"{cid}: 凍結時的承接節點被移除：{gone}")
    frozen_codes = set(lock.get("error_codes_at_freeze", []))
    current_codes = set(codes)
    allowed_codes = set(allowed_error_additions or [])
    removed_codes = sorted(frozen_codes - current_codes)
    added_codes = sorted(current_codes - frozen_codes)
    unapproved_codes = sorted(set(added_codes) - allowed_codes)
    unused_allowlist = sorted(allowed_codes - set(added_codes))
    if removed_codes:
        problems.append(f"error_codes 移除了凍結碼（不允許）：{removed_codes}")
    if unapproved_codes or unused_allowlist:
        problems.append(
            "error_codes 凍結基線增補清單與差異不符："
            f"未核准新增={unapproved_codes}、未使用核准={unused_allowlist}"
        )
    if problems:
        print("\n".join(problems), file=sys.stderr)
        print("--add-only 拒絕：既有凍結內容不可被刪除或改寫", file=sys.stderr)
        return 1

    existing = {e["case_id"] for e in lock.get("cases", [])}
    added = [_entry(c) for c in sorted(required, key=lambda c: c["case_id"]) if c["case_id"] not in existing]
    entries = sorted(lock.get("cases", []) + added, key=lambda e: e["case_id"])
    lock["cases"] = entries
    lock["case_set_sha256"] = _set_digest(entries)
    lock["error_codes_at_freeze"] = sorted(current_codes)
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


def _layer_problem(labels: ModuleType | None, record: dict[str, Any], case_layer: str | None) -> str | None:
    """L2／L3 證據層級判定（P2 evidence_labels）；缺模組或缺標皆拒收（fail-closed）。"""
    if case_layer not in REAL_BOARD_LAYERS and case_layer != "manual":
        return None
    layer = record.get("evidence_layer")
    if layer in DOCKER_EVIDENCE_LAYERS:
        return f"not_real_board（{layer} 證據不得計入 {case_layer} 真板案例，OD-15）"
    if labels is None:
        return "BLOCKED-PREREQ：缺 tests/acceptance/evidence_labels.py（需併入 feature/rust-refactor），L2／L3 證據無法判定"
    required = {"L2": "L2-bench", "L3": "L3-bench", "manual": "hw-bench"}[case_layer]
    try:
        labels.validate_report_layer(record)
        labels.assert_not_real_board(record, required)
    except (ValueError, AssertionError) as exc:
        return f"evidence_layer 判定失敗（{exc}）"
    if not labels.layer_satisfies(str(layer), required):
        return f"evidence_layer {layer!r} 不滿足 {case_layer} 案例（需 {required}）"
    return None


# ── P4 平台適用性規則（Stage 1 收尾 G4-(d)(e)）─────────────────────────────────────────────────────
# register_p4_evidence、check_p4_cases、ab_blackbox_1b 共用本節同一條規則（不得各自實作）：
# - cargo 節點（`sw-…::…`）：該平台 s1b_cargo 凍結鎖的 nodeids 含 `cargo:<節點>`。
# - contract_blackbox pytest 節點（`tests/contract_blackbox/…::…`）：該平台 s1b_20groups 凍結 selector 的 markexpr
#   收集得到該節點（pytest --collect-only -m <markexpr>，與凍結 selector 同一收集語意）。
# 不適用者不產生記錄、也不在該平台執行；每個 case.platforms 平台至少要有一筆適用且 PASS 的記錄；
# 兩個平台都不適用的節點一律 FAIL。不屬於上述兩類的節點（例如 tests/ 下的單元節點）不在本規則內。
CARGO_SELECTOR_SET = "s1b_cargo"
PYTEST_SELECTOR_SET = "s1b_20groups"
CARGO_LOCK_PREFIX = "cargo:"
WINDOWS_TARGET_TRIPLE = "x86_64-pc-windows-gnu"
# pytest 收集結果為「沒有任何測試被收集」（全數被 -m 取消選取）時的退出碼。
PYTEST_NO_TESTS_COLLECTED = 5


def applicability_rule_applies(node: str) -> bool:
    text = str(node)
    if text.startswith("sw-") and "::" in text:
        return True
    return text.startswith("tests/contract_blackbox/") and "::" in text


def _rule_node_id(test_id: Any) -> str | None:
    """紀錄的 test_id → 適用性規則的節點名（cargo 紀錄帶 `cargo:` 前綴）；不在規則範圍者回 None。"""
    if not isinstance(test_id, str):
        return None
    node = test_id.removeprefix(CARGO_LOCK_PREFIX)
    return node if applicability_rule_applies(node) else None


def load_l1_lock(path: Path | None = None) -> dict[str, Any]:
    lock_path = L1_LOCK if path is None else path
    return json.loads(lock_path.read_text(encoding="utf-8"))


def pytest_markexpr(lock: dict[str, Any], platform: str) -> str:
    return str(lock["sets"][PYTEST_SELECTOR_SET][platform]["selector"]["markexpr"])


def cargo_selector_argv(lock: dict[str, Any], platform: str) -> list[str]:
    return [str(item) for item in lock["sets"][CARGO_SELECTOR_SET][platform]["selector"]["argv"]]


def pytest_collected(nodes: list[str], markexpr: str, cwd: Path) -> set[str]:
    """以 `pytest --collect-only -m <markexpr>` 取得被凍結 selector 收集的節點（逐字 nodeid）。"""
    argv = [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", "-m", markexpr, *nodes]
    res = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, check=False)
    if res.returncode not in (0, PYTEST_NO_TESTS_COLLECTED):
        raise RuntimeError(f"pytest --collect-only 失敗（rc={res.returncode}）：{(res.stderr or res.stdout)[-400:]}")
    return {line.strip() for line in res.stdout.splitlines() if "::" in line}


def platform_applicability(
    nodes: list[str],
    *,
    cwd: Path,
    lock: dict[str, Any] | None = None,
    platforms: tuple[str, ...] = PLATFORMS,
) -> dict[str, frozenset[str]]:
    """每個節點可執行的平台集合（依上述共用規則）。來源檔不存在的 pytest 節點視為未被收集（不適用）。"""
    lock_data = load_l1_lock() if lock is None else lock
    unsupported = sorted(node for node in nodes if not applicability_rule_applies(node))
    if unsupported:
        raise ValueError(f"節點不在平台適用性規則內：{unsupported}")
    result: dict[str, set[str]] = {str(node): set() for node in nodes}
    cargo_nodes = [node for node in result if str(node).startswith("sw-")]
    pytest_nodes = [
        node for node in result
        if not str(node).startswith("sw-") and (cwd / str(node).split("::", 1)[0]).is_file()
    ]
    for platform in platforms:
        frozen = set(lock_data["sets"][CARGO_SELECTOR_SET][platform]["nodeids"])
        for node in cargo_nodes:
            if f"{CARGO_LOCK_PREFIX}{node}" in frozen:
                result[node].add(platform)
        if pytest_nodes:
            collected = pytest_collected(pytest_nodes, pytest_markexpr(lock_data, platform), cwd)
            for node in pytest_nodes:
                if node in collected:
                    result[node].add(platform)
    return {node: frozenset(values) for node, values in result.items()}


def check_case_evidence(
    case: dict[str, Any],
    head: str,
    evidence_dir: Path,
    labels: ModuleType | None,
    applicability: dict[str, frozenset[str]] | None = None,
) -> tuple[list[str], list[dict[str, Any]], str]:
    """回傳（問題列, 通過紀錄, 證據檔路徑字串）。

    `applicability`：節點 → 適用平台集合（見 `platform_applicability`）。受測 HEAD 的紀錄若帶 `test_id` 且屬適用性規則
    範圍，須該紀錄的 platform 在適用集合內；適用集合為空（兩平台皆不在凍結 selector）即 FAIL。
    """
    cid = case["case_id"]
    problems: list[str] = []
    if all(is_placeholder(t) for t in case.get("tests", [])):
        problems.append(f"{cid}: 只綁占位節點（{PLACEHOLDER_PREFIX}*），不得作為證據")
    path = evidence_dir / f"{cid}.json"
    if not path.exists():
        return problems + [f"{cid}: 缺少證據檔 {path}"], [], str(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return problems + [f"{cid}: 證據檔無法解析：{exc}"], [], str(path)
    records = data.get("records") if isinstance(data, dict) else None
    if not isinstance(records, list):
        return problems + [f"{cid}: 證據檔須含 records 陣列"], [], str(path)

    skips = case.get("skip_registered") or {}
    stale = 0
    passing: list[dict[str, Any]] = []
    top_layer = data.get("evidence_layer")
    for idx, record in enumerate(records):
        if isinstance(record, dict) and "evidence_layer" not in record and top_layer is not None:
            record = {**record, "evidence_layer": top_layer}
        layer_bad = _layer_problem(labels, record if isinstance(record, dict) else {}, case.get("layer"))
        if layer_bad:
            problems.append(f"{cid}#{idx}: {layer_bad}")
            continue
        bad = _record_problem(record, cid)
        if bad:
            problems.append(f"{cid}#{idx}: {bad}")
            continue
        if record["commit"] != head:
            stale += 1
            continue
        node_id = _rule_node_id(record.get("test_id"))
        if applicability is not None and node_id is not None and node_id in applicability:
            platforms_for_node = applicability[node_id]
            if not platforms_for_node:
                problems.append(f"{cid}#{idx}: 節點 {node_id} 不在 linux／windows 任一凍結 selector 中，一律 FAIL")
                continue
            if record["platform"] not in platforms_for_node:
                problems.append(
                    f"{cid}#{idx}: 節點 {node_id} 不適用於 {record['platform']}"
                    f"（僅適用 {sorted(platforms_for_node)}），不計入"
                )
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
        passing.append(record)

    for plat in case.get("platforms", []):
        ok = [r for r in passing if r["round"] == "rust" and r["platform"] == plat and r["result"] == "PASS"]
        if not ok:
            detail = f"（另有 {stale} 筆證據 commit 非受測 HEAD，不計入）" if stale else ""
            problems.append(f"{cid}: 缺少 {plat} 的 rust 輪 PASS（受測 HEAD、artifact 已驗）{detail}")
    return problems, passing, str(path)


def _junit_counts(path: Path) -> dict[str, int] | None:
    """讀 pytest junit.xml 根 testsuite 的 tests／failures／errors／skipped（與報告交叉核對用）。"""
    if not path.is_file():
        return None
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError:
        return None
    suite = root if root.tag == "testsuite" else root.find("testsuite")
    if suite is None:
        return None
    return {key: int(suite.get(key, "0")) for key in ("tests", "failures", "errors", "skipped")}


def check_l1(
    l1_dir: Path | None = None,
    lock_path: Path | None = None,
    judge: ModuleType | None = None,
) -> list[dict[str, Any]]:
    """R5-F3：對 `P4_L1_SETS` 的每個平台呼叫 P2 `l1_judge.judge`；回傳逐列判定（verdict／reasons）。

    判定實作唯一來源為 `tests/acceptance/l1_judge.py`；本函式只負責定位報告與 junit、組裝參數。
    """
    l1_dir = (EVIDENCE_DIR / "l1") if l1_dir is None else l1_dir
    lock_path = L1_LOCK if lock_path is None else lock_path
    judge = load_l1_judge() if judge is None else judge
    rows: list[dict[str, Any]] = []
    if not lock_path.is_file():
        return [{"set": "*", "platform": "*", "verdict": "INSUFFICIENT", "reasons": [f"缺少 L1 凍結檔 {lock_path}"]}]
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    sets = lock.get("sets") or {}
    for set_name, subdir in P4_L1_SETS.items():
        for platform_name in PLATFORMS:
            row: dict[str, Any] = {"set": set_name, "platform": platform_name}
            report_dir = l1_dir / platform_name / subdir
            if judge is None:
                row.update(verdict="INSUFFICIENT", reasons=["BLOCKED-PREREQ：缺 tests/acceptance/l1_judge.py（需併入 feature/rust-refactor）"])
                rows.append(row)
                continue
            entry = (sets.get(set_name) or {}).get(platform_name)
            if entry is None:
                row.update(verdict="INSUFFICIENT", reasons=["lock 無此集合／平台（missing）"])
                rows.append(row)
                continue
            report_path = report_dir / "resource-report.json"
            if not report_path.is_file():
                row.update(verdict="INSUFFICIENT", reasons=[f"missing 報告 {report_path}"])
                rows.append(row)
                continue
            try:
                report = json.loads(report_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                row.update(verdict="INSUFFICIENT", reasons=[f"報告無法解析：{exc}"])
                rows.append(row)
                continue
            junit = None if set_name in JUNIT_OPTIONAL_SETS else _junit_counts(report_dir / "junit.xml")
            try:
                required = judge.required_for(lock, set_name, platform_name)
            except KeyError:
                row.update(verdict="INSUFFICIENT", reasons=["lock 無此平台集合（required_for KeyError）"])
                rows.append(row)
                continue
            result = judge.judge(
                report,
                required,
                platform=platform_name,
                junit=junit,
                selector=entry.get("selector"),
            )
            verdict = result["verdict"]
            reasons = list(result.get("reasons", []))
            if subdir == "cargo" and report.get("runner") == "cargo":
                # S1X：擁有者機台相依列（cap_*／登記列）未執行 → BLOCKED-OWNER（非 PASS、非靜默）；
                # 登記列缺席於報告 → INSUFFICIENT。判定本身仍只來自 P2 judge。
                owner = owner_gated.scan_cargo_report(report, platform_name)
                owner_skipped = owner["owner_gated_skipped"]
                row["owner_gated"] = owner_skipped
                if verdict == "INSUFFICIENT" and owner_gated.insufficiency_owner_only(result, owner_skipped):
                    verdict = "BLOCKED-OWNER"
                    reasons.append("僅因擁有者機台相依列未執行（BLOCKED-OWNER）")
                elif verdict == "PASS" and owner_skipped:
                    verdict = "BLOCKED-OWNER"
                    reasons.append(f"擁有者機台相依列未執行：{owner_skipped}")
                if owner["absent_expected"]:
                    if verdict in ("PASS", "BLOCKED-OWNER"):
                        verdict = "INSUFFICIENT"
                    reasons.append(f"owner-gated 登記列缺席於報告：{owner['absent_expected']}")
            row.update(verdict=verdict, reasons=reasons)
            rows.append(row)
    return rows


def _evidence_test_ids(cases: list[dict[str, Any]], evidence_dir: Path, head: str) -> set[str]:
    """必要案例證據中、受測 HEAD、屬適用性規則範圍（cargo／contract_blackbox pytest）的 test_id。"""
    ids: set[str] = set()
    for case in cases:
        if case.get("necessity") != "required":
            continue
        try:
            data = json.loads((evidence_dir / f"{case['case_id']}.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, KeyError, TypeError):
            continue
        records = data.get("records") if isinstance(data, dict) else None
        if not isinstance(records, list):
            continue
        for record in records:
            if not isinstance(record, dict) or record.get("commit") != head:
                continue
            node_id = _rule_node_id(record.get("test_id"))
            if node_id is not None:
                ids.add(node_id)
    return ids


def _case_rule_nodes(cases: list[dict[str, Any]]) -> set[str]:
    """必要案例映射（tests）中屬適用性規則範圍的節點（cargo 去除前綴）。"""
    nodes: set[str] = set()
    for case in cases:
        if case.get("necessity") != "required":
            continue
        for test in case.get("tests", []):
            node = _rule_node_id(test)
            if node is not None:
                nodes.add(node)
    return nodes


def _not_applicable_pairs(cases: list[dict[str, Any]], applicability: dict[str, frozenset[str]]) -> list[dict[str, str]]:
    """必要案例中「節點適用於其他平台、不適用於本案例某平台」的（案例, 平台, 節點）組合。

    這些組合不產生紀錄、也不在該平台執行，因此須在 summary.json 逐列列出（不得靜默縮小分母）。
    兩平台皆不適用者不在此列，一律 FAIL（見 check_case_evidence）。
    """
    rows: list[dict[str, str]] = []
    for case in cases:
        if case.get("necessity") != "required":
            continue
        for test in case.get("tests", []):
            node = _rule_node_id(test)
            if node is None or not applicability.get(node):
                continue
            for plat in case.get("platforms", []):
                if plat not in applicability[node]:
                    rows.append({
                        "case_id": case["case_id"],
                        "platform": plat,
                        "node": node,
                        "reason": f"節點僅適用於 {sorted(applicability[node])}（platform_applicability），{plat} 不產生紀錄",
                    })
    rows.sort(key=lambda r: (r["case_id"], r["platform"], r["node"]))
    return rows


def check(
    evidence_dir: Path | None = None,
    l1_dir: Path | None = None,
    summary_out: Path | None = None,
    l1_lock: Path | None = None,
) -> int:
    evidence_dir = EVIDENCE_DIR if evidence_dir is None else evidence_dir
    l1_dir = (evidence_dir / "l1") if l1_dir is None else l1_dir
    summary_path = (evidence_dir / "summary.json") if summary_out is None else summary_out
    if not LOCK.exists():
        print("缺少 p4_required.lock.json（須於紅燈 commit 前執行 --freeze）", file=sys.stderr)
        return 1
    lock = json.loads(LOCK.read_text(encoding="utf-8"))
    cases = load_cases()
    failures = validate_map(cases)
    owner_gaps: list[str] = []
    current = {c["case_id"]: c for c in cases}
    head = head_commit()
    if head == "unknown":
        failures.append("無法取得受測 HEAD commit")
    labels = load_evidence_labels()

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
        gone = _gone_nodes(entry, case)
        if gone:
            failures.append(f"{cid}: 凍結時的承接節點已不存在於映射表：{gone}")

    applicability: dict[str, frozenset[str]] = {}
    evidence_test_ids = _evidence_test_ids(cases, evidence_dir, head)
    # 適用性須涵蓋全部必要案例的規則節點（不只有證據節點），才能逐列列出不適用的（案例, 平台, 節點）組合。
    rule_nodes = sorted(evidence_test_ids | _case_rule_nodes(cases))
    if rule_nodes:
        try:
            applicability = platform_applicability(rule_nodes, cwd=REPO_ROOT)
        except (RuntimeError, ValueError, OSError, KeyError) as exc:
            failures.append(f"平台適用性收集失敗（fail-closed，不得放行）：{exc}")
    not_applicable = _not_applicable_pairs(cases, applicability)

    summary_cases: list[dict[str, Any]] = []
    for cid, case in current.items():
        if case.get("necessity") != "required":
            continue
        case_problems: list[str] = []
        if cid not in frozen_ids:
            case_problems.append(f"{cid}: 必要案例未凍結（請以 --freeze --add-only 登記）")
        problems, passing, evidence_file = check_case_evidence(
            case, head, evidence_dir, labels, applicability
        )
        case_problems.extend(problems)
        owner = case.get("evidence_status") == BLOCKED_OWNER and case.get("layer") in BLOCKED_OWNER_LAYERS
        status = "PASS"
        if case_problems:
            if owner and all(
                ("缺少證據檔" in p) or ("缺少 " in p and "PASS" in p) or ("只綁占位" in p) for p in case_problems
            ):
                status = BLOCKED_OWNER
                owner_gaps.extend(case_problems)
            else:
                status = "FAIL"
                failures.extend(case_problems)
        summary_cases.append(
            {
                "case_id": cid,
                "layer": case.get("layer"),
                "status": status,
                "problems": case_problems,
                "evidence_file": evidence_file,
                "commit": head,
                "artifacts": [
                    {
                        "platform": r["platform"],
                        "round": r["round"],
                        "artifact_sha256": r["artifact_sha256"],
                        "artifact_path": r.get("artifact_path"),
                    }
                    for r in passing
                    if r.get("result") == "PASS"
                ],
            }
        )

    if sorted(error_codes()) != sorted(lock.get("error_codes_at_freeze", [])):
        failures.append("error_codes 與凍結時不同（本 Phase 不得新增錯誤碼）")

    l1_rows = check_l1(l1_dir, l1_lock)
    for row in l1_rows:
        if row["verdict"] == "BLOCKED-OWNER":
            # 擁有者機台相依列（不論是否在鎖內）須列出為 BLOCKED-OWNER，整體 exit 2，不得靜默或記 PASS。
            owner_gaps.append(f"L1 {row['set']} {row['platform']}: BLOCKED-OWNER — " + "；".join(row["reasons"]))
        elif row["verdict"] != "PASS":
            failures.append(f"L1 {row['set']} {row['platform']}: {row['verdict']} — " + "；".join(row["reasons"]))

    overall = "FAIL" if failures else ("BLOCKED-OWNER" if owner_gaps else "PASS")
    summary = {
        "head": head,
        "lock_frozen_commit": lock.get("frozen_commit"),
        "overall": overall,
        "failure_count": len(failures),
        "blocked_owner_count": len(owner_gaps),
        "cases": summary_cases,
        "not_applicable": not_applicable,
        "l1": {
            "status": ("PASS" if all(r["verdict"] == "PASS" for r in l1_rows)
                       else "BLOCKED-OWNER" if all(r["verdict"] in ("PASS", "BLOCKED-OWNER") for r in l1_rows)
                       else "FAIL"),
            "l1_dir": str(l1_dir),
            "sets": l1_rows,
        },
    }
    try:
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        summary_path.write_text(
            json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8"
        )
    except OSError as exc:
        failures.append(f"無法寫入 summary.json：{exc}")

    for line in list(failures) + [f"BLOCKED-OWNER（僅擁有者機台證據待補，exit 2）：{g}" for g in owner_gaps]:
        print(line)
    print(f"not_applicable：{len(not_applicable)} 筆（不產生紀錄、不在該平台執行；逐列見 summary.json）")
    for row in not_applicable:
        print(f"  not_applicable {row['case_id']} {row['platform']} {row['node']}：{row['reason']}")
    if failures:
        print(f"FAIL：{len(failures)} 項", file=sys.stderr)
        return 1
    if owner_gaps:
        print(f"BLOCKED-OWNER：{len(owner_gaps)} 項擁有者機台證據待補（exit 2）")
        return 2
    print(f"PASS：{len(frozen_ids)} 筆凍結案例與目前全部必要案例的證據齊全")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="P4 必要案例凍結與完成度檢查")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--freeze", action="store_true", help="寫入 p4_required.lock.json（既有 lock 不得覆寫）")
    group.add_argument("--check", action="store_true", help="檢查必要案例的證據與碼集合")
    parser.add_argument("--add-only", action="store_true", help="與 --freeze 併用：只追加新的必要案例")
    parser.add_argument(
        "--allow-error-code-addition",
        action="append",
        default=[],
        metavar="CODE",
        help="併入已核准的前序契約碼時逐碼列名；不得移除既有凍結碼",
    )
    parser.add_argument(
        "--evidence-dir",
        type=Path,
        default=EVIDENCE_DIR,
        help="案例證據目錄（預設 target/p4-evidence）；與 --check 併用",
    )
    parser.add_argument(
        "--l1-lock",
        type=Path,
        default=None,
        help="L1 凍結檔（預設 tests/acceptance/l1_required.lock.json）；測試可改用暫存鎖逐情境驗證",
    )
    parser.add_argument(
        "--summary-out",
        type=Path,
        default=None,
        help="summary.json 輸出路徑（預設 <evidence-dir>/summary.json；測試可改寫到暫存目錄）",
    )
    parser.add_argument(
        "--l1-results",
        type=Path,
        default=None,
        help="L1 報告目錄（預設 <evidence-dir>/l1）；與 --check 併用",
    )
    args = parser.parse_args()
    if args.add_only and not args.freeze:
        parser.error("--add-only 只能與 --freeze 併用")
    if args.allow_error_code_addition and not (args.freeze and args.add_only):
        parser.error("--allow-error-code-addition 只能與 --freeze --add-only 併用")
    if args.freeze:
        return freeze(add_only=args.add_only, allowed_error_additions=args.allow_error_code_addition)
    return check(
        evidence_dir=args.evidence_dir,
        l1_dir=args.l1_results,
        summary_out=args.summary_out,
        l1_lock=args.l1_lock,
    )


if __name__ == "__main__":
    sys.exit(main())
