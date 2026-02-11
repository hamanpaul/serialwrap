from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable
from typing import Any


class JsonRpcUnixServer:
    def __init__(self, socket_path: str, handler: Callable[[str, dict[str, Any]], dict[str, Any]]) -> None:
        self._socket_path = socket_path
        self._handler = handler
        self._server: asyncio.AbstractServer | None = None

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                req_id: Any = None
                try:
                    obj = json.loads(line.decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    resp = {"ok": False, "error_code": "INVALID_JSON"}
                    writer.write((json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8"))
                    await writer.drain()
                    continue

                if not isinstance(obj, dict):
                    resp = {"ok": False, "error_code": "INVALID_REQUEST"}
                    writer.write((json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8"))
                    await writer.drain()
                    continue

                req_id = obj.get("id")
                method = obj.get("method")
                params = obj.get("params")
                if not isinstance(method, str):
                    resp = {"id": req_id, "ok": False, "error_code": "INVALID_METHOD"}
                else:
                    if not isinstance(params, dict):
                        params = {}
                    try:
                        result = self._handler(method, params)
                    except Exception as exc:
                        result = {"ok": False, "error_code": "EXCEPTION", "message": str(exc)}
                    resp = {"id": req_id}
                    if isinstance(result, dict):
                        resp.update(result)
                    else:
                        resp.update({"ok": True, "data": result})

                writer.write((json.dumps(resp, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async def start(self) -> None:
        if os.path.exists(self._socket_path):
            os.unlink(self._socket_path)
        self._server = await asyncio.start_unix_server(self._handle_client, path=self._socket_path)

    async def serve_forever(self) -> None:
        if self._server is None:
            await self.start()
        assert self._server is not None
        async with self._server:
            await self._server.serve_forever()

    async def stop(self) -> None:
        if self._server is None:
            return
        self._server.close()
        await self._server.wait_closed()
        self._server = None
        if os.path.exists(self._socket_path):
            os.unlink(self._socket_path)
