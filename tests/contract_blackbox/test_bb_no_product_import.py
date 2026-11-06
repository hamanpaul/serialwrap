"""黑箱測試不得 import 產品碼（sw_core／serialwrap／serialwrapd）的 AST 稽核。

守護 P3-T2：黑箱 harness 只能以 CLI 位元組與 socket／PTY 與受測 artifact 互動，
不得藉由 import 產品模組取得實作細節（規格 §10.4、arch §9.2）。
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]

HERE = Path(__file__).resolve().parent
FORBIDDEN = ("sw_core", "serialwrap", "serialwrapd")


def test_no_product_import_anywhere_in_blackbox():
    offenders = []
    for py in sorted(HERE.rglob("*.py")):
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for n in names:
                if n.split(".")[0] in FORBIDDEN:
                    offenders.append(f"{py.relative_to(HERE)}:{node.lineno}:{n}")
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "__import__":
                offenders.append(f"{py.relative_to(HERE)}:{node.lineno}:__import__")
    assert offenders == []
