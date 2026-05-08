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
import time
import unittest
import unittest.mock as mock
import uuid as _uuid
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


# ──────────────────────────────────────────────
# 4. interactive_open allow_attached 功能測試（Phase B）
# ──────────────────────────────────────────────


class TestInteractiveOpenAllowAttached(unittest.TestCase):
    """測試 interactive_open(allow_attached=True) bootloader recovery lease 功能。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._old_state_path = sm_mod.STATE_PATH
        sm_mod.STATE_PATH = str(Path(self._tmp.name) / "state.json")

    def tearDown(self) -> None:
        sm_mod.STATE_PATH = self._old_state_path

    def _make_mgr_attached(
        self,
        bootloader_prompts: tuple[str, ...] = (r"^=> ",),
        bridge_running: bool = True,
        bridge_serial_alive: bool = True,
        bridge_vtty_alive: bool = True,
        rx_tail_str: str = "boot output\n=> ",
    ) -> tuple[SessionManager, SessionRuntime, mock.MagicMock]:
        from sw_core.device_watcher import DeviceInfo

        profile = _make_profile(bootloader_prompts=bootloader_prompts)
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
            "running": bridge_running,
            "serial_alive": bridge_serial_alive,
            "vtty_alive": bridge_vtty_alive,
            "vtty": "/dev/pts/9",
        }
        bridge.rx_tail.return_value = rx_tail_str
        bridge.console_has_external_peer.return_value = True

        session.bridge = bridge
        session.state = "ATTACHED"
        session.attached_real_path = "/dev/ttyUSB0"

        with mgr._lock:
            mgr._devices = {
                "/dev/serial/by-id/orig": DeviceInfo(
                    by_id="/dev/serial/by-id/orig", real_path="/dev/ttyUSB0"
                )
            }
        return mgr, session, bridge

    def _inject_human_lease(
        self,
        mgr: SessionManager,
        session: SessionRuntime,
        bridge: mock.MagicMock,
        client_id: str = "test_client",
    ) -> InteractiveLease:
        """注入 human interactive lease；同步更新 snapshot 的 interactive_owner。"""
        human_owner = f"human:{client_id}"
        current_snap = dict(bridge.snapshot.return_value)
        current_snap["interactive_owner"] = human_owner
        bridge.snapshot.return_value = current_snap
        bridge.console_has_external_peer.return_value = True

        lease = InteractiveLease(
            interactive_id=_uuid.uuid4().hex,
            session_id=session.session_id,
            owner=human_owner,
            created_at=sm_mod.now_iso(),
            timeout_s=86400.0,
        )
        with mgr._lock:
            mgr._interactive[lease.interactive_id] = lease
            session.interactive_session_id = lease.interactive_id
        return lease

    # ── 1: 預設 allow_attached=False 拒絕 ATTACHED ─────────────────────

    def test_default_rejects_attached_state(self) -> None:
        """interactive_open 預設（allow_attached=False）對 ATTACHED session 回傳 SESSION_NOT_READY。"""
        mgr, session, bridge = self._make_mgr_attached()
        resp = mgr.interactive_open("COM0", owner="agent")
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error_code"], "SESSION_NOT_READY")

    # ── 2: allow_attached=False 明確拒絕 ATTACHED ──────────────────────

    def test_allow_attached_false_rejects_attached(self) -> None:
        """allow_attached=False 明確時同樣拒絕 ATTACHED。"""
        mgr, session, bridge = self._make_mgr_attached()
        resp = mgr.interactive_open("COM0", owner="agent", allow_attached=False)
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error_code"], "SESSION_NOT_READY")

    # ── 3: allow_attached=True 無 bootloader match 回傳 NOT_BOOTLOADER ──

    def test_allow_attached_no_bootloader_match(self) -> None:
        """ATTACHED + no bootloader match → SESSION_NOT_READY + error_detail NOT_BOOTLOADER。"""
        mgr, session, bridge = self._make_mgr_attached(
            bootloader_prompts=(r"^=> ",),
            rx_tail_str="linux login: ",
        )
        resp = mgr.interactive_open("COM0", owner="agent", allow_attached=True)
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error_code"], "SESSION_NOT_READY")
        self.assertEqual(resp.get("error_detail"), "NOT_BOOTLOADER")

    # ── 4: unhealthy bridge ────────────────────────────────────────────

    def test_allow_attached_rejects_serial_not_alive(self) -> None:
        """bridge serial_alive=False → SESSION_NOT_READY。"""
        mgr, session, bridge = self._make_mgr_attached(bridge_serial_alive=False)
        resp = mgr.interactive_open("COM0", owner="agent", allow_attached=True)
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error_code"], "SESSION_NOT_READY")

    def test_allow_attached_rejects_bridge_not_running(self) -> None:
        """bridge running=False → SESSION_NOT_READY。"""
        mgr, session, bridge = self._make_mgr_attached(bridge_running=False)
        resp = mgr.interactive_open("COM0", owner="agent", allow_attached=True)
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error_code"], "SESSION_NOT_READY")

    def test_allow_attached_rejects_vtty_not_alive(self) -> None:
        """bridge vtty_alive=False → SESSION_NOT_READY。"""
        mgr, session, bridge = self._make_mgr_attached(bridge_vtty_alive=False)
        resp = mgr.interactive_open("COM0", owner="agent", allow_attached=True)
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error_code"], "SESSION_NOT_READY")

    # ── 5: 開啟 recovery lease（無 human lease）────────────────────────

    def test_allow_attached_opens_recovery_lease_no_human(self) -> None:
        """ATTACHED + bootloader + no existing lease → recovery lease, recovery_mode True。"""
        mgr, session, bridge = self._make_mgr_attached()
        resp = mgr.interactive_open("COM0", owner="agent:test", allow_attached=True)
        self.assertTrue(resp["ok"])
        self.assertTrue(resp["recovery_mode"])
        self.assertIn("interactive_id", resp)
        iid = resp["interactive_id"]
        self.assertIn(iid, mgr._interactive)
        lease = mgr._interactive[iid]
        self.assertTrue(lease.recovery_mode)
        self.assertFalse(lease.suspended_human)
        bridge.suspend_interactive.assert_not_called()

    # ── 6: stash existing human lease ─────────────────────────────────

    def test_allow_attached_stashes_human_lease(self) -> None:
        """ATTACHED + bootloader + existing human lease → stash, suspend called, recovery lease opened。"""
        mgr, session, bridge = self._make_mgr_attached()
        human_lease = self._inject_human_lease(mgr, session, bridge, "client1")
        human_id = human_lease.interactive_id

        resp = mgr.interactive_open("COM0", owner="agent:test", allow_attached=True)
        self.assertTrue(resp["ok"])
        self.assertTrue(resp["recovery_mode"])
        self.assertNotIn(human_id, mgr._interactive)
        new_iid = resp["interactive_id"]
        self.assertEqual(session.interactive_session_id, new_iid)
        self.assertIsNotNone(session._stashed_human_lease)
        self.assertEqual(session._stashed_human_lease.interactive_id, human_id)
        recovery_lease = mgr._interactive[new_iid]
        self.assertTrue(recovery_lease.suspended_human)
        self.assertTrue(recovery_lease.recovery_mode)
        bridge.suspend_interactive.assert_called_once()

    # ── 7: 拒絕既有 agent lease ───────────────────────────────────────

    def test_allow_attached_rejects_existing_agent_lease(self) -> None:
        """ATTACHED + bootloader + existing agent lease → SESSION_INTERACTIVE_BUSY。"""
        mgr, session, bridge = self._make_mgr_attached()
        agent_lease = InteractiveLease(
            interactive_id=_uuid.uuid4().hex,
            session_id=session.session_id,
            owner="agent:other",
            created_at=sm_mod.now_iso(),
            timeout_s=60.0,
        )
        with mgr._lock:
            mgr._interactive[agent_lease.interactive_id] = agent_lease
            session.interactive_session_id = agent_lease.interactive_id

        resp = mgr.interactive_open("COM0", owner="agent:test", allow_attached=True)
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error_code"], "SESSION_INTERACTIVE_BUSY")
        self.assertIsNone(session._stashed_human_lease)
        bridge.suspend_interactive.assert_not_called()

    # ── 8: recovery close restores stash（peer alive, not expired）──────

    def test_recovery_close_restores_stash(self) -> None:
        """recovery close 後若 stash 有效且 peer alive → resume 且 human lease 還原。"""
        mgr, session, bridge = self._make_mgr_attached()
        human_lease = self._inject_human_lease(mgr, session, bridge, "client1")
        human_id = human_lease.interactive_id

        resp = mgr.interactive_open("COM0", owner="agent:test", allow_attached=True)
        self.assertTrue(resp["ok"])
        recovery_iid = resp["interactive_id"]
        bridge.console_has_external_peer.return_value = True

        close_resp = mgr.interactive_close(recovery_iid)
        self.assertTrue(close_resp["ok"])
        bridge.resume_interactive.assert_called_once()
        self.assertIn(human_id, mgr._interactive)
        self.assertEqual(session.interactive_session_id, human_id)
        self.assertIsNone(session._stashed_human_lease)

    # ── 9: recovery close discards expired stash ──────────────────────

    def test_recovery_close_discards_expired_stash(self) -> None:
        """stash expired → resume called, bridge owner None, stash discarded。"""
        mgr, session, bridge = self._make_mgr_attached()
        human_lease = self._inject_human_lease(mgr, session, bridge, "client1")
        resp = mgr.interactive_open("COM0", owner="agent:test", allow_attached=True)
        self.assertTrue(resp["ok"])
        recovery_iid = resp["interactive_id"]

        session._stashed_human_lease.last_activity_at = time.monotonic() - 999999.0
        bridge.console_has_external_peer.return_value = True

        close_resp = mgr.interactive_close(recovery_iid)
        self.assertTrue(close_resp["ok"])
        bridge.resume_interactive.assert_called_once()
        self.assertIsNone(session.interactive_session_id)
        self.assertIsNone(session._stashed_human_lease)
        none_calls = [c for c in bridge.set_interactive_owner.call_args_list if c == mock.call(None)]
        self.assertTrue(len(none_calls) >= 1, f"set_interactive_owner(None) 至少需呼叫一次，actual: {bridge.set_interactive_owner.call_args_list}")

    # ── 10: recovery close discards stash when human detached ─────────

    def test_recovery_close_discards_stash_human_detached(self) -> None:
        """stash valid but human detached (peer not alive) → resume, ghost owner cleared。"""
        mgr, session, bridge = self._make_mgr_attached()
        human_lease = self._inject_human_lease(mgr, session, bridge, "client1")

        resp = mgr.interactive_open("COM0", owner="agent:test", allow_attached=True)
        self.assertTrue(resp["ok"])
        recovery_iid = resp["interactive_id"]
        bridge.console_has_external_peer.return_value = False

        close_resp = mgr.interactive_close(recovery_iid)
        self.assertTrue(close_resp["ok"])
        bridge.resume_interactive.assert_called_once()
        self.assertIsNone(session.interactive_session_id)
        self.assertIsNone(session._stashed_human_lease)
        none_calls = [c for c in bridge.set_interactive_owner.call_args_list if c == mock.call(None)]
        self.assertTrue(len(none_calls) >= 1)

    # ── 11: timeout clamp for recovery lease ──────────────────────────

    def test_recovery_lease_timeout_clamped(self) -> None:
        """timeout > MAX_RECOVERY_LEASE_S → clamped to MAX_RECOVERY_LEASE_S。"""
        from sw_core.constants import MAX_RECOVERY_LEASE_S
        mgr, session, bridge = self._make_mgr_attached()
        resp = mgr.interactive_open("COM0", owner="agent:test", allow_attached=True, timeout_s=9999.0)
        self.assertTrue(resp["ok"])
        lease = mgr._interactive[resp["interactive_id"]]
        self.assertAlmostEqual(lease.timeout_s, MAX_RECOVERY_LEASE_S, places=1)

    def test_recovery_lease_small_timeout_passthrough(self) -> None:
        """timeout < MAX_RECOVERY_LEASE_S → 保留原值。"""
        from sw_core.constants import MAX_RECOVERY_LEASE_S
        small_timeout = MAX_RECOVERY_LEASE_S - 10.0
        mgr, session, bridge = self._make_mgr_attached()
        resp = mgr.interactive_open("COM0", owner="agent:test", allow_attached=True, timeout_s=small_timeout)
        self.assertTrue(resp["ok"])
        lease = mgr._interactive[resp["interactive_id"]]
        self.assertAlmostEqual(lease.timeout_s, small_timeout, places=1)

    def test_ready_path_allow_attached_no_clamp(self) -> None:
        """READY + allow_attached=True → timeout 不 clamp，recovery_mode False。"""
        from sw_core.constants import MAX_RECOVERY_LEASE_S
        mgr, session, bridge = self._make_mgr_attached()
        session.state = "READY"
        big_timeout = MAX_RECOVERY_LEASE_S + 100.0
        resp = mgr.interactive_open("COM0", owner="agent:test", allow_attached=True, timeout_s=big_timeout)
        self.assertTrue(resp["ok"])
        self.assertFalse(resp.get("recovery_mode", True))
        lease = mgr._interactive[resp["interactive_id"]]
        self.assertAlmostEqual(lease.timeout_s, big_timeout, places=1)

    # ── 12: interactive_send 在 recovery lease 下正常運作 ────────────

    def test_interactive_send_plain_bytes_during_recovery(self) -> None:
        """recovery lease 下 interactive_send plain bytes 正常傳送。"""
        mgr, session, bridge = self._make_mgr_attached()
        resp = mgr.interactive_open("COM0", owner="agent:test", allow_attached=True)
        iid = resp["interactive_id"]
        send_resp = mgr.interactive_send(iid, data="reset\n", encoding="plain")
        self.assertTrue(send_resp["ok"])
        bridge.send_bytes.assert_called_once()
        args, _ = bridge.send_bytes.call_args
        self.assertEqual(args[0], b"reset\n")

    def test_interactive_send_key_during_recovery(self) -> None:
        """recovery lease 下 interactive_send key（enter）正常傳送。"""
        mgr, session, bridge = self._make_mgr_attached()
        resp = mgr.interactive_open("COM0", owner="agent:test", allow_attached=True)
        iid = resp["interactive_id"]
        send_resp = mgr.interactive_send(iid, data="enter", encoding="key")
        self.assertTrue(send_resp["ok"])
        args, _ = bridge.send_bytes.call_args
        self.assertEqual(args[0], b"\n")

    # ── 13: interactive_send expired recovery restores stash ──────────

    def test_interactive_send_expired_recovery_restores_and_returns_expired(self) -> None:
        """send 到 expired recovery lease → 恢復 stash，回 INTERACTIVE_EXPIRED。"""
        mgr, session, bridge = self._make_mgr_attached()
        human_lease = self._inject_human_lease(mgr, session, bridge, "client1")
        human_id = human_lease.interactive_id

        resp = mgr.interactive_open("COM0", owner="agent:test", allow_attached=True)
        iid = resp["interactive_id"]
        mgr._interactive[iid].last_activity_at = time.monotonic() - 9999.0
        bridge.console_has_external_peer.return_value = True

        send_resp = mgr.interactive_send(iid, data="x", encoding="plain")
        self.assertFalse(send_resp["ok"])
        self.assertEqual(send_resp["error_code"], "INTERACTIVE_EXPIRED")
        self.assertIn(human_id, mgr._interactive)
        self.assertEqual(session.interactive_session_id, human_id)
        bridge.resume_interactive.assert_called_once()

    # ── 14: interactive_status 包含 recovery_mode ─────────────────────

    def test_interactive_status_recovery_mode_true(self) -> None:
        """interactive_status 對 recovery lease 回傳 recovery_mode: True。"""
        mgr, session, bridge = self._make_mgr_attached()
        # 第一次 rx_tail 用於 interactive_open 的 bootloader prompt 比對
        # 第二次 rx_tail 用於 interactive_status 的 screen snapshot
        bridge.rx_tail.side_effect = ["boot output\n=> ", "screen output"]
        resp = mgr.interactive_open("COM0", owner="agent:test", allow_attached=True)
        iid = resp["interactive_id"]
        status_resp = mgr.interactive_status(iid)
        self.assertTrue(status_resp["ok"])
        self.assertTrue(status_resp.get("recovery_mode"), f"expected recovery_mode=True, got: {status_resp}")

    def test_interactive_status_normal_lease_recovery_mode_false(self) -> None:
        """interactive_status 對普通 READY lease 回傳 recovery_mode: False。"""
        mgr, session, bridge = self._make_mgr_attached()
        session.state = "READY"
        bridge.rx_tail.return_value = "screen output"
        resp = mgr.interactive_open("COM0", owner="agent:test")
        iid = resp["interactive_id"]
        status_resp = mgr.interactive_status(iid)
        self.assertTrue(status_resp["ok"])
        self.assertFalse(status_resp.get("recovery_mode", True))

    # ── 15: interactive_status expired recovery closes/restores ────────

    def test_interactive_status_expired_recovery_closes_restores(self) -> None:
        """status 對 expired recovery lease → close/restore，回 INTERACTIVE_EXPIRED。"""
        mgr, session, bridge = self._make_mgr_attached()
        human_lease = self._inject_human_lease(mgr, session, bridge, "client1")
        human_id = human_lease.interactive_id

        resp = mgr.interactive_open("COM0", owner="agent:test", allow_attached=True)
        iid = resp["interactive_id"]
        mgr._interactive[iid].last_activity_at = time.monotonic() - 9999.0
        bridge.console_has_external_peer.return_value = True

        status_resp = mgr.interactive_status(iid)
        self.assertFalse(status_resp["ok"])
        self.assertEqual(status_resp["error_code"], "INTERACTIVE_EXPIRED")
        self.assertIn(human_id, mgr._interactive)
        bridge.resume_interactive.assert_called_once()

    # ── 16: second recovery open after expired not busy ───────────────

    def test_second_recovery_open_after_expired_not_busy(self) -> None:
        """expired recovery lease 後再次 interactive_open → 不回 BUSY。"""
        mgr, session, bridge = self._make_mgr_attached()
        resp1 = mgr.interactive_open("COM0", owner="agent:test", allow_attached=True)
        iid1 = resp1["interactive_id"]
        mgr._interactive[iid1].last_activity_at = time.monotonic() - 9999.0

        resp2 = mgr.interactive_open("COM0", owner="agent:test", allow_attached=True)
        self.assertTrue(resp2["ok"], f"expected ok, got: {resp2}")
        self.assertNotEqual(resp2.get("interactive_id"), iid1)

    # ── 17: _detach_session_locked clears _stashed_human_lease ─────────

    def test_detach_session_clears_stashed_human_lease(self) -> None:
        """detach 後 session._stashed_human_lease 應被清除。"""
        mgr, session, bridge = self._make_mgr_attached()
        self._inject_human_lease(mgr, session, bridge, "client1")
        resp = mgr.interactive_open("COM0", owner="agent:test", allow_attached=True)
        self.assertTrue(resp["ok"])
        self.assertIsNotNone(session._stashed_human_lease)

        with mgr._lock:
            mgr._detach_session_locked(session, reason="TEST_DETACH")

        self.assertIsNone(session._stashed_human_lease)

    # ── 18: interactive_open READY 結果包含 recovery_mode: False ────────

    def test_interactive_open_ready_result_has_recovery_mode_false(self) -> None:
        """interactive_open 在 READY 下結果應包含 recovery_mode: False。"""
        mgr, session, bridge = self._make_mgr_attached()
        session.state = "READY"
        resp = mgr.interactive_open("COM0", owner="agent:test")
        self.assertTrue(resp["ok"])
        self.assertIn("recovery_mode", resp)
        self.assertFalse(resp["recovery_mode"])


# ──────────────────────────────────────────────
# 5. RPC / CLI / MCP passthrough 測試（Phase B）
# ──────────────────────────────────────────────


class TestRpcCliMcpPassthrough(unittest.TestCase):
    """測試 service / CLI / MCP 是否正確傳遞 allow_attached。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._old_state_path = sm_mod.STATE_PATH
        sm_mod.STATE_PATH = str(Path(self._tmp.name) / "state.json")

    def tearDown(self) -> None:
        sm_mod.STATE_PATH = self._old_state_path

    def test_rpc_service_passes_allow_attached_true(self) -> None:
        """service.rpc('session.interactive_open') 應把 allow_attached=True 傳給 SessionManager。"""
        from sw_core.service import SerialwrapService

        profile = _make_profile()
        with (
            mock.patch("sw_core.service.DeviceWatcher"),
            mock.patch("sw_core.service.EventEngine"),
            mock.patch("sw_core.service.CommandArbiter"),
        ):
            svc = SerialwrapService([profile])
            svc._sessions = mock.MagicMock()
            svc._sessions.interactive_open.return_value = {
                "ok": True, "interactive_id": "x", "recovery_mode": False,
            }
            svc.rpc(
                "session.interactive_open",
                {"selector": "COM0", "allow_attached": True},
            )
            svc._sessions.interactive_open.assert_called_once_with(
                "COM0",
                owner="agent",
                timeout_s=60.0,
                command="",
                allow_attached=True,
            )

    def test_rpc_service_allow_attached_false_by_default(self) -> None:
        """allow_attached 未指定時預設為 False。"""
        from sw_core.service import SerialwrapService

        profile = _make_profile()
        with (
            mock.patch("sw_core.service.DeviceWatcher"),
            mock.patch("sw_core.service.EventEngine"),
            mock.patch("sw_core.service.CommandArbiter"),
        ):
            svc = SerialwrapService([profile])
            svc._sessions = mock.MagicMock()
            svc._sessions.interactive_open.return_value = {
                "ok": True, "interactive_id": "x", "recovery_mode": False,
            }
            svc.rpc("session.interactive_open", {"selector": "COM0"})
            call_kwargs = svc._sessions.interactive_open.call_args[1]
            self.assertFalse(call_kwargs.get("allow_attached", None))

    def test_cli_parser_has_allow_attached_flag(self) -> None:
        """CLI 'session interactive-open --help' 應顯示 --allow-attached。"""
        import subprocess
        import sys

        result = subprocess.run(
            [sys.executable, "/home/paul_chen/prj_pri/serialwrap/serialwrap",
             "session", "interactive-open", "--help"],
            capture_output=True,
            text=True,
            cwd="/home/paul_chen/prj_pri/serialwrap",
        )
        output = result.stdout + result.stderr
        self.assertIn("--allow-attached", output, f"--allow-attached not found in help:\n{output}")

    def test_mcp_tool_schema_has_allow_attached(self) -> None:
        """MCP serialwrap_open_interactive inputSchema 應包含 allow_attached 屬性。"""
        from sw_mcp import server as mcp_server

        tool = next(
            (t for t in mcp_server._TOOL_DEFS if t["name"] == "serialwrap_open_interactive"),
            None,
        )
        self.assertIsNotNone(tool, "serialwrap_open_interactive 不在 _TOOL_DEFS 中")
        props = tool["inputSchema"]["properties"]
        self.assertIn("allow_attached", props, f"allow_attached 不在 inputSchema 中: {props}")
        self.assertEqual(props["allow_attached"].get("type"), "boolean")


if __name__ == "__main__":
    unittest.main()
