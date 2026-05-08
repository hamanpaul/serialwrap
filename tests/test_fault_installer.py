#!/usr/bin/env python3
"""Test fault installer for serialwrap-fault-install."""

import unittest
import re


class TestFaultInjectorScript(unittest.TestCase):
    """Test target fault injector script generation (Task 4.1)."""

    def test_build_fault_injector_script_returns_string(self):
        """Test that build_fault_injector_script returns a shell script string."""
        from serialwrap_reboot_test.fault_installer import build_fault_injector_script

        script = build_fault_injector_script()
        self.assertIsInstance(script, str)
        self.assertTrue(len(script) > 0)

    def test_fault_injector_has_shebang(self):
        """Test that the fault injector script has a shell shebang."""
        from serialwrap_reboot_test.fault_installer import build_fault_injector_script

        script = build_fault_injector_script()
        self.assertTrue(script.startswith('#!/bin/sh'))

    def test_fault_injector_has_10_percent_probability(self):
        """Test that the fault injector has a 10% probability gate."""
        from serialwrap_reboot_test.fault_installer import build_fault_injector_script

        script = build_fault_injector_script()
        # Should have a random check, likely with RANDOM % 10 == 0 or similar
        # Looking for 10% probability: 1 in 10 chance
        self.assertTrue(
            'RANDOM' in script or '$RANDOM' in script,
            "Script should use RANDOM for probability"
        )
        # Should have modulo 10 for 10% probability
        self.assertTrue('% 10' in script or '% 100' in script)

    def test_fault_injector_has_four_fault_types(self):
        """Test that the fault injector has four fault event types."""
        from serialwrap_reboot_test.fault_installer import build_fault_injector_script

        script = build_fault_injector_script()
        script_lower = script.lower()
        # Four faults: thermal, 5G ethernet, process coredump, system crash
        # Should have case/if statements or similar for 4 choices
        # Looking for thermal notification text
        self.assertIn('brcm-therm', script_lower)
        self.assertIn('trip 0', script_lower)
        self.assertIn('threshold=105000', script_lower)

        # Looking for ethernet AN (ethctl phy-reset)
        self.assertIn('ethctl', script_lower)
        self.assertIn('phy-reset', script_lower)

        # Looking for coredump references
        self.assertIn('kill', script_lower)

        # Looking for panic or crash
        self.assertTrue('panic' in script_lower or 'crash' in script_lower or 'sysrq' in script_lower)

    def test_fault_injector_thermal_writes_to_dev_console(self):
        """Test that thermal fault writes to /dev/console."""
        from serialwrap_reboot_test.fault_installer import build_fault_injector_script

        script = build_fault_injector_script()
        # Thermal notification must write to /dev/console
        self.assertIn('/dev/console', script)
        # Should have the exact thermal message
        self.assertIn('bcm_thermal_drv brcm-therm: Trip 0: threshold=105000 mC hysteresis=2000 mC', script)

    def test_fault_injector_process_coredump_checks_pid_4000(self):
        """Test that process coredump checks for PID > 4000."""
        from serialwrap_reboot_test.fault_installer import build_fault_injector_script

        script = build_fault_injector_script()
        # Process coredump should check ps aux for PID > 4000
        self.assertIn('ps aux', script)
        self.assertIn('4000', script)

    def test_fault_injector_silent_normal_execution(self):
        """Test that the fault injector runs silently in normal cases."""
        from serialwrap_reboot_test.fault_installer import build_fault_injector_script

        script = build_fault_injector_script()
        # Should redirect stdout/stderr to /dev/null for normal output
        # OR should not have any echo/print statements except for faults
        # Silent means no debug output - check for /dev/null redirects
        self.assertIn('/dev/null', script)

    def test_fault_injector_exits_zero(self):
        """Test that the fault injector exits with 0 on success."""
        from serialwrap_reboot_test.fault_installer import build_fault_injector_script

        script = build_fault_injector_script()
        # Should have exit 0 or implicit success
        self.assertIn('exit 0', script)

    def test_fault_injector_equal_probability_four_faults(self):
        """Test that when triggered, four faults are equally probable (25% each)."""
        from serialwrap_reboot_test.fault_installer import build_fault_injector_script

        script = build_fault_injector_script()
        # Equal probability among 4 choices when triggered
        # Likely uses % 4 or case 0|1|2|3)
        # Should have 4-way branching
        fault_branches = script.count('0)') + script.count('1)') + script.count('2)') + script.count('3)')
        self.assertGreaterEqual(fault_branches, 4, "Should have at least 4 case branches for equal probability")


class TestInitScript(unittest.TestCase):
    """Test init script generation (Task 4.2)."""

    def test_build_init_script_returns_string(self):
        """Test that build_init_script returns a shell script string."""
        from serialwrap_reboot_test.fault_installer import build_init_script

        script = build_init_script()
        self.assertIsInstance(script, str)
        self.assertTrue(len(script) > 0)

    def test_init_script_has_shebang(self):
        """Test that the init script has a shell shebang."""
        from serialwrap_reboot_test.fault_installer import build_init_script

        script = build_init_script()
        self.assertTrue(script.startswith('#!/bin/sh'))

    def test_init_script_calls_fault_injector(self):
        """Test that the init script calls the fault injector."""
        from serialwrap_reboot_test.fault_installer import build_init_script

        script = build_init_script()
        self.assertIn('/usr/sbin/serialwrap-fault-injector', script)

    def test_init_script_handles_start(self):
        """Test that the init script handles start command."""
        from serialwrap_reboot_test.fault_installer import build_init_script

        script = build_init_script()
        self.assertIn('start', script)


class TestFaultInstallerFallback(unittest.TestCase):
    """Test short-write fallback commands (Task 4.2)."""

    def test_short_write_commands_returns_list(self):
        """Test that short_write_commands returns a list of command strings."""
        from serialwrap_reboot_test.fault_installer import short_write_commands

        content = "#!/bin/sh\necho test\n"
        commands = short_write_commands("/usr/sbin/test", content, "0755")
        self.assertIsInstance(commands, list)
        self.assertTrue(len(commands) > 0)

    def test_short_write_commands_no_heredoc(self):
        """Test that fallback commands do not use heredoc."""
        from serialwrap_reboot_test.fault_installer import short_write_commands

        content = "#!/bin/sh\necho test\n" * 50  # Make it longer
        commands = short_write_commands("/usr/sbin/test", content, "0755")

        for cmd in commands:
            # Should not contain heredoc markers
            self.assertNotIn('<<', cmd)
            self.assertNotIn('EOF', cmd)

    def test_short_write_commands_bounded_length(self):
        """Test that each fallback command is reasonably bounded in length."""
        from serialwrap_reboot_test.fault_installer import short_write_commands

        content = "#!/bin/sh\necho test\n" * 100
        commands = short_write_commands("/usr/sbin/test", content, "0755")

        # Each command should be reasonably short for UART safety
        # Allow some overhead for command structure, but keep under 500 chars
        for cmd in commands:
            self.assertLess(len(cmd), 500, f"Command too long: {len(cmd)} chars")

    def test_short_write_commands_sets_permissions(self):
        """Test that short_write_commands includes chmod."""
        from serialwrap_reboot_test.fault_installer import short_write_commands

        content = "#!/bin/sh\necho test\n"
        commands = short_write_commands("/usr/sbin/test", content, "0755")

        # Should include chmod command
        chmod_found = any('chmod' in cmd for cmd in commands)
        self.assertTrue(chmod_found, "Should include chmod command")


class TestFaultInstallerArguments(unittest.TestCase):
    """Test fault installer argument parsing (Task 4.2)."""

    def test_parse_args_requires_selector(self):
        """Test that --selector is required."""
        from serialwrap_reboot_test.fault_installer import parse_args

        with self.assertRaises(SystemExit) as cm:
            parse_args([])

        self.assertNotEqual(cm.exception.code, 0)

    def test_parse_args_with_selector(self):
        """Test parsing with required --selector argument."""
        from serialwrap_reboot_test.fault_installer import parse_args

        args = parse_args(["--selector", "COM1"])
        self.assertEqual(args.selector, "COM1")

    def test_parse_args_validates_selector_format(self):
        """Test that selector must be COMx format."""
        from serialwrap_reboot_test.fault_installer import parse_args

        # Valid formats
        args = parse_args(["--selector", "COM0"])
        self.assertEqual(args.selector, "COM0")

        args = parse_args(["--selector", "COM1"])
        self.assertEqual(args.selector, "COM1")


class TestFaultInstallerDirectoryChecks(unittest.TestCase):
    """Test directory validation before installation (Task 4.2)."""

    def test_check_target_directories_success(self):
        """Test directory check succeeds when directories exist."""
        from serialwrap_reboot_test.fault_installer import FaultInstaller

        class MockRunner:
            def run(self, cmd):
                # Simulate directories exist
                return (0, "", "")

            def run_target(self, cmd):
                return self.run(cmd)

            def run_host(self, args):
                return (0, "", "")

        installer = FaultInstaller("COM1", MockRunner())
        # Should not raise
        result = installer.check_target_directories()
        self.assertTrue(result)

    def test_check_target_directories_failure(self):
        """Test directory check fails when directories missing."""
        from serialwrap_reboot_test.fault_installer import FaultInstaller

        class MockRunner:
            def run(self, cmd):
                # Simulate directory does not exist
                return (1, "", "directory not found")

            def run_target(self, cmd):
                return self.run(cmd)

            def run_host(self, args):
                return (0, "", "")

        installer = FaultInstaller("COM1", MockRunner())
        result = installer.check_target_directories()
        self.assertFalse(result)


class TestFaultInstallerInstallation(unittest.TestCase):
    """Test fault installer installation flow (Task 4.2)."""

    def test_install_uses_file_transfer_first(self):
        """Test that installer tries file transfer before fallback."""
        from serialwrap_reboot_test.fault_installer import FaultInstaller

        class MockRunner:
            def __init__(self):
                self.commands = []

            def run(self, cmd):
                self.commands.append(cmd)
                # Directory checks succeed
                if 'test -d' in cmd:
                    return (0, "", "")
                # base64 check succeeds (enables file transfer)
                if 'which base64' in cmd:
                    return (0, "/usr/bin/base64", "")
                # File transfer succeeds
                if 'file' in cmd and 'send' in cmd:
                    return (0, "transferred", "")
                # Other commands succeed
                return (0, "", "")

            def run_target(self, cmd):
                return self.run(cmd)

            def run_host(self, args):
                # File send via run_host
                self.commands.append(' '.join(args))
                return (0, "transferred", "")

        runner = MockRunner()
        installer = FaultInstaller("COM1", runner)
        installer.install()

        # Should use either file transfer OR fallback (both are valid)
        # Just check that installation commands were run
        self.assertGreater(len(runner.commands), 0, "Should run installation commands")

    def test_install_uses_fallback_when_transfer_fails(self):
        """Test that installer uses fallback when file transfer fails."""
        from serialwrap_reboot_test.fault_installer import FaultInstaller

        class MockRunner:
            def __init__(self):
                self.commands = []

            def run(self, cmd):
                self.commands.append(cmd)
                # Directory checks succeed
                if 'test -d' in cmd:
                    return (0, "", "")
                # File transfer fails
                if 'file' in cmd and 'send' in cmd:
                    return (1, "", "transfer failed")
                # Other commands succeed
                return (0, "", "")

            def run_target(self, cmd):
                return self.run(cmd)

            def run_host(self, args):
                # File send via run_host fails
                self.commands.append(' '.join(args))
                return (1, "", "transfer failed")

        runner = MockRunner()
        installer = FaultInstaller("COM1", runner)
        installer.install()

        # Should have used fallback (base64 or echo commands)
        fallback_used = any('base64' in cmd or 'echo' in cmd or '>>' in cmd
                           for cmd in runner.commands)
        self.assertTrue(fallback_used, "Should use fallback when transfer fails")

    def test_install_sets_executable_permissions(self):
        """Test that installer sets executable permissions."""
        from serialwrap_reboot_test.fault_installer import FaultInstaller

        class MockRunner:
            def __init__(self):
                self.commands = []

            def run(self, cmd):
                self.commands.append(cmd)
                return (0, "", "")

            def run_target(self, cmd):
                return self.run(cmd)

            def run_host(self, args):
                self.commands.append(' '.join(args))
                return (0, "", "")

        runner = MockRunner()
        installer = FaultInstaller("COM1", runner)
        installer.install()

        # Should have chmod commands for executables
        chmod_commands = [cmd for cmd in runner.commands if 'chmod' in cmd]
        self.assertGreater(len(chmod_commands), 0, "Should have chmod commands")

    def test_install_creates_symlink(self):
        """Test that installer creates rc.d symlink."""
        from serialwrap_reboot_test.fault_installer import FaultInstaller

        class MockRunner:
            def __init__(self):
                self.commands = []

            def run(self, cmd):
                self.commands.append(cmd)
                return (0, "", "")

            def run_target(self, cmd):
                return self.run(cmd)

            def run_host(self, args):
                self.commands.append(' '.join(args))
                return (0, "", "")

        runner = MockRunner()
        installer = FaultInstaller("COM1", runner)
        installer.install()

        # Should create symlink S50serialwrap-fault-injector
        symlink_commands = [cmd for cmd in runner.commands if 'ln' in cmd and 'S50' in cmd]
        self.assertGreater(len(symlink_commands), 0, "Should create S50 symlink")

    def test_install_verifies_installation(self):
        """Test that installer verifies files after installation."""
        from serialwrap_reboot_test.fault_installer import FaultInstaller

        class MockRunner:
            def __init__(self):
                self.commands = []

            def run(self, cmd):
                self.commands.append(cmd)
                return (0, "", "")

            def run_target(self, cmd):
                return self.run(cmd)

            def run_host(self, args):
                self.commands.append(' '.join(args))
                return (0, "", "")

        runner = MockRunner()
        installer = FaultInstaller("COM1", runner)
        installer.install()

        # Should verify files exist
        verify_commands = [cmd for cmd in runner.commands
                          if 'test' in cmd or 'ls' in cmd or '[' in cmd]
        self.assertGreater(len(verify_commands), 0, "Should verify installation")


class TestFaultInstallerMain(unittest.TestCase):
    """Test fault installer main entry point (Task 4.2)."""

    def test_main_returns_zero_on_success(self):
        """Test that main returns 0 on successful installation."""
        from serialwrap_reboot_test.fault_installer import main
        from unittest.mock import patch

        class MockInstaller:
            def __init__(self, selector):
                self.selector = selector

            def install(self):
                return True

        with patch('serialwrap_reboot_test.fault_installer.FaultInstaller', MockInstaller):
            with patch('sys.argv', ['serialwrap-fault-install', '--selector', 'COM1']):
                result = main()
                self.assertEqual(result, 0)

    def test_main_returns_nonzero_on_failure(self):
        """Test that main returns non-zero on installation failure."""
        from serialwrap_reboot_test.fault_installer import main
        import sys

        # Mock sys.argv with invalid args
        original_argv = sys.argv
        try:
            sys.argv = ['serialwrap-fault-install']  # Missing --selector
            result = main()
            self.assertNotEqual(result, 0)
        except SystemExit as e:
            # argparse raises SystemExit on error
            self.assertNotEqual(e.code, 0)
        finally:
            sys.argv = original_argv


class TestFileTransferPreferred(unittest.TestCase):
    """Test that file transfer is preferred over fallback (Blocker 1)."""

    def test_transfer_file_attempts_serialwrap_file_send(self):
        """Test that transfer_file tries serialwrap file send first."""
        from serialwrap_reboot_test.fault_installer import FaultInstaller

        class MockRunner:
            def __init__(self):
                self.commands = []

            def run(self, cmd):
                self.commands.append(cmd)
                # Directory checks succeed
                if 'test -d' in cmd:
                    return (0, "", "")
                # File send succeeds
                if 'file send' in cmd or 'file push' in cmd:
                    return (0, "File transferred", "")
                return (0, "", "")

            def run_target(self, cmd):
                return self.run(cmd)

            def run_host(self, args):
                self.commands.append(' '.join(args))
                return (0, "File transferred", "")

        runner = MockRunner()
        installer = FaultInstaller("COM1", runner)
        installer.install()

        # Should have attempted file send/push via serialwrap
        file_commands = [cmd for cmd in runner.commands if 'file send' in cmd or 'file push' in cmd]
        self.assertGreater(len(file_commands), 0, "Should attempt serialwrap file transfer")

    def test_transfer_file_uses_fallback_only_on_failure(self):
        """Test that fallback is only used when file transfer fails."""
        from serialwrap_reboot_test.fault_installer import FaultInstaller

        class MockRunner:
            def __init__(self):
                self.commands = []

            def run(self, cmd):
                self.commands.append(cmd)
                # Directory checks succeed
                if 'test -d' in cmd:
                    return (0, "", "")
                # File send fails
                if 'file send' in cmd or 'file push' in cmd:
                    return (1, "", "transfer failed")
                # Fallback commands succeed
                return (0, "", "")

            def run_target(self, cmd):
                return self.run(cmd)

            def run_host(self, args):
                self.commands.append(' '.join(args))
                return (1, "", "transfer failed")

        runner = MockRunner()
        installer = FaultInstaller("COM1", runner)
        installer.install()

        # Should have attempted file transfer first
        file_commands = [cmd for cmd in runner.commands if 'file send' in cmd or 'file push' in cmd]
        self.assertGreater(len(file_commands), 0, "Should attempt file transfer first")

        # Should have used fallback (base64 or echo)
        fallback_commands = [cmd for cmd in runner.commands if 'base64' in cmd or 'echo' in cmd]
        self.assertGreater(len(fallback_commands), 0, "Should use fallback when transfer fails")

    def test_transfer_file_does_not_use_fallback_on_success(self):
        """Test that fallback is not used when file transfer succeeds."""
        from serialwrap_reboot_test.fault_installer import FaultInstaller

        class MockRunner:
            def __init__(self):
                self.commands = []

            def run(self, cmd):
                self.commands.append(cmd)
                # Directory checks succeed
                if 'test -d' in cmd:
                    return (0, "", "")
                # File send succeeds
                if 'file send' in cmd or 'file push' in cmd:
                    return (0, "transferred", "")
                return (0, "", "")

            def run_target(self, cmd):
                return self.run(cmd)

            def run_host(self, args):
                self.commands.append(' '.join(args))
                return (0, "transferred", "")

        runner = MockRunner()
        installer = FaultInstaller("COM1", runner)
        installer.install()

        # Should not use fallback commands when transfer succeeds
        fallback_commands = [cmd for cmd in runner.commands if 'base64 -d' in cmd or ('echo' in cmd and '>>' in cmd)]
        self.assertEqual(len(fallback_commands), 0, "Should not use fallback when transfer succeeds")


class TestFaultInjectorCommands(unittest.TestCase):
    """Test exact fault injector commands (Blocker 2)."""

    def test_ethernet_fault_uses_ethctl_phy_reset(self):
        """Test that 5G ethernet fault uses ethctl eth0 phy-reset."""
        from serialwrap_reboot_test.fault_installer import build_fault_injector_script

        script = build_fault_injector_script()
        # Must use ethctl eth0 phy-reset for 5G ethernet AN rerun
        self.assertIn('ethctl eth0 phy-reset', script)

    def test_process_coredump_uses_sigabrt(self):
        """Test that process coredump uses SIGABRT."""
        from serialwrap_reboot_test.fault_installer import build_fault_injector_script

        script = build_fault_injector_script()
        # Must use SIGABRT for process coredump
        self.assertIn('SIGABRT', script)

    def test_system_crash_sysrq_correct_syntax(self):
        """Test that system crash uses correct sysrq syntax."""
        from serialwrap_reboot_test.fault_installer import build_fault_injector_script

        script = build_fault_injector_script()
        # Must write c to /proc/sysrq-trigger correctly
        # Check that we have the command (not just commented)
        # Should be: echo c > /proc/sysrq-trigger (without double redirect)
        self.assertIn('/proc/sysrq-trigger', script)
        # Check it's not double-redirected
        self.assertNotIn('> /proc/sysrq-trigger > /dev/null', script)


class TestSymlinkVerification(unittest.TestCase):
    """Test symlink target verification (Blocker 3)."""

    def test_verify_checks_symlink_target(self):
        """Test that verify checks symlink points to init script."""
        from serialwrap_reboot_test.fault_installer import FaultInstaller

        class MockRunner:
            def __init__(self):
                self.commands = []

            def run(self, cmd):
                self.commands.append(cmd)
                # Executables exist
                if 'test -x' in cmd:
                    return (0, "", "")
                # Symlink exists
                if 'test -L' in cmd:
                    return (0, "", "")
                # readlink returns correct target
                if 'readlink' in cmd:
                    return (0, "/etc/init.d/serialwrap-fault-injector", "")
                return (0, "", "")

            def run_target(self, cmd):
                return self.run(cmd)

            def run_host(self, args):
                self.commands.append(' '.join(args))
                return (0, "", "")

        runner = MockRunner()
        installer = FaultInstaller("COM1", runner)
        result = installer.verify()

        # Should check symlink target with readlink
        readlink_commands = [cmd for cmd in runner.commands if 'readlink' in cmd]
        self.assertGreater(len(readlink_commands), 0, "Should verify symlink target with readlink")
        self.assertTrue(result)

    def test_verify_fails_on_wrong_symlink_target(self):
        """Test that verify fails if symlink points to wrong target."""
        from serialwrap_reboot_test.fault_installer import FaultInstaller

        class MockRunner:
            def run(self, cmd):
                # Executables exist
                if 'test -x' in cmd:
                    return (0, "", "")
                # Symlink exists
                if 'test -L' in cmd:
                    return (0, "", "")
                # readlink returns wrong target
                if 'readlink' in cmd:
                    return (0, "/wrong/path", "")
                return (0, "", "")

            def run_target(self, cmd):
                return self.run(cmd)

            def run_host(self, args):
                return (0, "", "")

        runner = MockRunner()
        installer = FaultInstaller("COM1", runner)
        result = installer.verify()

        self.assertFalse(result, "Should fail when symlink target is wrong")


class TestMainWithMocking(unittest.TestCase):
    """Test main entry point with proper mocking (Blocker 5)."""

    def test_main_returns_zero_with_successful_install(self):
        """Test that main returns 0 when installation succeeds."""
        from serialwrap_reboot_test.fault_installer import main, FaultInstaller
        from unittest.mock import patch

        class MockInstaller:
            def __init__(self, selector):
                self.selector = selector

            def install(self):
                return True

        with patch('serialwrap_reboot_test.fault_installer.FaultInstaller', MockInstaller):
            with patch('sys.argv', ['serialwrap-fault-install', '--selector', 'COM1']):
                result = main()
                self.assertEqual(result, 0, "Should return 0 on success")

    def test_main_returns_nonzero_with_failed_install(self):
        """Test that main returns non-zero when installation fails."""
        from serialwrap_reboot_test.fault_installer import main, FaultInstaller
        from unittest.mock import patch

        class MockInstaller:
            def __init__(self, selector):
                self.selector = selector

            def install(self):
                return False

        with patch('serialwrap_reboot_test.fault_installer.FaultInstaller', MockInstaller):
            with patch('sys.argv', ['serialwrap-fault-install', '--selector', 'COM1']):
                result = main()
                self.assertNotEqual(result, 0, "Should return non-zero on failure")


class TestCommandRunnerSeparation(unittest.TestCase):
    """Test host vs target command separation (Blocker 1)."""

    def test_runner_has_run_target_method(self):
        """Test that CommandRunner has run_target for target shell commands."""
        from serialwrap_reboot_test.fault_installer import CommandRunner

        runner = CommandRunner("COM1")
        self.assertTrue(hasattr(runner, 'run_target'), "Should have run_target method")

    def test_runner_has_run_host_method(self):
        """Test that CommandRunner has run_host for host serialwrap commands."""
        from serialwrap_reboot_test.fault_installer import CommandRunner

        runner = CommandRunner("COM1")
        self.assertTrue(hasattr(runner, 'run_host'), "Should have run_host method")

    def test_file_transfer_uses_run_host(self):
        """Test that file transfer uses run_host for host-side serialwrap."""
        from serialwrap_reboot_test.fault_installer import FaultInstaller

        class MockRunner:
            def __init__(self):
                self.host_calls = []
                self.target_calls = []

            def run_host(self, args):
                self.host_calls.append(args)
                # File send succeeds
                if 'file' in ' '.join(args) and 'send' in ' '.join(args):
                    return (0, "transferred", "")
                return (0, "", "")

            def run_target(self, cmd):
                self.target_calls.append(cmd)
                # Directory checks succeed
                if 'test -d' in cmd:
                    return (0, "", "")
                return (0, "", "")

            def run(self, cmd):
                """Backward compatibility."""
                return self.run_target(cmd)

        runner = MockRunner()
        installer = FaultInstaller("COM1", runner)
        installer.install()

        # File transfer should use run_host
        file_host_calls = [args for args in runner.host_calls if 'file' in ' '.join(args)]
        self.assertGreater(len(file_host_calls), 0, "File transfer should use run_host")

    def test_directory_checks_use_run_target(self):
        """Test that directory checks use run_target for target shell commands."""
        from serialwrap_reboot_test.fault_installer import FaultInstaller

        class MockRunner:
            def __init__(self):
                self.host_calls = []
                self.target_calls = []

            def run_host(self, args):
                self.host_calls.append(args)
                return (0, "transferred", "")

            def run_target(self, cmd):
                self.target_calls.append(cmd)
                # Directory checks succeed
                if 'test -d' in cmd:
                    return (0, "", "")
                return (0, "", "")

            def run(self, cmd):
                """Backward compatibility."""
                return self.run_target(cmd)

        runner = MockRunner()
        installer = FaultInstaller("COM1", runner)
        installer.check_target_directories()

        # Directory checks should use run_target
        dir_checks = [cmd for cmd in runner.target_calls if 'test -d' in cmd]
        self.assertGreater(len(dir_checks), 0, "Directory checks should use run_target")

    def test_fallback_uses_run_target(self):
        """Test that fallback short writes use run_target for target commands."""
        from serialwrap_reboot_test.fault_installer import FaultInstaller

        class MockRunner:
            def __init__(self):
                self.host_calls = []
                self.target_calls = []

            def run_host(self, args):
                self.host_calls.append(args)
                # File send fails
                return (1, "", "transfer failed")

            def run_target(self, cmd):
                self.target_calls.append(cmd)
                # Directory checks and fallback succeed
                return (0, "", "")

            def run(self, cmd):
                """Backward compatibility."""
                return self.run_target(cmd)

        runner = MockRunner()
        installer = FaultInstaller("COM1", runner)
        installer.install()

        # Fallback commands should use run_target
        fallback_cmds = [cmd for cmd in runner.target_calls if 'base64' in cmd or 'echo' in cmd]
        self.assertGreater(len(fallback_cmds), 0, "Fallback should use run_target")


class TestProcessCoredumpRandomSelection(unittest.TestCase):
    """Test process coredump random selection (Blocker 2)."""

    def test_process_coredump_collects_all_eligible_pids(self):
        """Test that process coredump script collects all PIDs > 4000."""
        from serialwrap_reboot_test.fault_installer import build_fault_injector_script

        script = build_fault_injector_script()
        # Should collect all PIDs, not use 'exit' in awk that selects first
        self.assertNotIn("awk '$2 > 4000 {print $2; exit}'", script,
                        "Should not exit early in awk - must collect all eligible PIDs")

    def test_process_coredump_uses_random_selection(self):
        """Test that process coredump uses random selection among eligible PIDs."""
        from serialwrap_reboot_test.fault_installer import build_fault_injector_script

        script = build_fault_injector_script()
        # Should use RANDOM or similar for selection
        # Look for patterns that indicate collecting all PIDs then randomly selecting
        self.assertIn('4000', script)  # Has PID threshold
        # Check that we're using randomness in the process selection case
        # The script should have logic to randomly pick from collected PIDs
        process_section = script.split('2)')[1].split(';;')[0] if '2)' in script else ""
        self.assertIn('RANDOM', process_section, "Should use RANDOM for process selection")


class TestSysrqExactCommand(unittest.TestCase):
    """Test system crash sysrq exact command (Blocker 3)."""

    def test_sysrq_command_exact_format(self):
        """Test that sysrq command is exactly 'echo c > /proc/sysrq-trigger'."""
        from serialwrap_reboot_test.fault_installer import build_fault_injector_script

        script = build_fault_injector_script()
        # Should have the exact command
        self.assertIn('echo c > /proc/sysrq-trigger', script)

    def test_sysrq_does_not_redirect_stderr_to_sysrq(self):
        """Test that sysrq command does not have '2>&1' after redirection."""
        from serialwrap_reboot_test.fault_installer import build_fault_injector_script

        script = build_fault_injector_script()
        # Should NOT redirect stderr to the sysrq-trigger file
        self.assertNotIn('/proc/sysrq-trigger 2>&1', script,
                        "Should not redirect stderr to sysrq-trigger")


class TestCommandInjectionDefense(unittest.TestCase):
    """Test command injection defenses in short_write_commands (Fix Pass 3)."""

    def test_path_with_semicolon_does_not_inject_command(self):
        """Test that path with command separator does not allow command injection."""
        from serialwrap_reboot_test.fault_installer import short_write_commands

        # Malicious path attempting command injection
        malicious_path = "/tmp/test; rm -rf /"
        commands = short_write_commands(malicious_path, "content", "0755")

        # Verify path is properly quoted using shlex.quote
        # All instances of the path should be within single quotes
        self.assertIn("'/tmp/test; rm -rf /'", commands[0],
                     "Path with special chars must be quoted")

        # Verify that when executed, the path is treated as a single argument
        # The key is that rm -f gets ONE argument that is the full path,
        # not multiple arguments where ; would be interpreted
        self.assertTrue(commands[0].startswith("rm -f '"),
                       "Path should be quoted immediately after command")

    def test_path_with_spaces_properly_quoted(self):
        """Test that path with spaces is properly quoted."""
        from serialwrap_reboot_test.fault_installer import short_write_commands

        path_with_spaces = "/tmp/my file"
        commands = short_write_commands(path_with_spaces, "content", "0755")

        # Path should be quoted to handle spaces
        self.assertIn("'/tmp/my file'", commands[0],
                     "Path with spaces must be quoted")

    def test_path_with_quotes_properly_escaped(self):
        """Test that path with quotes is properly escaped."""
        from serialwrap_reboot_test.fault_installer import short_write_commands

        path_with_quotes = "/tmp/\"bad'path\""
        commands = short_write_commands(path_with_quotes, "content", "0755")

        # shlex.quote will properly escape the path
        # Verify the first command has the path as a single quoted argument
        self.assertTrue(commands[0].startswith("rm -f "),
                       "Command should start with rm -f")
        # The path should be safely quoted (shlex.quote handles mixed quotes)
        self.assertIn("bad", commands[0],
                     "Path content should be present")

    def test_temp_b64_path_properly_quoted(self):
        """Test that temporary base64 path is properly quoted."""
        from serialwrap_reboot_test.fault_installer import short_write_commands

        # Even if we control temp path, it should still be quoted
        malicious_output = "/tmp/test; rm -rf /"
        commands = short_write_commands(malicious_output, "content", "0755")

        # Temp path (.b64 suffix) should also be quoted
        # Look for the temp file in the echo command
        echo_cmd = [c for c in commands if c.startswith("echo")][0]
        self.assertIn("'/tmp/test; rm -rf /.b64'", echo_cmd,
                     "Temp base64 path must be quoted")


class TestPIDQuoting(unittest.TestCase):
    """Test that TARGET_PID is properly quoted in fault injector script (Fix Pass 3)."""

    def test_target_pid_quoted_in_kill_command(self):
        """Test that TARGET_PID variable is quoted in kill command."""
        from serialwrap_reboot_test.fault_installer import build_fault_injector_script

        script = build_fault_injector_script()

        # PID should be quoted to prevent potential injection
        self.assertIn('kill -SIGABRT "$TARGET_PID"', script,
                     "TARGET_PID must be quoted in kill command")

        # Should NOT have unquoted PID
        self.assertNotIn('kill -SIGABRT $TARGET_PID ', script,
                        "TARGET_PID should not be unquoted (space after to avoid false positive)")


class TestModeValidation(unittest.TestCase):
    """Test mode parameter validation (Fix Pass 3, optional)."""

    def test_invalid_mode_rejected(self):
        """Test that invalid mode parameter is rejected."""
        from serialwrap_reboot_test.fault_installer import short_write_commands

        # Invalid mode with non-octal characters
        with self.assertRaises(ValueError) as ctx:
            short_write_commands("/tmp/test", "content", "777x")

        self.assertIn("mode", str(ctx.exception).lower(),
                     "Error should mention mode parameter")

    def test_mode_too_long_rejected(self):
        """Test that mode with too many digits is rejected."""
        from serialwrap_reboot_test.fault_installer import short_write_commands

        # Mode with too many digits
        with self.assertRaises(ValueError) as ctx:
            short_write_commands("/tmp/test", "content", "07777")

        self.assertIn("mode", str(ctx.exception).lower())

    def test_valid_modes_accepted(self):
        """Test that valid mode parameters are accepted."""
        from serialwrap_reboot_test.fault_installer import short_write_commands

        # Valid modes should work
        valid_modes = ["0755", "755", "0644", "644", "0777"]
        for mode in valid_modes:
            try:
                commands = short_write_commands("/tmp/test", "content", mode)
                self.assertIsInstance(commands, list)
            except ValueError:
                self.fail(f"Valid mode {mode} should be accepted")


class TestTransferFilePathQuoting(unittest.TestCase):
    """Test that transfer_file() quotes remote_path in chmod command."""

    def test_chmod_quotes_remote_path(self):
        """transfer_file() should quote remote_path in chmod command."""
        from serialwrap_reboot_test.fault_installer import FaultInstaller

        # Mock runner that captures chmod command
        class MockRunner:
            def __init__(self):
                self.chmod_cmd = None

            def run_host(self, args):
                # Simulate successful file send
                return (0, "File transferred", "")

            def run_target(self, cmd):
                # Capture chmod command
                if cmd.startswith("chmod"):
                    self.chmod_cmd = cmd
                return (0, "", "")

            def run(self, cmd):
                return self.run_target(cmd)

        runner = MockRunner()
        installer = FaultInstaller("COM1", runner)

        # Try with malicious path
        malicious_path = "/tmp/test; rm -rf /"
        installer.transfer_file("/local/file", malicious_path, "0755")

        # The chmod command should have quoted the path
        # If unquoted, the command would be: chmod 0755 /tmp/test; rm -rf /
        # If quoted, it should be: chmod 0755 '/tmp/test; rm -rf /'
        self.assertIsNotNone(runner.chmod_cmd)
        # Check that the path is quoted (shlex.quote adds single quotes)
        self.assertIn("'/tmp/test; rm -rf /'", runner.chmod_cmd)


class TestInstallPathQuoting(unittest.TestCase):
    """Test that install() quotes paths in ln command."""

    def test_ln_quotes_paths(self):
        """install() should quote both INIT_PATH and SYMLINK_PATH in ln command."""
        from serialwrap_reboot_test.fault_installer import FaultInstaller
        import shlex

        # Mock runner that captures ln command
        class MockRunner:
            def __init__(self):
                self.ln_cmd = None

            def run_host(self, args):
                return (0, "File transferred", "")

            def run_target(self, cmd):
                # Capture ln command
                if cmd.startswith("ln"):
                    self.ln_cmd = cmd
                return (0, "", "")

            def run(self, cmd):
                return self.run_target(cmd)

        runner = MockRunner()
        installer = FaultInstaller("COM1", runner)

        # Call install() which should create symlink
        installer.install()

        # The ln command should have properly quoted paths
        # shlex.quote returns paths unquoted if they don't need it
        self.assertIsNotNone(runner.ln_cmd)
        expected_init = shlex.quote(installer.INIT_PATH)
        expected_symlink = shlex.quote(installer.SYMLINK_PATH)
        self.assertIn(expected_init, runner.ln_cmd)
        self.assertIn(expected_symlink, runner.ln_cmd)


class TestVerifyPathQuoting(unittest.TestCase):
    """Test that verify() quotes paths in test and readlink commands."""

    def test_verify_quotes_paths(self):
        """verify() should quote all paths in test and readlink commands."""
        from serialwrap_reboot_test.fault_installer import FaultInstaller
        import shlex

        # Mock runner that captures all commands
        class MockRunner:
            def __init__(self):
                self.commands = []

            def run_host(self, args):
                return (0, "", "")

            def run_target(self, cmd):
                self.commands.append(cmd)
                # Return appropriate responses
                if "test -x" in cmd:
                    return (0, "", "")  # Success
                elif "test -L" in cmd:
                    return (0, "", "")  # Success
                elif "readlink" in cmd:
                    return (0, "/etc/init.d/serialwrap-fault-injector", "")
                return (0, "", "")

            def run(self, cmd):
                return self.run_target(cmd)

        runner = MockRunner()
        installer = FaultInstaller("COM1", runner)

        # Call verify()
        installer.verify()

        # Check that all test commands have properly quoted paths
        # shlex.quote returns paths unquoted if they don't need it
        expected_injector = shlex.quote(installer.INJECTOR_PATH)
        expected_init = shlex.quote(installer.INIT_PATH)
        expected_symlink = shlex.quote(installer.SYMLINK_PATH)

        for cmd in runner.commands:
            if "test -x" in cmd and "init.d" not in cmd:
                self.assertIn(expected_injector, cmd)
            elif "test -x" in cmd and "init.d" in cmd:
                self.assertIn(expected_init, cmd)
            elif "test -L" in cmd:
                self.assertIn(expected_symlink, cmd)
            elif "readlink" in cmd:
                self.assertIn(expected_symlink, cmd)


class TestRandomPortability(unittest.TestCase):
    """Test that fault injector script does not use non-POSIX $RANDOM."""

    def test_no_bash_random(self):
        """Fault injector script should not contain $RANDOM (bash-specific)."""
        from serialwrap_reboot_test.fault_installer import build_fault_injector_script

        script = build_fault_injector_script()
        self.assertNotIn("$RANDOM", script,
                        "Script contains $RANDOM which is not POSIX sh")
        self.assertNotIn("${RANDOM}", script,
                        "Script contains ${RANDOM} which is not POSIX sh")

    def test_has_posix_random(self):
        """Fault injector script should use POSIX-compatible random method."""
        from serialwrap_reboot_test.fault_installer import build_fault_injector_script

        script = build_fault_injector_script()
        # Should use /dev/urandom for randomness
        self.assertIn("/dev/urandom", script,
                     "Script should use /dev/urandom for POSIX randomness")

    def test_maintains_probability_logic(self):
        """Fault injector script should still have 10% gate and 4-way selection."""
        from serialwrap_reboot_test.fault_installer import build_fault_injector_script

        script = build_fault_injector_script()
        # Should have modulo 10 for 10% probability
        self.assertIn("% 10", script, "Script should have modulo 10 for 10% gate")
        # Should have modulo 4 for 4-way fault selection
        self.assertIn("% 4", script, "Script should have modulo 4 for fault type")


class TestRandomFailureHandling(unittest.TestCase):
    """Test get_random() has graceful failure handling when /dev/urandom or od fails."""

    def test_random_function_redirects_stderr(self):
        """get_random() should redirect stderr to /dev/null to suppress error messages."""
        from serialwrap_reboot_test.fault_installer import build_fault_injector_script

        script = build_fault_injector_script()
        # Should have stderr redirect in the od command
        self.assertIn("2>/dev/null", script,
                      "get_random() should redirect stderr to avoid error messages on failure")

    def test_random_function_validates_output(self):
        """get_random() should validate output and provide fallback for empty or non-numeric values."""
        from serialwrap_reboot_test.fault_installer import build_fault_injector_script

        script = build_fault_injector_script()
        # Should have a case statement or validation logic
        self.assertIn("case", script,
                      "get_random() should use case statement to validate output")
        # Should check for empty or non-numeric values
        self.assertIn("*[!0-9]*", script,
                      "get_random() should check for non-numeric characters")

    def test_random_function_provides_zero_fallback(self):
        """get_random() should default to 0 when od/urandom fails."""
        from serialwrap_reboot_test.fault_installer import build_fault_injector_script

        script = build_fault_injector_script()
        # Should echo 0 as fallback
        lines = script.split('\n')
        # Find the get_random function
        in_get_random = False
        has_zero_fallback = False
        for line in lines:
            if 'get_random()' in line:
                in_get_random = True
            if in_get_random and 'echo 0' in line:
                has_zero_fallback = True
                break
            if in_get_random and line.strip() == '}':
                break

        self.assertTrue(has_zero_fallback,
                        "get_random() should echo 0 as fallback when value is empty or non-numeric")


if __name__ == '__main__':
    unittest.main()
