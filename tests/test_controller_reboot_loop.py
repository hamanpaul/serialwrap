#!/usr/bin/env python3
"""Test controller reboot loop logic (Task 2.4)."""

import json
import os
import time
import unittest
from unittest.mock import Mock, patch


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


class TestControllerRebootLoop(unittest.TestCase):
    """Test controller reboot loop decision logic."""

    def test_check_ready_state_when_ready(self):
        """Test checking READY state when session is READY."""
        from serialwrap_reboot_test.controller import RebootController

        runner = FakeCommandRunner()

        # Simulate READY state
        session_output = json.dumps({
            "sessions": [{
                "selector": "COM0",
                "state": "READY"
            }]
        })
        runner.set_response('session list', 0, session_output)

        controller = RebootController("COM0", runner=runner)
        result = controller.check_ready_state()

        self.assertTrue(result)

    def test_check_ready_state_accepts_com_field(self):
        """Treat session.com as the selector field in newer session list output."""
        from serialwrap_reboot_test.controller import RebootController

        runner = FakeCommandRunner()
        session_output = json.dumps({
            "sessions": [{
                "com": "COM0",
                "state": "READY"
            }]
        })
        runner.set_response('session list', 0, session_output)

        controller = RebootController("COM0", runner=runner)
        self.assertTrue(controller.check_ready_state())

    def test_check_ready_state_when_not_ready(self):
        """Test checking READY state when session is not READY."""
        from serialwrap_reboot_test.controller import RebootController

        runner = FakeCommandRunner()

        # Simulate NOT_READY state
        session_output = json.dumps({
            "sessions": [{
                "selector": "COM1",
                "state": "RECOVERY"
            }]
        })
        runner.set_response('session list', 0, session_output)

        controller = RebootController("COM1", runner=runner)
        result = controller.check_ready_state()

        self.assertFalse(result)

    def test_check_self_test_ok(self):
        """Test self-test check when classification is OK."""
        from serialwrap_reboot_test.controller import RebootController

        runner = FakeCommandRunner()

        # Simulate self-test OK
        selftest_output = json.dumps({
            "classification": "OK",
            "probe_ok": True
        })
        runner.set_response('session self-test', 0, selftest_output)

        controller = RebootController("COM0", runner=runner)
        result = controller.check_self_test()

        self.assertTrue(result)

    def test_check_self_test_not_ok(self):
        """Test self-test check when classification is not OK."""
        from serialwrap_reboot_test.controller import RebootController

        runner = FakeCommandRunner()

        # Simulate self-test failure
        selftest_output = json.dumps({
            "classification": "DEGRADED",
            "probe_ok": False
        })
        runner.set_response('session self-test', 0, selftest_output)

        controller = RebootController("COM1", runner=runner)
        result = controller.check_self_test()

        self.assertFalse(result)

    def test_submit_normal_reboot(self):
        """Test submitting normal reboot command."""
        from serialwrap_reboot_test.controller import RebootController

        runner = FakeCommandRunner()
        controller = RebootController("COM0", runner=runner)

        timestamp = controller.submit_normal_reboot()

        # Should return a timestamp
        self.assertIsNotNone(timestamp)
        self.assertIsInstance(timestamp, float)

        # Should have submitted reboot command with agent source
        cmd_found = False
        for cmd in runner.commands:
            cmd_str = ' '.join(cmd)
            if 'cmd submit' in cmd_str and 'agent:reboot-controller' in cmd_str and 'reboot' in cmd_str:
                cmd_found = True
                self.assertIn('--timeout', cmd)
                self.assertIn('--cmd-timeout', cmd)
                break
        self.assertTrue(cmd_found)

    def test_should_throttle_recovery_within_five_minutes(self):
        """Test recovery throttling within 5 minutes."""
        from serialwrap_reboot_test.controller import RebootController

        runner = FakeCommandRunner()
        controller = RebootController("COM1", runner=runner)

        # Set last action to 2 minutes ago
        last_action = time.time() - 120

        result = controller.should_throttle_recovery(last_action, throttle_seconds=300)

        self.assertTrue(result)

    def test_should_throttle_recovery_after_five_minutes(self):
        """Test recovery throttling after 5 minutes."""
        from serialwrap_reboot_test.controller import RebootController

        runner = FakeCommandRunner()
        controller = RebootController("COM0", runner=runner)

        # Set last action to 6 minutes ago
        last_action = time.time() - 360

        result = controller.should_throttle_recovery(last_action, throttle_seconds=300)

        self.assertFalse(result)

    def test_run_session_recover(self):
        """Test running session recover command."""
        from serialwrap_reboot_test.controller import RebootController

        runner = FakeCommandRunner()
        controller = RebootController("COM1", runner=runner)

        controller.run_session_recover()

        # Should have called session recover
        cmd_found = False
        for cmd in runner.commands:
            cmd_str = ' '.join(cmd)
            if 'session recover' in cmd_str:
                cmd_found = True
                self.assertIn('--timeout', cmd)
                break
        self.assertTrue(cmd_found)

    def test_check_log_tail_for_uboot_prompt(self):
        """Test checking log tail for U-Boot prompt."""
        from serialwrap_reboot_test.controller import RebootController
        from pathlib import Path
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = Path(tmpdir) / "mini_COM0_test.log"
            log_file.write_text("boot log\nsome output\n=>\nmore output\n")

            runner = FakeCommandRunner()
            controller = RebootController("COM0", runner=runner)

            result = controller.check_log_tail_for_prompt(log_file, "=>", tail_lines=50)

            self.assertTrue(result)

    def test_check_log_tail_for_prplos_prompt(self):
        """Test checking log tail for prplOS prompt."""
        from serialwrap_reboot_test.controller import RebootController
        from pathlib import Path
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = Path(tmpdir) / "mini_COM1_test.log"
            log_file.write_text("boot log\nlogin successful\nroot@prplOS:/# \n")

            runner = FakeCommandRunner()
            controller = RebootController("COM1", runner=runner)

            result = controller.check_log_tail_for_prompt(log_file, "root@prplOS:/#", tail_lines=50)

            self.assertTrue(result)

    def test_check_log_tail_no_prompt(self):
        """Test checking log tail when no prompt is found."""
        from serialwrap_reboot_test.controller import RebootController
        from pathlib import Path
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = Path(tmpdir) / "mini_COM0_test.log"
            log_file.write_text("boot log\nsome output\nno prompt here\n")

            runner = FakeCommandRunner()
            controller = RebootController("COM0", runner=runner)

            result = controller.check_log_tail_for_prompt(log_file, "=>", tail_lines=50)

            self.assertFalse(result)

    def test_send_raw_broker_command_reset(self):
        """Test sending raw broker command for reset."""
        from serialwrap_reboot_test.controller import RebootController

        runner = FakeCommandRunner()
        runner.set_response('session interactive-open', 0, json.dumps({"interactive_id": "agent-int"}))
        controller = RebootController("COM0", runner=runner)

        timestamp = controller.send_raw_broker_command("reset")

        # Should return a timestamp
        self.assertIsNotNone(timestamp)
        self.assertIsInstance(timestamp, float)

        # Should have sent via interactive console API
        cmd_found = False
        for cmd in runner.commands:
            cmd_str = ' '.join(cmd)
            if 'interactive-send' in cmd_str and 'reset' in cmd_str:
                cmd_found = True
                break
        self.assertTrue(cmd_found)

    def test_send_raw_broker_command_reboot_force(self):
        """Test sending raw broker command for reboot -f."""
        from serialwrap_reboot_test.controller import RebootController

        runner = FakeCommandRunner()
        runner.set_response('session interactive-open', 0, json.dumps({"interactive_id": "agent-int"}))
        controller = RebootController("COM1", runner=runner)

        timestamp = controller.send_raw_broker_command("reboot -f")

        # Should return a timestamp
        self.assertIsNotNone(timestamp)

        # Should have sent via interactive console API
        cmd_found = False
        for cmd in runner.commands:
            cmd_str = ' '.join(cmd)
            if 'interactive-send' in cmd_str and 'reboot -f' in cmd_str:
                cmd_found = True
                break
        self.assertTrue(cmd_found)

    @patch('serialwrap_reboot_test.controller.os.close')
    @patch('serialwrap_reboot_test.controller.os.write')
    @patch('serialwrap_reboot_test.controller.os.open')
    def test_send_raw_broker_command_falls_back_to_console_attach_when_session_not_ready(
        self,
        mock_open,
        mock_write,
        mock_close,
    ):
        """Use console attach fallback when interactive-open reports SESSION_NOT_READY."""
        from serialwrap_reboot_test.controller import RebootController

        runner = FakeCommandRunner()
        runner.set_response(
            'session interactive-open',
            1,
            json.dumps({"ok": False, "error_code": "SESSION_NOT_READY"}),
            "",
        )
        runner.set_response(
            'session console-attach',
            0,
            json.dumps({
                "client_id": "client-123",
                "interactive_owner": True,
                "vtty": "/dev/pts/42",
            }),
            "",
        )
        runner.set_response('session console-detach', 0, json.dumps({"ok": True}), "")
        mock_open.return_value = 99
        mock_write.return_value = len(b"reset\n")

        controller = RebootController("COM0", runner=runner)

        timestamp = controller.send_raw_broker_command("reset")

        self.assertIsInstance(timestamp, float)
        mock_open.assert_called_once_with("/dev/pts/42", os.O_WRONLY)
        mock_write.assert_called_once_with(99, b"reset\n")
        mock_close.assert_called_once_with(99)
        self.assertTrue(any('session console-attach' in ' '.join(cmd) for cmd in runner.commands))
        detach_cmd = next(
            cmd for cmd in runner.commands
            if 'session console-detach' in ' '.join(cmd)
        )
        self.assertIn('--selector', detach_cmd)
        self.assertIn('COM0', detach_cmd)
        self.assertIn('--client-id', detach_cmd)

    @patch('serialwrap_reboot_test.controller.os.open')
    def test_send_raw_broker_command_fails_when_console_attach_does_not_grant_ownership(
        self,
        mock_open,
    ):
        """Fallback must fail clearly when console attach does not grant ownership."""
        from serialwrap_reboot_test.controller import RebootController, ControllerError

        runner = FakeCommandRunner()
        runner.set_response(
            'session interactive-open',
            1,
            json.dumps({"ok": False, "error_code": "SESSION_NOT_READY"}),
            "",
        )
        runner.set_response(
            'session console-attach',
            0,
            json.dumps({
                "client_id": "client-456",
                "interactive_owner": False,
                "vtty": "/dev/pts/77",
            }),
            "",
        )

        controller = RebootController("COM1", runner=runner)

        with self.assertRaises(ControllerError) as cm:
            controller.send_raw_broker_command("reboot -f")

        self.assertIn("ownership", str(cm.exception).lower())
        mock_open.assert_not_called()
        self.assertTrue(any('session console-detach' in ' '.join(cmd) for cmd in runner.commands))

    @patch('serialwrap_reboot_test.controller.os.close')
    @patch('serialwrap_reboot_test.controller.os.write')
    @patch('serialwrap_reboot_test.controller.os.open')
    def test_send_raw_broker_command_ignores_console_not_found_on_detach(
        self,
        mock_open,
        mock_write,
        mock_close,
    ):
        """Console detach should be best-effort when console is already gone."""
        from serialwrap_reboot_test.controller import RebootController

        runner = FakeCommandRunner()
        runner.set_response(
            'session interactive-open',
            1,
            json.dumps({"ok": False, "error_code": "SESSION_NOT_READY"}),
            "",
        )
        runner.set_response(
            'session console-attach',
            0,
            json.dumps({
                "client_id": "client-789",
                "interactive_owner": True,
                "vtty": "/dev/pts/88",
            }),
            "",
        )
        runner.set_response(
            'session console-detach',
            1,
            json.dumps({"ok": False, "error_code": "CONSOLE_NOT_FOUND"}),
            "",
        )
        mock_open.return_value = 101
        mock_write.return_value = len(b"reset\n")

        controller = RebootController("COM0", runner=runner)

        timestamp = controller.send_raw_broker_command("reset")

        self.assertIsInstance(timestamp, float)
        mock_open.assert_called_once_with("/dev/pts/88", os.O_WRONLY)
        mock_write.assert_called_once_with(101, b"reset\n")
        mock_close.assert_called_once_with(101)

    @patch('serialwrap_reboot_test.controller.os.close')
    @patch('serialwrap_reboot_test.controller.os.write')
    @patch('serialwrap_reboot_test.controller.os.open')
    def test_send_raw_broker_command_fails_on_short_write_and_detaches(
        self,
        mock_open,
        mock_write,
        mock_close,
    ):
        """Short writes must fail and still release the attached console."""
        from serialwrap_reboot_test.controller import RebootController, ControllerError

        runner = FakeCommandRunner()
        runner.set_response(
            'session interactive-open',
            1,
            json.dumps({"ok": False, "error_code": "SESSION_NOT_READY"}),
            "",
        )
        runner.set_response(
            'session console-attach',
            0,
            json.dumps({
                "client_id": "client-short",
                "interactive_owner": True,
                "vtty": "/dev/pts/66",
            }),
            "",
        )
        runner.set_response('session console-detach', 0, json.dumps({"ok": True}), "")
        mock_open.return_value = 55
        mock_write.return_value = len(b"reset\n") - 1

        controller = RebootController("COM0", runner=runner)

        with self.assertRaises(ControllerError) as cm:
            controller.send_raw_broker_command("reset")

        self.assertIn("short write", str(cm.exception).lower())
        mock_open.assert_called_once_with("/dev/pts/66", os.O_WRONLY)
        mock_write.assert_called_once_with(55, b"reset\n")
        mock_close.assert_called_once_with(55)
        self.assertTrue(any('session console-detach' in ' '.join(cmd) for cmd in runner.commands))

    def test_reboot_decision_ready_and_self_test_ok(self):
        """Test reboot decision when READY and self-test OK."""
        from serialwrap_reboot_test.controller import RebootController

        runner = FakeCommandRunner()

        # Simulate READY and self-test OK
        session_output = json.dumps({"sessions": [{"selector": "COM0", "state": "READY"}]})
        selftest_output = json.dumps({"classification": "OK", "probe_ok": True})
        runner.set_response('session list', 0, session_output)
        runner.set_response('session self-test', 0, selftest_output)

        controller = RebootController("COM0", runner=runner)

        action = controller.decide_reboot_action(None)

        self.assertEqual(action['type'], 'normal_reboot')

    def test_reboot_decision_not_ready_within_throttle(self):
        """Test reboot decision when not READY and within throttle period."""
        from serialwrap_reboot_test.controller import RebootController

        runner = FakeCommandRunner()

        # Simulate NOT READY
        session_output = json.dumps({"sessions": [{"selector": "COM1", "state": "RECOVERY"}]})
        runner.set_response('session list', 0, session_output)

        controller = RebootController("COM1", runner=runner)

        # Last action 2 minutes ago (within 5 minute throttle)
        last_action = time.time() - 120

        action = controller.decide_reboot_action(last_action)

        self.assertEqual(action['type'], 'wait')

    def test_reboot_decision_not_ready_after_throttle_no_prompt(self):
        """Test reboot decision when not READY, after throttle, no prompt found."""
        from serialwrap_reboot_test.controller import RebootController
        from pathlib import Path
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = Path(tmpdir) / "mini_COM0_test.log"
            log_file.write_text("boot log\nno prompt\n")

            runner = FakeCommandRunner()

            # Simulate NOT READY
            session_output = json.dumps({"sessions": [{"selector": "COM0", "state": "RECOVERY"}]})
            runner.set_response('session list', 0, session_output)

            controller = RebootController("COM0", runner=runner, active_log=log_file)

            # Last action 6 minutes ago (after 5 minute throttle)
            last_action = time.time() - 360

            action = controller.decide_reboot_action(last_action)

            # Should have run recover, then wait since no prompt
            self.assertEqual(action['type'], 'wait')

    def test_reboot_decision_uses_prompt_fallback_after_recover_failure(self):
        """Test prompt fallback still works when recover returns a JSON error."""
        from serialwrap_reboot_test.controller import RebootController
        from pathlib import Path
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = Path(tmpdir) / "mini_COM1_test.log"
            log_file.write_text("boot log\nroot@prplOS:/# \n")

            runner = FakeCommandRunner()
            session_output = json.dumps({"sessions": [{"selector": "COM1", "state": "ATTACHED"}]})
            runner.set_response('session list', 0, session_output)
            runner.set_response(
                'session recover',
                2,
                json.dumps({"ok": False, "error_code": "PROMPT_TIMEOUT", "partial": True}),
                "",
            )

            controller = RebootController("COM1", runner=runner, active_log=log_file)

            last_action = time.time() - 360
            action = controller.decide_reboot_action(last_action)

            self.assertEqual(action['type'], 'raw_reboot')


if __name__ == "__main__":
    unittest.main()
