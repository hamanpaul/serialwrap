"""Agent 觸發式 per-session 日誌 capture 測試"""
from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from sw_core.config import SessionProfile, UartProfile
from sw_core.session_manager import (
    SessionCapture,
    SessionManager,
    SessionRuntime,
)
from sw_core.wal import WalWriter


def _make_profile(com: str = "COM0", *, log_dir: str | None = None,
                  alias: str = "test") -> SessionProfile:
    return SessionProfile(
        profile_name="test-tpl",
        com=com,
        act_no=1,
        alias=alias,
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


# ── 新增測試：錯誤路徑 ──────────────────────────────────────


class TestLogStartErrorPaths(unittest.TestCase):
    """log_start 的各種失敗路徑"""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._wal_dir = os.path.join(self._tmpdir, "wal")
        os.makedirs(self._wal_dir, exist_ok=True)

    def _make_mgr(self, log_dir: str | None = None) -> SessionManager:
        profile = _make_profile("COM0", log_dir=log_dir)
        wal = WalWriter(wal_dir=self._wal_dir)
        return SessionManager(
            [profile], wal,
            on_ready=lambda sid: None,
            on_detached=lambda sid: None,
        )

    def test_open_permission_denied(self) -> None:
        """log_dir 存在但無寫入權限時回傳 LOG_OPEN_FAILED"""
        bad_dir = os.path.join(self._tmpdir, "readonly")
        os.makedirs(bad_dir)
        os.chmod(bad_dir, 0o444)
        try:
            mgr = self._make_mgr(log_dir=bad_dir)
            result = mgr.log_start("COM0")
            self.assertFalse(result["ok"])
            self.assertEqual(result["error_code"], "LOG_OPEN_FAILED")
            self.assertIn("detail", result)
        finally:
            os.chmod(bad_dir, 0o755)

    def test_log_stop_session_not_found(self) -> None:
        mgr = self._make_mgr(log_dir=self._tmpdir)
        result = mgr.log_stop("NONEXISTENT")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "SESSION_NOT_FOUND")

    def test_log_status_session_not_found(self) -> None:
        mgr = self._make_mgr(log_dir=self._tmpdir)
        result = mgr.log_status("NONEXISTENT")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "SESSION_NOT_FOUND")

    def test_log_stop_no_active_capture(self) -> None:
        """未啟動 capture 即停止，應回傳 NO_ACTIVE_CAPTURE"""
        mgr = self._make_mgr(log_dir=self._tmpdir)
        result = mgr.log_stop("COM0")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "NO_ACTIVE_CAPTURE")


# ── 新增測試：log_stop 欄位完整性 ───────────────────────────


class TestLogStopFields(unittest.TestCase):
    """log_stop 回傳值的欄位驗證"""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._log_dir = os.path.join(self._tmpdir, "logs")
        self._wal_dir = os.path.join(self._tmpdir, "wal")
        os.makedirs(self._wal_dir, exist_ok=True)
        profile = _make_profile("COM0", log_dir=self._log_dir)
        wal = WalWriter(wal_dir=self._wal_dir)
        self._mgr = SessionManager(
            [profile], wal,
            on_ready=lambda sid: None,
            on_detached=lambda sid: None,
        )
        self._session_id = list(self._mgr._sessions.keys())[0]

    def tearDown(self) -> None:
        for s in self._mgr._sessions.values():
            if s.active_capture:
                self._mgr.log_stop(s.profile.com)

    def test_stop_returns_started_at(self) -> None:
        self._mgr.log_start("COM0")
        result = self._mgr.log_stop("COM0")
        self.assertTrue(result["ok"])
        self.assertIn("started_at", result)
        self.assertIsInstance(result["started_at"], str)
        self.assertGreater(len(result["started_at"]), 0)

    def test_stop_returns_accurate_counts(self) -> None:
        """寫入已知資料後 stop，驗證計數準確"""
        self._mgr.log_start("COM0")
        self._mgr._on_bridge_rx(self._session_id, b"line1\n")
        self._mgr._on_bridge_rx(self._session_id, b"line2\nline3\n")
        result = self._mgr.log_stop("COM0")
        self.assertEqual(result["line_count"], 3)
        expected_bytes = len("line1\n") + len("line2\nline3\n")
        self.assertEqual(result["byte_count"], expected_bytes)

    def test_stop_returns_log_path(self) -> None:
        start_r = self._mgr.log_start("COM0")
        stop_r = self._mgr.log_stop("COM0")
        self.assertEqual(start_r["log_path"], stop_r["log_path"])
        self.assertEqual(start_r["capture_id"], stop_r["capture_id"])


# ── 新增測試：RX 邊界條件 ───────────────────────────────────


class TestRxEdgeCases(unittest.TestCase):
    """_on_bridge_rx 與 capture 的邊界條件"""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._log_dir = os.path.join(self._tmpdir, "logs")
        self._wal_dir = os.path.join(self._tmpdir, "wal")
        os.makedirs(self._wal_dir, exist_ok=True)
        profile = _make_profile("COM0", log_dir=self._log_dir)
        wal = WalWriter(wal_dir=self._wal_dir)
        self._mgr = SessionManager(
            [profile], wal,
            on_ready=lambda sid: None,
            on_detached=lambda sid: None,
        )
        self._session_id = list(self._mgr._sessions.keys())[0]

    def tearDown(self) -> None:
        for s in self._mgr._sessions.values():
            if s.active_capture:
                self._mgr.log_stop(s.profile.com)

    def test_empty_rx_ignored(self) -> None:
        """空資料不應寫入也不應崩潰"""
        self._mgr.log_start("COM0")
        session = self._mgr._sessions[self._session_id]
        cap = session.active_capture
        self._mgr._on_bridge_rx(self._session_id, b"")
        self.assertEqual(cap.byte_count, 0)
        self.assertEqual(cap.line_count, 0)

    def test_non_utf8_binary_data(self) -> None:
        """非 UTF-8 二進位 RX 不崩潰，使用 replacement char"""
        self._mgr.log_start("COM0")
        session = self._mgr._sessions[self._session_id]
        cap = session.active_capture
        # 送入包含無效 UTF-8 的 byte sequence
        bad_bytes = b"\x80\xfe\xff hello\n"
        self._mgr._on_bridge_rx(self._session_id, bad_bytes)
        self.assertEqual(cap.line_count, 1)
        self.assertGreater(cap.byte_count, 0)
        # 讀回檔案，確認 replacement char 存在
        with open(cap.log_path, "r", encoding="utf-8") as fp:
            content = fp.read()
        self.assertIn("hello", content)
        self.assertIn("\ufffd", content)

    def test_rx_without_capture_noop(self) -> None:
        """未啟動 capture 時 RX 不崩潰"""
        self._mgr._on_bridge_rx(self._session_id, b"some data\n")
        session = self._mgr._sessions[self._session_id]
        self.assertIsNone(session.active_capture)

    def test_rx_with_foreground_busy_skipped(self) -> None:
        """foreground_busy 時 RX 整段跳過（含 capture 寫入）"""
        self._mgr.log_start("COM0")
        session = self._mgr._sessions[self._session_id]
        session.foreground_busy = True
        cap = session.active_capture
        self._mgr._on_bridge_rx(self._session_id, b"hidden data\n")
        self.assertEqual(cap.byte_count, 0)
        self.assertEqual(cap.line_count, 0)
        session.foreground_busy = False

    def test_rx_write_failure_graceful(self) -> None:
        """capture 檔案寫入失敗時不崩潰，靜默忽略"""
        self._mgr.log_start("COM0")
        session = self._mgr._sessions[self._session_id]
        cap = session.active_capture
        # 關掉 file handle 模擬寫入失敗
        fp = self._mgr._capture_fps[cap.capture_id]
        fp.close()
        # 不應拋出例外
        self._mgr._on_bridge_rx(self._session_id, b"will fail\n")
        # 計數器不應更新（因為 except 在 write 之前攔截）
        # 但即使有更新也不影響功能正確性，重點是不崩潰
        # 清理：設為 None 避免 tearDown 雙重 close
        self._mgr._capture_fps.pop(cap.capture_id, None)
        session.active_capture = None

    def test_large_rx_data_counts(self) -> None:
        """大量 RX 資料的 line_count 與 byte_count 累計正確"""
        self._mgr.log_start("COM0")
        session = self._mgr._sessions[self._session_id]
        cap = session.active_capture
        total_lines = 0
        total_bytes = 0
        for i in range(100):
            line = f"line-{i:04d} " + "x" * 80 + "\n"
            self._mgr._on_bridge_rx(self._session_id, line.encode("utf-8"))
            total_lines += 1
            total_bytes += len(line)
        self.assertEqual(cap.line_count, total_lines)
        self.assertEqual(cap.byte_count, total_bytes)


# ── 新增測試：多 session 隔離 ──────────────────────────────


class TestMultiSessionCapture(unittest.TestCase):
    """多 session 各自獨立 capture"""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._log_dir = os.path.join(self._tmpdir, "logs")
        self._wal_dir = os.path.join(self._tmpdir, "wal")
        os.makedirs(self._wal_dir, exist_ok=True)
        profiles = [
            _make_profile("COM0", log_dir=self._log_dir, alias="a0"),
            _make_profile("COM1", log_dir=self._log_dir, alias="a1"),
        ]
        wal = WalWriter(wal_dir=self._wal_dir)
        self._mgr = SessionManager(
            profiles, wal,
            on_ready=lambda sid: None,
            on_detached=lambda sid: None,
        )

    def tearDown(self) -> None:
        for s in self._mgr._sessions.values():
            if s.active_capture:
                self._mgr.log_stop(s.profile.com)

    def test_independent_captures(self) -> None:
        """兩個 session 各自 capture 互不影響"""
        r0 = self._mgr.log_start("COM0")
        r1 = self._mgr.log_start("COM1")
        self.assertTrue(r0["ok"])
        self.assertTrue(r1["ok"])
        self.assertNotEqual(r0["capture_id"], r1["capture_id"])
        self.assertNotEqual(r0["log_path"], r1["log_path"])

        # 分別寫入 RX，驗證不會交叉
        for s in self._mgr._sessions.values():
            data = f"data-for-{s.profile.com}\n".encode()
            self._mgr._on_bridge_rx(s.session_id, data)

        # 停止並驗證各自計數
        stop0 = self._mgr.log_stop("COM0")
        stop1 = self._mgr.log_stop("COM1")
        self.assertEqual(stop0["line_count"], 1)
        self.assertEqual(stop1["line_count"], 1)

        # 驗證檔案內容隔離
        with open(r0["log_path"], "r") as fp:
            self.assertIn("COM0", fp.read())
        with open(r1["log_path"], "r") as fp:
            self.assertIn("COM1", fp.read())

    def test_stop_one_doesnt_affect_other(self) -> None:
        """停止 COM0 capture 不影響 COM1"""
        self._mgr.log_start("COM0")
        self._mgr.log_start("COM1")
        self._mgr.log_stop("COM0")
        status1 = self._mgr.log_status("COM1")
        self.assertTrue(status1["active"])


# ── 新增測試：環境變數 fallback ─────────────────────────────


class TestEnvVarLogDir(unittest.TestCase):
    """SERIALWRAP_LOG_DIR 環境變數 fallback"""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._wal_dir = os.path.join(self._tmpdir, "wal")
        os.makedirs(self._wal_dir, exist_ok=True)

    def test_env_var_fallback(self) -> None:
        """profile.log_dir 為 None 時使用 LOG_DIR 常數"""
        profile = _make_profile("COM0", log_dir=None)
        wal = WalWriter(wal_dir=self._wal_dir)
        mgr = SessionManager(
            [profile], wal,
            on_ready=lambda sid: None,
            on_detached=lambda sid: None,
        )
        session = list(mgr._sessions.values())[0]
        from sw_core.constants import LOG_DIR
        resolved = mgr._resolve_log_dir(session)
        self.assertEqual(resolved, LOG_DIR)

    def test_profile_log_dir_overrides_env(self) -> None:
        """profile.log_dir 有值時不使用 LOG_DIR 常數"""
        custom = os.path.join(self._tmpdir, "custom")
        profile = _make_profile("COM0", log_dir=custom)
        wal = WalWriter(wal_dir=self._wal_dir)
        mgr = SessionManager(
            [profile], wal,
            on_ready=lambda sid: None,
            on_detached=lambda sid: None,
        )
        session = list(mgr._sessions.values())[0]
        resolved = mgr._resolve_log_dir(session)
        self.assertEqual(resolved, custom)


# ── 新增測試：檔名格式 ──────────────────────────────────────


class TestLogFilename(unittest.TestCase):
    """log 檔名格式驗證"""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._log_dir = os.path.join(self._tmpdir, "logs")
        self._wal_dir = os.path.join(self._tmpdir, "wal")
        os.makedirs(self._wal_dir, exist_ok=True)
        profile = _make_profile("COM0", log_dir=self._log_dir)
        wal = WalWriter(wal_dir=self._wal_dir)
        self._mgr = SessionManager(
            [profile], wal,
            on_ready=lambda sid: None,
            on_detached=lambda sid: None,
        )

    def tearDown(self) -> None:
        for s in self._mgr._sessions.values():
            if s.active_capture:
                self._mgr.log_stop(s.profile.com)

    def test_filename_format(self) -> None:
        """檔名格式為 {COM}_{YYMMDD}-{HHMMSS}.log"""
        import re
        result = self._mgr.log_start("COM0")
        basename = os.path.basename(result["log_path"])
        pattern = r"^COM0_\d{6}-\d{6}\.log$"
        self.assertRegex(basename, pattern)

    def test_nested_log_dir_created(self) -> None:
        """深層 log_dir 不存在時自動建立"""
        deep_dir = os.path.join(self._tmpdir, "a", "b", "c", "logs")
        profile = _make_profile("COM0", log_dir=deep_dir)
        wal = WalWriter(wal_dir=self._wal_dir)
        mgr = SessionManager(
            [profile], wal,
            on_ready=lambda sid: None,
            on_detached=lambda sid: None,
        )
        result = mgr.log_start("COM0")
        self.assertTrue(result["ok"])
        self.assertTrue(os.path.isdir(deep_dir))
        mgr.log_stop("COM0")


# ── 新增測試：to_public_dict 邊界 ──────────────────────────


class TestPublicDictCapture(unittest.TestCase):
    """to_public_dict 中 capture 欄位的邊界條件"""

    def test_no_capture_returns_none(self) -> None:
        """無 active capture 時 capture 欄位為 None"""
        tmpdir = tempfile.mkdtemp()
        wal_dir = os.path.join(tmpdir, "wal")
        os.makedirs(wal_dir, exist_ok=True)
        profile = _make_profile("COM0", log_dir=tmpdir)
        wal = WalWriter(wal_dir=wal_dir)
        mgr = SessionManager(
            [profile], wal,
            on_ready=lambda sid: None,
            on_detached=lambda sid: None,
        )
        session = list(mgr._sessions.values())[0]
        d = session.to_public_dict()
        self.assertIsNone(d["capture"])


# ── 新增測試：selector 多種形式 ─────────────────────────────


class TestSelectorVariants(unittest.TestCase):
    """log_start / log_stop / log_status 支援 COM / alias / session_id"""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._log_dir = os.path.join(self._tmpdir, "logs")
        self._wal_dir = os.path.join(self._tmpdir, "wal")
        os.makedirs(self._wal_dir, exist_ok=True)
        profile = _make_profile("COM0", log_dir=self._log_dir, alias="mybox")
        wal = WalWriter(wal_dir=self._wal_dir)
        self._mgr = SessionManager(
            [profile], wal,
            on_ready=lambda sid: None,
            on_detached=lambda sid: None,
        )
        self._session_id = list(self._mgr._sessions.keys())[0]

    def tearDown(self) -> None:
        for s in self._mgr._sessions.values():
            if s.active_capture:
                self._mgr.log_stop(s.profile.com)

    def test_start_by_alias(self) -> None:
        result = self._mgr.log_start("mybox")
        self.assertTrue(result["ok"])

    def test_status_by_session_id(self) -> None:
        self._mgr.log_start("COM0")
        result = self._mgr.log_status(self._session_id)
        self.assertTrue(result["ok"])
        self.assertTrue(result["active"])

    def test_stop_by_alias(self) -> None:
        self._mgr.log_start("COM0")
        result = self._mgr.log_stop("mybox")
        self.assertTrue(result["ok"])


# ── 新增測試：_stop_capture_locked 邊界 ────────────────────


class TestStopCaptureLocked(unittest.TestCase):
    """_stop_capture_locked 的邊界行為"""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._log_dir = os.path.join(self._tmpdir, "logs")
        self._wal_dir = os.path.join(self._tmpdir, "wal")
        os.makedirs(self._wal_dir, exist_ok=True)
        profile = _make_profile("COM0", log_dir=self._log_dir)
        wal = WalWriter(wal_dir=self._wal_dir)
        self._mgr = SessionManager(
            [profile], wal,
            on_ready=lambda sid: None,
            on_detached=lambda sid: None,
        )
        self._session_id = list(self._mgr._sessions.keys())[0]

    def test_stop_when_no_capture_noop(self) -> None:
        """active_capture 為 None 時 _stop_capture_locked 不崩潰"""
        session = self._mgr._sessions[self._session_id]
        self.assertIsNone(session.active_capture)
        with self._mgr._lock:
            self._mgr._stop_capture_locked(session)
        self.assertIsNone(session.active_capture)

    def test_stop_sets_status_to_stopped(self) -> None:
        """_stop_capture_locked 把 status 設為 stopped"""
        self._mgr.log_start("COM0")
        session = self._mgr._sessions[self._session_id]
        cap = session.active_capture
        self.assertEqual(cap.status, "active")
        with self._mgr._lock:
            self._mgr._stop_capture_locked(session)
        self.assertEqual(cap.status, "stopped")
        self.assertIsNone(session.active_capture)

    def test_stop_closes_file_handle(self) -> None:
        """_stop_capture_locked 關閉檔案 handle"""
        self._mgr.log_start("COM0")
        session = self._mgr._sessions[self._session_id]
        cap = session.active_capture
        fp = self._mgr._capture_fps[cap.capture_id]
        self.assertFalse(fp.closed)
        with self._mgr._lock:
            self._mgr._stop_capture_locked(session)
        self.assertTrue(fp.closed)
        self.assertNotIn(cap.capture_id, self._mgr._capture_fps)


if __name__ == "__main__":
    unittest.main()
