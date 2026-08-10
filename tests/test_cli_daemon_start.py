from __future__ import annotations

import argparse
import os
import tempfile
import unittest
from pathlib import Path
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
            mock.patch("sw_core.cli.should_auto_spawn", return_value=True),
            mock.patch("sw_core.cli._probe_healthy_daemon", return_value=False),
            # #173：spawn 防線掃真實 /proc，固定回報「無衝突」避免測到本機真實 daemon。
            mock.patch("sw_core.cli._find_conflicting_daemon", return_value=None),
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
            mock.patch("sw_core.cli.should_auto_spawn", return_value=True),
            mock.patch("sw_core.cli._probe_healthy_daemon", return_value=False),
            # #173：spawn 防線掃真實 /proc，固定回報「無衝突」避免測到本機真實 daemon。
            mock.patch("sw_core.cli._find_conflicting_daemon", return_value=None),
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

    def test_run_daemon_start_empty_socket_stays_explicit(self) -> None:
        args = argparse.Namespace(
            profile_dir="/tmp/profiles",
            socket="",
            lock="/tmp/serialwrap.lock",
            foreground=False,
            endpoint=None,
        )
        proc = mock.Mock(pid=4321, returncode=None)
        proc.poll.return_value = None

        with (
            mock.patch("sw_core.cli._safe_runtime_config", return_value=None),
            mock.patch("sw_core.cli._probe_healthy_daemon", return_value=False) as probe,
            # #173：spawn 防線掃真實 /proc，固定回報「無衝突」避免測到本機真實 daemon。
            mock.patch("sw_core.cli._find_conflicting_daemon", return_value=None),
            mock.patch("sw_core.cli._resolve_daemon_start_env_files", return_value=[]),
            mock.patch("sw_core.cli._load_daemon_start_env_files", return_value=({}, [])),
            mock.patch("sw_core.cli.subprocess.Popen", return_value=proc) as popen,
            mock.patch("sw_core.cli.rpc_call", side_effect=[{"ok": True}, {"ok": True}]),
            mock.patch("sw_core.cli.time.sleep"),
            mock.patch("sw_core.cli._print") as printer,
        ):
            rc = cli._run_daemon_start(args)

        self.assertEqual(rc, 0)
        probe.assert_called_once_with("")
        cmd = popen.call_args.args[0]
        self.assertEqual(cmd[cmd.index("--socket") + 1], "")
        self.assertEqual(printer.call_args.args[0]["socket"], "")


class TestDaemonStartSupervision(unittest.TestCase):
    """#108 #1：daemon start 監管模式 gate（systemd 重導 service start）+ on-demand 冪等。"""

    def _args(self) -> argparse.Namespace:
        return argparse.Namespace(
            profile_dir="/tmp/profiles",
            socket="/tmp/serialwrap.sock",
            lock="/tmp/serialwrap.lock",
            foreground=False,
            with_sudo=False,
        )

    def test_systemd_mode_routes_to_service_start(self) -> None:
        fake_rc = mock.Mock()
        fake_rc.mode.return_value = "systemd-system"
        with (
            mock.patch("sw_core.cli._default_runtime_config", return_value=fake_rc),
            mock.patch(
                "sw_core.cli.service_action",
                return_value={"ok": True, "mode": "systemd-system", "action": "start"},
            ) as svc,
            mock.patch("sw_core.cli.subprocess.Popen") as popen,
            mock.patch("sw_core.cli._print") as printer,
        ):
            rc = cli._run_daemon_start(self._args())

        self.assertEqual(rc, 0)
        svc.assert_called_once()
        self.assertEqual(svc.call_args.args[0], "start")
        self.assertEqual(svc.call_args.kwargs.get("mode"), "systemd-system")
        popen.assert_not_called()
        payload = printer.call_args.args[0]
        self.assertEqual(payload["_routed_to"], "service start")

    def test_ondemand_idempotent_when_daemon_healthy(self) -> None:
        fake_rc = mock.Mock()
        fake_rc.mode.return_value = "on-demand"
        with (
            mock.patch("sw_core.cli._default_runtime_config", return_value=fake_rc),
            mock.patch("sw_core.cli.rpc_call", return_value={"ok": True}),
            mock.patch("sw_core.cli.subprocess.Popen") as popen,
            mock.patch("sw_core.cli.time.sleep"),
            mock.patch("sw_core.cli._print") as printer,
        ):
            rc = cli._run_daemon_start(self._args())

        self.assertEqual(rc, 0)
        popen.assert_not_called()
        payload = printer.call_args.args[0]
        self.assertTrue(payload["ok"])
        self.assertTrue(payload.get("already_running"))

    def test_ondemand_idempotent_probes_config_endpoint_not_default_socket(self) -> None:
        """config 記錄健康 daemon 在非預設 socket 時，daemon start 應 probe 該 endpoint
        並 no-op，而非 probe 裸 args.socket（預設）miss 後 spawn 第二個（Codex Important #2）。"""
        args = argparse.Namespace(
            profile_dir="/tmp/profiles",
            socket=None,  # 未顯式覆寫（#120 None sentinel）→ 應解析到 config 的 socket
            lock="/tmp/serialwrap.lock",
            foreground=False,
            with_sudo=False,
            endpoint=None,
        )
        fake_rc = mock.Mock()
        fake_rc.mode.return_value = "on-demand"
        fake_rc.socket_path.return_value = "/tmp/cfg-live-108.sock"
        with (
            mock.patch("sw_core.cli._default_runtime_config", return_value=fake_rc),
            mock.patch("sw_core.cli._endpoint_alive", return_value=True),
            mock.patch("sw_core.cli._probe_healthy_daemon", return_value=True) as probe,
            mock.patch("sw_core.cli.subprocess.Popen") as popen,
            mock.patch("sw_core.cli._print") as printer,
        ):
            rc = cli._run_daemon_start(args)

        self.assertEqual(rc, 0)
        popen.assert_not_called()
        probe.assert_called_once_with("/tmp/cfg-live-108.sock")
        payload = printer.call_args.args[0]
        self.assertTrue(payload.get("already_running"))
        self.assertEqual(payload["socket"], "/tmp/cfg-live-108.sock")

    def test_unreadable_config_does_not_traceback(self) -> None:
        """config.yaml 損壞 → daemon start 退化為 on-demand 路徑、不 traceback（Codex Important #1）。"""
        args = argparse.Namespace(
            profile_dir="/tmp/profiles",
            socket="/tmp/serialwrap.sock",
            lock="/tmp/serialwrap.lock",
            foreground=False,
            with_sudo=False,
            endpoint=None,
        )
        with (
            mock.patch("sw_core.cli._default_runtime_config", side_effect=ValueError("bad yaml")),
            mock.patch("sw_core.cli._probe_healthy_daemon", return_value=True),
            mock.patch("sw_core.cli.subprocess.Popen") as popen,
            mock.patch("sw_core.cli._print") as printer,
        ):
            rc = cli._run_daemon_start(args)

        self.assertEqual(rc, 0)
        popen.assert_not_called()
        self.assertTrue(printer.call_args.args[0].get("already_running"))

    def test_daemon_stop_unreadable_config_does_not_traceback(self) -> None:
        """daemon stop 在 config.yaml 壞 YAML 時不 traceback，退化 on-demand RPC 路徑
        （PR #112 Copilot review：與 daemon start / _resolve_endpoint 容錯一致）。"""
        args = argparse.Namespace(socket="/tmp/serialwrap.sock", endpoint=None, with_sudo=False, timeout_s=2.0)
        with (
            mock.patch("sw_core.cli._default_runtime_config", side_effect=ValueError("bad yaml")),
            mock.patch("sw_core.cli.rpc_call", return_value={"ok": True}) as rpc,
            mock.patch("sw_core.cli._print") as printer,
        ):
            rc = cli._run_daemon_stop(args)

        self.assertEqual(rc, 0)
        self.assertTrue(printer.call_args.args[0]["ok"])
        # 確認走 on-demand RPC daemon.stop（非 systemd 重導），且未 traceback
        self.assertEqual(rpc.call_args.args[1], "daemon.stop")


def _make_fake_proc(proc_root, spec: dict) -> None:
    """依 spec 佈置 fake /proc：{pid: {"cmdline": str}}（鏡射 test_multi_open_detect）。"""
    proc_root.mkdir(parents=True, exist_ok=True)
    for pid, info in spec.items():
        pdir = proc_root / str(pid)
        pdir.mkdir()
        (pdir / "cmdline").write_bytes(info.get("cmdline", "").encode())
        (pdir / "fd").mkdir()


class TestFindConflictingDaemon(unittest.TestCase):
    """`cli._find_conflicting_daemon`（#173 spawn 防線的純函式部分，掃 fake /proc）。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.proc = Path(self._tmp.name) / "proc"

    def test_no_daemons_returns_none(self) -> None:
        _make_fake_proc(self.proc, {"3000": {"cmdline": "bash\0"}})
        self.assertIsNone(cli._find_conflicting_daemon("/tmp/target.sock", proc_root=str(self.proc)))

    def test_same_socket_returns_none(self) -> None:
        """既有 daemon 就綁在同一個 sock：既有冪等探測路徑已處理，本防線不重複擋。"""
        _make_fake_proc(
            self.proc,
            {"2114": {"cmdline": "python\0serialwrapd.py\0--socket\0/tmp/target.sock\0"}},
        )
        self.assertIsNone(cli._find_conflicting_daemon("/tmp/target.sock", proc_root=str(self.proc)))

    def test_different_socket_returns_conflict(self) -> None:
        _make_fake_proc(
            self.proc,
            {"2114": {"cmdline": "python\0serialwrapd.py\0--socket\0/run/serialwrap/serialwrapd.sock\0"}},
        )
        conflict = cli._find_conflicting_daemon("/tmp/target.sock", proc_root=str(self.proc))
        self.assertIsNotNone(conflict)
        self.assertEqual(conflict["pid"], 2114)
        self.assertEqual(conflict["socket"], "/run/serialwrap/serialwrapd.sock")

    def test_unknown_socket_returns_conflict(self) -> None:
        """--socket 擷取不到（None）仍視為衝突——無法排除是同機另一個 daemon 佔用中。"""
        _make_fake_proc(self.proc, {"2114": {"cmdline": "python\0serialwrapd.py\0"}})
        conflict = cli._find_conflicting_daemon("/tmp/target.sock", proc_root=str(self.proc))
        self.assertIsNotNone(conflict)
        self.assertIsNone(conflict["socket"])


class TestDaemonStartSpawnGuard(unittest.TestCase):
    """`daemon start` on-demand spawn 前的『同機已有其他 socket 的 daemon』防線（#173）。"""

    def _args(self, **overrides) -> argparse.Namespace:
        base = dict(
            profile_dir="/tmp/profiles",
            socket="/tmp/target.sock",
            lock="/tmp/serialwrap.lock",
            foreground=False,
            with_sudo=False,
            endpoint=None,
            force_spawn=False,
        )
        base.update(overrides)
        return argparse.Namespace(**base)

    def test_rejects_spawn_when_conflicting_daemon_found(self) -> None:
        conflict = {"pid": 2114, "socket": "/run/serialwrap/serialwrapd.sock"}
        with (
            mock.patch("sw_core.cli.should_auto_spawn", return_value=True),
            mock.patch("sw_core.cli._probe_healthy_daemon", return_value=False),
            mock.patch("sw_core.cli._find_conflicting_daemon", return_value=conflict) as finder,
            mock.patch("sw_core.cli.subprocess.Popen") as popen,
            mock.patch("sw_core.cli._print") as printer,
        ):
            rc = cli._run_daemon_start(self._args())

        self.assertEqual(rc, 2)
        popen.assert_not_called()
        finder.assert_called_once_with("/tmp/target.sock")
        payload = printer.call_args.args[0]
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error_code"], "DAEMON_ALREADY_RUNNING_ELSEWHERE")
        self.assertEqual(payload["existing_socket"], "/run/serialwrap/serialwrapd.sock")
        self.assertEqual(payload["existing_pid"], 2114)
        self.assertEqual(payload["target_socket"], "/tmp/target.sock")
        self.assertIn("/run/serialwrap/serialwrapd.sock", payload["message"])
        self.assertIn("--force-spawn", payload["hint"])

    def test_force_spawn_bypasses_guard(self) -> None:
        proc = mock.Mock(pid=4321, returncode=None)
        proc.poll.return_value = None
        with (
            mock.patch("sw_core.cli.should_auto_spawn", return_value=True),
            mock.patch("sw_core.cli._probe_healthy_daemon", return_value=False),
            mock.patch("sw_core.cli._find_conflicting_daemon") as finder,
            mock.patch("sw_core.cli._resolve_daemon_start_env_files", return_value=[]),
            mock.patch("sw_core.cli._load_daemon_start_env_files", return_value=({}, [])),
            mock.patch("sw_core.cli.subprocess.Popen", return_value=proc) as popen,
            mock.patch("sw_core.cli.rpc_call", side_effect=[{"ok": True}, {"ok": True}]),
            mock.patch("sw_core.cli.time.sleep"),
            mock.patch("sw_core.cli._print") as printer,
        ):
            rc = cli._run_daemon_start(self._args(force_spawn=True))

        self.assertEqual(rc, 0)
        finder.assert_not_called()
        popen.assert_called_once()
        self.assertTrue(printer.call_args.args[0]["ok"])

    def test_no_conflict_spawns_normally(self) -> None:
        """同 socket（或無其他 daemon）：既有冪等/spawn 路徑不變。"""
        proc = mock.Mock(pid=4321, returncode=None)
        proc.poll.return_value = None
        with (
            mock.patch("sw_core.cli.should_auto_spawn", return_value=True),
            mock.patch("sw_core.cli._probe_healthy_daemon", return_value=False),
            mock.patch("sw_core.cli._find_conflicting_daemon", return_value=None) as finder,
            mock.patch("sw_core.cli._resolve_daemon_start_env_files", return_value=[]),
            mock.patch("sw_core.cli._load_daemon_start_env_files", return_value=({}, [])),
            mock.patch("sw_core.cli.subprocess.Popen", return_value=proc) as popen,
            mock.patch("sw_core.cli.rpc_call", side_effect=[{"ok": True}, {"ok": True}]),
            mock.patch("sw_core.cli.time.sleep"),
            mock.patch("sw_core.cli._print") as printer,
        ):
            rc = cli._run_daemon_start(self._args())

        self.assertEqual(rc, 0)
        finder.assert_called_once_with("/tmp/target.sock")
        popen.assert_called_once()
        self.assertTrue(printer.call_args.args[0]["ok"])


if __name__ == "__main__":
    unittest.main()
