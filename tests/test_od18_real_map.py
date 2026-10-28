"""OD-18 對帳（真實資料）：以實際映射與實際 pytest 收集求得適用性，實算不適用列須逐列等於核准清單。

不使用 test_check_p4_cases.py 的合成適用性與 gate fixture（fixture 會改指向暫存映射）。真實適用性依 POSIX 收集而定，
故本檔標為 posix_only，由共同平台規則於 Windows 略過（不以平台 skip 繞過 core_portable 之稽核）。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

pytestmark = [pytest.mark.posix_only, pytest.mark.process_rpc]

REPO_ROOT = Path(__file__).resolve().parents[1]
GATE_PATH = REPO_ROOT / "scripts" / "rust" / "check_p4_cases.py"


def _load_gate():
    spec = importlib.util.spec_from_file_location("check_p4_cases_real_map", GATE_PATH)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_od18_real_map_not_applicable_rows_equal_approved_list() -> None:
    """新增或移除任一不適用列時，此測試即指出（待擁有者核准）。"""
    gate = _load_gate()
    cases = gate.load_cases()
    nodes = sorted(gate._case_rule_nodes(cases))
    applicability = gate.platform_applicability(nodes, cwd=gate.REPO_ROOT)
    pairs = gate._not_applicable_pairs(cases, applicability)
    assert gate.not_applicable_approval_problems(cases, pairs) == []
