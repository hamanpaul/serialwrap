"""命令回顯被終端折行拆開時的 stdout 擷取（WP-252）。

serial console 的 shell 回顯打到第 80 欄會折行：BusyBox ash 在回顯中插入
``\\r\\r\\n``，bash readline 補 ``" \\r"``。過去 ``_extract_command_stdout`` 只逐行比對
``endswith(command)``，折行後每一行都只有命令的一部分，整段回顯留在 stdout。
"""
from __future__ import annotations

import unittest

import pytest

from sw_core.session_manager import SessionManager, _strip_wrapped_command_echo

# Rust 重構 P0 T14 平台／資源 marker：純字串處理，不碰 PTY／行程（併入 main #237 時補標）
pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]

PROMPT_RE = r"(?m)^root@prplOS:.*# "
PROMPT = "root@prplOS:~# "
CMD96 = "echo " + "x" * 50 + " " + "y" * 40   # 96 字元，與實機重現同長度
CMD200 = "echo " + "a" * 60 + " " + "b" * 60 + " " + "c" * 73


def _extract(raw: str, command: str) -> str:
    mgr = SessionManager.__new__(SessionManager)
    return mgr._extract_command_stdout(raw, command, PROMPT_RE)


class TestWrappedEchoStrip(unittest.TestCase):
    def test_busybox_wrap_is_stripped(self) -> None:
        raw = CMD96[:65] + "\r\r\n" + CMD96[65:] + "\r\nOUT\r\n" + PROMPT
        self.assertEqual(_extract(raw, CMD96), "OUT")

    def test_three_line_wrap_is_stripped(self) -> None:
        raw = (CMD200[:65] + "\r\r\n" + CMD200[65:145] + "\r\r\n" + CMD200[145:]
               + "\r\nOUT\r\n" + PROMPT)
        self.assertEqual(_extract(raw, CMD200), "OUT")

    def test_readline_space_wrap_is_stripped(self) -> None:
        raw = CMD96[:79] + " \r" + CMD96[79:] + "\r\nOUT\r\n" + PROMPT
        self.assertEqual(_extract(raw, CMD96), "OUT")

    def _assert_readline_wraps_stripped(self, cuts: tuple[int, ...]) -> None:
        parts = [CMD200[start:end] for start, end in zip((0, *cuts), (*cuts, len(CMD200)))]
        raw = " \r".join(parts) + "\r\nOUT\r\n" + PROMPT
        self.assertEqual(_extract(raw, CMD200), "OUT")

    def test_two_readline_space_wraps_are_stripped(self) -> None:
        self._assert_readline_wraps_stripped((79, 159))

    def test_three_readline_space_wraps_are_stripped(self) -> None:
        self._assert_readline_wraps_stripped((50, 100, 150))

    def test_leading_blank_lines_do_not_hide_readline_wrap_markers(self) -> None:
        """前置空行後的單次／多次 readline 折行仍須完整剝除回顯。"""
        for prefix in ("\n", "\r\n"):
            for cuts in ((79,), (79, 159)):
                with self.subTest(prefix=repr(prefix), cuts=cuts):
                    parts = [CMD200[start:end] for start, end in zip((0, *cuts), (*cuts, len(CMD200)))]
                    raw = prefix + " \r".join(parts) + "\r\nOUT\r\n" + PROMPT
                    self.assertEqual(_extract(raw, CMD200), "OUT")

    def test_readline_marker_too_close_to_command_start_is_not_accepted(self) -> None:
        command = "x" * 60
        raw = command[:10] + " \r" + command[10:] + "\r\nOUT\r\n" + PROMPT
        self.assertEqual(_extract(raw, command), command[:10] + " " + command[10:] + "\nOUT")

    def test_prompt_prefix_does_not_count_toward_readline_marker_distance(self) -> None:
        command = "x" * 60
        raw = PROMPT + command[:10] + " \r" + command[10:] + "\r\nOUT\r\n" + PROMPT
        self.assertEqual(_extract(raw, command), PROMPT + command[:10] + " " + command[10:] + "\nOUT")

    def test_prompt_prefix_on_first_echo_line(self) -> None:
        raw = PROMPT + CMD96[:65] + "\r\r\n" + CMD96[65:] + "\r\nOUT\r\n" + PROMPT
        self.assertEqual(_extract(raw, CMD96), "OUT")

    def test_short_command_unchanged(self) -> None:
        raw = "cut -d. -f1 /proc/uptime\r\n65\r\n" + PROMPT
        self.assertEqual(_extract(raw, "cut -d. -f1 /proc/uptime"), "65")

    def test_single_line_output_matching_command_without_spaces_is_kept(self) -> None:
        """無回顯時，輸出即使省略命令空白後與命令相同，也不是折行回顯。"""
        raw = "catfile\r\n" + PROMPT
        self.assertEqual(_extract(raw, "cat file"), "catfile")

    def test_normal_newline_output_is_not_treated_as_terminal_wrap(self) -> None:
        """只有 BusyBox 的雙 CR 折行標記可授權跨行拼接，普通 stdout 換行須保留。"""
        raw = "echo x \r\ny\r\n" + PROMPT
        self.assertEqual(_extract(raw, "echo x y"), "echo x \ny")

    def test_no_echo_keeps_output(self) -> None:
        raw = "line1\r\nline2\r\n" + PROMPT
        self.assertEqual(_extract(raw, CMD96), "line1\nline2")

    def test_command_text_not_starting_on_first_line_is_kept(self) -> None:
        """命令字樣不是從第一行開始（不是回顯，是輸出）時不剝。"""
        raw = "header\r\n" + CMD96[:65] + "\r\n" + CMD96[65:] + "\r\n" + PROMPT
        self.assertEqual(_extract(raw, CMD96), "header\n" + CMD96[:65] + "\n" + CMD96[65:])

    def test_single_line_echo_is_not_stripped_twice(self) -> None:
        """單行回顯已剝掉時，不再把後面恰好拼得出命令的輸出當回顯。"""
        raw = CMD96 + "\r\n" + CMD96[:65] + "\r\n" + CMD96[65:] + "\r\n" + PROMPT
        self.assertEqual(_extract(raw, CMD96), CMD96[:65] + "\n" + CMD96[65:])

    def test_helper_handles_empty_inputs(self) -> None:
        self.assertEqual(_strip_wrapped_command_echo([], CMD96), [])
        self.assertEqual(_strip_wrapped_command_echo(["a", "b"], "   "), ["a", "b"])


if __name__ == "__main__":
    unittest.main()
