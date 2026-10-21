"""Stage 1B 管理面全群組黑箱 A/B 套件（P4-T16）。

AB_CASES 由 tests/acceptance/p4_required.lock.json 讀取（凍結於紅燈 commit），不得以 pytest collection 反推；
對每個案例以 P2 比較器在「Python CLI＋Python daemon」與「Rust CLI＋Python daemon」兩輪依序執行
（先停 A、確認資源釋放、再起 B，新模擬器實例），斷言比較器結果 pass；rust_only 案例只跑 B 並驗絕對契約。
凍結清單中任一 pytest 節點來源不存在、適用平台不可收集、或被 SKIP／xfail（未登記）→ FAIL。

現況（BLOCKED）：本分支的 scripts/rust/ab_blackbox.sh 僅支援 stage 1A（固定 rust-1a、cli_1A），
scripts/rust/ab_compare.py 無 `--stage 1b --lock` 介面；兩輪執行器尚未實作，執行案例以 BLOCKED-PREREQ 明確失敗，
不以 xfail 隱藏，亦不以既有 1A 介面假裝完成。
"""
from __future__ import annotations

import ast
import json
import os
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


def _pytest_node_exists_in_source(node: str) -> bool:
    """確認凍結節點存在於原始碼，避免平台 collection 排除改變凍結分母。"""
    file_part, *segments = node.split("::")
    path = ROOT / file_part
    if not path.is_file() or not segments:
        return False
    try:
        scope = ast.parse(path.read_text(encoding="utf-8")).body
    except (OSError, SyntaxError, UnicodeError):
        return False
    for segment in segments:
        name = segment.split("[", 1)[0]
        found = next(
            (
                item
                for item in scope
                if isinstance(item, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                and item.name == name
            ),
            None,
        )
        if found is None:
            return False
        scope = getattr(found, "body", [])
    return True


def _pytest_node_markers(node: str) -> set[str]:
    """讀取節點所屬 module／class／function 的 pytestmark 與 decorators。"""
    file_part, *segments = node.split("::")
    path = ROOT / file_part
    if not path.is_file():
        return set()
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeError):
        return set()
    owners: list[ast.AST] = [tree]
    scope = tree.body
    for segment in segments:
        name = segment.split("[", 1)[0]
        found = next(
            (
                item
                for item in scope
                if isinstance(item, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                and item.name == name
            ),
            None,
        )
        if found is None:
            return set()
        owners.append(found)
        scope = getattr(found, "body", [])

    markers: set[str] = set()
    for owner in owners:
        expressions: list[ast.AST] = list(getattr(owner, "decorator_list", []))
        for statement in getattr(owner, "body", []):
            if isinstance(statement, (ast.Assign, ast.AnnAssign)):
                targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
                if any(isinstance(target, ast.Name) and target.id == "pytestmark" for target in targets):
                    expressions.append(statement.value)
        for expression in expressions:
            for item in ast.walk(expression):
                if (
                    isinstance(item, ast.Attribute)
                    and isinstance(item.value, ast.Attribute)
                    and item.value.attr == "mark"
                ):
                    markers.add(item.attr)
    return markers


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
    """凍結鎖與節點來源存在；host 的平台排除不得縮小凍結分母。"""
    assert LOCK_PATH.is_file(), f"找不到凍結案例鎖檔：{LOCK_PATH}"
    data = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    assert "cases" in data and len(data["cases"]) > 0
    nodes = _frozen_pytest_nodes()
    assert nodes, "凍結集合中沒有 pytest 節點"
    missing = [node for node in nodes if not _pytest_node_exists_in_source(node)]
    assert not missing, f"凍結集合節點來源不存在（missing {len(missing)}）：{missing[:10]}"
    collectable = [
        node
        for node in nodes
        if not (os.name == "nt" and "posix_only" in _pytest_node_markers(node))
    ]
    if collectable:
        res = subprocess.run(
            [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", *collectable],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=600,
        )
        collected = {line.strip() for line in res.stdout.splitlines() if "::" in line}
        missing_collection = [node for node in collectable if node not in collected]
        assert res.returncode == 0 and not missing_collection, (
            f"適用平台凍結節點無法收集（missing {len(missing_collection)}）："
            f"{missing_collection[:10]}\n{res.stdout[-1000:]}{res.stderr[-1000:]}"
        )
