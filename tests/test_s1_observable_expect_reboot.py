"""可觀察變更表必須反映 Rust CLI 已接受 `cmd submit --expect-reboot`。"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]

DOC = (
    Path(__file__).resolve().parents[1]
    / "docs"
    / "rust-refactor"
    / "reviews"
    / "s1-main-sync-observable-changes.md"
)

_STALE = (
    "parser 沒有 `--expect-reboot`",
    "但未有 `--expect-reboot`",
    "也不組 `expect_reboot`",
)


def test_observable_changes_expect_reboot_rows_match_cli():
    text = DOC.read_text(encoding="utf-8")
    for stale in _STALE:
        assert stale not in text, stale
    assert "check_expect_reboot" in text
    assert "EXPECT_REBOOT_MODE_UNSUPPORTED" in text
