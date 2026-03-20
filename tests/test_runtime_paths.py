from __future__ import annotations

import importlib
import os
import tempfile
import unittest
from unittest import mock

import sw_core.constants as constants


class TestRuntimePaths(unittest.TestCase):
    def tearDown(self) -> None:
        importlib.reload(constants)

    def test_wal_dir_defaults_under_state_dir(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            state_dir = os.path.join(td, "state")

            with mock.patch.dict(
                os.environ,
                {
                    "SERIALWRAP_STATE_DIR": state_dir,
                },
                clear=True,
            ):
                importlib.reload(constants)

                self.assertEqual(constants.STATE_DIR, state_dir)
                self.assertEqual(constants.RUN_DIR, state_dir)
                self.assertEqual(constants.WAL_DIR, os.path.join(state_dir, "wal"))

    def test_wal_dir_can_be_overridden_independently(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = os.path.join(td, "home")
            state_dir = os.path.join(td, "state")
            run_dir = os.path.join(td, "run")
            os.makedirs(home, exist_ok=True)

            with mock.patch.dict(
                os.environ,
                {
                    "HOME": home,
                    "SERIALWRAP_STATE_DIR": state_dir,
                    "SERIALWRAP_RUN_DIR": run_dir,
                    "SERIALWRAP_WAL_DIR": "~/b-log",
                },
                clear=True,
            ):
                importlib.reload(constants)

                self.assertEqual(constants.STATE_DIR, state_dir)
                self.assertEqual(constants.RUN_DIR, run_dir)
                self.assertEqual(constants.WAL_DIR, os.path.join(home, "b-log"))

                constants.ensure_runtime_dirs()

                self.assertTrue(os.path.isdir(state_dir))
                self.assertTrue(os.path.isdir(run_dir))
                self.assertTrue(os.path.isdir(os.path.join(home, "b-log")))


if __name__ == "__main__":
    unittest.main()
