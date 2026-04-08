"""Issue #26 測試：recover ok 語義修正（成功恢復 prompt 後回 ok:True）。"""
from __future__ import annotations

import threading
import unittest
from unittest.mock import MagicMock

from sw_core.session_manager import SessionManager


class TestRecoverOkTrue(unittest.TestCase):
    """驗證 _recover_after_failure 成功恢復 prompt 後回 ok: True。"""

    def test_recover_ok_true_on_success(self) -> None:
        """CTRL_C 成功恢復 prompt 後，回應應包含 ok: True。"""
        mgr = MagicMock(spec=SessionManager)
        mgr._lock = threading.RLock()
        mgr._extract_command_stdout = MagicMock(return_value="partial output")
        mgr._set_terminal_capture_locked = MagicMock()

        bridge = MagicMock()
        bridge.rx_snapshot_len.return_value = 0
        bridge.wait_for_regex_from.return_value = True
        bridge.rx_text_from.return_value = "partial output"

        session = MagicMock()
        session.profile = MagicMock()
        session.profile.prompt_regex = r"\$ $"
        session.interactive_session_id = None
        session.bridge = bridge

        result = SessionManager._recover_after_failure(
            mgr,
            session,
            bridge,
            cmd_id="cmd-999",
            timeout_s=5.0,
            source="agent:test",
            command="bad_cmd",
            prompt_regex=r"\$ $",
            pre_offset=0,
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["error_code"], "PROMPT_TIMEOUT_RECOVERED")
        self.assertTrue(result["partial"])
        self.assertIn(result["recovery_action"], ("CTRL_C", "CTRL_D"))


if __name__ == "__main__":
    unittest.main()
