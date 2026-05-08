#!/usr/bin/env python3
"""Test controller event rule management (Task 2.3)."""

import json
import tempfile
import unittest
from pathlib import Path
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


class TestControllerEventRules(unittest.TestCase):
    """Test controller event rule management."""

    def test_generate_event_rule_json(self):
        """Test generating event rule JSON for shared rules."""
        from serialwrap_reboot_test.controller import RebootController

        runner = FakeCommandRunner()
        controller = RebootController("COM0", runner=runner)

        rules = controller.generate_event_rules()

        # Should have 5 shared rules
        self.assertEqual(len(rules), 5)

        # Check rule structure
        rule_names = [r['name'] for r in rules]
        self.assertIn('brcm-therm', rule_names)
        self.assertIn('link-down', rule_names)
        self.assertIn('pstate', rule_names)
        self.assertIn('kernel-panic', rule_names)
        self.assertIn('smc-bootloader', rule_names)

        # Check that each rule has both COM0 and COM1 selectors
        for rule in rules:
            self.assertIn('COM0', rule['selectors'])
            self.assertIn('COM1', rule['selectors'])
            self.assertEqual(rule['kind'], 'tool')

    def test_generate_event_rules_use_runtime_event_schema(self):
        """Generate rules compatible with serialwrap event file schema."""
        from serialwrap_reboot_test.controller import RebootController

        runner = FakeCommandRunner()
        controller = RebootController("COM0", runner=runner)

        rule = controller.generate_event_rules()[0]

        self.assertIn("schema_version", rule)
        self.assertIn("owner", rule)
        self.assertIn("rule_id", rule)
        self.assertIn("pattern", rule)
        self.assertIn("handler", rule)
        self.assertEqual(rule["schema_version"], 1)
        self.assertEqual(rule["owner"], "agent-reboot-controller")
        self.assertEqual(rule["rule_id"], "agent-reboot-controller.brcm-therm")
        self.assertEqual(rule["kind"], "tool")
        self.assertEqual(rule["pattern"]["kind"], "contains")
        self.assertEqual(rule["pattern"]["value"], "brcm-therm")
        self.assertEqual(rule["handler"]["exec"], ["serialwrap-event-handler"])
        self.assertFalse(rule["auto_enable_com_on_load"])

    def test_brcm_therm_rule_match(self):
        """Test brcm-therm rule has correct match pattern."""
        from serialwrap_reboot_test.controller import RebootController

        runner = FakeCommandRunner()
        controller = RebootController("COM0", runner=runner)

        rules = controller.generate_event_rules()
        brcm_rule = next(r for r in rules if r['name'] == 'brcm-therm')

        self.assertEqual(brcm_rule['pattern']['value'], 'brcm-therm')

    def test_link_down_rule_match(self):
        """Test link-down rule has correct match pattern."""
        from serialwrap_reboot_test.controller import RebootController

        runner = FakeCommandRunner()
        controller = RebootController("COM1", runner=runner)

        rules = controller.generate_event_rules()
        link_rule = next(r for r in rules if r['name'] == 'link-down')

        self.assertEqual(link_rule['pattern']['value'], 'Link is Down')

    def test_pstate_rule_match_case_sensitive(self):
        """Test pstate rule match is case-sensitive lowercase."""
        from serialwrap_reboot_test.controller import RebootController

        runner = FakeCommandRunner()
        controller = RebootController("COM0", runner=runner)

        rules = controller.generate_event_rules()
        pstate_rule = next(r for r in rules if r['name'] == 'pstate')

        # Should be lowercase 'pstate', not 'PSTATE' or 'Pstate'
        self.assertEqual(pstate_rule['pattern']['value'], 'pstate')

    def test_kernel_panic_rule_match(self):
        """Test kernel-panic rule has correct match pattern."""
        from serialwrap_reboot_test.controller import RebootController

        runner = FakeCommandRunner()
        controller = RebootController("COM1", runner=runner)

        rules = controller.generate_event_rules()
        panic_rule = next(r for r in rules if r['name'] == 'kernel-panic')

        self.assertEqual(panic_rule['pattern']['value'], 'Kernel panic')

    def test_smc_bootloader_rule_match(self):
        """Test smc-bootloader rule has correct match pattern."""
        from serialwrap_reboot_test.controller import RebootController

        runner = FakeCommandRunner()
        controller = RebootController("COM0", runner=runner)

        rules = controller.generate_event_rules()
        smc_rule = next(r for r in rules if r['name'] == 'smc-bootloader')

        self.assertEqual(smc_rule['pattern']['value'], 'SMC bootloader')

    def test_register_event_rules(self):
        """Test registering event rules with serialwrap."""
        from serialwrap_reboot_test.controller import RebootController

        runner = FakeCommandRunner()
        controller = RebootController("COM0", runner=runner)

        controller.register_event_rules()

        # Should have called event add for each rule
        add_commands = [cmd for cmd in runner.commands if 'event' in ' '.join(cmd) and 'add' in ' '.join(cmd)]
        self.assertEqual(len(add_commands), 5)

    def test_register_event_rules_uses_file_api(self):
        """Register rules through event add --file for the deployed CLI."""
        from serialwrap_reboot_test.controller import RebootController

        runner = FakeCommandRunner()
        controller = RebootController("COM0", runner=runner)

        controller.register_event_rules()

        add_commands = [
            cmd for cmd in runner.commands
            if 'event add' in ' '.join(cmd)
        ]
        self.assertEqual(len(add_commands), 5)
        for cmd in add_commands:
            self.assertIn("--timeout", cmd)
            self.assertIn("--file", cmd)

    def test_enable_selector(self):
        """Test enabling event matcher for selected COM."""
        from serialwrap_reboot_test.controller import RebootController

        runner = FakeCommandRunner()
        controller = RebootController("COM1", runner=runner)

        controller.enable_selector()

        # Should have called event enable for COM1
        enable_found = False
        for cmd in runner.commands:
            cmd_str = ' '.join(cmd)
            if 'event' in cmd_str and 'enable' in cmd_str and 'COM1' in cmd_str:
                enable_found = True
                break
        self.assertTrue(enable_found)

    def test_disable_selector(self):
        """Test disabling event matcher for selected COM."""
        from serialwrap_reboot_test.controller import RebootController

        runner = FakeCommandRunner()
        controller = RebootController("COM0", runner=runner)

        controller.disable_selector()

        # Should have called event disable for COM0
        disable_found = False
        for cmd in runner.commands:
            cmd_str = ' '.join(cmd)
            if 'event' in cmd_str and 'disable' in cmd_str and 'COM0' in cmd_str:
                disable_found = True
                break
        self.assertTrue(disable_found)

    def test_reset_selector(self):
        """Test resetting event state for selected COM."""
        from serialwrap_reboot_test.controller import RebootController

        runner = FakeCommandRunner()
        controller = RebootController("COM1", runner=runner)

        controller.reset_selector()

        # Should have called event reset for COM1
        reset_found = False
        for cmd in runner.commands:
            cmd_str = ' '.join(cmd)
            if 'event' in cmd_str and 'reset' in cmd_str and 'COM1' in cmd_str:
                reset_found = True
                break
        self.assertTrue(reset_found)

    def test_check_other_selectors_enabled(self):
        """Test checking if other COM selectors are still enabled."""
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
        result = controller.check_other_selectors_enabled()

        # COM1 is still enabled, so should return True
        self.assertTrue(result)

    def test_check_other_selectors_none_enabled(self):
        """Test checking when no other COM selectors are enabled."""
        from serialwrap_reboot_test.controller import RebootController

        runner = FakeCommandRunner()

        # Simulate all disabled
        status_output = json.dumps({
            "selectors": {
                "COM0": {"enabled": False},
                "COM1": {"enabled": False}
            }
        })
        runner.set_response('event status', 0, status_output)

        controller = RebootController("COM1", runner=runner)
        result = controller.check_other_selectors_enabled()

        # No other selectors enabled, so should return False
        self.assertFalse(result)

    def test_check_other_selectors_enabled_accepts_coms_list(self):
        """Treat status.coms as the enabled selector list in newer daemons."""
        from serialwrap_reboot_test.controller import RebootController

        runner = FakeCommandRunner()
        runner.set_response('event status', 0, json.dumps({"coms": ["COM1"]}))

        controller = RebootController("COM0", runner=runner)
        self.assertTrue(controller.check_other_selectors_enabled())

    def test_remove_event_rules(self):
        """Test removing shared event rules."""
        from serialwrap_reboot_test.controller import RebootController

        runner = FakeCommandRunner()
        controller = RebootController("COM0", runner=runner)

        controller.remove_event_rules()

        # Should have called event rm for each rule
        rm_commands = [cmd for cmd in runner.commands if 'event' in ' '.join(cmd) and 'rm' in ' '.join(cmd)]
        self.assertEqual(len(rm_commands), 5)
        self.assertIn('agent-reboot-controller.brcm-therm', ' '.join(rm_commands[0]))


if __name__ == "__main__":
    unittest.main()
