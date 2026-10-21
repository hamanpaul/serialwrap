"""P4 驗收案例綁定映射表（p4_case_map.yaml）契約測試。

驗證：
1. p4_case_map.yaml 符合 p4_case_map.schema.json 定義
2. behavior_changes.yaml 中所有 phase 含 P4 的變更項目其 case_ids 全部有對應條目
3. P2 `cases.lock.json` 的 `sets.cli_1B`（字典序 case_id 陣列）與 `cases.yaml` 的 `implemented_by` 交叉後，
   屬於本計畫者全部出現在映射表
4. 每筆承接節點真實存在，且**不執行 cargo**：
   - Rust 節點 `<crate>::<target>::<fn>` 對應 `crates/<crate>/tests/<target>.rs`，須有緊鄰（僅隔以屬性）的 `#[test]` 的 `fn <fn>(`；
   - pytest 節點 `tests/...py::...` 以 `pytest --collect-only` 收集，且原始檔含 `@pytest.mark.case("<case_id>")`。

環境變數 `SW_REQUIRE_CASES_LOCK=1`：P2 的 lock／cases.yaml 缺席時改為失敗（預設 skip，因 P2-T10+ 未交付時仍可跑其餘檢查）。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import jsonschema
import pytest
import yaml

pytestmark = [pytest.mark.core_portable, pytest.mark.process_rpc]

REPO_ROOT = Path(__file__).resolve().parents[1]
CASE_MAP_PATH = REPO_ROOT / "tests" / "acceptance" / "p4_case_map.yaml"
SCHEMA_PATH = REPO_ROOT / "tests" / "acceptance" / "p4_case_map.schema.json"
BEHAVIOR_CHANGES_PATH = REPO_ROOT / "contracts" / "behavior_changes.yaml"
CASES_LOCK_PATH = REPO_ROOT / "tests" / "acceptance" / "cases.lock.json"
CASES_YAML_PATH = REPO_ROOT / "tests" / "acceptance" / "cases.yaml"
PLAN_NAME = "05-stage1b-rust-cli-mgmt.md"
REQUIRE_P2 = os.environ.get("SW_REQUIRE_CASES_LOCK") == "1"


def _load_case_map() -> dict:
    with open(CASE_MAP_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _load_schema() -> dict:
    with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _rust_node_present(crate: str, target: str, fn_name: str) -> bool:
    """靜態比對：`crates/<crate>/tests/<target>.rs` 內 `fn <fn_name>(` 的屬性列含 `#[test]`。"""
    path = REPO_ROOT / "crates" / crate / "tests" / f"{target}.rs"
    if not path.is_file():
        return False
    text = path.read_text(encoding="utf-8")
    pattern = re.compile(
        r"(?:#\[[^\]]*\]\s*)*#\[test\]\s*(?:#\[[^\]]*\]\s*)*(?:pub\s+)?fn\s+"
        + re.escape(fn_name)
        + r"\s*\("
    )
    return pattern.search(text) is not None


def _pytest_node_present(node: str, case_id: str) -> str | None:
    """回傳 None 表示通過；否則回傳失敗原因。"""
    file_part = node.split("::", 1)[0]
    if not (REPO_ROOT / file_part).is_file():
        return f"檔案不存在：{file_part}"
    source = (REPO_ROOT / file_part).read_text(encoding="utf-8")
    if f'pytest.mark.case("{case_id}")' not in source:
        return f"缺少 @pytest.mark.case(\"{case_id}\")"
    res = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", node],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    if res.returncode != 0:
        return f"pytest --collect-only 失敗（rc={res.returncode}）：{res.stdout[-300:]}{res.stderr[-300:]}"
    collected = [line.strip() for line in res.stdout.splitlines() if "::" in line]
    if node not in collected:
        return "pytest 未收集到此節點"
    return None


def test_p4_case_map_matches_schema() -> None:
    data = _load_case_map()
    schema = _load_schema()
    jsonschema.validate(instance=data, schema=schema)


def test_p4_case_map_covers_all_p4_behavior_changes() -> None:
    data = _load_case_map()
    mapped_ids = {entry["case_id"] for entry in data.get("cases", [])}

    with open(BEHAVIOR_CHANGES_PATH, "r", encoding="utf-8") as f:
        bc_data = yaml.safe_load(f)

    expected_ids = set()
    for change in bc_data.get("changes", []):
        if "P4" in change.get("phases", []):
            expected_ids.update(change.get("case_ids", []))

    missing = expected_ids - mapped_ids
    assert not missing, f"p4_case_map.yaml 缺少以下 P4 行為變更案例：{sorted(missing)}"


def test_p4_case_map_covers_cli_1b_cases() -> None:
    if not (CASES_LOCK_PATH.exists() and CASES_YAML_PATH.exists()):
        message = "cases.lock.json／cases.yaml 尚未交付（依賴 P2-T10+）"
        if REQUIRE_P2:
            pytest.fail(message + "；SW_REQUIRE_CASES_LOCK=1 時不得略過")
        pytest.skip(message)

    lock_data = json.loads(CASES_LOCK_PATH.read_text(encoding="utf-8"))
    # P2 規格：sets.<name> 為依字典序排序的 case_id 陣列（03 §Task 11）。
    cli_1b = lock_data.get("sets", {}).get("cli_1B")
    assert isinstance(cli_1b, list), "cases.lock.json sets.cli_1B 必須為 case_id 陣列"

    cases_doc = yaml.safe_load(CASES_YAML_PATH.read_text(encoding="utf-8")) or {}
    rows = cases_doc.get("cases", []) if isinstance(cases_doc, dict) else cases_doc
    implemented_here = {row["case_id"] for row in rows if isinstance(row, dict) and row.get("implemented_by") == PLAN_NAME}

    expected = set(cli_1b) & implemented_here
    mapped_ids = {entry["case_id"] for entry in _load_case_map().get("cases", [])}
    missing = expected - mapped_ids
    assert not missing, f"p4_case_map.yaml 缺少 cli_1B 中 implemented_by 為本計畫的案例：{sorted(missing)}"


def test_p4_case_map_tests_exist() -> None:
    cases = _load_case_map().get("cases", [])
    assert cases, "案例映射表不得為空"

    failures: list[str] = []
    for entry in cases:
        case_id = entry["case_id"]
        tests = entry.get("tests", [])
        assert tests, f"案例 {case_id} 未定義任何承接測試節點"
        for node in tests:
            if node.startswith("tests/"):
                reason = _pytest_node_present(node, case_id)
                if reason:
                    failures.append(f"{case_id}: {node} — {reason}")
                continue
            parts = node.split("::")
            if len(parts) != 3:
                failures.append(f"{case_id}: Rust 節點格式應為 <crate>::<target>::<fn>：{node}")
                continue
            crate, target, fn_name = parts
            if not _rust_node_present(crate, target, fn_name):
                failures.append(f"{case_id}: 找不到 crates/{crate}/tests/{target}.rs 內的 #[test] fn {fn_name}")

    assert not failures, "承接測試節點不存在：\n" + "\n".join(failures)


@pytest.mark.process_rpc
def test_p4_freeze_add_only_requires_exact_error_code_allowlist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """合併前已核准的碼只能逐名補入凍結基線，其他碼新增或刪除仍拒絕。"""
    import importlib.util

    script = REPO_ROOT / "scripts" / "rust" / "check_p4_cases.py"
    spec = importlib.util.spec_from_file_location("check_p4_cases_freeze_fixture", script)
    assert spec is not None and spec.loader is not None
    checker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(checker)

    case = {
        "case_id": "fixture-case",
        "necessity": "required",
        "tests": ["sw-cli::target::test_case"],
        "invariants": [],
        "layer": "L1",
        "platforms": ["linux"],
        "capabilities": [],
        "artifact": "rust-cli-bundle",
        "evidence": "target/p4-evidence/fixture-case.json",
    }
    entry = checker._entry(case)
    lock_path = tmp_path / "p4_required.lock.json"
    lock_path.write_text(json.dumps({
        "schema_version": 2,
        "frozen_commit": "base",
        "amended_commits": [],
        "case_set_sha256": checker._set_digest([entry]),
        "cases": [entry],
        "error_codes_at_freeze": ["EXISTING_CODE"],
    }), encoding="utf-8")
    monkeypatch.setattr(checker, "LOCK", lock_path)
    monkeypatch.setattr(checker, "load_cases", lambda: [case])
    monkeypatch.setattr(checker, "validate_map", lambda _cases: [])
    monkeypatch.setattr(checker, "error_codes", lambda: ["DRAIN_STALLED", "EXISTING_CODE"])
    monkeypatch.setattr(checker, "head_commit", lambda: "merge-head")

    before = lock_path.read_bytes()
    assert checker.freeze(add_only=True) == 1
    assert lock_path.read_bytes() == before

    assert checker.freeze(add_only=True, allowed_error_additions=["DRAIN_STALLED"]) == 0
    updated = json.loads(lock_path.read_text(encoding="utf-8"))
    assert updated["error_codes_at_freeze"] == ["DRAIN_STALLED", "EXISTING_CODE"]
    assert updated["amended_commits"] == ["merge-head"]

    lock_after = lock_path.read_bytes()
    monkeypatch.setattr(checker, "error_codes", lambda: ["DRAIN_STALLED", "EXISTING_CODE", "UNAPPROVED"])
    assert checker.freeze(add_only=True, allowed_error_additions=["DRAIN_STALLED"]) == 1
    assert lock_path.read_bytes() == lock_after

    monkeypatch.setattr(checker, "error_codes", lambda: ["DRAIN_STALLED"])
    assert checker.freeze(add_only=True, allowed_error_additions=["EXISTING_CODE"]) == 1
    assert lock_path.read_bytes() == lock_after
