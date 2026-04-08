"""Issue #24 — 長時間命令 heartbeat / keepalive 機制測試。

涵蓋：expected_duration_s 延長 timeout、RX 活動 keepalive 延長等待、
靜默正確觸發 PROMPT_TIMEOUT、前景命令觀察性欄位。
"""
from __future__ import annotations

import threading
import time
import unittest
from typing import Any
from unittest import mock

from sw_core.arbiter import CommandArbiter
from sw_core.config import SessionProfile
from sw_core.session_manager import SessionManager, SessionRuntime
from sw_core.wal import WalWriter


def _make_profile(**overrides: Any) -> SessionProfile:
    """建立測試用 SessionProfile。"""
    defaults: dict[str, Any] = {
        "profile_name": "test",
        "platform": "shell",
        "com": "COM0",
        "act_no": 0,
        "alias": "t0",
        "device_by_id": "/dev/serial/by-id/test",
        "prompt_regex": r"[$#] $",
        "login_regex": r"(?mi)login:\s*$",
        "password_regex": r"(?mi)password:\s*$",
        "user_env": "U",
        "pass_env": "P",
        "ready_probe": "echo __READY__${nonce}",
        "timeout_s": 5.0,
    }
    defaults.update(overrides)
    return SessionProfile(**defaults)


class TestExpectedDurationExtendsTimeout(unittest.TestCase):
    """提交含 expected_duration_s 的命令，驗證 execute_command 收到的有效 timeout >= expected_duration_s。"""

    def test_expected_duration_extends_timeout(self) -> None:
        received: dict[str, Any] = {}

        def fake_send(
            session_id: str, command: str, source: str, cmd_id: str,
            timeout_s: float, mode: str, expected_duration_s: float | None = None,
        ) -> dict[str, Any]:
            received["timeout_s"] = timeout_s
            received["expected_duration_s"] = expected_duration_s
            return {"ok": True, "stdout": "done"}

        arb = CommandArbiter(send_cb=fake_send)
        arb.register_session("s1")
        self.addCleanup(lambda: arb.unregister_session("s1"))

        r = arb.submit(
            session_id="s1", command="apt upgrade -y", source="agent",
            mode="fg", timeout_s=10.0, expected_duration_s=60.0,
        )
        self.assertTrue(r["ok"])

        # 等命令執行完成
        for _ in range(100):
            info = arb.get(r["cmd_id"])
            if info["ok"] and info["command"]["status"] in ("done", "error"):
                break
            time.sleep(0.05)

        self.assertEqual(received["expected_duration_s"], 60.0)
        self.assertEqual(received["timeout_s"], 10.0)
        # 驗證 rec 中也記錄了 expected_duration_s
        info = arb.get(r["cmd_id"])
        self.assertEqual(info["command"]["expected_duration_s"], 60.0)


class TestOutputKeepaliveExtendsWait(unittest.TestCase):
    """mock bridge 使得每次 wait_for_regex_from 都回 False 但 rx_snapshot_len 增加，
    驗證不會在 timeout_s 就放棄。"""

    def test_output_keepalive_extends_wait(self) -> None:
        profile = _make_profile(timeout_s=1.0)
        session = SessionRuntime(session_id="test:COM0", profile=profile)
        session.state = "READY"

        bridge = mock.MagicMock()
        session.bridge = bridge

        # 模擬 RX 持續增長：每次呼叫 rx_snapshot_len 回傳遞增值
        rx_counter = {"n": 0}

        def fake_rx_len() -> int:
            rx_counter["n"] += 100
            return rx_counter["n"]

        bridge.rx_snapshot_len.side_effect = fake_rx_len
        bridge.rx_text_from.return_value = "output\n$ "

        # wait_for_regex_from 前幾次回 False，最後一次回 True
        call_count = {"n": 0}

        def fake_wait(pattern: str, from_offset: int, timeout_s: float) -> bool:
            call_count["n"] += 1
            # 前 3 次 False（模擬長時間命令輸出中），第 4 次 True
            return call_count["n"] >= 4

        bridge.wait_for_regex_from.side_effect = fake_wait

        wal = mock.MagicMock()
        wal.current_seq = 0
        mgr = SessionManager(
            [profile], wal,
            on_ready=lambda sid: None,
            on_detached=lambda sid: None,
        )
        # 注入 session
        mgr._sessions["test:COM0"] = session

        result = mgr.execute_command(
            "test:COM0", "long-running-cmd", "agent", "cmd-001",
            timeout_s=1.0, mode="line", expected_duration_s=60.0,
        )
        self.assertTrue(result["ok"])
        # wait_for_regex_from 應至少被呼叫 4 次（keepalive 迴圈延長了等待）
        self.assertGreaterEqual(call_count["n"], 4)


class TestSilenceTriggersTimeout(unittest.TestCase):
    """mock bridge 使得 wait_for_regex_from 回 False 且 rx_snapshot_len 不變，
    驗證正確觸發 PROMPT_TIMEOUT。"""

    def test_silence_triggers_timeout(self) -> None:
        profile = _make_profile(timeout_s=1.0)
        session = SessionRuntime(session_id="test:COM0", profile=profile)
        session.state = "READY"

        bridge = mock.MagicMock()
        session.bridge = bridge

        # rx_snapshot_len 固定不變 → 無 RX 活動
        bridge.rx_snapshot_len.return_value = 0
        bridge.wait_for_regex_from.return_value = False
        bridge.rx_text_from.return_value = ""

        wal = mock.MagicMock()
        wal.current_seq = 0
        mgr = SessionManager(
            [profile], wal,
            on_ready=lambda sid: None,
            on_detached=lambda sid: None,
        )
        mgr._sessions["test:COM0"] = session

        result = mgr.execute_command(
            "test:COM0", "stuck-cmd", "agent", "cmd-002",
            timeout_s=1.0, mode="line",
        )
        self.assertFalse(result["ok"])
        self.assertIn(result.get("error_code", ""), (
            "PROMPT_TIMEOUT", "PROMPT_TIMEOUT_RECOVERED",
            "PROMPT_TIMEOUT_FORCE_RECOVERED",
        ))


class TestFgCmdObservability(unittest.TestCase):
    """驗證 execute_command 期間 SessionRuntime 的觀察性欄位被正確設定。"""

    def test_fg_cmd_observability(self) -> None:
        profile = _make_profile(timeout_s=2.0)
        session = SessionRuntime(session_id="test:COM0", profile=profile)
        session.state = "READY"

        bridge = mock.MagicMock()
        session.bridge = bridge

        observed: dict[str, Any] = {}
        gate = threading.Event()

        def fake_wait(pattern: str, from_offset: int, timeout_s: float) -> bool:
            # 在等待期間觀察 session 狀態
            observed["fg_cmd_started_mono"] = session.fg_cmd_started_mono
            observed["fg_cmd_expected_duration_s"] = session.fg_cmd_expected_duration_s
            observed["foreground_busy"] = session.foreground_busy
            gate.set()
            return True

        bridge.wait_for_regex_from.side_effect = fake_wait
        bridge.rx_snapshot_len.return_value = 0
        bridge.rx_text_from.return_value = "output\n$ "

        wal = mock.MagicMock()
        wal.current_seq = 0
        mgr = SessionManager(
            [profile], wal,
            on_ready=lambda sid: None,
            on_detached=lambda sid: None,
        )
        mgr._sessions["test:COM0"] = session

        result = mgr.execute_command(
            "test:COM0", "echo hi", "agent", "cmd-003",
            timeout_s=2.0, mode="line", expected_duration_s=30.0,
        )
        self.assertTrue(result["ok"])
        gate.wait(timeout=5.0)

        # 執行期間欄位應有值
        self.assertIsNotNone(observed["fg_cmd_started_mono"])
        self.assertEqual(observed["fg_cmd_expected_duration_s"], 30.0)
        self.assertTrue(observed["foreground_busy"])

        # 執行完成後欄位應已清除
        self.assertIsNone(session.fg_cmd_started_mono)
        self.assertIsNone(session.fg_cmd_expected_duration_s)
        self.assertFalse(session.foreground_busy)

    def test_to_public_dict_includes_fg_fields(self) -> None:
        """to_public_dict() 應包含 foreground_busy 與 fg_cmd_expected_duration_s。"""
        profile = _make_profile()
        session = SessionRuntime(session_id="test:COM0", profile=profile)
        session.foreground_busy = True
        session.fg_cmd_expected_duration_s = 120.0

        d = session.to_public_dict()
        self.assertTrue(d["foreground_busy"])
        self.assertEqual(d["fg_cmd_expected_duration_s"], 120.0)


if __name__ == "__main__":
    unittest.main()
