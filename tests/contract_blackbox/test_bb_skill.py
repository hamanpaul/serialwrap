"""黑箱契約測試：skill 指令（P3-T15）。

驗證 skill 輸出與 Python 位元組相符、平台旗標切換、POSIX 與 Windows 原生換行處理。
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from bb_harness import repo_root, run_cli

pytestmark = [pytest.mark.process_rpc]

SKILL_MD_PATH = repo_root() / "sw_core" / "assets" / "skill" / "SKILL.md"
SKILL_WINDOWS_MD_PATH = repo_root() / "sw_core" / "assets" / "skill" / "SKILL_WINDOWS.md"


@pytest.mark.core_portable
def test_skill_matches_python(env):
    r_auto = run_cli(["skill"], env=env)
    assert r_auto.rc == 0

    r_linux = run_cli(["skill", "--platform", "linux"], env=env)
    assert r_linux.rc == 0

    r_win = run_cli(["skill", "--platform", "windows"], env=env)
    assert r_win.rc == 0


@pytest.mark.posix_only
@pytest.mark.skipif(os.name == "nt", reason="posix_only：僅 POSIX 平台執行")
def test_skill_default_platform_posix(env):
    r = run_cli(["skill"], env=env)
    assert r.rc == 0
    expected = SKILL_MD_PATH.read_bytes()
    assert r.stdout == expected


@pytest.mark.windows_native
@pytest.mark.skipif(os.name != "nt", reason="windows_native：僅原生 Windows 執行")
def test_skill_default_platform_windows(env):
    r = run_cli(["skill"], env=env)
    assert r.rc == 0
    raw = SKILL_WINDOWS_MD_PATH.read_bytes()
    expected = raw.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
    assert r.stdout == expected


@pytest.mark.core_portable
def test_skill_explicit_platform(env):
    nl = b"\r\n" if os.name == "nt" else b"\n"

    r_linux = run_cli(["skill", "--platform", "linux"], env=env)
    assert r_linux.rc == 0
    raw_linux = SKILL_MD_PATH.read_bytes()
    if os.name == "nt":
        expected_linux = raw_linux.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
    else:
        expected_linux = raw_linux
    assert r_linux.stdout == expected_linux

    r_win = run_cli(["skill", "--platform", "windows"], env=env)
    assert r_win.rc == 0
    raw_win = SKILL_WINDOWS_MD_PATH.read_bytes()
    if os.name == "nt":
        expected_win = raw_win.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
    else:
        expected_win = raw_win
    assert r_win.stdout == expected_win
