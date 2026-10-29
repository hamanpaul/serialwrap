"""OD-50：安裝文件改以 `install.sh`／bundle 為主要方式（README 英文與繁中兩段內容一致）。

- README 的 English 章與 繁體中文 章（含頂部 Install 導覽段）不再以 `pipx install git+...@v0.3.2` 為安裝方式；
- 兩段都寫明 `install.sh`、`--bundle` 離線安裝、系統需求（x86_64、glibc ≥ 2.39、`/usr/bin/python3.12`、Ubuntu 24.04）
  與「0.3.x 使用者升級」說明（升級前 `serialwrap daemon stop`，標註 TODO(OD-48)）；
- CLAUDE.md「實際命令」段與 release 計畫 RELEASE-TODO-02 同步。
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]

ROOT = Path(__file__).resolve().parents[1]
README = (ROOT / "README.md").read_text(encoding="utf-8")
LEGACY_PIPX = 'pipx install "git+https://github.com/hamanpaul/serialwrap@v0.3.2"'


def _sections() -> tuple[str, str]:
    en_start = README.index("\n## English\n")
    zh_start = README.index("\n## 繁體中文\n")
    assert en_start < zh_start
    return README[:zh_start], README[zh_start:]


@pytest.mark.parametrize("index", [0, 1], ids=["english", "zh-tw"])
def test_readme_install_uses_bundle(index):
    text = _sections()[index]
    assert LEGACY_PIPX not in text
    for token in ("./install.sh", "--bundle", "--version", "x86_64", "2.39", "/usr/bin/python3.12",
                  "Ubuntu 24.04", "SHA256SUMS", "mkbundle.sh --release-bundle --profile release",
                  "serialwrap daemon stop", "TODO(OD-48)", "pipx uninstall serialwrap", "0.3.x"):
        assert token in text, token


def test_claude_md_install_command_synced():
    text = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    start = text.index("## 實際命令")
    section = text[start:text.index("\n## ", start + 1)]
    assert "pipx install + `serialwrap setup`" not in section
    assert "--bundle" in section and "SHA256SUMS" in section


def test_release_plan_records_od50():
    plan = (ROOT / "docs" / "rust-refactor" / "release-0.4.0-plan.md").read_text(encoding="utf-8")
    todo = plan[plan.index("**RELEASE-TODO-02"):]
    todo = todo[:todo.index("\n\n")]
    assert "OD-50" in todo and "--bundle" in todo
    assert "目前仍為 pipx 安裝" not in plan
