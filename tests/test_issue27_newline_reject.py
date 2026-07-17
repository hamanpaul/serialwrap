"""Issue #27 — 命令換行符拒絕的單元測試；Issue #129 — 上限可查詢性。

驗證 CommandArbiter.submit() 在命令包含嵌入換行符時正確拒絕，
並確認 CMD_TOO_LONG 優先於 CMD_CONTAINS_NEWLINE。
另驗 health.status（daemon status）暴露的 limits 欄位與 arbiter 常數一致（#129），
讓 client 執行期查詢而非硬編碼上限。
"""
from __future__ import annotations

import unittest
from unittest import mock
from unittest.mock import MagicMock

from sw_core.arbiter import CMD_REJECT_BYTES, CMD_WARN_BYTES, CommandArbiter

try:
    import state_iso  # pytest／unittest discover：tests/ 在 sys.path
except ImportError:  # python3 -m unittest tests.test_x（repo root 跑法，#120）
    from tests import state_iso


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


class TestLimitsExposure(unittest.TestCase):
    """Issue #129 — health.status（daemon status）必須暴露可查詢的命令長度上限。

    limits 值直接引用 ``sw_core.arbiter`` 常數比對（單一事實來源），
    常數若調整，本測試不需改動即自動對齊。
    """

    def setUp(self) -> None:
        state_iso.isolate_testcase(self)  # #120 per-file 隔離（unittest 不載 conftest）

    def test_health_status_exposes_limits(self) -> None:
        """health.status 回應須含 limits 欄位，且值等於 arbiter 常數。"""
        from sw_core.service import SerialwrapService

        svc = SerialwrapService([])
        fake = {"multi_open": False, "daemons": [], "holders": {}, "holders_status": "ok"}
        with mock.patch("sw_core.service.detect_multi_open", return_value=fake):
            st = svc.rpc("health.status", {})
        self.assertTrue(st["ok"])
        self.assertEqual(
            st["limits"],
            {
                "max_submit_cmd_bytes": CMD_REJECT_BYTES,
                "warn_submit_cmd_bytes": CMD_WARN_BYTES,
                "reject_error_code": "CMD_TOO_LONG",
                "newline_forbidden": True,
            },
        )


if __name__ == "__main__":
    unittest.main()
