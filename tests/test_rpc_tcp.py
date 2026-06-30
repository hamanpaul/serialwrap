from __future__ import annotations

import asyncio
import threading

import pytest

from sw_core.rpc_win import TcpRpcServer, _parse_tcp
from sw_core.client import rpc_call


@pytest.mark.parametrize("endpoint", [
    "tcp://0.0.0.0:48700",
    "tcp://192.168.1.100:48700",
    "tcp://10.0.0.1:48700",
    "tcp://[::]:48700",  # all-interfaces IPv6（正確方括號格式，非 loopback）
])
def test_non_loopback_host_rejected(endpoint):
    """非 loopback host 應被 _parse_tcp 拒絕，避免 RPC server 對外暴露（Copilot review fix）。"""
    with pytest.raises(ValueError, match="loopback"):
        _parse_tcp(endpoint)


@pytest.mark.parametrize("endpoint,expected_host", [
    ("tcp://127.0.0.1:48700", "127.0.0.1"),
    ("tcp://localhost:48700", "localhost"),
    ("tcp://[::1]:48700", "::1"),  # IPv6 loopback 需方括號格式
])
def test_loopback_host_accepted(endpoint, expected_host):
    """loopback host 應被 _parse_tcp 接受。"""
    host, port = _parse_tcp(endpoint)
    assert host == expected_host
    assert port == 48700


def test_tcp_rpc_round_trip():
    """TCP loopback JSON-RPC 來回測試：client 送 echo，server 回 {ok, got}。"""
    endpoint = "tcp://127.0.0.1:48721"

    def handler(method, params):
        if method == "echo":
            return {"ok": True, "got": params}
        return {"ok": False, "error_code": "UNKNOWN_METHOD"}

    loop = asyncio.new_event_loop()
    server = TcpRpcServer(endpoint, handler)
    ready = threading.Event()
    _stop_ev: asyncio.Event | None = None

    async def run():
        nonlocal _stop_ev
        # 在 loop 內建立 Event，避免懸掛 Task 產生 warning
        _stop_ev = asyncio.Event()
        await server.start()
        ready.set()
        await _stop_ev.wait()  # 等待明確 stop 信號，run() 正常返回不留 pending task

    def serve():
        asyncio.set_event_loop(loop)
        loop.run_until_complete(run())

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    assert ready.wait(timeout=5.0)
    try:
        resp = rpc_call(endpoint, "echo", {"x": 1})
        assert resp["ok"] is True
        assert resp["got"] == {"x": 1}
    finally:
        # 先正確關閉 server，再解除 _stop_ev 讓 run() 自然結束
        asyncio.run_coroutine_threadsafe(server.stop(), loop).result(timeout=2.0)
        loop.call_soon_threadsafe(_stop_ev.set)
        t.join(timeout=2.0)
