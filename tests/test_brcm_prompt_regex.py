"""#174 — 出貨 brcm-template prompt_regex／login_regex 回歸測試。

舊版 ``prompt_regex: "(?m)[>#]\\s*$"`` 未錨定行首，會被 BDK login banner 的
``#####`` 裝飾線與 CEVENT 洪流行誤配成 prompt，讓 login FSM 整段跳過、把
``post_login_cmd`` 當帳密送進 login prompt。本檔直接載入出貨
``sw_core/assets/profiles/default.yaml``，對四類真實樣本釘死行為，
防止未來又漂移回寬鬆版本。
"""
from __future__ import annotations

import os
import unittest

from sw_core.config import load_profiles

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


if __name__ == "__main__":
    unittest.main()
