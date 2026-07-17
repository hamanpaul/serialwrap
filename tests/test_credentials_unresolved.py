"""tests/test_credentials_unresolved.py — 宣告帳密但解析空時的 CREDENTIALS_UNRESOLVED
終態行為（#140 Task 2）。

以 SessionManager + FakeBridge 的 unit 手法（比照 tests/test_readiness_reprobe.py）
確定性驗證，避免真實 PTY 競態；核心斷言為「不對 login prompt 送空帳密（零 probe TX）」。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sw_core import constants
from sw_core.config import SessionProfile, UartProfile
from sw_core.device_watcher import DeviceInfo
from sw_core.session_manager import SessionManager
import sw_core.session_manager as sm_mod
from sw_core.wal import WalWriter


class RecordingBridge:
    """記錄所有 probe/login TX 的假 bridge；任何 send_command/send_secret 都會被計數，
    用於證明宣告帳密但解析空時「零 probe TX」（不打斷 login prompt）。"""

    def __init__(self) -> None:
        self.sent_commands: list[str] = []
        self.sent_secrets: list[str] = []
        self.rx_tail_text = ""
        self.interactive_owner: str | None = None

    # --- probe/login 期間會被呼叫的 TX 介面（本測試核心：斷言未被呼叫）---
    def clear_rx_buffer(self) -> None:
        pass

    def send_command(self, cmd: str, source: str = "system") -> None:
        self.sent_commands.append(cmd)

    def send_secret(self, secret: str) -> None:
        self.sent_secrets.append(secret)

    def wait_for_regex(self, _pattern: str, _timeout_s: float) -> bool:
        return False

    def rx_tail(self) -> str:
        return self.rx_tail_text

    # --- to_public_dict 需要 ---
    def list_consoles(self) -> list[dict]:
        return []

    def console_endpoint(self) -> str | None:
        return None


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._old_state_path = sm_mod.STATE_PATH
        sm_mod.STATE_PATH = str(Path(self._tmp.name) / "state.json")

    def tearDown(self) -> None:
        sm_mod.STATE_PATH = self._old_state_path

    def _profile(
        self,
        *,
        login_regex: str = r"(?mi)^.*login:\s*$",
        user_env: str | None = "BRCM_USER",
        pass_env: str | None = "BRCM_PASS",
        env_file: str | None = None,
    ) -> SessionProfile:
        return SessionProfile(
            profile_name="brcm",
            com="COM0",
            act_no=1,
            alias="lab+1",
            device_by_id="/dev/serial/by-id/fake",
            platform="bcm",
            prompt_regex=r".*[#>] $",
            login_regex=login_regex,
            password_regex=r"(?mi)^password:\s*$",
            ready_probe="echo __READY__${nonce}",
            user_env=user_env,
            pass_env=pass_env,
            env_file=env_file,
            uart=UartProfile(),
        )

    def _manager(self, profile: SessionProfile) -> tuple[SessionManager, sm_mod.SessionRuntime]:
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
                profile.device_by_id: DeviceInfo(
                    by_id=profile.device_by_id,
                    real_path="/dev/ttyFAKE0",
                )
            }
        return mgr, session


class TestCredentialsUnresolvedGate(_Base):
    def test_declared_env_file_missing_marks_credentials_unresolved(self) -> None:
        """宣告 env_file（不存在）但缺帳密 → ATTACHED/last_error=CREDENTIALS_UNRESOLVED，
        且對 login prompt 零 probe TX（不進 login 迴圈）。"""
        missing = str(Path(self._tmp.name) / "nope.env")
        profile = self._profile(env_file=missing)
        mgr, session = self._manager(profile)
        bridge = RecordingBridge()
        session.state = "ATTACHED"
        session.bridge = bridge

        # 隔離 os.environ：resolve_session_auth 會 fallback 到 os.environ.get(user_env/pass_env)，
        # 若測機恰有 BRCM_USER/BRCM_PASS 會使帳密解析成功（reason=ok）→ gate 不 fire → 偽失敗。
        with mock.patch.dict(os.environ, {}, clear=True):
            result = mgr._probe_existing_bridge(session, bridge)

        self.assertTrue(result.get("ok"))
        self.assertEqual(session.state, "ATTACHED")
        self.assertEqual(session.last_error, "CREDENTIALS_UNRESOLVED")
        # 核心：完全沒有對 login prompt 送出任何字串（含空字串）。
        self.assertEqual(bridge.sent_commands, [])
        self.assertEqual(bridge.sent_secrets, [])

    def test_credentials_unresolved_is_terminal_no_reprobe(self) -> None:
        """CREDENTIALS_UNRESOLVED 為終態：自動 reprobe 不再排程。"""
        profile = self._profile(env_file=str(Path(self._tmp.name) / "nope.env"))
        mgr, session = self._manager(profile)
        session.state = "ATTACHED"
        session.bridge = RecordingBridge()
        session.last_error = "CREDENTIALS_UNRESOLVED"
        session.last_rx_mono = 0.0  # RX 已閒置

        with mgr._lock:
            prepared = mgr._prepare_reprobe_locked(session, now=1000.0)

        self.assertIsNone(prepared, "CREDENTIALS_UNRESOLVED 終態不得排 reprobe")

    def test_not_configured_still_probes(self) -> None:
        """未宣告帳密來源（not_configured）：行為不變，仍走 probe（送空 probe），
        last_error 非 CREDENTIALS_UNRESOLVED。"""
        profile = self._profile(user_env=None, pass_env=None, env_file=None)
        mgr, session = self._manager(profile)
        bridge = RecordingBridge()
        session.state = "ATTACHED"
        session.bridge = bridge

        result = mgr._probe_existing_bridge(session, bridge)

        self.assertTrue(result.get("ok"))
        self.assertNotEqual(session.last_error, "CREDENTIALS_UNRESOLVED")
        # not_configured 仍走既有 probe：至少送出一次 probe（空字串）。
        self.assertEqual(bridge.sent_commands, [""])

    def test_warning_emitted_once_with_reason_and_path_no_secret(self) -> None:
        """進入終態時輸出一次含 reason 與 env_file 路徑的 WAL 警告，且重入不重複。"""
        missing = str(Path(self._tmp.name) / "nope.env")
        profile = self._profile(env_file=missing)
        mgr, session = self._manager(profile)
        bridge = RecordingBridge()
        session.state = "ATTACHED"
        session.bridge = bridge

        # 同上：隔離 os.environ，避免測機殘留 BRCM_USER/BRCM_PASS 讓 gate 不 fire。
        with mock.patch.dict(os.environ, {}, clear=True):
            mgr._probe_existing_bridge(session, bridge)
            mgr._probe_existing_bridge(session, bridge)  # 重入不得再寫一次

        rows = mgr._wal.tail_raw(com="COM0", limit=200)
        events = [
            r for r in rows
            if isinstance(r.get("meta"), dict) and r["meta"].get("event") == "credentials_unresolved"
        ]
        self.assertEqual(len(events), 1, "警告事件應且僅一次")
        meta = events[0]["meta"]
        self.assertEqual(meta.get("reason"), "env_file_missing")
        self.assertTrue(str(meta.get("env_file", "")).endswith("nope.env"))
        # WAL payload 不得含帳密（本情境無帳密值，但仍確認 payload 為空）。
        self.assertEqual(events[0].get("len"), 0)


class TestCredentialsDeclaredHelper(_Base):
    def test_helper_matrix(self) -> None:
        profile = self._profile()
        mgr, _ = self._manager(profile)
        from sw_core.auth import AuthResolution

        declared = self._profile(env_file="/x")
        not_declared = self._profile(user_env=None, pass_env=None, env_file=None)

        # 宣告帳密 + reason 非 ok → True
        self.assertTrue(
            mgr._credentials_declared_but_unresolved(declared, AuthResolution(reason="env_file_missing"))
        )
        self.assertTrue(
            mgr._credentials_declared_but_unresolved(declared, AuthResolution(reason="key_absent"))
        )
        # 宣告帳密 + ok → False
        self.assertFalse(
            mgr._credentials_declared_but_unresolved(declared, AuthResolution(reason="ok"))
        )
        # 未宣告帳密（not_configured）→ False
        self.assertFalse(
            mgr._credentials_declared_but_unresolved(not_declared, AuthResolution(reason="not_configured"))
        )


if __name__ == "__main__":
    unittest.main()
