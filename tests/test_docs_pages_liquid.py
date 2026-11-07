"""GitHub Pages 建置守門：會發布的文件不得含 Liquid tag 起始符 `{%`。

Pages 以 Jekyll 建置 `docs/`，沒有 front matter 的 Markdown 也會經過 Liquid。文件裡引用的
`{%y%m%d}` 之類內容不是合法 tag，整站建置會中止。這個失敗只在 merge 進 main 後才出現，
PR 上看不到，所以在 pytest 先擋。程式碼密集的目錄由 `docs/_config.yml` 排除，不必檢查。
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]

DOCS = Path(__file__).resolve().parent.parent / "docs"
PAGE_SUFFIXES = {".md", ".markdown", ".html"}


def _excluded() -> list[str]:
    config = DOCS / "_config.yml"
    if not config.is_file():
        return []
    return yaml.safe_load(config.read_text(encoding="utf-8")).get("exclude") or []


def _published_pages() -> list[Path]:
    excluded = _excluded()
    pages = []
    for path in DOCS.rglob("*"):
        if path.suffix not in PAGE_SUFFIXES or not path.is_file():
            continue
        rel = path.relative_to(DOCS).as_posix()
        if not any(rel.startswith(f"{entry}/") for entry in excluded):
            pages.append(path)
    return pages


def test_published_pages_have_no_liquid_tags() -> None:
    pages = _published_pages()
    assert pages, "找不到會發布的文件，檢查條件形同虛設"
    offenders = [
        f"{path.relative_to(DOCS).as_posix()}:{lineno}"
        for path in pages
        for lineno, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1)
        if "{%" in line
    ]
    assert not offenders, f"這些文件的 `{{%` 會讓 Pages 建置失敗，請改寫或排除：{offenders}"
