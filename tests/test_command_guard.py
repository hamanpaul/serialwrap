import unittest

from sw_core.service import SerialwrapService
from sw_core.util import shell_command_incomplete_reason


class TestCommandGuard(unittest.TestCase):
    def test_detect_unbalanced_single_quote(self) -> None:
        reason = shell_command_incomplete_reason("wpa_cli -i wl1 set_network 0 ssid '\"B0_6G_AP\"")
        self.assertEqual(reason, "UNBALANCED_SINGLE_QUOTE")

    def test_detect_trailing_operator(self) -> None:
        reason = shell_command_incomplete_reason("iw dev wl0 link |")
        self.assertEqual(reason, "TRAILING_OPERATOR")

    def test_allow_complete_command(self) -> None:
        reason = shell_command_incomplete_reason("iw dev wl0 link | grep -q 'Connected to '")
        self.assertIsNone(reason)

    def test_service_no_longer_blocks_incomplete_command_preflight(self) -> None:
        svc = SerialwrapService([])
        resp = svc.rpc(
            "command.submit",
            {
                "selector": "COM0",
                "cmd": "echo 'broken",
                "source": "agent:test",
            },
        )
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error_code"], "SESSION_NOT_FOUND")


if __name__ == "__main__":
    unittest.main()
