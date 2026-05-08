"""Tests for event handler."""

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest

from serialwrap_reboot_test.event_handler import (
    parse_event_payload,
    resolve_active_log,
    scan_log_for_events,
    load_report_data,
    generate_report_content,
    load_scan_cursors,
    save_scan_cursors,
    handle_event,
)


class TestPayloadParsing:
    """Test event payload parsing."""

    def test_parse_valid_payload_with_selector_and_event(self):
        """Parse a valid payload with selector and event."""
        payload = json.dumps({
            "selector": "COM0",
            "event": "brcm-therm",
            "matched_text": "brcm-therm error",
            "timestamp": "2026-05-06T16:10:23+08:00"
        })
        result = parse_event_payload(payload)
        assert result["selector"] == "COM0"
        assert result["event"] == "brcm-therm"

    def test_parse_payload_normalizes_rule_name_to_match_text(self):
        """Normalize serialwrap rule names to report/log match text."""
        payload = json.dumps({
            "selector": "COM1",
            "event": "kernel-panic",
            "timestamp": "2026-05-06T16:10:23+08:00"
        })
        result = parse_event_payload(payload)
        assert result["selector"] == "COM1"
        assert result["event"] == "Kernel panic"

    def test_parse_current_serialwrap_dispatch_payload(self):
        """Parse payload emitted by current EventEngine dispatcher."""
        payload = json.dumps({
            "selector": "COM1",
            "rule_id": "agent-reboot-controller.smc-bootloader",
            "rule_name": "smc-bootloader",
            "matched_text": "SMC bootloader",
            "matched_at": 1778121867975,
        })
        result = parse_event_payload(payload)
        assert result["selector"] == "COM1"
        assert result["event"] == "SMC bootloader"
        assert result["timestamp"].startswith("2026-")

    def test_parse_payload_rejects_invalid_selector(self):
        """Reject selectors that could escape current-run state matching."""
        payload = json.dumps({"selector": "../COM0", "event": "brcm-therm"})
        result = parse_event_payload(payload)
        assert result is None

    def test_parse_payload_rejects_unknown_event(self):
        """Reject event names outside the reboot-test rule set."""
        payload = json.dumps({"selector": "COM0", "event": "unexpected"})
        result = parse_event_payload(payload)
        assert result is None

    def test_parse_payload_rejects_non_string_event(self):
        """Reject non-string event values before log scanning."""
        payload = json.dumps({"selector": "COM0", "event": ["brcm-therm"]})
        result = parse_event_payload(payload)
        assert result is None

    def test_parse_payload_missing_selector_returns_none(self):
        """Return None if selector is missing."""
        payload = json.dumps({"event": "brcm-therm"})
        result = parse_event_payload(payload)
        assert result is None

    def test_parse_payload_missing_event_returns_none(self):
        """Return None if event is missing."""
        payload = json.dumps({"selector": "COM0"})
        result = parse_event_payload(payload)
        assert result is None

    def test_parse_invalid_json_returns_none(self):
        """Return None for invalid JSON."""
        result = parse_event_payload("not json")
        assert result is None

    def test_parse_empty_payload_returns_none(self):
        """Return None for empty payload."""
        result = parse_event_payload("")
        assert result is None


class TestActiveLogResolution:
    """Test active log resolution."""

    def test_resolve_from_current_run_state(self):
        """Resolve active log from current-run state."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            state_dir = tmpdir / "serialwrap-reboot-test.COM0.12345"
            state_dir.mkdir()

            log_path = tmpdir / "mini_COM0_260506-152744.log"
            log_path.touch()
            (state_dir / "active_minicom_log.txt").write_text(str(log_path))

            result = resolve_active_log("COM0", state_root=tmpdir, log_dir=tmpdir)
            assert result == log_path

    def test_resolve_fallback_to_recent_log(self):
        """Fall back to recent minicom log in log_dir."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)

            # Create a recent log
            recent_log = tmpdir / "mini_COM1_260506-152744.log"
            recent_log.touch()

            result = resolve_active_log("COM1", state_root=tmpdir, log_dir=tmpdir)
            assert result == recent_log

    def test_resolve_returns_none_if_no_recent_log(self):
        """Return None if no recent log within 10 minutes."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)

            # Create an old log
            old_log = tmpdir / "mini_COM0_260506-000000.log"
            old_log.touch()
            # Make it old by setting mtime to 20 minutes ago
            import time
            old_time = time.time() - 20 * 60
            import os
            os.utime(old_log, (old_time, old_time))

            result = resolve_active_log("COM0", state_root=tmpdir, log_dir=tmpdir)
            assert result is None

    def test_resolve_returns_none_if_log_does_not_exist(self):
        """Return None if the state file points to non-existent log."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            state_dir = tmpdir / "serialwrap-reboot-test.COM0.12345"
            state_dir.mkdir()

            nonexistent = tmpdir / "nonexistent.log"
            (state_dir / "active_minicom_log.txt").write_text(str(nonexistent))

            result = resolve_active_log("COM0", state_root=tmpdir, log_dir=tmpdir)
            assert result is None


class TestLogScanning:
    """Test log scanning with cursors."""

    def test_scan_log_for_single_event(self):
        """Scan log and find event matches."""
        with tempfile.TemporaryFile(mode="w+") as f:
            f.write("line 1\n")
            f.write("line 2 brcm-therm error\n")
            f.write("line 3\n")
            f.write("line 4 brcm-therm again\n")
            f.seek(0)

            matches = list(scan_log_for_events(f, "brcm-therm", start_line=0))
            assert len(matches) == 2
            assert matches[0] == (2, "line 2 brcm-therm error")
            assert matches[1] == (4, "line 4 brcm-therm again")

    def test_scan_log_with_start_cursor(self):
        """Scan log starting from a cursor position."""
        with tempfile.TemporaryFile(mode="w+") as f:
            f.write("line 1 brcm-therm\n")
            f.write("line 2\n")
            f.write("line 3 brcm-therm again\n")
            f.seek(0)

            matches = list(scan_log_for_events(f, "brcm-therm", start_line=1))
            assert len(matches) == 1
            assert matches[0] == (3, "line 3 brcm-therm again")

    def test_scan_log_no_matches(self):
        """Return empty list if no matches."""
        with tempfile.TemporaryFile(mode="w+") as f:
            f.write("line 1\n")
            f.write("line 2\n")
            f.seek(0)

            matches = list(scan_log_for_events(f, "brcm-therm", start_line=0))
            assert len(matches) == 0


class TestScanCursors:
    """Test cursor persistence."""

    def test_load_nonexistent_cursors_returns_empty(self):
        """Load cursors from nonexistent file returns empty dict."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cursor_file = Path(tmpdir) / "cursors.json"
            cursors = load_scan_cursors(cursor_file)
            assert cursors == {}

    def test_save_and_load_cursors(self):
        """Save and load cursor state."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cursor_file = Path(tmpdir) / "cursors.json"
            data = {"brcm-therm": 5, "pstate": 10}

            save_scan_cursors(cursor_file, data)
            loaded = load_scan_cursors(cursor_file)

            assert loaded == data


class TestReportData:
    """Test report data loading and generation."""

    def test_load_report_data_new_report(self):
        """Load report data for a new report."""
        with tempfile.TemporaryDirectory() as tmpdir:
            report_path = Path(tmpdir) / "event-triggered_COM0_260506-152744.md"

            data = load_report_data(report_path)

            assert data["events"] == []
            assert data["summary"] == {}

    def test_load_report_data_existing_report(self):
        """Load report data from existing report."""
        with tempfile.TemporaryDirectory() as tmpdir:
            report_path = Path(tmpdir) / "event-triggered_COM0_260506-152744.md"
            report_path.write_text("""# Event Triggered Report

Log: mini_COM0_260506-152744.log
Generated: 2026-05-06T16:10:23+08:00

## Summary

Denominator: SMC bootloader = 2

| Event | Count | Probability vs SMC bootloader |
| --- | ---: | ---: |
| brcm-therm | 1 | 50.00% |
| SMC bootloader | 2 | 100.00% |

## Events

| Log name | Log line number | Event trigger time | Event |
| --- | ---: | --- | --- |
| mini_COM0_260506-152744.log | 10 | 2026-05-06T16:10:20+08:00 | brcm-therm |
| mini_COM0_260506-152744.log | 20 | 2026-05-06T16:10:23+08:00 | SMC bootloader |
""")

            data = load_report_data(report_path)

            assert len(data["events"]) == 2
            assert data["summary"]["brcm-therm"] == 1
            assert data["summary"]["SMC bootloader"] == 2

    def test_generate_report_with_zero_denominator(self):
        """Generate report with zero SMC bootloader count shows N/A."""
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

        assert "N/A" in content
        assert "Denominator: SMC bootloader = 0" in content

    def test_generate_report_with_nonzero_denominator(self):
        """Generate report with probabilities."""
        log_name = "mini_COM0_260506-152744.log"
        events = [
            {
                "log_name": log_name,
                "line_number": 10,
                "timestamp": "2026-05-06T16:10:20+08:00",
                "event": "brcm-therm"
            },
            {
                "log_name": log_name,
                "line_number": 20,
                "timestamp": "2026-05-06T16:10:23+08:00",
                "event": "SMC bootloader"
            },
            {
                "log_name": log_name,
                "line_number": 30,
                "timestamp": "2026-05-06T16:10:26+08:00",
                "event": "SMC bootloader"
            }
        ]
        summary = {"brcm-therm": 1, "SMC bootloader": 2}

        content = generate_report_content(log_name, events, summary)

        assert "Denominator: SMC bootloader = 2" in content
        assert "50.00%" in content
        assert "100.00%" in content


class TestEventHandlerIntegration:
    """Test the full event handler flow."""

    def test_handle_event_with_valid_payload(self):
        """Handle a complete event with valid payload."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)

            # Set up log
            log_path = tmpdir / "mini_COM0_260506-152744.log"
            log_path.write_text("line 1\nline 2 brcm-therm error\nline 3\n")

            # Set up state
            state_dir = tmpdir / "serialwrap-reboot-test.COM0.12345"
            state_dir.mkdir()
            (state_dir / "active_minicom_log.txt").write_text(str(log_path))

            # Prepare payload
            payload = {
                "selector": "COM0",
                "event": "brcm-therm",
                "timestamp": "2026-05-06T16:10:23+08:00"
            }

            result = handle_event(
                payload,
                state_root=tmpdir,
                log_dir=tmpdir
            )

            assert result == 0

            # Check that report was created
            report_path = tmpdir / f"event-triggered_COM0_{log_path.stem.split('_', 2)[2]}.md"
            assert report_path.exists()

    def test_handle_event_missing_log_returns_nonzero(self):
        """Return non-zero if log cannot be resolved."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)

            payload = {
                "selector": "COM0",
                "event": "brcm-therm",
                "timestamp": "2026-05-06T16:10:23+08:00"
            }

            result = handle_event(
                payload,
                state_root=tmpdir,
                log_dir=tmpdir
            )

            assert result != 0

    def test_handle_event_is_idempotent(self):
        """Handler can be called multiple times with same event."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)

            # Set up log
            log_path = tmpdir / "mini_COM0_260506-152744.log"
            log_path.write_text("line 1\nline 2 brcm-therm\nline 3 brcm-therm\nline 4\n")

            # Set up state
            state_dir = tmpdir / "serialwrap-reboot-test.COM0.12345"
            state_dir.mkdir()
            (state_dir / "active_minicom_log.txt").write_text(str(log_path))

            payload = {
                "selector": "COM0",
                "event": "brcm-therm",
                "timestamp": "2026-05-06T16:10:23+08:00"
            }

            # First call
            result1 = handle_event(payload, state_root=tmpdir, log_dir=tmpdir)
            assert result1 == 0

            # Second call should advance cursor
            result2 = handle_event(payload, state_root=tmpdir, log_dir=tmpdir)
            assert result2 == 0

            # Check that both events are in report
            report_path = tmpdir / f"event-triggered_COM0_{log_path.stem.split('_', 2)[2]}.md"
            content = report_path.read_text()
            assert "| 2 |" in content  # Line 2
            assert "| 3 |" in content  # Line 3
