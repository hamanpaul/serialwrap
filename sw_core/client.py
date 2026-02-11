from __future__ import annotations

import json
import socket
from typing import Any


def rpc_call(socket_path: str, method: str, params: dict[str, Any], *, req_id: int = 1, timeout_s: float = 5.0) -> dict[str, Any]:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout_s)
    try:
        sock.connect(socket_path)
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
