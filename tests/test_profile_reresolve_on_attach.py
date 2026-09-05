"""既有 session 重新 attach 前的 profile 再解析（#181）。

#95 建立的四層優先序（pin > sticky > detect > fallback）只實作在
``_attach_by_id_dynamic``——也就是「該裝置**從未**建過 session」的路徑。既有 session
走 ``_attach_by_id``，會原封不動沿用 ``session.profile``；而 ``clear_session`` 只 detach
bridge、不刪 session 物件，於是 ``session pin`` + ``session clear`` 對既有 session
完全沒有效果（#181 問題 1），掉進 ``others-template`` catch-all 的 session 也沒有任何
CLI 出口（#181 問題 2/3）。本檔驗證 ``_reresolve_profile_on_reattach`` 補上這條路徑。
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sw_core.config import ProfileTemplate, SessionProfile, UartProfile
from sw_core.session_manager import DeviceInfo, SessionManager
import sw_core.session_manager as sm_mod
from sw_core.wal import WalWriter

BY_ID = "/dev/serial/by-id/usb-1a86_USB_Serial-if00-port0"


def _tpl(name, platform, prompt_regex, ready_probe):
    return ProfileTemplate(profile_name=name, platform=platform,
                           prompt_regex=prompt_regex, login_regex="", password_regex="",
                           ready_probe=ready_probe, uart=UartProfile())


PRPL = _tpl("prpl-template", "prpl", r"(?m)^root@(prplOS|OpenWRT|OpenWrt):.*#", "echo __R__")
OTHERS = _tpl("others-template", "passthrough", ".*", "")


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._old = sm_mod.STATE_PATH
        sm_mod.STATE_PATH = str(Path(self._tmp.name) / "state.json")
        self.addCleanup(lambda: setattr(sm_mod, "STATE_PATH", self._old))

    def _mgr(self, profiles=(), templates=(PRPL, OTHERS)):
        return SessionManager(list(profiles), WalWriter(wal_dir=self._tmp.name),
                              templates=list(templates),
                              on_ready=lambda _sid: None, on_detached=lambda _sid: None)

    def _fallback_session(self, mgr, com="COM2", act_no=3, alias=None):
        """手動組出 #181 現場：掉進 others-template catch-all 的既有 dynamic session。"""
        profile = SessionProfile(
            profile_name="others-template", com=com, act_no=act_no,
            alias=alias if alias is not None else f"others-template+{act_no}",
            device_by_id=BY_ID, platform="passthrough",
            prompt_regex=".*", ready_probe="", uart=UartProfile(),
        )
        sid = f"others-template:{com}"
        session = sm_mod.SessionRuntime(session_id=sid, profile=profile)
        session.profile_source = "fallback"
        session.state = "DETACHED"
        mgr._sessions[sid] = session
        mgr._aliases.set_for_session(sid, profile.alias)
        mgr._devices[BY_ID] = DeviceInfo(by_id=BY_ID, real_path="/dev/ttyUSB2")
        return session

    def _no_detect(self):
        """讓 detect_template 一律回 None（不觸發偵測路徑），並記錄呼叫次數。"""
        calls = []
        orig = sm_mod.detect_template
        sm_mod.detect_template = lambda *a, **k: calls.append(1) or None
        self.addCleanup(lambda: setattr(sm_mod, "detect_template", orig))
        return calls

    def _no_bridge(self):
        """PROBE bridge 不得真的開裝置：換成不做事的替身，並記錄建構過的名稱。"""
        names = []

        class _FakeBridge:
            def __init__(self, name, *a, **k):
                names.append(name)

            def start(self):
                return None

            def stop(self):
                return None

        orig = sm_mod.UARTBridge
        sm_mod.UARTBridge = _FakeBridge
        self.addCleanup(lambda: setattr(sm_mod, "UARTBridge", orig))
        return names


class TestPinEscapeHatch(_Base):
    def test_pin_applies_to_existing_fallback_session(self):
        """#181 問題 1：pin 對既有 session 生效（原本被接受、被持久化，然後被忽略）。"""
        mgr = self._mgr()
        self._fallback_session(mgr)
        self._no_detect()
        mgr._profile_pins[BY_ID] = "prpl-template"

        mgr._reresolve_profile_on_reattach(BY_ID)

        session = next(s for s in mgr._sessions.values() if s.profile.device_by_id == BY_ID)
        self.assertEqual(session.profile.profile_name, "prpl-template")
        self.assertEqual(session.profile_source, "pin")
        self.assertEqual(session.profile.platform, "prpl")
        self.assertEqual(session.profile.ready_probe, "echo __R__")
        self.assertTrue(session.profile.command_capable)

    def test_pin_rekeys_session_id_and_keeps_com(self):
        """session_id 依新 profile_name 重算，COM／act_no／device_by_id 不變。"""
        mgr = self._mgr()
        self._fallback_session(mgr)
        self._no_detect()
        mgr._profile_pins[BY_ID] = "prpl-template"

        mgr._reresolve_profile_on_reattach(BY_ID)

        self.assertNotIn("others-template:COM2", mgr._sessions)
        self.assertIn("prpl-template:COM2", mgr._sessions)
        session = mgr._sessions["prpl-template:COM2"]
        self.assertEqual(session.session_id, "prpl-template:COM2")
        self.assertEqual(session.profile.com, "COM2")
        self.assertEqual(session.profile.act_no, 3)
        self.assertEqual(session.profile.device_by_id, BY_ID)

    def test_pin_hit_does_not_probe(self):
        """pin 的契約是「最高優先，繞過偵測」——不得開 PROBE bridge、不得跑 detect。"""
        mgr = self._mgr()
        self._fallback_session(mgr)
        calls = self._no_detect()
        names = self._no_bridge()
        mgr._profile_pins[BY_ID] = "prpl-template"

        mgr._reresolve_profile_on_reattach(BY_ID)

        self.assertEqual(calls, [])
        self.assertNotIn("PROBE", names)

    def test_pin_survives_manager_restart(self):
        """pin 已持久化；重啟後的第一次 re-attach 仍須套用（跨重啟的逃生口）。"""
        mgr = self._mgr()
        self._fallback_session(mgr)
        mgr._profile_pins[BY_ID] = "prpl-template"
        mgr._save_state()

        mgr2 = self._mgr()
        self.assertEqual(mgr2._profile_pins.get(BY_ID), "prpl-template")
        self._fallback_session(mgr2)
        self._no_detect()
        mgr2._reresolve_profile_on_reattach(BY_ID)
        self.assertEqual(
            mgr2._sessions["prpl-template:COM2"].profile_source, "pin")

    def test_auto_generated_alias_follows_new_profile(self):
        mgr = self._mgr()
        self._fallback_session(mgr)
        self._no_detect()
        mgr._profile_pins[BY_ID] = "prpl-template"

        mgr._reresolve_profile_on_reattach(BY_ID)

        session = mgr._sessions["prpl-template:COM2"]
        self.assertEqual(session.profile.alias, "prpl-template+3")
        aliases = {row["alias"] for row in mgr._aliases.list_alias()}
        self.assertIn("prpl-template+3", aliases)
        self.assertNotIn("others-template+3", aliases)

    def test_custom_alias_is_preserved(self):
        """使用者自訂過的 alias 不得被改名。"""
        mgr = self._mgr()
        self._fallback_session(mgr, alias="my-ch340")
        self._no_detect()
        mgr._profile_pins[BY_ID] = "prpl-template"

        mgr._reresolve_profile_on_reattach(BY_ID)

        session = mgr._sessions["prpl-template:COM2"]
        self.assertEqual(session.profile.alias, "my-ch340")
        self.assertIn("my-ch340", {row["alias"] for row in mgr._aliases.list_alias()})

    def test_pin_does_not_touch_yaml_target_session(self):
        """explicit YAML target 為權威宣告，pin 不得改動（與 pin_session 的 PROFILE_IS_EXPLICIT 一致）。"""
        profile = SessionProfile(profile_name="prpl-template", com="COM0", act_no=1,
                                 alias="prpl+1", device_by_id=BY_ID, platform="prpl",
                                 uart=UartProfile())
        mgr = self._mgr(profiles=[profile])
        mgr._devices[BY_ID] = DeviceInfo(by_id=BY_ID, real_path="/dev/ttyUSB2")
        self._no_detect()
        mgr._profile_pins[BY_ID] = "others-template"

        mgr._reresolve_profile_on_reattach(BY_ID)

        session = mgr._sessions["prpl-template:COM0"]
        self.assertEqual(session.profile.profile_name, "prpl-template")
        self.assertEqual(session.profile_source, "yaml-target")

    def test_pin_to_unknown_profile_is_a_noop(self):
        mgr = self._mgr()
        self._fallback_session(mgr)
        self._no_detect()
        mgr._profile_pins[BY_ID] = "no-such-template"

        mgr._reresolve_profile_on_reattach(BY_ID)

        session = mgr._sessions["others-template:COM2"]
        self.assertEqual(session.profile.profile_name, "others-template")
        self.assertEqual(session.profile_source, "fallback")

    def test_no_rekey_when_target_session_id_taken(self):
        """新 session_id 已被別的 session 佔用時不動作，避免把兩個 session 併成一個。"""
        mgr = self._mgr()
        self._fallback_session(mgr)
        squatter = SessionProfile(profile_name="prpl-template", com="COM2", act_no=9,
                                  alias="squatter", device_by_id="/dev/serial/by-id/other",
                                  platform="prpl", uart=UartProfile())
        mgr._sessions["prpl-template:COM2"] = sm_mod.SessionRuntime(
            session_id="prpl-template:COM2", profile=squatter)
        self._no_detect()
        mgr._profile_pins[BY_ID] = "prpl-template"

        mgr._reresolve_profile_on_reattach(BY_ID)

        self.assertIn("others-template:COM2", mgr._sessions)
        self.assertEqual(
            mgr._sessions["prpl-template:COM2"].profile.device_by_id,
            "/dev/serial/by-id/other")


class TestFallbackIsProvisional(_Base):
    def test_fallback_session_redetects_on_reattach(self):
        """#181 問題 2：fallback 是未經量測的分類，重新 attach 時再給一次偵測機會。"""
        mgr = self._mgr()
        self._fallback_session(mgr)
        self._no_bridge()
        orig = sm_mod.detect_template
        sm_mod.detect_template = lambda *a, **k: PRPL
        self.addCleanup(lambda: setattr(sm_mod, "detect_template", orig))

        mgr._reresolve_profile_on_reattach(BY_ID)

        session = mgr._sessions["prpl-template:COM2"]
        self.assertEqual(session.profile.profile_name, "prpl-template")
        self.assertEqual(session.profile_source, "detected")
        self.assertTrue(session.profile.command_capable)

    def test_fallback_stays_when_detection_still_fails(self):
        mgr = self._mgr()
        self._fallback_session(mgr)
        self._no_bridge()
        self._no_detect()

        mgr._reresolve_profile_on_reattach(BY_ID)

        session = mgr._sessions["others-template:COM2"]
        self.assertEqual(session.profile_source, "fallback")

    def test_no_redetect_during_boot_quiet(self):
        """#130：boot quiet window 內偵測會送 \\r 打斷 U-Boot autoboot，一律不做。"""
        mgr = self._mgr()
        session = self._fallback_session(mgr)
        session.arm_boot_quiet()
        self.assertTrue(session.boot_quiet_active())
        self._no_bridge()
        calls = self._no_detect()

        mgr._reresolve_profile_on_reattach(BY_ID)

        self.assertEqual(calls, [])
        self.assertIn("others-template:COM2", mgr._sessions)

    def test_detected_session_is_not_redetected(self):
        """detected／sticky 是量測過的分類，不重跑偵測。"""
        mgr = self._mgr()
        session = self._fallback_session(mgr)
        session.profile_source = "detected"
        self._no_bridge()
        calls = self._no_detect()

        mgr._reresolve_profile_on_reattach(BY_ID)

        self.assertEqual(calls, [])

    def test_no_redetect_when_bridge_open(self):
        """已有 bridge 時不得再開 PROBE bridge（two-reader）。"""
        mgr = self._mgr()
        session = self._fallback_session(mgr)
        session.bridge = object()
        names = self._no_bridge()
        calls = self._no_detect()

        mgr._reresolve_profile_on_reattach(BY_ID)

        self.assertEqual(calls, [])
        self.assertEqual(names, [])


class TestAttachWiring(_Base):
    def test_attach_by_id_invokes_reresolution(self):
        """_attach_by_id 這條既有 session 的路徑必須實際呼叫再解析（原本完全沒接上）。"""
        mgr = self._mgr()
        self._fallback_session(mgr)
        seen = []
        mgr._reresolve_profile_on_reattach = lambda by_id: seen.append(by_id)
        self._no_bridge()
        try:
            mgr._attach_by_id(BY_ID)
        except Exception:
            pass
        self.assertEqual(seen, [BY_ID])


class _SelfTestBase(_Base):
    """把 fallback session 佈置成 self_test 走得到 ATTACHED/passthrough 分支的樣子。"""

    def _attached_fallback(self, mgr, rx_tail):
        session = self._fallback_session(mgr)
        bridge = mock.MagicMock()
        bridge.snapshot.return_value = {
            "running": True, "serial_alive": True,
            "vtty_alive": True, "vtty": "/dev/pts/9",
        }
        bridge.rx_tail.return_value = rx_tail
        session.bridge = bridge
        session.state = "ATTACHED"
        session.attached_real_path = "/dev/ttyUSB2"
        return session


class TestFallbackDiagnostics(_SelfTestBase):
    """#181 問題 3／建議 3：self-test 應把「RX 內容比對所有 template」接起來，
    給出可執行的出口，而不是只回 console_attach 讓使用者以為只能改設定檔。"""

    def test_suggest_profile_from_rx_matches_template(self):
        mgr = self._mgr()
        bridge = mock.MagicMock()
        bridge.rx_tail.return_value = (
            "br-lan: entered promiscuous mode\nroot@prplOS:~# \nroot@prplOS:~# "
        )
        self.assertEqual(mgr._suggest_profile_from_rx(bridge), "prpl-template")

    def test_suggest_profile_returns_none_for_noise(self):
        mgr = self._mgr()
        bridge = mock.MagicMock()
        bridge.rx_tail.return_value = "wl0: random driver noise\n[  12.3] eth0 up\n"
        self.assertIsNone(mgr._suggest_profile_from_rx(bridge))

    def test_suggest_profile_skips_passthrough_templates(self):
        """others-template 的 prompt_regex 是 `.*`，恆真——絕不能被建議回去。"""
        mgr = self._mgr(templates=(OTHERS,))
        bridge = mock.MagicMock()
        bridge.rx_tail.return_value = "root@prplOS:~# "
        self.assertIsNone(mgr._suggest_profile_from_rx(bridge))

    def test_self_test_recommends_pin_for_fallback_session(self):
        mgr = self._mgr()
        self._attached_fallback(mgr, "br-lan: entered promiscuous mode\nroot@prplOS:~# ")

        resp = mgr.self_test("COM2")

        self.assertEqual(resp["classification"], "PASSTHROUGH")
        self.assertEqual(resp["recommended_action"], "pin_profile")
        self.assertEqual(resp["suggested_profile"], "prpl-template")
        self.assertIn("session pin", resp["hint"])
        self.assertIn("COM2", resp["hint"])

    def test_self_test_keeps_console_attach_when_nothing_matches(self):
        mgr = self._mgr()
        self._attached_fallback(mgr, "wl0: random driver noise\n")

        resp = mgr.self_test("COM2")

        self.assertEqual(resp["classification"], "PASSTHROUGH")
        self.assertEqual(resp["recommended_action"], "console_attach")
        self.assertNotIn("suggested_profile", resp)

    def test_self_test_does_not_suggest_for_explicit_passthrough(self):
        """顯式宣告的 passthrough（如 uboot-template target）是人為決定，不該被勸退。"""
        profile = SessionProfile(profile_name="others-template", com="COM2", act_no=3,
                                 alias="others-template+3", device_by_id=BY_ID,
                                 platform="passthrough", prompt_regex=".*",
                                 ready_probe="", uart=UartProfile())
        mgr = self._mgr(profiles=[profile])
        session = mgr.get_session("COM2")
        self.assertEqual(session.profile_source, "yaml-target")
        bridge = mock.MagicMock()
        bridge.snapshot.return_value = {
            "running": True, "serial_alive": True,
            "vtty_alive": True, "vtty": "/dev/pts/9",
        }
        bridge.rx_tail.return_value = "root@prplOS:~# "
        session.bridge = bridge
        session.state = "ATTACHED"
        session.attached_real_path = "/dev/ttyUSB2"
        mgr._devices[BY_ID] = DeviceInfo(by_id=BY_ID, real_path="/dev/ttyUSB2")

        resp = mgr.self_test("COM2")

        self.assertEqual(resp["recommended_action"], "console_attach")
        self.assertNotIn("suggested_profile", resp)


class TestNotCommandCapableHint(_Base):
    """#181 附帶：PROFILE_NOT_COMMAND_CAPABLE 的 hint 原本兩個講法都像在叫人去改設定檔。
    pin 修好之後，hint 應該直接寫出可用的動詞。"""

    def test_hint_names_the_usable_escape_hatch(self):
        mgr = self._mgr()
        session = self._fallback_session(mgr)
        session.bridge = mock.MagicMock()
        session.state = "ATTACHED"

        from sw_core.service import SerialwrapService

        with (
            mock.patch("sw_core.service.WalWriter"),
            mock.patch("sw_core.service.DeviceWatcher"),
        ):
            svc = SerialwrapService([])
        svc._sessions = mgr

        resp = svc.rpc("command.submit", {"selector": "COM2", "cmd": "ls"})

        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error_code"], "PROFILE_NOT_COMMAND_CAPABLE")
        hint = resp["hint"]
        self.assertIn("session pin", hint)
        self.assertIn("self-test", hint)
        # 明講 command_capable 只由 ready_probe 決定，避免使用者往「session 被佔用」排查
        self.assertIn("ready_probe", hint)
        self.assertIn("console", hint)


if __name__ == "__main__":
    unittest.main()
