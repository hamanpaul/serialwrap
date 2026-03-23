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
        self.assertEqual(bridge.clear_rx_buffer.call_count, 2)
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
        self.assertEqual(bridge.clear_rx_buffer.call_count, 2)

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
        bridge.clear_rx_buffer.assert_called_once_with()


class TestDetectTemplate(unittest.TestCase):
    """detect_template() 自動偵測 ProfileTemplate 的單元測試。"""

    def _make_templates(self) -> list:
        from sw_core.config import ProfileTemplate
        return [
            ProfileTemplate(
                profile_name="prpl-template",
                platform="prpl",
                prompt_regex=r"(?m)^root@prplOS:.*# ",
                login_regex=r"(?mi)^login:\s*$",
            ),
            ProfileTemplate(
                profile_name="brcm-template",
                platform="bcm",
                prompt_regex=r"(?m)[>#]\s*$",
                login_regex=r"(?mi)login:\s*$",
            ),
            ProfileTemplate(
                profile_name="op3-template",
                platform="shell",
                prompt_regex=r".*[$#] $",
                login_regex=r"(?mi)^.*login:\s*$",
            ),
            ProfileTemplate(
                profile_name="others-template",
                platform="passthrough",
                prompt_regex=".*",
                login_regex="$^",
            ),
        ]

    def test_detect_prpl_prompt(self) -> None:
        """UART 輸出含 prplOS prompt → 回傳 prpl-template。"""
        from sw_core.login_fsm import detect_template

        bridge = mock.MagicMock()
        bridge.rx_tail.return_value = "\r\nroot@prplOS:/# "
        templates = self._make_templates()
        result = detect_template(bridge, templates, probe_timeout_s=0.01)
        self.assertIsNotNone(result)
        self.assertEqual(result.profile_name, "prpl-template")

    def test_detect_bcm_prompt(self) -> None:
        """UART 輸出含 bcm prompt → 回傳 brcm-template。"""
        from sw_core.login_fsm import detect_template

        bridge = mock.MagicMock()
        bridge.rx_tail.return_value = "\r\nBCM968575> "
        templates = self._make_templates()
        result = detect_template(bridge, templates, probe_timeout_s=0.01)
        self.assertIsNotNone(result)
        self.assertEqual(result.profile_name, "brcm-template")

    def test_detect_shell_prompt(self) -> None:
        """UART 輸出含 generic shell prompt → 回傳 op3-template。"""
        from sw_core.login_fsm import detect_template

        bridge = mock.MagicMock()
        # 用 $ 結尾避免被 bcm 的 [>#]\s*$ 搶走
        bridge.rx_tail.return_value = "\r\nuser@host:~$ "
        templates = self._make_templates()
        result = detect_template(bridge, templates, probe_timeout_s=0.01)
        self.assertIsNotNone(result)
        self.assertEqual(result.profile_name, "op3-template")

    def test_detect_login_regex_fallback(self) -> None:
        """UART 輸出是 login prompt → 用 login_regex 匹配回傳 template。"""
        from sw_core.login_fsm import detect_template

        bridge = mock.MagicMock()
        bridge.rx_tail.return_value = "\r\norangepi3 login: "
        templates = self._make_templates()
        result = detect_template(bridge, templates, probe_timeout_s=0.01)
        self.assertIsNotNone(result)
        # login_regex "(?mi)^login:\s*$" 不匹配（因為有 hostname），
        # 但 op3 的 "(?mi)^.*login:\s*$" 會匹配
        self.assertIn(result.profile_name, ("prpl-template", "brcm-template", "op3-template"))

    def test_detect_none_when_no_output(self) -> None:
        """UART 沒有輸出 → 回傳 None。"""
        from sw_core.login_fsm import detect_template

        bridge = mock.MagicMock()
        bridge.rx_tail.return_value = ""
        templates = self._make_templates()
        result = detect_template(bridge, templates, probe_timeout_s=0.01)
        self.assertIsNone(result)

    def test_passthrough_never_matched(self) -> None:
        """passthrough 的 prompt_regex '.*' 不會被偵測使用。"""
        from sw_core.login_fsm import detect_template

        bridge = mock.MagicMock()
        bridge.rx_tail.return_value = "some random boot garbage\r\n"
        templates = self._make_templates()
        result = detect_template(bridge, templates, probe_timeout_s=0.01)
        # passthrough 被 skip，其他都不匹配 → None
        self.assertIsNone(result)

    def test_template_order_specificity(self) -> None:
        """prpl 排在 shell 前面 → prpl prompt 不會被 shell 搶走。"""
        from sw_core.login_fsm import detect_template

        bridge = mock.MagicMock()
        bridge.rx_tail.return_value = "root@prplOS:/# "
        templates = self._make_templates()
        result = detect_template(bridge, templates, probe_timeout_s=0.01)
        self.assertEqual(result.profile_name, "prpl-template")


if __name__ == "__main__":
    unittest.main()
