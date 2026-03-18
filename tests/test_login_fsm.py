import os
import unittest
from unittest import mock

from sw_core.auth import SessionAuth
from sw_core.config import SessionProfile, UartProfile
from sw_core.login_fsm import ensure_ready, probe_ready


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
            login_regex=r"(?mi)^.*login:\s*$",
            password_regex=r"(?mi)^password:\s*$",
            ready_probe="echo __READY__${nonce}",
            user_env="SW_OPI_U",
            pass_env="SW_OPI_P",
            uart=UartProfile(),
        )

    def test_shell_profile_can_login_with_env_credentials(self) -> None:
        """向後相容：auth=None 時仍從 os.environ 讀取帳密。"""
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

    def test_shell_profile_can_login_with_explicit_auth(self) -> None:
        """帶 SessionAuth 時，不依賴 os.environ。"""
        bridge = mock.MagicMock()
        bridge.wait_for_regex.side_effect = [False, True, True, True, True, True]
        profile = self._make_shell_profile()
        auth = SessionAuth(username="explicit_user", password="explicit_pass")

        with mock.patch.dict(os.environ, {}, clear=True):
            ok, err = ensure_ready(bridge, profile, auth=auth)

        self.assertTrue(ok)
        self.assertIsNone(err)
        bridge.send_command.assert_any_call("explicit_user", source="system")
        bridge.send_secret.assert_called_once_with("explicit_pass")

    def test_probe_ready_reports_login_required_without_auto_login(self) -> None:
        bridge = mock.MagicMock()
        bridge.wait_for_regex.return_value = False
        bridge.rx_tail.return_value = "orangepi3 login: "
        profile = self._make_shell_profile()

        ok, err = probe_ready(bridge, profile)

        self.assertFalse(ok)
        self.assertEqual(err, "LOGIN_REQUIRED")
        bridge.send_command.assert_called_once_with("", source="system")
        bridge.send_secret.assert_not_called()


if __name__ == "__main__":
    unittest.main()
