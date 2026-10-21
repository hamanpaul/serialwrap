"""P9-T9 Canary collector 的 Windows 原生紅燈測試（對應 docs/rust-refactor/plans/10-stage3-canary.md Task 9）。

涵蓋 boot_id（Kernel-Boot 事件，非 tick 推算）、daemon 執行檔 hash（OpenProcess＋QueryFullProcessImageNameW）、
host_id（MachineGuid）。本檔只在原生 Windows 執行（conftest 對非 nt 平台加 SKIP）。
tools.stage3.* 一律於函式內 lazy import，使非 Windows 收集不受影響。
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

# 資源 marker 逐項標註：test_daemon_exe_sha256_of_child 會起子行程（process_rpc），其餘為 memory_only。
pytestmark = [pytest.mark.windows_native]


@pytest.mark.memory_only
def test_boot_id_from_kernel_boot_event():
    from tools.stage3.canary_collect import boot_id

    first = boot_id()
    assert first is not None  # wevtutil Microsoft-Windows-Kernel-Boot EventID=27 最新一筆
    assert boot_id() == first


@pytest.mark.memory_only
def test_boot_id_not_tick_derived(monkeypatch):
    from tools.stage3.canary_collect import boot_id

    before = boot_id()
    real_time = time.time
    monkeypatch.setattr(time, "time", lambda: real_time() + 0.6)  # 牆鐘前進 0.6 s
    after = boot_id()
    assert after == before  # 不得以 time.time()−GetTickCount64() 推算（次秒量化會翻動）


@pytest.mark.process_rpc
def test_daemon_exe_sha256_of_child():
    from tools.stage3 import jsonio
    from tools.stage3.canary_collect import daemon_exe_sha256

    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        assert daemon_exe_sha256(child.pid) == jsonio.sha256_file(Path(sys.executable))
    finally:
        child.kill()
        child.wait()


@pytest.mark.memory_only
def test_host_id_reads_machine_guid():
    import hashlib
    import socket
    import winreg

    from tools.stage3.canary_collect import host_id

    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography") as key:
        guid, _kind = winreg.QueryValueEx(key, "MachineGuid")
    expected = hashlib.sha256((socket.gethostname() + "\0" + guid).encode("utf-8")).hexdigest()[:16]
    assert host_id() == expected
