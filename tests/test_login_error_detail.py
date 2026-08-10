"""#174 — last_error_detail 帶 rx tail 回歸測試。

login FSM 失敗碼（``login_fsm.LOGIN_FSM_DETAIL_ERRORS``）過去 ``last_error_detail``
恆為 ``null``，等於把唯一能定案的證據丟掉。本檔驗證
``SessionManager._refine_probe_failure``（attach/recover/reprobe/自動登入五處呼叫
共用的單一 choke point）在未翻轉為 ``TRANSPORT_STALL`` 時，把失敗當下的 rx tail
（清除控制碼／ANSI 後截尾 300 字元）附到 detail；非 login FSM 錯誤碼與既有
TRANSPORT_STALL 精煉行為維持不變（回歸線）。
"""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from typing import Any

from sw_core.config import SessionProfile, UartProfile
from sw_core.device_watcher import DeviceInfo
from sw_core.session_manager import SessionManager, SessionRuntime
import sw_core.session_manager as sm_mod
from sw_core.wal import WalWriter


class _DetailBridge:
    """``_refine_probe_failure`` 用 fake bridge：可控 rx_total_bytes／rx_tail。"""

    def __init__(self, *, rx_total: int = 0, tail: str = "") -> None:
        self._rx_total = rx_total
        self.tail = tail
        self.device_path = "/dev/ttyFAKE0"

    def rx_total_bytes(self) -> int:
        return self._rx_total

    def rx_tail(self, max_chars: int = 4096) -> str:
        return self.tail


class _NoRxTailBridge:
    """不支援 rx_tail 的舊 fake bridge（getattr 防禦回歸線）。"""

    def __init__(self, *, rx_total: int = 0) -> None:
        self._rx_total = rx_total
        self.device_path = "/dev/ttyFAKE0"

    def rx_total_bytes(self) -> int:
        return self._rx_total


def _make_profile(**overrides: Any) -> SessionProfile:
    defaults: dict[str, Any] = {
        "profile_name": "p",
        "com": "COM0",
        "act_no": 1,
        "alias": "lab+1",
        "device_by_id": "/dev/serial/by-id/fake",
        "platform": "bcm",
        "uart": UartProfile(),
    }
    defaults.update(overrides)
    return SessionProfile(**defaults)


class _ManagerTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._old_state_path = sm_mod.STATE_PATH
        sm_mod.STATE_PATH = str(Path(self._tmp.name) / "state.json")
        self.addCleanup(self._restore_state_path)

    def _restore_state_path(self) -> None:
        sm_mod.STATE_PATH = self._old_state_path

    def _make_manager(self, **profile_overrides: Any) -> tuple[SessionManager, SessionRuntime]:
        profile = _make_profile(**profile_overrides)
        mgr = SessionManager(
            [profile],
            WalWriter(wal_dir=self._tmp.name),
            on_ready=lambda _sid: None,
            on_detached=lambda _sid: None,
        )
        session = mgr.get_session("COM0")
        assert session is not None
        with mgr._lock:
            mgr._devices = {
                profile.device_by_id: DeviceInfo(
                    by_id=profile.device_by_id,
                    real_path="/dev/ttyFAKE0",
                )
            }
        return mgr, session


class TestLoginFsmErrorDetail(_ManagerTestBase):
    def test_login_required_gets_rx_tail_detail(self) -> None:
        mgr, session = self._make_manager()
        bridge = _DetailBridge(rx_total=100, tail="\x1b[31m(none) login: \x1b[0m")
        err, detail = mgr._refine_probe_failure(session, bridge, "LOGIN_REQUIRED", rx_before=0)
        self.assertEqual(err, "LOGIN_REQUIRED")
        self.assertEqual(detail, "(none) login: ")

    def test_post_login_cmd_timeout_gets_rx_tail_detail_when_not_stalled(self) -> None:
        """有 RX 流量（rx_delta != 0）→ 不翻轉 TRANSPORT_STALL，改附 rx tail。"""
        mgr, session = self._make_manager()
        bridge = _DetailBridge(rx_total=200, tail="login: ")
        err, detail = mgr._refine_probe_failure(session, bridge, "POST_LOGIN_CMD_TIMEOUT", rx_before=50)
        self.assertEqual(err, "POST_LOGIN_CMD_TIMEOUT")
        self.assertEqual(detail, "login: ")

    def test_detail_truncated_to_last_300_chars(self) -> None:
        mgr, session = self._make_manager()
        long_tail = "x" * 500 + "TAIL_MARKER"
        bridge = _DetailBridge(rx_total=1, tail=long_tail)
        err, detail = mgr._refine_probe_failure(session, bridge, "LOGIN_REQUIRED", rx_before=0)
        self.assertEqual(err, "LOGIN_REQUIRED")
        assert detail is not None
        self.assertLessEqual(len(detail), 300)
        self.assertTrue(detail.endswith("TAIL_MARKER"))

    def test_empty_rx_tail_yields_none_detail(self) -> None:
        mgr, session = self._make_manager()
        bridge = _DetailBridge(rx_total=1, tail="")
        err, detail = mgr._refine_probe_failure(session, bridge, "LOGIN_REQUIRED", rx_before=0)
        self.assertEqual(err, "LOGIN_REQUIRED")
        self.assertIsNone(detail)

    def test_bridge_without_rx_tail_does_not_crash(self) -> None:
        """舊 fake bridge（無 rx_tail）→ detail None、不拋例外（getattr 防禦）。"""
        mgr, session = self._make_manager()
        bridge = _NoRxTailBridge(rx_total=1)
        err, detail = mgr._refine_probe_failure(session, bridge, "LOGIN_REQUIRED", rx_before=0)
        self.assertEqual(err, "LOGIN_REQUIRED")
        self.assertIsNone(detail)

    def test_non_login_fsm_error_keeps_none_detail(self) -> None:
        """非 login FSM 錯誤碼（不在 LOGIN_FSM_DETAIL_ERRORS）→ detail 仍為 None（回歸線）。"""
        mgr, session = self._make_manager()
        bridge = _DetailBridge(rx_total=1, tail="some tail text")
        err, detail = mgr._refine_probe_failure(session, bridge, "DEVICE_NOT_FOUND", rx_before=0)
        self.assertEqual(err, "DEVICE_NOT_FOUND")
        self.assertIsNone(detail)

    def test_transport_stall_flip_keeps_its_own_hint_not_rx_tail(self) -> None:
        """真正翻轉為 TRANSPORT_STALL 時，detail 仍是既有 host 復原提示，不被 rx tail 蓋掉。"""
        mgr, session = self._make_manager()
        session.last_rx_mono = time.monotonic() - 60.0  # 已凍結超過 30s 門檻
        bridge = _DetailBridge(rx_total=0, tail="login: ")  # rx_delta == 0（全程零 echo）
        err, detail = mgr._refine_probe_failure(session, bridge, "BCM_PROMPT_TIMEOUT", rx_before=0)
        self.assertEqual(err, "TRANSPORT_STALL")
        assert detail is not None
        self.assertNotEqual(detail, "login: ")
        self.assertIn("USB", detail)


if __name__ == "__main__":
    unittest.main()
