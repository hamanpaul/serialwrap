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
        # #156：_on_command_flush 為 instance-only 屬性（僅在 __init__ 賦值，非
        # class 層方法），MagicMock(spec=SessionManager) 對未顯式 setattr 的
        # instance-only 屬性存取會拋 AttributeError，須比照上面兩行顯式補上。
        mgr._on_command_flush = MagicMock()

        bridge = MagicMock()
        bridge.rx_snapshot_len.return_value = 0
        bridge.wait_for_regex_from.return_value = True
        bridge.rx_text_from.return_value = "partial output"

        session = MagicMock()
        session.profile = MagicMock()
        session.profile.prompt_regex = r"\$ $"
        session.interactive_session_id = None
        session.bridge = bridge
        # #130（review 收斂）：_recover_after_failure 在送 CTRL_C/CTRL_D 前會先檢查
        # session.boot_quiet_active()；MagicMock 預設呼叫回傳值恆為 truthy，會被誤判
        # 成「quiet window 進行中」而整段跳過，與本測試「驗證 CTRL_C 成功恢復」的
        # 意圖無關，須顯式釘死為 False（非 quiet 中）才能重現原本要測的路徑。
        session.boot_quiet_active.return_value = False

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
        # #156：CTRL_C/CTRL_D 攔截成功時須顯式呼叫 flush callback，
        # 補上 detach 路徑天生跳過的 FLUSHED_BY_RECOVERY 終態語意。
        mgr._on_command_flush.assert_called_once_with(session.session_id, "FLUSHED_BY_RECOVERY")


if __name__ == "__main__":
    unittest.main()
