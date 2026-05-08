#!/usr/bin/env python3
"""Test controller cleanup and signal handling (Task 2.5)."""

import os
import signal
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock
import json


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

        # Track call count
        self.call_count[cmd_str] = self.call_count.get(cmd_str, 0) + 1

        # Check custom responses first
        for pattern, response in self.responses.items():
            if pattern in cmd_str:
                return response

        # Default responses
        return (0, "", "")

    def set_response(self, pattern, returncode, stdout, stderr=""):
        """Configure response for commands matching pattern."""
        self.responses[pattern] = (returncode, stdout, stderr)


class TestControllerCleanup(unittest.TestCase):
    """Test controller cleanup and signal handling."""

    def test_cleanup_disables_selector(self):
        """Test cleanup disables the controller's selector."""
        from serialwrap_reboot_test.controller import RebootController

        runner = FakeCommandRunner()
        controller = RebootController("COM0", runner=runner)

        controller.cleanup()

        # Should have called disable
        disable_found = False
        for cmd in runner.commands:
            cmd_str = ' '.join(cmd)
            if 'event' in cmd_str and 'disable' in cmd_str and 'COM0' in cmd_str:
                disable_found = True
                break
        self.assertTrue(disable_found)

    def test_cleanup_resets_selector(self):
        """Test cleanup resets the controller's selector."""
        from serialwrap_reboot_test.controller import RebootController

        runner = FakeCommandRunner()
        controller = RebootController("COM1", runner=runner)

        controller.cleanup()

        # Should have called reset
        reset_found = False
        for cmd in runner.commands:
            cmd_str = ' '.join(cmd)
            if 'event' in cmd_str and 'reset' in cmd_str and 'COM1' in cmd_str:
                reset_found = True
                break
        self.assertTrue(reset_found)

    def test_cleanup_removes_rules_when_last_active(self):
        """Test cleanup removes rules when this is the last active selector."""
        from serialwrap_reboot_test.controller import RebootController

        runner = FakeCommandRunner()

        # Simulate no other selectors enabled
        status_output = json.dumps({
            "selectors": {
                "COM0": {"enabled": False},
                "COM1": {"enabled": False}
            }
        })
        runner.set_response('event status', 0, status_output)

        controller = RebootController("COM0", runner=runner)
        controller.cleanup()

        # Should have removed rules
        rm_commands = [cmd for cmd in runner.commands if 'event' in ' '.join(cmd) and 'rm' in ' '.join(cmd)]
        self.assertEqual(len(rm_commands), 5)

    def test_cleanup_keeps_rules_when_other_active(self):
        """Test cleanup keeps rules when other selector is still active."""
        from serialwrap_reboot_test.controller import RebootController

        runner = FakeCommandRunner()

        # Simulate COM1 still enabled
        status_output = json.dumps({
            "selectors": {
                "COM0": {"enabled": False},
                "COM1": {"enabled": True}
            }
        })
        runner.set_response('event status', 0, status_output)

        controller = RebootController("COM0", runner=runner)
        controller.cleanup()

        # Should NOT have removed rules
        rm_commands = [cmd for cmd in runner.commands if 'event' in ' '.join(cmd) and 'rm' in ' '.join(cmd)]
        self.assertEqual(len(rm_commands), 0)

    def test_cleanup_removes_tmp_state_directory(self):
        """Test cleanup removes /tmp state directory."""
        from serialwrap_reboot_test.controller import RebootController

        with tempfile.TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir) / "state"
            state_dir.mkdir()

            # Create some files
            (state_dir / "test.txt").write_text("data")

            runner = FakeCommandRunner()
            controller = RebootController("COM1", runner=runner)
            controller.state_dir = state_dir

            self.assertTrue(state_dir.exists())

            controller.cleanup()

            # Directory should be removed
            self.assertFalse(state_dir.exists())

    def test_register_signal_handlers(self):
        """Test registering SIGINT and SIGTERM handlers."""
        from serialwrap_reboot_test.controller import RebootController

        runner = FakeCommandRunner()
        controller = RebootController("COM0", runner=runner)

        # Mock signal.signal to track calls
        original_signal = signal.signal
        signal_calls = []

        def mock_signal(signum, handler):
            signal_calls.append((signum, handler))
            return original_signal(signum, handler)

        with patch('signal.signal', side_effect=mock_signal):
            controller.register_signal_handlers()

        # Should have registered handlers for SIGINT and SIGTERM
        signal_nums = [s[0] for s in signal_calls]
        self.assertIn(signal.SIGINT, signal_nums)
        self.assertIn(signal.SIGTERM, signal_nums)

    def test_signal_handler_calls_cleanup(self):
        """Test signal handler triggers cleanup."""
        from serialwrap_reboot_test.controller import RebootController

        runner = FakeCommandRunner()
        controller = RebootController("COM1", runner=runner)

        # Track cleanup calls
        cleanup_called = False
        original_cleanup = controller.cleanup

        def mock_cleanup():
            nonlocal cleanup_called
            cleanup_called = True
            # Don't call original to avoid side effects

        controller.cleanup = mock_cleanup
        controller.register_signal_handlers()

        # Simulate receiving SIGINT - expect SystemExit
        handler = controller._signal_handler
        with self.assertRaises(SystemExit) as cm:
            handler(signal.SIGINT, None)

        # Cleanup should have been called
        self.assertTrue(cleanup_called)
        self.assertEqual(cm.exception.code, 0)

    def test_normal_exit_cleanup(self):
        """Test cleanup is called on normal exit."""
        from serialwrap_reboot_test.controller import RebootController

        runner = FakeCommandRunner()
        controller = RebootController("COM0", runner=runner)

        # Track cleanup calls
        cleanup_called = False

        def mock_cleanup():
            nonlocal cleanup_called
            cleanup_called = True

        controller.cleanup = mock_cleanup

        # Simulate normal exit
        controller.exit_gracefully()

        # Cleanup should have been called
        self.assertTrue(cleanup_called)

    def test_stop_condition_hours_limit(self):
        """Test stop condition with hours limit."""
        from serialwrap_reboot_test.controller import RebootController
        import time

        runner = FakeCommandRunner()
        controller = RebootController("COM0", runner=runner, hours_limit=1)

        # Set start time to 2 hours ago
        controller.start_time = time.time() - 7200

        result = controller.should_stop()

        self.assertTrue(result)

    def test_stop_condition_count_limit(self):
        """Test stop condition with count limit."""
        from serialwrap_reboot_test.controller import RebootController

        runner = FakeCommandRunner()
        controller = RebootController("COM1", runner=runner, count_limit=5)

        # Set reboot count to 5
        controller.reboot_count = 5

        result = controller.should_stop()

        self.assertTrue(result)

    def test_stop_condition_infinite_run(self):
        """Test infinite run behavior (no stop conditions)."""
        from serialwrap_reboot_test.controller import RebootController
        import time

        runner = FakeCommandRunner()
        controller = RebootController("COM0", runner=runner)

        # No limits set, should never stop
        controller.start_time = time.time() - 86400  # 1 day ago
        controller.reboot_count = 1000

        result = controller.should_stop()

        self.assertFalse(result)


if __name__ == "__main__":
    unittest.main()
