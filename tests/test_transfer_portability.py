"""#166 傳輸工具 fallback 與 console 單行預算測試。"""
from __future__ import annotations

import base64
import hashlib
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from sw_core.config import ProfileTemplate, SessionProfile, UartProfile, load_profiles
from sw_core.file_transfer import (
    _SENTINEL_BEGIN,
    _SENTINEL_END,
    pull_file,
    push_file,
)
from sw_core.session_manager import SessionManager
from sw_core.wal import WalWriter
from regression.serialwrap_regression.cases.f07_file_transfer import (
    _target_tools_probe_command,
)


_PROMPT = "root@target:~# "
_PROMPT_RE = r"root@target:~#\s"
_ANCHORED_PROMPT_RE = r"(?m)^root@target:~#\s"
_DECODER_OK = "SW_XFER_DECODER_B64_42"
_ENCODER_OK = "SW_XFER_ENCODER_B64_42"
_CHUNK_OK = "SW_XFER_CHUNK_42"
_MD5_OK = "SW_XFER_MD5_42"
_MV_OK = "SW_XFER_MV_42"
_ENCODER_FAILED = "SW_XFER_ENCODER_FAILED_42"


def _marker_expr(marker: str) -> str:
    """回傳不在命令 echo 中形成完整 marker 的 shell expression。"""
    prefix, suffix = marker.rsplit("===", 1)
    head, word = prefix.rsplit("_", 1)
    return f"'{head}_'{word}'{suffix}==='"


class _PortabilityBridge:
    """可模擬命令 echo、工具能力與 RX snapshot 的 fake transport。"""

    def __init__(self, tools: set[str] | None = None, *, fail_chunk: bool = False,
                 fail_md5: bool = False, fail_mv: bool = False,
                 fail_encoder: bool = False, fail_encoder_partial: bool = False,
                 fail_encoder_no_marker: bool = False,
                 echo_old_markers: bool = False) -> None:
        self.tools = set(tools or ())
        self.fail_chunk = fail_chunk
        self.fail_md5 = fail_md5
        self.fail_mv = fail_mv
        self.fail_encoder = fail_encoder
        self.fail_encoder_partial = fail_encoder_partial
        self.fail_encoder_no_marker = fail_encoder_no_marker
        self.echo_old_markers = echo_old_markers
        self.commands: list[str] = []
        self._rx_text = ""
        self.remote: dict[str, bytes] = {}

    def rx_snapshot_len(self) -> int:
        return len(self._rx_text)

    def rx_text_from(self, from_offset: int) -> str:
        return self._rx_text[from_offset:]

    def wait_for_regex_from(self, pattern: str, from_offset: int, timeout_s: float) -> bool:
        del timeout_s
        return bool(re.search(pattern, self._rx_text[from_offset:]))

    def send_command(self, cmd: str, *, source: str, cmd_id: str | None = None) -> None:
        del source, cmd_id
        self.commands.append(cmd)
        if self.echo_old_markers and "===SW_XFER_" in cmd:
            self._rx_text += f"echo '{_SENTINEL_BEGIN}' && echo '{_SENTINEL_END}'\r\n"
        self._rx_text += cmd + "\r\n"
        self._execute(cmd)
        self._rx_text += "\r\n" + _PROMPT

    def _execute(self, cmd: str) -> None:
        if "SW_XFER_DECODER_B64_" in cmd:
            if "base64" in self.tools:
                self._rx_text += _DECODER_OK.replace("42", str(6 * 7))
            elif "openssl" in self.tools:
                self._rx_text += "SW_XFER_DECODER_OPENSSL_42"
            return
        if "SW_XFER_ENCODER_B64_" in cmd:
            if "base64" in self.tools:
                self._rx_text += _ENCODER_OK
            elif "openssl" in self.tools:
                self._rx_text += "SW_XFER_ENCODER_OPENSSL_42"
            return
        if cmd.startswith("printf '%s'") and "SW_XFER_CHUNK_" in cmd:
            if self.fail_chunk:
                return
            encoded = re.search(r"printf '%s' ('[^']+'|\S+) \|", cmd)
            target = re.search(r"(?:>|>>)\s+('[^']+'|\S+) && printf", cmd)
            if encoded and target:
                path = shlex.split(target.group(1))[0]
                payload = base64.b64decode(shlex.split(encoded.group(1))[0])
                if ">>" in cmd:
                    self.remote[path] = self.remote.get(path, b"") + payload
                else:
                    self.remote[path] = payload
            self._rx_text += _CHUNK_OK
            return
        if cmd.startswith("md5sum ") and "SW_XFER_MD5_" in cmd:
            if self.fail_md5:
                return
            match = re.search(r"md5sum ('[^']+'|\S+)", cmd)
            if match:
                path = shlex.split(match.group(1))[0]
                digest = hashlib.md5(self.remote.get(path, b"")).hexdigest()
                self._rx_text += f"{digest}  {path}"
            # md5sum itself terminates its output line before the shell sentinel.
            self._rx_text += "\r\n" + _MD5_OK
            return
        if cmd.startswith("mv ") and "SW_XFER_MV_" in cmd:
            if self.fail_mv:
                return
            match = re.search(r"mv ('[^']+'|\S+) ('[^']+'|\S+)", cmd)
            if match:
                src = shlex.split(match.group(1))[0]
                dst = shlex.split(match.group(2))[0]
                self.remote[dst] = self.remote.pop(src, b"")
            self._rx_text += _MV_OK
            return
        if "===SW_XFER_" in cmd:
            match = re.search(r"< ('[^']+'|\S+)", cmd)
            if not match:
                return
            path = shlex.split(match.group(1))[0]
            if self.fail_encoder:
                if not self.fail_encoder_no_marker:
                    prefix = "partial-base64" if self.fail_encoder_partial else ""
                    self._rx_text += prefix + _ENCODER_FAILED
                return
            payload = self.remote.get(path, b"")
            self._rx_text += _SENTINEL_BEGIN
            if "openssl enc" in cmd:
                encoded = subprocess.run(
                    ["openssl", "enc", "-base64", "-A"],
                    input=payload,
                    stdout=subprocess.PIPE,
                    check=True,
                ).stdout.decode("ascii")
            else:
                encoded = base64.b64encode(payload).decode("ascii")
            self._rx_text += encoded + _SENTINEL_END


class _LocalShellBridge:
    """以實際 ``/bin/sh`` 執行 production command 的受控 OpenSSL bridge。"""

    def __init__(self) -> None:
        self.commands: list[str] = []
        self._rx_text = ""
        self._tmp = tempfile.TemporaryDirectory()
        self._bin = Path(self._tmp.name) / "bin"
        self._bin.mkdir()
        for name in ("openssl", "md5sum", "mv", "rm", "which"):
            target = shutil.which(name)
            if target is None:
                raise unittest.SkipTest(f"測試環境缺少 {name}")
            os.symlink(target, self._bin / name)
        self._env = {"PATH": str(self._bin)}

    def close(self) -> None:
        self._tmp.cleanup()

    def rx_snapshot_len(self) -> int:
        return len(self._rx_text)

    def rx_text_from(self, from_offset: int) -> str:
        return self._rx_text[from_offset:]

    def wait_for_regex_from(self, pattern: str, from_offset: int, timeout_s: float) -> bool:
        del timeout_s
        return bool(re.search(pattern, self._rx_text[from_offset:]))

    def send_command(self, cmd: str, *, source: str, cmd_id: str | None = None) -> None:
        del source, cmd_id
        self.commands.append(cmd)
        completed = subprocess.run(
            cmd,
            shell=True,
            executable="/bin/sh",
            env=self._env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=5.0,
        )
        self._rx_text += cmd + "\r\n" + completed.stdout + _PROMPT


class TestTransferPortability(unittest.TestCase):
    def _local(self, data: bytes) -> str:
        handle = tempfile.NamedTemporaryFile(delete=False)
        handle.write(data)
        handle.close()
        self.addCleanup(lambda: Path(handle.name).unlink(missing_ok=True))
        return handle.name

    def test_all_tools_missing_rejects_before_data_or_move(self) -> None:
        bridge = _PortabilityBridge()
        local = self._local(b"payload")

        result = push_file(bridge, local, "/tmp/dest", prompt_regex=_PROMPT_RE)

        self.assertEqual(result["error_code"], "TARGET_DECODER_MISSING")
        self.assertEqual(len(bridge.commands), 1)
        self.assertNotIn("SW_XFER_CHUNK_", "".join(bridge.commands))
        self.assertNotIn(" mv ", "".join(bridge.commands))
        self.assertEqual(bridge.remote, {})

    def test_echoing_probe_text_does_not_count_as_probe_success(self) -> None:
        bridge = _PortabilityBridge()
        bridge.send_command = lambda cmd, **kwargs: (
            bridge.commands.append(cmd),
            setattr(bridge, "_rx_text", bridge._rx_text + cmd + "\r\n" + _PROMPT),
        )
        local = self._local(b"payload")

        result = push_file(bridge, local, "/tmp/dest", prompt_regex=_PROMPT_RE)

        self.assertEqual(result["error_code"], "TARGET_DECODER_MISSING")
        self.assertEqual(len(bridge.commands), 1)

    def test_all_encoder_tools_missing_rejects_without_creating_local_file(self) -> None:
        bridge = _PortabilityBridge()
        out = self._local(b"keep")

        result = pull_file(bridge, "/tmp/remote", out, prompt_regex=_PROMPT_RE)

        self.assertEqual(result["error_code"], "TARGET_ENCODER_MISSING")
        self.assertEqual(Path(out).read_bytes(), b"keep")
        self.assertEqual(len(bridge.commands), 1)

    def test_echoing_encoder_probe_text_does_not_count_as_probe_success(self) -> None:
        bridge = _PortabilityBridge()

        def _echo_only(cmd: str, **kwargs: Any) -> None:
            del kwargs
            bridge.commands.append(cmd)
            bridge._rx_text += cmd + "\r\n" + _PROMPT

        bridge.send_command = _echo_only
        out = self._local(b"keep")

        result = pull_file(bridge, "/tmp/remote", out, prompt_regex=_PROMPT_RE)

        self.assertEqual(result["error_code"], "TARGET_ENCODER_MISSING")
        self.assertEqual(Path(out).read_bytes(), b"keep")

    def test_encoder_failure_after_probe_is_explicit_and_preserves_local_file(self) -> None:
        bridge = _PortabilityBridge({"base64", "md5sum"}, fail_encoder=True)
        out = self._local(b"keep")

        result = pull_file(bridge, "/tmp/remote", out, prompt_regex=_PROMPT_RE)

        self.assertEqual(result["error_code"], "TARGET_ENCODER_FAILED")
        self.assertEqual(Path(out).read_bytes(), b"keep")

    def test_encoder_partial_output_then_failure_marker_is_explicit(self) -> None:
        bridge = _PortabilityBridge(
            {"base64", "md5sum"}, fail_encoder=True, fail_encoder_partial=True,
        )
        out = self._local(b"keep")

        result = pull_file(bridge, "/tmp/remote", out, prompt_regex=_PROMPT_RE)

        self.assertEqual(result["error_code"], "TARGET_ENCODER_FAILED")
        self.assertEqual(Path(out).read_bytes(), b"keep")

    def test_missing_encoder_failure_marker_remains_parse_failure(self) -> None:
        bridge = _PortabilityBridge(
            {"base64", "md5sum"}, fail_encoder=True, fail_encoder_no_marker=True,
        )
        out = self._local(b"keep")

        result = pull_file(bridge, "/tmp/remote", out, prompt_regex=_PROMPT_RE)

        self.assertEqual(result["error_code"], "PULL_PARSE_FAILED")
        self.assertEqual(Path(out).read_bytes(), b"keep")

    def test_line_budget_probe_payload_is_bounded_by_source_size(self) -> None:
        bridge = _PortabilityBridge({"base64", "md5sum"})
        local = self._local(b"x")

        with mock.patch(
            "sw_core.file_transfer.base64.b64encode", wraps=base64.b64encode,
        ) as encode:
            result = push_file(
                bridge, local, "/tmp/dest", chunk_size=1_000_000,
                max_console_line_chars=1_000_000, prompt_regex=_PROMPT_RE,
            )

        self.assertTrue(result["ok"], result)
        sizes = [len(call.args[0]) for call in encode.call_args_list]
        self.assertEqual(sizes[0], 1)
        self.assertLessEqual(max(sizes), 1)

    def test_probe_timeout_is_not_reported_as_missing_tool(self) -> None:
        bridge = _PortabilityBridge()
        bridge.wait_for_regex_from = lambda pattern, offset, timeout: False
        local = self._local(b"payload")

        result = push_file(bridge, local, "/tmp/dest", prompt_regex=_PROMPT_RE)

        self.assertEqual(result["error_code"], "TRANSFER_TIMEOUT")

    def test_probe_shell_error_is_not_reported_as_missing_tool(self) -> None:
        bridge = _PortabilityBridge()

        def _syntax_error(cmd: str, **kwargs: Any) -> None:
            del kwargs
            bridge.commands.append(cmd)
            bridge._rx_text += cmd + "\r\nsh: syntax error\r\n" + _PROMPT

        bridge.send_command = _syntax_error
        local = self._local(b"payload")

        result = push_file(bridge, local, "/tmp/dest", prompt_regex=_PROMPT_RE)

        self.assertEqual(result["error_code"], "TRANSFER_PROBE_FAILED")

    def test_only_openssl_roundtrip_binary_and_empty(self) -> None:
        for payload in (bytes(range(256)) + b"\x00binary", b""):
            with self.subTest(size=len(payload)):
                bridge = _LocalShellBridge()
                self.addCleanup(bridge.close)
                local = self._local(payload)
                remote = str(Path(bridge._tmp.name) / "remote file '非ASCII'")
                try:
                    pushed = push_file(
                        bridge, local, remote, prompt_regex=_ANCHORED_PROMPT_RE,
                    )
                    self.assertTrue(pushed["ok"], pushed)
                    out = self._local(b"placeholder")
                    pulled = pull_file(
                        bridge, remote, out, prompt_regex=_ANCHORED_PROMPT_RE,
                    )
                    self.assertTrue(pulled["ok"], pulled)
                    self.assertEqual(Path(out).read_bytes(), payload)
                    self.assertEqual(pulled["md5"], hashlib.md5(payload).hexdigest())
                finally:
                    Path(remote).unlink(missing_ok=True)

    def test_only_openssl_roundtrip_remote_path_with_backslash(self) -> None:
        bridge = _LocalShellBridge()
        self.addCleanup(bridge.close)
        local = self._local(b"backslash filename payload")
        remote = str(Path(bridge._tmp.name) / "remote\\name")
        out = self._local(b"keep")
        try:
            pushed = push_file(
                bridge, local, remote, prompt_regex=_ANCHORED_PROMPT_RE,
            )
            self.assertTrue(pushed["ok"], pushed)
            pulled = pull_file(
                bridge, remote, out, prompt_regex=_ANCHORED_PROMPT_RE,
            )
            self.assertTrue(pulled["ok"], pulled)
            self.assertEqual(Path(out).read_bytes(), b"backslash filename payload")
        finally:
            Path(remote).unlink(missing_ok=True)

    def test_505_budget_bounds_every_push_and_pull_command(self) -> None:
        bridge = _PortabilityBridge({"base64", "md5sum"})
        local = self._local(os.urandom(1024))
        pushed = push_file(
            bridge, local, "/tmp/remote path with spaces", chunk_size=4096,
            max_console_line_chars=505, prompt_regex=_PROMPT_RE,
        )
        self.assertTrue(pushed["ok"], pushed)
        self.assertTrue(all(len(cmd.encode()) <= 505 for cmd in bridge.commands))

        out = self._local(b"")
        pulled = pull_file(
            bridge, "/tmp/remote path with spaces", out,
            max_console_line_chars=505, prompt_regex=_PROMPT_RE,
        )
        self.assertTrue(pulled["ok"], pulled)
        self.assertEqual(Path(out).read_bytes(), Path(local).read_bytes())
        self.assertTrue(all(len(cmd.encode()) <= 505 for cmd in bridge.commands))

    def test_zero_budget_rejects_without_transmit(self) -> None:
        bridge = _PortabilityBridge({"base64", "md5sum"})
        local = self._local(b"payload")

        result = push_file(
            bridge, local, "/tmp/dest", max_console_line_chars=0,
            prompt_regex=_PROMPT_RE,
        )

        self.assertEqual(result["error_code"], "CONSOLE_LINE_LIMIT_TOO_SMALL")
        self.assertEqual(bridge.commands, [])

    def test_positive_too_small_budget_rejects_without_transmit(self) -> None:
        bridge = _PortabilityBridge({"base64", "md5sum"})
        local = self._local(b"payload")

        result = push_file(
            bridge, local, "/tmp/dest", max_console_line_chars=1,
            prompt_regex=_PROMPT_RE,
        )

        self.assertEqual(result["error_code"], "CONSOLE_LINE_LIMIT_TOO_SMALL")
        self.assertEqual(bridge.commands, [])

    def test_later_decoder_failure_is_not_prompt_success(self) -> None:
        bridge = _PortabilityBridge({"base64", "md5sum"}, fail_chunk=True)
        local = self._local(b"payload")

        result = push_file(bridge, local, "/tmp/dest", prompt_regex=_PROMPT_RE)

        self.assertEqual(result["error_code"], "TARGET_DECODER_FAILED")
        self.assertFalse(any(" SW_XFER_MV_" in cmd for cmd in bridge.commands))

    def test_failed_md5_does_not_write_unverified_pull(self) -> None:
        bridge = _PortabilityBridge({"base64", "md5sum"}, fail_md5=True)
        bridge.remote["/tmp/remote"] = b"payload"
        out = self._local(b"sentinel")
        Path(out).unlink()

        result = pull_file(bridge, "/tmp/remote", out, prompt_regex=_PROMPT_RE)

        self.assertEqual(result["error_code"], "CHECKSUM_VERIFY_FAILED")
        self.assertFalse(Path(out).exists())

    def test_md5_filename_collision_does_not_count_as_success(self) -> None:
        bridge = _PortabilityBridge({"base64", "md5sum"}, fail_md5=True)
        remote = "/tmp/SW_XFER_MD5_42_0123456789abcdef0123456789abcdef"
        bridge.remote[remote] = b"payload"
        out = self._local(b"sentinel")
        Path(out).unlink()

        result = pull_file(bridge, remote, out, prompt_regex=_PROMPT_RE)

        self.assertEqual(result["error_code"], "CHECKSUM_VERIFY_FAILED")
        self.assertFalse(Path(out).exists())

    def test_md5_uses_checksum_field_when_filename_is_32_hex(self) -> None:
        bridge = _PortabilityBridge({"base64", "md5sum"})
        remote = "/tmp/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        payload = b"md5 field payload"
        bridge.remote[remote] = payload
        out = self._local(b"")

        result = pull_file(bridge, remote, out, prompt_regex=_PROMPT_RE)

        self.assertTrue(result["ok"], result)
        self.assertEqual(Path(out).read_bytes(), payload)
        self.assertEqual(result["md5"], hashlib.md5(payload).hexdigest())

    def test_mv_filename_collision_does_not_count_as_success(self) -> None:
        bridge = _PortabilityBridge({"base64", "md5sum"}, fail_mv=True)
        local = self._local(b"payload")
        remote = "/tmp/SW_XFER_MV_42"

        result = push_file(bridge, local, remote, prompt_regex=_PROMPT_RE)

        self.assertEqual(result["error_code"], "MOVE_FAILED")

    def test_pull_ignores_literal_marker_in_command_echo(self) -> None:
        bridge = _PortabilityBridge({"base64", "md5sum"}, echo_old_markers=True)
        payload = b"marker echo payload"
        bridge.remote["/tmp/remote"] = payload
        out = self._local(b"")

        result = pull_file(bridge, "/tmp/remote", out, prompt_regex=_PROMPT_RE)

        self.assertTrue(result["ok"], result)
        self.assertEqual(Path(out).read_bytes(), payload)


class TestTransferProfileBudget(unittest.TestCase):
    def test_valid_profile_budget_is_loaded(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            Path(td, "profile.yaml").write_text(
                "profiles:\n  p:\n    max_console_line_chars: 505\ntargets: []\n",
                encoding="utf-8",
            )
            result = load_profiles(td)
            self.assertEqual(result.templates[0].max_console_line_chars, 505)

    def test_invalid_profile_budget_is_rejected(self) -> None:
        for value in (True, False, 0, -1, "505", 1.5):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as td:
                Path(td, "profile.yaml").write_text(
                    f"profiles:\n  p:\n    max_console_line_chars: {value!r}\ntargets: []\n",
                    encoding="utf-8",
                )
                with self.assertRaises(ValueError):
                    load_profiles(td)

    def test_yaml_target_budget_inherits_overrides_and_clears_template(self) -> None:
        cases = (
            ("", 505),
            ("    max_console_line_chars: 777\n", 777),
            ("    max_console_line_chars: null\n", None),
        )
        for target_field, expected in cases:
            with self.subTest(target_field=target_field), tempfile.TemporaryDirectory() as td:
                Path(td, "profile.yaml").write_text(
                    "profiles:\n"
                    "  p:\n"
                    "    max_console_line_chars: 505\n"
                    "targets:\n"
                    "  - profile: p\n"
                    "    com: COM0\n"
                    "    device_by_id: /dev/serial/by-id/a\n"
                    f"{target_field}",
                    encoding="utf-8",
                )
                result = load_profiles(td)
                self.assertEqual(result.profiles[0].max_console_line_chars, expected)

    def test_invalid_yaml_target_budget_is_rejected(self) -> None:
        for value in (True, 0, -1, "777", 1.5):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as td:
                Path(td, "profile.yaml").write_text(
                    "profiles:\n"
                    "  p:\n"
                    "    max_console_line_chars: 505\n"
                    "targets:\n"
                    "  - profile: p\n"
                    "    com: COM0\n"
                    "    device_by_id: /dev/serial/by-id/a\n"
                    f"    max_console_line_chars: {value!r}\n",
                    encoding="utf-8",
                )
                with self.assertRaises(ValueError):
                    load_profiles(td)

    def test_dynamic_and_rematerialized_profiles_keep_line_budget(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            first = ProfileTemplate(profile_name="first", max_console_line_chars=505)
            second = ProfileTemplate(profile_name="second", max_console_line_chars=777)
            manager = SessionManager(
                [], WalWriter(wal_dir=td), templates=[first, second],
                on_ready=lambda _sid: None, on_detached=lambda _sid: None,
            )
            with manager._lock:
                session = manager._session_from_template(first, "/dev/serial/by-id/a")
                self.assertEqual(session.profile.max_console_line_chars, 505)
                self.assertTrue(manager._rematerialize_profile_locked(session, second, "detected"))
                self.assertEqual(session.profile.max_console_line_chars, 777)

    def test_session_manager_passes_profile_budget_to_push_and_pull(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            profile = SessionProfile(
                profile_name="p", com="COM0", act_no=1, alias="p+1",
                device_by_id="/dev/serial/by-id/a", platform="prpl",
                max_console_line_chars=505,
            )
            manager = SessionManager(
                [profile], WalWriter(wal_dir=td),
                on_ready=lambda _sid: None, on_detached=lambda _sid: None,
            )
            session = manager._sessions["p:COM0"]
            session.state = "READY"
            session.bridge = object()
            with mock.patch(
                "sw_core.file_transfer.push_file", return_value={"ok": True}
            ) as push:
                result = manager.file_push(
                    "COM0", local_path="/tmp/src", remote_path="/tmp/dst",
                )
            self.assertTrue(result["ok"])
            self.assertEqual(push.call_args.kwargs["max_console_line_chars"], 505)
            with mock.patch(
                "sw_core.file_transfer.pull_file", return_value={"ok": True}
            ) as pull:
                result = manager.file_pull("COM0", remote_path="/tmp/src")
            self.assertTrue(result["ok"])
            self.assertEqual(pull.call_args.kwargs["max_console_line_chars"], 505)


class TestOpenSSLProbeContract(unittest.TestCase):
    def test_f07_probe_executes_real_openssl_and_emits_lf_before_505_bytes(self) -> None:
        bridge = _LocalShellBridge()
        self.addCleanup(bridge.close)
        command = _target_tools_probe_command()

        self.assertLessEqual(len(command.encode("utf-8")), 505)
        completed = subprocess.run(
            command,
            shell=True,
            executable="/bin/sh",
            env=bridge._env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=5.0,
        )

        self.assertEqual(completed.returncode, 0, completed.stdout)
        self.assertEqual(completed.stdout, "TOOLS_OPENSSL_42\n")

    def test_actual_openssl_decoder_handles_binary_and_empty(self) -> None:
        for payload in (b"", bytes(range(256))):
            encoded = base64.b64encode(payload)
            result = subprocess.run(
                ["openssl", "enc", "-base64", "-d", "-A"],
                input=encoded,
                stdout=subprocess.PIPE,
                check=True,
            )
            self.assertEqual(result.stdout, payload)


if __name__ == "__main__":
    unittest.main()
