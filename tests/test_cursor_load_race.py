"""Test for cursor-load-before-lock race condition."""

import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from serialwrap_reboot_test.event_handler import handle_event, load_scan_cursors


class TestCursorLoadRace:
    """Test that cursor load happens inside the lock to prevent races."""

    def test_concurrent_same_event_handlers_with_delay_expose_race(self):
        """Use timing delays to expose the cursor-load-before-lock race."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)

            # Create log with multiple matching lines
            log_path = tmpdir / "mini_COM0_260506-152744.log"
            log_path.write_text(
                "line 1 brcm-therm\n"
                "line 2 brcm-therm\n"
                "line 3 brcm-therm\n"
            )

            # Set up state
            state_dir = tmpdir / "serialwrap-reboot-test.COM0.12345"
            state_dir.mkdir()
            (state_dir / "active_minicom_log.txt").write_text(str(log_path))

            payload = {
                "selector": "COM0",
                "event": "brcm-therm",
                "timestamp": "2026-05-06T16:10:23+08:00"
            }

            # Track which lines are being scanned
            scan_calls = []
            original_open = Path.open

            def delayed_open(self, *args, **kwargs):
                # Add delay to expose race window
                scan_calls.append(("open", self))
                time.sleep(0.01)  # Small delay
                return original_open(self, *args, **kwargs)

            results = []
            errors = []

            def run_handler():
                try:
                    with patch.object(Path, 'open', delayed_open):
                        result = handle_event(payload, state_root=tmpdir, log_dir=tmpdir)
                        results.append(result)
                except Exception as e:
                    errors.append(e)

            # Launch two threads concurrently
            thread1 = threading.Thread(target=run_handler)
            thread2 = threading.Thread(target=run_handler)

            thread1.start()
            thread2.start()
            thread1.join()
            thread2.join()

            # Both should succeed
            assert len(errors) == 0, f"Errors: {errors}"
            assert all(r == 0 for r in results), f"Results: {results}"

            # Check report - should have TWO events, not one duplicated
            report_path = tmpdir / "event-triggered_COM0_260506-152744.md"
            content = report_path.read_text()

            # Count event rows in report
            event_rows = [line for line in content.split('\n') if line.startswith('| mini_COM0')]

            # This test exposes the race - if both threads read cursor before lock,
            # they both see line 1. After fix, this should pass.
            assert len(event_rows) == 2, f"Expected 2 event rows, got {len(event_rows)}: {event_rows}"

            # Should have different lines (line 1 and line 2), not duplicates
            # Before fix: might have "| 1 |" twice
            # After fix: should have "| 1 |" and "| 2 |"
            has_line_1 = "| 1 |" in content
            has_line_2 = "| 2 |" in content

            # At minimum, should have 2 distinct entries
            assert has_line_1, "Should have line 1"
            # This assertion documents the expected behavior after fix
            assert has_line_2, "Should have line 2 (cursor should advance under lock)"

    def test_cursor_load_happens_inside_lock(self):
        """Verify cursor is loaded inside the report lock, not before."""
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

            # Track when cursor is loaded vs when lock is acquired
            events = []

            original_load = load_scan_cursors
            original_locked_file = None

            def tracked_load(cursor_file):
                events.append("cursor_load")
                return original_load(cursor_file)

            import serialwrap_reboot_test.event_handler as handler_module

            class TrackedLock:
                def __init__(self, orig_context):
                    self.orig_context = orig_context

                def __enter__(self):
                    events.append("lock_acquired")
                    return self.orig_context.__enter__()

                def __exit__(self, *args):
                    events.append("lock_released")
                    return self.orig_context.__exit__(*args)

            original_locked_file = handler_module._locked_file

            def tracked_locked_file(lock_path):
                orig_context = original_locked_file(lock_path)
                return TrackedLock(orig_context)

            with patch.object(handler_module, 'load_scan_cursors', tracked_load):
                with patch.object(handler_module, '_locked_file', tracked_locked_file):
                    result = handle_event(payload, state_root=tmpdir, log_dir=tmpdir)

            assert result == 0

            # Verify cursor_load happens AFTER lock_acquired
            if "cursor_load" in events and "lock_acquired" in events:
                cursor_idx = events.index("cursor_load")
                lock_idx = events.index("lock_acquired")
                assert lock_idx < cursor_idx, \
                    f"Cursor should be loaded INSIDE lock. Events: {events}"

    def test_sequential_same_event_handlers_advance_cursor(self):
        """Sequential handlers for same event should advance cursor properly."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)

            log_path = tmpdir / "mini_COM0_260506-152744.log"
            log_path.write_text(
                "line 1 brcm-therm\n"
                "line 2 brcm-therm\n"
                "line 3 brcm-therm\n"
            )

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

            # Second call
            result2 = handle_event(payload, state_root=tmpdir, log_dir=tmpdir)
            assert result2 == 0

            # Check report has both lines
            report_path = tmpdir / "event-triggered_COM0_260506-152744.md"
            content = report_path.read_text()

            assert "| 1 |" in content
            assert "| 2 |" in content
            assert "| brcm-therm | 2 |" in content
