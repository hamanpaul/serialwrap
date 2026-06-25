import json
import tempfile
import unittest
from pathlib import Path

from sw_core.config import ProfileTemplate, SessionProfile, UartProfile
from sw_core.session_manager import DeviceInfo, SessionManager
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


class TestPriority(_Base):
    def _mgr_with_templates(self):
        from sw_core.config import ProfileTemplate
        prpl = ProfileTemplate(profile_name="prpl-template", platform="prpl",
                               prompt_regex="root@prplOS", login_regex="", password_regex="",
                               ready_probe="echo __R__", uart=UartProfile())
        others = ProfileTemplate(profile_name="others-template", platform="passthrough",
                                 prompt_regex=".*", login_regex="", password_regex="",
                                 ready_probe="", uart=UartProfile())
        mgr = SessionManager([], WalWriter(wal_dir=self._tmp.name),
                             templates=[prpl, others],
                             on_ready=lambda _sid: None, on_detached=lambda _sid: None)
        return mgr

    def test_pin_skips_probe(self):
        from sw_core.session_manager import DeviceInfo
        mgr = self._mgr_with_templates()
        key = "/dev/serial/by-id/usb-X"
        mgr._devices[key] = DeviceInfo(by_id=key, real_path="/dev/ttyUSB9")
        mgr._profile_pins[key] = "prpl-template"
        import sw_core.session_manager as m
        # 監看 UARTBridge 建構：pin 命中時不應建構 "PROBE" bridge（load-bearing）
        constructed = []
        orig_bridge = m.UARTBridge

        def _spy(name, *a, **k):
            constructed.append(name)
            return orig_bridge(name, *a, **k)

        m.UARTBridge = _spy
        self.addCleanup(lambda: setattr(m, "UARTBridge", orig_bridge))
        called = {"n": 0}
        orig_detect = m.detect_template
        m.detect_template = lambda *a, **k: called.__setitem__("n", called["n"] + 1) or None
        self.addCleanup(lambda: setattr(m, "detect_template", orig_detect))
        try:
            mgr._attach_by_id_dynamic(key)
        except Exception:
            pass
        sess = next((s for s in mgr._sessions.values() if s.profile.device_by_id == key), None)
        self.assertIsNotNone(sess)
        self.assertEqual(sess.profile_source, "pin")
        self.assertNotIn("PROBE", constructed)   # 關鍵：pin 命中不建 PROBE bridge（load-bearing）
        self.assertEqual(called["n"], 0)

    def test_sticky_skips_probe(self):
        from sw_core.session_manager import DeviceInfo
        mgr = self._mgr_with_templates()
        key = "/dev/serial/by-id/usb-Y"
        mgr._devices[key] = DeviceInfo(by_id=key, real_path="/dev/ttyUSB8")
        mgr._profile_detected[key] = "prpl-template"
        import sw_core.session_manager as m
        # 監看 UARTBridge 建構：sticky 命中時不應建構 "PROBE" bridge（load-bearing）
        constructed = []
        orig_bridge = m.UARTBridge

        def _spy(name, *a, **k):
            constructed.append(name)
            return orig_bridge(name, *a, **k)

        m.UARTBridge = _spy
        self.addCleanup(lambda: setattr(m, "UARTBridge", orig_bridge))
        called = {"n": 0}
        orig_detect = m.detect_template
        m.detect_template = lambda *a, **k: called.__setitem__("n", called["n"] + 1) or None
        self.addCleanup(lambda: setattr(m, "detect_template", orig_detect))
        try:
            mgr._attach_by_id_dynamic(key)
        except Exception:
            pass
        sess = next((s for s in mgr._sessions.values() if s.profile.device_by_id == key), None)
        self.assertEqual(sess.profile_source, "sticky")
        self.assertNotIn("PROBE", constructed)   # 關鍵：sticky 命中不建 PROBE bridge（load-bearing）
        self.assertEqual(called["n"], 0)

    def test_unknown_pin_falls_through(self):
        mgr = self._mgr_with_templates()
        self.assertIsNone(mgr._template_by_name("no-such"))


class TestStickyWrite(_Base):
    def _mgr_with_templates(self):
        prpl = ProfileTemplate(profile_name="prpl-template", platform="prpl",
                               prompt_regex="x", login_regex="", password_regex="",
                               ready_probe="echo __R__", uart=UartProfile())
        return SessionManager([], WalWriter(wal_dir=self._tmp.name), templates=[prpl],
                              on_ready=lambda _sid: None, on_detached=lambda _sid: None)

    def test_maybe_persist_sticky_writes_when_ready_detected(self):
        mgr = self._mgr_with_templates()
        key = "/dev/serial/by-id/usb-Z"
        mgr._devices[key] = DeviceInfo(by_id=key, real_path="/dev/ttyUSB1")
        mgr._maybe_persist_sticky(by_id=key, profile_name="prpl-template",
                                  source="detected", real_path="/dev/ttyUSB1")
        self.assertEqual(mgr._profile_detected.get(key), "prpl-template")

    def test_no_write_when_source_not_detected(self):
        mgr = self._mgr_with_templates()
        key = "/dev/serial/by-id/usb-Z"
        mgr._devices[key] = DeviceInfo(by_id=key, real_path="/dev/ttyUSB1")
        mgr._maybe_persist_sticky(by_id=key, profile_name="prpl-template",
                                  source="fallback", real_path="/dev/ttyUSB1")
        self.assertNotIn(key, mgr._profile_detected)

    def test_no_write_when_real_path_changed(self):
        mgr = self._mgr_with_templates()
        key = "/dev/serial/by-id/usb-Z"
        mgr._devices[key] = DeviceInfo(by_id=key, real_path="/dev/ttyUSB2")  # 已換
        mgr._maybe_persist_sticky(by_id=key, profile_name="prpl-template",
                                  source="detected", real_path="/dev/ttyUSB1")  # attach 當時
        self.assertNotIn(key, mgr._profile_detected)


class TestPinUnpin(_Base):
    def _mgr(self):
        prpl = ProfileTemplate(profile_name="prpl-template", platform="prpl",
                               prompt_regex="root@prplOS", login_regex="", password_regex="",
                               ready_probe="echo __R__", uart=UartProfile())
        return SessionManager([_profile()], WalWriter(wal_dir=self._tmp.name),
                              templates=[prpl],
                              on_ready=lambda _sid: None, on_detached=lambda _sid: None)

    def test_pin_valid_profile(self):
        mgr = self._mgr()
        key = "/dev/serial/by-id/usb-NEW-if00-port0"
        mgr._devices[key] = DeviceInfo(by_id=key, real_path="/dev/ttyUSB5")
        resp = mgr.pin_session(key, "prpl-template")  # 用 by-id 當 selector（無既有 session）
        self.assertTrue(resp["ok"])
        self.assertEqual(mgr._profile_pins[key], "prpl-template")

    def test_pin_unknown_profile_rejected(self):
        mgr = self._mgr()
        resp = mgr.pin_session("COM0", "no-such-template")
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error_code"], "UNKNOWN_PROFILE")

    def test_pin_explicit_target_rejected(self):
        mgr = self._mgr()  # _profile() 經 __init__ → COM0 session 為 yaml-target
        resp = mgr.pin_session("COM0", "prpl-template")
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error_code"], "PROFILE_IS_EXPLICIT")

    def test_unpin_keeps_sticky(self):
        mgr = self._mgr()
        # FIX B：COM0 是 yaml-target session，unpin 現在對稱回 PROFILE_IS_EXPLICIT。
        # 改用不屬於任何 yaml-target session 的獨立 by-id key 測試 sticky 保留行為。
        key = "/dev/serial/by-id/usb-FTDI_B-if00-port0"  # 非 yaml-target，無既有 session
        mgr._profile_pins[key] = "prpl-template"
        mgr._profile_detected[key] = "op3-template"
        resp = mgr.unpin_session(key)
        self.assertTrue(resp["ok"])
        self.assertNotIn(key, mgr._profile_pins)
        self.assertEqual(mgr._profile_detected.get(key), "op3-template")

    def test_pin_explicit_target_by_byid_rejected(self):
        # I1 回歸：用 yaml-target 裝置自己的 by-id 當 selector 也須擋（不可繞過 explicit guard）
        mgr = self._mgr()
        key = "/dev/serial/by-id/usb-FTDI_A-if00-port0"  # = _profile() 的 device_by_id（COM0 yaml-target）
        mgr._devices[key] = DeviceInfo(by_id=key, real_path="/dev/ttyUSB0")
        resp = mgr.pin_session(key, "prpl-template")
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error_code"], "PROFILE_IS_EXPLICIT")

    def test_unpin_rejects_explicit_target(self):
        # FIX B：unpin 與 pin 對稱，yaml-target 裝置應回 PROFILE_IS_EXPLICIT
        mgr = self._mgr()
        resp = mgr.unpin_session("COM0")
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error_code"], "PROFILE_IS_EXPLICIT")


class TestRpc(_Base):
    def test_rpc_pin_unpin_routed(self):
        import sw_core.service as svc_mod
        seen = {}
        class _S:
            def pin_session(self, sel, prof): seen["pin"] = (sel, prof); return {"ok": True}
            def unpin_session(self, sel): seen["unpin"] = sel; return {"ok": True}
        service = svc_mod.SerialwrapService.__new__(svc_mod.SerialwrapService)
        service._sessions = _S()
        r1 = service.rpc("session.pin", {"selector": "COM0", "profile": "prpl-template"})
        r2 = service.rpc("session.unpin", {"selector": "COM0"})
        self.assertTrue(r1["ok"]); self.assertTrue(r2["ok"])
        self.assertEqual(seen["pin"], ("COM0", "prpl-template"))
        self.assertEqual(seen["unpin"], "COM0")


class TestDeviceKey(_Base):
    def test_by_path_selector_used_as_key(self):
        prpl = ProfileTemplate(profile_name="prpl-template", platform="prpl",
                               prompt_regex="x", login_regex="", password_regex="",
                               ready_probe="echo __R__", uart=UartProfile())
        mgr = SessionManager([], WalWriter(wal_dir=self._tmp.name), templates=[prpl],
                             on_ready=lambda _sid: None, on_detached=lambda _sid: None)
        bypath = "/dev/serial/by-path/pci-0000:00:14.0-usb-0:1:1.0-port0"
        mgr._devices[bypath] = DeviceInfo(by_id=bypath, real_path="/dev/ttyUSB0")
        resp = mgr.pin_session(bypath, "prpl-template")
        self.assertTrue(resp["ok"])
        self.assertEqual(mgr._profile_pins[bypath], "prpl-template")

    def test_pin_rejects_unstable_ttyusb_selector(self):
        # FIX A：/dev/ttyUSB* 不是穩定 key，pin 應回 DEVICE_NOT_FOUND（不可寫成永不命中的 pin）
        prpl = ProfileTemplate(profile_name="prpl-template", platform="prpl",
                               prompt_regex="x", login_regex="", password_regex="",
                               ready_probe="echo __R__", uart=UartProfile())
        mgr = SessionManager([], WalWriter(wal_dir=self._tmp.name), templates=[prpl],
                             on_ready=lambda _sid: None, on_detached=lambda _sid: None)
        resp = mgr.pin_session("/dev/ttyUSB0", "prpl-template")
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error_code"], "DEVICE_NOT_FOUND")


class TestIntegration(_Base):
    def test_sticky_written_then_reused_after_restart(self):
        prpl = ProfileTemplate(profile_name="prpl-template", platform="prpl",
                               prompt_regex="x", login_regex="", password_regex="",
                               ready_probe="echo __R__", uart=UartProfile())
        mgr = SessionManager([], WalWriter(wal_dir=self._tmp.name), templates=[prpl],
                             on_ready=lambda _sid: None, on_detached=lambda _sid: None)
        key = "/dev/serial/by-id/usb-INT"
        mgr._devices[key] = DeviceInfo(by_id=key, real_path="/dev/ttyUSB0")
        with mgr._lock:
            mgr._maybe_persist_sticky(by_id=key, profile_name="prpl-template",
                                      source="detected", real_path="/dev/ttyUSB0")
        # 重啟：新 SessionManager 載入同 STATE_PATH
        mgr2 = SessionManager([], WalWriter(wal_dir=self._tmp.name), templates=[prpl],
                              on_ready=lambda _sid: None, on_detached=lambda _sid: None)
        self.assertEqual(mgr2._profile_detected.get(key), "prpl-template")
        import sw_core.session_manager as m
        called = {"n": 0}
        orig = m.detect_template
        m.detect_template = lambda *a, **k: called.__setitem__("n", called["n"] + 1) or None
        self.addCleanup(lambda: setattr(m, "detect_template", orig))
        mgr2._devices[key] = DeviceInfo(by_id=key, real_path="/dev/ttyUSB0")
        try:
            mgr2._attach_by_id_dynamic(key)
        except Exception:
            pass
        sess = next((s for s in mgr2._sessions.values() if s.profile.device_by_id == key), None)
        self.assertEqual(sess.profile_source, "sticky")
        self.assertEqual(called["n"], 0)

    def test_sticky_idempotent_no_redundant_save(self):
        prpl = ProfileTemplate(profile_name="prpl-template", platform="prpl",
                               prompt_regex="x", login_regex="", password_regex="",
                               ready_probe="echo __R__", uart=UartProfile())
        mgr = SessionManager([], WalWriter(wal_dir=self._tmp.name), templates=[prpl],
                             on_ready=lambda _sid: None, on_detached=lambda _sid: None)
        key = "/dev/serial/by-id/usb-IDEM"
        mgr._devices[key] = DeviceInfo(by_id=key, real_path="/dev/ttyUSB0")
        with mgr._lock:
            mgr._maybe_persist_sticky(by_id=key, profile_name="prpl-template",
                                      source="detected", real_path="/dev/ttyUSB0")
        calls = {"n": 0}
        orig_save = mgr._save_state
        mgr._save_state = lambda: calls.__setitem__("n", calls["n"] + 1)
        self.addCleanup(lambda: setattr(mgr, "_save_state", orig_save))
        with mgr._lock:  # 第二次同值
            mgr._maybe_persist_sticky(by_id=key, profile_name="prpl-template",
                                      source="detected", real_path="/dev/ttyUSB0")
        self.assertEqual(calls["n"], 0)  # 同值不應再 _save_state

    def test_cli_pin_unpin_parser_args(self):
        # 鎖定 CLI argparse → args 的 selector/profile 對應（service 單元測試未覆蓋此層）
        import sw_core.cli as cli_mod
        # 找出 parser 建構函式：cli.py 應有 build_parser() 或類似；用實際存在的那個
        parser = cli_mod.build_parser()
        ns = parser.parse_args(["session", "pin", "--selector", "COM0", "--profile", "prpl-template"])
        self.assertEqual(ns.selector, "COM0")
        self.assertEqual(ns.profile, "prpl-template")
        ns2 = parser.parse_args(["session", "unpin", "--selector", "COM0"])
        self.assertEqual(ns2.selector, "COM0")
