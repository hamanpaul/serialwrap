"""變更紀錄 fragment 必須能被 policy_check 解析並收斂。"""

from __future__ import annotations

from pathlib import Path

import pytest
from policy_check.changelog import load_fragments, render_section

pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]


def test_changelog_fragments_parse_with_policy_check(tmp_path: Path):
    """全部 changelog.d/*.md 都要通過 frontmatter 與 TYPE_TO_SECTION。

    目錄被 release collate 清空時沒有 fragment，檢查仍然通過。
    不硬編碼單一檔名，也不只看 `---` 分隔線。
    """
    repo_root = Path(__file__).resolve().parents[1]
    loaded = load_fragments(repo_root)
    if loaded:
        render_section("0.0.0", "2026-01-01", [frag for _, frag in loaded])

    empty = tmp_path / "empty-repo"
    (empty / "changelog.d").mkdir(parents=True)
    assert load_fragments(empty) == []
