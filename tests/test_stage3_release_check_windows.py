# tests/test_stage3_release_check_windows.py
"""P9-T4 WindowsProbe 實測（msvcrt 鎖、程序存活、原生 COM 獨占開啟）的紅燈測試。

本檔為 windows_native：只在原生 Windows 執行（POSIX 上 conftest 會 skip 全部項目）。
test_windows_com_exclusive 需擁有者 host 的 SW_TEST_COM_PAIR（BO-P9-15）：未設定時 skip，
並以 CAPABILITY_SKIP 標記為「證據不足」（非 PASS）。

windows_com marker 不得放在項目層（tests/test_resource_markers.py 禁止），故此處以 inline skip 處理 COM 條件。
"""
from __future__ import annotations

import importlib
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

import resource_report_plugin as _rrp

pytestmark = [pytest.mark.windows_native, pytest.mark.process_rpc]


def _rc() -> Any:
    return importlib.import_module("tools.stage3.release_check")


def test_windows_lock_probe(tmp_path: Path):
    probe = _rc().WindowsProbe()
    lock = tmp_path / "serialwrapd.lock"
    code = (
        "import msvcrt, sys, time\n"
        "f = open(sys.argv[1], 'a+b')\n"
        "f.write(b'0'); f.flush(); f.seek(0)\n"
        "msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)\n"
        "print('locked', flush=True)\n"
        "time.sleep(30)\n"
    )
    child = subprocess.Popen([sys.executable, "-c", code, str(lock)], stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "locked"
        assert probe.lock_acquirable(str(lock)) is False
    finally:
        child.kill()
        child.wait()
    assert probe.lock_acquirable(str(lock)) is True


def test_windows_pid_alive():
    probe = _rc().WindowsProbe()
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert probe.pid_alive(child.pid) is True
    finally:
        child.kill()
        child.wait()
    assert probe.pid_alive(child.pid) is False


def test_windows_com_exclusive(tmp_path: Path, request: pytest.FixtureRequest):
    pair = os.environ.get("SW_TEST_COM_PAIR", "").strip()
    if not pair:
        request.node.stash[_rrp.CAPABILITY_SKIP] = True
        pytest.skip("cap:windows_com 證據不足")
    com_a = pair.split(":")[0]
    probe = _rc().WindowsProbe()
    # 子程序以 share=0 獨占開啟 COMa（serialwrap 端）；父程序的 device_exclusive_open 應看到 ERROR_ACCESS_DENIED。
    code = (
        "import ctypes, sys, time\n"
        "GENERIC_READ_WRITE = 0xC0000000\n"
        "OPEN_EXISTING = 3\n"
        "k = ctypes.WinDLL('kernel32', use_last_error=True)\n"
        "k.CreateFileW.restype = ctypes.c_void_p\n"
        "h = k.CreateFileW(sys.argv[1], GENERIC_READ_WRITE, 0, None, OPEN_EXISTING, 0, None)\n"
        "if h in (None, 0, ctypes.c_void_p(-1).value):\n"
        "    sys.exit(3)\n"
        "print('opened', flush=True)\n"
        "time.sleep(30)\n"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", code, "\\\\.\\" + com_a], stdout=subprocess.PIPE, text=True
    )
    try:
        assert child.stdout.readline().strip() == "opened"
        assert probe.device_exclusive_open(com_a) is False
    finally:
        child.kill()
        child.wait()
    assert probe.device_exclusive_open(com_a) is True
