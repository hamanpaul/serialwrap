import os
import tempfile
import unittest
from pathlib import Path

from sw_core.config import SessionProfile, UartProfile
from sw_core.device_watcher import DeviceInfo
from sw_core.session_manager import SessionManager
import sw_core.session_manager as sm_mod
from sw_core.wal import WalWriter


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._old_state_path = sm_mod.STATE_PATH
        sm_mod.STATE_PATH = str(Path(self._tmp.name) / "state.json")
        self.addCleanup(lambda: setattr(sm_mod, "STATE_PATH", self._old_state_path))

    def _make_profile(self, name: str, com: str, alias: str, by_id: str) -> SessionProfile:
        return SessionProfile(
            profile_name=name, com=com, act_no=1, alias=alias,
            device_by_id=by_id, platform="prpl", uart=UartProfile(),
        )

    def _mgr(self, profiles):
        return SessionManager(
            profiles, WalWriter(wal_dir=self._tmp.name),
            on_ready=lambda _s: None, on_detached=lambda _s: None,
        )


import time
import unittest.mock as mock


class TestReleasedStateFields(_Base):
    def test_session_has_released_fields_and_set(self) -> None:
        mgr = self._mgr([self._make_profile("p", "COM0", "lab+1", "/dev/serial/by-id/orig")])
        session = mgr.get_session("COM0")
        assert session is not None
        self.assertIsNone(session.released_by)
        self.assertIsNone(session.released_at)
        self.assertIsNone(session.released_reason)
        self.assertEqual(mgr._released_by_ids, set())


class TestSpawnAttachGuard(_Base):
    def test_spawn_attach_skips_released_by_id(self) -> None:
        by_id = "/dev/serial/by-id/orig"
        mgr = self._mgr([self._make_profile("p", "COM0", "lab+1", by_id)])
        with mgr._lock:
            mgr._released_by_ids.add(by_id)
        with mock.patch.object(mgr, "_attach_by_id") as attach_by_id:
            mgr._spawn_attach(by_id)
            time.sleep(0.1)
        attach_by_id.assert_not_called()
        self.assertNotIn(by_id, mgr._attach_inflight)

    def test_spawn_attach_runs_for_non_released_by_id(self) -> None:
        by_id = "/dev/serial/by-id/orig"
        mgr = self._mgr([self._make_profile("p", "COM0", "lab+1", by_id)])
        with mock.patch.object(mgr, "_attach_by_id") as attach_by_id:
            mgr._spawn_attach(by_id)
            time.sleep(0.1)
        attach_by_id.assert_called_once_with(by_id)


class TestDropConsolesDetach(_Base):
    def test_detach_drop_consoles_closes_and_does_not_stash(self) -> None:
        mgr = self._mgr([self._make_profile("p", "COM0", "lab+1", "/dev/serial/by-id/orig")])
        session = mgr.get_session("COM0")
        assert session is not None
        bridge = mock.MagicMock()
        bridge.stop.return_value = None
        session.bridge = bridge
        session.state = "READY"
        with mgr._lock:
            mgr._detach_session_locked(session, reason="RELEASED", drop_consoles=True)
        bridge.stop.assert_called_once_with(preserve_consoles=False)
        self.assertIsNone(session.retained_consoles)
        self.assertIsNone(session.bridge)

    def test_detach_default_preserves_consoles(self) -> None:
        mgr = self._mgr([self._make_profile("p", "COM0", "lab+1", "/dev/serial/by-id/orig")])
        session = mgr.get_session("COM0")
        assert session is not None
        bridge = mock.MagicMock()
        bridge.stop.return_value = None
        session.bridge = bridge
        session.state = "READY"
        with mgr._lock:
            mgr._detach_session_locked(session, reason="X")
        bridge.stop.assert_called_once_with(preserve_consoles=True)


class TestClearSessionReleasedGuard(_Base):
    def test_clear_on_released_session_is_noop(self) -> None:
        by_id = "/dev/serial/by-id/orig"
        mgr = self._mgr([self._make_profile("p", "COM0", "lab+1", by_id)])
        session = mgr.get_session("COM0")
        assert session is not None
        with mgr._lock:
            mgr._devices = {by_id: DeviceInfo(by_id=by_id, real_path="/dev/ttyUSB0")}
            mgr._released_by_ids.add(by_id)
            session.state = "RELEASED"
        with mock.patch.object(mgr, "_spawn_attach") as spawn_attach:
            resp = mgr.clear_session("COM0")
        self.assertTrue(resp["ok"])
        self.assertTrue(resp.get("released"))
        self.assertEqual(session.state, "RELEASED")
        spawn_attach.assert_not_called()


class TestReleaseDevice(_Base):
    def test_release_clean_slate_and_provenance(self) -> None:
        by_id = "/dev/serial/by-id/orig"
        mgr = self._mgr([self._make_profile("p", "COM0", "lab+1", by_id)])
        session = mgr.get_session("COM0")
        assert session is not None
        bridge = mock.MagicMock()
        bridge.stop.return_value = None
        bridge.list_consoles.return_value = [{"client_id": "c1"}]
        session.bridge = bridge
        session.state = "READY"
        with mgr._lock:
            mgr._devices = {by_id: DeviceInfo(by_id=by_id, real_path="/dev/ttyUSB0")}

        resp = mgr.release_device("COM0", source="agent:flash", reason="flash CC2674")

        self.assertTrue(resp["ok"])
        self.assertEqual(session.state, "RELEASED")
        self.assertEqual(session.released_by, "agent:flash")
        self.assertIsNotNone(session.released_at)
        self.assertEqual(session.released_reason, "flash CC2674")
        self.assertIn(by_id, mgr._released_by_ids)
        self.assertIsNone(session.retained_consoles)
        bridge.stop.assert_called_once_with(preserve_consoles=False)

    def test_release_idempotent(self) -> None:
        by_id = "/dev/serial/by-id/orig"
        mgr = self._mgr([self._make_profile("p", "COM0", "lab+1", by_id)])
        session = mgr.get_session("COM0")
        assert session is not None
        with mgr._lock:
            session.state = "RELEASED"
            mgr._released_by_ids.add(by_id)
        resp = mgr.release_device("COM0")
        self.assertTrue(resp["ok"])
        self.assertTrue(resp.get("already_released"))

    def test_release_session_not_found(self) -> None:
        mgr = self._mgr([self._make_profile("p", "COM0", "lab+1", "/dev/serial/by-id/orig")])
        resp = mgr.release_device("COM9")
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error_code"], "SESSION_NOT_FOUND")


class TestProbeExternalHolder(_Base):
    def test_probe_detects_holder_in_fake_proc(self) -> None:
        mgr = self._mgr([self._make_profile("p", "COM0", "lab+1", "/dev/serial/by-id/orig")])
        proc = Path(self._tmp.name) / "proc"
        (proc / "1234" / "fd").mkdir(parents=True)
        os.symlink("/dev/ttyUSB9", proc / "1234" / "fd" / "5")
        res = mgr._probe_external_holder("/dev/ttyUSB9", _proc_root=str(proc))
        self.assertEqual(res["pids"], [1234])
        self.assertEqual(res["holder"], 1234)

    def test_probe_no_holder(self) -> None:
        mgr = self._mgr([self._make_profile("p", "COM0", "lab+1", "/dev/serial/by-id/orig")])
        proc = Path(self._tmp.name) / "proc"
        (proc / "1234" / "fd").mkdir(parents=True)
        os.symlink("/dev/ttyUSB0", proc / "1234" / "fd" / "5")
        res = mgr._probe_external_holder("/dev/ttyUSB9", _proc_root=str(proc))
        self.assertEqual(res["pids"], [])
        self.assertIsNone(res["holder"])


class TestAttachDevice(_Base):
    def _released_mgr(self, by_id="/dev/serial/by-id/orig", real="/dev/ttyUSB0"):
        mgr = self._mgr([self._make_profile("p", "COM0", "lab+1", by_id)])
        session = mgr.get_session("COM0")
        with mgr._lock:
            mgr._devices = {by_id: DeviceInfo(by_id=by_id, real_path=real)}
            mgr._released_by_ids.add(by_id)
            session.state = "RELEASED"
            session.released_by = "agent:flash"
            session.released_at = "now"
        return mgr, session, by_id

    def test_attach_reclaims_when_free(self) -> None:
        mgr, session, by_id = self._released_mgr()
        with mock.patch.object(mgr, "_probe_external_holder", return_value={"pids": [], "holder": None}), \
             mock.patch.object(mgr, "_spawn_attach") as spawn_attach:
            resp = mgr.attach_device("COM0")
        self.assertTrue(resp["ok"])
        self.assertNotIn(by_id, mgr._released_by_ids)
        self.assertIsNone(session.released_by)
        self.assertEqual(session.state, "ATTACHING")
        spawn_attach.assert_called_once_with(by_id)

    def test_attach_refuses_when_externally_held(self) -> None:
        mgr, session, by_id = self._released_mgr()
        with mock.patch.object(mgr, "_probe_external_holder", return_value={"pids": [4321], "holder": 4321}), \
             mock.patch.object(mgr, "_spawn_attach") as spawn_attach:
            resp = mgr.attach_device("COM0")
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error_code"], "DEVICE_STILL_HELD")
        self.assertEqual(resp["pids"], [4321])
        self.assertEqual(session.state, "RELEASED")
        spawn_attach.assert_not_called()

    def test_attach_force_bypasses_holder_check(self) -> None:
        mgr, session, by_id = self._released_mgr()
        with mock.patch.object(mgr, "_probe_external_holder", return_value={"pids": [4321], "holder": 4321}) as probe, \
             mock.patch.object(mgr, "_spawn_attach") as spawn_attach:
            resp = mgr.attach_device("COM0", force=True)
        self.assertTrue(resp["ok"])
        probe.assert_not_called()
        spawn_attach.assert_called_once_with(by_id)

    def test_attach_device_not_present(self) -> None:
        mgr = self._mgr([self._make_profile("p", "COM0", "lab+1", "/dev/serial/by-id/orig")])
        session = mgr.get_session("COM0")
        with mgr._lock:
            session.state = "RELEASED"
            mgr._released_by_ids.add("/dev/serial/by-id/orig")
        resp = mgr.attach_device("COM0")
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error_code"], "DEVICE_NOT_PRESENT")


class TestReleasedPersistence(_Base):
    def test_released_survives_restart_and_bootstrap_skips(self) -> None:
        by_id = "/dev/serial/by-id/orig"
        profiles = [self._make_profile("p", "COM0", "lab+1", by_id)]
        mgr = self._mgr(profiles)
        session = mgr.get_session("COM0")
        bridge = mock.MagicMock()
        bridge.stop.return_value = None
        bridge.list_consoles.return_value = []
        session.bridge = bridge
        session.state = "READY"
        with mgr._lock:
            mgr._devices = {by_id: DeviceInfo(by_id=by_id, real_path="/dev/ttyUSB0")}
        mgr.release_device("COM0", source="agent:flash", reason="flash")

        # 模擬 daemon 重啟：同一 STATE_PATH、同 profiles 重建 SessionManager
        mgr2 = self._mgr(profiles)
        s2 = mgr2.get_session("COM0")
        assert s2 is not None
        self.assertEqual(s2.state, "RELEASED")
        self.assertEqual(s2.released_by, "agent:flash")
        self.assertIn(by_id, mgr2._released_by_ids)

        with mgr2._lock:
            mgr2._devices = {by_id: DeviceInfo(by_id=by_id, real_path="/dev/ttyUSB0")}
        with mock.patch.object(mgr2, "_attach_by_id") as attach_by_id:
            mgr2.bootstrap_attach()
            time.sleep(0.1)
        attach_by_id.assert_not_called()
        self.assertEqual(s2.state, "RELEASED")
