"""Tests for event handler race condition and error handling fixes."""

import json
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import Mock, patch, mock_open

import pytest

from serialwrap_reboot_test.event_handler import (
    save_scan_cursors,
    load_scan_cursors,
    handle_event,
    write_text_atomic,
)


class TestCursorSaveLocking:
    """Test cursor save operations with locking."""

    def test_cursor_save_preserves_concurrent_updates_sequential(self):
        """Sequential saves to different events preserve both (lock testing)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cursor_file = Path(tmpdir) / "cursors.json"

            # Save first event
            save_scan_cursors(cursor_file, {"brcm-therm": 10})

            # Save second event (should merge with first)
            save_scan_cursors(cursor_file, {"pstate": 20})

            # Load and verify both are present
            cursors = load_scan_cursors(cursor_file)
            assert cursors["brcm-therm"] == 10
            assert cursors["pstate"] == 20

    def test_cursor_save_concurrent_writes_with_threads(self):
        """Concurrent writes from threads preserve all updates."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cursor_file = Path(tmpdir) / "cursors.json"
            errors = []

            def save_event(event_name, line_num):
                try:
                    save_scan_cursors(cursor_file, {event_name: line_num})
                except Exception as e:
                    errors.append(e)

            # Launch concurrent saves
            threads = [
                threading.Thread(target=save_event, args=("brcm-therm", 10)),
                threading.Thread(target=save_event, args=("pstate", 20)),
                threading.Thread(target=save_event, args=("Link is Down", 30)),
            ]

            for t in threads:
                t.start()
            for t in threads:
                t.join()

            assert len(errors) == 0, f"Errors during concurrent saves: {errors}"

            # All events should be present
            cursors = load_scan_cursors(cursor_file)
            assert len(cursors) >= 3, f"Expected 3 events, got {len(cursors)}"
            assert "brcm-therm" in cursors
            assert "pstate" in cursors
            assert "Link is Down" in cursors


class TestReportWriteLocking:
    """Test report write operations with locking."""

    def test_sequential_event_updates_preserve_all_events(self):
        """Sequential updates to same report preserve all events."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)

            # Create log and state
            log_path = tmpdir / "mini_COM0_260506-152744.log"
            log_path.write_text(
                "line 1 brcm-therm\n"
                "line 2 pstate\n"
                "line 3 Link is Down\n"
            )

            state_dir = tmpdir / "serialwrap-reboot-test.COM0.12345"
            state_dir.mkdir()
            (state_dir / "active_minicom_log.txt").write_text(str(log_path))

            # First event
            payload1 = {
                "selector": "COM0",
                "event": "brcm-therm",
                "timestamp": "2026-05-06T16:10:20+08:00"
            }
            result1 = handle_event(payload1, state_root=tmpdir, log_dir=tmpdir)
            assert result1 == 0

            # Second event (different event type)
            payload2 = {
                "selector": "COM0",
                "event": "pstate",
                "timestamp": "2026-05-06T16:10:21+08:00"
            }
            result2 = handle_event(payload2, state_root=tmpdir, log_dir=tmpdir)
            assert result2 == 0

            # Check report has both events
            report_path = tmpdir / "event-triggered_COM0_260506-152744.md"
            content = report_path.read_text()
            assert "brcm-therm" in content
            assert "pstate" in content
            assert content.count("| mini_COM0") == 2


class TestAtomicReportWrite:
    """Test atomic report writing."""

    def test_write_text_atomic_creates_file(self):
        """write_text_atomic creates file atomically."""
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = Path(tmpdir) / "test.md"
            content = "# Test Report\n\nContent here"

            write_text_atomic(file_path, content)

            assert file_path.exists()
            assert file_path.read_text() == content

    def test_write_text_atomic_replaces_existing(self):
        """write_text_atomic replaces existing file atomically."""
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = Path(tmpdir) / "test.md"

            # Create initial file
            file_path.write_text("old content")

            # Atomic replace
            write_text_atomic(file_path, "new content")

            assert file_path.read_text() == "new content"

    def test_write_text_atomic_no_temp_files_remain(self):
        """No temporary files remain after atomic write."""
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = Path(tmpdir) / "test.md"

            write_text_atomic(file_path, "content")

            # Check no temp files
            temp_files = list(Path(tmpdir).glob("*.tmp*"))
            assert len(temp_files) == 0


class TestErrorHandling:
    """Test error handling with narrow exceptions."""

    def test_handle_event_returns_nonzero_on_log_open_failure(self):
        """Handler returns non-zero on log open failure without traceback."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            state_dir = tmpdir / "serialwrap-reboot-test.COM0.12345"
            state_dir.mkdir()

            # Point to non-existent log
            log_path = tmpdir / "mini_COM0_260506-152744.log"
            # Don't create the file

            (state_dir / "active_minicom_log.txt").write_text(str(log_path))

            payload = {
                "selector": "COM0",
                "event": "brcm-therm",
                "timestamp": "2026-05-06T16:10:23+08:00"
            }

            import sys
            from io import StringIO
            old_stderr = sys.stderr
            sys.stderr = StringIO()

            try:
                result = handle_event(payload, state_root=tmpdir, log_dir=tmpdir)
                stderr_output = sys.stderr.getvalue()
            finally:
                sys.stderr = old_stderr

            assert result != 0
            assert "error" in stderr_output.lower() or "failed" in stderr_output.lower()

    def test_handle_event_returns_nonzero_on_report_write_failure(self):
        """Handler returns non-zero on report write failure without traceback."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)

            # Create log
            log_path = tmpdir / "mini_COM0_260506-152744.log"
            log_path.write_text("line 1 brcm-therm\n")

            state_dir = tmpdir / "serialwrap-reboot-test.COM0.12345"
            state_dir.mkdir()
            (state_dir / "active_minicom_log.txt").write_text(str(log_path))

            # Make report directory read-only to cause write failure
            report_path = tmpdir / "event-triggered_COM0_260506-152744.md"
            # Create a directory with the report name to prevent writing
            report_path.mkdir()

            payload = {
                "selector": "COM0",
                "event": "brcm-therm",
                "timestamp": "2026-05-06T16:10:23+08:00"
            }

            import sys
            from io import StringIO
            old_stderr = sys.stderr
            sys.stderr = StringIO()

            try:
                result = handle_event(payload, state_root=tmpdir, log_dir=tmpdir)
                stderr_output = sys.stderr.getvalue()
            finally:
                sys.stderr = old_stderr

            assert result != 0
            assert "error" in stderr_output.lower() or "failed" in stderr_output.lower()

    def test_handle_event_handles_permission_error_gracefully(self):
        """Handler handles permission errors gracefully."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)

            log_path = tmpdir / "mini_COM0_260506-152744.log"
            log_path.write_text("line 1 brcm-therm\n")

            state_dir = tmpdir / "serialwrap-reboot-test.COM0.12345"
            state_dir.mkdir()
            (state_dir / "active_minicom_log.txt").write_text(str(log_path))

            payload = {
                "selector": "COM0",
                "event": "brcm-therm",
                "timestamp": "2026-05-06T16:10:23+08:00"
            }

            # Mock write_text_atomic to raise PermissionError
            with patch('serialwrap_reboot_test.event_handler.write_text_atomic') as mock_write:
                mock_write.side_effect = PermissionError("Permission denied")

                import sys
                from io import StringIO
                old_stderr = sys.stderr
                sys.stderr = StringIO()

                try:
                    result = handle_event(payload, state_root=tmpdir, log_dir=tmpdir)
                    stderr_output = sys.stderr.getvalue()
                finally:
                    sys.stderr = old_stderr

                assert result != 0
                # Should print error message, not traceback
                assert "error" in stderr_output.lower() or "permission" in stderr_output.lower()
