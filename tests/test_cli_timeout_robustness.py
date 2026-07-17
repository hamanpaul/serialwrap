"""#123：host 過載/長操作下 CLI 假性 TIMEOUT 修復的單元測試。

涵蓋三個面向：
1. timeout 解析——未顯式指定 ``--timeout`` 時，長操作（session.recover /
   session.attach / session.self_test）採較寬 floor、一般方法維持 5.0；
   顯式指定一律覆蓋。
2. TIMEOUT enrich——``rpc_call`` 逾時後補一次輕量 ``health.ping`` 探測，
   錯誤 JSON 附 ``daemon_reachable``（daemon 活著為 true、死了為 false），
   可達時再附 ``daemon_busy`` 上下文。
3. retry 白名單——``--retries`` 僅作用於冪等唯讀方法白名單；寫入類方法
   （如 session.recover）逾時絕不重送。

以 threading + 本機 AF_UNIX server 直測 ``sw_core.client.rpc_call``，
不需啟動完整 daemon。
"""
from __future__ import annotations

import io
import json
import os
import socket
import tempfile
import threading
import time
import unittest
from collections import Counter
from contextlib import redirect_stdout
from typing import Any, Callable
from unittest.mock import patch

import sw_core.client as client
from sw_core.client import RETRYABLE_READONLY_METHODS, rpc_call
from sw_core.cli import (
    ATTACH_SELF_TEST_TIMEOUT_MARGIN_S,
    DEFAULT_RPC_TIMEOUT_S,
    LONG_RPC_TIMEOUT_FLOOR_S,
    RECOVER_TIMEOUT_MARGIN_S,
    _effective_timeout_s,
    build_parser,
    main,
)


class TestEffectiveTimeout(unittest.TestCase):
    """timeout 解析：未指定時長操作拿 floor、一般方法 5.0、顯式指定覆蓋一切。"""

    def _args(self, argv: list[str]):
        return build_parser().parse_args(argv)

    def test_global_timeout_default_is_none(self) -> None:
        # 全域 --timeout 預設須為 None（=使用者未顯式指定），才能區分顯式與否
        args = self._args(["session", "list"])
        self.assertIsNone(args.timeout_s)

    def test_general_method_uses_default_5s(self) -> None:
        args = self._args(["session", "list"])
        self.assertEqual(_effective_timeout_s(args, "session.list"), DEFAULT_RPC_TIMEOUT_S)
        self.assertEqual(DEFAULT_RPC_TIMEOUT_S, 5.0)

    def test_recover_gets_floor_from_default_recover_timeout(self) -> None:
        # recover_timeout_s 預設 2.0 → max(30, 2+25) = 30
        args = self._args(["session", "recover", "--selector", "COM0"])
        self.assertEqual(
            _effective_timeout_s(args, "session.recover"),
            max(LONG_RPC_TIMEOUT_FLOOR_S, 2.0 + RECOVER_TIMEOUT_MARGIN_S),
        )
        self.assertGreaterEqual(_effective_timeout_s(args, "session.recover"), 30.0)

    def test_recover_floor_scales_with_recover_timeout(self) -> None:
        # 子命令層 --timeout 10（recover_timeout_s）→ max(30, 10+25) = 35
        args = self._args(["session", "recover", "--selector", "COM0", "--timeout", "10"])
        self.assertEqual(_effective_timeout_s(args, "session.recover"), 35.0)

    def test_self_test_gets_floor_from_probe_timeout(self) -> None:
        # probe_timeout_s 預設 2.0 → max(30, 2+15) = 30
        args = self._args(["session", "self-test", "--selector", "COM0"])
        self.assertEqual(
            _effective_timeout_s(args, "session.self_test"),
            max(LONG_RPC_TIMEOUT_FLOOR_S, 2.0 + ATTACH_SELF_TEST_TIMEOUT_MARGIN_S),
        )

    def test_self_test_floor_scales_with_probe_timeout(self) -> None:
        # --probe-timeout 20 → max(30, 20+15) = 35
        args = self._args(
            ["session", "self-test", "--selector", "COM0", "--probe-timeout", "20"]
        )
        self.assertEqual(_effective_timeout_s(args, "session.self_test"), 35.0)

    def test_attach_gets_fixed_floor(self) -> None:
        args = self._args(["session", "attach", "--selector", "COM0"])
        self.assertEqual(_effective_timeout_s(args, "session.attach"), LONG_RPC_TIMEOUT_FLOOR_S)

    def test_explicit_global_timeout_overrides_long_op_floor(self) -> None:
        args = self._args(["--timeout", "3", "session", "recover", "--selector", "COM0"])
        self.assertEqual(_effective_timeout_s(args, "session.recover"), 3.0)

    def test_explicit_global_timeout_overrides_general_default(self) -> None:
        args = self._args(["--timeout", "7.5", "session", "list"])
        self.assertEqual(_effective_timeout_s(args, "session.list"), 7.5)

    def test_file_methods_not_floored(self) -> None:
        # file.push / file.pull 已有自己的節奏，維持一般預設（#123 先不動）
        args = self._args(
            ["file", "push", "--selector", "COM0", "--local", "a", "--remote", "b"]
        )
        self.assertEqual(_effective_timeout_s(args, "file.push"), DEFAULT_RPC_TIMEOUT_S)


class TestCliPassesEffectiveTimeout(unittest.TestCase):
    """CLI main → _run_rpc 實際把 floor / retries 傳進 rpc_call。"""

    def _invoke(self, argv: list[str]) -> Any:
        with patch("sw_core.cli.rpc_call", return_value={"ok": True}) as mock_rpc:
            with redirect_stdout(io.StringIO()):
                main(["--socket", "/tmp/sw123-nonexistent.sock", *argv])
        return mock_rpc.call_args

    def test_recover_without_timeout_uses_floor(self) -> None:
        call = self._invoke(["session", "recover", "--selector", "COM0"])
        self.assertEqual(call.kwargs["timeout_s"], 30.0)

    def test_recover_with_explicit_timeout_uses_it(self) -> None:
        with patch("sw_core.cli.rpc_call", return_value={"ok": True}) as mock_rpc:
            with redirect_stdout(io.StringIO()):
                main([
                    "--socket", "/tmp/sw123-nonexistent.sock", "--timeout", "3",
                    "session", "recover", "--selector", "COM0",
                ])
        self.assertEqual(mock_rpc.call_args.kwargs["timeout_s"], 3.0)

    def test_general_method_uses_default(self) -> None:
        call = self._invoke(["session", "list"])
        self.assertEqual(call.kwargs["timeout_s"], 5.0)

    def test_retries_flag_forwarded(self) -> None:
        with patch("sw_core.cli.rpc_call", return_value={"ok": True}) as mock_rpc:
            with redirect_stdout(io.StringIO()):
                main([
                    "--socket", "/tmp/sw123-nonexistent.sock", "--retries", "2",
                    "session", "list",
                ])
        self.assertEqual(mock_rpc.call_args.kwargs["retries"], 2)

    def test_retries_default_zero(self) -> None:
        call = self._invoke(["session", "list"])
        self.assertEqual(call.kwargs["retries"], 0)


@unittest.skipUnless(hasattr(socket, "AF_UNIX"), "需 AF_UNIX（原生 Windows 無此屬性）")
class _UnixRpcServerMixin(unittest.TestCase):
    """threading + AF_UNIX 的最小 JSON-line RPC 假 server。

    handler(method, params) → dict 立即回應；→ None 表示「收下但不回應」
    （模擬 daemon 端長操作把 CLI 拖過 socket timeout）。
    """

    def _start_server(
        self,
        handler: Callable[[str, dict[str, Any]], dict[str, Any] | None],
        *,
        close_listener_after_first: bool = False,
    ) -> str:
        self._tmpdir = tempfile.mkdtemp(prefix="sw123-")
        path = os.path.join(self._tmpdir, "s.sock")
        self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._listener.bind(path)
        self._listener.listen(8)
        self.method_conns: Counter[str] = Counter()
        self._counter_lock = threading.Lock()
        self._open_conns: list[socket.socket] = []

        def _handle(conn: socket.socket) -> None:
            try:
                buf = b""
                while b"\n" not in buf:
                    chunk = conn.recv(65536)
                    if not chunk:
                        return
                    buf += chunk
                req = json.loads(buf.split(b"\n", 1)[0])
                method = str(req.get("method"))
                with self._counter_lock:
                    self.method_conns[method] += 1
                resp = handler(method, req.get("params") or {})
                if resp is None:
                    # 不回應：掛住連線直到測試結束（client 端自行逾時）
                    time.sleep(30.0)
                    return
                conn.sendall(json.dumps(resp).encode("utf-8") + b"\n")
            except OSError:
                pass
            finally:
                try:
                    conn.close()
                except OSError:
                    pass

        def _serve() -> None:
            first = True
            while True:
                try:
                    conn, _ = self._listener.accept()
                except OSError:
                    return
                self._open_conns.append(conn)
                threading.Thread(target=_handle, args=(conn,), daemon=True).start()
                if close_listener_after_first and first:
                    # 模擬「daemon 收下請求後死亡」：之後的探測連線一律被拒
                    self._listener.close()
                    return
                first = False

        self._accept_thread = threading.Thread(target=_serve, daemon=True)
        self._accept_thread.start()
        self.addCleanup(self._stop_server)
        return path

    def _stop_server(self) -> None:
        try:
            self._listener.close()
        except OSError:
            pass
        for conn in self._open_conns:
            try:
                conn.close()
            except OSError:
                pass


class TestTimeoutEnrichment(_UnixRpcServerMixin):
    """TIMEOUT 錯誤 enrich：附 daemon_reachable（活著 true / 死了 false）。"""

    def test_timeout_with_live_daemon_reports_reachable_and_busy(self) -> None:
        def handler(method: str, _params: dict[str, Any]) -> dict[str, Any] | None:
            if method == "health.ping":
                return {"ok": True, "pong": True}
            if method == "health.status":
                return {"ok": True, "commands": 3, "sessions": 2}
            return None  # 長操作：不回應，讓 client 逾時

        path = self._start_server(handler)
        resp = rpc_call(path, "session.recover", {"selector": "COM0"}, timeout_s=0.3)
        self.assertFalse(resp.get("ok"))
        self.assertEqual(resp.get("error_code"), "TIMEOUT")
        self.assertIs(resp.get("daemon_reachable"), True)
        self.assertEqual(resp.get("daemon_busy"), {"commands": 3, "sessions": 2})

    def test_timeout_with_dead_daemon_reports_unreachable(self) -> None:
        path = self._start_server(
            lambda _m, _p: None, close_listener_after_first=True
        )
        resp = rpc_call(path, "session.recover", {"selector": "COM0"}, timeout_s=0.3)
        self.assertFalse(resp.get("ok"))
        self.assertEqual(resp.get("error_code"), "TIMEOUT")
        self.assertIs(resp.get("daemon_reachable"), False)
        self.assertNotIn("daemon_busy", resp)

    def test_timeout_busy_omitted_when_status_unavailable(self) -> None:
        def handler(method: str, _params: dict[str, Any]) -> dict[str, Any] | None:
            if method == "health.ping":
                return {"ok": True, "pong": True}
            return None  # health.status 也不回應 → 省略 daemon_busy

        path = self._start_server(handler)
        resp = rpc_call(path, "session.recover", {"selector": "COM0"}, timeout_s=0.3)
        self.assertEqual(resp.get("error_code"), "TIMEOUT")
        self.assertIs(resp.get("daemon_reachable"), True)
        self.assertNotIn("daemon_busy", resp)

    def test_success_path_not_enriched(self) -> None:
        def handler(method: str, _params: dict[str, Any]) -> dict[str, Any] | None:
            return {"ok": True, "sessions": []}

        path = self._start_server(handler)
        resp = rpc_call(path, "session.list", {}, timeout_s=1.0)
        self.assertTrue(resp.get("ok"))
        self.assertNotIn("daemon_reachable", resp)

    def test_probe_methods_not_recursively_enriched(self) -> None:
        # health.ping 自身逾時不再遞迴探測（避免 doctor 的 0.5s ping 被拖慢）
        path = self._start_server(lambda _m, _p: None)
        resp = rpc_call(path, "health.ping", {}, timeout_s=0.3)
        self.assertEqual(resp.get("error_code"), "TIMEOUT")
        self.assertNotIn("daemon_reachable", resp)
        with self._counter_lock:
            self.assertEqual(self.method_conns["health.ping"], 1)


class TestRetryWhitelist(_UnixRpcServerMixin):
    """retry 白名單：非白名單不重試、白名單依 --retries 重試並可中途成功。"""

    def setUp(self) -> None:
        super().setUp()
        # 縮短退避讓測試跑得快；仍驗證「有睡過退避」由邏輯路徑涵蓋
        self._backoff_patch = patch.object(client, "_RETRY_BACKOFF_BASE_S", 0.01)
        self._backoff_patch.start()
        self.addCleanup(self._backoff_patch.stop)

    def test_whitelist_contains_expected_readonly_methods(self) -> None:
        for method in ("session.list", "health.ping", "health.status", "device.list"):
            self.assertIn(method, RETRYABLE_READONLY_METHODS)
        # 寫入類絕不可入白名單
        for method in ("session.recover", "session.attach", "command.submit", "wal.reset"):
            self.assertNotIn(method, RETRYABLE_READONLY_METHODS)

    def test_non_whitelist_method_not_retried(self) -> None:
        path = self._start_server(
            lambda m, _p: {"ok": True, "pong": True} if m.startswith("health.") else None
        )
        resp = rpc_call(path, "session.recover", {"selector": "COM0"}, timeout_s=0.2, retries=2)
        self.assertEqual(resp.get("error_code"), "TIMEOUT")
        with self._counter_lock:
            self.assertEqual(self.method_conns["session.recover"], 1)

    def test_whitelist_method_retried_n_times(self) -> None:
        # session.list 一律不回應；health.* 正常回應（供 TIMEOUT 探測用）
        path = self._start_server(
            lambda m, _p: {"ok": True} if m.startswith("health.") else None
        )
        resp = rpc_call(path, "session.list", {}, timeout_s=0.2, retries=2)
        self.assertEqual(resp.get("error_code"), "TIMEOUT")
        with self._counter_lock:
            # 1 次原始 + 2 次 retry
            self.assertEqual(self.method_conns["session.list"], 3)

    def test_whitelist_method_stops_on_success(self) -> None:
        fail_first = {"remaining": 1}

        def handler(method: str, _params: dict[str, Any]) -> dict[str, Any] | None:
            if method != "session.list":
                return {"ok": True}
            if fail_first["remaining"] > 0:
                fail_first["remaining"] -= 1
                return None  # 第一次逾時
            return {"ok": True, "sessions": []}

        path = self._start_server(handler)
        resp = rpc_call(path, "session.list", {}, timeout_s=0.2, retries=3)
        self.assertTrue(resp.get("ok"))
        with self._counter_lock:
            self.assertEqual(self.method_conns["session.list"], 2)

    def test_retries_zero_keeps_single_attempt(self) -> None:
        path = self._start_server(
            lambda m, _p: {"ok": True} if m.startswith("health.") else None
        )
        rpc_call(path, "session.list", {}, timeout_s=0.2)
        with self._counter_lock:
            self.assertEqual(self.method_conns["session.list"], 1)

    def test_whitelist_method_retried_on_connect_failure(self) -> None:
        # 連線失敗（SOCKET_ERROR）也屬可重試：對不存在的 socket 連 3 次都失敗
        with patch.object(client, "_rpc_call_once", wraps=client._rpc_call_once) as spy:
            resp = rpc_call("/tmp/sw123-no-such-daemon.sock", "session.list", {}, timeout_s=0.2, retries=2)
        self.assertFalse(resp.get("ok"))
        self.assertEqual(resp.get("error_code"), "SOCKET_ERROR")
        # 3 次嘗試；SOCKET_ERROR 不觸發 TIMEOUT 探測
        self.assertEqual(spy.call_count, 3)


if __name__ == "__main__":
    unittest.main()
