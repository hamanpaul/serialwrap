"""Tests for event handler fixes (issues 1-6)."""

import json
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch
from io import StringIO

import pytest

from serialwrap_reboot_test.event_handler import (
    resolve_active_state,
    derive_report_path_from_log,
    scan_log_for_events,
    generate_report_content,
    save_scan_cursors,
    load_scan_cursors,
    handle_event,
)


class TestIssue1ReportPathFromState:
    """Issue 1: report_path.txt from current-run state must be used."""

    def test_resolve_state_uses_report_path_from_state(self):
        """Use report_path.txt from state when available."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            state_dir = tmpdir / "serialwrap-reboot-test.COM0.12345"
            state_dir.mkdir()

            log_path = tmpdir / "mini_COM0_260506-152744.log"
            log_path.touch()
            report_path = tmpdir / "event-triggered_COM0_260506-152744.md"

            (state_dir / "active_minicom_log.txt").write_text(str(log_path))
            (state_dir / "report_path.txt").write_text(str(report_path))

            active_log, active_report = resolve_active_state(
                "COM0", state_root=tmpdir, log_dir=tmpdir
            )

            assert active_log == log_path
            assert active_report == report_path

    def test_resolve_state_derives_report_when_state_has_no_report_path(self):
        """Derive report path when state has no report_path.txt."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            state_dir = tmpdir / "serialwrap-reboot-test.COM0.12345"
            state_dir.mkdir()

            log_path = tmpdir / "mini_COM0_260506-152744.log"
            log_path.touch()

            (state_dir / "active_minicom_log.txt").write_text(str(log_path))
            # No report_path.txt

            active_log, active_report = resolve_active_state(
                "COM0", state_root=tmpdir, log_dir=tmpdir
            )

            assert active_log == log_path
            assert active_report is not None
            assert "event-triggered_COM0_260506-152744.md" in str(active_report)

    def test_handle_event_rejects_invalid_report_path_from_state(self):
        """handle_event should reject report_path.txt that does not match the log."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            state_dir = tmpdir / "serialwrap-reboot-test.COM0.12345"
            state_dir.mkdir()

            log_path = tmpdir / "mini_COM0_260506-152744.log"
            log_path.write_text("line 1 brcm-therm\n")

            # Forged/stale state specifies a custom report path
            custom_report = tmpdir / "custom-report.md"
            derived_report = tmpdir / "event-triggered_COM0_260506-152744.md"

            (state_dir / "active_minicom_log.txt").write_text(str(log_path))
            (state_dir / "report_path.txt").write_text(str(custom_report))

            payload = {
                "selector": "COM0",
                "event": "brcm-therm",
                "timestamp": "2026-05-06T16:10:23+08:00"
            }

            result = handle_event(payload, state_root=tmpdir, log_dir=tmpdir)

            assert result == 0
            assert not custom_report.exists()
            assert derived_report.exists()
            assert "brcm-therm" in derived_report.read_text()

    def test_resolve_state_rejects_report_path_outside_log_dir(self):
        """Reject report_path.txt pointing outside the minicom log directory."""
        with tempfile.TemporaryDirectory() as tmpdir, tempfile.TemporaryDirectory() as outside:
            tmpdir = Path(tmpdir)
            outside = Path(outside)
            state_dir = tmpdir / "serialwrap-reboot-test.COM0.12345"
            state_dir.mkdir()

            log_path = tmpdir / "mini_COM0_260506-152744.log"
            log_path.touch()
            forged_report = outside / "event-triggered_COM0_260506-152744.md"
            derived_report = tmpdir / "event-triggered_COM0_260506-152744.md"

            (state_dir / "active_minicom_log.txt").write_text(str(log_path))
            (state_dir / "report_path.txt").write_text(str(forged_report))

            active_log, active_report = resolve_active_state(
                "COM0", state_root=tmpdir, log_dir=tmpdir
            )

            assert active_log == log_path
            assert active_report == derived_report


class TestIssue2RobustReportPathDerivation:
    """Issue 2: Report path derivation must not crash on unexpected log names."""

    def test_derive_report_path_from_standard_log(self):
        """Derive report path from standard minicom log name."""
        log_path = Path("/home/user/b-log/mini_COM0_260506-152744.log")
        result = derive_report_path_from_log("COM0", log_path)

        assert result is not None
        assert result.name == "event-triggered_COM0_260506-152744.md"
        assert result.parent == log_path.parent

    def test_derive_report_path_from_nonstandard_log_returns_none(self):
        """Return None for non-standard log names."""
        log_path = Path("/home/user/b-log/weird-log.log")
        result = derive_report_path_from_log("COM0", log_path)

        assert result is None

    def test_derive_report_path_from_log_without_underscore_returns_none(self):
        """Return None for log names without expected format."""
        log_path = Path("/home/user/b-log/mini.log")
        result = derive_report_path_from_log("COM0", log_path)

        assert result is None

    def test_handle_event_returns_nonzero_for_nonstandard_log_name(self):
        """Handler returns non-zero and stderr if cannot derive report path."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            state_dir = tmpdir / "serialwrap-reboot-test.COM0.12345"
            state_dir.mkdir()

            # Non-standard log name
            log_path = tmpdir / "weird-log.log"
            log_path.write_text("line 1 brcm-therm\n")

            (state_dir / "active_minicom_log.txt").write_text(str(log_path))
            # No report_path.txt

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
            assert "report path" in stderr_output.lower() or "derive" in stderr_output.lower()


class TestIssue3PathTraversalSecurity:
    """Issue 3: Validate resolved log paths are within expected log directory."""

    def test_resolve_active_state_rejects_path_traversal_in_state(self):
        """Reject path traversal attempts in active_minicom_log.txt."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            state_dir = tmpdir / "serialwrap-reboot-test.COM0.12345"
            state_dir.mkdir()

            # Attacker tries to point to /etc/passwd
            (state_dir / "active_minicom_log.txt").write_text("/etc/passwd")

            active_log, active_report = resolve_active_state(
                "COM0", state_root=tmpdir, log_dir=tmpdir
            )

            # Should reject and return None
            assert active_log is None
            assert active_report is None

    def test_resolve_active_state_rejects_relative_path_traversal(self):
        """Reject relative path traversal like ../../etc/passwd."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            log_dir = tmpdir / "b-log"
            log_dir.mkdir()

            state_dir = tmpdir / "serialwrap-reboot-test.COM0.12345"
            state_dir.mkdir()

            # Attacker tries relative path traversal
            (state_dir / "active_minicom_log.txt").write_text("../../etc/passwd")

            active_log, active_report = resolve_active_state(
                "COM0", state_root=tmpdir, log_dir=log_dir
            )

            assert active_log is None
            assert active_report is None

    def test_resolve_active_state_accepts_valid_log_in_log_dir(self):
        """Accept valid logs within log_dir."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            log_dir = tmpdir / "b-log"
            log_dir.mkdir()

            state_dir = tmpdir / "serialwarp-reboot-test.COM0.12345"
            state_dir.mkdir()

            log_path = log_dir / "mini_COM0_260506-152744.log"
            log_path.touch()

            (state_dir / "active_minicom_log.txt").write_text(str(log_path))

            active_log, active_report = resolve_active_state(
                "COM0", state_root=tmpdir, log_dir=log_dir
            )

            assert active_log == log_path


class TestIssue4StreamingFirstMatchConsumption:
    """Issue 4: Only consume first match, not materialize all matches."""

    def test_scan_log_for_events_returns_generator(self):
        """scan_log_for_events should return a generator, not a list."""
        with tempfile.TemporaryFile(mode="w+") as f:
            f.write("line 1 brcm-therm\n")
            f.write("line 2\n")
            f.seek(0)

            result = scan_log_for_events(f, "brcm-therm", start_line=0)

            # Should be a generator or iterator, not a list
            import types
            assert isinstance(result, types.GeneratorType) or hasattr(result, '__next__')

    def test_handle_event_consumes_only_first_match(self):
        """Verify handle_event only consumes first match from scan."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            state_dir = tmpdir / "serialwrap-reboot-test.COM0.12345"
            state_dir.mkdir()

            # Log with many matches
            log_path = tmpdir / "mini_COM0_260506-152744.log"
            lines = ["line 1 brcm-therm\n"] + ["line X brcm-therm\n" for _ in range(10000)]
            log_path.write_text("".join(lines))

            (state_dir / "active_minicom_log.txt").write_text(str(log_path))

            payload = {
                "selector": "COM0",
                "event": "brcm-therm",
                "timestamp": "2026-05-06T16:10:23+08:00"
            }

            # This should complete quickly without materializing all 10001 matches
            result = handle_event(payload, state_root=tmpdir, log_dir=tmpdir)

            assert result == 0

            # Verify only first match was used
            report_path = tmpdir / "event-triggered_COM0_260506-152744.md"
            content = report_path.read_text()
            # Should only have one event
            assert content.count("| mini_COM0") == 1


class TestIssue5DenominatorZeroAllNA:
    """Issue 5: When denominator is 0, SMC bootloader should also show N/A."""

    def test_generate_report_zero_denominator_all_na(self):
        """All events including SMC bootloader show N/A when denominator is 0."""
        log_name = "mini_COM0_260506-152744.log"
        events = [
            {
                "log_name": log_name,
                "line_number": 10,
                "timestamp": "2026-05-06T16:10:20+08:00",
                "event": "brcm-therm"
            }
        ]
        summary = {"brcm-therm": 1}

        content = generate_report_content(log_name, events, summary)

        # Check that SMC bootloader also shows N/A
        assert "| SMC bootloader | 0 | N/A |" in content
        assert "100.00%" not in content


class TestIssue6AtomicCursorSaves:
    """Issue 6: Cursor saves should be atomic and merge with existing."""

    def test_save_cursors_merges_with_existing(self):
        """Saving cursors for one event preserves cursors for other events."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cursor_file = Path(tmpdir) / "cursors.json"

            # Save initial cursors
            save_scan_cursors(cursor_file, {"brcm-therm": 10})

            # Load and verify
            assert load_scan_cursors(cursor_file) == {"brcm-therm": 10}

            # Save different event (should merge, not overwrite)
            save_scan_cursors(cursor_file, {"pstate": 20})

            # Both should be present
            cursors = load_scan_cursors(cursor_file)
            assert cursors["brcm-therm"] == 10
            assert cursors["pstate"] == 20

    def test_save_cursors_updates_existing_event(self):
        """Saving cursors for an existing event updates its value."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cursor_file = Path(tmpdir) / "cursors.json"

            # Save initial
            save_scan_cursors(cursor_file, {"brcm-therm": 10, "pstate": 15})

            # Update brcm-therm
            save_scan_cursors(cursor_file, {"brcm-therm": 25})

            cursors = load_scan_cursors(cursor_file)
            assert cursors["brcm-therm"] == 25
            assert cursors["pstate"] == 15

    def test_save_cursors_is_atomic(self):
        """Cursor saves use atomic write (temp + rename)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cursor_file = Path(tmpdir) / "cursors.json"

            # Save some data
            save_scan_cursors(cursor_file, {"brcm-therm": 10})

            # File should exist and be readable
            assert cursor_file.exists()
            cursors = load_scan_cursors(cursor_file)
            assert cursors == {"brcm-therm": 10}

            # No temp files should remain
            temp_files = list(cursor_file.parent.glob("*.tmp*"))
            assert len(temp_files) == 0
