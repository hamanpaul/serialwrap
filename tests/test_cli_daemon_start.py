from __future__ import annotations

import argparse
import os
import tempfile
import unittest
from unittest import mock

from sw_core import cli


class TestCliDaemonStart(unittest.TestCase):
    def test_load_daemon_start_env_sources_env_file(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            env_file = os.path.join(td, "OPI.env")
            with open(env_file, "w", encoding="utf-8") as fp:
                fp.write("SW_OPI_U=haman\n")
                fp.write("export SW_OPI_P='secret value'\n")

            env, loaded = cli._load_daemon_start_env(env_file)

        self.assertEqual(loaded, env_file)
        self.assertEqual(env["SW_OPI_U"], "haman")
        self.assertEqual(env["SW_OPI_P"], "secret value")

    def test_load_daemon_start_env_keeps_current_env_when_missing(self) -> None:
        with mock.patch.dict(os.environ, {"SERIALWRAP_TEST_FLAG": "1"}, clear=False):
            env, loaded = cli._load_daemon_start_env("/tmp/serialwrap-missing-opi-env")

        self.assertIsNone(loaded)
        self.assertEqual(env["SERIALWRAP_TEST_FLAG"], "1")

    def test_resolve_daemon_start_env_files_uses_legacy_and_profile_env(self) -> None:
        """不設 SERIALWRAP_DAEMON_ENV_FILE 時，先載入 legacy，再載入 profile_dir/OPI.env。"""
        with mock.patch.dict(os.environ, {}, clear=True):
            env_files = cli._resolve_daemon_start_env_files("/tmp/any-profile-dir")

        self.assertEqual(env_files, ["~/OPI.env", "/tmp/any-profile-dir/OPI.env"])

    def test_resolve_daemon_start_env_files_explicit_override_wins(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            profile = os.path.join(td, "default.yaml")
            with open(profile, "w", encoding="utf-8") as fp:
                fp.write(
                    """
profiles:
  op3-template:
    platform: shell
    env_file: OPI.env
targets:
  - act_no: 3
    com: COM2
    profile: op3-template
    device_by_id: /dev/serial/by-id/tty2
""".lstrip()
                )

            with mock.patch.dict(os.environ, {"SERIALWRAP_DAEMON_ENV_FILE": "/tmp/global.env"}, clear=True):
                env_files = cli._resolve_daemon_start_env_files(td)

        self.assertEqual(env_files, ["/tmp/global.env"])

    def test_run_daemon_start_passes_loaded_env_to_daemon(self) -> None:
        args = argparse.Namespace(
            profile_dir="/tmp/profiles",
            socket="/tmp/serialwrap.sock",
            lock="/tmp/serialwrap.lock",
            foreground=False,
        )
        proc = mock.Mock(pid=4321, returncode=None)
        proc.poll.return_value = None

        with (
            mock.patch("sw_core.cli._resolve_daemon_start_env_files", return_value=["/tmp/OPI.env"]),
            mock.patch("sw_core.cli._load_daemon_start_env_files", return_value=({"SW_OPI_U": "haman"}, ["/tmp/OPI.env"])),
            mock.patch("sw_core.cli.subprocess.Popen", return_value=proc) as popen,
            mock.patch("sw_core.cli.rpc_call", side_effect=[{"ok": True}, {"ok": True, "warnings": ["no_profiles_loaded"]}]),
            mock.patch("sw_core.cli.time.sleep"),
            mock.patch("sw_core.cli._print") as printer,
        ):
            rc = cli._run_daemon_start(args)

        self.assertEqual(rc, 0)
        self.assertEqual(popen.call_args.kwargs["env"]["SW_OPI_U"], "haman")
        payload = printer.call_args.args[0]
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["pid"], 4321)
        self.assertEqual(payload["env_files"], ["/tmp/OPI.env"])
        self.assertEqual(payload["warnings"], ["no_profiles_loaded"])

    def test_run_daemon_start_reports_env_source_failure(self) -> None:
        args = argparse.Namespace(
            profile_dir="/tmp/profiles",
            socket="/tmp/serialwrap.sock",
            lock="/tmp/serialwrap.lock",
            foreground=False,
        )

        with (
            mock.patch("sw_core.cli._resolve_daemon_start_env_files", return_value=["/tmp/OPI.env"]),
            mock.patch("sw_core.cli._load_daemon_start_env_files", side_effect=cli.EnvFileSourceError("/tmp/OPI.env", "bad env")),
            mock.patch("sw_core.cli._print") as printer,
        ):
            rc = cli._run_daemon_start(args)

        self.assertEqual(rc, 2)
        payload = printer.call_args.args[0]
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error_code"], "ENV_FILE_SOURCE_FAILED")
        self.assertEqual(payload["env_file"], "/tmp/OPI.env")
        self.assertEqual(payload["env_files"], ["/tmp/OPI.env"])


if __name__ == "__main__":
    unittest.main()
