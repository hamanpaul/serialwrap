"""run_longrun_container 的 CLI／daemon 路徑解析（OD-15 docker 48 h 長跑）。

長跑在無監管的 serialwrap image（系統 pip 安裝）執行；systemd image 才有 /opt/serialwrap/.venv。
解析順序：SW_BUNDLE → venv（存在時）→ PATH；皆無則明確失敗，不得默默使用不存在的路徑。
"""

from __future__ import annotations

import importlib.util
import pathlib

import pytest

pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]

_P = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "docker" / "run_longrun_container.py"


def _load():
    spec = importlib.util.spec_from_file_location("run_longrun_container", _P)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_bundle_env_wins(tmp_path):
    mod = _load()
    cli, daemon = mod.resolve_artifacts({"SW_BUNDLE": str(tmp_path)}, venv=tmp_path / "nope", which=lambda n: None)
    assert cli == tmp_path / "bin" / "serialwrap" and daemon == tmp_path / "bin" / "serialwrapd"


def test_venv_used_when_present(tmp_path):
    mod = _load()
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "serialwrap").write_text("")
    (tmp_path / "bin" / "serialwrapd").write_text("")
    cli, daemon = mod.resolve_artifacts({}, venv=tmp_path, which=lambda n: "/usr/local/bin/" + n)
    assert cli == tmp_path / "bin" / "serialwrap" and daemon == tmp_path / "bin" / "serialwrapd"


def test_path_fallback_when_no_venv(tmp_path):
    mod = _load()
    cli, daemon = mod.resolve_artifacts({}, venv=tmp_path / "nope", which=lambda n: "/usr/local/bin/" + n)
    assert cli == pathlib.Path("/usr/local/bin/serialwrap") and daemon == pathlib.Path("/usr/local/bin/serialwrapd")


def test_missing_everywhere_fails_loudly(tmp_path):
    mod = _load()
    with pytest.raises(FileNotFoundError):
        mod.resolve_artifacts({}, venv=tmp_path / "nope", which=lambda n: None)


def test_serialwrap_image_root_in_dialout():
    """長跑 preflight 要求 serialwrap doctor 非 advisory 項全綠；容器以 root 執行，
    root 須在 dialout 群組以符合 doctor 的 dialout 檢查（只改測試 image，不改產品基線）。"""
    dockerfile = pathlib.Path(__file__).resolve().parents[1] / "tools" / "docker" / "serialwrap.Dockerfile"
    text = dockerfile.read_text(encoding="utf-8")
    assert "usermod -aG dialout root" in text
