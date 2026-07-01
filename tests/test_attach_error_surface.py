"""#94: attach_session 同步 probe 失敗時應浮出頂層 error（方案 C）。

實機重現（2026-07-01, ttyUSB1/AQ00OAQ7）：對 ATTACHED-but-not-ready 的 session 下
`session attach` 回 ok:True、頂層無 error_code，真錯埋進 session.last_error → 上層
拿到空 error（#94「冒號後空白」）。

方案 C：attach 進入點在 session 未達 READY 時回 ok:False + 頂層 error_code，
不動共用 helper `_probe_existing_bridge`（保留 reprobe/recover 的 stay-ATTACHED 語意）。
"""
from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from sw_core.cli import main as cli_main
from sw_core.config import SessionProfile, UartProfile
from sw_core.device_watcher import DeviceInfo
from sw_core.session_manager import SessionManager
import sw_core.session_manager as sm_mod
from sw_core.wal import WalWriter


class _StubClient:
    """替換 sw_core.cli.rpc_call，回固定回應。"""

    def __init__(self, responses: dict[str, object]) -> None:
        self.responses = responses

    def __call__(self, endpoint: str, method: str, params: dict | None = None,
                 timeout_s: float = 5.0) -> object:
        return self.responses.get(method, {"ok": True})


def _make_profile(by_id: str = "/dev/serial/by-id/dev0") -> SessionProfile:
    return SessionProfile(
        profile_name="p", com="COM0", act_no=1, alias="lab",
        device_by_id=by_id, platform="prpl", uart=UartProfile(),
    )


class TestAttachErrorSurface(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._old_state_path = sm_mod.STATE_PATH
        sm_mod.STATE_PATH = str(Path(self._tmp.name) / "state.json")
        self.addCleanup(lambda: setattr(sm_mod, "STATE_PATH", self._old_state_path))

    def _mgr(self, profile: SessionProfile) -> SessionManager:
        return SessionManager(
            [profile], WalWriter(wal_dir=self._tmp.name),
            on_ready=lambda _: None, on_detached=lambda _: None,
        )

    def test_attach_probe_fail_surfaces_top_level_error(self) -> None:
        """attach 對 ATTACHED session 同步 probe 失敗 → 回 ok:False + 頂層 error_code。

        現行 bug：`_probe_existing_bridge` else 分支一律回 ok:True、錯誤只埋
        `session.last_error`，頂層 error_code 為 None。
        """
        by_id = "/dev/serial/by-id/dev0"
        profile = _make_profile(by_id=by_id)
        mgr = self._mgr(profile)
        session = mgr.get_session("COM0")
        assert session is not None
        # 註冊 device，否則 attach_session 於 by_id not in _devices 早退 DEVICE_NOT_FOUND
        mgr._devices = {by_id: DeviceInfo(by_id=by_id, real_path="/dev/ttyUSB0")}
        # ATTACHED + bridge → 命中 attach_session 的同步 probe 分支
        session.state = "ATTACHED"
        session.bridge = mock.MagicMock()

        with mock.patch(
            "sw_core.session_manager.probe_ready",
            return_value=(False, "PROMPT_UNAVAILABLE"),
        ):
            result = mgr.attach_session("COM0")

        # 方案 C：未達 READY 應誠實回 ok:False + 頂層具體 error_code（非埋在 session 內）
        self.assertFalse(result["ok"], "attach 同步 probe 失敗不應謊報 ok:True")
        self.assertEqual(
            result.get("error_code"), "PROMPT_UNAVAILABLE",
            "頂層應帶具體 error_code，而非 None",
        )
        # 不破壞共用 helper 語意：session 仍應留在 ATTACHED、last_error 保留
        self.assertEqual(result["session"]["state"], "ATTACHED")
        self.assertEqual(result["session"]["last_error"], "PROMPT_UNAVAILABLE")

    def test_attach_passthrough_not_ready_still_ok(self) -> None:
        """passthrough（非 command_capable）停在 ATTACHED 即成功，attach 不應改寫成 ok:False。

        守護 command_capable gate：移除該 gate 會讓 passthrough 的 ATTACHED 被誤判成失敗。
        """
        by_id = "/dev/serial/by-id/dev0"
        profile = SessionProfile(
            profile_name="p", com="COM0", act_no=1, alias="lab",
            device_by_id=by_id, platform="passthrough", ready_probe="",
            uart=UartProfile(),
        )
        mgr = self._mgr(profile)
        session = mgr.get_session("COM0")
        assert session is not None
        self.assertFalse(session.profile.command_capable)  # 前置：確實非 command_capable
        mgr._devices = {by_id: DeviceInfo(by_id=by_id, real_path="/dev/ttyUSB0")}
        session.state = "ATTACHED"
        session.bridge = mock.MagicMock()

        result = mgr.attach_session("COM0")

        self.assertTrue(result["ok"], "passthrough ATTACHED 是成功狀態，不應回 ok:False")
        self.assertEqual(result["session"]["state"], "ATTACHED")

    def test_attach_reaches_ready_returns_ok(self) -> None:
        """happy-path 守護：command-capable session 同步 probe 成功達 READY → ok:True（fix 不 over-trigger）。"""
        by_id = "/dev/serial/by-id/dev0"
        profile = _make_profile(by_id=by_id)
        mgr = self._mgr(profile)
        session = mgr.get_session("COM0")
        assert session is not None
        mgr._devices = {by_id: DeviceInfo(by_id=by_id, real_path="/dev/ttyUSB0")}
        session.state = "ATTACHED"
        session.bridge = mock.MagicMock()

        with mock.patch("sw_core.session_manager.probe_ready", return_value=(True, None)):
            result = mgr.attach_session("COM0")

        self.assertTrue(result["ok"], "probe 成功達 READY 應回 ok:True")
        self.assertEqual(result["session"]["state"], "READY")
        self.assertIsNone(result["session"]["last_error"])

    def test_attach_passes_through_helper_ok_false(self) -> None:
        """helper 已回 ok:False（如 STATE_CHANGED）時，attach 應原樣直通、不再包裝一層。"""
        by_id = "/dev/serial/by-id/dev0"
        profile = _make_profile(by_id=by_id)
        mgr = self._mgr(profile)
        session = mgr.get_session("COM0")
        assert session is not None
        mgr._devices = {by_id: DeviceInfo(by_id=by_id, real_path="/dev/ttyUSB0")}
        session.state = "ATTACHED"
        session.bridge = mock.MagicMock()

        helper_result = {"ok": False, "error_code": "STATE_CHANGED", "session": {"state": "FLASHING"}}
        with mock.patch.object(mgr, "_probe_existing_bridge", return_value=helper_result):
            result = mgr.attach_session("COM0")

        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "STATE_CHANGED")
        self.assertEqual(result["session"]["state"], "FLASHING")

    def test_attach_human_lease_not_ready_surfaces_error(self) -> None:
        """有 human lease 早退分支時，command-capable + not-READY 仍應回 ok:False + 頂層 error_code。

        codex 對抗審查抓到的殘留缺口：human lease 分支在 probe 前就回 ok:True，繞過 #94 gate，
        把真錯（PROMPT_UNAVAILABLE）留在巢狀 session、頂層無 error_code（=#94 原缺陷、換觸發條件）。
        """
        by_id = "/dev/serial/by-id/dev0"
        profile = _make_profile(by_id=by_id)
        mgr = self._mgr(profile)
        session = mgr.get_session("COM0")
        assert session is not None
        mgr._devices = {by_id: DeviceInfo(by_id=by_id, real_path="/dev/ttyUSB0")}
        session.state = "ATTACHED"
        session.last_error = "PROMPT_UNAVAILABLE"
        session.bridge = mock.MagicMock()

        fake_lease = mock.MagicMock()
        fake_lease.owner = "human:tester"
        with mock.patch.object(
            mgr, "_refresh_interactive_locked",
            return_value=(fake_lease, mock.MagicMock()),
        ):
            result = mgr.attach_session("COM0")

        self.assertFalse(result["ok"], "human lease 不應讓 command-capable not-READY 回 ok:True")
        self.assertEqual(result.get("error_code"), "PROMPT_UNAVAILABLE")
        self.assertEqual(result["session"]["state"], "ATTACHED")

    def _attach_bridge_state(self, state: str):
        """helper：造 bridge 存在、無 human lease、指定 state 的 command-capable session 並 attach。"""
        by_id = "/dev/serial/by-id/dev0"
        profile = _make_profile(by_id=by_id)
        mgr = self._mgr(profile)
        session = mgr.get_session("COM0")
        assert session is not None
        mgr._devices = {by_id: DeviceInfo(by_id=by_id, real_path="/dev/ttyUSB0")}
        session.state = state
        session.bridge = mock.MagicMock()
        with mock.patch.object(
            mgr, "_refresh_interactive_locked", return_value=(None, mock.MagicMock())
        ):
            return mgr.attach_session("COM0")

    def test_attach_flashing_bridge_surfaces_flashing_busy(self) -> None:
        """bridge 存在的 command-capable FLASHING session → ok:False + FLASHING_BUSY（非 ok:True 埋錯誤）。

        codex 對抗審查第二輪：通用 bridge-present else 分支對 FLASHING/RECOVERING 一律回 ok:True。
        """
        result = self._attach_bridge_state("FLASHING")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "FLASHING_BUSY")
        self.assertEqual(result["session"]["state"], "FLASHING")

    def test_attach_recovering_bridge_surfaces_error(self) -> None:
        """bridge 存在的 command-capable RECOVERING session → ok:False + SESSION_RECOVERING。"""
        result = self._attach_bridge_state("RECOVERING")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "SESSION_RECOVERING")
        self.assertEqual(result["session"]["state"], "RECOVERING")

    def _attach_human_lease_state(self, state: str):
        """helper：造 bridge 存在、**有 human lease**、指定 state 的 command-capable session 並 attach。"""
        by_id = "/dev/serial/by-id/dev0"
        profile = _make_profile(by_id=by_id)
        mgr = self._mgr(profile)
        session = mgr.get_session("COM0")
        assert session is not None
        mgr._devices = {by_id: DeviceInfo(by_id=by_id, real_path="/dev/ttyUSB0")}
        session.state = state
        session.bridge = mock.MagicMock()
        fake_lease = mock.MagicMock()
        fake_lease.owner = "human:tester"
        with mock.patch.object(
            mgr, "_refresh_interactive_locked", return_value=(fake_lease, mock.MagicMock())
        ):
            return mgr.attach_session("COM0")

    def test_attach_human_lease_flashing_maps_flashing_busy(self) -> None:
        """human lease + FLASHING → 仍給狀態專用 FLASHING_BUSY（不被 human-lease 分支遮蔽成泛用碼）。

        codex 第三輪：human-lease 分支原本用泛用 error_code，遮蔽了 FLASHING/RECOVERING 專用碼。
        """
        result = self._attach_human_lease_state("FLASHING")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "FLASHING_BUSY")

    def test_attach_human_lease_recovering_maps_session_recovering(self) -> None:
        """human lease + RECOVERING → 仍給狀態專用 SESSION_RECOVERING。"""
        result = self._attach_human_lease_state("RECOVERING")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "SESSION_RECOVERING")

    def test_attach_release_during_probe_returns_released(self) -> None:
        """race：release_device 在同步 probe 進行中搶進 → attach 回一致的 released payload。

        codex 第四輪：probe（lock 外）期間被 release 時，helper 回 SESSION_NOT_READY/STATE_CHANGED，
        與文件承諾的 RELEASED ok:true 例外矛盾；attach 應 probe 後 re-check RELEASED 回 released payload。
        """
        by_id = "/dev/serial/by-id/dev0"
        profile = _make_profile(by_id=by_id)
        mgr = self._mgr(profile)
        session = mgr.get_session("COM0")
        assert session is not None
        mgr._devices = {by_id: DeviceInfo(by_id=by_id, real_path="/dev/ttyUSB0")}
        session.state = "ATTACHED"
        session.attached_real_path = "/dev/ttyUSB0"
        session.bridge = mock.MagicMock()

        def _release_mid_probe(*_a, **_k):
            mgr.release_device("COM0")  # release 在 probe 進行中搶進
            return (False, "PROMPT_UNAVAILABLE")

        with mock.patch("sw_core.session_manager.probe_ready", side_effect=_release_mid_probe):
            result = mgr.attach_session("COM0")

        self.assertTrue(result["ok"], "release-during-probe 應回 released ok:true（非 SESSION_NOT_READY）")
        self.assertTrue(result.get("released"))
        self.assertEqual(result.get("recommended_action"), "device_attach")


class TestCliFailureStderr(unittest.TestCase):
    """#94 次因：CLI 失敗只印 stdout、stderr 全空 → 讀 stderr 的 consumer 拿到空 error。"""

    def test_run_rpc_failure_emits_stderr_line(self) -> None:
        """ok:False 回應應在 stderr 印一行含具體 error_code（stdout 仍為機器可解析 JSON）。"""
        stub = _StubClient({"session.attach": {"ok": False, "error_code": "PROMPT_UNAVAILABLE"}})
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sw_core.cli.rpc_call", stub):
            with redirect_stdout(out), redirect_stderr(err):
                rc = cli_main(["session", "attach", "--selector", "COM0"])

        # exit code 誠實（非零）
        self.assertEqual(rc, 2)
        # stderr 非空且含具體 error_code（現行 stderr 全空 → RED）
        self.assertIn("PROMPT_UNAVAILABLE", err.getvalue())
        # stdout 仍是機器可解析 JSON、ok:False
        self.assertFalse(json.loads(out.getvalue())["ok"])


if __name__ == "__main__":
    unittest.main()
