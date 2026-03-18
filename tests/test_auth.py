"""tests/test_auth.py — per-session 帳密解析測試。"""

from __future__ import annotations

import os
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from sw_core.auth import SessionAuth, parse_env_file, resolve_session_auth
from sw_core.config import SessionProfile, UartProfile


def _make_shell_profile(
    *,
    env_file: str | None = None,
    user_env: str | None = "SW_OPI_U",
    pass_env: str | None = "SW_OPI_P",
    username: str | None = None,
) -> SessionProfile:
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
        username=username,
        user_env=user_env,
        pass_env=pass_env,
        env_file=env_file,
        uart=UartProfile(),
    )


class TestParseEnvFile(unittest.TestCase):
    def test_basic_key_value(self) -> None:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as fp:
            fp.write("FOO=bar\nBAZ=qux\n")
            path = fp.name
        try:
            env = parse_env_file(path)
            self.assertEqual(env, {"FOO": "bar", "BAZ": "qux"})
        finally:
            os.unlink(path)

    def test_export_prefix_and_quotes(self) -> None:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as fp:
            fp.write("export USER='haman'\n")
            fp.write('export PASS="secret value"\n')
            path = fp.name
        try:
            env = parse_env_file(path)
            self.assertEqual(env["USER"], "haman")
            self.assertEqual(env["PASS"], "secret value")
        finally:
            os.unlink(path)

    def test_comments_and_blank_lines(self) -> None:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as fp:
            fp.write("# 這是註解\n\nKEY=value\n\n# 結尾\n")
            path = fp.name
        try:
            env = parse_env_file(path)
            self.assertEqual(env, {"KEY": "value"})
        finally:
            os.unlink(path)

    def test_empty_value(self) -> None:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as fp:
            fp.write("EMPTY=\n")
            path = fp.name
        try:
            env = parse_env_file(path)
            self.assertEqual(env["EMPTY"], "")
        finally:
            os.unlink(path)


class TestResolveSessionAuth(unittest.TestCase):
    def test_from_env_file(self) -> None:
        """env_file 存在時，帳密從 env_file 解析。"""
        with tempfile.TemporaryDirectory() as td:
            env_path = str(Path(td) / "OPI.env")
            Path(env_path).write_text("SW_OPI_U=haman\nSW_OPI_P=secret\n")

            sp = _make_shell_profile(env_file=env_path)
            with mock.patch.dict(os.environ, {}, clear=True):
                auth = resolve_session_auth(sp)

            self.assertEqual(auth.username, "haman")
            self.assertEqual(auth.password, "secret")

    def test_fallback_to_os_environ(self) -> None:
        """沒有 env_file 時，fallback 到 os.environ（向後相容）。"""
        sp = _make_shell_profile(env_file=None)
        with mock.patch.dict(os.environ, {"SW_OPI_U": "global_user", "SW_OPI_P": "global_pass"}, clear=False):
            auth = resolve_session_auth(sp)

        self.assertEqual(auth.username, "global_user")
        self.assertEqual(auth.password, "global_pass")

    def test_env_file_overrides_os_environ(self) -> None:
        """env_file 的值優先於 os.environ。"""
        with tempfile.TemporaryDirectory() as td:
            env_path = str(Path(td) / "local.env")
            Path(env_path).write_text("SW_OPI_U=local_user\nSW_OPI_P=local_pass\n")

            sp = _make_shell_profile(env_file=env_path)
            with mock.patch.dict(os.environ, {"SW_OPI_U": "global_user", "SW_OPI_P": "global_pass"}, clear=False):
                auth = resolve_session_auth(sp)

            self.assertEqual(auth.username, "local_user")
            self.assertEqual(auth.password, "local_pass")

    def test_env_file_missing_graceful(self) -> None:
        """env_file 不存在時不崩潰，fallback 到 os.environ。"""
        sp = _make_shell_profile(env_file="/tmp/nonexistent-serialwrap-env")
        with mock.patch.dict(os.environ, {"SW_OPI_U": "fallback_u", "SW_OPI_P": "fallback_p"}, clear=False):
            auth = resolve_session_auth(sp)

        self.assertEqual(auth.username, "fallback_u")
        self.assertEqual(auth.password, "fallback_p")

    def test_username_field_as_last_resort(self) -> None:
        """user_env 無值時使用 username 欄位。"""
        sp = _make_shell_profile(user_env=None, username="direct_user")
        with mock.patch.dict(os.environ, {}, clear=True):
            auth = resolve_session_auth(sp)

        self.assertEqual(auth.username, "direct_user")

    def test_no_credentials_returns_none(self) -> None:
        """profile 無帳密設定時回傳空 auth。"""
        sp = _make_shell_profile(user_env=None, pass_env=None, username=None)
        with mock.patch.dict(os.environ, {}, clear=True):
            auth = resolve_session_auth(sp)

        self.assertIsNone(auth.username)
        self.assertIsNone(auth.password)

    def test_partial_env_file_with_os_environ_fallback(self) -> None:
        """env_file 只有 user，password 從 os.environ fallback。"""
        with tempfile.TemporaryDirectory() as td:
            env_path = str(Path(td) / "partial.env")
            Path(env_path).write_text("SW_OPI_U=local_user\n")

            sp = _make_shell_profile(env_file=env_path)
            with mock.patch.dict(os.environ, {"SW_OPI_P": "global_pass"}, clear=True):
                auth = resolve_session_auth(sp)

            self.assertEqual(auth.username, "local_user")
            self.assertEqual(auth.password, "global_pass")


if __name__ == "__main__":
    unittest.main()
