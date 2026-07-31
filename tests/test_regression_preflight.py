"""#155 preflight 版本 gate 純函式單測（#154 防線）。"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "regression"))

from serialwrap_regression import preflight  # noqa: E402


def test_version_gate_pass_on_match():
    assert preflight.version_gate("serialwrap 0.2.4", "0.2.4") is None


def test_version_gate_refuses_on_mismatch():
    got = preflight.version_gate("serialwrap 0.2.4", "0.2.3")
    assert got is not None and "0.2.4" in got and "0.2.3" in got and "#154" in got


def test_version_gate_refuses_on_unparsable():
    assert preflight.version_gate("", "0.2.4") is not None
    assert preflight.version_gate("serialwrap 0.2.4", "") is not None


def test_stale_client_note():
    assert preflight.stale_client_note("serialwrap 0.2.4", "serialwrap 0.2.4") is None
    note = preflight.stale_client_note("serialwrap 0.2.1", "serialwrap 0.2.4")
    assert note is not None and "0.2.1" in note and "#154" in note
    assert preflight.stale_client_note("", "serialwrap 0.2.4") is None  # 解析不出→不加註
