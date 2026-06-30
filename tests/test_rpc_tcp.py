from __future__ import annotations

import asyncio
import threading

from sw_core.rpc_win import TcpRpcServer
from sw_core.client import rpc_call


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
