"""#155 plugin 檔案契約單測——不 import testpilot、不 import plugin。

plugin.py 本體邏輯 bench 才驗（需 testpilot venv）；本檔只釘檔案層契約
（三大契約陷阱的設定值、entry-point、testbed 範例可載入）。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "regression"))

from serialwrap_regression import core  # noqa: E402

PLUGIN_ROOT = REPO_ROOT / "regression" / "serialwrap_regression"


def test_plugin_files_exist():
    for name in ("plugin.py", "core.py", "harness.py", "guards.py", "preflight.py",
                 "reporter.py", "agent-config.yaml", "testbed.yaml.example"):
        assert (PLUGIN_ROOT / name).is_file(), name


def test_pyproject_entry_point():
    text = (REPO_ROOT / "regression" / "pyproject.toml").read_text(encoding="utf-8")
    assert '"testpilot.plugins"' in text
    assert "serialwrap_regression.plugin:Plugin" in text
    assert '"testpilot-core>=0.3.4,<1.0"' in text


def test_agent_config_contract_traps():
    """三大契約陷阱釘死：remediation enabled、retry=1、on_failure hook。"""
    cfg = yaml.safe_load((PLUGIN_ROOT / "agent-config.yaml").read_text(encoding="utf-8"))
    assert cfg["remediation"]["enabled"] is True  # false → 所有 FAIL 變 Inconclusive
    assert cfg["execution"]["retry"]["max_attempts"] == 1  # 缺省 core 預設 2
    assert "on_failure" in cfg["hooks"]["enabled_hooks"]  # snapshot 通道
    assert cfg["execution"]["mode"] == "sequential"
    assert cfg["execution"]["max_concurrency"] == 1


def test_plugin_execute_step_always_success():
    """execute_step 失敗會跳過 evaluate——薄殼必須恆回 success=True。"""
    text = (PLUGIN_ROOT / "plugin.py").read_text(encoding="utf-8")
    assert re.search(r"from testpilot\.api import .*PluginBase", text)
    assert '"success": True' in text
    assert '"success": False' not in text


def test_testbed_example_loads_with_boards(tmp_path):
    # 複製到隔離目錄再載入：plugin 目錄若存在本機 testbed.yaml（bench 實跑產物、
    # gitignored）會覆蓋 example，直接原地載入會讓本測試依環境浮動。
    example = tmp_path / "testbed.yaml.example"
    example.write_text((PLUGIN_ROOT / "testbed.yaml.example").read_text(encoding="utf-8"),
                       encoding="utf-8")
    cfg = core.load_testbed(example)
    assert cfg["allow_destructive"] is False
    coms = [b["com"] for b in cfg["boards"]]
    assert coms == ["COM0", "COM1"]
    assert cfg["serialwrap_exe"].endswith("/.local/bin/serialwrap")
