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

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sw_core.config import SessionProfile, UartProfile
from sw_core.session_manager import SessionManager
import sw_core.session_manager as sm_mod
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
        result = load_profiles(str(repo_root / "profiles"))
        by_name = {t.profile_name: t for t in result.templates}
        self.assertIn("uboot-template", by_name)

        tpl = by_name["uboot-template"]
        self.assertTrue(tpl.command_capable)
        self.assertTrue((tpl.ready_probe or "").strip())
        self.assertRegex("=> ", tpl.prompt_regex)
        # 確認其他 bootloader prompt 也能匹配
        self.assertTrue(re.search(tpl.prompt_regex, "u-boot> "))
        self.assertTrue(re.search(tpl.prompt_regex, "CFE> "))


if __name__ == "__main__":
    unittest.main()
