"""plugin 契約檔單測（agent-config／testbed example／pyproject／plugin.py 原始碼掃描）。

plugin.py 本體邏輯 bench 才驗（需 testpilot venv）；本檔只釘「檔案契約」——
不 import testpilot、不 import plugin。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "reliability"))

from serialwrap_reliability import testbed_loader  # noqa: E402

PKG = REPO_ROOT / "reliability" / "serialwrap_reliability"


def test_agent_config_locks_remediation_and_retry():
    cfg = yaml.safe_load((PKG / "agent-config.yaml").read_text(encoding="utf-8"))
    execution = cfg["execution"]
    assert execution["mode"] == "sequential"
    assert execution["max_concurrency"] == 1
    assert execution["retry"]["max_attempts"] == 1
    hooks = set(cfg["hooks"]["enabled_hooks"])
    assert {"pre_case", "post_case", "on_failure"} <= hooks
    assert "on_retry" not in hooks
    assert cfg["remediation"]["enabled"] is True
    assert cfg["remediation"]["allowed_actions"] == []


def test_testbed_example_equivalent_to_config_json():
    raw = yaml.safe_load((PKG / "testbed.yaml.example").read_text(encoding="utf-8"))
    cfg_yaml = testbed_loader.testbed_to_cfg(raw)
    cfg_json = testbed_loader.config_json_to_cfg(REPO_ROOT / "realhw" / "config.json")
    assert cfg_yaml["boards"] == cfg_json["boards"]
    for key in ("usbipd_exe", "win_serialwrap_exe", "tmux_prefix", "timeouts", "longrun"):
        assert cfg_yaml[key] == cfg_json[key], f"{key} 兩來源不一致"
    assert cfg_yaml["duration_s"] == 900
    assert cfg_yaml.get("win_serialwrap_exe")


def test_pyproject_entry_point_and_pin():
    text = (REPO_ROOT / "reliability" / "pyproject.toml").read_text(encoding="utf-8")
    assert 'name = "serialwrap-reliability"' in text
    assert 'version = "0.1.0"' in text
    assert '"testpilot.plugins"' in text
    assert 'serialwrap_reliability = "serialwrap_reliability.plugin:Plugin"' in text
    assert '"testpilot-core>=0.3.4,<1.0"' in text


def test_plugin_source_contract():
    text = (PKG / "plugin.py").read_text(encoding="utf-8")
    assert re.search(r'^\s*api_version\s*=\s*"1\.1"', text, re.M)
    assert re.search(r"from testpilot\.api import .*PluginBase", text)
    assert re.search(r"^\s*def\s+execution_policy\(", text, re.M)
    assert re.search(r"^\s*def\s+create_reporter\(", text, re.M)
    assert re.search(r"^\s*def\s+report_formats\(", text, re.M)
