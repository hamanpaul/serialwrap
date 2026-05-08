#!/usr/bin/env python3
"""Test controller argument parsing."""

import sys
import unittest
from io import StringIO


class TestControllerArgumentParsing(unittest.TestCase):
    """Test argument parsing for serialwrap-reboot-controller."""

    def test_import_controller(self):
        """Test that controller module can be imported."""
        from serialwrap_reboot_test import controller
        self.assertIsNotNone(controller)

    def test_parse_args_requires_selector(self):
        """Test that --selector is required."""
        from serialwrap_reboot_test.controller import parse_args

        with self.assertRaises(SystemExit) as cm:
            parse_args([])

        self.assertNotEqual(cm.exception.code, 0)

    def test_parse_args_with_selector(self):
        """Test parsing with required --selector argument."""
        from serialwrap_reboot_test.controller import parse_args

        args = parse_args(["--selector", "COM0"])
        self.assertEqual(args.selector, "COM0")
        self.assertIsNone(args.hours)
        self.assertIsNone(args.count)

    def test_parse_args_with_hours_limit(self):
        """Test parsing with optional --hours argument."""
        from serialwrap_reboot_test.controller import parse_args

        args = parse_args(["--selector", "COM1", "--hours", "96"])
        self.assertEqual(args.selector, "COM1")
        self.assertEqual(args.hours, 96)
        self.assertIsNone(args.count)

    def test_parse_args_with_count_limit(self):
        """Test parsing with optional --count argument."""
        from serialwrap_reboot_test.controller import parse_args

        args = parse_args(["--selector", "COM0", "--count", "10"])
        self.assertEqual(args.selector, "COM0")
        self.assertIsNone(args.hours)
        self.assertEqual(args.count, 10)

    def test_parse_args_with_both_limits(self):
        """Test parsing with both --hours and --count arguments."""
        from serialwrap_reboot_test.controller import parse_args

        args = parse_args(["--selector", "COM1", "--hours", "24", "--count", "50"])
        self.assertEqual(args.selector, "COM1")
        self.assertEqual(args.hours, 24)
        self.assertEqual(args.count, 50)

    def test_parse_args_hours_rejects_non_integer(self):
        """Test that --hours rejects non-integer values."""
        from serialwrap_reboot_test.controller import parse_args

        with self.assertRaises(SystemExit) as cm:
            parse_args(["--selector", "COM0", "--hours", "not-a-number"])

        self.assertNotEqual(cm.exception.code, 0)

    def test_parse_args_hours_rejects_float(self):
        """Test that --hours rejects float values."""
        from serialwrap_reboot_test.controller import parse_args

        with self.assertRaises(SystemExit) as cm:
            parse_args(["--selector", "COM0", "--hours", "3.14"])

        self.assertNotEqual(cm.exception.code, 0)

    def test_parse_args_hours_rejects_zero(self):
        """Test that --hours rejects zero."""
        from serialwrap_reboot_test.controller import parse_args

        with self.assertRaises(SystemExit) as cm:
            parse_args(["--selector", "COM0", "--hours", "0"])

        self.assertNotEqual(cm.exception.code, 0)

    def test_parse_args_hours_rejects_negative(self):
        """Test that --hours rejects negative values."""
        from serialwrap_reboot_test.controller import parse_args

        with self.assertRaises(SystemExit) as cm:
            parse_args(["--selector", "COM0", "--hours", "-1"])

        self.assertNotEqual(cm.exception.code, 0)

    def test_parse_args_count_rejects_non_integer(self):
        """Test that --count rejects non-integer values."""
        from serialwrap_reboot_test.controller import parse_args

        with self.assertRaises(SystemExit) as cm:
            parse_args(["--selector", "COM0", "--count", "not-a-number"])

        self.assertNotEqual(cm.exception.code, 0)

    def test_parse_args_count_rejects_float(self):
        """Test that --count rejects float values."""
        from serialwrap_reboot_test.controller import parse_args

        with self.assertRaises(SystemExit) as cm:
            parse_args(["--selector", "COM0", "--count", "2.5"])

        self.assertNotEqual(cm.exception.code, 0)

    def test_parse_args_count_rejects_zero(self):
        """Test that --count rejects zero."""
        from serialwrap_reboot_test.controller import parse_args

        with self.assertRaises(SystemExit) as cm:
            parse_args(["--selector", "COM0", "--count", "0"])

        self.assertNotEqual(cm.exception.code, 0)

    def test_parse_args_count_rejects_negative(self):
        """Test that --count rejects negative values."""
        from serialwrap_reboot_test.controller import parse_args

        with self.assertRaises(SystemExit) as cm:
            parse_args(["--selector", "COM0", "--count", "-1"])

        self.assertNotEqual(cm.exception.code, 0)


if __name__ == "__main__":
    unittest.main()
