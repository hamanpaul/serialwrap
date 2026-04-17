"""remote-support endpoint 解析與 transport branch 測試。"""
from __future__ import annotations

import json
import socket
import threading
import unittest
from unittest.mock import MagicMock, call, patch

from sw_core.client import _parse_endpoint, rpc_call


class TestParseEndpoint(unittest.TestCase):
    def test_tcp_valid(self) -> None:
        transport, addr = _parse_endpoint("tcp://192.168.1.10:7777")
        self.assertEqual(transport, "tcp")
        self.assertEqual(addr, ("192.168.1.10", 7777))

    def test_tcp_localhost(self) -> None:
        transport, addr = _parse_endpoint("tcp://127.0.0.1:9000")
        self.assertEqual(transport, "tcp")
        self.assertEqual(addr, ("127.0.0.1", 9000))

    def test_unix_scheme(self) -> None:
        transport, addr = _parse_endpoint("unix:///tmp/serialwrap/serialwrapd.sock")
        self.assertEqual(transport, "unix")
        self.assertEqual(addr, "/tmp/serialwrap/serialwrapd.sock")

    def test_plain_path(self) -> None:
        transport, addr = _parse_endpoint("/tmp/serialwrap/serialwrapd.sock")
        self.assertEqual(transport, "unix")
        self.assertEqual(addr, "/tmp/serialwrap/serialwrapd.sock")

    def test_tcp_missing_port(self) -> None:
        with self.assertRaises(ValueError):
            _parse_endpoint("tcp://192.168.1.10")

    def test_tcp_missing_host(self) -> None:
        with self.assertRaises(ValueError):
            _parse_endpoint("tcp://:7777")

    def test_unix_empty_path(self) -> None:
        with self.assertRaises(ValueError):
            _parse_endpoint("unix://")

    def test_unix_requires_absolute_path(self) -> None:
        with self.assertRaises(ValueError):
            _parse_endpoint("unix://tmp/serialwrapd.sock")

    def test_unsupported_scheme(self) -> None:
        with self.assertRaises(ValueError):
            _parse_endpoint("http://127.0.0.1:7777")

    def test_tcp_rejects_path_suffix(self) -> None:
        with self.assertRaises(ValueError):
            _parse_endpoint("tcp://127.0.0.1:7777/rpc")


class TestRpcCallEndpointBranch(unittest.TestCase):
    """確認 rpc_call 依 endpoint scheme 選擇正確 transport。"""

    def _make_ok_response(self) -> bytes:
        return json.dumps({"ok": True, "result": "pong"}).encode() + b"\n"

    def test_invalid_endpoint_returns_structured_error(self) -> None:
        resp = rpc_call("tcp://nohost", "health.ping", {})
        self.assertFalse(resp.get("ok"))
        self.assertEqual(resp.get("error_code"), "INVALID_ENDPOINT")

    def test_unix_path_uses_af_unix(self) -> None:
        mock_sock = MagicMock()
        mock_sock.recv.return_value = self._make_ok_response()
        mock_sock.__enter__ = lambda s: s
        mock_sock.__exit__ = MagicMock(return_value=False)

        with patch("socket.socket", return_value=mock_sock) as mock_socket_cls:
            rpc_call("/tmp/fake.sock", "health.ping", {})
        mock_socket_cls.assert_called_once_with(socket.AF_UNIX, socket.SOCK_STREAM)
        mock_sock.connect.assert_called_once_with("/tmp/fake.sock")

    def test_unix_scheme_uses_af_unix(self) -> None:
        mock_sock = MagicMock()
        mock_sock.recv.return_value = self._make_ok_response()

        with patch("socket.socket", return_value=mock_sock) as mock_socket_cls:
            rpc_call("unix:///tmp/fake.sock", "health.ping", {})
        mock_socket_cls.assert_called_once_with(socket.AF_UNIX, socket.SOCK_STREAM)
        mock_sock.connect.assert_called_once_with("/tmp/fake.sock")

    def test_tcp_endpoint_uses_create_connection(self) -> None:
        mock_sock = MagicMock()
        mock_sock.recv.return_value = self._make_ok_response()

        with patch("socket.create_connection", return_value=mock_sock) as mock_cc:
            rpc_call("tcp://127.0.0.1:7777", "health.ping", {})
        mock_cc.assert_called_once_with(("127.0.0.1", 7777), timeout=5.0)
        mock_sock.settimeout.assert_called_once_with(5.0)

    def test_socket_error_returns_structured_error(self) -> None:
        with patch("socket.socket") as mock_socket_cls:
            mock_socket_cls.return_value.connect.side_effect = OSError("connection refused")
            resp = rpc_call("/tmp/nonexistent.sock", "health.ping", {})
        self.assertFalse(resp.get("ok"))
        self.assertEqual(resp.get("error_code"), "SOCKET_ERROR")

    def test_tcp_socket_error_returns_structured_error(self) -> None:
        with patch("socket.create_connection", side_effect=OSError("connection refused")):
            resp = rpc_call("tcp://127.0.0.1:7777", "health.ping", {})
        self.assertFalse(resp.get("ok"))
        self.assertEqual(resp.get("error_code"), "SOCKET_ERROR")

    def test_tcp_connect_timeout_returns_timeout(self) -> None:
        with patch("socket.create_connection", side_effect=socket.timeout("timed out")):
            resp = rpc_call("tcp://127.0.0.1:7777", "health.ping", {})
        self.assertFalse(resp.get("ok"))
        self.assertEqual(resp.get("error_code"), "TIMEOUT")

    def test_unsupported_scheme_returns_invalid_endpoint(self) -> None:
        resp = rpc_call("http://127.0.0.1:7777", "health.ping", {})
        self.assertFalse(resp.get("ok"))
        self.assertEqual(resp.get("error_code"), "INVALID_ENDPOINT")


class TestRpcCallOverRealTcpSocket(unittest.TestCase):
    """以 real loopback TCP server 驗證完整 rpc_call TCP path。"""

    def setUp(self) -> None:
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind(("127.0.0.1", 0))
        self._server.listen(1)
        self._port = self._server.getsockname()[1]
        self._response = json.dumps({"ok": True, "result": "pong"}).encode() + b"\n"

        def _serve() -> None:
            try:
                conn, _ = self._server.accept()
                conn.recv(4096)
                conn.sendall(self._response)
                conn.close()
            except OSError:
                pass

        self._thread = threading.Thread(target=_serve, daemon=True)
        self._thread.start()

    def tearDown(self) -> None:
        self._server.close()

    def test_tcp_rpc_call_succeeds(self) -> None:
        resp = rpc_call(f"tcp://127.0.0.1:{self._port}", "health.ping", {})
        self.assertTrue(resp.get("ok"))


class TestCliEndpoint(unittest.TestCase):
    """CLI --endpoint / _resolve_endpoint 行為測試。"""

    def test_resolve_endpoint_prefers_endpoint_over_socket(self) -> None:
        import argparse
        from sw_core.cli import _resolve_endpoint

        args = argparse.Namespace(socket="/tmp/default.sock", endpoint="tcp://127.0.0.1:7777")
        self.assertEqual(_resolve_endpoint(args), "tcp://127.0.0.1:7777")

    def test_resolve_endpoint_falls_back_to_socket(self) -> None:
        import argparse
        from sw_core.cli import _resolve_endpoint

        args = argparse.Namespace(socket="/tmp/default.sock", endpoint=None)
        self.assertEqual(_resolve_endpoint(args), "/tmp/default.sock")

    def test_daemon_start_rejects_endpoint(self) -> None:
        from sw_core.cli import main
        import io
        from contextlib import redirect_stdout

        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = main(["--endpoint", "tcp://1.2.3.4:7777", "daemon", "start"])
        self.assertEqual(rc, 2)
        resp = json.loads(buf.getvalue())
        self.assertEqual(resp.get("error_code"), "REMOTE_NOT_SUPPORTED")

    def test_parser_has_endpoint_argument(self) -> None:
        from sw_core.cli import build_parser
        p = build_parser()
        # 確認 --endpoint 存在，不會 raise
        args = p.parse_args(["--endpoint", "tcp://127.0.0.1:7777", "daemon", "status"])
        self.assertEqual(args.endpoint, "tcp://127.0.0.1:7777")


class TestMcpEndpoint(unittest.TestCase):
    """MCP server --endpoint 參數測試。"""

    def test_mcp_parser_has_endpoint(self) -> None:
        import argparse
        from sw_mcp.server import main
        # 直接解析 argv，確認 --endpoint 不會 argparse error
        import sys
        from io import StringIO
        with patch("sw_mcp.server.call_tool", return_value={"ok": True}):
            with patch("sys.stdout", new_callable=StringIO):
                rc = main(["--endpoint", "tcp://127.0.0.1:7777", "--tool", "serialwrap_ping"])
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
