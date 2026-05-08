#!/usr/bin/env python3
"""Integration tests for controller main flow and startup sequence."""

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock
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

        # Default successful responses
        if 'event --help' in cmd_str:
            return (0, "Usage: serialwrap event ...", "")
        elif 'daemon status' in cmd_str:
            return (0, "Daemon: RUNNING\nPID: 12345", "")
        elif 'cmd submit' in cmd_str:
            return (0, "Command submitted", "")
        elif 'session list' in cmd_str:
            return (0, json.dumps({"sessions": [{"selector": "COM0", "state": "READY"}]}), "")
        elif 'session self-test' in cmd_str:
            return (0, json.dumps({"classification": "OK", "probe_ok": True}), "")
        elif 'event add' in cmd_str:
            return (0, "", "")
        elif 'event enable' in cmd_str:
            return (0, "", "")
        elif 'event disable' in cmd_str:
            return (0, "", "")
        elif 'event reset' in cmd_str:
            return (0, "", "")
        elif 'event status' in cmd_str:
            return (0, json.dumps({"selectors": {"COM0": {"enabled": False}, "COM1": {"enabled": False}}}), "")
        elif 'event rm' in cmd_str:
            return (0, "", "")
        elif 'session recover' in cmd_str:
            return (0, "", "")
        elif 'session interactive-open' in cmd_str:
            return (0, json.dumps({"interactive_id": "agent-int"}), "")
        elif 'session interactive-send' in cmd_str:
            return (0, "", "")
        elif 'session interactive-close' in cmd_str:
            return (0, "", "")

        return (0, "", "")

    def set_response(self, pattern, returncode, stdout, stderr=""):
        """Configure response for commands matching pattern."""
        self.responses[pattern] = (returncode, stdout, stderr)


class TestControllerIntegration(unittest.TestCase):
    """Integration tests for main controller flow."""

    def test_startup_sequence_order(self):
        """Test startup calls methods in correct order."""
        from serialwrap_reboot_test.controller import RebootController

        with tempfile.TemporaryDirectory() as tmpdir:
            runner = FakeCommandRunner()
            controller = RebootController("COM0", runner=runner, log_dir=tmpdir, count_limit=1)

            # Track method calls
            call_order = []
            marker_captured = []

            original_check_event = controller.check_serialwrap_event_support
            original_check_daemon = controller.check_daemon_status
            original_send_marker = controller.send_marker_command
            original_find_log = controller.find_active_minicom_log
            original_register = controller.register_event_rules
            original_enable = controller.enable_selector

            def tracked_check_event():
                call_order.append('check_event_support')
                return original_check_event()

            def tracked_check_daemon():
                call_order.append('check_daemon_status')
                return original_check_daemon()

            def tracked_send_marker():
                call_order.append('send_marker')
                marker = original_send_marker()
                marker_captured.append(marker)
                # Create log with actual marker
                log_file = Path(tmpdir) / "mini_COM0_test.log"
                log_file.write_text(f"boot log\n{marker}\n")
                return marker

            def tracked_find_log(marker, **kwargs):
                call_order.append('find_active_log')
                return original_find_log(marker, **kwargs)

            def tracked_register():
                call_order.append('register_rules')
                return original_register()

            def tracked_enable():
                call_order.append('enable_selector')
                return original_enable()

            controller.check_serialwrap_event_support = tracked_check_event
            controller.check_daemon_status = tracked_check_daemon
            controller.send_marker_command = tracked_send_marker
            controller.find_active_minicom_log = tracked_find_log
            controller.register_event_rules = tracked_register
            controller.enable_selector = tracked_enable

            # Run startup
            result = controller.startup()

            # Verify order
            self.assertTrue(result)
            self.assertEqual(call_order[0], 'check_event_support')
            self.assertEqual(call_order[1], 'check_daemon_status')
            self.assertEqual(call_order[2], 'send_marker')
            self.assertEqual(call_order[3], 'find_active_log')
            # register_rules and enable_selector should come after find_log
            self.assertIn('register_rules', call_order)
            self.assertIn('enable_selector', call_order)

    def test_startup_failure_event_support(self):
        """Test startup fails when event support not available."""
        from serialwrap_reboot_test.controller import RebootController

        runner = FakeCommandRunner()
        runner.set_response('event --help', 1, "", "Unknown command")

        controller = RebootController("COM0", runner=runner)

        result = controller.startup()

        self.assertFalse(result)

    def test_startup_failure_daemon_not_running(self):
        """Test startup fails when daemon not running."""
        from serialwrap_reboot_test.controller import RebootController

        runner = FakeCommandRunner()
        runner.set_response('daemon status', 1, "", "Daemon not running")

        controller = RebootController("COM1", runner=runner)

        result = controller.startup()

        self.assertFalse(result)

    def test_startup_failure_no_active_log(self):
        """Test startup fails when no active log found."""
        from serialwrap_reboot_test.controller import RebootController

        with tempfile.TemporaryDirectory() as tmpdir:
            runner = FakeCommandRunner()
            controller = RebootController("COM0", runner=runner, log_dir=tmpdir)

            result = controller.startup()

            self.assertFalse(result)

    def test_startup_sets_active_log_path(self):
        """Test startup sets active_log_path from find_active_minicom_log."""
        from serialwrap_reboot_test.controller import RebootController

        with tempfile.TemporaryDirectory() as tmpdir:
            runner = FakeCommandRunner()
            controller = RebootController("COM0", runner=runner, log_dir=tmpdir)

            # Intercept marker to create log
            original_send_marker = controller.send_marker_command
            def tracked_send_marker():
                marker = original_send_marker()
                # Create log with actual marker
                log_file = Path(tmpdir) / "mini_COM0_test.log"
                log_file.write_text(f"boot log\n{marker}\n")
                return marker
            controller.send_marker_command = tracked_send_marker

            self.assertIsNone(controller.active_log_path)

            result = controller.startup()

            self.assertTrue(result)
            self.assertIsNotNone(controller.active_log_path)
            self.assertTrue(controller.active_log_path.exists())

    def test_startup_adopts_prompted_active_log_when_marker_session_not_ready(self):
        """Startup should adopt a recent prompted log when marker submit is blocked."""
        from serialwrap_reboot_test.controller import RebootController

        with tempfile.TemporaryDirectory() as tmpdir:
            runner = FakeCommandRunner()
            runner.set_response(
                'cmd submit',
                1,
                json.dumps({
                    "ok": False,
                    "error_code": "SESSION_NOT_READY",
                    "session": {"state": "ATTACHED"}
                }),
                ""
            )
            log_file = Path(tmpdir) / "mini_COM0_existing.log"
            log_file.write_text("U-Boot\n=> \n")

            controller = RebootController("COM0", runner=runner, log_dir=tmpdir)

            result = controller.startup()

            self.assertTrue(result)
            self.assertEqual(controller.active_log_path, log_file)
            self.assertIsNotNone(controller.state_dir)
            self.assertTrue(controller.state_dir.exists())

    def test_startup_still_fails_without_prompted_log_when_marker_session_not_ready(self):
        """Startup should still fail if no prompted active log can be adopted."""
        from serialwrap_reboot_test.controller import RebootController

        with tempfile.TemporaryDirectory() as tmpdir:
            runner = FakeCommandRunner()
            runner.set_response(
                'cmd submit',
                1,
                json.dumps({
                    "ok": False,
                    "error_code": "SESSION_NOT_READY",
                    "session": {"state": "ATTACHED"}
                }),
                ""
            )
            log_file = Path(tmpdir) / "mini_COM0_existing.log"
            log_file.write_text("U-Boot\nno prompt yet\n")

            controller = RebootController("COM0", runner=runner, log_dir=tmpdir)

            result = controller.startup()

            self.assertFalse(result)
            self.assertIsNone(controller.active_log_path)

    def test_startup_does_not_adopt_older_prompted_log_when_newer_log_lacks_prompt(self):
        """Fallback should not use an older prompted log over the newest active-looking log."""
        from serialwrap_reboot_test.controller import RebootController

        with tempfile.TemporaryDirectory() as tmpdir:
            runner = FakeCommandRunner()
            runner.set_response(
                'cmd submit',
                1,
                json.dumps({
                    "ok": False,
                    "error_code": "SESSION_NOT_READY",
                    "session": {"state": "ATTACHED"}
                }),
                ""
            )

            older_log = Path(tmpdir) / "mini_COM0_older.log"
            older_log.write_text("old session\n=> \n")
            newer_log = Path(tmpdir) / "mini_COM0_newer.log"
            newer_log.write_text("new session\nstill booting\n")
            now = time.time()
            os.utime(older_log, (now - 5, now - 5))
            os.utime(newer_log, (now, now))

            controller = RebootController("COM0", runner=runner, log_dir=tmpdir)

            result = controller.startup()

            self.assertFalse(result)
            self.assertIsNone(controller.active_log_path)

    def test_startup_uses_structured_session_not_ready_signal(self):
        """Fallback should use structured error metadata, not exception text matching."""
        from serialwrap_reboot_test.controller import RebootController, ControllerError

        with tempfile.TemporaryDirectory() as tmpdir:
            runner = FakeCommandRunner()
            log_file = Path(tmpdir) / "mini_COM0_existing.log"
            log_file.write_text("login\nroot@prplOS:/# \n")

            controller = RebootController("COM0", runner=runner, log_dir=tmpdir)

            def structured_send_marker_failure():
                error = ControllerError("marker submission blocked")
                error.error_code = "SESSION_NOT_READY"
                raise error

            controller.send_marker_command = structured_send_marker_failure

            result = controller.startup()

            self.assertTrue(result)
            self.assertEqual(controller.active_log_path, log_file)

    def test_startup_reuses_trusted_previous_active_log_when_marker_blocked(self):
        """Startup should reuse a trusted prior active log when marker submit is blocked."""
        from serialwrap_reboot_test.controller import RebootController

        with tempfile.TemporaryDirectory() as tmpdir:
            runner = FakeCommandRunner()
            runner.set_response(
                'cmd submit',
                1,
                json.dumps({
                    "ok": False,
                    "error_code": "SESSION_NOT_READY",
                    "session": {"state": "ATTACHED"}
                }),
                ""
            )
            log_file = Path(tmpdir) / "mini_COM0_latest.log"
            log_file.write_text("boot continues\nno prompt right now\n")

            previous_state_dir = Path(tmpdir) / "saved-state"
            previous_state_dir.mkdir()
            (previous_state_dir / "active_minicom_log.txt").write_text(str(log_file))

            controller = RebootController("COM0", runner=runner, log_dir=tmpdir)
            run_state_dir = Path(tmpdir) / "new-state"
            controller.iter_previous_run_state_dirs = lambda: [previous_state_dir]
            def fake_create_run_state_directory():
                run_state_dir.mkdir()
                return run_state_dir
            controller.create_run_state_directory = fake_create_run_state_directory
            controller.register_event_rules = lambda: None
            controller.enable_selector = lambda: None
            controller.register_signal_handlers = lambda: None

            result = controller.startup()

            self.assertTrue(result)
            self.assertEqual(controller.active_log_path, log_file)

    def test_startup_reuses_trusted_previous_active_log_when_marker_lookup_misses(self):
        """Startup should reuse a trusted prior active log when marker discovery misses."""
        from serialwrap_reboot_test.controller import RebootController

        with tempfile.TemporaryDirectory() as tmpdir:
            runner = FakeCommandRunner()
            log_file = Path(tmpdir) / "mini_COM0_latest.log"
            log_file.write_text("boot continues\nno prompt right now\n")

            previous_state_dir = Path(tmpdir) / "saved-state"
            previous_state_dir.mkdir()
            (previous_state_dir / "active_minicom_log.txt").write_text(str(log_file))

            controller = RebootController("COM0", runner=runner, log_dir=tmpdir)
            run_state_dir = Path(tmpdir) / "new-state-marker-miss"
            controller.find_active_minicom_log = lambda marker, **kwargs: None
            controller.iter_previous_run_state_dirs = lambda: [previous_state_dir]

            def fake_create_run_state_directory():
                run_state_dir.mkdir()
                return run_state_dir

            controller.create_run_state_directory = fake_create_run_state_directory
            controller.register_event_rules = lambda: None
            controller.enable_selector = lambda: None
            controller.register_signal_handlers = lambda: None

            result = controller.startup()

            self.assertTrue(result)
            self.assertEqual(controller.active_log_path, log_file)

    def test_startup_prefers_prompted_log_before_trusted_previous_when_marker_lookup_misses(self):
        """Startup should prefer a prompted current log before trusted prior state."""
        from serialwrap_reboot_test.controller import RebootController

        with tempfile.TemporaryDirectory() as tmpdir:
            runner = FakeCommandRunner()
            prompted_log = Path(tmpdir) / "mini_COM0_latest.log"
            prompted_log.write_text("login\nroot@prplOS:/# \n")
            older_trusted_log = Path(tmpdir) / "mini_COM0_older.log"
            older_trusted_log.write_text("older session\n")
            now = time.time()
            os.utime(older_trusted_log, (now - 5, now - 5))
            os.utime(prompted_log, (now, now))

            previous_state_dir = Path(tmpdir) / "saved-state"
            previous_state_dir.mkdir()
            (previous_state_dir / "active_minicom_log.txt").write_text(str(older_trusted_log))

            controller = RebootController("COM0", runner=runner, log_dir=tmpdir)
            run_state_dir = Path(tmpdir) / "new-state-prompted-first"
            controller.find_active_minicom_log = lambda marker, **kwargs: None
            controller.iter_previous_run_state_dirs = lambda: [previous_state_dir]

            def fake_create_run_state_directory():
                run_state_dir.mkdir()
                return run_state_dir

            controller.create_run_state_directory = fake_create_run_state_directory
            controller.register_event_rules = lambda: None
            controller.enable_selector = lambda: None
            controller.register_signal_handlers = lambda: None

            result = controller.startup()

            self.assertTrue(result)
            self.assertEqual(controller.active_log_path, prompted_log)

    def test_startup_rejects_previous_active_log_if_not_newest_selector_log(self):
        """Startup should not trust prior state when it points to an older selector log."""
        from serialwrap_reboot_test.controller import RebootController

        with tempfile.TemporaryDirectory() as tmpdir:
            runner = FakeCommandRunner()
            runner.set_response(
                'cmd submit',
                1,
                json.dumps({
                    "ok": False,
                    "error_code": "SESSION_NOT_READY",
                    "session": {"state": "ATTACHED"}
                }),
                ""
            )
            older_log = Path(tmpdir) / "mini_COM0_older.log"
            older_log.write_text("older session\n")
            newer_log = Path(tmpdir) / "mini_COM0_newer.log"
            newer_log.write_text("newer session\nstill booting\n")
            now = time.time()
            os.utime(older_log, (now - 5, now - 5))
            os.utime(newer_log, (now, now))

            previous_state_dir = Path(tmpdir) / "saved-state"
            previous_state_dir.mkdir()
            (previous_state_dir / "active_minicom_log.txt").write_text(str(older_log))

            controller = RebootController("COM0", runner=runner, log_dir=tmpdir)
            controller.iter_previous_run_state_dirs = lambda: [previous_state_dir]
            controller.register_event_rules = lambda: None
            controller.enable_selector = lambda: None
            controller.register_signal_handlers = lambda: None

            result = controller.startup()

            self.assertFalse(result)
            self.assertIsNone(controller.active_log_path)

    def test_startup_reuses_trusted_previous_active_log_even_when_log_is_older_than_recency_window(self):
        """Trusted previous-log fallback should ignore the normal recency window."""
        from serialwrap_reboot_test.controller import RebootController

        with tempfile.TemporaryDirectory() as tmpdir:
            runner = FakeCommandRunner()
            runner.set_response(
                'cmd submit',
                1,
                json.dumps({
                    "ok": False,
                    "error_code": "SESSION_NOT_READY",
                    "session": {"state": "ATTACHED"}
                }),
                ""
            )
            log_file = Path(tmpdir) / "mini_COM0_old_but_current.log"
            log_file.write_text("quiet active log\n")
            old_time = time.time() - 1200
            os.utime(log_file, (old_time, old_time))

            previous_state_dir = Path(tmpdir) / "saved-state"
            previous_state_dir.mkdir()
            (previous_state_dir / "active_minicom_log.txt").write_text(str(log_file))

            controller = RebootController("COM0", runner=runner, log_dir=tmpdir)
            run_state_dir = Path(tmpdir) / "new-state-old-log"
            controller.iter_previous_run_state_dirs = lambda: [previous_state_dir]

            def fake_create_run_state_directory():
                run_state_dir.mkdir()
                return run_state_dir

            controller.create_run_state_directory = fake_create_run_state_directory
            controller.register_event_rules = lambda: None
            controller.enable_selector = lambda: None
            controller.register_signal_handlers = lambda: None

            result = controller.startup()

            self.assertTrue(result)
            self.assertEqual(controller.active_log_path, log_file)

    def test_startup_sets_state_dir(self):
        """Test startup sets state_dir for cleanup."""
        from serialwrap_reboot_test.controller import RebootController

        with tempfile.TemporaryDirectory() as tmpdir:
            runner = FakeCommandRunner()
            controller = RebootController("COM1", runner=runner, log_dir=tmpdir)

            # Intercept marker to create log
            original_send_marker = controller.send_marker_command
            def tracked_send_marker():
                marker = original_send_marker()
                # Create log with actual marker
                log_file = Path(tmpdir) / "mini_COM1_test.log"
                log_file.write_text(f"boot log\n{marker}\n")
                return marker
            controller.send_marker_command = tracked_send_marker

            self.assertIsNone(controller.state_dir)

            result = controller.startup()

            self.assertTrue(result)
            self.assertIsNotNone(controller.state_dir)
            self.assertTrue(controller.state_dir.exists())

    def test_run_loop_executes_normal_reboot(self):
        """Test run loop executes normal reboot action."""
        from serialwrap_reboot_test.controller import RebootController

        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = Path(tmpdir) / "mini_COM0_test.log"
            log_file.write_text("boot log\n__SW_REBOOT_TEST_COM0_test__\n")

            runner = FakeCommandRunner()
            controller = RebootController("COM0", runner=runner, log_dir=tmpdir, count_limit=1)
            controller.startup()

            # Run one loop iteration
            controller.run_loop()

            # Should have submitted reboot
            reboot_submitted = any('cmd submit' in ' '.join(cmd) and 'reboot' in ' '.join(cmd)
                                  for cmd in runner.commands)
            self.assertTrue(reboot_submitted)
            self.assertEqual(controller.reboot_count, 1)

    def test_run_loop_executes_raw_reset(self):
        """Test run loop executes raw reset fallback."""
        from serialwrap_reboot_test.controller import RebootController

        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = Path(tmpdir) / "mini_COM0_test.log"
            log_file.write_text("boot log\n=>\n")

            runner = FakeCommandRunner()
            # Simulate NOT READY
            runner.set_response('session list', 0,
                json.dumps({"sessions": [{"selector": "COM0", "state": "RECOVERY"}]}))

            controller = RebootController("COM0", runner=runner, log_dir=tmpdir, count_limit=1)

            # Intercept marker to create log
            original_send_marker = controller.send_marker_command
            def tracked_send_marker():
                marker = original_send_marker()
                log_file.write_text(f"boot log\n{marker}\n=>\n")
                return marker
            controller.send_marker_command = tracked_send_marker

            controller.startup()
            # Set last action time to allow recovery
            controller.last_action_time = time.time() - 400

            # Run one loop iteration
            controller.run_loop()

            # Should have sent raw reset through interactive console API
            raw_reset = any('interactive-send' in ' '.join(cmd) and 'reset' in ' '.join(cmd)
                           for cmd in runner.commands)
            self.assertTrue(raw_reset)

    def test_run_loop_sleeps_on_wait(self):
        """Test run loop sleeps when action is wait."""
        from serialwrap_reboot_test.controller import RebootController

        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = Path(tmpdir) / "mini_COM1_test.log"
            log_file.write_text("boot log\nno prompt\n")

            runner = FakeCommandRunner()
            # Simulate NOT READY
            runner.set_response('session list', 0,
                json.dumps({"sessions": [{"selector": "COM1", "state": "RECOVERY"}]}))

            controller = RebootController("COM1", runner=runner, log_dir=tmpdir, count_limit=1)
            controller.startup()
            # Set last action time to trigger throttle
            controller.last_action_time = time.time() - 10

            # Mock sleep
            sleep_called = []
            def mock_sleep(seconds):
                sleep_called.append(seconds)

            controller.sleep_fn = mock_sleep

            # Run one loop iteration
            controller.run_loop()

            # Should have slept
            self.assertTrue(len(sleep_called) > 0)

    def test_main_integration_with_count_limit(self):
        """Test main() runs full startup -> loop -> cleanup flow."""
        from serialwrap_reboot_test.controller import main_with_runner

        with tempfile.TemporaryDirectory() as tmpdir:
            runner = FakeCommandRunner()

            # Intercept the first marker command to create the log
            marker_created = []
            original_run = runner.run
            def intercepting_run(cmd, **kwargs):
                result = original_run(cmd, **kwargs)
                # Check if this is a marker submission
                cmd_str = ' '.join(cmd)
                if 'cmd submit' in cmd_str and '__SW_REBOOT_TEST_' in cmd_str:
                    # Extract marker from command
                    for part in cmd:
                        if '__SW_REBOOT_TEST_' in part:
                            marker = part.replace('echo ', '').strip()
                            if marker not in marker_created:
                                marker_created.append(marker)
                                # Create log with marker
                                log_file = Path(tmpdir) / "mini_COM0_test.log"
                                log_file.write_text(f"boot log\n{marker}\n")
                            break
                return result
            runner.run = intercepting_run

            # Run main with test runner
            exit_code = main_with_runner(
                ["--selector", "COM0", "--count", "1"],
                runner=runner,
                log_dir=tmpdir
            )

            self.assertEqual(exit_code, 0)

            # Verify startup happened
            event_check = any('event --help' in ' '.join(cmd) for cmd in runner.commands)
            daemon_check = any('daemon status' in ' '.join(cmd) for cmd in runner.commands)
            self.assertTrue(event_check)
            self.assertTrue(daemon_check)

            # Verify rules registered
            rule_adds = [cmd for cmd in runner.commands if 'event' in ' '.join(cmd) and 'add' in ' '.join(cmd)]
            self.assertEqual(len(rule_adds), 5)

            # Verify reboot submitted
            reboot_cmd = any('cmd submit' in ' '.join(cmd) and 'reboot' in ' '.join(cmd)
                            for cmd in runner.commands)
            self.assertTrue(reboot_cmd)

            # Verify cleanup happened
            disable_cmd = any('event' in ' '.join(cmd) and 'disable' in ' '.join(cmd)
                             for cmd in runner.commands)
            self.assertTrue(disable_cmd)

    def test_main_returns_nonzero_on_startup_failure(self):
        """Test main() returns non-zero exit code on startup failure."""
        from serialwrap_reboot_test.controller import main_with_runner

        runner = FakeCommandRunner()
        runner.set_response('event --help', 1, "", "Unknown command")

        exit_code = main_with_runner(
            ["--selector", "COM0"],
            runner=runner
        )

        self.assertNotEqual(exit_code, 0)

    def test_marker_submission_failure(self):
        """Test controller handles marker submission failure."""
        from serialwrap_reboot_test.controller import RebootController, ControllerError

        runner = FakeCommandRunner()
        runner.set_response('cmd submit', 1, "", "Failed to submit")

        controller = RebootController("COM0", runner=runner)

        with self.assertRaises(ControllerError):
            controller.send_marker_command()

    def test_reboot_submission_failure(self):
        """Test controller handles reboot submission failure."""
        from serialwrap_reboot_test.controller import RebootController, ControllerError

        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = Path(tmpdir) / "mini_COM0_test.log"
            log_file.write_text("boot log\n")

            runner = FakeCommandRunner()
            runner.set_response('cmd submit', 1, "", "Failed to submit reboot")

            controller = RebootController("COM0", runner=runner, log_dir=tmpdir)
            controller.active_log_path = log_file

            with self.assertRaises(ControllerError):
                controller.submit_normal_reboot()

            # Reboot count should not increment on failure
            self.assertEqual(controller.reboot_count, 0)

    def test_event_add_failure(self):
        """Test controller handles event rule add failure."""
        from serialwrap_reboot_test.controller import RebootController, ControllerError

        runner = FakeCommandRunner()
        runner.set_response('event add', 1, "", "Failed to add rule")

        controller = RebootController("COM0", runner=runner)

        with self.assertRaises(ControllerError):
            controller.register_event_rules()


if __name__ == "__main__":
    unittest.main()
