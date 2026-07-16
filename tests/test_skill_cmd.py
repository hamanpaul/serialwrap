"""#131 點 4：`serialwrap skill` 子命令——輸出操作指南原文到 stdout。

- ``--platform windows`` → ``sw_core/assets/skill/SKILL_WINDOWS.md`` 原文
  （兼守打包 glob：資產遺失時測試即失敗）；
- ``--platform linux`` → 既有 ``SKILL.md`` 原文；
- ``auto``（預設）依實際平台選擇；
- 唯讀、不需 daemon。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pytest

import sw_core.assets
from sw_core import cli

_ASSET_DIR = Path(sw_core.assets.__file__).parent / "skill"


def _asset_text(name: str) -> str:
    return (_ASSET_DIR / name).read_text(encoding="utf-8")


def test_skill_windows_prints_asset_verbatim(capsys: pytest.CaptureFixture[str]) -> None:
    rc = cli._run_skill(argparse.Namespace(platform="windows"))
    out = capsys.readouterr().out
    assert rc == 0
    assert out == _asset_text("SKILL_WINDOWS.md")
    assert "Tera Term" in out
    assert "Telnet" in out


def test_skill_linux_prints_existing_skill(capsys: pytest.CaptureFixture[str]) -> None:
    rc = cli._run_skill(argparse.Namespace(platform="linux"))
    out = capsys.readouterr().out
    assert rc == 0
    assert out == _asset_text("SKILL.md")


def test_skill_auto_matches_current_platform(capsys: pytest.CaptureFixture[str]) -> None:
    rc = cli._run_skill(argparse.Namespace(platform="auto"))
    out = capsys.readouterr().out
    assert rc == 0
    expected = "SKILL_WINDOWS.md" if sys.platform.startswith("win") else "SKILL.md"
    assert out == _asset_text(expected)


def test_parser_accepts_skill_subcommand() -> None:
    args = cli.build_parser().parse_args(["skill", "--platform", "windows"])
    assert args.cmd == "skill"
    assert args.platform == "windows"


def test_parser_skill_platform_defaults_to_auto() -> None:
    args = cli.build_parser().parse_args(["skill"])
    assert args.platform == "auto"
