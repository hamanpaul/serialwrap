"""#131 補強：`serialwrap --version` 與 `daemon start --profile-dir` help。

- `--version`：v0.2.2 起 CLI 從未接上版本旗標（argparse 直接回缺 <group>），
  release exe 使用者無從確認手上版本。解析順序：repo checkout 的 VERSION →
  已安裝套件 metadata（pip/pipx）→ PyInstaller 內嵌 assets/VERSION（release
  exe，serialwrap.spec datas）→ "unknown"。
- `--profile-dir`：原本無 help 字串、不顯示預設路徑。
"""
from __future__ import annotations

import os
from pathlib import Path
from unittest import mock

import pytest

from sw_core import cli

_REPO_VERSION = (Path(__file__).parent.parent / "VERSION").read_text(encoding="utf-8").strip()


class TestResolveVersion:
    def test_source_checkout_reads_repo_version_file(self) -> None:
        assert cli._resolve_version() == _REPO_VERSION

    def test_frozen_fallback_reads_bundled_assets(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """repo VERSION 不存在（frozen/安裝環境）→ metadata → assets/VERSION。"""
        monkeypatch.setattr(cli, "_repo_version_path", lambda: "/nonexistent/VERSION")
        with (
            mock.patch("importlib.metadata.version", side_effect=Exception("not installed")),
            mock.patch("sw_core.assets.read_text", return_value="9.9.9\n"),
        ):
            assert cli._resolve_version() == "9.9.9"

    def test_all_sources_missing_reports_unknown(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cli, "_repo_version_path", lambda: "/nonexistent/VERSION")
        with (
            mock.patch("importlib.metadata.version", side_effect=Exception("not installed")),
            mock.patch("sw_core.assets.read_text", side_effect=FileNotFoundError),
        ):
            assert cli._resolve_version() == "unknown"


class TestVersionFlag:
    def test_version_flag_prints_and_exits_zero(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit) as ei:
            cli.build_parser().parse_args(["--version"])
        assert ei.value.code == 0
        assert capsys.readouterr().out.strip() == f"serialwrap {_REPO_VERSION}"


class TestProfileDirHelp:
    def test_daemon_start_help_shows_profile_dir_default(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit) as ei:
            cli.build_parser().parse_args(["daemon", "start", "--help"])
        assert ei.value.code == 0
        out = capsys.readouterr().out
        assert "profile YAML" in out
        # 路徑可能被 argparse 換行切開：去除所有空白後做子字串比對（路徑本身無空白）
        assert cli.PROFILE_DIR in "".join(out.split())


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
