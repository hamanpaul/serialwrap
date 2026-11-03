"""變更紀錄 fragment 必須符合 policy_check 的 YAML frontmatter 契約。"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]


def test_expect_reboot_fragment_has_yaml_frontmatter():
    fragment = Path(__file__).resolve().parents[1] / "changelog.d" / "242-rust-cli-expect-reboot.md"
    lines = fragment.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "---"
    assert "---" in lines[1:]
