#!/usr/bin/env python3
"""Security and robustness fix tests for Task 2 final review."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import sys


class FakeCommandRunner:
    """Fake command runner for testing."""

    def __init__(self):
        self.commands = []

    def run(self, cmd, **kwargs):
        """Record command and return success."""
        self.commands.append(cmd)

        cmd_str = ' '.join(cmd) if isinstance(cmd, list) else cmd

        if 'event --help' in cmd_str:
            return (0, "Usage: serialwrap event", "")
        elif 'daemon status' in cmd_str:
            return (0, "Daemon running", "")
        elif 'event status' in cmd_str:
            return (0, json.dumps({"selectors": {}}), "")

        return (0, "", "")


class TestCleanupErrorHandling(unittest.TestCase):
    """Test cleanup handles OSError from state directory removal."""

    def test_cleanup_logs_warning_on_state_dir_removal_failure(self):
        """Test cleanup logs warning and doesn't raise when state dir removal fails."""
        from serialwrap_reboot_test.controller import RebootController

        with tempfile.TemporaryDirectory() as tmpdir:
            runner = FakeCommandRunner()
            controller = RebootController("COM0", runner=runner, log_dir=tmpdir)

            # Create a state directory that will fail to remove
            state_dir = Path(tmpdir) / "fake_state"
            state_dir.mkdir()
            controller.state_dir = state_dir

            # Make it non-removable by creating a file with no permissions
            bad_file = state_dir / "locked"
            bad_file.write_text("test")
            bad_file.chmod(0o000)
            state_dir.chmod(0o000)

            # Capture stderr
            stderr_output = []
            original_stderr = sys.stderr.write
            def mock_stderr(msg):
                stderr_output.append(msg)
                return original_stderr(msg)

            try:
                with patch('sys.stderr.write', mock_stderr):
                    # Should not raise
                    controller.cleanup()

                # Should have logged warning
                stderr_text = ''.join(stderr_output)
                self.assertTrue('WARNING' in stderr_text or 'state' in stderr_text.lower())
            finally:
                # Restore permissions for cleanup
                try:
                    state_dir.chmod(0o755)
                    bad_file.chmod(0o644)
                    bad_file.unlink()
                    state_dir.rmdir()
                except:
                    pass


class TestSelectorValidation(unittest.TestCase):
    """Test selector validation against path traversal."""

    def test_parse_args_rejects_path_traversal_parent(self):
        """Test parse_args rejects selector with parent directory traversal."""
        from serialwrap_reboot_test.controller import parse_args

        with self.assertRaises(SystemExit):
            parse_args(["--selector", "../COM0"])

    def test_parse_args_rejects_path_traversal_slash(self):
        """Test parse_args rejects selector with forward slash."""
        from serialwrap_reboot_test.controller import parse_args

        with self.assertRaises(SystemExit):
            parse_args(["--selector", "COM0/evil"])

    def test_parse_args_rejects_deep_path_traversal(self):
        """Test parse_args rejects deep path traversal."""
        from serialwrap_reboot_test.controller import parse_args

        with self.assertRaises(SystemExit):
            parse_args(["--selector", "../../../root"])

    def test_parse_args_rejects_empty_selector(self):
        """Test parse_args rejects empty selector."""
        from serialwrap_reboot_test.controller import parse_args

        with self.assertRaises(SystemExit):
            parse_args(["--selector", ""])

    def test_parse_args_rejects_non_com_selector(self):
        """Test parse_args rejects non-COM selector."""
        from serialwrap_reboot_test.controller import parse_args

        with self.assertRaises(SystemExit):
            parse_args(["--selector", "EVIL123"])

    def test_parse_args_accepts_valid_com0(self):
        """Test parse_args accepts valid COM0."""
        from serialwrap_reboot_test.controller import parse_args

        args = parse_args(["--selector", "COM0"])
        self.assertEqual(args.selector, "COM0")

    def test_parse_args_accepts_valid_com1(self):
        """Test parse_args accepts valid COM1."""
        from serialwrap_reboot_test.controller import parse_args

        args = parse_args(["--selector", "COM1"])
        self.assertEqual(args.selector, "COM1")

    def test_parse_args_accepts_valid_com2(self):
        """Test parse_args accepts future COM2."""
        from serialwrap_reboot_test.controller import parse_args

        args = parse_args(["--selector", "COM2"])
        self.assertEqual(args.selector, "COM2")

    def test_controller_init_rejects_invalid_selector(self):
        """Test RebootController.__init__ rejects invalid selector."""
        from serialwrap_reboot_test.controller import RebootController

        runner = FakeCommandRunner()

        with self.assertRaises(ValueError):
            RebootController("../COM0", runner=runner)

    def test_controller_init_accepts_valid_selector(self):
        """Test RebootController.__init__ accepts valid selector."""
        from serialwrap_reboot_test.controller import RebootController

        runner = FakeCommandRunner()
        controller = RebootController("COM0", runner=runner)
        self.assertEqual(controller.selector, "COM0")


class TestStateDirPathSafety(unittest.TestCase):
    """Test state directory path safety checks."""

    def test_create_run_state_directory_validates_path_under_tmp(self):
        """Test create_run_state_directory validates resolved path is under /tmp."""
        from serialwrap_reboot_test.controller import RebootController

        with tempfile.TemporaryDirectory() as tmpdir:
            runner = FakeCommandRunner()

            # Even with valid selector, if symlink shenanigans happen, should detect
            controller = RebootController("COM0", runner=runner, log_dir=tmpdir)

            # This should work normally
            state_dir = controller.create_run_state_directory()
            self.assertTrue(str(state_dir).startswith("/tmp/"))

            # Clean up
            import shutil
            shutil.rmtree(state_dir)


if __name__ == "__main__":
    unittest.main()
