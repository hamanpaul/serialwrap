"""SC-35a passthrough 狀態機失敗測試（P1-OC T5，OD-14 #2）。

規格來源：`docs/rust-refactor/plans/02b-stage0-owner-confirmations.md` Task T5。

- attach（初次、熱插、手動、recover 後重 attach）對 `platform=passthrough` 不登入、不 probe、
  不做 boot quiet gate，bridge 起來即 `ATTACHED`、`last_error` 為 None、零 TX。
- `SessionRuntime.passthrough_quiet_since` 於進入 `ATTACHED` 與每次 RX 時更新；TX 不更新。
- `ATTACHED` 連續 `PASSTHROUGH_READY_QUIET_S`（30 s）無 RX 自動升 `READY`（零 TX）。
- submit、self-test、recover、attach 的 passthrough 分支行為依 SC-35。

`PASSTHROUGH_READY_QUIET_S` 與其他本 Phase 新增名稱一律於函式內以 `_load()` 延遲 import，
使產品碼尚未實作時本模組仍可 collection。
"""

from __future__ import annotations

from unittest import mock

import pytest

import sw_core.session_manager as sm_mod
from sw_core.session_manager import DeviceInfo
from sw_core.uart_io import UARTBridge
from tests.stage0_fakes import FakePort, make_bridge, make_manager, make_profile

pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]

BY_ID = "/dev/serial/by-id/X"  # 與 tests/stage0_fakes.make_profile 預設 by_id 一致
REAL_PATH = "/dev/ttyUSB0"
UBOOT_PROMPT = "(?mi)^(=>|u-boot>|cfe>) $"
UBOOT_READY = "echo __READY__${nonce}"
UBOOT_BANNER = "U-Boot 2023.10 (Oct 01 2023 - 10:00:00 +0000)\r\n"
RUNNING_SNAPSHOT = {
    "running": True,
    "serial_alive": True,
    "vtty_alive": True,
    "vtty": "/dev/pts/0",
}


def _load():
    from sw_core.constants import PASSTHROUGH_READY_QUIET_S

    return PASSTHROUGH_READY_QUIET_S


@pytest.fixture(autouse=True)
def _isolate_default_state(tmp_path, monkeypatch):
    """SerialwrapService([]) 會建立預設 SessionManager，將預設 state 路徑導向 tmp。"""
    monkeypatch.setattr(sm_mod, "STATE_PATH", str(tmp_path / "default-state.json"))


def _attached_pt(tmp_path, **kw):
    """ATTACHED 的 passthrough session：預設 ready_probe 為空（command_capable 為 False）。

    `kw` 可覆寫任一 profile 欄位（例如 uboot 變體以 `ready_probe=UBOOT_READY` 覆寫）。
    """
    port = FakePort()
    overrides = {"ready_probe": ""}
    overrides.update(kw)
    mgr = make_manager(tmp_path, [make_profile(platform="passthrough", **overrides)])
    mgr._devices[BY_ID] = DeviceInfo(by_id=BY_ID, real_path=REAL_PATH)
    s = mgr.get_session("COM0")
    s.bridge, _ = make_bridge(tmp_path, port=port)
    s.state = "ATTACHED"
    s.passthrough_quiet_since = 1000.0
    return mgr, s, port


def _svc_for(mgr):
    """以 SerialwrapService 的 command.submit 入口驗證 gate，sessions 換成測試用 manager。"""
    from sw_core.service import SerialwrapService

    with mock.patch("sw_core.service.WalWriter"), mock.patch("sw_core.service.DeviceWatcher"):
        svc = SerialwrapService([])
    svc._sessions = mgr  # type: ignore[attr-defined]
    return svc


def test_quiet_constant_is_30():
    assert _load() == 30.0


def test_no_ready_before_window(tmp_path):
    mgr, s, port = _attached_pt(tmp_path)
    with mgr._lock:
        assert mgr._promote_quiet_passthrough_locked(1029.9) == []
    assert s.state == "ATTACHED"
    assert mgr.passthrough_ready_in_s(s, 1029.9) == 0.1


def test_ready_at_window_zero_tx(tmp_path):
    mgr, s, port = _attached_pt(tmp_path)
    with mgr._lock:
        assert mgr._promote_quiet_passthrough_locked(1030.0) == [s.session_id]
    assert s.state == "READY" and s.last_error is None and port.writes == []


def test_rx_restarts_window(tmp_path, monkeypatch):
    mgr, s, port = _attached_pt(tmp_path)
    monkeypatch.setattr("sw_core.session_manager.time.monotonic", lambda: 1020.0)
    mgr._mark_session_rx(s)
    assert s.passthrough_quiet_since == 1020.0
    with mgr._lock:
        assert mgr._promote_quiet_passthrough_locked(1049.9) == []
        assert mgr._promote_quiet_passthrough_locked(1050.0) == [s.session_id]


def test_tx_does_not_restart_window(tmp_path, monkeypatch):
    mgr, s, port = _attached_pt(tmp_path)
    monkeypatch.setattr("sw_core.session_manager.time.monotonic", lambda: 1020.0)
    mgr._mark_session_tx(s)
    assert s.passthrough_quiet_since == 1000.0
    # 非空洞化：TX 不重啟窗口，故以 1000.0 起算滿 30 s（1030.0）即須升 READY，而非延到 1050.0
    with mgr._lock:
        assert mgr._promote_quiet_passthrough_locked(1030.0) == [s.session_id]
    assert s.state == "READY" and port.writes == []


@pytest.mark.parametrize("blocker", ["draining", "released", "flash_critical", "no_bridge"])
def test_blocked_states_not_promoted(tmp_path, blocker):
    mgr, s, port = _attached_pt(tmp_path)
    by_id = s.profile.device_by_id
    if blocker == "draining":
        mgr._draining_by_ids[by_id] = mock.MagicMock()
    elif blocker == "released":
        s.state = "RELEASED"
    elif blocker == "flash_critical":
        mgr.mark_flash_critical(s.profile.com)
    else:
        s.bridge = None
    with mgr._lock:
        assert mgr._promote_quiet_passthrough_locked(2000.0) == []
    assert s.state != "READY"


def test_attach_passthrough_zero_tx_even_with_ready_probe(tmp_path, monkeypatch):
    port = FakePort()
    mgr = make_manager(
        tmp_path,
        [make_profile(platform="passthrough", prompt_regex=UBOOT_PROMPT, ready_probe=UBOOT_READY)],
    )
    mgr._devices[BY_ID] = DeviceInfo(by_id=BY_ID, real_path=REAL_PATH)
    s = mgr.get_session("COM0")

    def _forbidden(*_args, **_kwargs):
        raise AssertionError("passthrough attach 不得呼叫 ensure_ready／probe_ready")

    monkeypatch.setattr(sm_mod, "ensure_ready", _forbidden)
    monkeypatch.setattr(sm_mod, "probe_ready", _forbidden)
    monkeypatch.setattr(UARTBridge, "start", lambda self: None)
    with mock.patch("sw_core.uart_io.open_serial_port", return_value=port):
        mgr._attach_by_id(BY_ID)

    assert s.state == "ATTACHED"
    assert s.last_error is None
    assert port.writes == []


def test_ready_rx_does_not_demote_or_arm_quiet(tmp_path):
    mgr, s, port = _attached_pt(tmp_path)
    s.state = "READY"
    mgr._on_bridge_rx(s.session_id, UBOOT_BANNER.encode("utf-8"))
    assert s.state == "READY"
    assert s.boot_quiet_active() is False
    assert s.ready_reconfirm_pending is False


def test_submit_attached_passthrough_session_not_ready(tmp_path, monkeypatch):
    mgr, s, port = _attached_pt(tmp_path)
    svc = _svc_for(mgr)
    monkeypatch.setattr("sw_core.session_manager.time.monotonic", lambda: 1010.0)
    resp = svc.rpc("command.submit", {"selector": "COM0", "cmd": "ls"})
    # SerialwrapService.rpc 會於回應尾端補 daemon 版本欄位 version（#154），鍵序比對時排除。
    assert [k for k in resp if k != "version"] == ["ok", "error_code", "passthrough_ready_in_s", "session"]
    assert resp["error_code"] == "SESSION_NOT_READY"
    assert resp["passthrough_ready_in_s"] == 20.0


def test_non_passthrough_empty_probe_still_not_capable(tmp_path):
    mgr = make_manager(tmp_path, [make_profile(platform="prpl", ready_probe="")])
    s = mgr.get_session("COM0")
    s.bridge, _ = make_bridge(tmp_path, port=FakePort(), wal=mgr._wal)
    s.state = "ATTACHED"
    svc = _svc_for(mgr)
    resp = svc.rpc("command.submit", {"selector": "COM0", "cmd": "ls"})
    assert resp["error_code"] == "PROFILE_NOT_COMMAND_CAPABLE"
    assert "passthrough_ready_in_s" not in resp


def test_ready_passthrough_skips_quiet_gates(tmp_path):
    mgr, s, port = _attached_pt(tmp_path)
    s.state = "READY"
    s.ready_reconfirm_pending = True
    svc = _svc_for(mgr)
    svc._arbiter.register_session(s.session_id)
    try:
        resp = svc.rpc("command.submit", {"selector": "COM0", "cmd": "ls"})
        assert resp["ok"] is True
        assert "cmd_id" in resp
    finally:
        svc._arbiter.unregister_session(s.session_id, error_code="FLUSHED_BY_SHUTDOWN")


def test_self_test_ready_passthrough_zero_tx(tmp_path):
    mgr, s, port = _attached_pt(tmp_path)
    s.state = "READY"
    with mock.patch.object(s.bridge, "snapshot", return_value=RUNNING_SNAPSHOT):
        result = mgr.self_test("COM0")
    assert result["classification"] == "PASSTHROUGH"
    assert result["recommended_action"] == "none"
    assert port.writes == []


def test_self_test_attached_passthrough_has_ready_in(tmp_path):
    mgr, s, port = _attached_pt(tmp_path)
    with mock.patch.object(s.bridge, "snapshot", return_value=RUNNING_SNAPSHOT):
        result = mgr.self_test("COM0")
    assert result["classification"] == "PASSTHROUGH"
    assert "passthrough_ready_in_s" in result
    assert port.writes == []


def test_recover_passthrough_no_probe(tmp_path):
    mgr, s, port = _attached_pt(tmp_path, prompt_regex=UBOOT_PROMPT, ready_probe=UBOOT_READY)
    result = mgr.recover_session("COM0")
    assert result["action"] == "REPROBE"
    assert result["recovered"] is False
    assert result["error_code"] == "SESSION_NOT_READY"
    assert result.get("last_error") is None
    assert result["session"]["last_error"] is None
    assert port.writes == []


def test_attach_attached_passthrough_ok(tmp_path):
    mgr, s, port = _attached_pt(tmp_path, prompt_regex=UBOOT_PROMPT, ready_probe=UBOOT_READY)
    result = mgr.attach_session("COM0")
    assert result["ok"] is True
    assert "session" in result
    assert port.writes == []


def test_bridge_down_returns_to_attached_and_restarts_window(tmp_path, monkeypatch):
    mgr, s, port = _attached_pt(tmp_path)
    s.state = "READY"
    old_bridge = s.bridge
    mgr._handle_bridge_down(s.session_id, old_bridge, "test-bridge-down")

    monkeypatch.setattr("sw_core.session_manager.time.monotonic", lambda: 5000.0)
    monkeypatch.setattr(UARTBridge, "start", lambda self: None)
    with mock.patch("sw_core.uart_io.open_serial_port", return_value=FakePort()):
        mgr._attach_by_id(BY_ID)

    assert s.state == "ATTACHED"
    assert s.passthrough_quiet_since == 5000.0


def test_flash_candidates_include_ready_passthrough(tmp_path):
    mgr, s, port = _attached_pt(tmp_path)
    s.state = "READY"
    candidates = mgr.collect_flash_candidates_and_mark()
    assert [c["com"] for c in candidates] == ["COM0"]
    assert candidates[0]["command_capable"] is False
