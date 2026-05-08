#!/usr/bin/env python3
"""Test startup error handling and cleanup."""

import json
import tempfile
import unittest
from pathlib import Path


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
        if 'event --help' in cmd_str:
            return (0, "Usage: serialwrap event", "")
        elif 'daemon status' in cmd_str:
            return (0, "Daemon running", "")
        elif 'cmd submit' in cmd_str:
            return (0, "", "")
        elif 'event add' in cmd_str:
            return (0, "", "")
        elif 'event enable' in cmd_str:
            return (0, "", "")
        elif 'event disable' in cmd_str:
            return (0, "", "")
        elif 'event reset' in cmd_str:
            return (0, "", "")
        elif 'event status' in cmd_str:
            return (0, json.dumps({"selectors": {"COM0": {"enabled": False}}}), "")
        elif 'event rm' in cmd_str:
            return (0, "", "")

        return (0, "", "")

    def set_response(self, pattern, returncode, stdout, stderr=""):
        """Configure response for commands matching pattern."""
        self.responses[pattern] = (returncode, stdout, stderr)


class TestStartupCleanupOnFailure(unittest.TestCase):
    """Test startup cleans up properly on failure."""

    def test_startup_cleans_state_on_enable_failure(self):
        """Test startup removes state dir if enable fails after creation."""
        from serialwrap_reboot_test.controller import RebootController

        with tempfile.TemporaryDirectory() as tmpdir:
            runner = FakeCommandRunner()

            # Make enable fail
            runner.set_response('event enable', 1, "", "Failed to enable")

            controller = RebootController("COM0", runner=runner, log_dir=tmpdir)

            # Intercept marker to create log
            original_send_marker = controller.send_marker_command
            def tracked_send_marker():
                marker = original_send_marker()
                log_file = Path(tmpdir) / "mini_COM0_test.log"
                log_file.write_text(f"boot log\n{marker}\n")
                return marker
            controller.send_marker_command = tracked_send_marker

            # Run startup
            result = controller.startup()

            # Should return False
            self.assertFalse(result)

            # State directory should be cleaned up (not left behind)
            if controller.state_dir:
                self.assertFalse(controller.state_dir.exists(),
                    "State directory should be removed on startup failure")

    def test_startup_cleans_rules_on_enable_failure(self):
        """Test startup cleans rules if enable fails after registration."""
        from serialwrap_reboot_test.controller import RebootController

        with tempfile.TemporaryDirectory() as tmpdir:
            runner = FakeCommandRunner()

            # Make enable fail
            runner.set_response('event enable', 1, "", "Failed to enable")

            controller = RebootController("COM1", runner=runner, log_dir=tmpdir)

            # Intercept marker to create log
            original_send_marker = controller.send_marker_command
            def tracked_send_marker():
                marker = original_send_marker()
                log_file = Path(tmpdir) / "mini_COM1_test.log"
                log_file.write_text(f"boot log\n{marker}\n")
                return marker
            controller.send_marker_command = tracked_send_marker

            # Run startup
            result = controller.startup()

            # Should return False
            self.assertFalse(result)

            # Should have attempted to remove rules (cleanup)
            rm_commands = [cmd for cmd in runner.commands
                          if 'event' in ' '.join(cmd) and 'rm' in ' '.join(cmd)]
            self.assertTrue(len(rm_commands) > 0,
                "Should attempt to clean up rules on startup failure")

    def test_main_returns_nonzero_on_enable_failure(self):
        """Test main returns non-zero when enable fails during startup."""
        from serialwrap_reboot_test.controller import main_with_runner

        with tempfile.TemporaryDirectory() as tmpdir:
            runner = FakeCommandRunner()

            # Make enable fail
            runner.set_response('event enable', 1, "", "Failed to enable")

            # Intercept marker command
            marker_created = []
            original_run = runner.run
            def intercepting_run(cmd, **kwargs):
                result = original_run(cmd, **kwargs)
                cmd_str = ' '.join(cmd)
                if 'cmd submit' in cmd_str and '__SW_REBOOT_TEST_' in cmd_str:
                    for part in cmd:
                        if '__SW_REBOOT_TEST_' in part:
                            marker = part.replace('echo ', '').strip()
                            if marker not in marker_created:
                                marker_created.append(marker)
                                log_file = Path(tmpdir) / "mini_COM0_test.log"
                                log_file.write_text(f"boot log\n{marker}\n")
                            break
                return result
            runner.run = intercepting_run

            # Run main
            exit_code = main_with_runner(
                ["--selector", "COM0"],
                runner=runner,
                log_dir=tmpdir
            )

            # Should return non-zero
            self.assertNotEqual(exit_code, 0)

    def test_startup_cleans_state_on_any_post_creation_failure(self):
        """Test startup cleans state on any failure after state creation."""
        from serialwrap_reboot_test.controller import RebootController

        with tempfile.TemporaryDirectory() as tmpdir:
            runner = FakeCommandRunner()

            # Make event add fail (after state dir created)
            runner.set_response('event add', 1, "", "Failed to add rule")

            controller = RebootController("COM0", runner=runner, log_dir=tmpdir)

            # Intercept marker to create log
            original_send_marker = controller.send_marker_command
            def tracked_send_marker():
                marker = original_send_marker()
                log_file = Path(tmpdir) / "mini_COM0_test.log"
                log_file.write_text(f"boot log\n{marker}\n")
                return marker
            controller.send_marker_command = tracked_send_marker

            # Run startup
            result = controller.startup()

            # Should return False
            self.assertFalse(result)

            # State directory should be cleaned up
            if controller.state_dir:
                self.assertFalse(controller.state_dir.exists(),
                    "State directory should be removed on any startup failure")


class TestStartupCleanupErrorLogging(unittest.TestCase):
    """Test startup cleanup logs warnings on cleanup failures."""

    def test_startup_cleanup_logs_warnings_on_disable_failure(self):
        """Test startup cleanup logs warning when disable fails."""
        from serialwrap_reboot_test.controller import RebootController
        from unittest.mock import patch
        import sys

        with tempfile.TemporaryDirectory() as tmpdir:
            runner = FakeCommandRunner()

            # Make enable fail (triggers cleanup)
            runner.set_response('event enable', 1, "", "Failed to enable")
            # Make disable fail during cleanup
            runner.set_response('event disable', 1, "", "Failed to disable during cleanup")

            controller = RebootController("COM0", runner=runner, log_dir=tmpdir)

            # Intercept marker to create log
            original_send_marker = controller.send_marker_command
            def tracked_send_marker():
                marker = original_send_marker()
                log_file = Path(tmpdir) / "mini_COM0_test.log"
                log_file.write_text(f"boot log\n{marker}\n")
                return marker
            controller.send_marker_command = tracked_send_marker

            # Capture stderr
            stderr_output = []
            original_stderr = sys.stderr.write
            def mock_stderr(msg):
                stderr_output.append(msg)
                return original_stderr(msg)

            with patch('sys.stderr.write', mock_stderr):
                result = controller.startup()

            # Should return False
            self.assertFalse(result)

            # Should log warning about disable failure
            stderr_text = ''.join(stderr_output)
            self.assertTrue('WARNING' in stderr_text or 'disable' in stderr_text.lower(),
                f"Expected warning about disable failure, got: {stderr_text}")

            # State directory should still be removed
            if controller.state_dir:
                self.assertFalse(controller.state_dir.exists())

    def test_startup_cleanup_logs_warnings_on_reset_failure(self):
        """Test startup cleanup logs warning when reset fails."""
        from serialwrap_reboot_test.controller import RebootController
        from unittest.mock import patch
        import sys

        with tempfile.TemporaryDirectory() as tmpdir:
            runner = FakeCommandRunner()

            # Make enable fail (triggers cleanup)
            runner.set_response('event enable', 1, "", "Failed to enable")
            # Make reset fail during cleanup
            runner.set_response('event reset', 1, "", "Failed to reset during cleanup")

            controller = RebootController("COM1", runner=runner, log_dir=tmpdir)

            # Intercept marker to create log
            original_send_marker = controller.send_marker_command
            def tracked_send_marker():
                marker = original_send_marker()
                log_file = Path(tmpdir) / "mini_COM1_test.log"
                log_file.write_text(f"boot log\n{marker}\n")
                return marker
            controller.send_marker_command = tracked_send_marker

            # Capture stderr
            stderr_output = []
            original_stderr = sys.stderr.write
            def mock_stderr(msg):
                stderr_output.append(msg)
                return original_stderr(msg)

            with patch('sys.stderr.write', mock_stderr):
                result = controller.startup()

            # Should return False
            self.assertFalse(result)

            # Should log warning about reset failure
            stderr_text = ''.join(stderr_output)
            self.assertTrue('WARNING' in stderr_text or 'reset' in stderr_text.lower(),
                f"Expected warning about reset failure, got: {stderr_text}")

            # State directory should still be removed
            if controller.state_dir:
                self.assertFalse(controller.state_dir.exists())

    def test_startup_cleanup_logs_warnings_on_rule_removal_failure(self):
        """Test startup cleanup logs warning when rule removal fails."""
        from serialwrap_reboot_test.controller import RebootController
        from unittest.mock import patch
        import sys

        with tempfile.TemporaryDirectory() as tmpdir:
            runner = FakeCommandRunner()

            # Make enable fail (triggers cleanup)
            runner.set_response('event enable', 1, "", "Failed to enable")
            # Make event rm fail during cleanup
            runner.set_response('event rm', 1, "", "Failed to remove rule")

            controller = RebootController("COM0", runner=runner, log_dir=tmpdir)

            # Intercept marker to create log
            original_send_marker = controller.send_marker_command
            def tracked_send_marker():
                marker = original_send_marker()
                log_file = Path(tmpdir) / "mini_COM0_test.log"
                log_file.write_text(f"boot log\n{marker}\n")
                return marker
            controller.send_marker_command = tracked_send_marker

            # Capture stderr
            stderr_output = []
            original_stderr = sys.stderr.write
            def mock_stderr(msg):
                stderr_output.append(msg)
                return original_stderr(msg)

            with patch('sys.stderr.write', mock_stderr):
                result = controller.startup()

            # Should return False
            self.assertFalse(result)

            # Should log warning about rule removal failure
            stderr_text = ''.join(stderr_output)
            self.assertTrue('WARNING' in stderr_text or 'remove' in stderr_text.lower() or 'rule' in stderr_text.lower(),
                f"Expected warning about rule removal failure, got: {stderr_text}")

            # State directory should still be removed
            if controller.state_dir:
                self.assertFalse(controller.state_dir.exists())


if __name__ == "__main__":
    unittest.main()
