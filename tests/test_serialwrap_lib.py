import unittest

import serialwrap_lib


class TestSerialwrapParsing(unittest.TestCase):
    def test_parse_minicom_args(self) -> None:
        log_path, device, baud = serialwrap_lib._parse_minicom_args(
            "/usr/bin/minicom --color=on -C /home/paul_chen/arc_prj/b-log/mini_COM1_251215-151729.log -D /dev/ttyUSB1 -b 115200"
        )
        self.assertEqual(log_path, "/home/paul_chen/arc_prj/b-log/mini_COM1_251215-151729.log")
        self.assertEqual(device, "/dev/ttyUSB1")
        self.assertEqual(baud, 115200)

    def test_derive_com_from_log(self) -> None:
        com = serialwrap_lib._derive_com("/home/paul_chen/arc_prj/b-log/mini_COM12_251215-000000.log", None)
        self.assertEqual(com, "COM12")

    def test_clean_output_strips_ansi(self) -> None:
        text = "\x1b[31mRED\x1b[0m\r\nOK\r\n"
        self.assertEqual(serialwrap_lib._clean_output(text), "RED\nOK\n")

    def test_find_marker_line_not_command_echo(self) -> None:
        run_id = "deadbeef"
        marker = f"__SERIALWRAP_BEGIN__{run_id}"
        text = f"root# echo {marker}\n{marker}\n"
        norm = serialwrap_lib._normalize_newlines(text)
        begin_pos = serialwrap_lib._find_marker_output_line_end(norm, marker)
        self.assertIsNotNone(begin_pos)
        self.assertEqual(norm[begin_pos:], "")

        begin_pos = serialwrap_lib._find_marker_after_line(norm, marker)
        self.assertIsNotNone(begin_pos)
        self.assertEqual(norm[begin_pos:], f"{marker}\n")

    def test_find_marker_line_start(self) -> None:
        marker = "__SERIALWRAP_END__x"
        text = f"ok\nroot# echo {marker}\n{marker}\n# "
        norm = serialwrap_lib._normalize_newlines(text)
        end_pos = serialwrap_lib._find_marker_output_line_start(norm, marker)
        self.assertIsNotNone(end_pos)
        self.assertEqual(norm[:end_pos], "ok\nroot# echo __SERIALWRAP_END__x\n")

        end_pos = serialwrap_lib._find_marker_line_start(norm, marker)
        self.assertIsNotNone(end_pos)
        self.assertEqual(norm[:end_pos], "ok\n")

    def test_strip_shell_noise(self) -> None:
        cmd = "echo true"
        begin_marker = "__SERIALWRAP_BEGIN__x"
        end_marker = "__SERIALWRAP_END__x"
        text = f"root# {cmd}\ntrue\nroot# echo {end_marker}\n"
        stripped = serialwrap_lib._strip_shell_noise(text, cmd=cmd, begin_marker=begin_marker, end_marker=end_marker)
        self.assertEqual(stripped, "true\n")


if __name__ == "__main__":
    unittest.main()
