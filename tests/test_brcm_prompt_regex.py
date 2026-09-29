"""#174 — 出貨 brcm-template prompt_regex／login_regex 回歸測試。

舊版 ``prompt_regex: "(?m)[>#]\\s*$"`` 未錨定行首，會被 BDK login banner 的
``#####`` 裝飾線與 CEVENT 洪流行誤配成 prompt，讓 login FSM 整段跳過、把
``post_login_cmd`` 當帳密送進 login prompt。本檔直接載入出貨
``sw_core/assets/profiles/default.yaml``，對四類真實樣本釘死行為，
防止未來又漂移回寬鬆版本。
"""
from __future__ import annotations

import os
import re
import unittest
from unittest import mock

from sw_core.auth import SessionAuth
from sw_core.config import SessionProfile, load_profiles
from sw_core.login_fsm import detect_template, ensure_ready

_ASSETS_PROFILE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "sw_core", "assets", "profiles",
)


def _load_brcm_template():
    result = load_profiles(_ASSETS_PROFILE_DIR)
    tpl = next((t for t in result.templates if t.profile_name == "brcm-template"), None)
    assert tpl is not None, "出貨 profile 缺少 brcm-template"
    return tpl


class TestShippedBrcmPromptRegex(unittest.TestCase):
    """prompt_regex 四類樣本（#174 issue 原文釘死清單）。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tpl = _load_brcm_template()

    def test_bdk_bare_hash_prompt_matches(self) -> None:
        import re
        self.assertIsNotNone(re.search(self.tpl.prompt_regex, "# "))

    def test_root_shell_prompt_matches(self) -> None:
        import re
        self.assertIsNotNone(re.search(self.tpl.prompt_regex, "root@host:~# "))

    def test_indented_bdk_prompts_match(self) -> None:
        """#224：BDK 提示符的前導水平空白不得造成登入逾時。"""
        for sample in ("> ", " > ", "\t>\t", " # ", "\t#\t", "\r\n > ", "\n\t> "):
            with self.subTest(sample=repr(sample)):
                self.assertIsNotNone(re.search(self.tpl.prompt_regex, sample))

    def test_indented_decoration_and_whitespace_do_not_match(self) -> None:
        """#224：接受前導空白後仍須保留 #174 的 banner 排除防線。"""
        for sample in (" ##### ", "\t>>>>\t", " #># ", " >#> ", " \t ", "\r\n ##### "):
            with self.subTest(sample=repr(sample)):
                self.assertIsNone(re.search(self.tpl.prompt_regex, sample))

    def test_banner_decoration_line_no_match(self) -> None:
        import re
        self.assertIsNone(re.search(self.tpl.prompt_regex, "#########################################"))

    def test_cevent_flood_line_no_match(self) -> None:
        import re
        cevent_line = "... wl1 00:00:00:00:00:00 24 0000 0000 0000 DRIVER -- E_RADIO/40"
        self.assertIsNone(re.search(self.tpl.prompt_regex, cevent_line))

    def test_full_login_banner_block_no_match(self) -> None:
        """完整 banner 區塊（裝飾線＋標題＋login prompt）也不得誤配（防線 D）。"""
        import re
        banner = (
            "#########################################\n"
            "#   ... Broadband Router ...            #\n"
            "#########################################\n"
            "(none) login: "
        )
        self.assertIsNone(re.search(self.tpl.prompt_regex, banner))


class TestShippedBrcmLoginRegex(unittest.TestCase):
    """login_regex 不得錨定行首，須容忍 getty 的 "<hostname> login: " 格式（#174 S4）。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tpl = _load_brcm_template()

    def test_matches_getty_hostname_login(self) -> None:
        import re
        self.assertIsNotNone(re.search(self.tpl.login_regex, "(none) login: "))

    def test_matches_bare_login(self) -> None:
        import re
        self.assertIsNotNone(re.search(self.tpl.login_regex, "login: "))


class _WhitespaceBrcmBridge:
    """以真實 regex 比對模擬 UART 回應，涵蓋登入與 BCM CLI → sh 的切換。"""

    def __init__(self, *, needs_login: bool, newline: str, padding: str) -> None:
        self.needs_login = needs_login
        self.newline = newline
        self.padding = padding
        self.rx = ""
        self.commands: list[str] = []
        self.secrets: list[str] = []

    def _prompt(self, marker: str) -> str:
        return f"{self.newline}{self.padding}{marker} "

    def clear_rx_buffer(self) -> None:
        self.rx = ""

    def send_command(self, cmd: str, *, source: str) -> None:
        self.commands.append(cmd)
        if cmd == "":
            self.rx = "(none) login: " if self.needs_login else self._prompt(">")
        elif cmd == "regression-user" and self.needs_login:
            self.rx = "Password: "
        elif cmd == "sh" and not self.needs_login:
            self.rx = self._prompt("#")
        elif cmd.startswith("echo __READY__") and not self.needs_login:
            self.rx = cmd.removeprefix("echo ") + self._prompt("#")
        else:
            raise AssertionError(f"未預期的登入流程命令：{cmd!r}")

    def send_secret(self, secret: str) -> None:
        self.secrets.append(secret)
        self.needs_login = False
        self.rx = self._prompt(">")

    def wait_for_regex(self, pattern: str, timeout_s: float) -> bool:
        return re.search(pattern, self.rx) is not None

    def rx_tail(self, max_chars: int = 4096) -> str:
        return self.rx[-max_chars:]


class TestShippedBrcmWhitespaceFlow(unittest.TestCase):
    """#224：直接載入出貨 YAML，驗證前導空白下的 auto-login／READY 流程。"""

    def _profile(self) -> SessionProfile:
        return SessionProfile(
            **vars(_load_brcm_template()),
            com="COM1",
            act_no=2,
            alias="sta",
            device_by_id="COM7",
        )

    def test_ready_from_logged_in_or_login_prompt(self) -> None:
        auth = SessionAuth(username="regression-user", password="regression-pass")
        for needs_login in (False, True):
            for newline in ("\n", "\r\n"):
                for padding in (" ", "\t"):
                    with self.subTest(needs_login=needs_login, newline=repr(newline), padding=repr(padding)):
                        bridge = _WhitespaceBrcmBridge(
                            needs_login=needs_login, newline=newline, padding=padding,
                        )
                        ok, err = ensure_ready(bridge, self._profile(), auth=auth)
                        self.assertTrue(ok, err)
                        self.assertIsNone(err)
                        self.assertEqual(bridge.commands[0], "")
                        self.assertEqual(bridge.commands[-2], "sh")
                        self.assertTrue(bridge.commands[-1].startswith("echo __READY__"))
                        self.assertEqual(
                            bridge.commands[1:-2], [auth.username] if needs_login else [],
                        )
                        self.assertEqual(bridge.secrets, [auth.password] if needs_login else [])

    def test_detects_template_from_indented_bdk_prompt(self) -> None:
        """同一份出貨 template 的自動偵測也必須接受真實 UART 樣本。"""
        bridge = mock.MagicMock()
        bridge.rx_tail.return_value = "\r\n > "
        templates = load_profiles(_ASSETS_PROFILE_DIR).templates
        detected = detect_template(bridge, templates, probe_timeout_s=0)
        self.assertIsNotNone(detected)
        self.assertEqual(detected.profile_name, "brcm-template")


if __name__ == "__main__":
    unittest.main()
