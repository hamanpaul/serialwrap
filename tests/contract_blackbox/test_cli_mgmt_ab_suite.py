"""Stage 1B 管理面全群組黑箱 A/B 套件（P4-T16）。

AB_CASES 由 tests/acceptance/p4_required.lock.json 讀取（凍結於紅燈 commit），不得以 pytest collection 反推；
對每個案例以 P2 比較器在「Python CLI＋Python daemon」與「Rust CLI＋Python daemon」兩輪依序執行
（先停 A、確認資源釋放、再起 B，新模擬器實例），斷言比較器結果 equal；
rust_only 案例只跑 B 並驗絕對契約。凍結清單中任一案例的測試節點不存在或被 SKIP（未登記）→ FAIL。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from bb_harness import (
    ab_env,
    cli_prefix,
    isolated_env,
    python_baseline_prefix,
    repo_root,
)

pytestmark = [
    pytest.mark.core_portable,
    pytest.mark.process_rpc,
]

ROOT = repo_root()
LOCK_PATH = ROOT / "tests" / "acceptance" / "p4_required.lock.json"


def _load_ab_cases() -> list[str]:
    """從 p4_required.lock.json 讀取凍結之案例清單，不得以 pytest collection 反推。"""
    if not LOCK_PATH.is_file():
        pytest.fail(f"缺少凍結案例鎖檔 {LOCK_PATH}，無法取得 AB_CASES")
    data = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    cases = data.get("cases", [])
    case_ids = [c["case_id"] for c in cases if isinstance(c, dict) and "case_id" in c]
    if not case_ids:
        pytest.fail("凍結集合 AB_CASES 為空")
    return case_ids


@pytest.mark.case("s1b-ab-suite-execution")
@pytest.mark.xfail(
    strict=True,
    reason="P4-T16 紅燈：Stage 1B 全群組黑箱 A/B 比較套件尚未全數通過",
)
def test_cli_mgmt_ab_suite_execution(tmp_path: Path) -> None:
    """執行全群組黑箱 A/B 比較。

    依序以 Python CLI＋Python daemon 與 Rust CLI＋Python daemon 兩輪執行，
    斷言比較器結果 equal，且凍結清單中任一案例的測試節點不存在或未登記 SKIP 則 FAIL。
    """
    case_ids = _load_ab_cases()
    comparator = ROOT / "scripts" / "rust" / "ab_compare.py"
    if not comparator.is_file():
        pytest.fail(f"A/B 比較器不存在：{comparator}")

    env = isolated_env(tmp_path)
    res = subprocess.run(
        [sys.executable, str(comparator), "--stage", "1b", "--lock", str(LOCK_PATH)],
        env=env,
        capture_output=True,
        text=True,
        timeout=1800,
    )
    assert res.returncode == 0, res.stdout + res.stderr
    report = json.loads(res.stdout)
    assert report.get("result") == "equal"
    assert report.get("unregistered_skips", []) == []


@pytest.mark.case("s1b-ab-cases-lock-integrity")
@pytest.mark.xfail(
    strict=True,
    reason="P4-T16 紅燈：p4_required.lock.json 尚未凍結",
)
def test_cli_mgmt_ab_cases_lock_present() -> None:
    """驗證 AB_CASES 來源 p4_required.lock.json 存在且格式正確。"""
    assert LOCK_PATH.is_file(), f"找不到凍結案例鎖檔：{LOCK_PATH}"
    data = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    assert "cases" in data and len(data["cases"]) > 0
