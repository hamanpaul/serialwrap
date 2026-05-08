"""Tests for robustness issues in event handler."""

import json
import sys
import tempfile
from io import StringIO
from pathlib import Path
from unittest.mock import patch, mock_open, MagicMock

import pytest

from serialwrap_reboot_test.event_handler import (
    load_scan_cursors,
    load_report_data,
    write_text_atomic,
)


class TestCursorLoadFailureWarning:
    """Test that cursor load failures emit warnings to stderr."""

    def test_load_cursors_warns_on_corrupt_json(self):
        """Corrupt JSON cursor file should emit warning and return empty dict."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cursor_file = Path(tmpdir) / "cursors.json"
            cursor_file.write_text("{ invalid json")

            old_stderr = sys.stderr
            sys.stderr = StringIO()
            try:
                result = load_scan_cursors(cursor_file)
                stderr_output = sys.stderr.getvalue()
            finally:
                sys.stderr = old_stderr

            # Should return empty dict (fail-open)
            assert result == {}

            # Should emit warning to stderr
            assert len(stderr_output) > 0
            assert "warning" in stderr_output.lower() or "error" in stderr_output.lower()
            assert "cursor" in stderr_output.lower()

    def test_load_cursors_warns_on_io_error(self):
        """I/O error reading cursor file should emit warning and return empty dict."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cursor_file = Path(tmpdir) / "cursors.json"
            # Create the file so exists() returns True
            cursor_file.write_text("{}")

            old_stderr = sys.stderr
            sys.stderr = StringIO()
            try:
                # Mock Path.open to raise IOError
                with patch.object(Path, 'open', side_effect=IOError("Read error")):
                    result = load_scan_cursors(cursor_file)
                stderr_output = sys.stderr.getvalue()
            finally:
                sys.stderr = old_stderr

            # Should return empty dict (fail-open)
            assert result == {}

            # Should emit warning to stderr
            assert len(stderr_output) > 0
            assert "warning" in stderr_output.lower() or "error" in stderr_output.lower()

    def test_load_cursors_succeeds_normally_without_warning(self):
        """Valid cursor file should load without warnings."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cursor_file = Path(tmpdir) / "cursors.json"
            cursor_file.write_text('{"brcm-therm": 10}')

            old_stderr = sys.stderr
            sys.stderr = StringIO()
            try:
                result = load_scan_cursors(cursor_file)
                stderr_output = sys.stderr.getvalue()
            finally:
                sys.stderr = old_stderr

            assert result == {"brcm-therm": 10}
            # No warnings for valid file
            assert stderr_output == ""


class TestReportParseFailureWarning:
    """Test that report parse failures emit warnings to stderr."""

    def test_load_report_warns_on_malformed_summary_row(self):
        """Malformed summary row should emit warning and skip that row."""
        with tempfile.TemporaryDirectory() as tmpdir:
            report_path = Path(tmpdir) / "report.md"
            report_path.write_text("""# Event Triggered Report

Log: mini_COM0_260506-152744.log

## Summary

| Event | Count | Probability vs SMC bootloader |
| --- | ---: | ---: |
| brcm-therm | not-a-number | 50.00% |
| pstate | 2 | 100.00% |

## Events

| Log name | Log line number | Event trigger time | Event |
| --- | ---: | --- | --- |
| mini_COM0_260506-152744.log | 1 | 2026-05-06T16:10:23+08:00 | pstate |
""")

            old_stderr = sys.stderr
            sys.stderr = StringIO()
            try:
                result = load_report_data(report_path)
                stderr_output = sys.stderr.getvalue()
            finally:
                sys.stderr = old_stderr

            # Should load valid data and skip malformed
            assert result["summary"]["pstate"] == 2
            assert "brcm-therm" not in result["summary"]  # Malformed row skipped

            # Should emit warning
            assert len(stderr_output) > 0
            assert "warning" in stderr_output.lower()
            assert "summary" in stderr_output.lower() or "malformed" in stderr_output.lower()

    def test_load_report_warns_on_malformed_event_row(self):
        """Malformed event row should emit warning and skip that row."""
        with tempfile.TemporaryDirectory() as tmpdir:
            report_path = Path(tmpdir) / "report.md"
            report_path.write_text("""# Event Triggered Report

Log: mini_COM0_260506-152744.log

## Summary

| Event | Count | Probability vs SMC bootloader |
| --- | ---: | ---: |
| pstate | 2 | 100.00% |

## Events

| Log name | Log line number | Event trigger time | Event |
| --- | ---: | --- | --- |
| mini_COM0_260506-152744.log | not-a-line-number | 2026-05-06T16:10:23+08:00 | pstate |
| mini_COM0_260506-152744.log | 5 | 2026-05-06T16:10:24+08:00 | brcm-therm |
""")

            old_stderr = sys.stderr
            sys.stderr = StringIO()
            try:
                result = load_report_data(report_path)
                stderr_output = sys.stderr.getvalue()
            finally:
                sys.stderr = old_stderr

            # Should load valid event and skip malformed
            assert len(result["events"]) == 1
            assert result["events"][0]["event"] == "brcm-therm"
            assert result["events"][0]["line_number"] == 5

            # Should emit warning
            assert len(stderr_output) > 0
            assert "warning" in stderr_output.lower()
            assert "event" in stderr_output.lower() or "malformed" in stderr_output.lower()

    def test_load_report_succeeds_normally_without_warnings(self):
        """Valid report should load without warnings."""
        with tempfile.TemporaryDirectory() as tmpdir:
            report_path = Path(tmpdir) / "report.md"
            report_path.write_text("""# Event Triggered Report

Log: mini_COM0_260506-152744.log

## Summary

| Event | Count | Probability vs SMC bootloader |
| --- | ---: | ---: |
| pstate | 1 | 100.00% |

## Events

| Log name | Log line number | Event trigger time | Event |
| --- | ---: | --- | --- |
| mini_COM0_260506-152744.log | 5 | 2026-05-06T16:10:24+08:00 | pstate |
""")

            old_stderr = sys.stderr
            sys.stderr = StringIO()
            try:
                result = load_report_data(report_path)
                stderr_output = sys.stderr.getvalue()
            finally:
                sys.stderr = old_stderr

            assert result["summary"]["pstate"] == 1
            assert len(result["events"]) == 1
            # No warnings for valid file
            assert stderr_output == ""


class TestAtomicWriteCleanup:
    """Test that atomic write cleans up temp files on failure."""

    def test_write_text_atomic_cleans_temp_on_write_failure(self):
        """Temp file should be cleaned up when write fails."""
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = Path(tmpdir) / "report.md"

            # Track temp files created
            created_temp_files = []
            original_mkstemp = tempfile.mkstemp

            def track_mkstemp(*args, **kwargs):
                fd, path = original_mkstemp(*args, **kwargs)
                created_temp_files.append(path)
                return fd, path

            with patch('tempfile.mkstemp', side_effect=track_mkstemp):
                # Mock fdopen to raise IOError during write
                with patch('os.fdopen', side_effect=IOError("Disk full")):
                    with pytest.raises(IOError, match="Failed to write file atomically"):
                        write_text_atomic(file_path, "content")

            # Verify temp file was cleaned up
            assert len(created_temp_files) == 1
            temp_path = created_temp_files[0]
            assert not Path(temp_path).exists(), "Temp file should be cleaned up"

    def test_write_text_atomic_cleans_temp_on_rename_failure(self):
        """Temp file should be cleaned up when rename fails."""
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = Path(tmpdir) / "report.md"

            # Track temp files
            created_temp_files = []
            original_mkstemp = tempfile.mkstemp

            def track_mkstemp(*args, **kwargs):
                fd, path = original_mkstemp(*args, **kwargs)
                created_temp_files.append(path)
                return fd, path

            with patch('tempfile.mkstemp', side_effect=track_mkstemp):
                # Mock os.replace to fail
                with patch('os.replace', side_effect=OSError("Permission denied")):
                    with pytest.raises(IOError, match="Failed to write file atomically"):
                        write_text_atomic(file_path, "content")

            # Verify temp file was cleaned up
            assert len(created_temp_files) == 1
            temp_path = created_temp_files[0]
            assert not Path(temp_path).exists(), "Temp file should be cleaned up"

    def test_write_text_atomic_succeeds_without_temp_remnants(self):
        """Successful write should not leave temp files."""
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = Path(tmpdir) / "report.md"

            write_text_atomic(file_path, "content")

            # Check no temp files remain
            temp_files = list(Path(tmpdir).glob("*.tmp*")) + list(Path(tmpdir).glob(".tmp*"))
            assert len(temp_files) == 0, f"Found temp files: {temp_files}"

            # Target file should exist
            assert file_path.exists()
            assert file_path.read_text() == "content"
