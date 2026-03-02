import tempfile
import unittest
from pathlib import Path

from sw_core.config import SessionProfile, UartProfile
from sw_core.session_manager import SessionManager
import sw_core.session_manager as sm_mod
from sw_core.wal import WalWriter


class TestSessionBind(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._old_state_path = sm_mod.STATE_PATH
        sm_mod.STATE_PATH = str(Path(self._tmp.name) / "state.json")

    def tearDown(self) -> None:
        sm_mod.STATE_PATH = self._old_state_path

    def _make_profile(self, name: str, com: str, alias: str, by_id: str) -> SessionProfile:
        return SessionProfile(
            profile_name=name,
            com=com,
            act_no=1,
            alias=alias,
            device_by_id=by_id,
            platform="prpl",
            uart=UartProfile(),
        )

    def test_bind_updates_device_without_yaml_edit(self) -> None:
        profiles = [self._make_profile("p", "COM0", "lab+1", "/dev/serial/by-id/orig")]
        mgr = SessionManager(profiles, WalWriter(wal_dir=self._tmp.name), on_ready=lambda _sid: None, on_detached=lambda _sid: None)

        resp = mgr.bind_session("COM0", "/dev/serial/by-id/new")
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["session"]["device_by_id"], "/dev/serial/by-id/new")
        self.assertEqual(resp["session"]["state"], "DETACHED")
        self.assertEqual(resp["session"]["last_error"], "DEVICE_NOT_FOUND")

    def test_bind_rejects_duplicate_device(self) -> None:
        profiles = [
            self._make_profile("p1", "COM0", "lab+1", "/dev/serial/by-id/a"),
            self._make_profile("p2", "COM1", "lab+2", "/dev/serial/by-id/b"),
        ]
        mgr = SessionManager(profiles, WalWriter(wal_dir=self._tmp.name), on_ready=lambda _sid: None, on_detached=lambda _sid: None)

        resp = mgr.bind_session("COM0", "/dev/serial/by-id/b")
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error_code"], "DEVICE_ALREADY_BOUND")

    def test_attach_returns_device_not_found_when_missing(self) -> None:
        profiles = [self._make_profile("p", "COM0", "lab+1", "/dev/serial/by-id/missing")]
        mgr = SessionManager(profiles, WalWriter(wal_dir=self._tmp.name), on_ready=lambda _sid: None, on_detached=lambda _sid: None)
        resp = mgr.attach_session("COM0")
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error_code"], "DEVICE_NOT_FOUND")

    def test_auto_bind_on_device_attach(self) -> None:
        """裝置 by-id 不符合 profile 佔位符時，_attach_by_id 應自動綁定並更新 device_by_id。"""
        from sw_core.device_watcher import DeviceInfo
        import unittest.mock as mock

        profiles = [self._make_profile("p", "COM0", "lab+1", "/dev/serial/by-id/placeholder")]
        ready_called: list[str] = []
        mgr = SessionManager(
            profiles,
            WalWriter(wal_dir=self._tmp.name),
            on_ready=lambda sid: ready_called.append(sid),
            on_detached=lambda _sid: None,
        )

        real_by_id = "/dev/serial/by-id/usb-FTDI_REAL-if00"
        real_device = DeviceInfo(by_id=real_by_id, real_path="/dev/ttyUSB0")
        mgr.update_devices({real_by_id: real_device})

        # update_devices 後，session 的 device_by_id 應已被自動綁定
        # (attach 本身需要真實 serial port，這裡 mock UARTBridge + ensure_ready 跳過實體 attach)
        with mock.patch("sw_core.session_manager.UARTBridge") as MockBridge, \
             mock.patch("sw_core.session_manager.ensure_ready", return_value=(True, None)):
            bridge_inst = MockBridge.return_value
            bridge_inst.vtty_path = "/dev/pts/99"
            bridge_inst.start.return_value = None

            # 等 spawn_attach 執行緒完成
            import time
            for _ in range(50):
                sessions = mgr.list_sessions()
                if sessions and sessions[0]["state"] == "READY":
                    break
                time.sleep(0.05)

        sessions = mgr.list_sessions()
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["device_by_id"], real_by_id)
        self.assertEqual(sessions[0]["state"], "READY")

        # binding 應已寫入 state.json
        import json
        state = json.loads(Path(sm_mod.STATE_PATH).read_text())
        self.assertEqual(state["bindings"]["p:COM0"], real_by_id)


if __name__ == "__main__":
    unittest.main()
