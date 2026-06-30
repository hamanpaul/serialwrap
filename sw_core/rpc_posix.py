from __future__ import annotations

import asyncio
import json
import os
import shutil
from collections.abc import Callable
from typing import Any


class JsonRpcUnixServer:
    def __init__(
        self,
        socket_path: str,
        handler: Callable[[str, dict[str, Any]], dict[str, Any]],
        *,
        blocking_methods: set[str] | None = None,
    ) -> None:
        self._socket_path = socket_path
        self._handler = handler
        # 這些 method 的 handler 會長時間阻塞（如 file.push/file.pull 的 UART 傳輸），
        # 須丟到 executor 執行，避免卡住單執行緒 asyncio event loop 而凍結其他 RPC（#52）。
        self._blocking_methods = blocking_methods or set()
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
                        if method in self._blocking_methods:
                            # 長阻塞 handler（如 file.push/file.pull）丟到 executor 執行，
                            # 釋放 event loop 以繼續服務其他 RPC，避免傳輸期間全 daemon 凍結（#52）。
                            loop = asyncio.get_running_loop()
                            result = await loop.run_in_executor(None, self._handler, method, params)
                        else:
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
        self._secure_socket()

    def _secure_socket(self) -> None:
        """限制 socket 權限為 0660，並（若有指定）chgrp 到 SERIALWRAP_SOCKET_GROUP。

        systemd-system 模式下 daemon 以 serialwrap 帳號跑，需把 socket 群組設為 dialout（由 unit
        的 Environment 指定），讓同群組的其他使用者 CLI 連得上；同時避免 world 可寫（Codex #2）。
        user 模式 socket 在 $XDG_RUNTIME_DIR、本就僅本人可及，0660 無妨。
        """
        try:
            os.chmod(self._socket_path, 0o660)
        except OSError:
            pass
        group = os.environ.get("SERIALWRAP_SOCKET_GROUP")
        if group and group.strip():
            try:
                shutil.chown(self._socket_path, group=group.strip())
            except (OSError, LookupError):
                pass

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
