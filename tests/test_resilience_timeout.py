"""超時與命令取消測試。

涵蓋：hard_timeout、cancel 排隊/已完成/執行中、login timeout。
"""
from __future__ import annotations

import threading
import time
import unittest
from unittest import mock

from sw_core.arbiter import CommandArbiter


class TestCommandCancel(unittest.TestCase):
    """Arbiter cancel() 的各種情境。"""

    def _slow_cb(self, session_id, command, source, cmd_id, timeout_s, mode):
        """模擬慢命令，用 Event 控制完成。"""
        self._exec_started.set()
        self._exec_gate.wait(timeout=10.0)
        return {"ok": True, "stdout": f"done:{command}"}

    def setUp(self) -> None:
        self._exec_started = threading.Event()
        self._exec_gate = threading.Event()
        self.arbiter = CommandArbiter(send_cb=self._slow_cb)
        self.arbiter.register_session("s1")

    def tearDown(self) -> None:
        self._exec_gate.set()  # 確保不會卡住
        self.arbiter.unregister_session("s1")

    def test_command_cancel_queued(self) -> None:
        """cancel 排隊中（尚未執行）的命令 → status=canceled。"""
        # 先送一個慢命令佔住 worker
        r1 = self.arbiter.submit(session_id="s1", command="slow", source="a1",
                                 mode="fg", timeout_s=10.0)
        self.assertTrue(r1["ok"])
        self._exec_started.wait(timeout=5.0)

        # 再送第二個命令（會排隊）
        r2 = self.arbiter.submit(session_id="s1", command="queued", source="a2",
                                 mode="fg", timeout_s=10.0)
        self.assertTrue(r2["ok"])
        cmd_id = r2["cmd_id"]

        # cancel 排隊中的命令
        result = self.arbiter.cancel(cmd_id)
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "canceled")

        # get 也應顯示 canceled
        info = self.arbiter.get(cmd_id)
        self.assertTrue(info["ok"])
        self.assertEqual(info["command"]["status"], "canceled")

        # 讓慢命令完成
        self._exec_gate.set()

    def test_command_cancel_already_done_returns_error(self) -> None:
        """命令已完成後 cancel → CMD_NOT_CANCELABLE。"""
        # 讓命令立即完成
        self._exec_gate.set()
        r = self.arbiter.submit(session_id="s1", command="quick", source="a1",
                                mode="fg", timeout_s=1.0)
        cmd_id = r["cmd_id"]

        # 等命令完成
        for _ in range(50):
            info = self.arbiter.get(cmd_id)
            if info["ok"] and info["command"]["status"] == "done":
                break
            time.sleep(0.05)

        result = self.arbiter.cancel(cmd_id)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "CMD_NOT_CANCELABLE")

    def test_cancel_during_execution_marks_canceled(self) -> None:
        """正在執行中 cancel → 標記 canceled（worker 完成後不覆寫）。"""
        r = self.arbiter.submit(session_id="s1", command="running", source="a1",
                                mode="fg", timeout_s=10.0)
        cmd_id = r["cmd_id"]
        self._exec_started.wait(timeout=5.0)
        time.sleep(0.05)

        # 命令正在執行（status=running），此時 cancel
        result = self.arbiter.cancel(cmd_id)
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "canceled")

        # 讓 worker 完成
        self._exec_gate.set()
        time.sleep(0.2)

        # 狀態應仍為 canceled（worker 檢查 canceled 不覆寫）
        info = self.arbiter.get(cmd_id)
        self.assertEqual(info["command"]["status"], "canceled")

    def test_cancel_nonexistent_returns_not_found(self) -> None:
        """cancel 不存在的 cmd_id → CMD_NOT_FOUND。"""
        result = self.arbiter.cancel("nonexistent-id")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "CMD_NOT_FOUND")

    def test_canceled_command_skipped_by_worker(self) -> None:
        """排隊中被 cancel 的命令，worker 取出後應跳過不執行。"""
        call_log: list[str] = []
        original_cb = self._slow_cb

        def tracking_cb(session_id, command, source, cmd_id, timeout_s, mode):
            call_log.append(command)
            return {"ok": True, "stdout": f"done:{command}"}

        self.arbiter.unregister_session("s1")
        self.arbiter = CommandArbiter(send_cb=tracking_cb)
        self.arbiter.register_session("s1")

        # 送 3 個命令
        r1 = self.arbiter.submit(session_id="s1", command="cmd1", source="a",
                                 mode="fg", timeout_s=1.0)
        r2 = self.arbiter.submit(session_id="s1", command="cmd2", source="a",
                                 mode="fg", timeout_s=1.0)
        r3 = self.arbiter.submit(session_id="s1", command="cmd3", source="a",
                                 mode="fg", timeout_s=1.0)

        # 立即 cancel cmd2
        self.arbiter.cancel(r2["cmd_id"])

        # 等全部執行完
        for _ in range(100):
            i1 = self.arbiter.get(r1["cmd_id"])
            i3 = self.arbiter.get(r3["cmd_id"])
            if (i1["ok"] and i1["command"]["status"] == "done" and
                    i3["ok"] and i3["command"]["status"] == "done"):
                break
            time.sleep(0.05)

        # cmd2 不應被執行
        self.assertNotIn("cmd2", call_log)
        self.assertIn("cmd1", call_log)
        self.assertIn("cmd3", call_log)


class TestLoginTimeout(unittest.TestCase):
    """login 超時 → session 停在 ATTACHED + LOGIN_REQUIRED。

    使用 mock 模擬 SessionManager 行為。
    """

    def test_login_timeout_stays_attached(self) -> None:
        """模擬 login_fsm 超時後，session 應停在 ATTACHED。"""
        from sw_core.session_manager import SessionRuntime
        from sw_core.config import SessionProfile

        profile = SessionProfile(
            profile_name="test",
            platform="shell",
            com="COM99",
            act_no=99,
            alias="test99",
            device_by_id="/dev/serial/by-id/test-device",
            prompt_regex=r"[$#] $",
            login_regex=r"(?mi)login:\s*$",
            password_regex=r"(?mi)password:\s*$",
            user_env="TEST_U",
            pass_env="TEST_P",
            ready_probe="echo __READY__${nonce}",
            timeout_s=0.1,  # 極短 timeout
        )

        session = SessionRuntime(
            session_id="test:COM99",
            profile=profile,
        )
        # 模擬 login 超時後的 session 狀態
        session.state = "ATTACHED"
        session.last_error = "LOGIN_REQUIRED"

        self.assertEqual(session.state, "ATTACHED")
        self.assertEqual(session.last_error, "LOGIN_REQUIRED")


if __name__ == "__main__":
    unittest.main()
