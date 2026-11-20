"""P5-T20 Step 0：既有套件改由 tests/ab_env 選擇 A/B artifact 的測試（測試資產變更，§10.4）。

被測套件：tests/test_multiagent_e2e.py、tests/test_human_agent_coexist.py。
- 以子行程載入套件模組（清除外層 A/B 選擇器），讀回其解析出的 CLI 與 daemon argv；
- 未設 env 時必須與修改前逐字相同（預設 [sys.executable, serialwrapd.py]、serialwrap）；
- 設 env 時 argv 指向所選 artifact；
- daemon /proc/<pid>/exe 與所選 artifact 不符時 pytest.fail（以 stub readlink 驗證）。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

# 被測套件本身為 posix_only（PTY／AF_UNIX）；模組層只放平台 marker，資源 marker 逐案標示（規則 2）。
pytestmark = [pytest.mark.posix_only]

TESTS_DIR = Path(__file__).resolve().parent
ROOT = TESTS_DIR.parent
SUITES = ["test_multiagent_e2e.py", "test_human_agent_coexist.py"]
AB_KEYS = ("SERIALWRAP_CLI_EXE", "SERIALWRAP_DAEMON_EXE", "SERIALWRAP_AB_LABEL", "SW_BASELINE_PYTHON")

_LOADER = """
import importlib.util, json, sys
sys.path.insert(0, sys.argv[2])
spec = importlib.util.spec_from_file_location("suite_under_test", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
print(json.dumps({"cli": mod.SERIALWRAP, "daemon": list(mod.SERIALWRAPD_ARGV)}))
"""


def _load_argv(suite: str, **overrides: str) -> dict:
    """以子行程載入套件，回傳 {"cli": SERIALWRAP, "daemon": SERIALWRAPD_ARGV}。"""
    env = {k: v for k, v in os.environ.items() if k not in AB_KEYS}
    env.update(overrides)
    proc = subprocess.run(
        [sys.executable, "-c", _LOADER, str(TESTS_DIR / suite), str(TESTS_DIR)],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, f"載入 {suite} 失敗：{proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.mark.process_rpc
@pytest.mark.parametrize("suite", SUITES)
def test_default_path_unchanged(suite):
    """未設 A/B env：argv 與修改前逐字相同。"""
    got = _load_argv(suite)
    assert got["cli"] == str(ROOT / "serialwrap")
    assert got["daemon"] == [sys.executable, str(ROOT / "serialwrapd.py")]


@pytest.mark.process_rpc
@pytest.mark.parametrize("suite", SUITES)
def test_env_selects_artifact(suite):
    """設 A/B env：argv[0] 為所選 artifact。"""
    got = _load_argv(
        suite,
        SERIALWRAP_CLI_EXE="/opt/sw/bin/serialwrap",
        SERIALWRAP_DAEMON_EXE="/opt/sw/bin/serialwrapd",
        SERIALWRAP_AB_LABEL="rust-s21",
    )
    assert got["cli"] == "/opt/sw/bin/serialwrap"
    assert got["daemon"] == ["/opt/sw/bin/serialwrapd"]


@pytest.mark.memory_only
def test_mismatch_detected():
    """/proc/<pid>/exe 與所選 artifact 不符 → pytest.fail；相符則不失敗。"""
    from ab_env import assert_daemon_artifact, expected_daemon_exe

    assert_daemon_artifact(os.getpid(), readlink=lambda _pid: expected_daemon_exe())  # 相符：不 fail
    with pytest.raises(pytest.fail.Exception, match="artifact mismatch"):
        assert_daemon_artifact(os.getpid(), readlink=lambda _pid: "/nonexistent/other-daemon")
