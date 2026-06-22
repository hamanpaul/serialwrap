import os
import tempfile
import unittest
from pathlib import Path

from sw_core.config import SessionProfile, UartProfile
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


class TestFlashingState(_Base):
    def test_enter_flashing_sets_state(self):
        mgr = self._mgr([self._make_profile("p", "COM0", "lab+1", "/dev/serial/by-id/orig")])
        res = mgr.enter_flashing("COM0")
        self.assertTrue(res["ok"])
        self.assertEqual(mgr.get_session("COM0").state, "FLASHING")

    def test_cmd_submit_rejected_while_flashing(self):
        mgr = self._mgr([self._make_profile("p", "COM0", "lab+1", "/dev/serial/by-id/orig")])
        mgr.enter_flashing("COM0")
        sid = mgr.get_session("COM0").session_id
        res = mgr.execute_command(sid, "ls", "agent", "cid", timeout_s=1, mode="line")
        self.assertEqual(res.get("error_code"), "FLASHING_BUSY")

    def test_exit_flashing_restores_prev_state(self):
        mgr = self._mgr([self._make_profile("p", "COM0", "lab+1", "/dev/serial/by-id/orig")])
        prev = mgr.get_session("COM0").state          # DETACHED in fixture
        mgr.enter_flashing("COM0")
        res = mgr.exit_flashing("COM0")
        self.assertTrue(res["ok"])
        self.assertEqual(mgr.get_session("COM0").state, prev)

    def test_enter_flashing_does_not_close_bridge(self):
        # FLASHING 不得 detach：bridge 參考在 enter 前後一致（fixture 下兩者皆 None，但不可變動其他狀態）
        mgr = self._mgr([self._make_profile("p", "COM0", "lab+1", "/dev/serial/by-id/orig")])
        before = mgr.get_session("COM0").bridge
        mgr.enter_flashing("COM0")
        self.assertIs(mgr.get_session("COM0").bridge, before)

    def test_exit_flashing_clears_mode_even_if_state_clobbered(self):
        """防禦縱深（#69 r4）：即使競態路徑（command timeout recovery / probe）已把 state
        搶改出 FLASHING，exit_flashing 仍須無條件清除 bridge flash 模式，避免 bridge 永久卡
        flash、靜默丟棄所有非 flash 寫入。"""
        mgr = self._mgr([self._make_profile("p", "COM0", "lab+1", "/dev/serial/by-id/orig")])

        class _FakeBridge:
            def __init__(self):
                self.flash = False
            def set_flash_mode(self, enabled):
                self.flash = enabled
            def list_consoles(self):
                return []

        fb = _FakeBridge()
        session = mgr.get_session("COM0")
        session.bridge = fb
        mgr.enter_flashing("COM0")
        self.assertTrue(fb.flash)
        # 模擬競態：flash 仍進行中，但 state 被搶改回 ATTACHED。
        session.state = "ATTACHED"
        res = mgr.exit_flashing("COM0")
        self.assertTrue(res["ok"])
        self.assertFalse(fb.flash, "exit_flashing 必須清除 flash 模式，即使 state 已非 FLASHING")

    def test_transition_to_attached_refuses_flashing_or_released(self):
        """在途 command 的 timeout/recovery 走的 _transition_to_attached 不得把 FLASHING/
        RELEASED 打回 ATTACHED（否則 exit_flashing 提早 return、bridge 卡 flash；#69 r4）。"""
        mgr = self._mgr([self._make_profile("p", "COM0", "lab+1", "/dev/serial/by-id/orig")])
        session = mgr.get_session("COM0")
        for guarded in ("FLASHING", "RELEASED"):
            with self.subTest(guarded):
                session.state = guarded
                mgr._transition_to_attached(session, reason="CMD_TIMEOUT")
                self.assertEqual(session.state, guarded)


class TestFlashingBlocksInjection(_Base):
    def test_enter_exit_toggles_bridge_flash_mode(self):
        """enter/exit_flashing 須在 bridge 上開關 flash_mode，據以擋 console 注入（C2）。"""
        mgr = self._mgr([self._make_profile("p", "COM0", "lab+1", "/dev/serial/by-id/orig")])

        class _FakeBridge:
            def __init__(self):
                self.flash = False
            def set_flash_mode(self, enabled):
                self.flash = enabled
            def list_consoles(self):
                return []

        fb = _FakeBridge()
        mgr.get_session("COM0").bridge = fb
        mgr.enter_flashing("COM0")
        self.assertTrue(fb.flash)
        mgr.exit_flashing("COM0")
        self.assertFalse(fb.flash)
