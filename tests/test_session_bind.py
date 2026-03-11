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

    def test_clear_keeps_session_registration_and_binding(self) -> None:
        profiles = [self._make_profile("p", "COM0", "lab+1", "/dev/serial/by-id/orig")]
        mgr = SessionManager(profiles, WalWriter(wal_dir=self._tmp.name), on_ready=lambda _sid: None, on_detached=lambda _sid: None)

        resp = mgr.clear_session("COM0")
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["session"]["session_id"], "p:COM0")
        self.assertEqual(resp["session"]["device_by_id"], "/dev/serial/by-id/orig")
        self.assertEqual(resp["session"]["state"], "DETACHED")

        sessions = mgr.list_sessions()
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["session_id"], "p:COM0")
        self.assertEqual(sessions[0]["device_by_id"], "/dev/serial/by-id/orig")

    def test_clear_with_existing_device_triggers_attach(self) -> None:
        from sw_core.device_watcher import DeviceInfo
        import unittest.mock as mock

        profiles = [self._make_profile("p", "COM0", "lab+1", "/dev/serial/by-id/orig")]
        mgr = SessionManager(profiles, WalWriter(wal_dir=self._tmp.name), on_ready=lambda _sid: None, on_detached=lambda _sid: None)
        with mgr._lock:
            mgr._devices = {
                "/dev/serial/by-id/orig": DeviceInfo(
                    by_id="/dev/serial/by-id/orig",
                    real_path="/dev/ttyUSB0",
                )
            }

        with mock.patch.object(mgr, "_spawn_attach") as spawn_attach:
            resp = mgr.clear_session("COM0")

        self.assertTrue(resp["ok"])
        self.assertEqual(resp["session"]["state"], "ATTACHING")
        spawn_attach.assert_called_once_with("/dev/serial/by-id/orig")

    def test_execute_command_prompt_timeout_triggers_recovery(self) -> None:
        import unittest.mock as mock

        profiles = [self._make_profile("p", "COM0", "lab+1", "/dev/serial/by-id/orig")]
        mgr = SessionManager(profiles, WalWriter(wal_dir=self._tmp.name), on_ready=lambda _sid: None, on_detached=lambda _sid: None)
        session = mgr.get_session("COM0")
        self.assertIsNotNone(session)
        assert session is not None

        bridge = mock.MagicMock()
        bridge.rx_snapshot_len.side_effect = [10, 20]
        bridge.wait_for_regex_from.side_effect = [False, True]
        bridge.rx_text_from.return_value = "# "
        session.bridge = bridge
        session.state = "READY"

        resp = mgr.execute_command("p:COM0", "printf 'broken", "agent:test", "cid-1", timeout_s=0.1)

        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error_code"], "PROMPT_TIMEOUT_RECOVERED")
        self.assertEqual(resp["recovery_action"], "CTRL_C")
        self.assertEqual(bridge.send_command.call_count, 1)
        bridge.send_command.assert_any_call("printf 'broken", source="agent:test", cmd_id="cid-1")
        bridge.send_bytes.assert_called_once_with(b"\x03", source="system:recover", cmd_id=None)

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

        with mock.patch("sw_core.session_manager.UARTBridge") as MockBridge, \
             mock.patch("sw_core.session_manager.ensure_ready", return_value=(True, None)):
            bridge_inst = MockBridge.return_value
            bridge_inst.vtty_path = "/dev/pts/99"
            bridge_inst.start.return_value = None

            real_by_id = "/dev/serial/by-id/usb-FTDI_REAL-if00"
            real_device = DeviceInfo(by_id=real_by_id, real_path="/dev/ttyUSB0")
            mgr.update_devices({real_by_id: real_device})

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

    def test_multi_device_auto_bind_order(self) -> None:
        """兩顆裝置依序到來時，應按 act_no 升序分配給 COM0、COM1。"""
        from sw_core.device_watcher import DeviceInfo
        import unittest.mock as mock
        import time

        profiles = [
            self._make_profile("p", "COM0", "lab+1", "/dev/serial/by-id/placeholder0"),
            SessionProfile(
                profile_name="p",
                com="COM1",
                act_no=2,
                alias="lab+2",
                device_by_id="/dev/serial/by-id/placeholder1",
                platform="prpl",
                uart=UartProfile(),
            ),
        ]
        mgr = SessionManager(
            profiles,
            WalWriter(wal_dir=self._tmp.name),
            on_ready=lambda _sid: None,
            on_detached=lambda _sid: None,
        )

        dev0_by_id = "/dev/serial/by-id/usb-FTDI_AAA-if00"
        dev1_by_id = "/dev/serial/by-id/usb-FTDI_BBB-if00"

        with mock.patch("sw_core.session_manager.UARTBridge") as MockBridge, \
             mock.patch("sw_core.session_manager.ensure_ready", return_value=(True, None)):
            MockBridge.return_value.vtty_path = "/dev/pts/10"
            MockBridge.return_value.start.return_value = None

            # 兩顆裝置同時出現
            mgr.update_devices({
                dev0_by_id: DeviceInfo(by_id=dev0_by_id, real_path="/dev/ttyUSB0"),
                dev1_by_id: DeviceInfo(by_id=dev1_by_id, real_path="/dev/ttyUSB1"),
            })

            for _ in range(80):
                sessions = mgr.list_sessions()
                ready = [s for s in sessions if s["state"] == "READY"]
                if len(ready) == 2:
                    break
                time.sleep(0.05)

        sessions = sorted(mgr.list_sessions(), key=lambda s: s["act_no"])
        self.assertEqual(len(sessions), 2)
        # 每個 session 都被綁定到某個真實裝置（非佔位符）
        bound = {s["device_by_id"] for s in sessions}
        self.assertEqual(bound, {dev0_by_id, dev1_by_id})
        # COM0 (act_no=1) 應拿到字母序較小的裝置（sorted auto-bind 依 act_no 排序）
        self.assertEqual(sessions[0]["com"], "COM0")
        self.assertEqual(sessions[1]["com"], "COM1")
        self.assertEqual(sessions[0]["state"], "READY")
        self.assertEqual(sessions[1]["state"], "READY")

    def test_self_test_detects_real_path_rebind(self) -> None:
        from sw_core.device_watcher import DeviceInfo
        import unittest.mock as mock

        profiles = [self._make_profile("p", "COM0", "lab+1", "/dev/serial/by-id/orig")]
        mgr = SessionManager(profiles, WalWriter(wal_dir=self._tmp.name), on_ready=lambda _sid: None, on_detached=lambda _sid: None)
        session = mgr.get_session("COM0")
        assert session is not None

        bridge = mock.MagicMock()
        bridge.snapshot.return_value = {"running": True, "serial_alive": True, "vtty_alive": True, "vtty": "/dev/pts/9"}
        session.bridge = bridge
        session.state = "READY"
        session.attached_real_path = "/dev/ttyUSB0"
        with mgr._lock:
            mgr._devices = {
                "/dev/serial/by-id/orig": DeviceInfo(by_id="/dev/serial/by-id/orig", real_path="/dev/ttyUSB1")
            }

        resp = mgr.self_test("COM0")
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["classification"], "DEVICE_REBOUND_REQUIRED")

    def test_attach_console_requires_ready_session(self) -> None:
        profiles = [self._make_profile("p", "COM0", "lab+1", "/dev/serial/by-id/orig")]
        mgr = SessionManager(profiles, WalWriter(wal_dir=self._tmp.name), on_ready=lambda _sid: None, on_detached=lambda _sid: None)

        resp = mgr.attach_console("COM0")
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error_code"], "SESSION_NOT_READY")

    def test_bridge_down_triggers_reattach_when_device_still_exists(self) -> None:
        from sw_core.device_watcher import DeviceInfo
        import unittest.mock as mock

        profiles = [self._make_profile("p", "COM0", "lab+1", "/dev/serial/by-id/orig")]
        mgr = SessionManager(profiles, WalWriter(wal_dir=self._tmp.name), on_ready=lambda _sid: None, on_detached=lambda _sid: None)
        session = mgr.get_session("COM0")
        assert session is not None

        bridge = mock.MagicMock()
        session.bridge = bridge
        session.state = "READY"
        session.vtty_path = "/dev/pts/9"
        session.attached_real_path = "/dev/ttyUSB0"
        with mgr._lock:
            mgr._devices = {
                "/dev/serial/by-id/orig": DeviceInfo(by_id="/dev/serial/by-id/orig", real_path="/dev/ttyUSB0")
            }

        with mock.patch.object(mgr, "_spawn_attach") as spawn_attach:
            mgr._handle_bridge_down("p:COM0", bridge, "SERIAL_READ:5")

        self.assertEqual(session.state, "ATTACHING")
        self.assertIsNone(session.bridge)
        self.assertIsNone(session.vtty_path)
        spawn_attach.assert_called_once_with("/dev/serial/by-id/orig")


if __name__ == "__main__":
    unittest.main()
