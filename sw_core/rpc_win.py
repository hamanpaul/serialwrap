from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

from sw_core.constants import LOOPBACK_TCP_HOSTS
from sw_core.rpc_posix import serve_connection


class TcpRpcServer:
    """TCP loopback JSON-RPC server（Windows daemon RPC plane，#84 PORT-4）。

    介面與 JsonRpcUnixServer 對齊（start/serve_forever/stop），
    分派邏輯共用 serve_connection，不重複 transport-無關程式碼（DRY）。
    """

    def __init__(
        self,
        endpoint: str,
        handler: Callable[[str, dict[str, Any]], dict[str, Any]],
        *,
        blocking_methods: set[str] | None = None,
    ) -> None:
        self._endpoint = endpoint
        self._handler = handler
        self._blocking_methods = blocking_methods or set()
        self._server: asyncio.AbstractServer | None = None
        host, port = _parse_tcp(endpoint)
        self._host = host
        self._port = port

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await serve_connection(reader, writer, self._handler, self._blocking_methods)

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle_client,
            host=self._host,
            port=self._port,
        )

    async def serve_forever(self) -> None:
        if self._server is None:
            await self.start()
        assert self._server is not None
        async with self._server:
            await self._server.serve_forever()
        # async with 離開後 server 已關閉，置 None 避免 stop() 重複 close
        self._server = None

    async def stop(self) -> None:
        if self._server is None:
            return
        self._server.close()
        await self._server.wait_closed()
        self._server = None


# 單一事實來源移至 constants（#131：CLI 的 daemon start loopback 白名單共用）。
_LOOPBACK_HOSTS = LOOPBACK_TCP_HOSTS


def _parse_tcp(endpoint: str) -> tuple[str, int]:
    """解析 tcp://host:port 格式的 endpoint，回傳 (host, port)。

    僅允許 loopback 位址（127.0.0.1/localhost/::1）作為 server bind host，
    拒絕任何非 loopback host（如 0.0.0.0 或 LAN IP），避免 RPC 對外暴露。
    """
    parsed = urlsplit(endpoint)
    if parsed.scheme != "tcp" or not parsed.hostname or parsed.port is None:
        raise ValueError(f"invalid tcp endpoint: {endpoint!r}")
    host = parsed.hostname
    if host not in _LOOPBACK_HOSTS:
        raise ValueError(
            f"RPC server 僅允許 loopback 位址（127.0.0.1/localhost/::1），"
            f"拒絕 {host!r} 避免遠端暴露（RPC 無認證，非 loopback 會讓遠端呼叫所有命令）"
        )
    return host, parsed.port
