"""tests/test_windows_claim.py — Windows COM 接管 + 藍牙排除測試（#84 PORT-4）

涵蓋：
1. ``test_bluetooth_never_opened``：藍牙 COM（COM3）不出現在 ``WindowsDeviceSource.scan()``
   結果中——scan 階段即被排除，根本不會進入接管/開埠流程。
2. ``test_busy_port_retries_not_ready``：被佔用的 COM（``UARTBridge.start`` 拋
   ``PermissionError``）→ session 退回 DETACHED，不進 READY / RELEASED，
   不汙染狀態，下次可手動或透過 ``attach_device`` / ``clear_session`` 重試。
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

import sw_core.session_manager as sm_mod
from sw_core.config import SessionProfile, UartProfile
from sw_core.device_source import WindowsDeviceSource
from sw_core.device_watcher import DeviceInfo
from sw_core import device_source as ds
from sw_core.session_manager import SessionManager
from sw_core.wal import WalWriter


# ──────────────────────────────────────────────────────────────────────────────
# 藍牙排除測試（scan 層）
# ──────────────────────────────────────────────────────────────────────────────


def test_bluetooth_never_opened(monkeypatch):
    """藍牙 COM3 不出現在 scan() 結果中 → scan 階段就被排除，不可能被接管/開啟。

    COM8（\\Device\\Serial2）不是藍牙，應保留在掃描結果中。
    """
    monkeypatch.setattr(ds, "_read_serialcomm", lambda: {
        r"\Device\BthModem0": "COM3", r"\Device\Serial2": "COM8",
    })
    monkeypatch.setattr(ds, "_read_bt_ports", lambda: {"COM3"})
    devices = WindowsDeviceSource().scan()
    # 藍牙 COM3 不在掃描結果 → 不可能被接管/開啟
    assert "COM3" not in devices
    assert "COM8" in devices


# ──────────────────────────────────────────────────────────────────────────────
# 被佔用埠跳過測試（session attach 層）
# ──────────────────────────────────────────────────────────────────────────────


@pytest.fixture()
def tmp_state(tmp_path, monkeypatch):
    """重定向 STATE_PATH 至暫存目錄，避免汙染系統 state.json。"""
    monkeypatch.setattr(sm_mod, "STATE_PATH", str(tmp_path / "state.json"))
    return tmp_path


def test_busy_port_retries_not_ready(tmp_state, monkeypatch):
    """被佔用的 COM 埠（bridge.start 拋 PermissionError）→ session 退回 DETACHED。

    驗證重點：
    - session.state 為 DETACHED，不進 READY 也不污染 RELEASED。
    - session.last_error 含 ATTACH_FAILED 前綴（可辨識錯誤類別）。
    - session 物件仍存在於 SessionManager，可供下次 attach 重試。

    此測試直接呼叫 ``_attach_by_id`` 避免 thread join 複雜度；
    語意等同 _spawn_attach 在 attach thread 內呼叫的路徑。
    """
    from sw_core.uart_io import UARTBridge

    # 準備一個指向 COM8 的 YAML target profile
    profile = SessionProfile(
        profile_name="p",
        com="COM8",
        act_no=1,
        alias="lab+1",
        device_by_id="COM8",
        platform="passthrough",
        uart=UartProfile(),
    )
    mgr = SessionManager(
        [profile],
        WalWriter(wal_dir=str(tmp_state)),
        on_ready=lambda _sid: None,
        on_detached=lambda _sid: None,
    )

    # 模擬 DeviceWatcher 掃到 COM8（讓 _devices 知道裝置存在）
    with mgr._lock:
        mgr._devices["COM8"] = DeviceInfo(by_id="COM8", real_path=r"\\.\COM8")

    # 注入：bridge.start() 模擬埠被其他程式佔用（PermissionError）
    def _busy_start(self):  # type: ignore[override]
        raise PermissionError("[WinError 5] 拒絕存取。")

    monkeypatch.setattr(UARTBridge, "start", _busy_start)

    # 直接呼叫 attach 路徑（同步，不走 _spawn_attach thread）
    mgr._attach_by_id("COM8")

    # 驗證：session 退回 DETACHED，不進 READY / RELEASED
    session = mgr.get_session("COM8")
    assert session is not None, "session 物件應仍存在（可供重試）"
    assert session.state == "DETACHED", (
        f"被佔用埠 attach 失敗後應退回 DETACHED，實際 state={session.state!r}"
    )
    assert session.last_error is not None, "last_error 應有錯誤說明"
    assert "ATTACH_FAILED" in session.last_error, (
        f"last_error 應含 ATTACH_FAILED 前綴，實際 last_error={session.last_error!r}"
    )
    assert session.state != "RELEASED", "開埠失敗不得污染 RELEASED 狀態"
    assert session.bridge is None, "開埠失敗後 bridge 應為 None"
    assert "COM8" not in mgr._attach_inflight  # 開埠失敗後 inflight 已清除，下次可重試
