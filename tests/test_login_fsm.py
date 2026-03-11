import os
import unittest
from unittest import mock

from sw_core.config import SessionProfile, UartProfile
from sw_core.login_fsm import ensure_ready


class TestLoginFsm(unittest.TestCase):
    def _make_shell_profile(self) -> SessionProfile:
        return SessionProfile(
            profile_name="opi-shell",
            com="COM2",
            act_no=3,
            alias="default+3",
            device_by_id="/dev/serial/by-id/tty2",
            platform="shell",
            prompt_regex=r".*[$#] $",
            login_regex=r"(?mi)^login:\s*$",
            password_regex=r"(?mi)^password:\s*$",
            ready_probe="echo __READY__${nonce}",
            user_env="SW_OPI_U",
            pass_env="SW_OPI_P",
            uart=UartProfile(),
        )

    def test_shell_profile_can_login_with_env_credentials(self) -> None:
        bridge = mock.MagicMock()
        bridge.wait_for_regex.side_effect = [False, True, True, True, True, True]
        profile = self._make_shell_profile()

        with mock.patch.dict(os.environ, {"SW_OPI_U": "haman", "SW_OPI_P": "secret"}, clear=False):
            ok, err = ensure_ready(bridge, profile)

        self.assertTrue(ok)
        self.assertIsNone(err)
        bridge.send_command.assert_any_call("", source="system")
        bridge.send_command.assert_any_call("haman", source="system")
        bridge.send_secret.assert_called_once_with("secret")
        probe_calls = [call for call in bridge.send_command.call_args_list if "__READY__" in str(call)]
        self.assertEqual(len(probe_calls), 1)


if __name__ == "__main__":
    unittest.main()
