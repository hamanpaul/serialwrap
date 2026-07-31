"""U-Boot autoboot 保護（#130）：boot quiet window 測試。

涵蓋：
- ``detect_boot_banner`` 純函式（banner 命中／不命中／倒數行）
- RX 路徑的 quiet window 生命週期（banner 進場、prompt/login 解除、倒數延長、
  banner 前殘留 prompt 不得誤解除）
- ``_handle_reboot_command`` 收到 reboot 當下即設 quiet window
- ``_prepare_reprobe_locked`` / ``_reprobe_target_still_valid_locked`` 的 quiet gate
- ``_spawn_reboot_recovery`` 在 quiet window 內不送 probe、deadline 延伸涵蓋
  window 結束後至少一輪 probe、RX 解除後立即恢復探測
- prpl-template 的 ``bootloader_prompts`` 資產載入

以下為對抗審查（review）findings 收斂後補的回歸測試：
- ``_self_test_impl`` READY 分支的 nonce probe gate（Finding 1b）
- ``_recover_after_failure`` 的 CTRL_C/CTRL_D 強制按鍵迴圈 gate（Finding 1c）
- ``attach_session`` / ``recover_session`` 共用的 ``_probe_existing_bridge`` gate（Finding 2）
- 寬鬆 prompt_regex 誤配 bootloader prompt 時不得解除 quiet window（Finding 3）

#139（#130 Finding 4 收斂）——agent 顯式命令的雙層 AUTOBOOT_QUIET gate：
- ``TestAgentQuietGateExecute``：session_manager 層（第二層 execute-time gate、
  file_push/file_pull gate、四個 READY 轉移點與 reboot 提前返回的 clear_boot_quiet）
- ``TestSubmitQuietGate``：service 層（第一層 submit-time gate、queue race 終態、
  human 來源不受 gate）
"""
from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from sw_core import constants
from sw_core.config import SessionProfile, UartProfile, load_profiles
from sw_core.device_watcher import DeviceInfo
from sw_core.login_fsm import detect_boot_banner
from sw_core.service import SerialwrapService
from sw_core.session_manager import SessionManager
import sw_core.session_manager as sm_mod
from sw_core.wal import WalWriter

try:
    import state_iso  # pytest／unittest discover：tests/ 在 sys.path
except ImportError:  # python3 -m unittest tests.test_x（repo root 跑法，#120）
    from tests import state_iso


class FakeBridge:
    """最小 bridge stub：滿足 to_public_dict / reconcile / reboot 路徑所需介面。"""

    def __init__(self, *, prompt_within_2s: bool = False) -> None:
        self.prompt_within_2s = prompt_within_2s
        self.interactive_owner: str | None = None
        self.sent: list[tuple[str, str]] = []

    def list_consoles(self) -> list[dict]:
        return []

    def console_endpoint(self) -> str | None:
        return None

    def snapshot(self) -> dict:
        return {
            "running": True,
            "serial_alive": True,
            "vtty_alive": True,
            "interactive_owner": self.interactive_owner,
            "last_human_input_at": None,
            "vtty": "/tmp/fake-vtty",
        }

    def console_has_external_peer(self, _client_id: str) -> bool:
        return True

    def set_interactive_owner(self, owner: str | None) -> None:
        self.interactive_owner = owner

    def _enumerate_all_held_paths(self):
        return None

    def reap_stale_consoles(self, *, held_slave_paths=None):
        return []

    # reboot 命令路徑所需
    def rx_snapshot_len(self) -> int:
        return 0

    def send_command(self, cmd: str, *, source: str, cmd_id: str | None = None) -> None:
        self.sent.append((cmd, source))

    def wait_for_regex_from(self, _pattern: str, _offset: int, _timeout: float) -> bool:
        return self.prompt_within_2s

    def rx_text_from(self, _offset: int) -> str:
        return ""

    # _recover_after_failure（CTRL_C/CTRL_D）路徑所需
    def send_bytes(self, payload: bytes, *, source: str, cmd_id: str | None = None) -> None:
        self.sent.append((payload, source))

    # _probe_existing_bridge → probe_ready/ensure_ready（attach/recover 共用）所需
    def clear_rx_buffer(self) -> None:
        pass

    def wait_for_regex(self, _pattern: str, _timeout: float) -> bool:
        return self.prompt_within_2s

    def rx_tail(self, max_chars: int = 4096) -> str:
        return ""


class _ManagerMixin(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._old_state_path = sm_mod.STATE_PATH
        sm_mod.STATE_PATH = str(Path(self._tmp.name) / "state.json")

    def tearDown(self) -> None:
        sm_mod.STATE_PATH = self._old_state_path

    def _make_profile(self, *, login_regex: str = "") -> SessionProfile:
        return SessionProfile(
            profile_name="p",
            com="COM0",
            act_no=1,
            alias="lab+1",
            device_by_id="/dev/serial/by-id/fake",
            platform="prpl",
            prompt_regex=r"(?m)^root@prplOS:.*# ",
            login_regex=login_regex,
            ready_probe="echo __READY__${nonce}",
            uart=UartProfile(),
        )

    def _make_manager(self, *, login_regex: str = "") -> tuple[SessionManager, sm_mod.SessionRuntime]:
        profile = self._make_profile(login_regex=login_regex)
        mgr = SessionManager(
            [profile],
            WalWriter(wal_dir=self._tmp.name),
            on_ready=lambda _sid: None,
            on_detached=lambda _sid: None,
        )
        session = mgr.get_session("COM0")
        self.assertIsNotNone(session)
        assert session is not None
        with mgr._lock:
            mgr._devices = {
                profile.device_by_id: DeviceInfo(
                    by_id=profile.device_by_id,
                    real_path="/dev/ttyFAKE0",
                )
            }
        return mgr, session

    def _rx(self, mgr: SessionManager, session: sm_mod.SessionRuntime, text: str) -> None:
        mgr._on_bridge_rx(session.session_id, text.encode("utf-8"))


class TestDetectBootBanner(unittest.TestCase):
    """detect_boot_banner 純函式。"""

    def test_uboot_version_line_hits(self) -> None:
        self.assertTrue(detect_boot_banner("U-Boot 2022.01 (Jul 17 2026 - 00:00:00 +0800)"))

    def test_autoboot_countdown_line_hits(self) -> None:
        self.assertTrue(detect_boot_banner("Hit any key to stop autoboot:  3 "))

    def test_normal_shell_output_misses(self) -> None:
        self.assertFalse(detect_boot_banner("root@prplOS:~# echo hello\nhello"))

    def test_empty_misses(self) -> None:
        self.assertFalse(detect_boot_banner(""))


class TestBootQuietWindowRx(_ManagerMixin):
    """RX 路徑的 quiet window 生命週期。"""

    def test_banner_arms_quiet_window(self) -> None:
        mgr, session = self._make_manager()
        session.state = "ATTACHED"
        self.assertEqual(session.boot_quiet_until, 0.0)
        self._rx(mgr, session, "U-Boot 2022.01 (fake)\r\nDRAM:  512 MiB\r\n")
        now = time.monotonic()
        self.assertGreater(session.boot_quiet_until, now)
        self.assertLessEqual(session.boot_quiet_until, now + constants.BOOT_QUIET_WINDOW_S + 1.0)

    def test_countdown_extends_quiet_window(self) -> None:
        mgr, session = self._make_manager()
        session.state = "ATTACHED"
        self._rx(mgr, session, "U-Boot 2022.01 (fake)\r\n")
        first = session.boot_quiet_until
        time.sleep(0.05)
        self._rx(mgr, session, "\rHit any key to stop autoboot:  2 ")
        self.assertGreater(session.boot_quiet_until, first)

    def test_prompt_clears_quiet_window(self) -> None:
        mgr, session = self._make_manager()
        session.state = "ATTACHED"
        self._rx(mgr, session, "U-Boot 2022.01 (fake)\r\n")
        self.assertGreater(session.boot_quiet_until, 0.0)
        self._rx(mgr, session, "[    1.234567] init: fake boot complete\r\n\r\nroot@prplOS:~# ")
        self.assertEqual(session.boot_quiet_until, 0.0)

    def test_login_prompt_clears_quiet_window(self) -> None:
        mgr, session = self._make_manager(login_regex=r"(?mi)^login:\s*$")
        session.state = "ATTACHED"
        self._rx(mgr, session, "Hit any key to stop autoboot:  1 ")
        self.assertGreater(session.boot_quiet_until, 0.0)
        self._rx(mgr, session, "\r\nprplOS v3\r\nlogin:")
        self.assertEqual(session.boot_quiet_until, 0.0)

    def test_stale_prompt_before_banner_does_not_clear(self) -> None:
        """banner 之前殘留在 rolling tail 的舊 prompt 不得誤解除 quiet window。"""
        mgr, session = self._make_manager()
        session.state = "ATTACHED"
        # 舊 prompt 先進 tail（window 未啟用）
        self._rx(mgr, session, "root@prplOS:~# ")
        # banner 進場 → 啟用並截尾
        self._rx(mgr, session, "reboot: Restarting system\r\nU-Boot 2022.01 (fake)\r\n")
        self.assertGreater(session.boot_quiet_until, 0.0)
        # 後續非 prompt、非 banner 的 RX 不得因舊 prompt 殘留而解除
        self._rx(mgr, session, "DRAM:  512 MiB\r\n")
        self.assertGreater(session.boot_quiet_until, 0.0)

    def test_clear_resets_banner_tail(self) -> None:
        """解除時同步清 tail，避免下一輪誤判。"""
        mgr, session = self._make_manager()
        session.state = "ATTACHED"
        self._rx(mgr, session, "U-Boot 2022.01 (fake)\r\n")
        self._rx(mgr, session, "boot done\r\nroot@prplOS:~# ")
        self.assertEqual(session.boot_quiet_until, 0.0)
        self.assertEqual(session.boot_banner_tail, "")


class TestRebootCommandArmsQuiet(_ManagerMixin):
    """_handle_reboot_command 收到 reboot 當下即設 quiet window（不等 banner）。"""

    def test_reboot_command_arms_quiet_immediately(self) -> None:
        mgr, session = self._make_manager()
        session.state = "READY"
        session.boot_banner_tail = "root@prplOS:~# "  # 殘留 prompt 必須被清掉
        bridge = FakeBridge(prompt_within_2s=False)
        session.bridge = bridge

        with mock.patch.object(mgr, "_spawn_reboot_recovery") as spawn:
            result = mgr._handle_reboot_command(
                session,
                bridge,
                command="reboot",
                source="agent:test",
                cmd_id="cmd-1",
                timeout_s=10.0,
                execution_mode="line",
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "recovering")
        spawn.assert_called_once()
        now = time.monotonic()
        self.assertGreater(session.boot_quiet_until, now)
        self.assertEqual(session.boot_banner_tail, "")
        # 送出的第一個 TX 必須就是 reboot 命令本身（quiet 只擋自動 probe，不擋命令）
        self.assertEqual(bridge.sent[0][0], "reboot")


class TestReprobeQuietGate(_ManagerMixin):
    """quiet window 內 reprobe 不 fire、不累加 attempts；過期後恢復。"""

    def _make_attached_candidate(self) -> tuple[SessionManager, sm_mod.SessionRuntime]:
        mgr, session = self._make_manager()
        session.state = "ATTACHED"
        session.last_error = "PROMPT_UNAVAILABLE"
        session.bridge = FakeBridge()
        session.last_rx_mono = 100.0 - constants.REPROBE_RX_IDLE_S - 0.1
        session.reprobe_attempts = 2
        session.next_reprobe_at = 99.0
        return mgr, session

    def test_reprobe_gated_inside_quiet_window(self) -> None:
        mgr, session = self._make_attached_candidate()
        session.boot_quiet_until = 200.0  # now=100 → window 進行中

        with mock.patch.object(sm_mod.time, "monotonic", return_value=100.0):
            with mock.patch.object(mgr, "_probe_existing_bridge") as probe:
                with mock.patch.object(mgr, "_spawn_attach") as spawn:
                    mgr.reconcile_readiness()
                    mgr.join_reprobe_workers(2.0)

        probe.assert_not_called()
        spawn.assert_not_called()
        self.assertEqual(session.reprobe_attempts, 2)

    def test_reprobe_resumes_after_quiet_window_expires(self) -> None:
        mgr, session = self._make_attached_candidate()
        session.boot_quiet_until = 200.0
        session.last_rx_mono = 300.0 - constants.REPROBE_RX_IDLE_S - 0.1

        def probe_success(_session, _bridge) -> dict:
            session.state = "READY"
            session.last_error = None
            return {"ok": True, "session": session.to_public_dict()}

        with mock.patch.object(sm_mod.time, "monotonic", return_value=300.0):
            with mock.patch.object(mgr, "_probe_existing_bridge", side_effect=probe_success) as probe:
                mgr.reconcile_readiness()
                mgr.join_reprobe_workers(2.0)

        probe.assert_called_once()
        self.assertEqual(session.state, "READY")

    def test_detached_attach_reprobe_gated_inside_quiet_window(self) -> None:
        """DETACHED 的 attach 型 reprobe 同樣受 quiet gate。"""
        mgr, session = self._make_manager()
        session.state = "DETACHED"
        session.last_error = "LOGIN_PROMPT_TIMEOUT"
        session.last_rx_mono = 100.0 - constants.REPROBE_RX_IDLE_S - 0.1
        session.boot_quiet_until = 200.0

        with mock.patch.object(sm_mod.time, "monotonic", return_value=100.0):
            with mock.patch.object(mgr, "_spawn_attach") as spawn:
                mgr.reconcile_readiness()

        spawn.assert_not_called()
        self.assertEqual(session.state, "DETACHED")

    def test_still_valid_recheck_respects_quiet_window(self) -> None:
        """job 收集後、worker 寫入前 banner 才到 → 最終驗證必須擋下。"""
        mgr, session = self._make_attached_candidate()
        bridge = session.bridge
        with mgr._lock:
            self.assertTrue(mgr._reprobe_target_still_valid_locked(session, bridge, 100.0))
            session.boot_quiet_until = 200.0
            self.assertFalse(mgr._reprobe_target_still_valid_locked(session, bridge, 100.0))


class TestRebootRecoveryQuietGate(_ManagerMixin):
    """_spawn_reboot_recovery：quiet window 內純被動等待、結束後至少一輪 probe。"""

    def _arm_recovering(self, mgr: SessionManager, session: sm_mod.SessionRuntime) -> FakeBridge:
        bridge = FakeBridge()
        session.bridge = bridge
        session.state = "RECOVERING"
        session.recovering = True
        return bridge

    def test_no_probe_inside_quiet_and_probe_after_expiry(self) -> None:
        """timeout_s < quiet 剩餘時，deadline 必須延伸到 window 結束後仍能 probe。"""
        mgr, session = self._make_manager()
        bridge = self._arm_recovering(mgr, session)
        quiet_until = time.monotonic() + 0.6
        session.boot_quiet_until = quiet_until

        calls: list[float] = []

        def fake_ensure_ready(_bridge, _sp, auth=None):
            calls.append(time.monotonic())
            return True, None

        with mock.patch.object(sm_mod, "ensure_ready", side_effect=fake_ensure_ready):
            mgr._spawn_reboot_recovery(session.session_id, timeout_s=0.3)
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and session.state != "READY":
                time.sleep(0.05)

        self.assertEqual(session.state, "READY", "quiet 結束後應完成 probe 回 READY")
        self.assertTrue(calls, "quiet 結束後至少要有一次 probe（deadline 延伸）")
        self.assertGreaterEqual(
            calls[0], quiet_until,
            "quiet window 內不得送出任何 system probe",
        )

    def test_rx_clear_unblocks_recovery_probe_immediately(self) -> None:
        """RX 見 prompt 解除 quiet 後，recovery 應立即恢復探測（不等 window 過期）。"""
        mgr, session = self._make_manager()
        self._arm_recovering(mgr, session)
        session.boot_quiet_until = time.monotonic() + 30.0

        calls: list[float] = []

        def fake_ensure_ready(_bridge, _sp, auth=None):
            calls.append(time.monotonic())
            return True, None

        with mock.patch.object(sm_mod, "ensure_ready", side_effect=fake_ensure_ready):
            mgr._spawn_reboot_recovery(session.session_id, timeout_s=1.0)
            time.sleep(0.2)
            self.assertEqual(calls, [], "quiet window 內不得 probe")
            # 模擬開機完成：RX 出現 prompt → 解除 quiet
            self._rx(mgr, session, "boot complete\r\nroot@prplOS:~# ")
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and session.state != "READY":
                time.sleep(0.05)

        self.assertEqual(session.state, "READY")
        self.assertTrue(calls, "quiet 解除後應立即恢復探測")


class TestPrplTemplateBootloaderPrompts(unittest.TestCase):
    """方向 3：prpl-template 資產須帶 bootloader_prompts（含 `=> ` 與大寫 `U-Boot> ` 兩種實測 prompt）。"""

    def test_asset_prpl_template_has_bootloader_prompts(self) -> None:
        assets_dir = os.path.join(
            os.path.dirname(os.path.abspath(sm_mod.__file__)), "assets", "profiles"
        )
        result = load_profiles(assets_dir)
        tpl = next(t for t in result.templates if t.profile_name == "prpl-template")
        self.assertIn("^=> $", tpl.bootloader_prompts)
        self.assertIn("^U-Boot> $", tpl.bootloader_prompts)


class TestSelfTestQuietGate(_ManagerMixin):
    """#130 review Finding 1b（必修）：self_test 的 READY 分支 nonce probe gate。"""

    def test_ready_session_in_quiet_window_returns_autoboot_quiet(self) -> None:
        mgr, session = self._make_manager()
        bridge = FakeBridge()
        session.bridge = bridge
        session.state = "READY"
        session.attached_real_path = "/dev/ttyFAKE0"
        # 模擬自發重開機：RX 見到 boot banner，但 state 未被降級——boot quiet 只設
        # boot_quiet_until，不改 session.state（#130 Finding 4，架構層限制，未修）。
        self._rx(mgr, session, "U-Boot 2022.01 (fake)\r\n")
        self.assertEqual(session.state, "READY")
        self.assertTrue(session.boot_quiet_active())

        result = mgr.self_test("COM0", timeout_s=0.2)

        self.assertEqual(result.get("classification"), "AUTOBOOT_QUIET")
        self.assertEqual(bridge.sent, [], "quiet window 內不得送出 nonce probe")

    def test_gate_is_not_vacuous(self) -> None:
        """反證：拿掉 gate（boot_quiet_active 恆回 False）時，probe 真的會送出。"""
        mgr, session = self._make_manager()
        bridge = FakeBridge(prompt_within_2s=False)
        session.bridge = bridge
        session.state = "READY"
        session.attached_real_path = "/dev/ttyFAKE0"
        self._rx(mgr, session, "U-Boot 2022.01 (fake)\r\n")

        with mock.patch.object(sm_mod.SessionRuntime, "boot_quiet_active", return_value=False):
            mgr.self_test("COM0", timeout_s=0.05)

        self.assertTrue(bridge.sent, "拿掉 gate 應該會送出 probe（驗證測試非 vacuous）")


class TestRecoverAfterFailureQuietGate(_ManagerMixin):
    """#130 review Finding 1c（必修）：_recover_after_failure 的 CTRL_C/CTRL_D gate。

    這是最容易觸發的路徑：agent 正常送命令途中 target 自發重開機、逾時觸發本函式。
    """

    def test_ctrl_c_ctrl_d_skipped_inside_quiet_window(self) -> None:
        mgr, session = self._make_manager()
        bridge = FakeBridge(prompt_within_2s=False)
        session.bridge = bridge
        session.state = "READY"
        session.arm_boot_quiet()

        result = mgr._recover_after_failure(
            session, bridge,
            cmd_id="cmd-x", timeout_s=1.0, source="agent:test",
            command="some-long-cmd", prompt_regex=session.profile.prompt_regex, pre_offset=0,
        )

        self.assertEqual(bridge.sent, [], "quiet window 內不得送 CTRL_C/CTRL_D")
        self.assertFalse(result.get("ok"))
        self.assertEqual(result.get("error_code"), "PROMPT_TIMEOUT")
        self.assertEqual(result.get("recovery_action"), "NONE")
        self.assertEqual(session.state, "ATTACHED", "應直接落到既有 timeout 收尾（轉 ATTACHED）")

    def test_gate_is_not_vacuous(self) -> None:
        mgr, session = self._make_manager()
        bridge = FakeBridge(prompt_within_2s=False)
        session.bridge = bridge
        session.state = "READY"
        session.arm_boot_quiet()

        with mock.patch.object(sm_mod.SessionRuntime, "boot_quiet_active", return_value=False):
            mgr._recover_after_failure(
                session, bridge,
                cmd_id="cmd-x", timeout_s=0.05, source="agent:test",
                command="some-long-cmd", prompt_regex=session.profile.prompt_regex, pre_offset=0,
            )

        self.assertTrue(bridge.sent, "拿掉 gate 應該會送出 CTRL_C/CTRL_D（驗證測試非 vacuous）")


class TestAttachAndRecoverProbeQuietGate(_ManagerMixin):
    """#130 review Finding 2（必修）：attach_session 與 recover_session 共用的
    ``_probe_existing_bridge`` gate——單點 gate 同時涵蓋兩條呼叫路徑。"""

    def _make_attached_with_quiet(self) -> tuple[SessionManager, sm_mod.SessionRuntime, FakeBridge]:
        mgr, session = self._make_manager()
        bridge = FakeBridge(prompt_within_2s=False)
        session.bridge = bridge
        session.state = "ATTACHED"
        session.last_error = "PROMPT_UNAVAILABLE"
        session.arm_boot_quiet()
        return mgr, session, bridge

    def test_attach_session_gated_during_quiet(self) -> None:
        mgr, session, bridge = self._make_attached_with_quiet()

        result = mgr.attach_session("COM0")

        self.assertEqual(bridge.sent, [], "quiet window 內 attach_session 不得送 probe")
        self.assertFalse(result.get("ok"))
        self.assertEqual(result.get("error_code"), "PROMPT_UNAVAILABLE")
        self.assertEqual(result["session"]["state"], "ATTACHED")

    def test_recover_session_attached_branch_gated_during_quiet(self) -> None:
        """額外收斂（超出原 review findings 清單，實作時發現的同類漏洞）：
        recover_session 的 ATTACHED 分支與 attach_session 共用同一個
        ``_probe_existing_bridge``，屬同一漏洞類別，一併驗證。"""
        mgr, session, bridge = self._make_attached_with_quiet()

        result = mgr.recover_session("COM0")

        self.assertEqual(bridge.sent, [], "quiet window 內 recover_session 不得送 probe")
        self.assertFalse(result.get("recovered", False))
        self.assertEqual(result.get("error_code"), "PROMPT_UNAVAILABLE")

    def test_gate_is_not_vacuous(self) -> None:
        mgr, session, bridge = self._make_attached_with_quiet()

        with mock.patch.object(sm_mod.SessionRuntime, "boot_quiet_active", return_value=False):
            mgr.attach_session("COM0")

        self.assertTrue(bridge.sent, "拿掉 gate 應該會送出 probe（驗證測試非 vacuous）")


class TestBootloaderPromptDoesNotClearQuiet(unittest.TestCase):
    """#130 review Finding 3（必修）：寬鬆 prompt_regex 誤配 bootloader prompt 時不得
    解除 quiet window（例如 brcm-template 風格 ``(?m)[>#]\\s*$`` 撞上 U-Boot ``=> ``）。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._old_state_path = sm_mod.STATE_PATH
        sm_mod.STATE_PATH = str(Path(self._tmp.name) / "state.json")

    def tearDown(self) -> None:
        sm_mod.STATE_PATH = self._old_state_path

    def _make_manager(self, *, bootloader_prompts: tuple[str, ...] = ()) -> tuple[SessionManager, sm_mod.SessionRuntime]:
        profile = SessionProfile(
            profile_name="p",
            com="COM0",
            act_no=1,
            alias="lab+1",
            device_by_id="/dev/serial/by-id/fake",
            platform="bcm",
            prompt_regex=r"(?m)[>#]\s*$",
            login_regex=r"(?mi)login:\s*$",
            ready_probe="echo __READY__${nonce}",
            uart=UartProfile(),
            bootloader_prompts=bootloader_prompts,
        )
        mgr = SessionManager(
            [profile],
            WalWriter(wal_dir=self._tmp.name),
            on_ready=lambda _sid: None,
            on_detached=lambda _sid: None,
        )
        session = mgr.get_session("COM0")
        assert session is not None
        with mgr._lock:
            mgr._devices = {
                profile.device_by_id: DeviceInfo(by_id=profile.device_by_id, real_path="/dev/ttyFAKE0")
            }
        return mgr, session

    def _rx(self, mgr: SessionManager, session: sm_mod.SessionRuntime, text: str) -> None:
        mgr._on_bridge_rx(session.session_id, text.encode("utf-8"))

    def test_uboot_prompt_does_not_clear_quiet_window(self) -> None:
        mgr, session = self._make_manager(bootloader_prompts=("^=> $", "^CFE> $", r"^BCM\d+>> $"))
        session.state = "READY"
        self._rx(mgr, session, "> ")
        session.arm_boot_quiet()
        self._rx(mgr, session, "U-Boot 2022.01 (fake)\r\n")
        self._rx(mgr, session, "Hit any key to stop autoboot:  3 \r")
        self.assertTrue(session.boot_quiet_active())

        # U-Boot 自己的 "=> " prompt 出現：不得被寬鬆 prompt_regex 誤判為「開機完成」
        # 而解除 quiet window（板子其實仍卡在 bootloader）。
        self._rx(mgr, session, "\r\n=> ")

        self.assertTrue(session.boot_quiet_active(), "bootloader prompt 不應解除 quiet window")

    def test_gate_is_not_vacuous_without_bootloader_prompts_configured(self) -> None:
        """反證：template 未設定 bootloader_prompts（排除清單為空）時，``=> `` 仍會被
        寬鬆 prompt_regex 誤判為開機完成而解除——證明上一個測試通過並非巧合
        （例如 ``=> `` 本身就不會命中 prompt_regex）。"""
        mgr, session = self._make_manager(bootloader_prompts=())
        session.state = "READY"
        self._rx(mgr, session, "> ")
        session.arm_boot_quiet()
        self._rx(mgr, session, "U-Boot 2022.01 (fake)\r\n")
        self._rx(mgr, session, "\r\n=> ")

        self.assertFalse(
            session.boot_quiet_active(),
            "未設定 bootloader_prompts 時仍會被誤解除（驗證測試非 vacuous）",
        )


class TestAgentQuietGateExecute(_ManagerMixin):
    """#139 第二層 gate（session_manager 層）：READY 但 quiet armed 的過渡態，
    agent 顯式命令／file 傳輸被 AUTOBOOT_QUIET 即時拒絕、零 UART 副作用；
    READY 轉移點與「板其實沒重開」提前返回同步 clear quiet。"""

    def _make_ready_with_quiet(
        self, *, prompt_within_2s: bool = False
    ) -> tuple[SessionManager, sm_mod.SessionRuntime, FakeBridge]:
        mgr, session = self._make_manager()
        bridge = FakeBridge(prompt_within_2s=prompt_within_2s)
        session.bridge = bridge
        session.state = "READY"
        session.arm_boot_quiet()
        return mgr, session, bridge

    def test_execute_gated_when_quiet_armed_on_ready(self) -> None:
        mgr, session, bridge = self._make_ready_with_quiet()

        result = mgr.execute_command(session.session_id, "echo hi", "agent:test", "cmd-1")

        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "AUTOBOOT_QUIET")
        self.assertEqual(result["selector"], "COM0")
        self.assertEqual(bridge.sent, [], "gate 拒絕必須零 TX 副作用（bytes 不得落入 autoboot 窗）")

    def test_execute_human_source_not_gated(self) -> None:
        """human 來源循 #130 慣例永遠放行（#114 刻意進 bootloader 相容）。"""
        mgr, session, bridge = self._make_ready_with_quiet(prompt_within_2s=True)

        result = mgr.execute_command(session.session_id, "echo hi", "human:tester", "cmd-1")

        self.assertTrue(result["ok"])
        self.assertIn(("echo hi", "human:tester"), bridge.sent)

    def test_ready_transition_clears_quiet(self) -> None:
        """READY ⇒ clear（相容性關鍵）：probe 期間 banner 才 arm 的殘留 quiet，
        在 ``_probe_existing_bridge`` 設 READY 的同一 locked 區塊被清掉——
        f9-quiet-window-agent-passthrough 的 pytest 對照（防 gate 誤傷真 READY）。

        註：quiet 已 armed 時 ``_probe_existing_bridge`` 入口即被 #130 Finding 2 gate
        擋下（不送 probe），故以「wait_for_regex 期間 arm」模擬 banner 與 probe 競速
        的唯一真實路徑。"""
        mgr, session = self._make_manager()

        class _BannerDuringProbeBridge(FakeBridge):
            def __init__(self, target: sm_mod.SessionRuntime) -> None:
                super().__init__(prompt_within_2s=True)
                self._target = target

            def wait_for_regex(self, _pattern: str, _timeout: float) -> bool:
                self._target.arm_boot_quiet()  # 模擬 probe 進行中 banner 才到
                return True

        bridge = _BannerDuringProbeBridge(session)
        session.bridge = bridge
        session.state = "ATTACHED"
        session.last_error = "PROMPT_UNAVAILABLE"

        result = mgr.attach_session("COM0")

        self.assertTrue(result["ok"])
        self.assertEqual(session.state, "READY")
        self.assertEqual(session.boot_quiet_until, 0.0, "READY 落定必須同步 clear quiet")
        follow = mgr.execute_command(session.session_id, "echo hi", "agent:test", "cmd-2")
        self.assertTrue(follow["ok"], "READY 後 agent 命令不得再被 gate")

    def test_reboot_prompt_return_clears_quiet(self) -> None:
        """板其實沒重開（2s 內 prompt 回來）：提前返回分支補清 quiet，
        避免 READY session 白白被 gate 到 180s 過期（belt-and-suspenders）。"""
        mgr, session = self._make_manager()
        bridge = FakeBridge(prompt_within_2s=True)
        session.bridge = bridge
        session.state = "READY"

        result = mgr._handle_reboot_command(
            session,
            bridge,
            command="reboot",
            source="agent:test",
            cmd_id="cmd-1",
            timeout_s=10.0,
            execution_mode="line",
        )

        self.assertTrue(result["ok"])
        self.assertEqual(session.boot_quiet_until, 0.0)
        follow = mgr.execute_command(session.session_id, "echo hi", "agent:test", "cmd-2")
        self.assertTrue(follow["ok"])

    def test_file_push_gated(self) -> None:
        mgr, session, bridge = self._make_ready_with_quiet()

        result = mgr.file_push(
            "COM0", local_path="/nonexistent/f", remote_path="/tmp/f", source="agent:test"
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "AUTOBOOT_QUIET")
        self.assertEqual(bridge.sent, [])

    def test_file_pull_gated(self) -> None:
        mgr, session, bridge = self._make_ready_with_quiet()

        result = mgr.file_pull("COM0", remote_path="/tmp/f", source="agent:test")

        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "AUTOBOOT_QUIET")
        self.assertEqual(bridge.sent, [])

    def test_reboot_recovery_ready_clears_quiet(self) -> None:
        """``_spawn_reboot_recovery`` 成功設 READY 時同步 clear（probe 期間 banner
        才 arm 的競態面，與 _probe_existing_bridge 同語意）。"""
        mgr, session = self._make_manager()
        bridge = FakeBridge()
        session.bridge = bridge
        session.state = "RECOVERING"
        session.recovering = True

        def fake_ensure_ready(_bridge, _sp, auth=None):
            session.arm_boot_quiet()  # 模擬 ensure_ready（lock 外）期間 banner 才到
            return True, None

        with mock.patch.object(sm_mod, "ensure_ready", side_effect=fake_ensure_ready):
            mgr._spawn_reboot_recovery(session.session_id, timeout_s=5.0)
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and session.state != "READY":
                time.sleep(0.05)

        self.assertEqual(session.state, "READY")
        self.assertEqual(session.boot_quiet_until, 0.0, "recovery READY 落定必須同步 clear quiet")


class TestSubmitQuietGate(unittest.TestCase):
    """#139 第一層 gate（service 層 submit-time）：照 test_command_guard.py 手法建
    ``SerialwrapService([])``，直接注入 READY SessionRuntime 驗 RPC 邊界行為。"""

    def setUp(self) -> None:
        state_iso.isolate_testcase(self)  # #120 per-file 隔離（unittest 不載 conftest）
        self.svc = SerialwrapService([])

    def _inject_ready_session(self, *, arm_quiet: bool) -> sm_mod.SessionRuntime:
        profile = SessionProfile(
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
        session = sm_mod.SessionRuntime(session_id="p:COM0", profile=profile)
        session.bridge = FakeBridge(prompt_within_2s=True)
        session.state = "READY"
        if arm_quiet:
            session.arm_boot_quiet()
        with self.svc._sessions._lock:
            self.svc._sessions._sessions[session.session_id] = session
        self.svc._arbiter.register_session(session.session_id)
        self.addCleanup(self.svc._arbiter.unregister_session, session.session_id)
        return session

    def _wait_terminal(self, cmd_id: str, timeout_s: float = 5.0) -> dict:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            rec = self.svc.rpc("command.get", {"cmd_id": cmd_id}).get("command") or {}
            if rec.get("status") in ("done", "error", "interactive"):
                return rec
            time.sleep(0.02)
        self.fail(f"命令 {cmd_id} 在 {timeout_s}s 內未達終態")

    def test_command_submit_rejected_with_retry_after(self) -> None:
        session = self._inject_ready_session(arm_quiet=True)

        resp = self.svc.rpc(
            "command.submit",
            {"selector": "COM0", "cmd": "echo hi", "source": "agent:test"},
        )

        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error_code"], "AUTOBOOT_QUIET")
        self.assertGreater(resp["retry_after_s"], 0)
        self.assertEqual(resp["session"]["state"], "READY", "error payload 應附完整 session dict")
        self.assertNotIn("cmd_id", resp, "submit-time 拒絕為純 RPC 錯誤，不產生 cmd_id")

        # quiet 解除後同一 submit 必須被接受（READY ⇒ 放行的 service 層對照）。
        session.clear_boot_quiet()
        resp2 = self.svc.rpc(
            "command.submit",
            {"selector": "COM0", "cmd": "echo hi", "source": "agent:test"},
        )
        self.assertTrue(resp2["ok"])
        self.assertTrue(resp2.get("cmd_id"))
        self._wait_terminal(resp2["cmd_id"])  # 收斂 worker，避免 cleanup 時殘留 in-flight

    def test_file_push_pull_submit_time_gate(self) -> None:
        self._inject_ready_session(arm_quiet=True)

        push = self.svc.rpc(
            "file.push",
            {"selector": "COM0", "local_path": "/tmp/x", "remote_path": "/tmp/y",
             "source": "agent:test"},
        )
        pull = self.svc.rpc(
            "file.pull",
            {"selector": "COM0", "remote_path": "/tmp/y", "source": "agent:test"},
        )

        for resp in (push, pull):
            self.assertFalse(resp["ok"])
            self.assertEqual(resp["error_code"], "AUTOBOOT_QUIET")

    def test_queue_race_terminalizes_as_autoboot_quiet(self) -> None:
        """submit 通過第一層後 banner 才到（命令已在 arbiter queue）：worker 執行時被
        第二層攔下，``command.get`` 終態 status=error＋AUTOBOOT_QUIET（驗 arbiter 的
        error_code 傳遞與可重送語意）。以持有 SessionManager RLock 讓時序確定：worker
        的 execute_command 會阻塞在鎖上，直到本執行緒 arm 完 quiet 才放行。"""
        session = self._inject_ready_session(arm_quiet=False)
        mgr = self.svc._sessions

        with mgr._lock:  # RLock：同執行緒 reentrant，worker 執行緒被擋在 execute_command
            resp = self.svc.rpc(
                "command.submit",
                {"selector": "COM0", "cmd": "echo hi", "source": "agent:test"},
            )
            self.assertTrue(resp["ok"], "quiet 未 arm 時第一層必須放行")
            cmd_id = resp["cmd_id"]
            session.arm_boot_quiet()

        rec = self._wait_terminal(cmd_id)
        self.assertEqual(rec["status"], "error")
        self.assertEqual(rec["error_code"], "AUTOBOOT_QUIET")

    def test_human_source_submit_not_gated(self) -> None:
        self._inject_ready_session(arm_quiet=True)

        resp = self.svc.rpc(
            "command.submit",
            {"selector": "COM0", "cmd": "echo hi", "source": "human:x"},
        )

        self.assertTrue(resp["ok"], "human 來源不受第一層 gate")
        rec = self._wait_terminal(resp["cmd_id"])
        self.assertEqual(rec["status"], "done", "human 來源亦不受第二層 gate（命令真的執行）")


if __name__ == "__main__":
    unittest.main()
