"""Windows bundle 建置腳本在非 UTF-8 stdout 下印中文不得失敗。

GitHub hosted Windows runner 的 Python stdout 是 cp1252。release.yml 對 v0.4.0 預演補傳時，
mkbundle_windows.py validate 已通過，卻在印中文成功訊息時丟 UnicodeEncodeError，job 失敗。
腳本以 `python -I` 執行，-I 會忽略 PYTHONIOENCODING，只能在腳本內處理。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.core_portable, pytest.mark.process_rpc]

ROOT = Path(__file__).resolve().parent.parent


def _run_cp1252(*args: str) -> subprocess.CompletedProcess[bytes]:
    env = dict(os.environ, PYTHONIOENCODING="cp1252")
    env.pop("PYTHONUTF8", None)
    return subprocess.run([sys.executable, "-B", *args], env=env, capture_output=True, timeout=60, check=False)


def test_mkbundle_windows_help_survives_cp1252_stdout() -> None:
    result = _run_cp1252(str(ROOT / "scripts" / "rust" / "mkbundle_windows.py"), "--help")
    assert b"UnicodeEncodeError" not in result.stderr, result.stderr.decode("utf-8", "replace")
    assert result.returncode == 0
    assert "fingerprint" in result.stdout.decode("utf-8")


def test_check_win_imports_usage_survives_cp1252_stderr() -> None:
    result = _run_cp1252(str(ROOT / "scripts" / "rust" / "check_win_imports.py"))
    assert b"UnicodeEncodeError" not in result.stderr, result.stderr.decode("utf-8", "replace")
    assert result.returncode == 2
    assert "用法" in result.stderr.decode("utf-8")
