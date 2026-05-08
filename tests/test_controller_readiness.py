#!/usr/bin/env python3
"""Test controller readiness checks and setup (Task 2.2)."""

import os
import sys
import unittest
from unittest.mock import Mock, patch, MagicMock
from pathlib import Path
import tempfile
import time


class FakeCommandRunner:
    """Fake command runner for testing without invoking real serialwrap."""

    def __init__(self):
        self.commands = []
        self.responses = {}
        self.call_count = {}

    def run(self, cmd, **kwargs):
        """Record command and return configured response."""
        self.commands.append(cmd)
        cmd_str = ' '.join(cmd) if isinstance(cmd, list) else cmd

        # Track call count for this command
        self.call_count[cmd_str] = self.call_count.get(cmd_str, 0) + 1

        # Check custom responses first
        for pattern, response in self.responses.items():
            if pattern in cmd_str:
                return response

        # Default responses
        if 'event --help' in cmd_str:
            return (0, "Usage: serialwrap event ...", "")
        elif 'daemon status' in cmd_str:
            return (0, "Daemon: RUNNING\nPID: 12345", "")
        elif 'cmd submit' in cmd_str:
            return (0, "Command submitted", "")

        return (0, "", "")

    def set_response(self, pattern, returncode, stdout, stderr=""):
        """Configure response for commands matching pattern."""
        self.responses[pattern] = (returncode, stdout, stderr)


class TestControllerReadiness(unittest.TestCase):
    """Test controller readiness checks and initialization."""

    def test_check_serialwrap_event_support(self):
        """Test validation of serialwrap event subcommand support."""
        from serialwrap_reboot_test.controller import RebootController

        runner = FakeCommandRunner()
        controller = RebootController("COM0", runner=runner)

        # Should succeed with event --help available
        result = controller.check_serialwrap_event_support()
        self.assertTrue(result)

        # Should fail if event command not supported
        runner.set_response('event --help', 1, "", "Unknown command")
        result = controller.check_serialwrap_event_support()
        self.assertFalse(result)

    def test_check_daemon_status(self):
        """Test serialwrap daemon health check."""
        from serialwrap_reboot_test.controller import RebootController

        runner = FakeCommandRunner()
        controller = RebootController("COM1", runner=runner)

        # Should succeed with daemon running
        result = controller.check_daemon_status()
        self.assertTrue(result)

        # Should fail if daemon not running
        runner.set_response('daemon status', 1, "", "Daemon not running")
        result = controller.check_daemon_status()
        self.assertFalse(result)

    def test_send_marker_command(self):
        """Test sending marker command through serialwrap."""
        from serialwrap_reboot_test.controller import RebootController

        runner = FakeCommandRunner()
        controller = RebootController("COM0", runner=runner)

        marker = controller.send_marker_command()

        # Should return a marker string
        self.assertIn("__SW_REBOOT_TEST_", marker)
        self.assertIn("COM0", marker)

        # Should have submitted command with agent source
        cmd_found = False
        for cmd in runner.commands:
            cmd_str = ' '.join(cmd) if isinstance(cmd, list) else cmd
            if 'cmd submit' in cmd_str and 'agent:reboot-controller' in cmd_str:
                cmd_found = True
                self.assertIn('--timeout', cmd)
                break
        self.assertTrue(cmd_found)

    def test_find_active_minicom_log(self):
        """Test finding active minicom log with marker."""
        from serialwrap_reboot_test.controller import RebootController

        # Create temp directory with test logs
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create an old log (should be ignored)
            old_log = Path(tmpdir) / "mini_COM0_260501-120000.log"
            old_log.write_text("some old content\n")
            # Set old timestamp
            old_time = time.time() - 3600  # 1 hour ago
            os.utime(old_log, (old_time, old_time))

            # Create a recent log without marker (should be ignored)
            recent_no_marker = Path(tmpdir) / "mini_COM0_260506-140000.log"
            recent_no_marker.write_text("recent but no marker\n")

            # Create a recent log with marker (should be found)
            marker = "__SW_REBOOT_TEST_COM0_12345__"
            active_log = Path(tmpdir) / "mini_COM0_260506-152744.log"
            active_log.write_text(f"log content\n{marker}\nmore content\n")

            runner = FakeCommandRunner()
            controller = RebootController("COM0", runner=runner, log_dir=tmpdir)

            found_log = controller.find_active_minicom_log(marker, max_age_seconds=600)

            self.assertIsNotNone(found_log)
            self.assertEqual(found_log.name, "mini_COM0_260506-152744.log")

    def test_find_active_minicom_log_no_match(self):
        """Test finding active minicom log when none match."""
        from serialwrap_reboot_test.controller import RebootController

        with tempfile.TemporaryDirectory() as tmpdir:
            runner = FakeCommandRunner()
            controller = RebootController("COM1", runner=runner, log_dir=tmpdir)

            found_log = controller.find_active_minicom_log("nonexistent_marker")

            self.assertIsNone(found_log)

    def test_find_active_minicom_log_waits_for_delayed_marker(self):
        """Retry briefly when marker appears after command acceptance."""
        from serialwrap_reboot_test.controller import RebootController

        with tempfile.TemporaryDirectory() as tmpdir:
            marker = "__SW_REBOOT_TEST_COM0_DELAYED__"
            active_log = Path(tmpdir) / "mini_COM0_260507-101108.log"
            active_log.write_text("boot log\n")

            runner = FakeCommandRunner()
            controller = RebootController("COM0", runner=runner, log_dir=tmpdir)
            controller.marker_wait_seconds = 2
            controller.marker_poll_interval = 1

            sleep_calls = []
            def fake_sleep(seconds):
                sleep_calls.append(seconds)
                if len(sleep_calls) == 1:
                    with active_log.open("a") as f:
                        f.write(f"{marker}\n")

            controller.sleep_fn = fake_sleep

            found_log = controller.find_active_minicom_log(marker, max_age_seconds=600)

            self.assertEqual(found_log, active_log)
            self.assertGreaterEqual(len(sleep_calls), 1)

    def test_derive_report_path(self):
        """Test deriving report path from minicom log name."""
        from serialwrap_reboot_test.controller import RebootController

        runner = FakeCommandRunner()
        controller = RebootController("COM1", runner=runner)

        minicom_log = Path("/home/user/b-log/mini_COM1_260506-152744.log")
        report_path = controller.derive_report_path(minicom_log)

        self.assertEqual(report_path.name, "event-triggered_COM1_260506-152744.md")
        self.assertEqual(report_path.parent, minicom_log.parent)

    def test_create_run_state_directory(self):
        """Test creating /tmp run-state directory."""
        from serialwrap_reboot_test.controller import RebootController
        import shutil

        runner = FakeCommandRunner()
        controller = RebootController("COM0", runner=runner)

        state_dir = controller.create_run_state_directory()

        # Should be under /tmp with selector and PID
        self.assertTrue(str(state_dir).startswith("/tmp/serialwrap-reboot-test.COM0."))
        self.assertTrue(state_dir.exists())

        # Clean up
        shutil.rmtree(state_dir)

    def test_store_run_state(self):
        """Test storing run state files."""
        from serialwrap_reboot_test.controller import RebootController

        with tempfile.TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir) / "state"
            state_dir.mkdir()

            runner = FakeCommandRunner()
            controller = RebootController("COM1", runner=runner)

            minicom_log = Path("/home/user/b-log/mini_COM1_260506-152744.log")
            report_path = Path("/home/user/b-log/event-triggered_COM1_260506-152744.md")

            controller.store_run_state(state_dir, minicom_log, report_path)

            # Should have stored the paths
            minicom_file = state_dir / "active_minicom_log.txt"
            report_file = state_dir / "report_path.txt"

            self.assertTrue(minicom_file.exists())
            self.assertTrue(report_file.exists())

            self.assertEqual(minicom_file.read_text().strip(), str(minicom_log))
            self.assertEqual(report_file.read_text().strip(), str(report_path))


if __name__ == "__main__":
    unittest.main()
