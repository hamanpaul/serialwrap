"""Issue #27 — 命令換行符拒絕的單元測試。

驗證 CommandArbiter.submit() 在命令包含嵌入換行符時正確拒絕，
並確認 CMD_TOO_LONG 優先於 CMD_CONTAINS_NEWLINE。
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from sw_core.arbiter import CMD_REJECT_BYTES, CommandArbiter


class TestNewlineReject(unittest.TestCase):
    """測試 CommandArbiter 對嵌入換行符命令的拒絕行為。"""

    def setUp(self) -> None:
        self.send_cb = MagicMock(return_value={"ok": True})
        self.arbiter = CommandArbiter(send_cb=self.send_cb)
        self.session_id = "test-session"
        self.arbiter.register_session(self.session_id)

    def tearDown(self) -> None:
        self.arbiter.unregister_session(self.session_id)

    def _submit(self, command: str) -> dict:
        return self.arbiter.submit(
            session_id=self.session_id,
            command=command,
            source="agent",
            mode="foreground",
            timeout_s=5.0,
        )

    def test_command_with_newline_rejected(self) -> None:
        """含嵌入 \\n 的命令應被拒絕，回傳 CMD_CONTAINS_NEWLINE。"""
        result = self._submit("echo hello\necho world")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "CMD_CONTAINS_NEWLINE")

    def test_command_without_newline_accepted(self) -> None:
        """不含換行符的正常命令不應被此規則攔截。"""
        result = self._submit("echo hello")
        self.assertTrue(result["ok"])

    def test_command_with_only_trailing_newline_rejected(self) -> None:
        """尾部 \\n 也應被拒絕。"""
        result = self._submit("echo hello\n")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "CMD_CONTAINS_NEWLINE")

    def test_command_too_long_takes_priority(self) -> None:
        """超長命令（>16KB）的 CMD_TOO_LONG 應優先於 CMD_CONTAINS_NEWLINE。"""
        long_cmd = "A" * (CMD_REJECT_BYTES + 1) + "\n"
        result = self._submit(long_cmd)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "CMD_TOO_LONG")


if __name__ == "__main__":
    unittest.main()
