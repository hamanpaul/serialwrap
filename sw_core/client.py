from __future__ import annotations

import json
import socket
from typing import Any
from urllib.parse import urlsplit


def _parse_endpoint(endpoint: str) -> tuple[str, tuple[str, int] | str]:
    """解析 endpoint 字串，回傳 (transport, address)。

    支援格式：
    - ``tcp://host:port``        → ("tcp", ("host", port))
    - ``unix:///path/to/sock``   → ("unix", "/path/to/sock")
    - ``/path/to/sock``          → ("unix", "/path/to/sock")

    解析失敗時 raise ``ValueError``。
    """
    if "://" in endpoint:
        parsed = urlsplit(endpoint)
        if parsed.scheme == "tcp":
            try:
                port = parsed.port
            except ValueError as exc:
                raise ValueError(f"invalid tcp endpoint: {endpoint!r}") from exc
            host = parsed.hostname
            if parsed.path or parsed.query or parsed.fragment or not host or port is None:
                raise ValueError(f"invalid tcp endpoint: {endpoint!r}")
            return "tcp", (host, port)

        if parsed.scheme == "unix":
            path = parsed.path
            if parsed.netloc:
                raise ValueError(
                    f"invalid unix endpoint: {endpoint!r} "
                    "(unix endpoint must use an absolute path such as 'unix:///path/to/sock')"
                )
            if parsed.query or parsed.fragment or not path or not path.startswith("/"):
                raise ValueError(
                    f"invalid unix endpoint: {endpoint!r} "
                    "(unix endpoint path must be absolute)"
                )
            return "unix", path

        raise ValueError(f"unsupported endpoint scheme: {parsed.scheme!r}")
    # plain path (backward compat)
    return "unix", endpoint


def rpc_call(socket_path: str, method: str, params: dict[str, Any], *, req_id: int = 1, timeout_s: float = 5.0) -> dict[str, Any]:
    """執行 RPC 呼叫。

    ``socket_path`` 可以是：
    - 純路徑（AF_UNIX，向後相容）
    - ``unix:///path``（AF_UNIX）
    - ``tcp://host:port``（AF_INET，用於 ssh-tunnel 連接遠端 daemon）
    """
    try:
        transport, address = _parse_endpoint(socket_path)
    except ValueError as exc:
        return {"ok": False, "error_code": "INVALID_ENDPOINT", "message": str(exc)}

    try:
        if transport == "tcp":
            sock = socket.create_connection(address, timeout=timeout_s)  # type: ignore[arg-type]
            sock.settimeout(timeout_s)
        else:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(timeout_s)
            sock.connect(address)  # type: ignore[arg-type]
    except socket.timeout:
        return {"ok": False, "error_code": "TIMEOUT"}
    except OSError as exc:
        return {"ok": False, "error_code": "SOCKET_ERROR", "message": str(exc)}

    try:
        req = {"id": req_id, "method": method, "params": params}
        payload = json.dumps(req, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        sock.sendall(payload)

        buf = b""
        while b"\n" not in buf:
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk
        line = buf.split(b"\n", 1)[0].strip()
        if not line:
            return {"ok": False, "error_code": "EMPTY_RESPONSE"}
        obj = json.loads(line.decode("utf-8", errors="replace"))
        if not isinstance(obj, dict):
            return {"ok": False, "error_code": "INVALID_RESPONSE"}
        return obj
    except socket.timeout:
        return {"ok": False, "error_code": "TIMEOUT"}
    except OSError as exc:
        return {"ok": False, "error_code": "SOCKET_ERROR", "message": str(exc)}
    finally:
        sock.close()
