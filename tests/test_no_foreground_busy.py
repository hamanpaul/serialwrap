"""P2-T14 red：GAP-G-12——realhw／regression 不得再含 FOREGROUND_BUSY。

對應 docs/rust-refactor/plans/03-stage0-test-infra.md Task 14 T14-1。
產品端仍會回傳 FOREGROUND_BUSY；測試端改以 per-board lock、--source 與 BgResultTracker 收斂，
不再把它列為容許碼或在說明文字中當作前提。
"""
from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

pytestmark = [pytest.mark.core_portable]


@pytest.mark.memory_only
def test_no_foreground_busy():
    hits: list[str] = []
    for root in (REPO_ROOT / "realhw", REPO_ROOT / "regression"):
        for path in sorted(root.rglob("*.py")):
            text = path.read_text(encoding="utf-8", errors="replace")
            for lineno, line in enumerate(text.splitlines(), start=1):
                if "FOREGROUND_BUSY" in line:
                    hits.append(f"{path.relative_to(REPO_ROOT)}:{lineno}")
    assert hits == []
