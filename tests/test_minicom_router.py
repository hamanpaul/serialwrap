from __future__ import annotations

from contextlib import contextmanager
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
ROUTER = REPO_ROOT / "tools" / "minicom_router.sh"
TEMP_ROOT = REPO_ROOT / ".test-minicom-router.tmp"


class TestMinicomRouter(unittest.TestCase):
    @contextmanager
    def _temporary_directory(self):
        TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as td:
            yield td
        try:
            TEMP_ROOT.rmdir()
        except OSError:
            pass

    def _write_executable(self, path: Path, content: str) -> None:
        path.write_text(content, encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)

    def _write_unavailable_serialwrap(self, root: Path) -> Path:
        fake_serialwrap = root / "fake-serialwrap.sh"
        self._write_executable(
            fake_serialwrap,
            "#!/usr/bin/env bash\n"
            "echo '{\"ok\":false}'\n",
        )
        return fake_serialwrap

    def _base_env(self, root: Path, fake_minicom: Path, blog_dir: Path | None = None) -> dict[str, str]:
        env = os.environ.copy()
        env["MINICOM_BIN"] = str(fake_minicom)
        env["BLOG_DIR"] = str(blog_dir or (root / "b-log"))
        env["MINICOM_AUTO_CAPTURE"] = "1"
        env["MINICOM_DEFAULT_COLOR"] = ""
        env["SERIALWRAP_BIN"] = str(self._write_unavailable_serialwrap(root))
        env["SERIALWRAP_AUTO_START_DAEMON"] = "0"
        env["SERIALWRAP_SOCKET"] = str(root / "serialwrapd.sock")
        env.pop("MINICOM_CAPTURE_MODE", None)
        env.pop("MINICOM_CAPTURE_WRAPPER", None)
        return env

    def _path_without_script(self, root: Path) -> Path:
        bin_dir = root / "bin-without-script"
        bin_dir.mkdir(parents=True)
        for name in ("bash", "date", "dirname", "jq", "mkdir", "tr"):
            target = shutil.which(name)
            if target is None:
                self.skipTest(f"{name} command is required")
            os.symlink(target, bin_dir / name)
        return bin_dir

    @unittest.skipUnless(shutil.which("script"), "script command is required")
    def test_wrapper_generates_transcript_log(self) -> None:
        with self._temporary_directory() as td:
            root = Path(td)
            fake_minicom = root / "fake-minicom.sh"
            blog_dir = root / "b-log"

            self._write_executable(
                fake_minicom,
                "#!/usr/bin/env bash\n"
                "echo 'fake minicom output'\n",
            )

            env = self._base_env(root, fake_minicom, blog_dir)
            env["MINICOM_CAPTURE_WRAPPER"] = "1"

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

    @unittest.skipUnless(shutil.which("script"), "script command is required")
    def test_wrapper_prefers_home_b_log_over_build_log_path(self) -> None:
        with self._temporary_directory() as td:
            root = Path(td)
            fake_minicom = root / "fake-minicom.sh"
            home_dir = root / "home"
            legacy_dir = root / "legacy-b-log"
            home_dir.mkdir(parents=True, exist_ok=True)

            self._write_executable(
                fake_minicom,
                "#!/usr/bin/env bash\n"
                "echo 'fake minicom output'\n",
            )

            env = self._base_env(root, fake_minicom)
            env["HOME"] = str(home_dir)
            env["BUILD_LOG_PATH"] = str(legacy_dir)
            env["MINICOM_CAPTURE_WRAPPER"] = "1"
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

    @unittest.skipUnless(shutil.which("script"), "script command is required")
    def test_default_capture_uses_script_wrapper_without_minicom_capturefile(self) -> None:
        with self._temporary_directory() as td:
            root = Path(td)
            fake_minicom = root / "fake-minicom.sh"
            args_out = root / "args.txt"
            blog_dir = root / "b-log"

            self._write_executable(
                fake_minicom,
                "#!/usr/bin/env bash\n"
                "printf '%s\\n' \"$@\" > \"$FAKE_ARGS_OUT\"\n"
                "for arg in \"$@\"; do\n"
                "  if [[ \"$arg\" == '-C' || \"$arg\" == --capturefile* ]]; then\n"
                "    echo 'unexpected minicom native capture arg' >&2\n"
                "    exit 44\n"
                "  fi\n"
                "done\n"
                "printf 'fake minicom output\\n'\n",
            )

            env = self._base_env(root, fake_minicom, blog_dir)
            env["FAKE_ARGS_OUT"] = str(args_out)

            subprocess.run(
                ["bash", str(ROUTER), "-D", "/dev/null"],
                check=True,
                cwd=str(REPO_ROOT),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )

            args = args_out.read_text(encoding="utf-8")
            self.assertNotIn("-C", args)
            logs = sorted(blog_dir.glob("mini_*.log"))
            self.assertEqual(len(logs), 1)
            content = logs[0].read_text(encoding="utf-8", errors="replace")
            self.assertIn("fake minicom output", content)

    def test_user_capture_args_disable_auto_transcript(self) -> None:
        cases = (
            ("short", ["-C", "user-short.log"], ["-C", "user-short.log"], 1),
            ("long", ["--capturefile=user-long.log"], ["--capturefile=user-long.log"], 0),
        )
        for name, capture_args, expected_args, expected_short_count in cases:
            with self.subTest(name=name), self._temporary_directory() as td:
                root = Path(td)
                fake_minicom = root / "fake-minicom.sh"
                fake_bin = root / "fake-bin"
                args_out = root / "args.txt"
                script_marker = root / "script-ran.txt"
                blog_dir = root / "b-log"
                fake_bin.mkdir()

                self._write_executable(
                    fake_minicom,
                    "#!/usr/bin/env bash\n"
                    "printf '%s\\n' \"$@\" > \"$FAKE_ARGS_OUT\"\n",
                )
                self._write_executable(
                    fake_bin / "script",
                    "#!/usr/bin/env bash\n"
                    "echo 'script wrapper unexpectedly invoked' > \"$FAKE_SCRIPT_MARKER\"\n"
                    "exit 66\n",
                )

                env = self._base_env(root, fake_minicom, blog_dir)
                env["FAKE_ARGS_OUT"] = str(args_out)
                env["FAKE_SCRIPT_MARKER"] = str(script_marker)
                env["PATH"] = f"{fake_bin}:{env['PATH']}"

                subprocess.run(
                    ["bash", str(ROUTER), "-D", "/dev/null", *capture_args],
                    check=True,
                    cwd=str(REPO_ROOT),
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                )

                args = args_out.read_text(encoding="utf-8").splitlines()
                for expected in expected_args:
                    self.assertIn(expected, args)
                self.assertEqual(args.count("-C"), expected_short_count)
                self.assertFalse(list(blog_dir.glob("mini_*.log")))
                self.assertFalse(script_marker.exists())

    def test_script_unavailable_warns_and_runs_without_native_capture(self) -> None:
        with self._temporary_directory() as td:
            root = Path(td)
            fake_minicom = root / "fake-minicom.sh"
            args_out = root / "args.txt"
            blog_dir = root / "b-log"
            path_without_script = self._path_without_script(root)
            bash_bin = shutil.which("bash")
            if bash_bin is None:
                self.skipTest("bash command is required")

            self._write_executable(
                fake_minicom,
                "#!/usr/bin/env bash\n"
                "printf '%s\\n' \"$@\" > \"$FAKE_ARGS_OUT\"\n"
                "exit 23\n",
            )

            env = self._base_env(root, fake_minicom, blog_dir)
            env["FAKE_ARGS_OUT"] = str(args_out)
            env["PATH"] = str(path_without_script)

            result = subprocess.run(
                [bash_bin, str(ROUTER), "-D", "/dev/null"],
                check=False,
                cwd=str(REPO_ROOT),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            self.assertEqual(result.returncode, 23)
            self.assertIn("warning", result.stderr)
            self.assertIn("script", result.stderr)
            args = args_out.read_text(encoding="utf-8")
            self.assertNotIn("-C", args)
            self.assertFalse(list(blog_dir.glob("mini_*.log")))

    def test_capture_mode_minicom_uses_native_capturefile(self) -> None:
        with self._temporary_directory() as td:
            root = Path(td)
            fake_minicom = root / "fake-minicom.sh"
            args_out = root / "args.txt"
            blog_dir = root / "b-log"

            self._write_executable(
                fake_minicom,
                "#!/usr/bin/env bash\n"
                "printf '%s\\n' \"$@\" > \"$FAKE_ARGS_OUT\"\n"
                "capture=''\n"
                "while [[ $# -gt 0 ]]; do\n"
                "  if [[ \"$1\" == '-C' && $# -ge 2 ]]; then\n"
                "    capture=\"$2\"\n"
                "    shift 2\n"
                "    continue\n"
                "  fi\n"
                "  shift\n"
                "done\n"
                "if [[ -n \"$capture\" ]]; then\n"
                "  printf 'fake minicom output\\n' > \"$capture\"\n"
                "fi\n"
                "printf 'fake minicom output\\n'\n",
            )

            env = self._base_env(root, fake_minicom, blog_dir)
            env["FAKE_ARGS_OUT"] = str(args_out)
            env["MINICOM_CAPTURE_MODE"] = "minicom"

            subprocess.run(
                ["bash", str(ROUTER), "-D", "/dev/null"],
                check=True,
                cwd=str(REPO_ROOT),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )

            args = args_out.read_text(encoding="utf-8")
            self.assertIn("-C", args)
            logs = sorted(blog_dir.glob("mini_*.log"))
            self.assertEqual(len(logs), 1)
            self.assertIn("fake minicom output", logs[0].read_text(encoding="utf-8"))

    def test_capture_mode_off_disables_auto_transcript(self) -> None:
        with self._temporary_directory() as td:
            root = Path(td)
            fake_minicom = root / "fake-minicom.sh"
            args_out = root / "args.txt"
            blog_dir = root / "b-log"

            self._write_executable(
                fake_minicom,
                "#!/usr/bin/env bash\n"
                "printf '%s\\n' \"$@\" > \"$FAKE_ARGS_OUT\"\n"
                "printf 'fake minicom output\\n'\n",
            )

            env = self._base_env(root, fake_minicom, blog_dir)
            env["FAKE_ARGS_OUT"] = str(args_out)
            env["MINICOM_CAPTURE_MODE"] = "off"

            subprocess.run(
                ["bash", str(ROUTER), "-D", "/dev/null"],
                check=True,
                cwd=str(REPO_ROOT),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )

            args = args_out.read_text(encoding="utf-8")
            self.assertNotIn("-C", args)
            self.assertFalse(list(blog_dir.glob("mini_*.log")))

    def test_legacy_explicit_capture_wrapper_zero_uses_native_capturefile(self) -> None:
        with self._temporary_directory() as td:
            root = Path(td)
            fake_minicom = root / "fake-minicom.sh"
            args_out = root / "args.txt"
            blog_dir = root / "b-log"

            self._write_executable(
                fake_minicom,
                "#!/usr/bin/env bash\n"
                "printf '%s\\n' \"$@\" > \"$FAKE_ARGS_OUT\"\n"
                "capture=''\n"
                "while [[ $# -gt 0 ]]; do\n"
                "  if [[ \"$1\" == '-C' && $# -ge 2 ]]; then\n"
                "    capture=\"$2\"\n"
                "    shift 2\n"
                "    continue\n"
                "  fi\n"
                "  shift\n"
                "done\n"
                "if [[ -n \"$capture\" ]]; then\n"
                "  printf 'legacy capture\\n' > \"$capture\"\n"
                "fi\n",
            )

            env = self._base_env(root, fake_minicom, blog_dir)
            env["FAKE_ARGS_OUT"] = str(args_out)
            env["MINICOM_CAPTURE_WRAPPER"] = "0"

            subprocess.run(
                ["bash", str(ROUTER), "-D", "/dev/null"],
                check=True,
                cwd=str(REPO_ROOT),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )

            args = args_out.read_text(encoding="utf-8")
            self.assertIn("-C", args)
            logs = sorted(blog_dir.glob("mini_*.log"))
            self.assertEqual(len(logs), 1)
            self.assertIn("legacy capture", logs[0].read_text(encoding="utf-8"))

    def test_invalid_capture_mode_exits_with_clear_error(self) -> None:
        with self._temporary_directory() as td:
            root = Path(td)
            fake_minicom = root / "fake-minicom.sh"
            self._write_executable(
                fake_minicom,
                "#!/usr/bin/env bash\n"
                "exit 0\n",
            )

            env = self._base_env(root, fake_minicom)
            env["MINICOM_CAPTURE_MODE"] = "native"

            result = subprocess.run(
                ["bash", str(ROUTER), "-D", "/dev/null"],
                check=False,
                cwd=str(REPO_ROOT),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("invalid MINICOM_CAPTURE_MODE", result.stdout)

    def test_broker_console_detaches_after_minicom_nonzero_exit(self) -> None:
        with self._temporary_directory() as td:
            root = Path(td)
            fake_minicom = root / "fake-minicom.sh"
            fake_serialwrap = root / "fake-serialwrap.sh"
            serialwrap_log = root / "serialwrap.log"
            args_out = root / "args.txt"

            self._write_executable(
                fake_minicom,
                "#!/usr/bin/env bash\n"
                "printf '%s\\n' \"$@\" > \"$FAKE_ARGS_OUT\"\n"
                "exit 37\n",
            )
            self._write_executable(
                fake_serialwrap,
                "#!/usr/bin/env bash\n"
                "printf '%s\\n' \"$*\" >> \"$FAKE_SERIALWRAP_LOG\"\n"
                "if [[ \"${1:-}\" == '--socket' ]]; then\n"
                "  shift 2\n"
                "fi\n"
                "if [[ \"${1:-}\" == 'session' && \"${2:-}\" == 'list' ]]; then\n"
                "  echo '{\"ok\":true,\"sessions\":[{\"com\":\"COM0\",\"alias\":\"default\",\"session_id\":\"s0\",\"state\":\"READY\"}]}'\n"
                "  exit 0\n"
                "fi\n"
                "if [[ \"${1:-}\" == 'session' && \"${2:-}\" == 'console-attach' ]]; then\n"
                "  echo '{\"ok\":true,\"client_id\":\"client-1\",\"vtty\":\"/dev/null\"}'\n"
                "  exit 0\n"
                "fi\n"
                "if [[ \"${1:-}\" == 'session' && \"${2:-}\" == 'console-detach' ]]; then\n"
                "  echo '{\"ok\":true}'\n"
                "  exit 0\n"
                "fi\n"
                "echo '{\"ok\":false}'\n"
                "exit 1\n",
            )

            env = os.environ.copy()
            env["SERIALWRAP_BIN"] = str(fake_serialwrap)
            env["SERIALWRAP_SOCKET"] = str(root / "serialwrapd.sock")
            env["SERIALWRAP_AUTO_START_DAEMON"] = "0"
            env["FAKE_SERIALWRAP_LOG"] = str(serialwrap_log)
            env["MINICOM_BIN"] = str(fake_minicom)
            env["FAKE_ARGS_OUT"] = str(args_out)
            env["MINICOM_AUTO_CAPTURE"] = "0"
            env["MINICOM_DEFAULT_COLOR"] = ""
            env["MINICOM_CAPTURE_MODE"] = "off"

            result = subprocess.run(
                ["bash", str(ROUTER), "COM0"],
                check=False,
                cwd=str(REPO_ROOT),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )

            self.assertEqual(result.returncode, 37)
            calls = serialwrap_log.read_text(encoding="utf-8").splitlines()
            self.assertTrue(any("session console-attach" in call for call in calls))
            self.assertTrue(any("session console-detach" in call for call in calls))


if __name__ == "__main__":
    unittest.main()
