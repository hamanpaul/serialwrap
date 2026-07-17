"""#123：host 過載/長操作下 CLI 假性 TIMEOUT 修復的單元測試。

涵蓋四個面向：
1. timeout 解析——未顯式指定 ``--timeout`` 時，長操作（session.recover /
   session.attach / session.self_test / session.console_attach，MAJOR-1
   補上 console_attach）一律採固定 floor（45.0，MINOR-2：不再隨
   recover_timeout_s／probe_timeout_s 縮放——daemon 端對這兩個 CLI 參數皆有
   cap，該推導前提本來就錯）、一般方法維持 5.0；顯式指定一律覆蓋。
2. TIMEOUT enrich——``rpc_call`` 逾時後補一次輕量 ``health.ping`` 探測，
   錯誤 JSON 附 ``daemon_reachable``（daemon 活著為 true、死了為 false），
   可達時再附 ``daemon_busy`` 上下文；MINOR-3：探測整段另有 wall-clock
   deadline，超過即放棄剩餘階段。
3. retry 白名單——``--retries`` 僅作用於冪等唯讀方法白名單；寫入類方法
   （如 session.recover）逾時絕不重送；NIT-6 起 ``EMPTY_RESPONSE`` 與
   ``SOCKET_ERROR``／``TIMEOUT`` 同等可重試；NIT-7 單次退避 delay 有上限。

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
    DEFAULT_RPC_TIMEOUT_S,
    LONG_RPC_TIMEOUT_FLOOR_S,
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

    def test_recover_gets_fixed_floor_regardless_of_recover_timeout(self) -> None:
        # MINOR-2：floor 為固定常數 45.0，recover_timeout_s 預設 2.0 不影響結果
        # （daemon 端 min(timeout_s, 2.0) cap 讓超過 2.0 的值本就無作用）。
        args = self._args(["session", "recover", "--selector", "COM0"])
        self.assertEqual(_effective_timeout_s(args, "session.recover"), LONG_RPC_TIMEOUT_FLOOR_S)
        self.assertEqual(LONG_RPC_TIMEOUT_FLOOR_S, 45.0)

    def test_recover_floor_does_not_scale_with_recover_timeout(self) -> None:
        # 子命令層 --timeout 10（recover_timeout_s）不再讓 floor 隨之成長
        # （MINOR-2：該推導前提已證實錯誤，改為固定 45.0）。
        args = self._args(["session", "recover", "--selector", "COM0", "--timeout", "10"])
        self.assertEqual(_effective_timeout_s(args, "session.recover"), 45.0)

    def test_self_test_gets_fixed_floor_regardless_of_probe_timeout(self) -> None:
        args = self._args(["session", "self-test", "--selector", "COM0"])
        self.assertEqual(_effective_timeout_s(args, "session.self_test"), LONG_RPC_TIMEOUT_FLOOR_S)

    def test_self_test_floor_does_not_scale_with_probe_timeout(self) -> None:
        # --probe-timeout 20 不再讓 floor 隨之成長，維持固定 45.0（MINOR-2）。
        args = self._args(
            ["session", "self-test", "--selector", "COM0", "--probe-timeout", "20"]
        )
        self.assertEqual(_effective_timeout_s(args, "session.self_test"), 45.0)

    def test_attach_gets_fixed_floor(self) -> None:
        args = self._args(["session", "attach", "--selector", "COM0"])
        self.assertEqual(_effective_timeout_s(args, "session.attach"), LONG_RPC_TIMEOUT_FLOOR_S)

    def test_console_attach_gets_fixed_floor(self) -> None:
        # MAJOR-1：session.console_attach 在 daemon 端 BLOCKING_RPC_METHODS
        # 內（recover 升級分支可同步跑數十秒），CLI 側 floor 先前漏了它。
        args = self._args(["session", "console-attach", "--selector", "COM0"])
        self.assertEqual(_effective_timeout_s(args, "session.console_attach"), LONG_RPC_TIMEOUT_FLOOR_S)

    def test_explicit_global_timeout_overrides_long_op_floor(self) -> None:
        args = self._args(["--timeout", "3", "session", "recover", "--selector", "COM0"])
        self.assertEqual(_effective_timeout_s(args, "session.recover"), 3.0)

    def test_explicit_global_timeout_overrides_console_attach_floor(self) -> None:
        args = self._args(["--timeout", "3", "session", "console-attach", "--selector", "COM0"])
        self.assertEqual(_effective_timeout_s(args, "session.console_attach"), 3.0)

    def test_explicit_global_timeout_overrides_general_default(self) -> None:
        args = self._args(["--timeout", "7.5", "session", "list"])
        self.assertEqual(_effective_timeout_s(args, "session.list"), 7.5)

    def test_file_methods_not_floored(self) -> None:
        # file.push / file.pull 為已知缺口（chunk 傳輸分鐘級、CLI 無可靠信號
        # 推得傳輸時長），暫維持一般預設、defer 至 follow-up（MINOR-5，#123）。
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
        self.assertEqual(call.kwargs["timeout_s"], 45.0)

    def test_console_attach_without_timeout_uses_floor(self) -> None:
        # MAJOR-1：console-attach 走完整 CLI 分派也要拿到 floor，不只是
        # _effective_timeout_s 單元層級正確。
        call = self._invoke(["session", "console-attach", "--selector", "COM0"])
        self.assertEqual(call.kwargs["timeout_s"], 45.0)

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
    （模擬 daemon 端長操作把 CLI 拖過 socket timeout）；→ 常數字串
    ``_CLOSE_EMPTY`` 表示「立即關閉連線、不送任何位元組」（NIT-6：模擬
    client 端收到 ``EMPTY_RESPONSE`` 的情境，例如 daemon 重啟撞上請求中）。
    """

    _CLOSE_EMPTY = "__CLOSE_EMPTY__"

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
                if resp == self._CLOSE_EMPTY:
                    # 立即關閉、不送任何位元組：client 端 recv() 馬上收到 EOF
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

    def test_probe_gives_up_status_stage_past_wall_clock_deadline(self) -> None:
        # MINOR-3：即使 ping 在其自身 1s timeout 內回應，若期間已耗掉超過
        # _PROBE_DEADLINE_S（monotonic 起算），status 這段就該放棄、省略
        # daemon_busy，而非只靠兩段個別 timeout 相加的隱含上限。
        def fake_once(endpoint: str, method: str, _params: dict[str, Any], *, req_id: int = 0, timeout_s: float = 1.0) -> dict[str, Any]:
            if method == "health.ping":
                return {"ok": True}
            return {"ok": True, "commands": 1, "sessions": 1}

        # 第一次呼叫（算 deadline）回 0.0；ping 後檢查 deadline 回 100.0（已超過）
        times = iter([0.0, 100.0])
        with patch.object(client, "_rpc_call_once", side_effect=fake_once) as spy, \
             patch("time.monotonic", side_effect=lambda: next(times)):
            info = client._probe_daemon_after_timeout("dummy-endpoint")
        self.assertIs(info.get("daemon_reachable"), True)
        self.assertNotIn("daemon_busy", info)
        called_methods = [c.args[1] for c in spy.call_args_list]
        self.assertEqual(called_methods, ["health.ping"])  # health.status 未被呼叫


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

    def test_whitelist_method_retried_on_empty_response(self) -> None:
        # NIT-6：EMPTY_RESPONSE（對端立即關閉連線、無位元組）與 SOCKET_ERROR／
        # TIMEOUT 同等視為暫時性、可重試；第一次 EMPTY_RESPONSE 後第二次成功。
        calls = {"n": 0}

        def handler(method: str, _params: dict[str, Any]) -> Any:
            if method.startswith("health."):
                return {"ok": True}
            calls["n"] += 1
            if calls["n"] == 1:
                return self._CLOSE_EMPTY
            return {"ok": True, "sessions": []}

        path = self._start_server(handler)
        resp = rpc_call(path, "session.list", {}, timeout_s=0.5, retries=2)
        self.assertTrue(resp.get("ok"))
        with self._counter_lock:
            self.assertEqual(self.method_conns["session.list"], 2)

    def test_non_whitelist_method_not_retried_on_empty_response(self) -> None:
        path = self._start_server(
            lambda m, _p: {"ok": True} if m.startswith("health.") else self._CLOSE_EMPTY
        )
        resp = rpc_call(path, "session.recover", {"selector": "COM0"}, timeout_s=0.5, retries=2)
        self.assertEqual(resp.get("error_code"), "EMPTY_RESPONSE")
        with self._counter_lock:
            self.assertEqual(self.method_conns["session.recover"], 1)


class TestRetryBackoffCap(unittest.TestCase):
    """NIT-7：單次退避 delay 有上限，不會隨 retries 指數爆炸。"""

    def test_single_delay_capped(self) -> None:
        with patch.object(client, "_RETRY_BACKOFF_BASE_S", 4.0), \
             patch.object(client, "_rpc_call_once", return_value={"ok": False, "error_code": "TIMEOUT"}), \
             patch.object(client.time, "sleep") as mock_sleep:
            rpc_call("/tmp/sw123-nonexistent.sock", "session.list", {}, timeout_s=0.1, retries=3)
        delays = [call.args[0] for call in mock_sleep.call_args_list]
        # 未 cap 理論值為 4.0/8.0/16.0；夾在 _RETRY_BACKOFF_MAX_S=5.0 後應為
        # 4.0/5.0/5.0——第一次本就 <5.0 不受影響，之後每次皆被夾住。
        self.assertEqual(delays, [4.0, client._RETRY_BACKOFF_MAX_S, client._RETRY_BACKOFF_MAX_S])


if __name__ == "__main__":
    unittest.main()
