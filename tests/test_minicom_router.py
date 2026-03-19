from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
ROUTER = REPO_ROOT / "tools" / "minicom_router.sh"


@unittest.skipUnless(shutil.which("script"), "script command is required")
class TestMinicomRouter(unittest.TestCase):
    def test_wrapper_generates_transcript_log(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fake_minicom = root / "fake-minicom.sh"
            blog_dir = root / "b-log"
            capture_out = root / "stdout.txt"

            fake_minicom.write_text(
                "#!/usr/bin/env bash\n"
                "echo 'fake minicom output'\n",
                encoding="utf-8",
            )
            fake_minicom.chmod(fake_minicom.stat().st_mode | stat.S_IXUSR)

            env = os.environ.copy()
            env["MINICOM_BIN"] = str(fake_minicom)
            env["BLOG_DIR"] = str(blog_dir)
            env["MINICOM_AUTO_CAPTURE"] = "1"
            env["MINICOM_CAPTURE_WRAPPER"] = "1"
            env["MINICOM_DEFAULT_COLOR"] = ""

            subprocess.run(
                ["bash", str(ROUTER), "-D", "/dev/null"],
                check=True,
                cwd=str(REPO_ROOT),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )

            logs = sorted(blog_dir.glob("mini_*.log"))
            self.assertEqual(len(logs), 1)
            content = logs[0].read_text(encoding="utf-8", errors="replace")
            self.assertIn("fake minicom output", content)

    def test_wrapper_prefers_home_b_log_over_build_log_path(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fake_minicom = root / "fake-minicom.sh"
            home_dir = root / "home"
            legacy_dir = root / "legacy-b-log"
            home_dir.mkdir(parents=True, exist_ok=True)

            fake_minicom.write_text(
                "#!/usr/bin/env bash\n"
                "echo 'fake minicom output'\n",
                encoding="utf-8",
            )
            fake_minicom.chmod(fake_minicom.stat().st_mode | stat.S_IXUSR)

            env = os.environ.copy()
            env["HOME"] = str(home_dir)
            env["BUILD_LOG_PATH"] = str(legacy_dir)
            env["MINICOM_BIN"] = str(fake_minicom)
            env["MINICOM_AUTO_CAPTURE"] = "1"
            env["MINICOM_CAPTURE_WRAPPER"] = "1"
            env["MINICOM_DEFAULT_COLOR"] = ""
            env.pop("BLOG_DIR", None)

            subprocess.run(
                ["bash", str(ROUTER), "-D", "/dev/null"],
                check=True,
                cwd=str(REPO_ROOT),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )

            logs = sorted((home_dir / "b-log").glob("mini_*.log"))
            self.assertEqual(len(logs), 1)
            self.assertFalse(legacy_dir.exists())


if __name__ == "__main__":
    unittest.main()
