"""#131 Fix B：`daemon start` 在原生 Windows 可用。

issue #131 點 2：原生 Windows 上 `daemon start` 兩條路都不通——不帶
``--endpoint`` 踩 AF_UNIX 崩潰；帶了被無條件 ``REMOTE_NOT_SUPPORTED``；
且 spawn 時把 unix 檔案路徑塞給 ``--socket``，spawn 出的 ``TcpRpcServer``
直接 ValueError 退出。本檔驗證：

- win backend 下預設 spawn ``--socket`` 為 tcp ``DEFAULT_ENDPOINT``；
- ``--endpoint`` 於 win backend 開放 loopback tcp://（視為本機 bind 位址）、
  非 loopback 照舊拒絕；POSIX 拒絕訊息逐字不變；
- ``_daemon_spawn_argv()``：凍結（PyInstaller）→ 同層 serialwrapd.exe →
  PATH → 全落空 ``DAEMON_BINARY_NOT_FOUND``；原始碼 → serialwrapd.py →
  ``-m sw_core.daemon``；
- readiness 等待窗：win 50 次（10s）、posix 15 次（3s，既有行為 pin）。
"""
from __future__ import annotations

import argparse
import os
import sys
from unittest import mock

import pytest

from sw_core import cli


def _args(**overrides) -> argparse.Namespace:
    base = {
        "profile_dir": "/tmp/profiles",
        "socket": None,
        "lock": "/tmp/serialwrap.lock",
        "foreground": False,
        "with_sudo": False,
        "endpoint": None,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def _spawn_mocks(rpc_side_effect=None):
    """_run_daemon_start spawn 路徑的標準 mock 組。"""
    proc = mock.Mock(pid=4321, returncode=None)
    proc.poll.return_value = None
    return proc, (
        mock.patch("sw_core.cli._safe_runtime_config", return_value=None),
        mock.patch("sw_core.cli._probe_healthy_daemon", return_value=False),
        mock.patch("sw_core.cli._resolve_daemon_start_env_files", return_value=[]),
        mock.patch("sw_core.cli._load_daemon_start_env_files", return_value=({}, [])),
        mock.patch("sw_core.cli.subprocess.Popen", return_value=proc),
        mock.patch(
            "sw_core.cli.rpc_call",
            side_effect=rpc_side_effect or [{"ok": True}, {"ok": True}],
        ),
        mock.patch("sw_core.cli.time.sleep"),
        mock.patch("sw_core.cli._print"),
    )


def _run_with_mocks(args, monkeypatch, backend: str, rpc_side_effect=None):
    monkeypatch.setenv("SERIALWRAP_RPC_BACKEND", backend)
    proc, patches = _spawn_mocks(rpc_side_effect)
    with (
        patches[0],
        patches[1],
        patches[2],
        patches[3],
        patches[4] as popen,
        patches[5],
        patches[6] as sleeper,
        patches[7] as printer,
    ):
        rc = cli._run_daemon_start(args)
    return rc, popen, printer, sleeper


class TestWinSpawnSocketDefault:
    def test_default_spawn_socket_is_tcp_on_win_backend(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(cli, "DEFAULT_ENDPOINT", "tcp://127.0.0.1:48700", raising=False)
        rc, popen, printer, _ = _run_with_mocks(_args(), monkeypatch, backend="win")
        assert rc == 0
        cmd = popen.call_args.args[0]
        assert cmd[cmd.index("--socket") + 1] == "tcp://127.0.0.1:48700"

    def test_explicit_socket_still_wins_on_win_backend(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rc, popen, printer, _ = _run_with_mocks(
            _args(socket="tcp://127.0.0.1:49123"), monkeypatch, backend="win"
        )
        assert rc == 0
        cmd = popen.call_args.args[0]
        assert cmd[cmd.index("--socket") + 1] == "tcp://127.0.0.1:49123"


class TestEndpointCarveOut:
    def test_loopback_endpoint_accepted_as_bind_on_win(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rc, popen, printer, _ = _run_with_mocks(
            _args(endpoint="tcp://127.0.0.1:49999"), monkeypatch, backend="win"
        )
        assert rc == 0
        cmd = popen.call_args.args[0]
        assert cmd[cmd.index("--socket") + 1] == "tcp://127.0.0.1:49999"

    def test_non_loopback_endpoint_rejected_on_win(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SERIALWRAP_RPC_BACKEND", "win")
        with (
            mock.patch("sw_core.cli.subprocess.Popen") as popen,
            mock.patch("sw_core.cli._print") as printer,
        ):
            rc = cli._run_daemon_start(_args(endpoint="tcp://192.168.1.50:48700"))
        assert rc == 2
        popen.assert_not_called()
        payload = printer.call_args.args[0]
        assert payload["error_code"] == "REMOTE_NOT_SUPPORTED"

    def test_posix_rejects_loopback_endpoint_with_original_message(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """POSIX 行為逐位元組不變：連 loopback tcp 也照舊拒絕、原訊息逐字保留。"""
        monkeypatch.setenv("SERIALWRAP_RPC_BACKEND", "posix")
        with (
            mock.patch("sw_core.cli.subprocess.Popen") as popen,
            mock.patch("sw_core.cli._print") as printer,
        ):
            rc = cli._run_daemon_start(_args(endpoint="tcp://127.0.0.1:48700"))
        assert rc == 2
        popen.assert_not_called()
        payload = printer.call_args.args[0]
        assert payload["error_code"] == "REMOTE_NOT_SUPPORTED"
        assert payload["message"] == "--endpoint 不支援 daemon start（daemon 只能在本機啟動）"


class TestIsLoopbackTcp:
    @pytest.mark.parametrize(
        "ep",
        ["tcp://127.0.0.1:48700", "tcp://localhost:1", "tcp://[::1]:48700"],
    )
    def test_loopback_true(self, ep: str) -> None:
        assert cli._is_loopback_tcp(ep) is True

    @pytest.mark.parametrize(
        "ep",
        ["tcp://192.168.1.50:48700", "tcp://0.0.0.0:48700", "/tmp/x.sock", "unix:///tmp/x", "tcp://"],
    )
    def test_non_loopback_or_non_tcp_false(self, ep: str) -> None:
        assert cli._is_loopback_tcp(ep) is False


class TestDaemonSpawnArgv:
    def test_frozen_prefers_sibling_exe(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        exe_dir = tmp_path / "dist"
        exe_dir.mkdir()
        (exe_dir / "serialwrapd.exe").write_bytes(b"MZ")
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(sys, "executable", str(exe_dir / "serialwrap.exe"))
        assert cli._daemon_spawn_argv() == [str(exe_dir / "serialwrapd.exe")]

    def test_frozen_falls_back_to_path(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(sys, "executable", str(tmp_path / "serialwrap.exe"))
        with mock.patch("sw_core.cli.shutil.which", return_value="C:/tools/serialwrapd.exe"):
            assert cli._daemon_spawn_argv() == ["C:/tools/serialwrapd.exe"]

    def test_frozen_none_found_returns_none(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(sys, "executable", str(tmp_path / "serialwrap.exe"))
        with mock.patch("sw_core.cli.shutil.which", return_value=None):
            assert cli._daemon_spawn_argv() is None

    def test_source_checkout_uses_script(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delattr(sys, "frozen", raising=False)
        assert os.path.isfile(cli._daemon_script_path())  # repo checkout 前提
        assert cli._daemon_spawn_argv() == [sys.executable, cli._daemon_script_path()]

    def test_installed_package_falls_back_to_module(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delattr(sys, "frozen", raising=False)
        with mock.patch("sw_core.cli._daemon_script_path", return_value="/nonexistent/serialwrapd.py"):
            assert cli._daemon_spawn_argv() == [sys.executable, "-m", "sw_core.daemon"]

    def test_frozen_none_found_makes_daemon_start_fail_cleanly(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        monkeypatch.setenv("SERIALWRAP_RPC_BACKEND", "win")
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(sys, "executable", str(tmp_path / "serialwrap.exe"))
        with (
            mock.patch("sw_core.cli.shutil.which", return_value=None),
            mock.patch("sw_core.cli._safe_runtime_config", return_value=None),
            mock.patch("sw_core.cli._probe_healthy_daemon", return_value=False),
            mock.patch("sw_core.cli.subprocess.Popen") as popen,
            mock.patch("sw_core.cli._print") as printer,
        ):
            rc = cli._run_daemon_start(_args())
        assert rc == 2
        popen.assert_not_called()
        assert printer.call_args.args[0]["error_code"] == "DAEMON_BINARY_NOT_FOUND"


class TestReadinessWindow:
    def test_win_backend_waits_50_attempts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rc, popen, printer, sleeper = _run_with_mocks(
            _args(), monkeypatch, backend="win", rpc_side_effect=lambda *a, **k: {"ok": False}
        )
        assert rc == 2
        assert printer.call_args.args[0]["error_code"] == "DAEMON_NOT_READY"
        assert sleeper.call_count == 50

    def test_posix_backend_keeps_15_attempts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rc, popen, printer, sleeper = _run_with_mocks(
            _args(), monkeypatch, backend="posix", rpc_side_effect=lambda *a, **k: {"ok": False}
        )
        assert rc == 2
        assert printer.call_args.args[0]["error_code"] == "DAEMON_NOT_READY"
        assert sleeper.call_count == 15


class TestEnvFileWithoutBash:
    def test_missing_bash_raises_env_file_source_error_on_win(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """Windows 無 bash 但 env 檔存在 → 結構化 EnvFileSourceError，非 FileNotFoundError traceback。"""
        monkeypatch.setenv("SERIALWRAP_RPC_BACKEND", "win")
        env_file = tmp_path / "OPI.env"
        env_file.write_text("SW_OPI_U=haman\n", encoding="utf-8")
        with (
            mock.patch("sw_core.cli.subprocess.run", side_effect=FileNotFoundError("bash")),
            pytest.raises(cli.EnvFileSourceError),
        ):
            cli._load_daemon_start_env_files([str(env_file)])

    def test_missing_bash_reraises_on_posix(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """POSIX 例外流不變：OSError 照舊上拋。"""
        monkeypatch.setenv("SERIALWRAP_RPC_BACKEND", "posix")
        env_file = tmp_path / "OPI.env"
        env_file.write_text("SW_OPI_U=haman\n", encoding="utf-8")
        with (
            mock.patch("sw_core.cli.subprocess.run", side_effect=FileNotFoundError("bash")),
            pytest.raises(FileNotFoundError),
        ):
            cli._load_daemon_start_env_files([str(env_file)])


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="原生 Windows detach 旗標對測")
class TestWindowsDetachFlags:
    def test_popen_uses_detached_creationflags(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import subprocess

        rc, popen, printer, _ = _run_with_mocks(_args(), monkeypatch, backend="win")
        assert rc == 0
        kwargs = popen.call_args.kwargs
        assert kwargs["creationflags"] == (
            subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        )
        assert "start_new_session" not in kwargs


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
