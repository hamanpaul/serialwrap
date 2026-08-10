import os
import unittest
from unittest import mock

from sw_core.auth import SessionAuth
from sw_core.config import SessionProfile, UartProfile
from sw_core.login_fsm import (
    LOGIN_FSM_DETAIL_ERRORS,
    ensure_ready,
    matches_login_or_password,
    probe_ready,
)


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


class _FakeBcmBridge:
    """login guard／分流測試用 fake bridge：wait_for_regex 與 rx_tail 皆走預排序列。"""

    def __init__(self, *, wait_results: list, rx_tail_sequence: list[str]) -> None:
        self._wait_results = list(wait_results)
        self._rx_tail_sequence = list(rx_tail_sequence)
        self.sent: list[str] = []

    def clear_rx_buffer(self) -> None:
        pass

    def send_command(self, cmd: str, *, source: str, cmd_id: str | None = None) -> None:
        self.sent.append(cmd)

    def send_secret(self, secret: str) -> None:
        self.sent.append("<secret>")

    def wait_for_regex(self, pattern: str, timeout_s: float) -> bool:
        return self._wait_results.pop(0)

    def rx_tail(self, max_chars: int = 4096) -> str:
        if self._rx_tail_sequence:
            return self._rx_tail_sequence.pop(0)
        return ""


class TestLoginFsmHardening(unittest.TestCase):
    """#174：post_login_cmd 送出前的 login guard、失敗分流、regex 四類樣本。"""

    def _make_bcm_profile(self, **overrides) -> SessionProfile:
        defaults: dict = dict(
            profile_name="brcm-template",
            com="COM1",
            act_no=1,
            alias="brcm+1",
            device_by_id="/dev/serial/by-id/bcm",
            platform="bcm",
            prompt_regex=r"(?m)^(?:.*[^>#\s])?[>#][ \t]*$",
            login_regex=r"(?mi)login:\s*$",
            password_regex=r"(?mi)password:\s*$",
            post_login_cmd="sh",
            ready_probe="echo __READY__${nonce}",
            timeout_s=0.01,
            uart=UartProfile(),
        )
        defaults.update(overrides)
        return SessionProfile(**defaults)

    def test_guard_never_sends_post_login_cmd_into_login_prompt(self) -> None:
        """S2'：prompt_regex 誤配成功，但 rx tail 其實仍在 login prompt——
        絕不能把 post_login_cmd 當帳密送出去，直接回可行動的 LOGIN_REQUIRED。"""
        bridge = _FakeBcmBridge(
            wait_results=[True],  # _probe_prompt 誤配成功
            rx_tail_sequence=["(none) login: "],  # guard 讀到的 rx tail
        )
        profile = self._make_bcm_profile()

        ok, err = probe_ready(bridge, profile)

        self.assertFalse(ok)
        self.assertEqual(err, "LOGIN_REQUIRED")
        self.assertNotIn("sh", bridge.sent)

    def test_post_login_cmd_timeout_reclassified_to_login_required(self) -> None:
        """POST_LOGIN_CMD_TIMEOUT 逾時後 rx tail 已回到 login prompt → 改分流為 LOGIN_REQUIRED。"""
        bridge = _FakeBcmBridge(
            wait_results=[True, False],  # probe 成功、post_login_cmd 逾時
            rx_tail_sequence=["", "login: "],  # guard 通過、逾時後讀到 login prompt
        )
        profile = self._make_bcm_profile()

        ok, err = probe_ready(bridge, profile)

        self.assertFalse(ok)
        self.assertEqual(err, "LOGIN_REQUIRED")
        self.assertIn("sh", bridge.sent)

    def test_post_login_cmd_timeout_stays_when_not_at_login_prompt(self) -> None:
        """POST_LOGIN_CMD_TIMEOUT 逾時但 rx tail 非 login/password prompt → 原碼不變（回歸線）。"""
        bridge = _FakeBcmBridge(
            wait_results=[True, False],
            rx_tail_sequence=["", "some garbage output\n"],
        )
        profile = self._make_bcm_profile()

        ok, err = probe_ready(bridge, profile)

        self.assertFalse(ok)
        self.assertEqual(err, "POST_LOGIN_CMD_TIMEOUT")

    def test_ready_nonce_timeout_reclassified_to_login_required(self) -> None:
        """READY_NONCE_TIMEOUT 逾時後 rx tail 已回到 password prompt → 改分流為 LOGIN_REQUIRED。"""
        bridge = _FakeBcmBridge(
            wait_results=[True, True, False],  # probe 成功、post_login_cmd 成功、nonce 逾時
            rx_tail_sequence=["", "Password: "],
        )
        profile = self._make_bcm_profile()

        ok, err = probe_ready(bridge, profile)

        self.assertFalse(ok)
        self.assertEqual(err, "LOGIN_REQUIRED")

    def test_ready_nonce_timeout_stays_when_not_at_login_prompt(self) -> None:
        """READY_NONCE_TIMEOUT 逾時但 rx tail 非 login/password prompt → 原碼不變（回歸線）。"""
        bridge = _FakeBcmBridge(
            wait_results=[True, True, False],
            rx_tail_sequence=["", "garbage\n"],
        )
        profile = self._make_bcm_profile()

        ok, err = probe_ready(bridge, profile)

        self.assertFalse(ok)
        self.assertEqual(err, "READY_NONCE_TIMEOUT")

    def test_guard_skipped_when_no_post_login_cmd(self) -> None:
        """post_login_cmd 為空的 platform（如 shell）不受 guard 影響（回歸線）。"""
        bridge = mock.MagicMock()
        bridge.wait_for_regex.side_effect = [False, True, True, True, True, True]
        profile = SessionProfile(
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
        with mock.patch.dict(os.environ, {"SW_OPI_U": "haman", "SW_OPI_P": "secret"}, clear=False):
            ok, err = ensure_ready(bridge, profile)
        self.assertTrue(ok)
        self.assertIsNone(err)


class TestMatchesLoginOrPassword(unittest.TestCase):
    """matches_login_or_password() 純函式：login guard 與 interactive_open 共用判準。"""

    def _profile(self, **overrides) -> SessionProfile:
        defaults: dict = dict(
            profile_name="p",
            com="COM0",
            act_no=1,
            alias="p+1",
            device_by_id="/dev/serial/by-id/p",
            platform="bcm",
            login_regex=r"(?mi)login:\s*$",
            password_regex=r"(?mi)password:\s*$",
            uart=UartProfile(),
        )
        defaults.update(overrides)
        return SessionProfile(**defaults)

    def test_matches_getty_hostname_login(self) -> None:
        self.assertTrue(matches_login_or_password("(none) login: ", self._profile()))

    def test_matches_password_prompt(self) -> None:
        self.assertTrue(matches_login_or_password("Password: ", self._profile()))

    def test_no_match_on_unrelated_text(self) -> None:
        self.assertFalse(matches_login_or_password("root@dut:~# ls\n", self._profile()))

    def test_empty_text_no_match(self) -> None:
        self.assertFalse(matches_login_or_password("", self._profile()))

    def test_invalid_regex_tolerated(self) -> None:
        """login_regex 為 invalid regex 時不拋例外，續檢查 password_regex。"""
        profile = self._profile(login_regex="(unterminated", password_regex=r"(?mi)password:\s*$")
        self.assertTrue(matches_login_or_password("Password: ", profile))

    def test_login_fsm_detail_errors_contains_login_required(self) -> None:
        """LOGIN_FSM_DETAIL_ERRORS 涵蓋 LOGIN_REQUIRED 與 POST_LOGIN_CMD_TIMEOUT（回歸線）。"""
        self.assertIn("LOGIN_REQUIRED", LOGIN_FSM_DETAIL_ERRORS)
        self.assertIn("POST_LOGIN_CMD_TIMEOUT", LOGIN_FSM_DETAIL_ERRORS)
        self.assertIn("READY_NONCE_TIMEOUT", LOGIN_FSM_DETAIL_ERRORS)


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
