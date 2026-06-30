"""tests/test_windows_claim.py — Windows COM 接管 + 藍牙排除測試（#84 PORT-4）

涵蓋：
1. ``test_bluetooth_never_opened``：藍牙 COM（COM3）不出現在 ``WindowsDeviceSource.scan()``
   結果中——scan 階段即被排除，根本不會進入接管/開埠流程。
2. ``test_busy_port_retries_not_ready``：被佔用的 COM（``UARTBridge.start`` 拋
   ``PermissionError``）→ session 退回 DETACHED，不進 READY / RELEASED，
   不汙染狀態，下次可手動或透過 ``attach_device`` / ``clear_session`` 重試。
3. ``test_daemon_injects_windows_passthrough_template_when_no_profiles``：
   daemon._make_windows_passthrough_templates 在無 passthrough template 時注入預設，
   已有時不重複（#84 PORT-4 根因修正：POSIX 行為隔離）。
4. ``test_no_profiles_windows_passthrough_claim``：SessionManager 帶預設 passthrough
   template 時，update_devices 觸發動態接管 → COM8 成為 ATTACHED passthrough session
   （端對端機制驗證，不依賴真硬體）。
"""
from __future__ import annotations

import tempfile
import time
from pathlib import Path

import pytest

import sw_core.session_manager as sm_mod
from sw_core.config import ProfileTemplate, SessionProfile, UartProfile
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


# ──────────────────────────────────────────────────────────────────────────────
# daemon.py 注入函式單元測試（#84 PORT-4 根因修正）
# ──────────────────────────────────────────────────────────────────────────────


def test_daemon_injects_windows_passthrough_template_when_no_profiles():
    """daemon._make_windows_passthrough_templates 在 Windows daemon 無 passthrough template 時，
    自動注入 windows-default-passthrough；已有 passthrough 時不重複注入。

    此函式是 #84 PORT-4 根因修正的核心：空 templates → _attach_by_id 守衛
    ``if session is None and self._templates`` 為 False → 動態接管永遠跳過。
    注入後 templates 非空，守衛通過，_attach_by_id_dynamic 才能被呼叫。
    """
    from sw_core.daemon import _make_windows_passthrough_templates

    # 1. 無任何 template → 應注入預設 passthrough
    result = _make_windows_passthrough_templates([])
    assert len(result) == 1, "空 templates 應注入 1 個預設 passthrough"
    assert result[0].profile_name == "windows-default-passthrough"
    assert result[0].platform == "passthrough"
    assert not result[0].command_capable, "預設 passthrough 的 ready_probe 應為空（command_capable=False）"

    # 2. 已有 passthrough template → 不重複注入
    existing_pt = ProfileTemplate(
        profile_name="my-passthrough",
        platform="passthrough",
        ready_probe="",
    )
    result2 = _make_windows_passthrough_templates([existing_pt])
    assert len(result2) == 1, "已有 passthrough 不應重複注入"
    assert result2[0].profile_name == "my-passthrough", "應保留原有 template"

    # 3. 有非 passthrough template → 應追加注入
    other_tpl = ProfileTemplate(profile_name="shell-tpl", platform="shell")
    result3 = _make_windows_passthrough_templates([other_tpl])
    assert len(result3) == 2, "有其他 template 但無 passthrough 時應注入"
    assert any(t.platform == "passthrough" for t in result3), "注入後應有 passthrough template"


# ──────────────────────────────────────────────────────────────────────────────
# 端對端機制測試：SessionManager + 預設 passthrough template → COM8 被接管
# ──────────────────────────────────────────────────────────────────────────────


def test_no_profiles_windows_passthrough_claim(tmp_state, monkeypatch):
    """Windows daemon 無 profile 時，注入預設 passthrough template 後 COM8 應被動態接管為
    platform=passthrough、state=ATTACHED 的 session（不依賴真硬體）。

    對應 daemon.py 在 Windows（select_device_backend()=="win"）無任何 passthrough template
    時注入 windows-default-passthrough 的修正路徑。SessionManager 收到非空 templates 後，
    _attach_by_id 守衛通過，_attach_by_id_dynamic 走 fallback passthrough 建立 session。
    """
    from sw_core.uart_io import UARTBridge

    # 注入預設 passthrough template（模擬 daemon.py Windows 路徑注入結果）
    windows_default_tpl = ProfileTemplate(
        profile_name="windows-default-passthrough",
        platform="passthrough",
        ready_probe="",  # 空 → command_capable=False → 停在 ATTACHED
    )

    mgr = SessionManager(
        [],  # 無 YAML profiles（對應 Windows 無設定檔啟動）
        WalWriter(wal_dir=str(tmp_state)),
        templates=[windows_default_tpl],
        on_ready=lambda _sid: None,
        on_detached=lambda _sid: None,
    )

    # bridge.start() 靜默成功（不實際開 \\.\COM8；CI 環境無此 COM）
    monkeypatch.setattr(UARTBridge, "start", lambda self: None)

    # 觸發動態接管（_spawn_attach 在背景執行緒執行）
    mgr.update_devices({"COM8": DeviceInfo(by_id="COM8", real_path=r"\\.\COM8")})

    # 等 attach thread 完成（by_id 從 _attach_inflight 移除即完成，最多 5 秒）
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        with mgr._lock:
            done = "COM8" not in mgr._attach_inflight
        if done:
            break
        time.sleep(0.05)

    # 驗證：COM8 的裝置被動態接管為 passthrough session，停在 ATTACHED。
    # 動態 session 的 COM 編號為 daemon 自行分配（COM0, COM1, …），不是 Windows 原始 COM8；
    # 故以 device_by_id 搜尋，而非 get_session("COM8")。
    with mgr._lock:
        session = next(
            (s for s in mgr._sessions.values() if s.profile.device_by_id == "COM8"),
            None,
        )
    assert session is not None, "COM8 應被接管為動態 passthrough session"
    assert session.profile.platform == "passthrough", (
        f"session 應為 passthrough，實際 platform={session.profile.platform!r}"
    )
    assert session.state == "ATTACHED", (
        f"passthrough session 應停在 ATTACHED，實際 state={session.state!r}"
    )
