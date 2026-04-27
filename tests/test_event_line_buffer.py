from __future__ import annotations
import unittest

from sw_core.event_engine.line_buffer import LineBuffer, strip_ansi


class TestLineBuffer(unittest.TestCase):
    def test_simple_line(self) -> None:
        lb = LineBuffer()
        lines = lb.feed(b"hello\n")
        self.assertEqual(lines, ["hello"])

    def test_partial_then_complete(self) -> None:
        lb = LineBuffer()
        self.assertEqual(lb.feed(b"part"), [])
        self.assertEqual(lb.feed(b"ial\n"), ["partial"])

    def test_multiple_lines_in_one_chunk(self) -> None:
        lb = LineBuffer()
        self.assertEqual(lb.feed(b"a\nb\nc\n"), ["a", "b", "c"])

    def test_crlf_normalized(self) -> None:
        lb = LineBuffer()
        self.assertEqual(lb.feed(b"alpha\r\nbeta\r\n"), ["alpha", "beta"])

    def test_max_line_truncates_and_emits(self) -> None:
        lb = LineBuffer(max_line_bytes=8)
        out = lb.feed(b"abcdefghIJK\n")
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0].startswith("abcdefgh"))
        self.assertIn("truncated", out[0])

    def test_strip_ansi_basic(self) -> None:
        self.assertEqual(strip_ansi("\x1b[31mred\x1b[0m"), "red")
        self.assertEqual(strip_ansi("\x1b[?2004hpaste mode"), "paste mode")

    def test_strip_ansi_in_buffer(self) -> None:
        lb = LineBuffer()
        out = lb.feed(b"\x1b[31mred line\x1b[0m\n")
        self.assertEqual(out, ["red line"])


if __name__ == "__main__":
    unittest.main()
