"""profile 的 timeout_recovery_keys：命令逾時後送哪些恢復按鍵。

預設 [CTRL_C, CTRL_D]，與既有行為相同。CTRL_D 落在已登入 shell 的空提示符上＝EOF＝
登出（prplOS 的 askconsole 之後要人按 Enter），所以 profile 可設 [CTRL_C] 只送 CTRL_C，
或 [] 完全不送；兩種情況救不回 prompt 時都照既有收尾（PROMPT_TIMEOUT、session 轉 ATTACHED）。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pytest

from sw_core.config import (
    DEFAULT_TIMEOUT_RECOVERY_KEYS,
    ProfileTemplate,
    SessionProfile,
    UartProfile,
    load_profiles,
)
from sw_core.session_manager import SessionManager
import sw_core.session_manager as sm_mod
from sw_core.wal import WalWriter

pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]

ASSET_PROFILES = Path(__file__).resolve().parents[1] / "sw_core" / "assets" / "profiles"


def _load(td: str, body: str):
    (Path(td) / "p.yaml").write_text(body, encoding="utf-8")
    return load_profiles(td)


class TestConfigParsing(unittest.TestCase):
    def test_default_keeps_ctrl_c_then_ctrl_d(self) -> None:
        self.assertEqual(DEFAULT_TIMEOUT_RECOVERY_KEYS, ("CTRL_C", "CTRL_D"))
        self.assertEqual(ProfileTemplate(profile_name="x").timeout_recovery_keys, ("CTRL_C", "CTRL_D"))
        with tempfile.TemporaryDirectory() as td:
            result = _load(td, "profiles:\n  p:\n    platform: shell\ntargets: []\n")
        self.assertEqual(result.templates[0].timeout_recovery_keys, ("CTRL_C", "CTRL_D"))

    def test_shipped_templates_keep_default(self) -> None:
        """出貨 profile 不改預設：升級後行為不變。"""
        result = load_profiles(str(ASSET_PROFILES))
        self.assertTrue(result.templates)
        for tpl in result.templates:
            self.assertEqual(tpl.timeout_recovery_keys, ("CTRL_C", "CTRL_D"), tpl.profile_name)

    def test_template_values(self) -> None:
        for raw, expected in (
            ("[CTRL_C]", ("CTRL_C",)),
            ("[]", ()),
            ("[CTRL_C, CTRL_D]", ("CTRL_C", "CTRL_D")),
            ("null", ("CTRL_C", "CTRL_D")),
        ):
            with self.subTest(raw=raw), tempfile.TemporaryDirectory() as td:
                result = _load(td, f"profiles:\n  p:\n    timeout_recovery_keys: {raw}\ntargets: []\n")
                self.assertEqual(result.templates[0].timeout_recovery_keys, expected)

    def test_target_inherits_and_overrides(self) -> None:
        body = (
            "profiles:\n  p:\n    platform: shell\n    timeout_recovery_keys: [CTRL_C]\n"
            "targets:\n"
            "  - {act_no: 1, com: COM0, profile: p, device_by_id: /dev/serial/by-id/a}\n"
            "  - {act_no: 2, com: COM1, profile: p, device_by_id: /dev/serial/by-id/b,"
            " timeout_recovery_keys: []}\n"
            "  - {act_no: 3, com: COM2, profile: p, device_by_id: /dev/serial/by-id/c,"
            " timeout_recovery_keys: null}\n"
        )
        with tempfile.TemporaryDirectory() as td:
            result = _load(td, body)
        by_com = {p.com: p.timeout_recovery_keys for p in result.profiles}
        self.assertEqual(by_com, {"COM0": ("CTRL_C",), "COM1": (), "COM2": ("CTRL_C",)})

    def test_invalid_values_rejected(self) -> None:
        """拼錯或型別不對時拒絕載入，不悄悄退回預設（否則照送 CTRL_D）。"""
        for raw in ("CTRL_C", "[CTRL_Z]", "[ctrl_c]", "[CTRL_C, CTRL_C]", "[3]", "true", "{a: 1}"):
            with self.subTest(raw=raw), tempfile.TemporaryDirectory() as td:
                with self.assertRaises(ValueError):
                    _load(td, f"profiles:\n  p:\n    timeout_recovery_keys: {raw}\ntargets: []\n")

    def test_dynamic_and_rematerialized_sessions_carry_keys(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            first = ProfileTemplate(profile_name="first", timeout_recovery_keys=("CTRL_C",))
            second = ProfileTemplate(profile_name="second", timeout_recovery_keys=())
            manager = SessionManager(
                [], WalWriter(wal_dir=td), templates=[first, second],
                on_ready=lambda _sid: None, on_detached=lambda _sid: None,
            )
            with manager._lock:
                session = manager._session_from_template(first, "/dev/serial/by-id/a")
                self.assertEqual(session.profile.timeout_recovery_keys, ("CTRL_C",))
                self.assertTrue(manager._rematerialize_profile_locked(session, second, "detected"))
                self.assertEqual(session.profile.timeout_recovery_keys, ())


class TestRecoveryKeys(unittest.TestCase):
    """比照 tests/test_bad_command_recovery.py 的 MagicMock bridge。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        old = sm_mod.STATE_PATH
        sm_mod.STATE_PATH = str(Path(self._tmp.name) / "state.json")
        self.addCleanup(setattr, sm_mod, "STATE_PATH", old)

    def _ready(self, keys: tuple[str, ...] | None):
        extra = {} if keys is None else {"timeout_recovery_keys": keys}
        profile = SessionProfile(
            profile_name="p", com="COM0", act_no=1, alias="lab",
            device_by_id="/dev/serial/by-id/dev0", platform="shell",
            prompt_regex=r"[$#] $", uart=UartProfile(), **extra,
        )
        mgr = SessionManager(
            [profile], WalWriter(wal_dir=self._tmp.name),
            on_ready=lambda _: None, on_detached=lambda _: None,
        )
        session = mgr.get_session("COM0")
        assert session is not None
        bridge = mock.MagicMock()
        session.bridge = bridge
        session.state = "READY"
        return mgr, session, bridge

    @staticmethod
    def _sent_keys(bridge: mock.MagicMock) -> list[bytes]:
        return [c.args[0] for c in bridge.send_bytes.call_args_list if c.args[0] in (b"\x03", b"\x04")]

    def test_default_still_sends_ctrl_d_after_ctrl_c_fails(self) -> None:
        mgr, session, bridge = self._ready(None)
        bridge.rx_snapshot_len.side_effect = [10, 10, 10, 20, 30]
        bridge.wait_for_regex_from.side_effect = [False, False, True]
        bridge.rx_text_from.return_value = "x\n$ "
        resp = mgr.execute_command("p:COM0", "cat", "agent:t", "c1", timeout_s=0.1)
        self.assertEqual(resp["recovery_action"], "CTRL_D")
        self.assertEqual(self._sent_keys(bridge), [b"\x03", b"\x04"])

    def test_ctrl_c_only_never_sends_ctrl_d(self) -> None:
        mgr, session, bridge = self._ready(("CTRL_C",))
        bridge.rx_snapshot_len.side_effect = [10, 10, 10, 20]
        bridge.wait_for_regex_from.side_effect = [False, False]
        bridge.rx_text_from.return_value = "partial"
        resp = mgr.execute_command("p:COM0", "cat", "agent:t", "c2", timeout_s=0.1)
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error_code"], "PROMPT_TIMEOUT")
        self.assertEqual(resp["recovery_action"], "NONE")
        self.assertEqual(self._sent_keys(bridge), [b"\x03"])
        self.assertEqual(session.state, "ATTACHED")
        self.assertEqual(session.last_error, "PROMPT_TIMEOUT")

    def test_ctrl_c_only_still_recovers_with_ctrl_c(self) -> None:
        mgr, session, bridge = self._ready(("CTRL_C",))
        bridge.rx_snapshot_len.side_effect = [10, 10, 10, 20]
        bridge.wait_for_regex_from.side_effect = [False, True]
        bridge.rx_text_from.return_value = "partial\n$ "
        resp = mgr.execute_command("p:COM0", "cat", "agent:t", "c3", timeout_s=0.1)
        self.assertEqual(resp["error_code"], "PROMPT_TIMEOUT_RECOVERED")
        self.assertEqual(resp["recovery_action"], "CTRL_C")
        self.assertEqual(session.state, "READY")

    def test_empty_list_sends_no_keys(self) -> None:
        mgr, session, bridge = self._ready(())
        bridge.rx_snapshot_len.side_effect = [10, 10, 10]
        bridge.wait_for_regex_from.side_effect = [False]
        bridge.rx_text_from.return_value = "partial"
        resp = mgr.execute_command("p:COM0", "cat", "agent:t", "c4", timeout_s=0.1)
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error_code"], "PROMPT_TIMEOUT")
        self.assertEqual(resp["recovery_action"], "NONE")
        self.assertEqual(self._sent_keys(bridge), [])
        bridge.send_bytes.assert_not_called()
        self.assertEqual(session.state, "ATTACHED")


if __name__ == "__main__":
    unittest.main()
