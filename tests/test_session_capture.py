"""Agent 觸發式 per-session 日誌 capture 測試"""
from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock

from sw_core.config import SessionProfile, UartProfile
from sw_core.session_manager import (
    SessionCapture,
    SessionManager,
    SessionRuntime,
)
from sw_core.wal import WalWriter


def _make_profile(com: str = "COM0", *, log_dir: str | None = None) -> SessionProfile:
    return SessionProfile(
        profile_name="test-tpl",
        com=com,
        act_no=1,
        alias="test",
        device_by_id="/dev/null",
        platform="shell",
        log_dir=log_dir,
    )


class TestSessionCaptureDataclass(unittest.TestCase):
    def test_defaults(self) -> None:
        cap = SessionCapture(
            capture_id="c1",
            session_id="s1",
            log_path="/tmp/test.log",
            started_at="2025-01-01T00:00:00Z",
        )
        self.assertEqual(cap.status, "active")
        self.assertEqual(cap.line_count, 0)
        self.assertEqual(cap.byte_count, 0)


class TestLogStartStop(unittest.TestCase):
    """使用真實 SessionManager（搭配 mock bridge）測試 log_start / log_stop"""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._log_dir = os.path.join(self._tmpdir, "logs")
        self._wal_dir = os.path.join(self._tmpdir, "wal")
        os.makedirs(self._wal_dir, exist_ok=True)

        profile = _make_profile("COM0", log_dir=self._log_dir)
        wal = WalWriter(wal_dir=self._wal_dir)
        self._mgr = SessionManager(
            [profile],
            wal,
            on_ready=lambda sid: None,
            on_detached=lambda sid: None,
        )
        self._session_id = list(self._mgr._sessions.keys())[0]

    def tearDown(self) -> None:
        # 確保所有 capture file handle 被關閉
        for session in self._mgr._sessions.values():
            if session.active_capture:
                self._mgr.log_stop(session.profile.com)

    def test_log_start_creates_file(self) -> None:
        result = self._mgr.log_start("COM0")
        self.assertTrue(result["ok"])
        self.assertIn("capture_id", result)
        self.assertTrue(os.path.exists(result["log_path"]))
        self.assertTrue(result["log_path"].startswith(self._log_dir))
        self.assertIn("COM0_", os.path.basename(result["log_path"]))

    def test_log_start_already_active(self) -> None:
        r1 = self._mgr.log_start("COM0")
        self.assertTrue(r1["ok"])
        r2 = self._mgr.log_start("COM0")
        self.assertTrue(r2["ok"])
        self.assertTrue(r2.get("already_active"))
        self.assertEqual(r1["capture_id"], r2["capture_id"])

    def test_log_stop(self) -> None:
        self._mgr.log_start("COM0")
        result = self._mgr.log_stop("COM0")
        self.assertTrue(result["ok"])
        self.assertIn("line_count", result)
        self.assertIn("byte_count", result)
        # 再 stop 應回傳無 active capture
        result2 = self._mgr.log_stop("COM0")
        self.assertFalse(result2["ok"])
        self.assertEqual(result2["error_code"], "NO_ACTIVE_CAPTURE")

    def test_log_status_no_capture(self) -> None:
        result = self._mgr.log_status("COM0")
        self.assertTrue(result["ok"])
        self.assertFalse(result["active"])

    def test_log_status_active(self) -> None:
        self._mgr.log_start("COM0")
        result = self._mgr.log_status("COM0")
        self.assertTrue(result["ok"])
        self.assertTrue(result["active"])
        self.assertIn("capture_id", result)

    def test_rx_writes_to_capture(self) -> None:
        """模擬 RX 資料寫入 capture 檔案"""
        self._mgr.log_start("COM0")
        session = self._mgr._sessions[self._session_id]
        cap = session.active_capture
        self.assertIsNotNone(cap)

        # 直接呼叫 _on_bridge_rx 模擬 UART 接收
        self._mgr._on_bridge_rx(self._session_id, b"hello world\n")
        self._mgr._on_bridge_rx(self._session_id, b"second line\n")

        self.assertEqual(cap.line_count, 2)
        self.assertGreater(cap.byte_count, 0)

        # 讀回檔案內容驗證
        with open(cap.log_path, "r", encoding="utf-8") as fp:
            content = fp.read()
        self.assertIn("hello world", content)
        self.assertIn("second line", content)

    def test_detach_stops_capture(self) -> None:
        """session detach 時自動停止 capture"""
        self._mgr.log_start("COM0")
        session = self._mgr._sessions[self._session_id]
        self.assertIsNotNone(session.active_capture)

        with self._mgr._lock:
            self._mgr._detach_session_locked(session, reason="TEST")

        self.assertIsNone(session.active_capture)

    def test_session_not_found(self) -> None:
        result = self._mgr.log_start("NONEXISTENT")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "SESSION_NOT_FOUND")

    def test_to_public_dict_includes_capture(self) -> None:
        self._mgr.log_start("COM0")
        session = self._mgr._sessions[self._session_id]
        d = session.to_public_dict()
        self.assertIsNotNone(d["capture"])
        self.assertIn("capture_id", d["capture"])
        self.assertEqual(d["capture"]["status"], "active")


class TestLogDirFallback(unittest.TestCase):
    """log_dir 解析優先序測試"""

    def test_profile_log_dir(self) -> None:
        p = _make_profile(log_dir="/custom/path")
        self.assertEqual(p.log_dir, "/custom/path")

    def test_profile_log_dir_none_fallback(self) -> None:
        p = _make_profile(log_dir=None)
        self.assertIsNone(p.log_dir)


class TestConfigLogDir(unittest.TestCase):
    """YAML config 中 defaults.log_dir 與 profile/target 覆寫測試"""

    def test_defaults_log_dir_from_yaml(self) -> None:
        import tempfile
        import yaml

        tmpdir = tempfile.mkdtemp()
        yaml_content = {
            "defaults": {"log_dir": "/my/log/dir"},
            "profiles": {
                "test-tpl": {
                    "platform": "shell",
                    "prompt_regex": ".*",
                }
            },
            "targets": [
                {
                    "act_no": 1,
                    "com": "COM0",
                    "alias": "t",
                    "profile": "test-tpl",
                    "device_by_id": "/dev/null",
                }
            ],
        }
        path = os.path.join(tmpdir, "test.yaml")
        with open(path, "w") as fp:
            yaml.dump(yaml_content, fp)

        from sw_core.config import load_profiles

        profiles = load_profiles(tmpdir)
        self.assertEqual(len(profiles), 1)
        self.assertEqual(profiles[0].log_dir, "/my/log/dir")

    def test_target_overrides_defaults(self) -> None:
        import tempfile
        import yaml

        tmpdir = tempfile.mkdtemp()
        yaml_content = {
            "defaults": {"log_dir": "/default/dir"},
            "profiles": {
                "test-tpl": {
                    "platform": "shell",
                    "prompt_regex": ".*",
                }
            },
            "targets": [
                {
                    "act_no": 1,
                    "com": "COM0",
                    "alias": "t",
                    "profile": "test-tpl",
                    "device_by_id": "/dev/null",
                    "log_dir": "/target/dir",
                }
            ],
        }
        path = os.path.join(tmpdir, "test.yaml")
        with open(path, "w") as fp:
            yaml.dump(yaml_content, fp)

        from sw_core.config import load_profiles

        profiles = load_profiles(tmpdir)
        self.assertEqual(profiles[0].log_dir, "/target/dir")

    def test_profile_template_overrides_defaults(self) -> None:
        import tempfile
        import yaml

        tmpdir = tempfile.mkdtemp()
        yaml_content = {
            "defaults": {"log_dir": "/default/dir"},
            "profiles": {
                "test-tpl": {
                    "platform": "shell",
                    "prompt_regex": ".*",
                    "log_dir": "/profile/dir",
                }
            },
            "targets": [
                {
                    "act_no": 1,
                    "com": "COM0",
                    "alias": "t",
                    "profile": "test-tpl",
                    "device_by_id": "/dev/null",
                }
            ],
        }
        path = os.path.join(tmpdir, "test.yaml")
        with open(path, "w") as fp:
            yaml.dump(yaml_content, fp)

        from sw_core.config import load_profiles

        profiles = load_profiles(tmpdir)
        self.assertEqual(profiles[0].log_dir, "/profile/dir")


if __name__ == "__main__":
    unittest.main()
