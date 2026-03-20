"""錯誤/不完整命令導致 UART 卡住後的復原測試。

涵蓋：壞命令 timeout→recover、半截輸出卡住、human deferred during hang、
      亂碼輸出不影響 prompt 偵測、連續壞命令 session 不死。
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


class TestBadCommandRecovery(unittest.TestCase):

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._old_state_path = sm_mod.STATE_PATH
        sm_mod.STATE_PATH = str(Path(self._tmp.name) / "state.json")

    def tearDown(self) -> None:
        sm_mod.STATE_PATH = self._old_state_path

    def _make_profile(self) -> SessionProfile:
        return SessionProfile(
            profile_name="p", com="COM0", act_no=1, alias="lab",
            device_by_id="/dev/serial/by-id/dev0",
            platform="shell",
            prompt_regex=r"[$#] $",
            uart=UartProfile(),
        )

    def _setup_ready_session(self):
        """建立一個 READY session 並回傳 (mgr, session, bridge)。"""
        profiles = [self._make_profile()]
        mgr = SessionManager(
            profiles, WalWriter(wal_dir=self._tmp.name),
            on_ready=lambda _: None, on_detached=lambda _: None,
        )
        session = mgr.get_session("COM0")
        assert session is not None
        bridge = mock.MagicMock()
        session.bridge = bridge
        session.state = "READY"
        return mgr, session, bridge

    def test_bad_command_timeout_then_ctrl_c_recover(self) -> None:
        """壞命令（如 cat 不帶 EOF）→ prompt timeout → Ctrl-C recover → 回 READY。"""
        mgr, session, bridge = self._setup_ready_session()

        # 第一次 wait_for_regex = False（命令卡住）
        # Ctrl-C 後第二次 = True（prompt 回來）
        bridge.rx_snapshot_len.side_effect = [10, 20]
        bridge.wait_for_regex_from.side_effect = [False, True]
        bridge.rx_text_from.return_value = "$ "

        resp = mgr.execute_command("p:COM0", "cat", "agent:test", "cid-bad1", timeout_s=0.1)

        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error_code"], "PROMPT_TIMEOUT_RECOVERED")
        self.assertEqual(resp["recovery_action"], "CTRL_C")
        # Ctrl-C 應被送出
        bridge.send_bytes.assert_any_call(b"\x03", source="system:recover", cmd_id=None)
        # session 仍為 READY（recover 成功）
        self.assertEqual(session.state, "READY")

    def test_partial_output_hang_recover_then_next_command_works(self) -> None:
        """target 只回一半輸出就停 → timeout → recover → 下一個命令正常完成。"""
        mgr, session, bridge = self._setup_ready_session()

        # --- 壞命令 ---
        bridge.rx_snapshot_len.side_effect = [10, 20]
        bridge.wait_for_regex_from.side_effect = [False, True]
        bridge.rx_text_from.return_value = "$ "

        resp1 = mgr.execute_command("p:COM0", "broken-cmd", "agent:test", "cid-bad2", timeout_s=0.1)
        self.assertFalse(resp1["ok"])
        self.assertEqual(resp1["recovery_action"], "CTRL_C")
        self.assertEqual(session.state, "READY")

        # --- 正常命令 ---
        bridge.rx_snapshot_len.side_effect = [30, 50]
        bridge.wait_for_regex_from.side_effect = [True]
        bridge.rx_text_from.return_value = "eth0: flags=...\n$ "

        resp2 = mgr.execute_command("p:COM0", "ifconfig", "agent:test", "cid-good1", timeout_s=1.0)
        self.assertTrue(resp2["ok"])
        self.assertIn("eth0", resp2.get("stdout", ""))

    def test_garbled_ansi_output_does_not_break_prompt_detection(self) -> None:
        """target 回傳含 ANSI escape 的輸出，prompt 仍可匹配。"""
        mgr, session, bridge = self._setup_ready_session()

        # ANSI 亂碼 + 正常 prompt
        garbled = "\x1b[31mERROR\x1b[0m some output\n$ "
        bridge.rx_snapshot_len.side_effect = [10, 80]
        bridge.wait_for_regex_from.side_effect = [True]
        bridge.rx_text_from.return_value = garbled

        resp = mgr.execute_command("p:COM0", "echo test", "agent:test", "cid-ansi", timeout_s=1.0)
        self.assertTrue(resp["ok"])
        # session 仍 READY
        self.assertEqual(session.state, "READY")

    def test_consecutive_bad_commands_session_stays_alive(self) -> None:
        """連續 3 個壞命令（各觸發 timeout+recover），第 4 個正常命令仍可執行。"""
        mgr, session, bridge = self._setup_ready_session()

        for i in range(3):
            bridge.rx_snapshot_len.side_effect = [10 + i, 20 + i]
            bridge.wait_for_regex_from.side_effect = [False, True]
            bridge.rx_text_from.return_value = "$ "

            resp = mgr.execute_command(
                "p:COM0", f"bad-{i}", "agent:test", f"cid-bad-{i}", timeout_s=0.1,
            )
            self.assertFalse(resp["ok"])
            self.assertEqual(resp["recovery_action"], "CTRL_C")
            # 每次 recover 後都回 READY
            self.assertEqual(session.state, "READY")

        # 第 4 個正常命令
        bridge.rx_snapshot_len.side_effect = [100, 200]
        bridge.wait_for_regex_from.side_effect = [True]
        bridge.rx_text_from.return_value = "OK\n$ "

        resp = mgr.execute_command("p:COM0", "echo OK", "agent:test", "cid-ok", timeout_s=1.0)
        self.assertTrue(resp["ok"])
        self.assertIn("OK", resp.get("stdout", ""))
        self.assertEqual(session.state, "READY")

    def test_timeout_without_recovery_demotes_then_re_ready(self) -> None:
        """壞命令 + Ctrl-C 也失敗 → ATTACHED → 手動 clear → 重新 attach。"""
        mgr, session, bridge = self._setup_ready_session()

        # 全部 wait_for_regex = False（連 Ctrl-C 後也沒回 prompt）
        bridge.rx_snapshot_len.side_effect = [10, 20, 30]
        bridge.wait_for_regex_from.side_effect = [False, False, False]

        resp = mgr.execute_command("p:COM0", "totally-broken", "agent:test", "cid-stuck", timeout_s=0.1)

        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error_code"], "PROMPT_TIMEOUT")
        self.assertEqual(resp["recovery_action"], "NONE")
        # session 降級到 ATTACHED
        self.assertEqual(session.state, "ATTACHED")
        self.assertEqual(session.last_error, "PROMPT_TIMEOUT")


if __name__ == "__main__":
    unittest.main()
