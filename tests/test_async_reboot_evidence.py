"""#162 延伸：非同步 reboot 後，舊 shell 在 banner 前應答 nonce 不得提早確認 READY。

背景（實機，IB8 prplOS／BusyBox，WAL 8 次單獨 ``reboot`` 皆同一模式）：
``reboot`` 送出後 shell 立刻重印 prompt（2s 內 prompt 回來＝非同步 reboot，#162 保留
``ready_reconfirm_pending`` 等確認 probe），但板子 shutdown 很慢——``SMCOS: rebooting``
在 +8.9 到 +17.8s、``U-Boot`` banner 在 +16.9 到 +25.9s，而 quiet 一清，readiness
reprobe 在 RX idle 3s 後（+3.1 到 +6.3s）就送 ``\\n`` 加 ``echo __READY__<nonce>``，
**舊 shell 還活著而照常應答** → ``confirm_ready()`` 清 pending → stale-READY：agent 命令
通過 gate、落進關機中的系統（讀到舊 boot_id／被重開打斷）。

修法：判定為非同步 reboot 的那一刻起 ``BOOT_EVIDENCE_GRACE_S`` 內，確認 probe 要等 RX 見到
boot banner 才送；寬限內沒看到 banner 則視為其實沒重開、照常確認（不卡死）。

測試用 ``FakeBridge(prompt_within_2s=True)``：``wait_for_regex_from`` 恆 True，等於「舊 shell
永遠會應答 nonce」——因此 pending 能否在 banner 前被清，完全取決於 daemon 有沒有送出
確認 probe。
"""
from __future__ import annotations

import dataclasses
import time
import unittest
from unittest import mock

from sw_core import constants
from sw_core.config import SessionProfile, UartProfile
from sw_core.service import SerialwrapService
import sw_core.session_manager as sm_mod

try:
    import state_iso  # pytest／unittest discover：tests/ 在 sys.path
except ImportError:  # python3 -m unittest tests.test_x（repo root 跑法，#120）
    from tests import state_iso

try:
    from test_boot_quiet import FakeBridge, _ManagerMixin
except ImportError:  # pragma: no cover - repo root 跑法
    from tests.test_boot_quiet import FakeBridge, _ManagerMixin

_BOOT_DONE = r"Please press Enter to activate this console"


class _AsyncRebootBase(_ManagerMixin):
    def _reboot(self, mgr, session, bridge, *, command: str = "reboot", source: str = "agent:test"):
        return mgr._handle_reboot_command(
            session, bridge, command=command, source=source,
            cmd_id="cmd-reboot", timeout_s=10.0, execution_mode="line",
        )

    def _make_after_async_reboot(self, *, boot_done_regex: str = ""):
        """READY session 送出 reboot、2s 內 prompt 回來（非同步 reboot）後的過渡態。"""
        mgr, session = self._make_manager(boot_done_regex=boot_done_regex)
        bridge = FakeBridge(prompt_within_2s=True)
        session.bridge = bridge
        session.state = "READY"
        result = self._reboot(mgr, session, bridge)
        self.assertTrue(result["ok"])
        self.assertTrue(session.ready_reconfirm_pending)
        self.assertEqual(session.boot_quiet_until, 0.0, "TX 靜默維度已解除（#139 不變）")
        return mgr, session, bridge

    def _tick(self, mgr, session) -> None:
        """模擬 RX 已閒置 REPROBE_RX_IDLE_S 後的一個 reconcile tick，並等 probe worker 收斂。"""
        session.last_rx_mono = time.monotonic() - constants.REPROBE_RX_IDLE_S - 0.5
        mgr.reconcile_readiness()
        mgr.join_reprobe_workers(5.0)


class TestStaleShellNonceBeforeBanner(_AsyncRebootBase):
    """核心：banner 前舊 shell 會應答 nonce，但 daemon 不得送確認 probe、不得確認 READY。"""

    def test_wait_started_by_async_reboot(self) -> None:
        mgr, session, _bridge = self._make_after_async_reboot()
        self.assertTrue(session.boot_evidence_wait_active())
        remaining = session.to_public_dict()["boot_evidence_wait_remaining_s"]
        self.assertIsNotNone(remaining)
        self.assertLessEqual(remaining, constants.BOOT_EVIDENCE_GRACE_S)
        self.assertGreater(remaining, constants.BOOT_EVIDENCE_GRACE_S - 5.0)

    def test_stale_shell_nonce_does_not_confirm_ready(self) -> None:
        mgr, session, bridge = self._make_after_async_reboot()
        sent_after_reboot = list(bridge.sent)

        self._tick(mgr, session)  # 實機：+3.1~+6.3s 就是這一刻送出 "\n"+nonce

        self.assertEqual(bridge.sent, sent_after_reboot, "banner 前不得送確認 probe（舊 shell 會應答）")
        self.assertTrue(session.ready_reconfirm_pending, "pending 不得被舊 shell 的 nonce 應答清掉")

    def test_agent_command_gets_autoboot_quiet_before_banner(self) -> None:
        mgr, session, bridge = self._make_after_async_reboot()
        self._tick(mgr, session)
        sent_before = list(bridge.sent)

        result = mgr.execute_command(session.session_id, "cat /proc/sys/kernel/random/boot_id", "agent:test", "cmd-2")

        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "AUTOBOOT_QUIET", "stale-READY 窗不得放行 agent 命令")
        self.assertEqual(bridge.sent, sent_before, "gate 拒絕必須零 TX 副作用")

    def test_wait_does_not_burn_reprobe_attempts(self) -> None:
        """等待期間不排 probe：不累加 attempts、不排 backoff（同 quiet 的語意）。"""
        mgr, session, _bridge = self._make_after_async_reboot()
        for _ in range(3):
            self._tick(mgr, session)
        self.assertEqual(session.reprobe_attempts, 0)
        self.assertIsNone(session.next_reprobe_at)
        self.assertEqual(session.ready_reconfirm_attempts, 0)
        self.assertFalse(session.reprobe_exhausted)

    def test_still_valid_recheck_blocks_probe_when_wait_starts_after_collection(self) -> None:
        """job 收集後、worker 寫入前才進入等待（例如 reboot 剛送出）→ 最終驗證擋下。"""
        mgr, session = self._make_manager()
        bridge = FakeBridge(prompt_within_2s=True)
        session.bridge = bridge
        session.state = "READY"
        session.arm_boot_quiet()
        session.clear_boot_quiet()  # pending 留著、quiet 已解除：確認 probe 原本合法
        now = time.monotonic()
        with mgr._lock:
            self.assertTrue(mgr._reprobe_target_still_valid_locked(session, bridge, now))
            session.begin_boot_evidence_wait(now)
            self.assertFalse(mgr._reprobe_target_still_valid_locked(session, bridge, now))

    def test_probe_existing_bridge_is_noop_during_wait(self) -> None:
        """attach／recover／reprobe 共用的 probe 入口：等待期間不送 probe、不覆寫 READY 的 last_error。"""
        mgr, session, bridge = self._make_after_async_reboot()
        sent_before = list(bridge.sent)

        result = mgr._probe_existing_bridge(session, bridge)

        self.assertTrue(result["ok"])
        self.assertEqual(bridge.sent, sent_before)
        self.assertEqual(session.state, "READY")
        self.assertIsNone(session.last_error)
        self.assertTrue(session.ready_reconfirm_pending)

    def test_self_test_during_wait_reports_autoboot_quiet_without_probe(self) -> None:
        mgr, session, bridge = self._make_after_async_reboot()
        session.attached_real_path = "/dev/ttyFAKE0"
        sent_before = list(bridge.sent)

        result = mgr.self_test("COM0", timeout_s=0.2)

        self.assertEqual(result.get("classification"), "AUTOBOOT_QUIET")
        self.assertEqual(result.get("recommended_action"), "wait")
        self.assertEqual(bridge.sent, sent_before, "self-test 不得在等待期間送 nonce")
        self.assertTrue(session.ready_reconfirm_pending)


class TestBannerReleasesWait(_AsyncRebootBase):
    def test_boot_noise_without_banner_keeps_waiting(self) -> None:
        """shutdown 訊息（含平台字串如 ``SMCOS: rebooting``）本身不是證據，只認 boot banner。"""
        mgr, session, bridge = self._make_after_async_reboot()
        self._rx(mgr, session, "EXT4-fs (flash-data): unmounting filesystem\r\nSMCOS: rebooting system...\r\n")
        self.assertTrue(session.boot_evidence_wait_active())
        self._tick(mgr, session)
        self.assertTrue(session.ready_reconfirm_pending)

    def test_banner_then_boot_done_confirms_ready(self) -> None:
        """完整流程：banner（證據）→ 開機完成解除 quiet → 確認 probe → 真 READY → 命令放行。"""
        mgr, session, bridge = self._make_after_async_reboot(boot_done_regex=_BOOT_DONE)

        self._rx(mgr, session, "U-Boot TPL 2024.04 (Oct 27 2026 - 15:48:00 +0800)\r\n")
        self.assertFalse(session.boot_evidence_wait_active(), "banner 即解除證據等待")
        self.assertTrue(session.boot_quiet_active(), "banner 重臂 quiet（#130 不變）")
        self.assertTrue(session.ready_reconfirm_pending)

        # banner 之後仍在 quiet：TX 靜默照舊，不送 probe。
        sent_before = list(bridge.sent)
        self._tick(mgr, session)
        self.assertEqual(bridge.sent, sent_before)
        self.assertTrue(session.ready_reconfirm_pending)

        # 開機完成（askconsole）解除 quiet → 確認 probe → confirm_ready。
        self._rx(mgr, session, "Please press Enter to activate this console.\r\n")
        self.assertFalse(session.boot_quiet_active())
        self._tick(mgr, session)

        self.assertTrue(len(bridge.sent) > len(sent_before), "確認 probe 必須實際送出")
        self.assertFalse(session.ready_reconfirm_pending)
        follow = mgr.execute_command(session.session_id, "echo hi", "agent:test", "cmd-2")
        self.assertTrue(follow["ok"], "真 READY 後 agent 命令必須放行")

    def test_banner_split_across_chunks_releases_wait(self) -> None:
        mgr, session, _bridge = self._make_after_async_reboot()
        self._rx(mgr, session, "...\r\nHit any key to sto")
        self.assertTrue(session.boot_evidence_wait_active())
        self._rx(mgr, session, "p autoboot:  3 ")
        self.assertFalse(session.boot_evidence_wait_active())


class TestGraceExpiryDoesNotDeadlock(_AsyncRebootBase):
    def test_no_banner_confirms_after_grace(self) -> None:
        """一直沒有 banner（板子其實沒重開）：寬限過後照常確認，不卡死。"""
        mgr, session, bridge = self._make_after_async_reboot()
        self._tick(mgr, session)
        self.assertTrue(session.ready_reconfirm_pending, "寬限內仍擋")

        # 寬限過期（模擬 BOOT_EVIDENCE_GRACE_S 已過）
        session.boot_evidence_wait_until = time.monotonic() - 0.1
        self.assertFalse(session.boot_evidence_wait_active())
        self.assertIsNone(session.to_public_dict()["boot_evidence_wait_remaining_s"])
        self._tick(mgr, session)

        self.assertFalse(session.ready_reconfirm_pending, "寬限過後確認 probe 成功即清 pending")
        follow = mgr.execute_command(session.session_id, "echo hi", "agent:test", "cmd-2")
        self.assertTrue(follow["ok"])

    def test_wait_boundaries_with_explicit_now(self) -> None:
        mgr, session = self._make_manager()
        session.begin_boot_evidence_wait(100.0)
        grace = constants.BOOT_EVIDENCE_GRACE_S
        self.assertTrue(session.boot_evidence_wait_active(100.0))
        self.assertTrue(session.boot_evidence_wait_active(100.0 + grace - 0.01))
        self.assertFalse(session.boot_evidence_wait_active(100.0 + grace))

    def test_confirm_ready_clears_wait(self) -> None:
        """任一 READY 確認點落定（confirm_ready）後不殘留證據等待。"""
        mgr, session, _bridge = self._make_after_async_reboot()
        session.confirm_ready()
        self.assertEqual(session.boot_evidence_wait_until, 0.0)

    def test_grace_covers_observed_ib8_banner_and_stays_bounded(self) -> None:
        """寬限須涵蓋 IB8 實測 banner 最晚時點（reboot 後 25.9s），且小於 pending 的有界上限。"""
        self.assertGreaterEqual(constants.BOOT_EVIDENCE_GRACE_S, 26.0 * 2)
        self.assertLess(constants.BOOT_EVIDENCE_GRACE_S, constants.READY_RECONFIRM_MAX_S)

    def test_grace_does_not_extend_ready_reconfirm_deadline(self) -> None:
        mgr, session, _bridge = self._make_after_async_reboot()
        deadline = session.ready_reconfirm_deadline
        session.begin_boot_evidence_wait()
        self.assertEqual(session.ready_reconfirm_deadline, deadline)
        self.assertLessEqual(
            session.ready_reconfirm_deadline - time.monotonic(), constants.READY_RECONFIRM_MAX_S
        )

    def test_ready_reconfirm_bound_still_terminates_during_wait(self) -> None:
        """#162 有界化不被繞過：等待中 pending 逾 deadline 仍落 READY_UNCONFIRMED（不可重試），
        且殘留的證據等待不得擋住 operator 隨後的 self-test。"""
        mgr, session, bridge = self._make_after_async_reboot()
        session.ready_reconfirm_deadline = time.monotonic() - 1.0

        result = mgr.execute_command(session.session_id, "echo hi", "agent:test", "cmd-2")

        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "READY_UNCONFIRMED")
        self.assertNotIn("retry_after_s", result)
        self.assertTrue(session.ready_reconfirm_failed)
        self.assertFalse(session.boot_evidence_wait_active(), "終態後證據等待一併清除")

        session.attached_real_path = "/dev/ttyFAKE0"
        self.assertEqual(mgr.self_test("COM0", timeout_s=0.2).get("classification"), "OK")


class TestUnaffectedPaths(_AsyncRebootBase):
    def test_spontaneous_reboot_is_not_delayed(self) -> None:
        """自發重開機（daemon 沒送 reboot）：沒有證據等待，banner → 開機完成 → 立即確認。"""
        mgr, session = self._make_manager(boot_done_regex=_BOOT_DONE)
        bridge = FakeBridge(prompt_within_2s=True)
        session.bridge = bridge
        session.state = "READY"

        self._rx(mgr, session, "U-Boot 2024.04\r\nHit any key to stop autoboot:  3 \r\n")
        self.assertTrue(session.ready_reconfirm_pending)
        self.assertEqual(session.boot_evidence_wait_until, 0.0)
        self._rx(mgr, session, "Please press Enter to activate this console.\r\n")
        self._tick(mgr, session)

        self.assertFalse(session.ready_reconfirm_pending)

    def test_recovery_branch_does_not_start_wait(self) -> None:
        """2s 內沒 prompt（走 recovery 迴圈）：維持原行為，不起證據等待，並作廢上一輪殘留。"""
        mgr, session = self._make_manager()
        bridge = FakeBridge(prompt_within_2s=False)
        session.bridge = bridge
        session.state = "READY"
        session.begin_boot_evidence_wait()  # 上一輪非同步 reboot 的殘留

        with mock.patch.object(mgr, "_spawn_reboot_recovery") as spawn:
            result = self._reboot(mgr, session, bridge)

        self.assertEqual(result["recovery_action"], "EXPECT_REBOOT")
        spawn.assert_called_once()
        self.assertEqual(session.boot_evidence_wait_until, 0.0)

    def test_human_source_recovery_path_unchanged(self) -> None:
        mgr, session = self._make_manager()
        bridge = FakeBridge(prompt_within_2s=False)
        session.bridge = bridge
        session.state = "READY"

        result = self._reboot(mgr, session, bridge, source="human:cid-7")

        self.assertEqual(result["recovery_action"], "PROMOTE_HUMAN_INTERACTIVE")
        self.assertEqual(session.boot_evidence_wait_until, 0.0)

    def test_non_reboot_command_does_not_start_wait(self) -> None:
        mgr, session = self._make_manager()
        bridge = FakeBridge(prompt_within_2s=True)
        session.bridge = bridge
        session.state = "READY"
        with mock.patch.object(sm_mod, "_is_reboot_command", return_value=False):
            mgr._execute_command_inner(
                session, bridge, "echo hi", "agent:test", "cmd-1",
                timeout_s=1.0, normalized_mode="line", prompt_regex=session.profile.prompt_regex,
            )
        self.assertEqual(session.boot_evidence_wait_until, 0.0)

    def test_execute_command_reboot_path_starts_wait(self) -> None:
        """經 execute_command 的單獨 reboot（``_is_reboot_command`` 猜到）端到端走到同一條等待。"""
        mgr, session = self._make_manager()
        bridge = FakeBridge(prompt_within_2s=True)
        session.bridge = bridge
        session.state = "READY"

        result = mgr.execute_command(session.session_id, "reboot", "agent:test", "cmd-1", timeout_s=10.0)

        self.assertTrue(result["ok"])
        self.assertTrue(session.boot_evidence_wait_active())
        self.assertTrue(session.ready_reconfirm_pending)

    def test_chained_reboot_via_handler_starts_wait(self) -> None:
        """判斷掛在 ``_handle_reboot_command`` 而非命令文字：``--expect-reboot`` 的串接命令
        （``sync && reboot``，``_is_reboot_command`` 猜不到）合併後走同一個 handler，自然生效。"""
        mgr, session = self._make_manager()
        bridge = FakeBridge(prompt_within_2s=True)
        session.bridge = bridge
        session.state = "READY"
        self.assertFalse(sm_mod._is_reboot_command("sync && reboot"))

        result = self._reboot(mgr, session, bridge, command="sync && reboot")

        self.assertTrue(result["ok"])
        self.assertTrue(session.boot_evidence_wait_active())

    def test_detach_clears_wait(self) -> None:
        mgr, session, _bridge = self._make_after_async_reboot()
        session.bridge = None
        with mgr._lock:
            mgr._detach_session_locked(session, reason="BRIDGE_DOWN:test")
        self.assertEqual(session.boot_evidence_wait_until, 0.0)

    def test_console_unlock_profile_follows_same_wait(self) -> None:
        """console_unlock_env 路徑：quiet 不擋它的 probe（FSM 被動等 RX 靜默，既有語意不變），
        但舊 shell 的 nonce 應答同樣不是實證——證據等待照常生效，banner 後照常放行。"""
        mgr, session = self._make_manager()
        session.profile = dataclasses.replace(session.profile, console_unlock_env="UNLOCK_ENV")
        bridge = FakeBridge(prompt_within_2s=True)
        session.bridge = bridge
        session.state = "READY"
        self._reboot(mgr, session, bridge)
        now = time.monotonic()
        session.last_rx_mono = now - constants.REPROBE_RX_IDLE_S - 0.5

        with mgr._lock:
            self.assertFalse(mgr._boot_quiet_blocks_probe(session, now), "unlock profile 的 quiet 不擋 probe（既有）")
            self.assertIsNone(mgr._prepare_reprobe_locked(session, now), "但證據等待擋住確認 probe")

        self._rx(mgr, session, "U-Boot 2024.04\r\n")
        now = time.monotonic()
        session.last_rx_mono = now - constants.REPROBE_RX_IDLE_S - 0.5  # banner RX 刷新了 last_rx
        with mgr._lock:
            prepared = mgr._prepare_reprobe_locked(session, now)
        self.assertIsNotNone(prepared, "banner 後與既有行為一致")


class TestSubmitTimeGateDuringWait(unittest.TestCase):
    """service 層：等待期間 submit-time gate 仍回可重試 AUTOBOOT_QUIET，並暴露剩餘寬限秒數。"""

    def setUp(self) -> None:
        state_iso.isolate_testcase(self)
        self.svc = SerialwrapService([])

    def test_submit_rejected_with_wait_visible(self) -> None:
        profile = SessionProfile(
            profile_name="p", com="COM0", act_no=1, alias="lab+1",
            device_by_id="/dev/serial/by-id/fake", platform="prpl",
            prompt_regex=r"(?m)^root@prplOS:.*# ", login_regex="",
            ready_probe="echo __READY__${nonce}", uart=UartProfile(),
        )
        session = sm_mod.SessionRuntime(session_id="p:COM0", profile=profile)
        session.bridge = FakeBridge(prompt_within_2s=True)
        session.state = "READY"
        session.arm_boot_quiet()
        session.clear_boot_quiet()
        session.begin_boot_evidence_wait()
        with self.svc._sessions._lock:
            self.svc._sessions._sessions[session.session_id] = session
        self.svc._arbiter.register_session(session.session_id)
        self.addCleanup(self.svc._arbiter.unregister_session, session.session_id)

        resp = self.svc.rpc("command.submit", {"selector": "COM0", "cmd": "echo hi", "source": "agent:test"})

        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error_code"], "AUTOBOOT_QUIET")
        self.assertEqual(resp["retry_after_s"], constants.READY_RECONFIRM_RETRY_S)
        self.assertIsNotNone(resp["session"]["boot_evidence_wait_remaining_s"])
        self.assertNotIn("cmd_id", resp)


if __name__ == "__main__":
    unittest.main()
