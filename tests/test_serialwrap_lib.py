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


if __name__ == "__main__":
    unittest.main()

