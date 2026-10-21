"""Stage 1B 管理面全群組黑箱 A/B 套件（P4-T16）。

AB_CASES 由 tests/acceptance/p4_required.lock.json 讀取（凍結於紅燈 commit），不得以 pytest collection 反推；
對每個案例以 P2 比較器在「Python CLI＋Python daemon」與「Rust CLI＋Python daemon」兩輪依序執行
（先停 A、確認資源釋放、再起 B，新模擬器實例），斷言比較器結果 pass；rust_only 案例只跑 B 並驗絕對契約。
凍結清單中任一 pytest 節點不可收集、或被 SKIP／xfail（未登記）→ FAIL。

現況（BLOCKED）：本分支的 scripts/rust/ab_blackbox.sh 僅支援 stage 1A（固定 rust-1a、cli_1A），
scripts/rust/ab_compare.py 無 `--stage 1b --lock` 介面；兩輪執行器尚未實作，執行案例以 BLOCKED-PREREQ 明確失敗，
不以 xfail 隱藏，亦不以既有 1A 介面假裝完成。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from bb_harness import repo_root

pytestmark = [
    pytest.mark.core_portable,
    pytest.mark.process_rpc,
]

ROOT = repo_root()
LOCK_PATH = ROOT / "tests" / "acceptance" / "p4_required.lock.json"


def _load_lock() -> dict:
    if not LOCK_PATH.is_file():
        pytest.fail(f"缺少凍結案例鎖檔 {LOCK_PATH}，無法取得 AB_CASES")
    return json.loads(LOCK_PATH.read_text(encoding="utf-8"))


def _load_ab_cases() -> list[str]:
    """從 p4_required.lock.json 讀取凍結之案例清單，不得以 pytest collection 反推。"""
    cases = _load_lock().get("cases", [])
    case_ids = [c["case_id"] for c in cases if isinstance(c, dict) and "case_id" in c]
    if not case_ids:
        pytest.fail("凍結集合 AB_CASES 為空")
    return case_ids


def _frozen_pytest_nodes() -> list[str]:
    """凍結集合中的 pytest 節點（tests/…::…）；Rust 節點（sw-cli::…）由 cargo 驗證，不在此範圍。"""
    nodes: set[str] = set()
    for case in _load_lock().get("cases", []):
        for node in case.get("tests", []):
            if isinstance(node, str) and node.startswith("tests/") and "::" in node:
                nodes.add(node)
    return sorted(nodes)


def test_cli_mgmt_ab_suite_execution(tmp_path: Path) -> None:
    """全群組黑箱 A/B 執行（BLOCKED-PREREQ）。

    通過判準（待實作）：依序以 Python CLI＋Python daemon 與 Rust CLI＋Python daemon 兩輪執行凍結節點，
    比較器結果 pass，且 unregistered skip／xfail 與 evidence-insufficient 皆為 0。
    """
    pytest.fail(
        "BLOCKED-PREREQ：全群組 A/B 兩輪執行器未實作（scripts/rust/ab_blackbox.sh 僅 stage 1A；"
        "ab_compare.py 無 --stage 1b／--lock）。需擴充執行器後才可判定 pass；本案例不得以 xfail 或既有 1A 結果代替。"
        f" 凍結案例數：{len(_load_ab_cases())}"
    )


def test_cli_mgmt_ab_cases_lock_present() -> None:
    """凍結鎖存在；且每個凍結 pytest 節點可收集（不因缺漏被排除；收集失敗即 FAIL）。"""
    assert LOCK_PATH.is_file(), f"找不到凍結案例鎖檔：{LOCK_PATH}"
    data = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    assert "cases" in data and len(data["cases"]) > 0
    nodes = _frozen_pytest_nodes()
    assert nodes, "凍結集合中沒有 pytest 節點"
    res = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", *nodes],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=600,
    )
    collected = {line.strip() for line in res.stdout.splitlines() if "::" in line}
    missing = [node for node in nodes if node not in collected]
    assert res.returncode == 0 and not missing, (
        f"凍結節點無法收集（missing {len(missing)}）：{missing[:5]}\n{res.stdout[-800:]}{res.stderr[-800:]}"
    )
