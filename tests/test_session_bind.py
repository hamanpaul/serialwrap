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


if __name__ == "__main__":
    unittest.main()
