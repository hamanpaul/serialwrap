#!/usr/bin/env python3
"""Focused tests for error handling on critical serialwrap commands."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import sys


class FakeCommandRunner:
    """Fake command runner for testing."""

    def __init__(self):
        self.commands = []
        self.responses = {}

    def run(self, cmd, **kwargs):
        """Record command and return configured response."""
        self.commands.append(cmd)
        cmd_str = ' '.join(cmd) if isinstance(cmd, list) else cmd

        # Check custom responses first
        for pattern, response in self.responses.items():
            if pattern in cmd_str:
                return response

        # Default success
        return (0, "", "")

    def set_response(self, pattern, returncode, stdout, stderr=""):
        """Configure response for commands matching pattern."""
        self.responses[pattern] = (returncode, stdout, stderr)


class TestCriticalCommandErrorHandling(unittest.TestCase):
    """Test error handling for critical serialwrap commands."""

    def test_enable_selector_failure(self):
        """Test enable_selector raises ControllerError on failure."""
        from serialwrap_reboot_test.controller import RebootController, ControllerError

        runner = FakeCommandRunner()
        runner.set_response('event enable', 1, "", "Failed to enable")

        controller = RebootController("COM0", runner=runner)

        with self.assertRaises(ControllerError) as cm:
            controller.enable_selector()

        self.assertIn("enable", str(cm.exception).lower())

    def test_disable_selector_failure(self):
        """Test disable_selector raises ControllerError on failure."""
        from serialwrap_reboot_test.controller import RebootController, ControllerError

        runner = FakeCommandRunner()
        runner.set_response('event disable', 1, "", "Failed to disable")

        controller = RebootController("COM1", runner=runner)

        with self.assertRaises(ControllerError) as cm:
            controller.disable_selector()

        self.assertIn("disable", str(cm.exception).lower())

    def test_reset_selector_failure(self):
        """Test reset_selector raises ControllerError on failure."""
        from serialwrap_reboot_test.controller import RebootController, ControllerError

        runner = FakeCommandRunner()
        runner.set_response('event reset', 1, "", "Failed to reset")

        controller = RebootController("COM0", runner=runner)

        with self.assertRaises(ControllerError) as cm:
            controller.reset_selector()

        self.assertIn("reset", str(cm.exception).lower())

    def test_remove_event_rules_failure(self):
        """Test remove_event_rules raises ControllerError on failure."""
        from serialwrap_reboot_test.controller import RebootController, ControllerError

        runner = FakeCommandRunner()
        runner.set_response('event rm', 1, "", "Failed to remove rule")

        controller = RebootController("COM1", runner=runner)

        with self.assertRaises(ControllerError) as cm:
            controller.remove_event_rules()

        self.assertIn("remove", str(cm.exception).lower())

    def test_run_session_recover_failure(self):
        """Test run_session_recover raises ControllerError on failure."""
        from serialwrap_reboot_test.controller import RebootController, ControllerError

        runner = FakeCommandRunner()
        runner.set_response('session recover', 1, "", "Failed to recover")

        controller = RebootController("COM0", runner=runner)

        with self.assertRaises(ControllerError) as cm:
            controller.run_session_recover()

        self.assertIn("recover", str(cm.exception).lower())

    def test_run_session_recover_failure_uses_stdout_error_payload(self):
        """Test recover failure surfaces JSON stdout error details when stderr is empty."""
        from serialwrap_reboot_test.controller import RebootController, ControllerError

        runner = FakeCommandRunner()
        runner.set_response(
            'session recover',
            2,
            json.dumps({"ok": False, "error_code": "TIMEOUT"}),
            "",
        )

        controller = RebootController("COM0", runner=runner)

        with self.assertRaises(ControllerError) as cm:
            controller.run_session_recover()

        self.assertIn("TIMEOUT", str(cm.exception))

    def test_send_raw_broker_command_failure(self):
        """Test send_raw_broker_command raises ControllerError on failure."""
        from serialwrap_reboot_test.controller import RebootController, ControllerError

        runner = FakeCommandRunner()
        runner.set_response('session interactive-open', 0, json.dumps({"interactive_id": "agent-int"}))
        runner.set_response('session interactive-send', 1, "", "Failed to send raw command")
        runner.set_response('session interactive-close', 0, json.dumps({"ok": True}))

        controller = RebootController("COM1", runner=runner)

        with self.assertRaises(ControllerError) as cm:
            controller.send_raw_broker_command("reset")

        self.assertIn("broker", str(cm.exception).lower())
        self.assertTrue(any('session interactive-close' in ' '.join(cmd) for cmd in runner.commands))

    def test_cleanup_continues_despite_disable_failure(self):
        """Test cleanup removes state dir even if disable fails."""
        from serialwrap_reboot_test.controller import RebootController

        with tempfile.TemporaryDirectory() as tmpdir:
            runner = FakeCommandRunner()
            runner.set_response('event disable', 1, "", "Failed to disable")

            controller = RebootController("COM0", runner=runner)

            # Create state directory
            state_dir = Path(tmpdir) / "state"
            state_dir.mkdir()
            controller.state_dir = state_dir

            self.assertTrue(state_dir.exists())

            # Cleanup should attempt disable/reset, but still remove state
            # Should not raise despite disable failure
            controller.cleanup()

            # State directory should be removed
            self.assertFalse(state_dir.exists())

    def test_cleanup_continues_despite_reset_failure(self):
        """Test cleanup removes state dir even if reset fails."""
        from serialwrap_reboot_test.controller import RebootController

        with tempfile.TemporaryDirectory() as tmpdir:
            runner = FakeCommandRunner()
            runner.set_response('event reset', 1, "", "Failed to reset")
            runner.set_response('event status', 0,
                json.dumps({"selectors": {"COM0": {"enabled": False}}}))

            controller = RebootController("COM1", runner=runner)

            # Create state directory
            state_dir = Path(tmpdir) / "state"
            state_dir.mkdir()
            controller.state_dir = state_dir

            self.assertTrue(state_dir.exists())

            # Cleanup should attempt disable/reset, but still remove state
            controller.cleanup()

            # State directory should be removed
            self.assertFalse(state_dir.exists())

    def test_cleanup_continues_despite_rule_removal_failure(self):
        """Test cleanup removes state dir even if rule removal fails."""
        from serialwrap_reboot_test.controller import RebootController

        with tempfile.TemporaryDirectory() as tmpdir:
            runner = FakeCommandRunner()
            runner.set_response('event rm', 1, "", "Failed to remove rule")
            runner.set_response('event status', 0,
                json.dumps({"selectors": {"COM0": {"enabled": False}}}))

            controller = RebootController("COM0", runner=runner)

            # Create state directory
            state_dir = Path(tmpdir) / "state"
            state_dir.mkdir()
            controller.state_dir = state_dir

            self.assertTrue(state_dir.exists())

            # Cleanup should still remove state
            controller.cleanup()

            # State directory should be removed
            self.assertFalse(state_dir.exists())

    def test_run_loop_handles_session_recover_failure(self):
        """Test run_loop handles session recover failure gracefully."""
        from serialwrap_reboot_test.controller import RebootController

        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = Path(tmpdir) / "mini_COM0_test.log"
            log_file.write_text("boot log\n")

            runner = FakeCommandRunner()
            runner.set_response('session list', 0,
                json.dumps({"sessions": [{"selector": "COM0", "state": "RECOVERY"}]}))
            runner.set_response('session recover', 1, "", "Failed to recover")

            controller = RebootController("COM0", runner=runner, log_dir=tmpdir)
            controller.active_log_path = log_file
            controller.last_action_time = None  # Trigger recovery

            # Mock sleep and stderr
            sleep_called = []
            controller.sleep_fn = lambda s: sleep_called.append(s)

            stderr_output = []
            original_stderr = sys.stderr.write
            def mock_stderr(msg):
                stderr_output.append(msg)
                return original_stderr(msg)

            with patch('sys.stderr.write', mock_stderr):
                # Should not raise, should handle gracefully
                controller.run_loop()

            # Should have slept (error handling)
            self.assertTrue(len(sleep_called) > 0)

            # Should have printed error
            self.assertTrue(any('ERROR' in msg or 'recover' in msg for msg in stderr_output))

    def test_run_loop_handles_raw_reset_failure(self):
        """Test run_loop handles raw reset failure gracefully."""
        from serialwrap_reboot_test.controller import RebootController

        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = Path(tmpdir) / "mini_COM1_test.log"
            log_file.write_text("boot log\n=>\n")

            runner = FakeCommandRunner()
            runner.set_response('session list', 0,
                json.dumps({"sessions": [{"selector": "COM1", "state": "RECOVERY"}]}))
            runner.set_response('session interactive-open', 0, json.dumps({"interactive_id": "agent-int"}))
            runner.set_response('session interactive-send', 1, "", "Failed to send raw command")

            controller = RebootController("COM1", runner=runner, log_dir=tmpdir)
            controller.active_log_path = log_file
            controller.last_action_time = None  # Force past throttle

            # Mock sleep
            sleep_called = []
            controller.sleep_fn = lambda s: sleep_called.append(s)

            stderr_output = []
            original_stderr = sys.stderr.write
            def mock_stderr(msg):
                stderr_output.append(msg)
                return original_stderr(msg)

            with patch('sys.stderr.write', mock_stderr):
                # Should not raise, should handle gracefully
                controller.run_loop()

            # Should have slept
            self.assertTrue(len(sleep_called) > 0)

            # Should have printed error
            self.assertTrue(any('ERROR' in msg or 'broker' in msg for msg in stderr_output))

    def test_run_loop_handles_raw_reboot_failure(self):
        """Test run_loop handles raw reboot failure gracefully."""
        from serialwrap_reboot_test.controller import RebootController

        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = Path(tmpdir) / "mini_COM0_test.log"
            log_file.write_text("boot log\nroot@prplOS:/# \n")

            runner = FakeCommandRunner()
            runner.set_response('session list', 0,
                json.dumps({"sessions": [{"selector": "COM0", "state": "RECOVERY"}]}))
            runner.set_response('session interactive-open', 0, json.dumps({"interactive_id": "agent-int"}))
            runner.set_response('session interactive-send', 1, "", "Failed to send raw reboot")

            controller = RebootController("COM0", runner=runner, log_dir=tmpdir)
            controller.active_log_path = log_file
            controller.last_action_time = None

            # Mock sleep
            sleep_called = []
            controller.sleep_fn = lambda s: sleep_called.append(s)

            stderr_output = []
            original_stderr = sys.stderr.write
            def mock_stderr(msg):
                stderr_output.append(msg)
                return original_stderr(msg)

            with patch('sys.stderr.write', mock_stderr):
                # Should not raise
                controller.run_loop()

            # Should have slept
            self.assertTrue(len(sleep_called) > 0)

            # Should have printed error
            self.assertTrue(any('ERROR' in msg or 'broker' in msg or 'reboot' in msg for msg in stderr_output))


if __name__ == "__main__":
    unittest.main()
