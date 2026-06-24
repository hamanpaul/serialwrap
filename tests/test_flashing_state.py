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

    def test_flashing_blocks_destructive_management_ops(self):
        """FLASHING 期間 clear/release/bind/force-recover 一律回 FLASHING_BUSY、不 detach bridge、
        不改變 FLASHING 狀態，避免切斷正在進行的 MCU 燒錄 transport（#69 r5）。"""

        class _FakeBridge:
            def __init__(self):
                self.flash = False
                self.stopped = False
            def set_flash_mode(self, enabled):
                self.flash = enabled
            def list_consoles(self):
                return []
            def stop(self, *a, **k):
                self.stopped = True
                return {}

        for op in ("clear", "release", "bind", "recover_force"):
            with self.subTest(op):
                mgr = self._mgr([self._make_profile("p", "COM0", "lab+1", "/dev/serial/by-id/orig")])
                session = mgr.get_session("COM0")
                fb = _FakeBridge()
                session.bridge = fb
                mgr.enter_flashing("COM0")
                self.assertEqual(session.state, "FLASHING")

                if op == "clear":
                    res = mgr.clear_session("COM0")
                elif op == "release":
                    res = mgr.release_device("COM0")
                elif op == "bind":
                    res = mgr.bind_session("COM0", "/dev/serial/by-id/other")
                else:
                    res = mgr.recover_session("COM0", force=True)

                self.assertEqual(res.get("error_code"), "FLASHING_BUSY")
                self.assertEqual(session.state, "FLASHING")
                self.assertFalse(fb.stopped, f"{op} 不得在 FLASHING 期間 stop bridge")
                self.assertTrue(fb.flash, f"{op} 不得解除 flash 模式")

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


class TestFlashProbeCriticalGuard(_Base):
    """#83 RACE-2 final：flash 偵測 probe 窗口（enter_flashing 之前、state 仍非 FLASHING）內，
    destructive op（clear/release/bind）須一律回 FLASHING_BUSY，不得 detach/rebind bridge 中斷 probe。"""

    def _ready_mgr(self):
        mgr = self._mgr([self._make_profile("p", "COM0", "lab+1", "/dev/serial/by-id/orig")])
        # 模擬 probe 窗口：session 仍 READY/ATTACHED（非 FLASHING），但已標 flash-critical。
        mgr.get_session("COM0").state = "READY"
        return mgr

    def test_clear_rejected_during_probe_window(self):
        mgr = self._ready_mgr()
        mgr.mark_flash_critical("COM0")
        self.assertEqual(mgr.clear_session("COM0").get("error_code"), "FLASHING_BUSY")

    def test_release_rejected_during_probe_window(self):
        mgr = self._ready_mgr()
        mgr.mark_flash_critical("COM0")
        self.assertEqual(mgr.release_device("COM0").get("error_code"), "FLASHING_BUSY")

    def test_bind_rejected_during_probe_window(self):
        mgr = self._ready_mgr()
        mgr.mark_flash_critical("COM0")
        self.assertEqual(
            mgr.bind_session("COM0", "/dev/serial/by-id/new").get("error_code"), "FLASHING_BUSY"
        )

    def test_unmark_lifts_guard(self):
        mgr = self._ready_mgr()
        mgr.mark_flash_critical("COM0")
        mgr.unmark_flash_critical("COM0")
        # 解標後不再因 flash-critical 被擋（clear 於 READY+device 走正常 detach 路徑，非 FLASHING_BUSY）
        self.assertNotEqual(mgr.clear_session("COM0").get("error_code"), "FLASHING_BUSY")

    def test_other_com_not_affected(self):
        mgr = self._mgr([
            self._make_profile("p", "COM0", "lab+1", "/dev/serial/by-id/a"),
            self._make_profile("p", "COM1", "lab+2", "/dev/serial/by-id/b"),
        ])
        mgr.get_session("COM1").state = "READY"
        mgr.mark_flash_critical("COM0")                      # 只標 COM0
        self.assertNotEqual(mgr.clear_session("COM1").get("error_code"), "FLASHING_BUSY")

    def test_recover_force_rejected_during_probe_window(self):
        """Codex final [high]：force recover 也是 destructive 路徑（_force_recover detach/重連 bridge），
        probe 窗口須一律 FLASHING_BUSY。"""
        mgr = self._ready_mgr()
        mgr.mark_flash_critical("COM0")
        res = mgr.recover_session("COM0", force=True)
        self.assertEqual(res.get("error_code"), "FLASHING_BUSY")

    def test_collect_candidates_marks_atomically(self):
        """Codex final [high]：collect_flash_candidates_and_mark 須在回傳的同時已標記候選（snapshot+mark
        同鎖），杜絕 snapshot 與 mark 之間的 TOCTOU。"""
        import dataclasses
        prof = dataclasses.replace(
            self._make_profile("p", "COM0", "lab+1", "/dev/serial/by-id/a"), ready_probe=""
        )  # ready_probe="" → 非 command_capable → 合格 flash 候選
        self.assertFalse(prof.command_capable)
        mgr = self._mgr([prof])
        mgr.get_session("COM0").state = "READY"
        cands = mgr.collect_flash_candidates_and_mark()
        self.assertIn("COM0", [c["com"] for c in cands])     # 確為候選（非 trivial skip）
        # 回傳後立即就已是 critical（destructive op 被擋）→ 證明 snapshot 與 mark 原子完成
        self.assertEqual(mgr.clear_session("COM0").get("error_code"), "FLASHING_BUSY")
        mgr.unmark_flash_critical("COM0")
        self.assertNotEqual(mgr.clear_session("COM0").get("error_code"), "FLASHING_BUSY")

    def test_detach_by_id_skips_flash_critical(self):
        """Codex final2 [high]：device hotplug 在 flash 臨界區（probe/FLASHING）不得 detach——MCU reset
        期間 by_id 短暫消失常見，誤 detach 會切斷 transport。"""
        mgr = self._mgr([self._make_profile("p", "COM0", "lab+1", "/dev/serial/by-id/x")])
        s = mgr.get_session("COM0")
        s.state = "READY"
        mgr.mark_flash_critical("COM0")
        mgr._detach_by_id("/dev/serial/by-id/x", reason="DEVICE_REMOVED")
        self.assertEqual(mgr.get_session("COM0").state, "READY")   # 未被 detach

    def test_bridge_down_skips_flash_critical(self):
        """Codex final2 [high]：flash 臨界區不得因 bridge-down 回呼自動 detach+reattach 與 flash pump 對撞。"""
        mgr = self._mgr([self._make_profile("p", "COM0", "lab+1", "/dev/serial/by-id/x")])
        s = mgr.get_session("COM0")

        class _FB:
            def list_consoles(self): return []
        fb = _FB()
        s.bridge = fb
        s.state = "FLASHING"                                       # flash 進行中
        mgr._handle_bridge_down(s.session_id, fb, reason="EIO")
        self.assertEqual(mgr.get_session("COM0").state, "FLASHING")  # 未被搶進 ATTACHING
        self.assertIs(mgr.get_session("COM0").bridge, fb)            # bridge 未被 detach
