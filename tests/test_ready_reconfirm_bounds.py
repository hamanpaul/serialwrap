"""#162 回歸修：``ready_reconfirm_pending`` 的有界化與明確失敗終態。

背景（實機事故 2026-07-31，f9-quiet-window-agent-passthrough 9 分鐘零收斂）：
#162 讓 agent 顯式命令的 gate 改判 ``agent_gate_active()``＝quiet **或**
``ready_reconfirm_pending``，但 pending 只有一個 clearer（``confirm_ready()``，
且要求對 UART probe 成功），既沒有 deadline、沒有 attempt 上限、也沒有失敗終態。
只要確認 probe 的任一守衛長期不成立，pending 就永久 True，
``AUTOBOOT_QUIET``（可重試、帶 ``retry_after_s``）於是變成
**一個永遠不可能成功的「可重試」錯誤**——呼叫端的重試迴圈被拖進無界迴圈。

已識別的四條永久阻塞路徑（本檔逐一釘住其「必落終態」）：
  a. ``reprobe_exhausted`` latch：為 ATTACHED readiness 重探設計，只在 READY
     **轉移**點解開；一個「已經是 READY」的 session 不會再轉移 → 永遠解不開。
  b. banner 週期性重臂：RX 只要出現字面 ``U-Boot``（cat /proc/cmdline、dmesg…）
     就重臂 quiet 180s，確認 probe 永無排程機會。
  c. RX 洪水：``_rx_idle_enough``（3s）永不成立。ATTACHED 分支特地把 ``RX_FLOOD``
     保留為可重探，READY 分支原本沒有等價逃生口。
  d. human console 持續活躍：``_human_active_locked`` 令 agent 命令可用性取決於
     human 是否閒置——正是 #114/#130 刻意解耦的兩條路徑。

修法：pending 於「首次置位」時起算 ``READY_RECONFIRM_MAX_S`` deadline
（**重臂不延長**）與 ``READY_RECONFIRM_MAX_ATTEMPTS`` 次數上限，逾越即由
``expire_ready_reconfirm()`` 落 ``ready_reconfirm_failed`` 終態，gate 改回
**不可重試**的 ``READY_UNCONFIRMED``（不帶 ``retry_after_s``、帶
``recommended_action="self_test"``）。
"""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from sw_core import constants
from sw_core.config import SessionProfile, UartProfile
from sw_core.device_watcher import DeviceInfo
from sw_core.service import SerialwrapService
from sw_core.session_manager import SessionManager
import sw_core.session_manager as sm_mod
from sw_core.wal import WalWriter

try:
    import state_iso  # pytest／unittest discover：tests/ 在 sys.path
except ImportError:  # python3 -m unittest tests.test_x（repo root 跑法，#120）
    from tests import state_iso

try:
    from test_boot_quiet import FakeBridge
except ImportError:  # pragma: no cover - repo root 跑法
    from tests.test_boot_quiet import FakeBridge


def _make_profile() -> SessionProfile:
    return SessionProfile(
        profile_name="p",
        com="COM0",
        act_no=1,
        alias="lab+1",
        device_by_id="/dev/serial/by-id/fake",
        platform="prpl",
        prompt_regex=r"(?m)^root@prplOS:.*# ",
        login_regex="",
        ready_probe="echo __READY__${nonce}",
        uart=UartProfile(),
    )


class _BoundsMixin(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._old_state_path = sm_mod.STATE_PATH
        sm_mod.STATE_PATH = str(Path(self._tmp.name) / "state.json")

    def tearDown(self) -> None:
        sm_mod.STATE_PATH = self._old_state_path

    def _make_manager(self) -> tuple[SessionManager, sm_mod.SessionRuntime, FakeBridge]:
        profile = _make_profile()
        mgr = SessionManager(
            [profile],
            WalWriter(wal_dir=self._tmp.name),
            on_ready=lambda _sid: None,
            on_detached=lambda _sid: None,
        )
        session = mgr.get_session("COM0")
        assert session is not None
        bridge = FakeBridge(prompt_within_2s=False)
        session.bridge = bridge
        session.state = "READY"
        with mgr._lock:
            mgr._devices = {
                profile.device_by_id: DeviceInfo(
                    by_id=profile.device_by_id, real_path="/dev/ttyFAKE0"
                )
            }
        return mgr, session, bridge

    def _arm_pending(self, session: sm_mod.SessionRuntime) -> None:
        """READY＋pending-only（quiet 已過期）：#162 的受測過渡態。"""
        session.arm_boot_quiet()
        session.boot_quiet_until = time.monotonic() - 1.0
        self.assertTrue(session.ready_reconfirm_pending)
        self.assertFalse(session.boot_quiet_active())


class TestPendingIsBounded(_BoundsMixin):
    """pending 必須有 deadline、有次數上限、有終態。"""

    def test_pending_expires_after_deadline_into_terminal(self) -> None:
        mgr, session, bridge = self._make_manager()
        self._arm_pending(session)
        future = time.monotonic() + constants.READY_RECONFIRM_MAX_S + 1.0

        self.assertTrue(session.agent_gate_active(), "未逾期前 gate 仍成立（非 vacuous）")
        self.assertFalse(session.agent_gate_active(future))

        self.assertFalse(session.ready_reconfirm_pending)
        self.assertTrue(session.ready_reconfirm_failed)

    def test_pending_expires_after_max_attempts(self) -> None:
        mgr, session, bridge = self._make_manager()
        self._arm_pending(session)

        for _ in range(constants.READY_RECONFIRM_MAX_ATTEMPTS):
            self.assertTrue(session.ready_reconfirm_pending, "上限前不得提前落終態")
            # 確認 probe 失敗時 _probe_existing_bridge 已先把 state 打回 ATTACHED，
            # _finish_probe_reprobe 才據此累加；下一輪由其他路徑升回名義 READY。
            session.state = "ATTACHED"
            mgr._finish_probe_reprobe(
                session.session_id, bridge,
                {"ok": True, "session": {"state": "ATTACHED"}}, time.monotonic(),
            )
            session.state = "READY"

        self.assertFalse(session.ready_reconfirm_pending)
        self.assertTrue(session.ready_reconfirm_failed)
        self.assertIsNone(
            mgr._prepare_reprobe_locked(session, time.monotonic()),
            "落終態後不得再排確認 probe",
        )

    def test_rearm_does_not_extend_deadline(self) -> None:
        """堵路徑 (b)：重臂若延長 deadline，整個有界化就失效。"""
        mgr, session, bridge = self._make_manager()
        session.arm_boot_quiet(now=100.0)
        first_deadline = session.ready_reconfirm_deadline
        self.assertAlmostEqual(first_deadline, 100.0 + constants.READY_RECONFIRM_MAX_S, places=3)

        for tick in (160.0, 220.0, 280.0):
            session.arm_boot_quiet(now=tick)
            self.assertEqual(
                session.ready_reconfirm_deadline, first_deadline,
                "pending 期間重臂不得推遲 deadline",
            )

        self.assertTrue(session.expire_ready_reconfirm(first_deadline + 0.1))
        self.assertTrue(session.ready_reconfirm_failed)

    def test_confirm_ready_resets_all_bound_fields(self) -> None:
        mgr, session, bridge = self._make_manager()
        self._arm_pending(session)
        session.ready_reconfirm_attempts = 3
        session.ready_reconfirm_failed = True

        session.confirm_ready()

        self.assertFalse(session.ready_reconfirm_pending)
        self.assertFalse(session.ready_reconfirm_failed)
        self.assertEqual(session.ready_reconfirm_attempts, 0)
        self.assertEqual(session.ready_reconfirm_deadline, 0.0)
        self.assertEqual(session.boot_quiet_until, 0.0)

    def test_fresh_banner_after_terminal_starts_new_cycle(self) -> None:
        """落終態後若真的又偵測到 boot banner，視為新一輪重開機：重新 pending、
        重新起算 deadline、清掉 failed 旗標（終態不是永久黑名單）。"""
        mgr, session, bridge = self._make_manager()
        self._arm_pending(session)
        session.expire_ready_reconfirm(force=True)
        self.assertTrue(session.ready_reconfirm_failed)

        session.arm_boot_quiet(now=1000.0)

        self.assertTrue(session.ready_reconfirm_pending)
        self.assertFalse(session.ready_reconfirm_failed)
        self.assertAlmostEqual(
            session.ready_reconfirm_deadline, 1000.0 + constants.READY_RECONFIRM_MAX_S, places=3
        )

    def test_public_dict_exposes_bounds(self) -> None:
        mgr, session, bridge = self._make_manager()
        self._arm_pending(session)

        public = session.to_public_dict()
        self.assertFalse(public["ready_reconfirm_failed"])
        self.assertIsNotNone(public["ready_reconfirm_remaining_s"])
        self.assertLessEqual(public["ready_reconfirm_remaining_s"], constants.READY_RECONFIRM_MAX_S)

        session.expire_ready_reconfirm(force=True)
        public = session.to_public_dict()
        self.assertTrue(public["ready_reconfirm_failed"])
        self.assertIsNone(public["ready_reconfirm_remaining_s"])


class TestTerminalErrorShape(_BoundsMixin):
    """終態後所有 agent gate 一律回不可重試的 ``READY_UNCONFIRMED``。"""

    def _to_terminal(self) -> tuple[SessionManager, sm_mod.SessionRuntime, FakeBridge]:
        mgr, session, bridge = self._make_manager()
        self._arm_pending(session)
        session.expire_ready_reconfirm(force=True)
        self.assertTrue(session.ready_reconfirm_failed)
        return mgr, session, bridge

    def test_execute_returns_ready_unconfirmed_not_autoboot_quiet(self) -> None:
        mgr, session, bridge = self._to_terminal()

        result = mgr.execute_command(session.session_id, "echo hi", "agent:test", "cmd-1")

        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "READY_UNCONFIRMED")
        self.assertNotIn("retry_after_s", result, "不可重試：不得帶 retry_after_s")
        self.assertEqual(result["recommended_action"], "self_test")
        self.assertEqual(bridge.sent, [], "拒絕必須零 TX 副作用")

    def test_file_push_pull_return_ready_unconfirmed(self) -> None:
        mgr, session, bridge = self._to_terminal()

        push = mgr.file_push(
            "COM0", local_path="/nonexistent/f", remote_path="/tmp/f", source="agent:test"
        )
        pull = mgr.file_pull("COM0", remote_path="/tmp/f", source="agent:test")

        for result in (push, pull):
            self.assertFalse(result["ok"])
            self.assertEqual(result["error_code"], "READY_UNCONFIRMED")
            self.assertNotIn("retry_after_s", result)
        self.assertEqual(bridge.sent, [])

    def test_human_source_not_gated_in_terminal(self) -> None:
        """#114/#130 相容：human console／lease 永遠放行，終態也不例外。"""
        mgr, session, bridge = self._to_terminal()
        bridge.prompt_within_2s = True

        result = mgr.execute_command(session.session_id, "echo hi", "human:tester", "cmd-1")

        self.assertTrue(result["ok"])
        self.assertIn(("echo hi", "human:tester"), bridge.sent)

    def test_pending_still_returns_retryable_autoboot_quiet(self) -> None:
        """反向斷言（gate is not vacuous）：仍在有界期限內時維持**可重試**語意，
        不得把所有 pending 都當成終態。"""
        mgr, session, bridge = self._make_manager()
        self._arm_pending(session)

        result = mgr.execute_command(session.session_id, "echo hi", "agent:test", "cmd-1")

        self.assertEqual(result["error_code"], "AUTOBOOT_QUIET")
        self.assertNotIn("recommended_action", result)


class TestUnboundedPathsReachTerminal(_BoundsMixin):
    """(a)~(d) 四條永久阻塞路徑各釘一條：都必須在 ``READY_RECONFIRM_MAX_S`` 內落終態。"""

    def test_reprobe_exhausted_does_not_freeze_ready_gate(self) -> None:
        """(a) 最短的無界路徑：exhausted latch 只在 READY 轉移點解開，
        而「已經是 READY」的 session 不會再轉移。"""
        mgr, session, bridge = self._make_manager()
        self._arm_pending(session)
        session.reprobe_exhausted = True
        session.next_reprobe_at = None
        session.last_rx_mono = time.monotonic() - constants.REPROBE_RX_IDLE_S - 1.0

        with mgr._lock:
            prepared = mgr._prepare_reprobe_locked(session, time.monotonic())

        self.assertIsNone(prepared)
        self.assertTrue(session.ready_reconfirm_failed, "latch 不得靜默凍結 gate，須落終態")
        result = mgr.execute_command(session.session_id, "echo hi", "agent:test", "cmd-1")
        self.assertEqual(result["error_code"], "READY_UNCONFIRMED")

    def test_repeated_uboot_substring_pending_reaches_terminal(self) -> None:
        """(b) 每 60s 出現一次含字面 ``U-Boot`` 的 RX（cat /proc/cmdline、dmesg…）。

        重臂只延長 **quiet**（TX 靜默，#130 既有語意、每次 banner 180s 自帶上限），
        不得延長 **pending** 的 deadline——否則 pending 永久 True。
        """
        mgr, session, bridge = self._make_manager()
        start = time.monotonic()
        session.arm_boot_quiet(now=start)
        deadline = session.ready_reconfirm_deadline

        for offset in (60.0, 120.0, 180.0, 240.0):
            session.arm_boot_quiet(now=start + offset)
            self.assertEqual(session.ready_reconfirm_deadline, deadline, "deadline 不得被重臂推遲")
            self.assertTrue(
                session.agent_gate_active(start + offset), "期限內仍 gate（可重試語意）"
            )

        expired = start + constants.READY_RECONFIRM_MAX_S + 1.0
        session.agent_gate_active(expired)
        self.assertFalse(session.ready_reconfirm_pending, "pending 維度必須有界")
        self.assertTrue(session.ready_reconfirm_failed)

        # 最後一次 banner 的 quiet（180s）自然過期後，gate 完全釋放為終態語意。
        self.assertFalse(session.agent_gate_active(start + 240.0 + constants.BOOT_QUIET_WINDOW_S + 1.0))

    def test_rx_flood_pending_reaches_terminal(self) -> None:
        """(c) RX 洪水：``_rx_idle_enough`` 永不成立，確認 probe 永遠排不上。"""
        mgr, session, bridge = self._make_manager()
        start = time.monotonic()
        session.arm_boot_quiet(now=start)
        session.boot_quiet_until = start - 1.0  # quiet 已過期，只剩 pending

        # 洪水：每次 tick 的 last_rx 都貼著 now，RX idle 前置永不滿足。
        for offset in (10.0, 100.0, 200.0, 290.0):
            now = start + offset
            session.last_rx_mono = now
            with mgr._lock:
                self.assertIsNone(mgr._prepare_reprobe_locked(session, now), "洪水下排不上 probe")
            self.assertTrue(session.ready_reconfirm_pending, "期限內維持可重試 pending")

        now = start + constants.READY_RECONFIRM_MAX_S + 1.0
        session.last_rx_mono = now
        with mgr._lock:
            mgr._prepare_reprobe_locked(session, now)
        self.assertTrue(session.ready_reconfirm_failed, "洪水不得讓 pending 無界")

    def test_human_active_pending_reaches_terminal(self) -> None:
        """(d) human console 持續活躍：agent 命令可用性不得永久取決於 human 是否閒置。"""
        mgr, session, bridge = self._make_manager()
        start = time.monotonic()
        session.arm_boot_quiet(now=start)
        session.boot_quiet_until = start - 1.0
        session.last_rx_mono = start - constants.REPROBE_RX_IDLE_S - 10.0

        with mock.patch.object(
            SessionManager, "_human_active_locked", return_value=True
        ):
            for offset in (10.0, 150.0, 290.0):
                with mgr._lock:
                    self.assertIsNone(mgr._prepare_reprobe_locked(session, start + offset))
                self.assertTrue(session.ready_reconfirm_pending)

            now = start + constants.READY_RECONFIRM_MAX_S + 1.0
            with mgr._lock:
                mgr._prepare_reprobe_locked(session, now)

        self.assertTrue(session.ready_reconfirm_failed)


class TestSubmitGateTerminal(unittest.TestCase):
    """service 層 submit-time gate：終態改回不可重試的 ``READY_UNCONFIRMED``。"""

    def setUp(self) -> None:
        state_iso.isolate_testcase(self)  # #120 per-file 隔離（unittest 不載 conftest）
        self.svc = SerialwrapService([])

    def _inject(self) -> sm_mod.SessionRuntime:
        session = sm_mod.SessionRuntime(session_id="p:COM0", profile=_make_profile())
        session.bridge = FakeBridge(prompt_within_2s=True)
        session.state = "READY"
        session.arm_boot_quiet()
        session.boot_quiet_until = time.monotonic() - 1.0
        with self.svc._sessions._lock:
            self.svc._sessions._sessions[session.session_id] = session
        self.svc._arbiter.register_session(session.session_id)
        self.addCleanup(self.svc._arbiter.unregister_session, session.session_id)
        return session

    def test_submit_returns_ready_unconfirmed_after_deadline(self) -> None:
        session = self._inject()

        pending_resp = self.svc.rpc(
            "command.submit", {"selector": "COM0", "cmd": "echo hi", "source": "agent:test"}
        )
        self.assertEqual(pending_resp["error_code"], "AUTOBOOT_QUIET", "期限內仍可重試")
        self.assertIn("retry_after_s", pending_resp)

        # 把 deadline 推到過去＝模擬逾時（monotonic 不可回撥，改動狀態欄位）。
        session.ready_reconfirm_deadline = time.monotonic() - 1.0

        resp = self.svc.rpc(
            "command.submit", {"selector": "COM0", "cmd": "echo hi", "source": "agent:test"}
        )

        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error_code"], "READY_UNCONFIRMED")
        self.assertNotIn("retry_after_s", resp, "不可重試：不得帶 retry_after_s")
        self.assertEqual(resp["recommended_action"], "self_test")
        self.assertNotIn("cmd_id", resp, "submit-time 拒絕為純 RPC 錯誤，不產生 cmd_id")
        self.assertTrue(resp["session"]["ready_reconfirm_failed"])


if __name__ == "__main__":
    unittest.main()
