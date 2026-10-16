"""P3-T8: 錯誤碼與 session last_error 生成腳本測試。"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "rust" / "gen_error_codes.py"
pytestmark = [pytest.mark.core_portable, pytest.mark.process_rpc]


def test_p0_contracts_exist():
    """P0 contracts 必須交付，否則直接 FAIL 而非 SKIP。"""
    error_codes = ROOT / "contracts" / "error_codes.yaml"
    session_last_errors = ROOT / "contracts" / "session_last_errors.yaml"
    if not (error_codes.is_file() and session_last_errors.is_file()):
        pytest.fail("P0 contracts 未交付，證據不足")


def test_gen_error_codes_check():
    """執行 scripts/rust/gen_error_codes.py --check 必須 exit 0。"""
    error_codes = ROOT / "contracts" / "error_codes.yaml"
    session_last_errors = ROOT / "contracts" / "session_last_errors.yaml"
    if not (error_codes.is_file() and session_last_errors.is_file()):
        pytest.fail("P0 contracts 未交付，證據不足")

    res = subprocess.run(
        [sys.executable, str(SCRIPT), "--check"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 0, f"stdout: {res.stdout}\nstderr: {res.stderr}"
