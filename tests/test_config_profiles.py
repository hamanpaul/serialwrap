import tempfile
import textwrap
import unittest
from pathlib import Path

from sw_core.config import ProfileTemplate, load_profiles


class TestBootloaderPrompts(unittest.TestCase):
    """TDD：bootloader_prompts 欄位與 constants 常數測試。"""

    # --- B1: constants ---

    def test_constants_max_recovery_lease_s(self) -> None:
        """MAX_RECOVERY_LEASE_S 應為 120.0。"""
        from sw_core.constants import MAX_RECOVERY_LEASE_S

        self.assertEqual(MAX_RECOVERY_LEASE_S, 120.0)

    def test_constants_bootloader_rx_tail_bytes(self) -> None:
        """BOOTLOADER_RX_TAIL_BYTES 應為 512。"""
        from sw_core.constants import BOOTLOADER_RX_TAIL_BYTES

        self.assertEqual(BOOTLOADER_RX_TAIL_BYTES, 512)

    # --- B2: ProfileTemplate default ---

    def test_profile_template_default_bootloader_prompts_empty(self) -> None:
        """ProfileTemplate 預設 bootloader_prompts 應為空 tuple（不可變）。"""
        tpl = ProfileTemplate(profile_name="x")
        self.assertIsInstance(tpl.bootloader_prompts, tuple)
        self.assertEqual(tpl.bootloader_prompts, ())

    # --- B2: YAML parser with bootloader_prompts ---

    def test_yaml_with_bootloader_prompts_parsed(self) -> None:
        """YAML 含 bootloader_prompts list 應解析成 tuple[str, ...] 到 ProfileTemplate。"""
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "brcm.yaml"
            p.write_text(
                textwrap.dedent(
                    """
                    profiles:
                      brcm-template:
                        platform: bcm
                        prompt_regex: "(?m)[>#]\\\\s*$"
                        bootloader_prompts:
                          - "^=> $"
                          - "^Marvell>> $"
                    targets:
                      - act_no: 1
                        com: COM0
                        alias: brcm+1
                        profile: brcm-template
                        device_by_id: /dev/serial/by-id/tty0
                    """
                ),
                encoding="utf-8",
            )
            result = load_profiles(td)
            tpl = next(t for t in result.templates if t.profile_name == "brcm-template")
            self.assertIsInstance(tpl.bootloader_prompts, tuple)
            self.assertEqual(tpl.bootloader_prompts, ("^=> $", "^Marvell>> $"))

    def test_yaml_without_bootloader_prompts_yields_empty(self) -> None:
        """YAML 不含 bootloader_prompts 應使 ProfileTemplate.bootloader_prompts 為空 tuple。"""
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "prpl.yaml"
            p.write_text(
                textwrap.dedent(
                    """
                    profiles:
                      prpl-template:
                        platform: prpl
                        prompt_regex: "(?m)^root@prplOS:.*# "
                    targets:
                      - act_no: 1
                        com: COM0
                        alias: prpl+1
                        profile: prpl-template
                        device_by_id: /dev/serial/by-id/tty0
                    """
                ),
                encoding="utf-8",
            )
            result = load_profiles(td)
            tpl = next(t for t in result.templates if t.profile_name == "prpl-template")
            self.assertIsInstance(tpl.bootloader_prompts, tuple)
            self.assertEqual(tpl.bootloader_prompts, ())

    def test_session_profile_propagates_bootloader_prompts_as_tuple(self) -> None:
        """SessionProfile.bootloader_prompts 應為 tuple，且從 template 正確傳播。"""
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "brcm.yaml"
            p.write_text(
                textwrap.dedent(
                    """
                    profiles:
                      brcm-template:
                        platform: bcm
                        prompt_regex: "(?m)[>#]\\\\s*$"
                        bootloader_prompts:
                          - "^CFE> $"
                          - "^=> $"
                    targets:
                      - act_no: 1
                        com: COM0
                        alias: brcm+1
                        profile: brcm-template
                        device_by_id: /dev/serial/by-id/tty0
                    """
                ),
                encoding="utf-8",
            )
            rows = load_profiles(td).profiles
            self.assertEqual(len(rows), 1)
            sp = rows[0]
            self.assertIsInstance(sp.bootloader_prompts, tuple)
            self.assertEqual(sp.bootloader_prompts, ("^CFE> $", "^=> $"))

    def test_yaml_bootloader_prompts_rejects_non_str_elements(self) -> None:
        """YAML bootloader_prompts 中 int/null/dict 元素應被過濾，只保留 str。"""
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "mixed.yaml"
            p.write_text(
                textwrap.dedent(
                    """
                    profiles:
                      mixed-template:
                        platform: bcm
                        prompt_regex: "(?m)[>#]\\\\s*$"
                        bootloader_prompts:
                          - 42
                          - null
                          - x: "y"
                          - "^=> $"
                    targets:
                      - act_no: 1
                        com: COM0
                        alias: mixed+1
                        profile: mixed-template
                        device_by_id: /dev/serial/by-id/tty0
                    """
                ),
                encoding="utf-8",
            )
            result = load_profiles(td)
            tpl = next(t for t in result.templates if t.profile_name == "mixed-template")
            # int(42)、null、dict 均應被過濾，只保留 str "^=> $"
            self.assertEqual(tpl.bootloader_prompts, ("^=> $",))


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
            rows = load_profiles(td).profiles
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
            rows = load_profiles(td).profiles
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
                      op3-template:
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
                        profile: op3-template
                        device_by_id: /dev/serial/by-id/tty2
                    """
                ),
                encoding="utf-8",
            )
            rows = load_profiles(td).profiles
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].profile_name, "op3-template")
            self.assertEqual(rows[0].platform, "shell")
            self.assertEqual(rows[0].login_regex, r"(?mi)^.*login:\s*$")
            self.assertEqual(rows[0].user_env, "SW_OPI_U")
            self.assertEqual(rows[0].pass_env, "SW_OPI_P")
            self.assertEqual(rows[0].ready_probe, "echo __READY__${nonce}")

    def test_shell_profile_resolves_env_file_relative_to_yaml(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "shell.yaml"
            p.write_text(
                textwrap.dedent(
                    """
                    profiles:
                      op3-template:
                        platform: shell
                        user_env: "SW_OPI_U"
                        pass_env: "SW_OPI_P"
                        env_file: "OPI.env"
                    targets:
                      - act_no: 3
                        com: COM2
                        alias: shell+3
                        profile: op3-template
                        device_by_id: /dev/serial/by-id/tty2
                    """
                ),
                encoding="utf-8",
            )

            rows = load_profiles(td).profiles

            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].env_file, str(Path(td) / "OPI.env"))

    def test_target_can_override_template_env_file(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "shell.yaml"
            p.write_text(
                textwrap.dedent(
                    """
                    profiles:
                      op3-template:
                        platform: shell
                        env_file: "OPI.env"
                    targets:
                      - act_no: 3
                        com: COM2
                        alias: shell+3
                        profile: op3-template
                        env_file: "special.env"
                        device_by_id: /dev/serial/by-id/tty2
                    """
                ),
                encoding="utf-8",
            )

            rows = load_profiles(td).profiles

            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].env_file, str(Path(td) / "special.env"))

    def test_passthrough_profile_loads_without_ready_constraints(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "others.yaml"
            p.write_text(
                textwrap.dedent(
                    """
                    profiles:
                      others-template:
                        platform: passthrough
                        prompt_regex: ".*"
                        login_regex: "$^"
                        password_regex: "$^"
                        ready_probe: ""
                    targets:
                      - act_no: 4
                        com: COM3
                        alias: others+4
                        profile: others-template
                        device_by_id: /dev/serial/by-id/tty3
                    """
                ),
                encoding="utf-8",
            )
            rows = load_profiles(td).profiles
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].profile_name, "others-template")
            self.assertEqual(rows[0].platform, "passthrough")
            self.assertEqual(rows[0].prompt_regex, ".*")
            self.assertEqual(rows[0].ready_probe, "")


if __name__ == "__main__":
    unittest.main()
