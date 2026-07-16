"""#108 #2：_resolve_endpoint 對 config socket_path dangling 時依 supervision_mode fallback。"""
from __future__ import annotations

import argparse
import os
import unittest
from unittest import mock

from sw_core import cli


class TestResolveEndpointFallback(unittest.TestCase):
    def _args(self, socket: str | None = None, endpoint: str | None = None) -> argparse.Namespace:
        # #120 向量 2 起 --socket argparse default 為 None sentinel：
        # 「未指定」的模擬值即 None（有傳任何值皆視為明確指定）。
        return argparse.Namespace(socket=socket, endpoint=endpoint)

    def test_dangling_config_socket_falls_back_to_system_socket(self) -> None:
        """systemd-system 下 config socket 失聯 → 改連 SYSTEM_SOCKET。"""
        fake_rc = mock.Mock()
        fake_rc.socket_path.return_value = "/tmp/dead-xyz.sock"
        fake_rc.mode.return_value = "systemd-system"
        with (
            mock.patch("sw_core.cli._default_runtime_config", return_value=fake_rc),
            mock.patch("sw_core.cli._endpoint_alive", side_effect=lambda ep: ep == cli.SYSTEM_SOCKET),
            mock.patch("sw_core.cli.sys.stderr"),
        ):
            self.assertEqual(cli._resolve_endpoint(self._args()), cli.SYSTEM_SOCKET)

    def test_alive_config_socket_used_unchanged(self) -> None:
        fake_rc = mock.Mock()
        fake_rc.socket_path.return_value = "/run/serialwrap/serialwrapd.sock"
        fake_rc.mode.return_value = "systemd-system"
        with (
            mock.patch("sw_core.cli._default_runtime_config", return_value=fake_rc),
            mock.patch("sw_core.cli._endpoint_alive", return_value=True),
        ):
            self.assertEqual(
                cli._resolve_endpoint(self._args()), "/run/serialwrap/serialwrapd.sock"
            )

    def test_both_dead_returns_original_config_socket(self) -> None:
        fake_rc = mock.Mock()
        fake_rc.socket_path.return_value = "/tmp/dead-xyz.sock"
        fake_rc.mode.return_value = "systemd-system"
        with (
            mock.patch("sw_core.cli._default_runtime_config", return_value=fake_rc),
            mock.patch("sw_core.cli._endpoint_alive", return_value=False),
            mock.patch("sw_core.cli.sys.stderr"),
        ):
            self.assertEqual(cli._resolve_endpoint(self._args()), "/tmp/dead-xyz.sock")

    def test_explicit_socket_not_overridden_by_fallback(self) -> None:
        with mock.patch("sw_core.cli._endpoint_alive") as alive:
            self.assertEqual(
                cli._resolve_endpoint(self._args(socket="/tmp/explicit.sock")),
                "/tmp/explicit.sock",
            )
            alive.assert_not_called()

    def test_explicit_endpoint_wins(self) -> None:
        with mock.patch("sw_core.cli._endpoint_alive") as alive:
            self.assertEqual(
                cli._resolve_endpoint(self._args(endpoint="tcp://127.0.0.1:7777")),
                "tcp://127.0.0.1:7777",
            )
            alive.assert_not_called()

    def test_unreadable_config_does_not_raise(self) -> None:
        """config.yaml 損壞/不可讀（建構丟例外）→ 不 traceback，回預設 SOCKET_PATH（Codex Important #1）。

        鎖定 posix backend：預設 fallback 自 #131 起平台感知，本測試 pin POSIX 行為。
        """
        with (
            mock.patch.dict(os.environ, {"SERIALWRAP_RPC_BACKEND": "posix"}),
            mock.patch("sw_core.cli._default_runtime_config", side_effect=ValueError("bad yaml")),
        ):
            self.assertEqual(cli._resolve_endpoint(self._args()), cli.SOCKET_PATH)

    def test_unix_scheme_dead_socket_triggers_fallback(self) -> None:
        """config socket_path 為 unix:// scheme 且死 → 仍正確觸發 fallback（Codex Minor #1）。"""
        fake_rc = mock.Mock()
        fake_rc.socket_path.return_value = "unix:///tmp/dead-108.sock"
        fake_rc.mode.return_value = "systemd-system"
        with (
            mock.patch("sw_core.cli._default_runtime_config", return_value=fake_rc),
            mock.patch(
                "sw_core.cli._endpoint_alive",
                side_effect=lambda ep: ep == cli.SYSTEM_SOCKET,
            ),
            mock.patch("sw_core.cli.sys.stderr"),
        ):
            self.assertEqual(cli._resolve_endpoint(self._args()), cli.SYSTEM_SOCKET)


class TestEndpointAlive(unittest.TestCase):
    def test_tcp_endpoint_skipped_as_alive(self) -> None:
        self.assertTrue(cli._endpoint_alive("tcp://127.0.0.1:65000"))

    def test_unparseable_endpoint_skipped_as_alive(self) -> None:
        self.assertTrue(cli._endpoint_alive("tcp://"))  # 無 host/port → ValueError → skip

    def test_dead_plain_unix_path_is_not_alive(self) -> None:
        self.assertFalse(cli._endpoint_alive("/tmp/serialwrap-nonexistent-108.sock"))

    def test_dead_unix_scheme_is_not_alive(self) -> None:
        self.assertFalse(cli._endpoint_alive("unix:///tmp/serialwrap-nonexistent-108.sock"))


class TestRuntimeConfigRobustness(unittest.TestCase):
    """config.yaml 為合法 YAML 但非 dict（純量/list）時不得讓 mode()/socket_path() traceback
    （Codex re-review 殘留路徑：`should_auto_spawn(rc).mode()` 在 wrong-type _data 上 .get）。"""

    def test_non_dict_yaml_coerced_to_empty(self) -> None:
        import tempfile
        from pathlib import Path

        from sw_core.runtime_config import RuntimeConfig

        for content in ("just a scalar string\n", "- a\n- b\n", "42\n"):
            with tempfile.TemporaryDirectory() as td:
                p = Path(td) / "config.yaml"
                p.write_text(content, encoding="utf-8")
                rc = RuntimeConfig(p)
                self.assertIsNone(rc.mode(), f"mode() should be None for {content!r}")
                self.assertIsNone(rc.socket_path(), f"socket_path() should be None for {content!r}")


if __name__ == "__main__":
    unittest.main()
