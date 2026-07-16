"""#131 Fix A：原生 Windows CLI endpoint 平台感知。

原生 Windows 的 CPython 無 ``socket.AF_UNIX``；CLI 預設 endpoint 又是 unix
檔案路徑（``SOCKET_PATH``），導致任何不帶 ``--endpoint`` 的子命令直接
``AttributeError`` 崩潰（issue #131 點 1）。本檔驗證：

- ``client._af_unix_available()`` / ``rpc_call`` / ``cli._endpoint_alive`` 在無
  AF_UNIX 平台不崩潰、回結構化結果；
- ``cli._local_default_endpoint()`` 依 rpc backend 選平台預設（win → tcp
  ``DEFAULT_ENDPOINT``、posix → ``SOCKET_PATH``，POSIX 行為逐位元組不變）；
- ``_resolve_endpoint`` 於 win backend 下的預設與 WSL 殘留 unix config 的
  #108 fallback 行為。

測試以 ``monkeypatch.delattr(socket.AF_UNIX, raising=False)`` 模擬（原生
Windows 上本來就不存在，等價 no-op），Linux CI 與原生 Windows 皆可跑。
"""
from __future__ import annotations

import argparse
import socket
import sys
from unittest import mock

import pytest

from sw_core import cli, client


def _args(socket_arg: str | None = None, endpoint: str | None = None) -> argparse.Namespace:
    return argparse.Namespace(socket=socket_arg, endpoint=endpoint)


class TestAfUnixAvailable:
    def test_reports_true_when_attr_present(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(socket, "AF_UNIX", 1, raising=False)
        assert client._af_unix_available() is True

    def test_reports_false_when_attr_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delattr(socket, "AF_UNIX", raising=False)
        assert client._af_unix_available() is False


class TestEndpointAliveWithoutAfUnix:
    """無 AF_UNIX 平台上 unix endpoint 一律視為不可連（觸發 #108 fallback）。"""

    def test_unix_path_is_dead(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delattr(socket, "AF_UNIX", raising=False)
        assert cli._endpoint_alive("/tmp/serialwrap-131.sock") is False

    def test_unix_scheme_is_dead(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delattr(socket, "AF_UNIX", raising=False)
        assert cli._endpoint_alive("unix:///tmp/serialwrap-131.sock") is False

    def test_tcp_probed_for_real_on_win_backend(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """win backend（#131 review）：tcp endpoint 改實測（lock_win 0.2s probe），
        殘留死 port 的 config 才會觸發 #108 dangling fallback。"""
        monkeypatch.setenv("SERIALWRAP_RPC_BACKEND", "win")
        with mock.patch("sw_core.lock_win._endpoint_alive", return_value=False) as probe:
            assert cli._endpoint_alive("tcp://127.0.0.1:65000") is False
        probe.assert_called_once_with("tcp://127.0.0.1:65000")
        with mock.patch("sw_core.lock_win._endpoint_alive", return_value=True):
            assert cli._endpoint_alive("tcp://127.0.0.1:65000") is True

    def test_tcp_still_skipped_as_alive_on_posix_backend(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """POSIX 原語意逐位元組不變：tcp 一律視為可連、不探測（可能是 ssh tunnel）。"""
        monkeypatch.setenv("SERIALWRAP_RPC_BACKEND", "posix")
        with mock.patch("sw_core.lock_win._endpoint_alive") as probe:
            assert cli._endpoint_alive("tcp://127.0.0.1:65000") is True
        probe.assert_not_called()

    def test_unparseable_still_skipped_as_alive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delattr(socket, "AF_UNIX", raising=False)
        assert cli._endpoint_alive("tcp://") is True


class TestRpcCallWithoutAfUnix:
    def test_unix_endpoint_returns_socket_error_not_crash(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delattr(socket, "AF_UNIX", raising=False)
        resp = client.rpc_call("/tmp/serialwrap-131.sock", "health.ping", {})
        assert resp["ok"] is False
        assert resp["error_code"] == "SOCKET_ERROR"

    def test_tcp_endpoint_unaffected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delattr(socket, "AF_UNIX", raising=False)
        # 連不上的 tcp endpoint 應照舊回 SOCKET_ERROR/TIMEOUT，不 AttributeError。
        resp = client.rpc_call("tcp://127.0.0.1:1", "health.ping", {}, timeout_s=0.2)
        assert resp["ok"] is False
        assert resp["error_code"] in {"SOCKET_ERROR", "TIMEOUT"}


class TestLocalDefaultEndpoint:
    def test_win_backend_uses_platform_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SERIALWRAP_RPC_BACKEND", "win")
        monkeypatch.setattr(cli, "DEFAULT_ENDPOINT", "tcp://127.0.0.1:48700", raising=False)
        assert cli._local_default_endpoint() == "tcp://127.0.0.1:48700"

    def test_win_backend_always_yields_tcp_even_on_posix(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """POSIX 上以 env 模擬 win backend（CLAUDE.md 明文 seam）：DEFAULT_ENDPOINT 仍為
        unix 路徑，須改組 tcp 預設，不得把 unix 路徑餵給 win 後端。"""
        monkeypatch.setenv("SERIALWRAP_RPC_BACKEND", "win")
        monkeypatch.setattr(cli, "DEFAULT_ENDPOINT", "/run/user/1000/serialwrapd.sock", raising=False)
        assert cli._local_default_endpoint() == f"tcp://127.0.0.1:{cli.DEFAULT_TCP_PORT}"

    def test_invalid_backend_env_falls_back_to_real_platform(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SERIALWRAP_RPC_BACKEND", "not-a-backend")
        expected = cli.DEFAULT_ENDPOINT if sys.platform.startswith("win") else cli.SOCKET_PATH
        assert cli._local_default_endpoint() == expected

    def test_posix_backend_keeps_socket_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SERIALWRAP_RPC_BACKEND", "posix")
        assert cli._local_default_endpoint() == cli.SOCKET_PATH

    @pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX 預設行為")
    def test_auto_on_posix_keeps_socket_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SERIALWRAP_RPC_BACKEND", raising=False)
        assert cli._local_default_endpoint() == cli.SOCKET_PATH

    @pytest.mark.skipif(not sys.platform.startswith("win"), reason="原生 Windows 對測")
    def test_auto_on_windows_uses_default_endpoint(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("SERIALWRAP_RPC_BACKEND", raising=False)
        assert cli._local_default_endpoint() == cli.DEFAULT_ENDPOINT
        assert cli._local_default_endpoint().startswith("tcp://127.0.0.1:")


class TestResolveEndpointWinBackend:
    def test_no_config_falls_back_to_tcp_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SERIALWRAP_RPC_BACKEND", "win")
        monkeypatch.setattr(cli, "DEFAULT_ENDPOINT", "tcp://127.0.0.1:48700", raising=False)
        with mock.patch("sw_core.cli._default_runtime_config", side_effect=ValueError("no config")):
            assert cli._resolve_endpoint(_args()) == "tcp://127.0.0.1:48700"

    def test_stale_wsl_unix_config_falls_back_to_tcp(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """config.yaml 殘留 WSL unix socket_path → 依 #108 語意改連 tcp canonical。"""
        monkeypatch.setenv("SERIALWRAP_RPC_BACKEND", "win")
        monkeypatch.delattr(socket, "AF_UNIX", raising=False)
        monkeypatch.setattr(cli, "DEFAULT_ENDPOINT", "tcp://127.0.0.1:48700", raising=False)
        fake_rc = mock.Mock()
        fake_rc.socket_path.return_value = "/run/user/1000/serialwrap/serialwrapd.sock"
        fake_rc.mode.return_value = "on-demand"
        with (
            mock.patch("sw_core.cli._default_runtime_config", return_value=fake_rc),
            mock.patch("sw_core.lock_win._endpoint_alive", return_value=True),  # canonical 可連
        ):
            resolved = cli._resolve_endpoint(_args())
        assert resolved == "tcp://127.0.0.1:48700"
        assert "/run/user/1000/serialwrap/serialwrapd.sock" in capsys.readouterr().err

    def test_stale_tcp_config_falls_back_to_canonical(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """config 殘留死 tcp port（如舊 --endpoint 實驗）→ 探測失敗改連 canonical（#131 review）。"""
        monkeypatch.setenv("SERIALWRAP_RPC_BACKEND", "win")
        monkeypatch.setattr(cli, "DEFAULT_ENDPOINT", "tcp://127.0.0.1:48700", raising=False)
        fake_rc = mock.Mock()
        fake_rc.socket_path.return_value = "tcp://127.0.0.1:47000"
        fake_rc.mode.return_value = "on-demand"
        with (
            mock.patch("sw_core.cli._default_runtime_config", return_value=fake_rc),
            mock.patch(
                "sw_core.lock_win._endpoint_alive",
                side_effect=lambda ep: ep == "tcp://127.0.0.1:48700",
            ),
        ):
            assert cli._resolve_endpoint(_args()) == "tcp://127.0.0.1:48700"
        assert "tcp://127.0.0.1:47000" in capsys.readouterr().err

    def test_tcp_config_used_verbatim_when_alive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """config 記錄的 tcp endpoint 可連 → 原值直用，不 fallback。"""
        monkeypatch.setenv("SERIALWRAP_RPC_BACKEND", "win")
        fake_rc = mock.Mock()
        fake_rc.socket_path.return_value = "tcp://127.0.0.1:49001"
        fake_rc.mode.return_value = "on-demand"
        with (
            mock.patch("sw_core.cli._default_runtime_config", return_value=fake_rc),
            mock.patch("sw_core.lock_win._endpoint_alive", return_value=True),
        ):
            assert cli._resolve_endpoint(_args()) == "tcp://127.0.0.1:49001"

    def test_explicit_socket_wins_regardless_of_backend(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SERIALWRAP_RPC_BACKEND", "win")
        assert cli._resolve_endpoint(_args(socket_arg="/tmp/explicit.sock")) == "/tmp/explicit.sock"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
