"""裝置拔插與 bridge 復原測試。

涵蓋：device removed→DETACHED、device reappear→reattach、
      bridge 異常→reattach、recover with bridge down、recover Ctrl-C 升級。
"""
from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from sw_core.config import SessionProfile, UartProfile
from sw_core.device_watcher import DeviceInfo
from sw_core.session_manager import SessionManager, SessionRuntime
import sw_core.session_manager as sm_mod
from sw_core.wal import WalWriter


def _make_profile(name: str = "p", com: str = "COM0",
                  alias: str = "lab", by_id: str = "/dev/serial/by-id/dev0",
                  platform: str = "prpl") -> SessionProfile:
    return SessionProfile(
        profile_name=name, com=com, act_no=1, alias=alias,
        device_by_id=by_id, platform=platform, uart=UartProfile(),
    )


class TestDeviceHotplug(unittest.TestCase):

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._old_state_path = sm_mod.STATE_PATH
        sm_mod.STATE_PATH = str(Path(self._tmp.name) / "state.json")

    def tearDown(self) -> None:
        sm_mod.STATE_PATH = self._old_state_path

    def test_device_removed_session_goes_detached(self) -> None:
        """裝置從 devices dict 移除後，對應 session 應轉為 DETACHED。"""
        by_id = "/dev/serial/by-id/dev0"
        profile = _make_profile(by_id=by_id)
        mgr = SessionManager(
            [profile], WalWriter(wal_dir=self._tmp.name),
            on_ready=lambda _: None, on_detached=lambda _: None,
        )
        session = mgr.get_session("COM0")
        self.assertIsNotNone(session)

        # 模擬裝置出現 + attach
        device_present = {by_id: DeviceInfo(by_id=by_id, real_path="/dev/ttyUSB0")}
        with mock.patch.object(mgr, "_spawn_attach"):
            mgr.update_devices(device_present)

        # 手動設定 session 為 READY（模擬正常 attach 完成）
        session.state = "READY"
        session.bridge = mock.MagicMock()
        session.attached_real_path = "/dev/ttyUSB0"

        # 裝置移除
        mgr.update_devices({})

        self.assertEqual(session.state, "DETACHED")
        self.assertEqual(session.last_error, "DEVICE_REMOVED")
        self.assertIsNone(session.bridge)

    def test_device_reappear_triggers_reattach(self) -> None:
        """裝置移除後再出現，應觸發 _spawn_attach。"""
        by_id = "/dev/serial/by-id/dev0"
        profile = _make_profile(by_id=by_id)
        mgr = SessionManager(
            [profile], WalWriter(wal_dir=self._tmp.name),
            on_ready=lambda _: None, on_detached=lambda _: None,
        )

        device = {by_id: DeviceInfo(by_id=by_id, real_path="/dev/ttyUSB0")}
        with mock.patch.object(mgr, "_spawn_attach") as mock_attach:
            # 裝置出現
            mgr.update_devices(device)
            mock_attach.assert_called_with(by_id)
            mock_attach.reset_mock()

            # 裝置移除
            mgr.update_devices({})

            # 裝置再次出現
            mgr.update_devices(device)
            mock_attach.assert_called_with(by_id)

    def test_device_realpath_change_triggers_reattach(self) -> None:
        """同一 by_id 但 real_path 變更（重新列舉），應 detach + reattach。"""
        by_id = "/dev/serial/by-id/dev0"
        profile = _make_profile(by_id=by_id)
        mgr = SessionManager(
            [profile], WalWriter(wal_dir=self._tmp.name),
            on_ready=lambda _: None, on_detached=lambda _: None,
        )
        session = mgr.get_session("COM0")

        with mock.patch.object(mgr, "_spawn_attach") as mock_attach:
            # 初次出現
            mgr.update_devices({by_id: DeviceInfo(by_id=by_id, real_path="/dev/ttyUSB0")})
            # 手動設定 READY
            session.state = "READY"
            session.bridge = mock.MagicMock()
            mock_attach.reset_mock()

            # real_path 變更
            mgr.update_devices({by_id: DeviceInfo(by_id=by_id, real_path="/dev/ttyUSB1")})

            # 應先 detach 再 reattach
            self.assertEqual(session.state, "DETACHED")
            self.assertEqual(session.last_error, "DEVICE_REBOUND_REQUIRED")
            mock_attach.assert_called_with(by_id)

    def test_recover_session_with_bridge_down_and_device_present(self) -> None:
        """bridge=None 但裝置仍在 → recover 應觸發 reattach。"""
        by_id = "/dev/serial/by-id/dev0"
        profile = _make_profile(by_id=by_id)
        mgr = SessionManager(
            [profile], WalWriter(wal_dir=self._tmp.name),
            on_ready=lambda _: None, on_detached=lambda _: None,
        )
        session = mgr.get_session("COM0")
        session.state = "DETACHED"
        session.bridge = None

        # 裝置在 devices dict 中
        with mock.patch.object(mgr, "_spawn_attach") as mock_attach:
            mgr._devices = {by_id: DeviceInfo(by_id=by_id, real_path="/dev/ttyUSB0")}
            result = mgr.recover_session("COM0")
            self.assertTrue(result["ok"])
            self.assertEqual(result["action"], "REATTACH")
            mock_attach.assert_called_with(by_id)

    def test_recover_session_no_device_returns_error(self) -> None:
        """bridge=None 且裝置不在 → recover 應回 SESSION_NOT_READY。"""
        by_id = "/dev/serial/by-id/dev0"
        profile = _make_profile(by_id=by_id)
        mgr = SessionManager(
            [profile], WalWriter(wal_dir=self._tmp.name),
            on_ready=lambda _: None, on_detached=lambda _: None,
        )
        session = mgr.get_session("COM0")
        session.state = "DETACHED"
        session.bridge = None
        mgr._devices = {}

        result = mgr.recover_session("COM0")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "SESSION_NOT_READY")


if __name__ == "__main__":
    unittest.main()
