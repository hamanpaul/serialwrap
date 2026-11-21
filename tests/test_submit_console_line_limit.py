"""command.submit 依 profile 的 max_console_line_chars 拒絕過長命令。

profile 有量測過的 console 單行上限時，超過的命令送出後會被 target 截斷：命令走樣、
等不到 prompt，逾時再送 CTRL_C／CTRL_D。改成提交時就拒絕、零 TX。未設上限時行為不變。
錯誤碼沿用 CMD_TOO_LONG（Stage 1B 凍結錯誤碼集合），limit 帶 profile 的值。
計法與 file transfer 的單行預算相同：完整命令的 UTF-8 bytes，不含結尾換行。
"""
from __future__ import annotations

import time
import unittest
from unittest import mock

import pytest

from sw_core.arbiter import CMD_REJECT_BYTES, ERROR_CMD_TOO_LONG, CommandArbiter
from sw_core.config import SessionProfile, UartProfile
from sw_core.service import SerialwrapService

pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]


class TestArbiterConsoleLineLimit(unittest.TestCase):
    def setUp(self) -> None:
        self.callback = mock.Mock(return_value={"ok": True})
        self.arbiter = CommandArbiter(self.callback)
        self.arbiter.register_session("s")
        self.addCleanup(self.arbiter.unregister_session, "s")

    def _submit(self, command: str, limit: int | None, mode: str = "line") -> dict:
        return self.arbiter.submit(
            session_id="s", command=command, source="agent:t", mode=mode,
            timeout_s=1.0, max_console_line_chars=limit,
        )

    def test_over_limit_rejected_without_enqueue(self) -> None:
        result = self._submit("echo " + "A" * 20, 20)
        self.assertEqual(result["error_code"], ERROR_CMD_TOO_LONG)
        self.assertEqual(result["error_code"], "CMD_TOO_LONG")
        self.assertFalse(result["ok"])
        self.assertEqual(result["cmd_length"], 25)
        self.assertEqual(result["limit"], 20)
        self.assertIn("max_console_line_chars", result["hint"])
        self.assertEqual(self.arbiter.snapshot(), [])
        self.callback.assert_not_called()

    def test_at_limit_accepted(self) -> None:
        result = self._submit("A" * 20, 20)
        self.assertTrue(result["ok"], result)

    def test_counts_utf8_bytes(self) -> None:
        """與 file transfer 的 _line_budget_exceeded 相同，以 UTF-8 bytes 計（「中」為 3 bytes）。"""
        result = self._submit("中" * 7, 20)
        self.assertEqual(result["error_code"], ERROR_CMD_TOO_LONG)
        self.assertEqual(result["cmd_length"], 21)

    def test_no_limit_keeps_old_behavior(self) -> None:
        result = self._submit("A" * 5000, None)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["warning"]["code"], "CMD_LENGTH_WARNING")

    def test_applies_to_every_submit_mode(self) -> None:
        for mode in ("line", "background", "fg", "bg", "interactive"):
            with self.subTest(mode=mode):
                result = self._submit("A" * 21, 20, mode=mode)
                self.assertEqual(result["error_code"], ERROR_CMD_TOO_LONG)
                self.assertEqual(result["limit"], 20)

    def test_order_matches_contract(self) -> None:
        """16 KB 硬上限先判（limit=16384）；profile 上限與 CMD_TOO_LONG 同位、先於換行檢查
        （contracts/rpc_schema.json command.submit 的 error_order：CMD_TOO_LONG → CMD_CONTAINS_NEWLINE）。"""
        result = self._submit("A" * (CMD_REJECT_BYTES + 1), 20)
        self.assertEqual(result["error_code"], "CMD_TOO_LONG")
        self.assertEqual(result["limit"], CMD_REJECT_BYTES)
        result = self._submit("A" * 30 + "\n", 20)
        self.assertEqual(result["error_code"], "CMD_TOO_LONG")
        self.assertEqual(result["limit"], 20)
        result = self._submit("A" * 5 + "\n", 20)
        self.assertEqual(result["error_code"], "CMD_CONTAINS_NEWLINE")


def _profile(limit: int | None) -> SessionProfile:
    return SessionProfile(
        profile_name="p", com="COM0", act_no=1, alias="lab",
        device_by_id="/dev/serial/by-id/dev0", platform="shell",
        prompt_regex=r"[$#] $", uart=UartProfile(), max_console_line_chars=limit,
    )


def _wait_done(service: SerialwrapService, cmd_id: str) -> None:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if service.rpc("command.get", {"cmd_id": cmd_id})["command"]["done_at"]:
            return
        time.sleep(0.01)
    raise AssertionError("command did not finish")


class TestServiceUsesProfileLimit(unittest.TestCase):
    """RPC 入口從 session 的 profile 取上限，傳給 arbiter；被拒時不呼叫 execute_command。"""

    def _service(self, limit: int | None) -> tuple[SerialwrapService, str]:
        service = SerialwrapService([_profile(limit)])
        session = service._sessions.get_session("COM0")
        assert session is not None
        service._arbiter.register_session(session.session_id)
        self.addCleanup(service._arbiter.unregister_session, session.session_id)
        return service, session.session_id

    def test_profile_limit_rejects_with_zero_tx(self) -> None:
        service, session_id = self._service(20)
        with mock.patch.object(service, "_resolve_session_id", return_value=(session_id, None)), \
             mock.patch.object(service._sessions, "execute_command") as execute:
            result = service.rpc("command.submit", {"selector": "COM0", "cmd": "A" * 21})
        self.assertEqual(result["error_code"], "CMD_TOO_LONG")
        self.assertEqual(result["limit"], 20)
        execute.assert_not_called()

    def test_within_profile_limit_runs(self) -> None:
        service, session_id = self._service(20)
        with mock.patch.object(service, "_resolve_session_id", return_value=(session_id, None)), \
             mock.patch.object(service._sessions, "execute_command",
                               return_value={"ok": True, "stdout": ""}) as execute:
            result = service.rpc("command.submit", {"selector": "COM0", "cmd": "A" * 20})
            self.assertTrue(result["ok"], result)
            _wait_done(service, result["cmd_id"])
        execute.assert_called_once()

    def test_profile_without_limit_unchanged(self) -> None:
        service, session_id = self._service(None)
        with mock.patch.object(service, "_resolve_session_id", return_value=(session_id, None)), \
             mock.patch.object(service._sessions, "execute_command",
                               return_value={"ok": True, "stdout": ""}):
            result = service.rpc("command.submit", {"selector": "COM0", "cmd": "A" * 600})
            self.assertTrue(result["ok"], result)
            _wait_done(service, result["cmd_id"])


if __name__ == "__main__":
    unittest.main()
