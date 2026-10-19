# tests/test_win_task_throwaway.py
"""Task P2-T18：Windows host 監管 >72h throwaway 排程工作的失敗測試（OD-15）。

驗證：
1. throwaway 工作名稱前綴 `serialwrap-throwaway-<run_id>`，嚴禁等於或衝突正式 `serialwrap` 工作；
   run_id 格式必須符合 `^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{6}$`。
2. 五個 `SERIALWRAP_*_DIR` 環境變數皆位於 `<localappdata>\\sw-throwaway\\<run_id>\\` 之下；
   `SERIALWRAP_TEST_DEVICE_ALLOWLIST` 為 COM20；RPC endpoint 為 loopback TCP 且 port ≠ 48700。
3. `win-task-throwaway.ps1 -WhatIf` 輸出的 Task XML 包含 `<ExecutionTimeLimit>PT0S</ExecutionTimeLimit>`、
   `<URI>\\serialwrap-throwaway-<rid></URI>`，所有路徑在隔離根下，且不含 `\\serialwrap</URI>`。
4. `-Cleanup` 僅移除 throwaway 排程工作，不影響正式 `serialwrap` 工作的存在與否。

同檔混合平台（P0 T14 規則 2）：
模組層不放平台與資源 marker。
- 純字串與環境變數函式：`core_portable` ＋ `memory_only`
- PowerShell 與 schtasks 原生排程器測試：`windows_native` ＋ `process_rpc`
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_throwaway_name_prefix_and_not_production():
    """工作名稱必須帶 serialwrap-throwaway- 前綴，不得為正式工作名稱，且 run_id 格式不合拋 ValueError。"""
    from realhw.acceptance.spike1_remaining import throwaway_task_name

    valid_id = "20261016T120000Z-a1b2c3"
    name = throwaway_task_name(valid_id)
    assert name == f"serialwrap-throwaway-{valid_id}"
    assert name != "serialwrap"
    assert not name.endswith("/serialwrap")
    assert not name.endswith("\\serialwrap")

    # run_id 格式不符 → ValueError
    with pytest.raises(ValueError):
        throwaway_task_name("x")

    with pytest.raises(ValueError):
        throwaway_task_name("")

    with pytest.raises(ValueError):
        throwaway_task_name("20261016T120000Z")  # 缺 hash 後綴


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_throwaway_env_all_under_isolated_root():
    """驗證五個 SERIALWRAP_*_DIR 在隔離目錄下、COM20 允許名單與獨立 TCP port。"""
    from realhw.acceptance.spike1_remaining import throwaway_env

    rid = "20261016T120000Z-a1b2c3"
    localappdata = r"C:\Users\u\AppData\Local"
    env = throwaway_env(rid, localappdata)

    expected_prefix_win = rf"{localappdata}\sw-throwaway\{rid}\\"
    expected_prefix_alt = rf"{localappdata}\sw-throwaway\{rid}/"
    expected_prefix_norm = rf"{localappdata}\sw-throwaway\{rid}"

    dir_keys = (
        "SERIALWRAP_STATE_DIR",
        "SERIALWRAP_RUN_DIR",
        "SERIALWRAP_CONFIG_DIR",
        "SERIALWRAP_WAL_DIR",
        "SERIALWRAP_LOG_DIR",
    )
    for k in dir_keys:
        assert k in env, f"缺少環境變數 {k}"
        val = env[k]
        assert (
            val.startswith(expected_prefix_win)
            or val.startswith(expected_prefix_alt)
            or val.startswith(expected_prefix_norm)
        ), f"{k}={val} 未在隔離目錄 {expected_prefix_norm} 下"
        assert "\\serialwrap\\" not in val and "/serialwrap/" not in val, f"{k}={val} 不得指向正式 serialwrap 目錄"

    assert env.get("SERIALWRAP_TEST_DEVICE_ALLOWLIST") == "COM20"

    assert "SERIALWRAP_SOCKET" in env
    sock = env["SERIALWRAP_SOCKET"]
    assert sock.startswith("tcp://127.0.0.1:"), f"SERIALWRAP_SOCKET 必須以 tcp://127.0.0.1: 開頭，得到 {sock}"
    port_str = sock.split(":")[-1]
    assert port_str.isdigit(), f"無效 port: {port_str}"
    port = int(port_str)
    assert port != 48700, f"RPC endpoint port 不得為 48700（正式 daemon port），得到 {port}"

    with pytest.raises(ValueError):
        throwaway_env("x", localappdata)


@pytest.mark.windows_native
@pytest.mark.process_rpc
def test_task_xml_whatif():
    """驗證 win-task-throwaway.ps1 -WhatIf 輸出的 Task XML 格式與隔離設定。"""
    ps = shutil.which("pwsh") or shutil.which("powershell")
    if not ps:
        pytest.skip("capability missing: pwsh or powershell not found")

    script = REPO_ROOT / "scripts" / "stage0" / "win-task-throwaway.ps1"
    assert script.exists(), f"腳本不存在: {script}"

    rid = "20261016T120000Z-a1b2c3"
    bundle_path = r"C:\b"
    cmd = [
        ps,
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script),
        "-RunId",
        rid,
        "-Hours",
        "72",
        "-Bundle",
        bundle_path,
        "-WhatIf",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f"-WhatIf 失敗 (rc={proc.returncode}):\nSTDOUT: {proc.stdout}\nSTDERR: {proc.stderr}"

    out = proc.stdout
    assert "<ExecutionTimeLimit>PT0S</ExecutionTimeLimit>" in out, "XML 必須含 <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>"
    assert f"<URI>\\serialwrap-throwaway-{rid}</URI>" in out or f"<URI>/serialwrap-throwaway-{rid}</URI>" in out, (
        f"XML 必須含 <URI>\\serialwrap-throwaway-{rid}</URI>"
    )
    assert r"\serialwrap</URI>" not in out and "/serialwrap</URI>" not in out, "XML 不得含正式工作名稱 \\serialwrap</URI>"
    assert f"sw-throwaway\\{rid}" in out or f"sw-throwaway/{rid}" in out, "所有 env 路徑必須在隔離根目錄下"


@pytest.mark.windows_native
@pytest.mark.process_rpc
def test_cleanup_removes_only_throwaway():
    """驗證 -Cleanup 僅移除 throwaway 工作，正式 serialwrap 工作狀態不變。"""
    if not shutil.which("schtasks"):
        pytest.skip("capability missing: schtasks not found")
    ps = shutil.which("pwsh") or shutil.which("powershell")
    if not ps:
        pytest.skip("capability missing: pwsh or powershell not found")

    script = REPO_ROOT / "scripts" / "stage0" / "win-task-throwaway.ps1"
    assert script.exists(), f"腳本不存在: {script}"

    rid = "20261016T120000Z-a1b2c3"
    task_name = f"serialwrap-throwaway-{rid}"

    # 記錄執行前正式 serialwrap 排程工作的存在狀態
    prod_query_before = subprocess.run(
        ["schtasks", "/Query", "/TN", "serialwrap"],
        capture_output=True,
    )
    prod_existed_before = prod_query_before.returncode == 0

    # 註冊 throwaway 工作
    register_cmd = [
        ps,
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script),
        "-RunId",
        rid,
        "-Hours",
        "72",
        "-Bundle",
        r"C:\b",
    ]
    reg_proc = subprocess.run(register_cmd, capture_output=True, text=True, timeout=30)
    assert reg_proc.returncode == 0, f"註冊 throwaway 工作失敗:\n{reg_proc.stderr}"

    # 執行 -Cleanup
    cleanup_cmd = [
        ps,
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script),
        "-RunId",
        rid,
        "-Cleanup",
    ]
    clean_proc = subprocess.run(cleanup_cmd, capture_output=True, text=True, timeout=30)
    assert clean_proc.returncode == 0, f"Cleanup 失敗:\n{clean_proc.stderr}"

    # 確認 throwaway 工作已被移除
    throwaway_query = subprocess.run(
        ["schtasks", "/Query", "/TN", task_name],
        capture_output=True,
    )
    assert throwaway_query.returncode != 0, f"throwaway 工作 {task_name} 於 cleanup 後仍然存在"

    # 確認正式 serialwrap 工作的狀態完全未變
    prod_query_after = subprocess.run(
        ["schtasks", "/Query", "/TN", "serialwrap"],
        capture_output=True,
    )
    prod_existed_after = prod_query_after.returncode == 0
    assert prod_existed_after == prod_existed_before, (
        f"正式 serialwrap 工作狀態在 cleanup 後發生改變: before={prod_existed_before}, after={prod_existed_after}"
    )
