#!/usr/bin/env python3
"""Quality fix tests for Task 2 review issues."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock, mock_open
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

        for pattern, response in self.responses.items():
            if pattern in cmd_str:
                return response

        # Defaults
        if 'session list' in cmd_str:
            return (0, json.dumps({"sessions": [{"selector": "COM0", "state": "READY"}]}), "")
        elif 'session self-test' in cmd_str:
            return (0, json.dumps({"classification": "OK", "probe_ok": True}), "")
        elif 'cmd submit' in cmd_str:
            return (0, "", "")

        return (0, "", "")

    def set_response(self, pattern, returncode, stdout, stderr=""):
        """Configure response for commands matching pattern."""
        self.responses[pattern] = (returncode, stdout, stderr)


class TestRunLoopSleepBehavior(unittest.TestCase):
    """Test run_loop sleeps after successful normal reboot."""

    def test_run_loop_sleeps_after_successful_normal_reboot(self):
        """Test run_loop sleeps after successful normal reboot, not just on error."""
        from serialwrap_reboot_test.controller import RebootController

        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = Path(tmpdir) / "mini_COM0_test.log"
            log_file.write_text("boot log\n")

            runner = FakeCommandRunner()
            controller = RebootController("COM0", runner=runner, log_dir=tmpdir)
            controller.active_log_path = log_file

            # Mock sleep to verify it's called
            sleep_called = []
            controller.sleep_fn = lambda s: sleep_called.append(s)

            # Run one loop (should submit normal reboot)
            controller.run_loop()

            # Should have submitted reboot successfully
            self.assertEqual(controller.reboot_count, 1)

            # Should have slept after successful reboot
            self.assertTrue(len(sleep_called) > 0, "Should sleep after successful normal reboot")


class TestKeyboardInterruptCleanup(unittest.TestCase):
    """Test KeyboardInterrupt triggers cleanup."""

    def test_main_with_runner_cleanup_on_keyboard_interrupt(self):
        """Test main_with_runner runs cleanup when KeyboardInterrupt raised."""
        from serialwrap_reboot_test.controller import main_with_runner

        with tempfile.TemporaryDirectory() as tmpdir:
            runner = FakeCommandRunner()

            # Create log file
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

            # Patch run_loop to raise KeyboardInterrupt
            from serialwrap_reboot_test import controller as controller_module
            original_reboot_controller = controller_module.RebootController

            cleanup_called = []

            class InterruptingController(original_reboot_controller):
                def run_loop(self):
                    # Raise KeyboardInterrupt after first call
                    raise KeyboardInterrupt("Test interrupt")

                def cleanup(self):
                    cleanup_called.append(True)
                    super().cleanup()

            with patch.object(controller_module, 'RebootController', InterruptingController):
                exit_code = main_with_runner(
                    ["--selector", "COM0"],
                    runner=runner,
                    log_dir=tmpdir
                )

            # Should have called cleanup
            self.assertTrue(len(cleanup_called) > 0, "Cleanup should be called on KeyboardInterrupt")

            # Exit code should be 0 (graceful interrupt)
            self.assertEqual(exit_code, 0)

    def test_main_with_runner_cleanup_on_runtime_error(self):
        """Test main_with_runner runs cleanup when run_loop raises unexpectedly."""
        from serialwrap_reboot_test.controller import main_with_runner

        with tempfile.TemporaryDirectory() as tmpdir:
            runner = FakeCommandRunner()

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

            from serialwrap_reboot_test import controller as controller_module
            original_reboot_controller = controller_module.RebootController

            cleanup_called = []

            class FailingController(original_reboot_controller):
                def run_loop(self):
                    raise RuntimeError("unexpected loop failure")

                def cleanup(self):
                    cleanup_called.append(True)
                    super().cleanup()

            with patch.object(controller_module, 'RebootController', FailingController):
                with self.assertRaises(RuntimeError):
                    main_with_runner(
                        ["--selector", "COM0"],
                        runner=runner,
                        log_dir=tmpdir
                    )

            self.assertTrue(cleanup_called, "Cleanup should run before propagating runtime errors")


class TestMemoryEfficientLogReading(unittest.TestCase):
    """Test log reading methods don't load entire files into memory."""

    def test_find_active_minicom_log_does_not_use_read_text(self):
        """Test find_active_minicom_log doesn't call Path.read_text() for marker search."""
        from serialwrap_reboot_test.controller import RebootController

        with tempfile.TemporaryDirectory() as tmpdir:
            runner = FakeCommandRunner()
            controller = RebootController("COM0", runner=runner, log_dir=tmpdir)

            # Create a log file
            log_file = Path(tmpdir) / "mini_COM0_test.log"
            log_file.write_text("line1\nMARKER_TEST\nline3\n")

            # Patch Path.read_text to detect if it's called
            read_text_called = []
            original_read_text = Path.read_text

            def tracking_read_text(self, *args, **kwargs):
                read_text_called.append(str(self))
                return original_read_text(self, *args, **kwargs)

            with patch.object(Path, 'read_text', tracking_read_text):
                result = controller.find_active_minicom_log("MARKER_TEST", max_age_seconds=600)

            # Should not have called read_text (streaming instead)
            self.assertEqual(len(read_text_called), 0,
                f"Should not call read_text for marker search, but called on: {read_text_called}")

    def test_check_log_tail_for_prompt_does_not_use_read_text(self):
        """Test check_log_tail_for_prompt doesn't call Path.read_text() for tail check."""
        from serialwrap_reboot_test.controller import RebootController

        with tempfile.TemporaryDirectory() as tmpdir:
            runner = FakeCommandRunner()
            controller = RebootController("COM1", runner=runner, log_dir=tmpdir)

            # Create a log file
            log_file = Path(tmpdir) / "test.log"
            log_file.write_text("\n".join([f"line{i}" for i in range(100)]) + "\nroot@prplOS:/# \n")

            # Patch Path.read_text to detect if it's called
            read_text_called = []
            original_read_text = Path.read_text

            def tracking_read_text(self, *args, **kwargs):
                read_text_called.append(str(self))
                return original_read_text(self, *args, **kwargs)

            with patch.object(Path, 'read_text', tracking_read_text):
                result = controller.check_log_tail_for_prompt(log_file, "root@prplOS:/#", tail_lines=50)

            # Should not have called read_text (use bounded tail instead)
            self.assertEqual(len(read_text_called), 0,
                f"Should not call read_text for tail check, but called on: {read_text_called}")


class TestSpecificExceptionHandling(unittest.TestCase):
    """Test helper methods have specific exception handling."""

    def test_find_active_minicom_log_handles_file_errors(self):
        """Test find_active_minicom_log handles file errors specifically."""
        from serialwrap_reboot_test.controller import RebootController

        with tempfile.TemporaryDirectory() as tmpdir:
            runner = FakeCommandRunner()
            controller = RebootController("COM0", runner=runner, log_dir=tmpdir)

            # Create a log file that will cause permission error
            log_file = Path(tmpdir) / "mini_COM0_test.log"
            log_file.write_text("test")
            log_file.chmod(0o000)  # No permissions

            stderr_output = []
            original_stderr = sys.stderr.write
            def mock_stderr(msg):
                stderr_output.append(msg)
                return original_stderr(msg)

            try:
                with patch('sys.stderr.write', mock_stderr):
                    result = controller.find_active_minicom_log("MARKER", max_age_seconds=600)

                # Should return None (not found) and log warning
                self.assertIsNone(result)

                # Should have warning about file error
                stderr_text = ''.join(stderr_output)
                self.assertTrue('WARNING' in stderr_text or 'error' in stderr_text.lower())
            finally:
                # Restore permissions for cleanup
                log_file.chmod(0o644)

    def test_check_other_selectors_enabled_handles_json_errors(self):
        """Test check_other_selectors_enabled handles JSON parse errors."""
        from serialwrap_reboot_test.controller import RebootController

        runner = FakeCommandRunner()
        runner.set_response('event status', 0, "invalid json{", "")

        controller = RebootController("COM0", runner=runner)

        stderr_output = []
        original_stderr = sys.stderr.write
        def mock_stderr(msg):
            stderr_output.append(msg)
            return original_stderr(msg)

        with patch('sys.stderr.write', mock_stderr):
            result = controller.check_other_selectors_enabled()

        # Unknown status must not be treated as safe to remove shared rules.
        self.assertIsNone(result)

        # Should have warning about parse error
        stderr_text = ''.join(stderr_output)
        self.assertTrue('WARNING' in stderr_text or 'parse' in stderr_text.lower() or 'json' in stderr_text.lower())

    def test_check_other_selectors_enabled_handles_non_object_status(self):
        """Treat valid JSON with the wrong top-level type as unknown."""
        from serialwrap_reboot_test.controller import RebootController

        runner = FakeCommandRunner()
        runner.set_response('event status', 0, "[]", "")

        controller = RebootController("COM0", runner=runner)
        self.assertIsNone(controller.check_other_selectors_enabled())

    def test_check_other_selectors_enabled_handles_non_object_selectors(self):
        """Treat non-object selectors data as unknown."""
        from serialwrap_reboot_test.controller import RebootController

        runner = FakeCommandRunner()
        runner.set_response('event status', 0, json.dumps({"selectors": []}), "")

        controller = RebootController("COM0", runner=runner)
        self.assertIsNone(controller.check_other_selectors_enabled())

    def test_check_other_selectors_enabled_handles_non_object_selector_info(self):
        """Treat non-object selector info as unknown."""
        from serialwrap_reboot_test.controller import RebootController

        runner = FakeCommandRunner()
        runner.set_response('event status', 0, json.dumps({"selectors": {"COM1": True}}), "")

        controller = RebootController("COM0", runner=runner)
        self.assertIsNone(controller.check_other_selectors_enabled())

    def test_check_ready_state_handles_json_errors(self):
        """Test check_ready_state handles JSON parse errors."""
        from serialwrap_reboot_test.controller import RebootController

        runner = FakeCommandRunner()
        runner.set_response('session list', 0, "not json", "")

        controller = RebootController("COM1", runner=runner)

        stderr_output = []
        original_stderr = sys.stderr.write
        def mock_stderr(msg):
            stderr_output.append(msg)
            return original_stderr(msg)

        with patch('sys.stderr.write', mock_stderr):
            result = controller.check_ready_state()

        # Should return False (safe default) and log warning
        self.assertFalse(result)

        # Should have warning about parse error
        stderr_text = ''.join(stderr_output)
        self.assertTrue('WARNING' in stderr_text or 'parse' in stderr_text.lower())

    def test_check_self_test_handles_json_errors(self):
        """Test check_self_test handles JSON parse errors."""
        from serialwrap_reboot_test.controller import RebootController

        runner = FakeCommandRunner()
        runner.set_response('session self-test', 0, "{broken", "")

        controller = RebootController("COM0", runner=runner)

        stderr_output = []
        original_stderr = sys.stderr.write
        def mock_stderr(msg):
            stderr_output.append(msg)
            return original_stderr(msg)

        with patch('sys.stderr.write', mock_stderr):
            result = controller.check_self_test()

        # Should return False (safe default) and log warning
        self.assertFalse(result)

        # Should have warning about parse error
        stderr_text = ''.join(stderr_output)
        self.assertTrue('WARNING' in stderr_text or 'parse' in stderr_text.lower())


if __name__ == "__main__":
    unittest.main()
