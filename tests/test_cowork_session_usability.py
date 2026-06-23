"""issue #51：command_capable 判準與 PROFILE_NOT_COMMAND_CAPABLE 錯誤碼。

passthrough / others-template 等「僅 console」profile 過去永遠停在 ATTACHED，
`cmd submit` 卻回語意不清的 SESSION_NOT_READY。本測試驗證新行為：

- 以 ``command_capable = bool(profile.ready_probe.strip())`` 判定可否下命令。
- 非 command-capable 的 session 在 ATTACHED 下 `cmd submit` 應回
  ``PROFILE_NOT_COMMAND_CAPABLE``，而非 ``SESSION_NOT_READY``。
- 設有 ready_probe 的 target（含 passthrough）應能走正常 probe 進 READY，
  且 command_capable 判定為真。
"""
from __future__ import annotations

import os
import pty
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from sw_core.config import SessionProfile, UartProfile
from sw_core.constants import HUMAN_ACTIVE_WINDOW_S
from sw_core.session_manager import InteractiveLease, SessionManager
import sw_core.session_manager as sm_mod
from sw_core.uart_io import UARTBridge
from sw_core.wal import WalWriter


def _make_profile(
    *,
    name: str = "p",
    com: str = "COM0",
    alias: str = "lab+1",
    by_id: str = "/dev/serial/by-id/test",
    platform: str = "passthrough",
    ready_probe: str = "",
    prompt_regex: str = r"(?m)^root@.*[#$]\s*$",
) -> SessionProfile:
    return SessionProfile(
        profile_name=name,
        com=com,
        act_no=1,
        alias=alias,
        device_by_id=by_id,
        platform=platform,
        uart=UartProfile(),
        prompt_regex=prompt_regex,
        ready_probe=ready_probe,
    )


class TestCommandCapableGate(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._old_state_path = sm_mod.STATE_PATH
        sm_mod.STATE_PATH = str(Path(self._tmp.name) / "state.json")

    def tearDown(self) -> None:
        sm_mod.STATE_PATH = self._old_state_path

    def _make_manager(self, profiles: list[SessionProfile]) -> SessionManager:
        return SessionManager(
            profiles,
            WalWriter(wal_dir=self._tmp.name),
            on_ready=lambda _sid: None,
            on_detached=lambda _sid: None,
        )

    def test_empty_ready_probe_not_command_capable_cmd_submit(self) -> None:
        """空 ready_probe 的 passthrough session 在 ATTACHED 下，
        cmd submit 應回 PROFILE_NOT_COMMAND_CAPABLE。"""
        profiles = [_make_profile(platform="passthrough", ready_probe="")]
        mgr = self._make_manager(profiles)
        session = mgr.get_session("COM0")
        assert session is not None
        session.bridge = mock.MagicMock()
        session.state = "ATTACHED"

        # 公開狀態應標示為非 command-capable
        public = session.to_public_dict()
        self.assertFalse(public["command_capable"])

        # 走 service 的 cmd submit gate（_resolve_session_id）
        from sw_core.service import SerialwrapService

        with (
            mock.patch("sw_core.service.WalWriter"),
            mock.patch("sw_core.service.DeviceWatcher"),
        ):
            svc = SerialwrapService([])
        svc._sessions = mgr  # type: ignore[attr-defined]

        resp = svc.rpc("command.submit", {"selector": "COM0", "cmd": "ls"})
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error_code"], "PROFILE_NOT_COMMAND_CAPABLE")

    def test_command_capable_when_ready_probe_set(self) -> None:
        """profile 有 ready_probe 時 command_capable 為真，
        ATTACHED 下 cmd submit 不再回 PROFILE_NOT_COMMAND_CAPABLE
        （而是回 command-capable 但尚未 READY 的 SESSION_NOT_READY）。"""
        profiles = [
            _make_profile(platform="passthrough", ready_probe="echo __READY__${nonce}")
        ]
        mgr = self._make_manager(profiles)
        session = mgr.get_session("COM0")
        assert session is not None
        session.bridge = mock.MagicMock()
        session.state = "ATTACHED"

        public = session.to_public_dict()
        self.assertTrue(public["command_capable"])

        from sw_core.service import SerialwrapService

        with (
            mock.patch("sw_core.service.WalWriter"),
            mock.patch("sw_core.service.DeviceWatcher"),
        ):
            svc = SerialwrapService([])
        svc._sessions = mgr  # type: ignore[attr-defined]

        resp = svc.rpc("command.submit", {"selector": "COM0", "cmd": "ls"})
        self.assertFalse(resp["ok"])
        self.assertNotEqual(resp["error_code"], "PROFILE_NOT_COMMAND_CAPABLE")
        self.assertEqual(resp["error_code"], "SESSION_NOT_READY")

    def test_command_capable_when_prompt_only_no_ready_probe(self) -> None:
        """即便平台非 passthrough，只要 ready_probe 為空字串就視為非
        command-capable（判準改以 ready_probe 為準，非寫死 platform）。"""
        profiles = [_make_profile(platform="shell", ready_probe="   ")]
        mgr = self._make_manager(profiles)
        session = mgr.get_session("COM0")
        assert session is not None
        self.assertFalse(session.to_public_dict()["command_capable"])


class TestSelfTestTopLevelCommandCapable(unittest.TestCase):
    """#51 sub-task A：self_test 須在最外層 result dict 暴露 command_capable。

    現有實作僅把 command_capable 放進巢狀的 "session" dict（to_public_dict），
    呼叫端必須鑽進去才能取得。本測試要求 self_test 在所有分支的最外層
    return 都帶有 command_capable，包含 SESSION_NOT_FOUND 早退分支。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._old_state_path = sm_mod.STATE_PATH
        sm_mod.STATE_PATH = str(Path(self._tmp.name) / "state.json")

    def tearDown(self) -> None:
        sm_mod.STATE_PATH = self._old_state_path

    def _make_manager(self, profiles: list[SessionProfile]) -> SessionManager:
        return SessionManager(
            profiles,
            WalWriter(wal_dir=self._tmp.name),
            on_ready=lambda _sid: None,
            on_detached=lambda _sid: None,
        )

    def _attach_mock_bridge(self, session) -> None:
        """以 mock bridge 讓 self_test 能跑到 ATTACHED 分類分支。"""
        session.bridge = mock.MagicMock()
        session.bridge.snapshot.return_value = {
            "running": True,
            "serial_alive": True,
            "vtty_alive": True,
            "vtty": "/dev/pts/0",
        }
        session.state = "ATTACHED"

    def test_self_test_top_level_command_capable_false(self) -> None:
        """空 ready_probe 的 passthrough session：最外層 command_capable 為 False。"""
        profiles = [_make_profile(platform="passthrough", ready_probe="")]
        mgr = self._make_manager(profiles)
        session = mgr.get_session("COM0")
        assert session is not None
        self._attach_mock_bridge(session)

        result = mgr.self_test("COM0")
        self.assertIn("command_capable", result)
        self.assertIs(result["command_capable"], False)

    def test_self_test_top_level_command_capable_true(self) -> None:
        """有 ready_probe 的 session：最外層 command_capable 為 True。"""
        profiles = [
            _make_profile(platform="passthrough", ready_probe="echo __READY__${nonce}")
        ]
        mgr = self._make_manager(profiles)
        session = mgr.get_session("COM0")
        assert session is not None
        self._attach_mock_bridge(session)

        result = mgr.self_test("COM0")
        self.assertIn("command_capable", result)
        self.assertIs(result["command_capable"], True)

    def test_self_test_session_not_found_top_level_command_capable(self) -> None:
        """SESSION_NOT_FOUND 早退分支：最外層仍須帶 command_capable=False。"""
        mgr = self._make_manager([_make_profile()])
        result = mgr.self_test("COM-DOES-NOT-EXIST")
        self.assertEqual(result["error_code"], "SESSION_NOT_FOUND")
        self.assertIn("command_capable", result)
        self.assertIs(result["command_capable"], False)


class TestUbootTemplateProfile(unittest.TestCase):
    """#51 sub-task B：uboot-template profile 應為 command-capable。"""

    def test_uboot_template_is_command_capable(self) -> None:
        import re

        from sw_core.config import load_profiles

        repo_root = Path(__file__).resolve().parent.parent
        result = load_profiles(str(repo_root / "sw_core" / "assets" / "profiles"))
        by_name = {t.profile_name: t for t in result.templates}
        self.assertIn("uboot-template", by_name)

        tpl = by_name["uboot-template"]
        self.assertTrue(tpl.command_capable)
        self.assertTrue((tpl.ready_probe or "").strip())
        self.assertRegex("=> ", tpl.prompt_regex)
        # 確認其他 bootloader prompt 也能匹配（含實機 MT7988 的大寫 `U-Boot> `）
        self.assertTrue(re.search(tpl.prompt_regex, "u-boot> "))
        self.assertTrue(re.search(tpl.prompt_regex, "U-Boot> "))  # 實機驗證字串
        self.assertTrue(re.search(tpl.prompt_regex, "CFE> "))


class _FakeTarget:
    """以 pty 模擬 UART target，鏡像 tests/test_agent_defer_tx.py 的 FakeTarget。"""

    def __init__(self) -> None:
        self.master_fd, self.slave_fd = pty.openpty()
        self.slave_path = os.ttyname(self.slave_fd)

    def close(self) -> None:
        for fd in (self.master_fd, self.slave_fd):
            try:
                os.close(fd)
            except OSError:
                pass


class TestLastHumanInputAt(unittest.TestCase):
    """#53 sub-task 4：UARTBridge 須追蹤最後一次「真實 human owner 鍵入」時間。

    - 只有 human-OWNER 的直接 raw 送出分支會更新 `last_human_input_at`。
    - broker/非 owner 的 console RX 路徑（走 line-buffered 佇列）不得更新。
    - snapshot() 須回出 `last_human_input_at` 欄位。
    """

    def _make_target(self) -> _FakeTarget:
        try:
            return _FakeTarget()
        except OSError as exc:
            self.skipTest(f"pty not available in current environment: {exc}")

    def _make_bridge(self, td: str) -> tuple[UARTBridge, _FakeTarget]:
        target = self._make_target()
        self.addCleanup(target.close)
        bridge = UARTBridge("COM0", target.slave_path, UartProfile(), WalWriter(wal_dir=td))
        bridge.start()
        self.addCleanup(bridge.stop)
        return bridge, target

    def test_snapshot_has_last_human_input_at_none_before_keystroke(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            bridge, _target = self._make_bridge(td)
            snap = bridge.snapshot()
            self.assertIn("last_human_input_at", snap)
            self.assertIsNone(snap["last_human_input_at"])

    def test_human_owner_keystroke_updates_last_human_input_at(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            bridge, _target = self._make_bridge(td)
            attached = bridge.attach_console(label="human")
            client = bridge._clients[attached["client_id"]]
            bridge.set_interactive_owner(f"human:{client.client_id}")

            self.assertIsNone(bridge.snapshot()["last_human_input_at"])
            bridge._handle_console_rx(client, b"x")
            after = bridge.snapshot()["last_human_input_at"]
            self.assertIsInstance(after, float)

    def test_non_owner_rx_does_not_update_last_human_input_at(self) -> None:
        """非 owner（broker line-buffered 佇列路徑）的 console RX 不更新時間。"""
        with tempfile.TemporaryDirectory() as td:
            bridge, _target = self._make_bridge(td)
            attached = bridge.attach_console(label="observer")
            client = bridge._clients[attached["client_id"]]
            # 不設 interactive owner → 走 line-buffered on_console_line 路徑
            bridge._handle_console_rx(client, b"ls\n")
            self.assertIsNone(bridge.snapshot()["last_human_input_at"])

    def test_deferred_buffer_branch_does_not_update(self) -> None:
        """agent 執行中、human 被 suspend → deferred-buffer 分支不更新時間。"""
        with tempfile.TemporaryDirectory() as td:
            bridge, _target = self._make_bridge(td)
            attached = bridge.attach_console(label="human")
            client = bridge._clients[attached["client_id"]]
            bridge.set_interactive_owner(f"human:{client.client_id}")
            bridge.suspend_interactive()  # agent_active=True, owner 移到 suspended

            bridge._handle_console_rx(client, b"y")
            self.assertIsNone(bridge.snapshot()["last_human_input_at"])


class TestHumanActiveSemantics(unittest.TestCase):
    """#53 sub-task 5：self_test 須暴露 human_active（最近鍵入時間窗）。

    - human_active 僅在 human_attached 且 bridge 有 last_human_input_at 且
      age <= HUMAN_ACTIVE_WINDOW_S 時為 True。
    - human_attached 語意維持不變（只看 owner 是否 human:）。
    - 無 lease 時 human_attached / human_active 皆 False。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._old_state_path = sm_mod.STATE_PATH
        sm_mod.STATE_PATH = str(Path(self._tmp.name) / "state.json")

    def tearDown(self) -> None:
        sm_mod.STATE_PATH = self._old_state_path

    def _make_manager(self, profiles: list[SessionProfile]) -> SessionManager:
        return SessionManager(
            profiles,
            WalWriter(wal_dir=self._tmp.name),
            on_ready=lambda _sid: None,
            on_detached=lambda _sid: None,
        )

    def _setup_human_session(self, mgr: SessionManager, *, last_human_input_at):
        """在 session 上掛 human InteractiveLease 與 mock bridge。"""
        session = mgr.get_session("COM0")
        assert session is not None
        session.bridge = mock.MagicMock()
        session.bridge.snapshot.return_value = {
            "running": True,
            "serial_alive": True,
            "vtty_alive": True,
            "vtty": "/dev/pts/0",
            "interactive_owner": "human:c1",
            "last_human_input_at": last_human_input_at,
        }
        session.state = "ATTACHED"

        lease = InteractiveLease(
            interactive_id="iv-1",
            session_id=session.session_id,
            owner="human:c1",
            created_at="2026-06-17T00:00:00Z",
            timeout_s=3600.0,
        )
        mgr._interactive[lease.interactive_id] = lease
        session.interactive_session_id = lease.interactive_id
        return session

    def test_human_active_false_when_input_stale(self) -> None:
        profiles = [_make_profile(platform="passthrough", ready_probe="")]
        mgr = self._make_manager(profiles)
        # 最後鍵入在時間窗外（> 60s）
        self._setup_human_session(
            mgr, last_human_input_at=time.monotonic() - (HUMAN_ACTIVE_WINDOW_S + 10.0)
        )

        result = mgr.self_test("COM0")
        self.assertTrue(result["human_attached"])
        self.assertFalse(result["human_active"])

    def test_human_active_true_when_input_recent(self) -> None:
        profiles = [_make_profile(platform="passthrough", ready_probe="")]
        mgr = self._make_manager(profiles)
        self._setup_human_session(
            mgr, last_human_input_at=time.monotonic() - 1.0
        )

        result = mgr.self_test("COM0")
        self.assertTrue(result["human_attached"])
        self.assertTrue(result["human_active"])

    def test_human_active_false_when_never_typed(self) -> None:
        """human attached 但從未鍵入（last_human_input_at=None）→ human_active False。"""
        profiles = [_make_profile(platform="passthrough", ready_probe="")]
        mgr = self._make_manager(profiles)
        self._setup_human_session(mgr, last_human_input_at=None)

        result = mgr.self_test("COM0")
        self.assertTrue(result["human_attached"])
        self.assertFalse(result["human_active"])

    def test_no_lease_both_false(self) -> None:
        """無 lease（無 human attach）→ human_attached / human_active 皆 False。"""
        profiles = [_make_profile(platform="passthrough", ready_probe="")]
        mgr = self._make_manager(profiles)
        session = mgr.get_session("COM0")
        assert session is not None
        session.bridge = mock.MagicMock()
        session.bridge.snapshot.return_value = {
            "running": True,
            "serial_alive": True,
            "vtty_alive": True,
            "vtty": "/dev/pts/0",
            "interactive_owner": None,
            "last_human_input_at": None,
        }
        session.state = "ATTACHED"

        result = mgr.self_test("COM0")
        self.assertFalse(result["human_attached"])
        self.assertFalse(result["human_active"])


class TestSoftPreemptAndLiveness(unittest.TestCase):
    """#53 sub-task 6：閒置 human lease 可被 agent soft preempt（降級而非踢除）；
    死孤兒 console 由既有 liveness（peer-gone）在 self_test 時 detach。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._old_state_path = sm_mod.STATE_PATH
        sm_mod.STATE_PATH = str(Path(self._tmp.name) / "state.json")

    def tearDown(self) -> None:
        sm_mod.STATE_PATH = self._old_state_path

    def _make_manager(self, profiles: list[SessionProfile]) -> SessionManager:
        return SessionManager(
            profiles,
            WalWriter(wal_dir=self._tmp.name),
            on_ready=lambda _sid: None,
            on_detached=lambda _sid: None,
        )

    def _ready_session_with_human_lease(
        self, mgr: SessionManager, *, last_human_input_at, peer_alive: bool = True
    ):
        session = mgr.get_session("COM0")
        assert session is not None
        bridge = mock.MagicMock()
        bridge.snapshot.return_value = {
            "running": True,
            "serial_alive": True,
            "vtty_alive": True,
            "vtty": "/dev/pts/0",
            "interactive_owner": "human:c1",
            "last_human_input_at": last_human_input_at,
        }
        bridge.console_has_external_peer.return_value = peer_alive
        bridge.detach_console.return_value = True
        bridge.vtty_path = "/dev/pts/0"
        session.bridge = bridge
        session.state = "READY"
        lease = InteractiveLease(
            interactive_id="iv-1",
            session_id=session.session_id,
            owner="human:c1",
            created_at="2026-06-17T00:00:00Z",
            timeout_s=3600.0,
        )
        mgr._interactive[lease.interactive_id] = lease
        session.interactive_session_id = lease.interactive_id
        return session

    def test_agent_soft_preempts_idle_human(self) -> None:
        mgr = self._make_manager([_make_profile()])
        session = self._ready_session_with_human_lease(
            mgr, last_human_input_at=time.monotonic() - (HUMAN_ACTIVE_WINDOW_S + 10.0)
        )
        resp = mgr.interactive_open("COM0", owner="agent")
        self.assertTrue(resp["ok"], resp)
        self.assertTrue(resp.get("soft_preempted"))
        session.bridge.suspend_interactive.assert_called_once()
        self.assertIsNotNone(session._stashed_human_lease)
        self.assertEqual(session._stashed_human_lease.interactive_id, "iv-1")

    def test_agent_cannot_preempt_active_human(self) -> None:
        mgr = self._make_manager([_make_profile()])
        self._ready_session_with_human_lease(
            mgr, last_human_input_at=time.monotonic() - 1.0
        )
        resp = mgr.interactive_open("COM0", owner="agent")
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error_code"], "SESSION_INTERACTIVE_BUSY")
        self.assertFalse(resp.get("soft_preempted", False))

    def test_soft_preempt_restores_human_on_close(self) -> None:
        mgr = self._make_manager([_make_profile()])
        session = self._ready_session_with_human_lease(
            mgr, last_human_input_at=time.monotonic() - (HUMAN_ACTIVE_WINDOW_S + 10.0)
        )
        resp = mgr.interactive_open("COM0", owner="agent")
        agent_iv = resp["interactive_id"]

        close = mgr.interactive_close(agent_iv)
        self.assertTrue(close["ok"], close)
        # 關閉 agent lease 後，stash 的 human lease 應被還原
        self.assertEqual(session.interactive_session_id, "iv-1")
        session.bridge.resume_interactive.assert_called()

    def test_agent_lease_not_preempted_stays_busy(self) -> None:
        """READY 路徑既有為 agent lease 時，第二個 agent interactive_open 維持 BUSY（不 preempt）。"""
        mgr = self._make_manager([_make_profile()])
        session = mgr.get_session("COM0")
        assert session is not None
        bridge = mock.MagicMock()
        bridge.snapshot.return_value = {
            "running": True,
            "serial_alive": True,
            "vtty_alive": True,
            "vtty": "/dev/pts/0",
            "interactive_owner": "agent",
            "last_human_input_at": None,
        }
        bridge.console_has_external_peer.return_value = True
        bridge.vtty_path = "/dev/pts/0"
        session.bridge = bridge
        session.state = "READY"
        lease = InteractiveLease(
            interactive_id="iv-agent",
            session_id=session.session_id,
            owner="agent",
            created_at="2026-06-17T00:00:00Z",
            timeout_s=3600.0,
        )
        mgr._interactive[lease.interactive_id] = lease
        session.interactive_session_id = lease.interactive_id

        resp = mgr.interactive_open("COM0", owner="agent")
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error_code"], "SESSION_INTERACTIVE_BUSY")
        bridge.suspend_interactive.assert_not_called()

    def test_dead_orphan_detached_via_self_test(self) -> None:
        mgr = self._make_manager([_make_profile()])
        session = self._ready_session_with_human_lease(
            mgr,
            last_human_input_at=time.monotonic() - (HUMAN_ACTIVE_WINDOW_S + 10.0),
            peer_alive=False,
        )
        result = mgr.self_test("COM0")
        self.assertIsNone(result["interactive_owner"])
        self.assertFalse(result["human_attached"])
        self.assertIsNone(session.interactive_session_id)


class TestPassthroughFallback(unittest.TestCase):
    """#51 後續：新增第二個 passthrough template（uboot-template，command-capable）後，
    dynamic auto-detect 的「通用 passthrough fallback」必須仍是非 command-capable 的
    others-template，不可被 uboot-template 搶走（避免無對應 profile 的裝置被誤綁成可下命令）。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._old_state_path = sm_mod.STATE_PATH
        sm_mod.STATE_PATH = str(Path(self._tmp.name) / "state.json")

    def tearDown(self) -> None:
        sm_mod.STATE_PATH = self._old_state_path

    def _mgr_with_templates(self, templates):
        return SessionManager(
            [],
            WalWriter(wal_dir=self._tmp.name),
            templates=templates,
            on_ready=lambda _sid: None,
            on_detached=lambda _sid: None,
        )

    def test_fallback_prefers_non_command_capable_passthrough(self) -> None:
        from sw_core.config import ProfileTemplate

        # uboot-template（command-capable）刻意排在前面
        uboot = ProfileTemplate(
            profile_name="uboot-template",
            platform="passthrough",
            ready_probe="echo __READY__${nonce}",
        )
        others = ProfileTemplate(
            profile_name="others-template",
            platform="passthrough",
            ready_probe="",
        )
        mgr = self._mgr_with_templates([uboot, others])
        fallback = mgr._default_passthrough_template()
        self.assertIsNotNone(fallback)
        self.assertEqual(fallback.profile_name, "others-template")

    def test_fallback_uses_any_passthrough_when_no_generic(self) -> None:
        """若沒有非 command-capable 的 passthrough，退而用任一 passthrough（向後相容）。"""
        from sw_core.config import ProfileTemplate

        uboot = ProfileTemplate(
            profile_name="uboot-template",
            platform="passthrough",
            ready_probe="echo __READY__${nonce}",
        )
        mgr = self._mgr_with_templates([uboot])
        fallback = mgr._default_passthrough_template()
        self.assertIsNotNone(fallback)
        self.assertEqual(fallback.profile_name, "uboot-template")


if __name__ == "__main__":
    unittest.main()
