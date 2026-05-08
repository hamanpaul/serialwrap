"""
tests/test_bootloader_recovery.py

Issue #44 Phase B：self_test BOOTLOADER classification 與 recovery lease 基礎 schema。

TDD RED → GREEN 流程：
  1. 先確認這些測試在實作前全部失敗。
  2. 最小實作後確認全部通過。
"""
from __future__ import annotations

import dataclasses
import tempfile
import unittest
import unittest.mock as mock
from pathlib import Path

from sw_core.config import SessionProfile, UartProfile
from sw_core.session_manager import InteractiveLease, SessionManager, SessionRuntime
import sw_core.session_manager as sm_mod
from sw_core.wal import WalWriter


# ──────────────────────────────────────────────
# 輔助：建立最小 SessionProfile（含 bootloader_prompts）
# ──────────────────────────────────────────────

def _make_profile(
    name: str = "p",
    com: str = "COM0",
    bootloader_prompts: tuple[str, ...] = (),
) -> SessionProfile:
    return SessionProfile(
        profile_name=name,
        com=com,
        act_no=1,
        alias="lab+1",
        device_by_id="/dev/serial/by-id/orig",
        platform="prpl",
        uart=UartProfile(),
        bootloader_prompts=bootloader_prompts,
    )


# ──────────────────────────────────────────────
# 1. _matches_any_bootloader_prompt helper 測試
# ──────────────────────────────────────────────

class TestMatchesAnyBootloaderPrompt(unittest.TestCase):
    """測試模組層級 helper _matches_any_bootloader_prompt。"""

    def _fn(self, rx_tail: str, patterns: list[str] | tuple[str, ...]):
        return sm_mod._matches_any_bootloader_prompt(rx_tail, patterns)

    def test_hits_last_nonempty_line(self) -> None:
        """最後一個非空行符合 pattern → 回傳該 pattern。"""
        result = self._fn("login: \n=> ", [r"^=> "])
        self.assertEqual(result, r"^=> ")

    def test_no_match_returns_none(self) -> None:
        """最後一行不符合任何 pattern → None。"""
        result = self._fn("login: \nsome other text", [r"^=> "])
        self.assertIsNone(result)

    def test_empty_patterns_returns_none(self) -> None:
        """patterns 為空 → None。"""
        result = self._fn("=> ", [])
        self.assertIsNone(result)

    def test_empty_rx_tail_returns_none(self) -> None:
        """rx_tail 為空字串 → None。"""
        result = self._fn("", [r"^=> "])
        self.assertIsNone(result)

    def test_only_last_nonempty_line_is_checked(self) -> None:
        """只看最後一個非空/非純空白行；中間行不影響結果。"""
        # 第二行有 "=>" 但第三行才是最後非空行
        result = self._fn("=> \nshell prompt # ", [r"^=> "])
        self.assertIsNone(result)

    def test_trailing_whitespace_line_skipped(self) -> None:
        """尾端只有空白的行應被跳過；取其前一個非空白行。"""
        # "=> " 後面跟著一個純空白行
        result = self._fn("=> \n   ", [r"^=> "])
        self.assertEqual(result, r"^=> ")

    def test_invalid_regex_does_not_crash(self) -> None:
        """invalid regex pattern 不應讓函式拋出例外。"""
        result = self._fn("=> ", [r"[invalid(", r"^=> "])
        # invalid pattern 略過，第二個 pattern 命中
        self.assertEqual(result, r"^=> ")

    def test_rx_tail_with_no_newline(self) -> None:
        """rx_tail 沒有換行符號，直接把整個字串當最後一行。"""
        result = self._fn("=> ", [r"^=> "])
        self.assertEqual(result, r"^=> ")

    def test_returns_first_matching_pattern(self) -> None:
        """有多個 pattern 命中時，回傳第一個。"""
        result = self._fn("CFE> ", [r"CFE>", r"=> "])
        self.assertEqual(result, r"CFE>")

    def test_rx_tail_all_whitespace_lines_returns_none(self) -> None:
        """rx_tail 全部為空白行時，找不到非空白行，應返回 None。"""
        result = self._fn("  \n  ", [r"^=> "])
        self.assertIsNone(result)


# ──────────────────────────────────────────────
# 2. self_test ATTACHED BOOTLOADER classification 測試
# ──────────────────────────────────────────────

class TestSelfTestBootloaderClassification(unittest.TestCase):
    """測試 SessionManager.self_test 在 ATTACHED 狀態下的 BOOTLOADER 分類。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._old_state_path = sm_mod.STATE_PATH
        sm_mod.STATE_PATH = str(Path(self._tmp.name) / "state.json")

    def tearDown(self) -> None:
        sm_mod.STATE_PATH = self._old_state_path

    def _make_manager_with_device(
        self, profile: SessionProfile
    ) -> tuple[SessionManager, "mock.MagicMock"]:
        """建立 manager、注入 fake device 與 fake bridge，回傳 (mgr, bridge)。"""
        from sw_core.device_watcher import DeviceInfo

        mgr = SessionManager(
            [profile],
            WalWriter(wal_dir=self._tmp.name),
            on_ready=lambda _sid: None,
            on_detached=lambda _sid: None,
        )
        session = mgr.get_session("COM0")
        assert session is not None

        bridge = mock.MagicMock()
        bridge.snapshot.return_value = {
            "running": True,
            "serial_alive": True,
            "vtty_alive": True,
            "vtty": "/dev/pts/9",
        }
        session.bridge = bridge
        session.state = "ATTACHED"
        session.attached_real_path = "/dev/ttyUSB0"

        with mgr._lock:
            mgr._devices = {
                "/dev/serial/by-id/orig": DeviceInfo(
                    by_id="/dev/serial/by-id/orig", real_path="/dev/ttyUSB0"
                )
            }
        return mgr, bridge

    def test_bootloader_prompt_matched_returns_bootloader_classification(self) -> None:
        """ATTACHED + profile bootloader_prompts + rx_tail 命中 → BOOTLOADER。"""
        profile = _make_profile(bootloader_prompts=(r"^=> ",))
        mgr, bridge = self._make_manager_with_device(profile)

        bridge.rx_tail.return_value = "some boot output\n=> "

        resp = mgr.self_test("COM0")

        self.assertTrue(resp["ok"])
        self.assertEqual(resp["classification"], "BOOTLOADER")
        self.assertEqual(resp["recommended_action"], "recover_interactive")
        self.assertIn("matched_prompt", resp)
        self.assertEqual(resp["matched_prompt"], r"^=> ")
        self.assertIn("rx_tail", resp)
        # 不應發送 probe
        bridge.send_command.assert_not_called()

    def test_empty_bootloader_prompts_falls_back_to_attached_not_ready(self) -> None:
        """bootloader_prompts 為空 → 維持 ATTACHED_NOT_READY（向後相容）。"""
        profile = _make_profile(bootloader_prompts=())
        mgr, bridge = self._make_manager_with_device(profile)

        bridge.rx_tail.return_value = "=> "

        resp = mgr.self_test("COM0")

        self.assertTrue(resp["ok"])
        self.assertEqual(resp["classification"], "ATTACHED_NOT_READY")
        self.assertEqual(resp["recommended_action"], "console_attach")

    def test_bootloader_prompts_no_match_falls_back_to_attached_not_ready(self) -> None:
        """bootloader_prompts 有值但 rx_tail 不符合 → ATTACHED_NOT_READY。"""
        profile = _make_profile(bootloader_prompts=(r"^CFE> ",))
        mgr, bridge = self._make_manager_with_device(profile)

        bridge.rx_tail.return_value = "linux login: "

        resp = mgr.self_test("COM0")

        self.assertTrue(resp["ok"])
        self.assertEqual(resp["classification"], "ATTACHED_NOT_READY")

    def test_passthrough_takes_priority_over_bootloader(self) -> None:
        """passthrough platform 優先於 BOOTLOADER 分類。"""
        profile = dataclasses.replace(
            _make_profile(bootloader_prompts=(r"^=> ",)),
            platform="passthrough",
            ready_probe="",
        )
        mgr, bridge = self._make_manager_with_device(profile)
        bridge.rx_tail.return_value = "=> "

        resp = mgr.self_test("COM0")

        self.assertEqual(resp["classification"], "PASSTHROUGH")

    def test_login_required_takes_priority_over_bootloader(self) -> None:
        """last_error == LOGIN_REQUIRED 優先於 BOOTLOADER 分類。"""
        profile = _make_profile(bootloader_prompts=(r"^=> ",))
        mgr, bridge = self._make_manager_with_device(profile)
        bridge.rx_tail.return_value = "=> "

        session = mgr.get_session("COM0")
        assert session is not None
        session.last_error = "LOGIN_REQUIRED"

        resp = mgr.self_test("COM0")

        self.assertEqual(resp["classification"], "LOGIN_REQUIRED")

    def test_rebooting_takes_priority_over_bootloader(self) -> None:
        """last_error == REBOOTING 優先於 BOOTLOADER 分類。"""
        profile = _make_profile(bootloader_prompts=(r"^=> ",))
        mgr, bridge = self._make_manager_with_device(profile)
        bridge.rx_tail.return_value = "=> "

        session = mgr.get_session("COM0")
        assert session is not None
        session.last_error = "REBOOTING"

        resp = mgr.self_test("COM0")

        self.assertEqual(resp["classification"], "REBOOTING")

    def test_all_classifications_include_recovery_mode_false_when_no_lease(self) -> None:
        """所有 classification result 應包含 recovery_mode False（無 lease）。"""
        profile = _make_profile(bootloader_prompts=(r"^=> ",))
        mgr, bridge = self._make_manager_with_device(profile)
        bridge.rx_tail.return_value = "=> "

        resp = mgr.self_test("COM0")

        self.assertIn("recovery_mode", resp)
        self.assertFalse(resp["recovery_mode"])

    def test_bootloader_result_contains_lease_context_fields(self) -> None:
        """BOOTLOADER 結果應包含完整 _lease_context 欄位。"""
        profile = _make_profile(bootloader_prompts=(r"^=> ",))
        mgr, bridge = self._make_manager_with_device(profile)
        bridge.rx_tail.return_value = "=> "

        resp = mgr.self_test("COM0")

        self.assertIn("interactive_owner", resp)
        self.assertIn("human_attached", resp)
        self.assertIn("recovery_mode", resp)


# ──────────────────────────────────────────────
# 3. Recovery lease 基礎 schema 測試
# ──────────────────────────────────────────────

class TestRecoveryLeaseSchema(unittest.TestCase):
    """測試 InteractiveLease / SessionRuntime 的 recovery_mode / suspended_human schema。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._old_state_path = sm_mod.STATE_PATH
        sm_mod.STATE_PATH = str(Path(self._tmp.name) / "state.json")

    def tearDown(self) -> None:
        sm_mod.STATE_PATH = self._old_state_path

    def _make_manager(self) -> SessionManager:
        profile = _make_profile()
        return SessionManager(
            [profile],
            WalWriter(wal_dir=self._tmp.name),
            on_ready=lambda _sid: None,
            on_detached=lambda _sid: None,
        )

    def test_interactive_lease_recovery_mode_defaults_false(self) -> None:
        """`InteractiveLease.recovery_mode` 預設值應為 False。"""
        lease = InteractiveLease(
            interactive_id="lid",
            session_id="p:COM0",
            owner="agent:test",
            created_at="2025-01-01T00:00:00+00:00",
            timeout_s=60.0,
        )
        self.assertFalse(lease.recovery_mode)

    def test_interactive_lease_suspended_human_defaults_false(self) -> None:
        """`InteractiveLease.suspended_human` 預設值應為 False。"""
        lease = InteractiveLease(
            interactive_id="lid",
            session_id="p:COM0",
            owner="agent:test",
            created_at="2025-01-01T00:00:00+00:00",
            timeout_s=60.0,
        )
        self.assertFalse(lease.suspended_human)

    def test_interactive_lease_recovery_mode_can_be_set_true(self) -> None:
        """可以明確設定 `recovery_mode=True`。"""
        lease = InteractiveLease(
            interactive_id="lid",
            session_id="p:COM0",
            owner="agent:test",
            created_at="2025-01-01T00:00:00+00:00",
            timeout_s=60.0,
            recovery_mode=True,
        )
        self.assertTrue(lease.recovery_mode)

    def test_session_runtime_stashed_human_lease_defaults_none(self) -> None:
        """`SessionRuntime._stashed_human_lease` 預設值應為 None。"""
        runtime = SessionRuntime(session_id="p:COM0", profile=_make_profile())
        self.assertIsNone(runtime._stashed_human_lease)

    def test_lease_context_recovery_mode_true_when_lease_has_recovery_mode(self) -> None:
        """`_lease_context` 在 lease.recovery_mode=True 時回傳 recovery_mode: True。"""
        mgr = self._make_manager()
        lease = InteractiveLease(
            interactive_id="lid",
            session_id="p:COM0",
            owner="agent:test",
            created_at="2025-01-01T00:00:00+00:00",
            timeout_s=60.0,
            recovery_mode=True,
        )
        ctx = mgr._lease_context(lease)
        self.assertTrue(ctx["recovery_mode"])

    def test_lease_context_recovery_mode_false_when_lease_is_none(self) -> None:
        """`_lease_context(None)` 回傳 recovery_mode: False。"""
        ctx = self._make_manager()._lease_context(None)
        self.assertFalse(ctx["recovery_mode"])

    def test_lease_context_recovery_mode_false_when_lease_not_recovery(self) -> None:
        """`_lease_context` 在 lease.recovery_mode=False 時回傳 recovery_mode: False。"""
        mgr = self._make_manager()
        lease = InteractiveLease(
            interactive_id="lid",
            session_id="p:COM0",
            owner="agent:test",
            created_at="2025-01-01T00:00:00+00:00",
            timeout_s=60.0,
            recovery_mode=False,
        )
        ctx = mgr._lease_context(lease)
        self.assertFalse(ctx["recovery_mode"])

    def test_suspended_human_not_exposed_in_lease_context(self) -> None:
        """`suspended_human` 是內部 flag，不應出現在 _lease_context 結果中。"""
        mgr = self._make_manager()
        lease = InteractiveLease(
            interactive_id="lid",
            session_id="p:COM0",
            owner="agent:test",
            created_at="2025-01-01T00:00:00+00:00",
            timeout_s=60.0,
            suspended_human=True,
        )
        ctx = mgr._lease_context(lease)
        self.assertNotIn("suspended_human", ctx)


if __name__ == "__main__":
    unittest.main()
