import json
import tempfile
import unittest
from pathlib import Path

from sw_core.config import SessionProfile, UartProfile
from sw_core.session_manager import SessionManager
import sw_core.session_manager as sm_mod
from sw_core.wal import WalWriter


def _profile(name="prpl-template", com="COM0", alias="prpl+1",
             by_id="/dev/serial/by-id/usb-FTDI_A-if00-port0", platform="prpl"):
    return SessionProfile(profile_name=name, com=com, act_no=1, alias=alias,
                          device_by_id=by_id, platform=platform, uart=UartProfile())


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._old = sm_mod.STATE_PATH
        sm_mod.STATE_PATH = str(Path(self._tmp.name) / "state.json")
        self.addCleanup(lambda: setattr(sm_mod, "STATE_PATH", self._old))

    def _mgr(self, profiles):
        return SessionManager(profiles, WalWriter(wal_dir=self._tmp.name),
                              on_ready=lambda _sid: None, on_detached=lambda _sid: None)


class TestPersistence(_Base):
    def test_backward_compat_load_without_new_keys(self):
        Path(sm_mod.STATE_PATH).write_text(
            json.dumps({"aliases": {}, "bindings": {}, "released": {}}), encoding="utf-8")
        mgr = self._mgr([_profile()])
        self.assertEqual(mgr._profile_pins, {})
        self.assertEqual(mgr._profile_detected, {})

    def test_pins_persist_across_restart(self):
        mgr = self._mgr([_profile()])
        mgr._profile_pins["/dev/serial/by-id/x"] = "prpl-template"
        mgr._save_state()
        mgr2 = self._mgr([_profile()])
        self.assertEqual(mgr2._profile_pins, {"/dev/serial/by-id/x": "prpl-template"})

    def test_init_save_does_not_wipe_new_keys(self):
        Path(sm_mod.STATE_PATH).write_text(json.dumps({
            "aliases": {}, "bindings": {}, "released": {},
            "profile_pins": {"/dev/serial/by-id/x": "prpl-template"},
            "profile_detected": {"/dev/serial/by-id/y": "op3-template"},
        }), encoding="utf-8")
        self._mgr([_profile()])  # __init__ 尾段會 _save_state()
        on_disk = json.loads(Path(sm_mod.STATE_PATH).read_text(encoding="utf-8"))
        self.assertEqual(on_disk["profile_pins"], {"/dev/serial/by-id/x": "prpl-template"})
        self.assertEqual(on_disk["profile_detected"], {"/dev/serial/by-id/y": "op3-template"})

    def test_yaml_target_session_profile_source(self):
        mgr = self._mgr([_profile()])
        sess = mgr._sessions["prpl-template:COM0"]
        self.assertEqual(sess.profile_source, "yaml-target")
        self.assertEqual(sess.to_public_dict()["profile_source"], "yaml-target")
