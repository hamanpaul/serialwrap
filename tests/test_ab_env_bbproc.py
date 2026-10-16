"""BB-proc 兩檔改讀 A/B 選擇器（ab_env.AB_ENV）驗證。

root conftest 會 pop SERIALWRAP_CLI_EXE／SERIALWRAP_DAEMON_EXE，故 BB-proc 模組必須讀 ab_env.AB_ENV。
受測的 test_multiagent_e2e 與 test_human_agent_coexist 為 posix_only，
本檔 exec_module 動態載入兩模組，故同屬 posix_only。
"""
import importlib.util, sys
from pathlib import Path
import pytest
import ab_env

pytestmark = [pytest.mark.posix_only, pytest.mark.memory_only]

ROOT = Path(__file__).resolve().parents[1]

@pytest.mark.parametrize("mod", ["test_multiagent_e2e", "test_human_agent_coexist"])
def test_bbproc_honors_cli_and_daemon_exe(monkeypatch, mod):
    monkeypatch.setattr(ab_env, "AB_ENV", {"SERIALWRAP_CLI_EXE": "/fake/cli", "SERIALWRAP_DAEMON_EXE": "/fake/d"})
    spec = importlib.util.spec_from_file_location(f"_probe_{mod}", ROOT / "tests" / f"{mod}.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    assert m.SERIALWRAP == "/fake/cli"
    assert m.SERIALWRAPD_ARGV == ["/fake/d"]


# 紅燈前即已成立（HEAD 的預設即 Python 基線），為回歸守護，不標 xfail（strict 下會變 XPASS 失敗）。
@pytest.mark.parametrize("mod", ["test_multiagent_e2e", "test_human_agent_coexist"])
def test_bbproc_defaults_to_python_baseline(monkeypatch, mod):
    monkeypatch.setattr(ab_env, "AB_ENV", {})
    spec = importlib.util.spec_from_file_location(f"_probe2_{mod}", ROOT / "tests" / f"{mod}.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    assert m.SERIALWRAP == str(ROOT / "serialwrap")
