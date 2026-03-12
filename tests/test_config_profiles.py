import tempfile
import textwrap
import unittest
from pathlib import Path

from sw_core.config import load_profiles


class TestConfigProfiles(unittest.TestCase):
    def test_load_profiles_defaults_alias(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "demo.yaml"
            p.write_text(
                textwrap.dedent(
                    """
                    profile_name: demo
                    targets:
                      - act_no: 3
                        com: COM2
                        device_by_id: /dev/serial/by-id/abc
                        platform: prpl
                    """
                ),
                encoding="utf-8",
            )
            rows = load_profiles(td)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].profile_name, "demo")
            self.assertEqual(rows[0].com, "COM2")
            self.assertEqual(rows[0].alias, "demo+3")
            self.assertEqual(rows[0].uart.baud, 115200)

    def test_profile_template_reused_by_multiple_targets(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "multi.yaml"
            p.write_text(
                textwrap.dedent(
                    """
                    profiles:
                      prpl-template:
                        platform: prpl
                        prompt_regex: "(?m)^root@prplOS:.*# "
                        ready_probe: "echo __READY__${nonce}"
                        uart:
                          baud: 115200
                          data_bits: 8
                          parity: N
                          stop_bits: 1
                    targets:
                      - act_no: 1
                        com: COM0
                        alias: lab+1
                        profile: prpl-template
                        device_by_id: /dev/serial/by-id/tty0
                      - act_no: 2
                        com: COM1
                        alias: lab+2
                        profile: prpl-template
                        device_by_id: /dev/serial/by-id/tty1
                    """
                ),
                encoding="utf-8",
            )
            rows = load_profiles(td)
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0].profile_name, "prpl-template")
            self.assertEqual(rows[1].profile_name, "prpl-template")
            self.assertEqual(rows[0].uart.baud, 115200)
            self.assertEqual(rows[1].uart.baud, 115200)
            self.assertEqual(rows[0].device_by_id, "/dev/serial/by-id/tty0")
            self.assertEqual(rows[1].device_by_id, "/dev/serial/by-id/tty1")
            self.assertEqual(rows[0].prompt_regex, r"(?m)^root@prplOS:.*# ")
            self.assertEqual(rows[1].prompt_regex, r"(?m)^root@prplOS:.*# ")
            self.assertEqual(rows[0].ready_probe, "echo __READY__${nonce}")
            self.assertEqual(rows[1].ready_probe, "echo __READY__${nonce}")

    def test_shell_profile_loads_short_env_fields(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "shell.yaml"
            p.write_text(
                textwrap.dedent(
                    """
                    profiles:
                      opi-shell:
                        platform: shell
                        prompt_regex: ".*[$#] $"
                        login_regex: '(?mi)^.*login:\s*$'
                        user_env: "SW_OPI_U"
                        pass_env: "SW_OPI_P"
                        ready_probe: "echo __READY__${nonce}"
                    targets:
                      - act_no: 3
                        com: COM2
                        alias: shell+3
                        profile: opi-shell
                        device_by_id: /dev/serial/by-id/tty2
                    """
                ),
                encoding="utf-8",
            )
            rows = load_profiles(td)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].profile_name, "opi-shell")
            self.assertEqual(rows[0].platform, "shell")
            self.assertEqual(rows[0].login_regex, r"(?mi)^.*login:\s*$")
            self.assertEqual(rows[0].user_env, "SW_OPI_U")
            self.assertEqual(rows[0].pass_env, "SW_OPI_P")
            self.assertEqual(rows[0].ready_probe, "echo __READY__${nonce}")


if __name__ == "__main__":
    unittest.main()
