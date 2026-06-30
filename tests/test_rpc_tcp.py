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

    async def run():
        await server.start()
        ready.set()
        await asyncio.Event().wait()  # 阻塞直到外部 stop

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
        loop.call_soon_threadsafe(loop.stop)
