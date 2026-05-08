#!/usr/bin/env python3
"""Test package structure and constants."""

import os
import sys
import unittest


class TestPackageStructure(unittest.TestCase):
    """Test that package structure exists with correct constants."""

    def test_package_import(self):
        """Test that serialwrap_reboot_test package can be imported."""
        import serialwrap_reboot_test
        self.assertIsNotNone(serialwrap_reboot_test)

    def test_serialwrap_command_default(self):
        """Test that SERIALWRAP_CMD defaults correctly when no env override."""
        # Clear any existing override
        old_value = os.environ.pop('SERIALWRAP_CMD', None)
        try:
            # Force reload to pick up cleared env
            import serialwrap_reboot_test.constants
            import importlib
            importlib.reload(serialwrap_reboot_test.constants)

            self.assertEqual(
                serialwrap_reboot_test.constants.SERIALWRAP_CMD,
                "/home/paul_chen/.paul_tools/serialwrap"
            )
        finally:
            # Restore old value if it existed
            if old_value is not None:
                os.environ['SERIALWRAP_CMD'] = old_value

    def test_serialwrap_command_env_override(self):
        """Test that SERIALWRAP_CMD honors environment variable override."""
        test_path = "/custom/path/to/serialwrap"
        old_value = os.environ.get('SERIALWRAP_CMD')
        try:
            os.environ['SERIALWRAP_CMD'] = test_path

            # Force reload to pick up new env
            import serialwrap_reboot_test.constants
            import importlib
            importlib.reload(serialwrap_reboot_test.constants)

            self.assertEqual(
                serialwrap_reboot_test.constants.SERIALWRAP_CMD,
                test_path
            )
        finally:
            # Restore old value or clear
            if old_value is not None:
                os.environ['SERIALWRAP_CMD'] = old_value
            else:
                os.environ.pop('SERIALWRAP_CMD', None)


class TestEntrypointFiles(unittest.TestCase):
    """Test that entrypoint files exist and are executable."""

    def setUp(self):
        """Set up test fixtures."""
        self.project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.bin_dir = os.path.join(self.project_root, "bin")

    def test_bin_directory_exists(self):
        """Test that bin/ directory exists."""
        self.assertTrue(
            os.path.isdir(self.bin_dir),
            f"bin directory does not exist at {self.bin_dir}"
        )

    def test_reboot_controller_exists(self):
        """Test that serialwrap-reboot-controller entrypoint exists."""
        entrypoint = os.path.join(self.bin_dir, "serialwrap-reboot-controller")
        self.assertTrue(
            os.path.isfile(entrypoint),
            f"serialwrap-reboot-controller does not exist at {entrypoint}"
        )

    def test_event_handler_exists(self):
        """Test that serialwrap-event-handler entrypoint exists."""
        entrypoint = os.path.join(self.bin_dir, "serialwrap-event-handler")
        self.assertTrue(
            os.path.isfile(entrypoint),
            f"serialwrap-event-handler does not exist at {entrypoint}"
        )

    def test_fault_install_exists(self):
        """Test that serialwrap-fault-install entrypoint exists."""
        entrypoint = os.path.join(self.bin_dir, "serialwrap-fault-install")
        self.assertTrue(
            os.path.isfile(entrypoint),
            f"serialwrap-fault-install does not exist at {entrypoint}"
        )

    def test_reboot_controller_executable(self):
        """Test that serialwrap-reboot-controller is executable."""
        entrypoint = os.path.join(self.bin_dir, "serialwrap-reboot-controller")
        if os.path.isfile(entrypoint):
            self.assertTrue(
                os.access(entrypoint, os.X_OK),
                f"serialwrap-reboot-controller is not executable"
            )

    def test_event_handler_executable(self):
        """Test that serialwrap-event-handler is executable."""
        entrypoint = os.path.join(self.bin_dir, "serialwrap-event-handler")
        if os.path.isfile(entrypoint):
            self.assertTrue(
                os.access(entrypoint, os.X_OK),
                f"serialwrap-event-handler is not executable"
            )

    def test_fault_install_executable(self):
        """Test that serialwrap-fault-install is executable."""
        entrypoint = os.path.join(self.bin_dir, "serialwrap-fault-install")
        if os.path.isfile(entrypoint):
            self.assertTrue(
                os.access(entrypoint, os.X_OK),
                f"serialwrap-fault-install is not executable"
            )


if __name__ == "__main__":
    unittest.main()
