"""Issue #28：background command 在 BackgroundCapture 建立前的 result_tail 回應。

submit() 回傳 cmd_id 後、worker 尚未執行完畢前，
``command.result_tail`` 應回傳 arbiter 狀態（accepted / running），
而非 CMD_NOT_FOUND。
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch


def _make_service() -> "SerialwrapService":
    """建立最小化 SerialwrapService，用 mock 替代所有 I/O 元件。"""
    from sw_core.config import SessionProfile

    profiles: list[SessionProfile] = []
    with (
        patch("sw_core.service.WalWriter"),
        patch("sw_core.service.SessionManager"),
        patch("sw_core.service.DeviceWatcher"),
    ):
        from sw_core.service import SerialwrapService

        svc = SerialwrapService(profiles)
    return svc


class TestBgResultTailFallback(unittest.TestCase):
    """測試 command.result_tail 在 BackgroundCapture 不存在時的 arbiter fallback。"""

    def setUp(self) -> None:
        self.svc = _make_service()

    # ------------------------------------------------------------------
    # 輔助
    # ------------------------------------------------------------------

    def _arbiter_rec(self, cmd_id: str, status: str, **overrides) -> dict:
        """產生 arbiter.get() 的模擬回傳。"""
        rec = {
            "cmd_id": cmd_id,
            "session_id": "COM0",
            "command": "sleep 999",
            "source": "agent:a1",
            "mode": "bg",
            "execution_mode": "background",
            "timeout_s": 30.0,
            "priority": 0,
            "status": status,
            "created_at": "2025-01-01T00:00:00Z",
            "accepted_at": "2025-01-01T00:00:00Z",
            "started_at": None,
            "done_at": None,
            "error_code": None,
            "stdout": "",
            "partial": False,
            "background_capture_id": None,
            "interactive_session_id": None,
            "recovery_action": None,
        }
        rec.update(overrides)
        return {"ok": True, "command": rec}

    def _call_result_tail(self, cmd_id: str, from_chunk: int = 0) -> dict:
        return self.svc.rpc("command.result_tail", {"cmd_id": cmd_id, "from_chunk": from_chunk})

    # ------------------------------------------------------------------
    # 測試
    # ------------------------------------------------------------------

    def test_result_tail_before_execution_returns_accepted(self) -> None:
        """BackgroundCapture 不存在、arbiter status=accepted → ok + 空 chunks。"""
        cmd_id = "abc123"
        self.svc._sessions.get_background_result = MagicMock(
            return_value={"ok": False, "error_code": "CMD_NOT_FOUND", "cmd_id": cmd_id},
        )
        self.svc._arbiter.get = MagicMock(
            return_value=self._arbiter_rec(cmd_id, "accepted"),
        )

        result = self._call_result_tail(cmd_id)

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "accepted")
        self.assertIsNone(result["error_code"])
        self.assertEqual(result["chunks"], [])
        self.assertEqual(result["from_chunk"], 0)
        self.assertEqual(result["next_chunk"], 0)

    def test_result_tail_during_execution_returns_running(self) -> None:
        """BackgroundCapture 不存在、arbiter status=running → ok + 空 chunks。"""
        cmd_id = "def456"
        self.svc._sessions.get_background_result = MagicMock(
            return_value={"ok": False, "error_code": "CMD_NOT_FOUND", "cmd_id": cmd_id},
        )
        self.svc._arbiter.get = MagicMock(
            return_value=self._arbiter_rec(cmd_id, "running"),
        )

        result = self._call_result_tail(cmd_id)

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "running")
        self.assertEqual(result["chunks"], [])

    def test_result_tail_after_completion_returns_chunks(self) -> None:
        """BackgroundCapture 存在時，直接回傳 capture 結果，不走 fallback。"""
        cmd_id = "ghi789"
        expected = {
            "ok": True,
            "cmd_id": cmd_id,
            "status": "done",
            "error_code": None,
            "from_seq": 100,
            "last_seq": 105,
            "from_chunk": 0,
            "next_chunk": 2,
            "chunks": ["line1\n", "line2\n"],
        }
        self.svc._sessions.get_background_result = MagicMock(return_value=expected)
        # arbiter.get 不應被呼叫
        self.svc._arbiter.get = MagicMock()

        result = self._call_result_tail(cmd_id)

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "done")
        self.assertEqual(result["chunks"], ["line1\n", "line2\n"])
        self.svc._arbiter.get.assert_not_called()

    def test_result_tail_unknown_cmd_still_not_found(self) -> None:
        """cmd_id 在 capture 和 arbiter 都不存在 → CMD_NOT_FOUND。"""
        cmd_id = "nonexistent"
        self.svc._sessions.get_background_result = MagicMock(
            return_value={"ok": False, "error_code": "CMD_NOT_FOUND", "cmd_id": cmd_id},
        )
        self.svc._arbiter.get = MagicMock(
            return_value={"ok": False, "error_code": "CMD_NOT_FOUND", "cmd_id": cmd_id},
        )

        result = self._call_result_tail(cmd_id)

        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "CMD_NOT_FOUND")

    def test_result_tail_error_without_capture(self) -> None:
        """arbiter status=error 且 BackgroundCapture 從未建立 → 回傳 error 狀態。"""
        cmd_id = "err001"
        self.svc._sessions.get_background_result = MagicMock(
            return_value={"ok": False, "error_code": "CMD_NOT_FOUND", "cmd_id": cmd_id},
        )
        self.svc._arbiter.get = MagicMock(
            return_value=self._arbiter_rec(cmd_id, "error", error_code="SEND_FAILED"),
        )

        result = self._call_result_tail(cmd_id)

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error_code"], "SEND_FAILED")
        self.assertEqual(result["chunks"], [])

    def test_result_tail_canceled_before_start(self) -> None:
        """arbiter status=canceled、started_at=None → 回傳 canceled（命令未開始執行）。"""
        cmd_id = "can001"
        self.svc._sessions.get_background_result = MagicMock(
            return_value={"ok": False, "error_code": "CMD_NOT_FOUND", "cmd_id": cmd_id},
        )
        self.svc._arbiter.get = MagicMock(
            return_value=self._arbiter_rec(cmd_id, "canceled"),
        )

        result = self._call_result_tail(cmd_id)

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "canceled")
        self.assertEqual(result["chunks"], [])

    def test_result_tail_canceled_after_start_not_synthesized(self) -> None:
        """arbiter status=canceled 但已開始執行 → CMD_NOT_FOUND（capture 可能稍後建立）。"""
        cmd_id = "can002"
        self.svc._sessions.get_background_result = MagicMock(
            return_value={"ok": False, "error_code": "CMD_NOT_FOUND", "cmd_id": cmd_id},
        )
        self.svc._arbiter.get = MagicMock(
            return_value=self._arbiter_rec(
                cmd_id, "canceled",
                started_at="2025-01-01T00:00:01Z",
            ),
        )

        result = self._call_result_tail(cmd_id)

        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "CMD_NOT_FOUND")

    def test_result_tail_done_without_capture_still_not_found(self) -> None:
        """arbiter status=done 但 BackgroundCapture 缺失 → CMD_NOT_FOUND（不遮蓋異常）。"""
        cmd_id = "done001"
        self.svc._sessions.get_background_result = MagicMock(
            return_value={"ok": False, "error_code": "CMD_NOT_FOUND", "cmd_id": cmd_id},
        )
        self.svc._arbiter.get = MagicMock(
            return_value=self._arbiter_rec(cmd_id, "done"),
        )

        result = self._call_result_tail(cmd_id)

        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "CMD_NOT_FOUND")

    def test_from_chunk_echoed_in_fallback(self) -> None:
        """fallback 回應的 from_chunk / next_chunk 應反映 caller 的 from_chunk。"""
        cmd_id = "pg001"
        self.svc._sessions.get_background_result = MagicMock(
            return_value={"ok": False, "error_code": "CMD_NOT_FOUND", "cmd_id": cmd_id},
        )
        self.svc._arbiter.get = MagicMock(
            return_value=self._arbiter_rec(cmd_id, "accepted"),
        )

        result = self._call_result_tail(cmd_id, from_chunk=5)

        self.assertTrue(result["ok"])
        self.assertEqual(result["from_chunk"], 5)
        self.assertEqual(result["next_chunk"], 5)
        self.assertEqual(result["chunks"], [])

    def test_fg_command_not_eligible_for_fallback(self) -> None:
        """非 background 的命令不走 fallback，仍回 CMD_NOT_FOUND。"""
        cmd_id = "fg001"
        self.svc._sessions.get_background_result = MagicMock(
            return_value={"ok": False, "error_code": "CMD_NOT_FOUND", "cmd_id": cmd_id},
        )
        fg_rec = self._arbiter_rec(cmd_id, "running")
        fg_rec["command"]["execution_mode"] = "line"
        self.svc._arbiter.get = MagicMock(return_value=fg_rec)

        result = self._call_result_tail(cmd_id)

        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "CMD_NOT_FOUND")


class TestLegacyResultTailFallback(unittest.TestCase):
    """確保 legacy result.tail（帶 cmd_id）也有相同 fallback。"""

    def setUp(self) -> None:
        self.svc = _make_service()

    def test_legacy_result_tail_accepted(self) -> None:
        cmd_id = "leg001"
        self.svc._sessions.get_background_result = MagicMock(
            return_value={"ok": False, "error_code": "CMD_NOT_FOUND", "cmd_id": cmd_id},
        )
        arb_rec = {
            "cmd_id": cmd_id, "session_id": "COM0", "command": "sleep 1",
            "source": "agent:a", "mode": "bg", "execution_mode": "background",
            "timeout_s": 30.0, "priority": 0, "status": "accepted",
            "created_at": "", "accepted_at": "", "started_at": None,
            "done_at": None, "error_code": None, "stdout": "",
            "partial": False, "background_capture_id": None,
            "interactive_session_id": None, "recovery_action": None,
        }
        self.svc._arbiter.get = MagicMock(
            return_value={"ok": True, "command": arb_rec},
        )

        result = self.svc.rpc("result.tail", {"cmd_id": cmd_id})

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(result["chunks"], [])


if __name__ == "__main__":
    unittest.main()
