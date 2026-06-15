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
