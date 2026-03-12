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
            mock.patch("sw_core.cli._load_daemon_start_env", return_value=({"SW_OPI_U": "haman"}, "/tmp/OPI.env")),
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
        self.assertEqual(payload["warnings"], ["no_profiles_loaded"])

    def test_run_daemon_start_reports_env_source_failure(self) -> None:
        args = argparse.Namespace(
            profile_dir="/tmp/profiles",
            socket="/tmp/serialwrap.sock",
            lock="/tmp/serialwrap.lock",
            foreground=False,
        )

        with (
            mock.patch("sw_core.cli._load_daemon_start_env", side_effect=RuntimeError("bad env")),
            mock.patch("sw_core.cli._print") as printer,
        ):
            rc = cli._run_daemon_start(args)

        self.assertEqual(rc, 2)
        payload = printer.call_args.args[0]
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error_code"], "ENV_FILE_SOURCE_FAILED")
        self.assertIn("OPI.env", payload["env_file"])


if __name__ == "__main__":
    unittest.main()
