"""以 loopback TCP 傳送獨立於 UART byte stream 的 bridge 控制訊息。"""

from __future__ import annotations

import hmac
import json
import os
import pathlib
import socket
import tempfile
import threading
import uuid
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

from uart_stdio_bridge import CONTROL_SCHEMA


class ControlError(RuntimeError):
    """控制通道錯誤，`code` 為測試工具 HARNESS 原因碼。"""

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(message or code)


_MAX_LINE_BYTES = 1_048_576


def _encode_line(obj: dict[str, Any]) -> bytes:
    return (
        json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def write_control_info(
    path: pathlib.Path, *, host: str, port: int, token: str
) -> None:
    """以 0600 權限原子寫入控制端點與權杖。"""
    target = pathlib.Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = _encode_line({"endpoint": f"tcp://{host}:{int(port)}", "token": token})
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        if os.name != "nt":
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp_name, target)
        if os.name != "nt":
            os.chmod(target, 0o600)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


class ControlServer:
    """多連線 loopback server；每個連線至多處理一筆 JSON line。"""

    def __init__(
        self,
        handler: Callable[[str, dict[str, Any], str], dict[str, Any]],
        *,
        token: str,
    ) -> None:
        self.handler = handler
        self.token = token
        self._listener: socket.socket | None = None
        self._accept_thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._clients: set[socket.socket] = set()
        self._client_threads: set[threading.Thread] = set()
        self._lock = threading.Lock()

    def start(self) -> tuple[str, int]:
        if self._listener is not None:
            raise RuntimeError("控制通道已啟動")
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(16)
        listener.settimeout(0.2)
        self._listener = listener
        address = listener.getsockname()
        self._accept_thread = threading.Thread(
            target=self._accept_loop, name="bridge-control", daemon=True
        )
        self._accept_thread.start()
        return str(address[0]), int(address[1])

    def _accept_loop(self) -> None:
        listener = self._listener
        if listener is None:
            return
        while not self._stop.is_set():
            try:
                conn, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                if self._stop.is_set():
                    return
                continue
            conn.settimeout(30.0)
            thread = threading.Thread(
                target=self._serve_client,
                args=(conn,),
                name="bridge-control-client",
                daemon=True,
            )
            with self._lock:
                self._clients.add(conn)
                self._client_threads.add(thread)
            thread.start()

    def _serve_client(self, conn: socket.socket) -> None:
        try:
            data = bytearray()
            while len(data) <= _MAX_LINE_BYTES and b"\n" not in data:
                chunk = conn.recv(min(65536, _MAX_LINE_BYTES + 1 - len(data)))
                if not chunk:
                    return
                data.extend(chunk)
            line, sep, _rest = bytes(data).partition(b"\n")
            if not sep or len(line) > _MAX_LINE_BYTES:
                self._send_protocol_error(conn, None)
                return
            try:
                request = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._send_protocol_error(conn, None)
                return
            if not isinstance(request, dict):
                self._send_protocol_error(conn, None)
                return
            action_id = request.get("action_id")
            action_id = action_id if isinstance(action_id, str) else None
            received_token = request.get("token")
            if not isinstance(received_token, str) or not hmac.compare_digest(
                self.token.encode("utf-8"), received_token.encode("utf-8")
            ):
                self._send_protocol_error(conn, action_id)
                return
            action = request.get("action")
            params = request.get("params", {})
            if not isinstance(action, str) or not action or not isinstance(params, dict):
                self._send_protocol_error(conn, action_id)
                return
            if not action_id:
                action_id = uuid.uuid4().hex
            try:
                result = self.handler(action, params, action_id)
                if not isinstance(result, dict):
                    response = {
                        "action_id": action_id,
                        "ok": False,
                        "error": "HARNESS_CONTROL_PROTOCOL_ERROR",
                    }
                elif result.get("ok") is False:
                    response = {
                        "action_id": action_id,
                        "ok": False,
                        "error": str(result.get("error", "HARNESS_CONTROL_PROTOCOL_ERROR")),
                    }
                else:
                    response = {
                        "action_id": action_id,
                        "ok": True,
                        "result": result.get("result", result),
                    }
            except ControlError as exc:
                response = {"action_id": action_id, "ok": False, "error": exc.code}
            except Exception:
                response = {
                    "action_id": action_id,
                    "ok": False,
                    "error": "HARNESS_CONTROL_PROTOCOL_ERROR",
                }
            conn.sendall(_encode_line(response))
        except (OSError, TimeoutError):
            return
        finally:
            try:
                conn.close()
            except OSError:
                pass
            with self._lock:
                self._clients.discard(conn)
                self._client_threads.discard(threading.current_thread())

    @staticmethod
    def _send_protocol_error(conn: socket.socket, action_id: str | None) -> None:
        try:
            conn.sendall(
                _encode_line(
                    {
                        "action_id": action_id,
                        "ok": False,
                        "error": "HARNESS_CONTROL_PROTOCOL_ERROR",
                    }
                )
            )
        except OSError:
            pass

    def stop(self) -> None:
        self._stop.set()
        listener = self._listener
        self._listener = None
        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass
        with self._lock:
            clients = list(self._clients)
        for conn in clients:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                conn.close()
            except OSError:
                pass
        if self._accept_thread is not None:
            self._accept_thread.join(timeout=1.0)
        with self._lock:
            threads = list(self._client_threads)
        for thread in threads:
            thread.join(timeout=1.0)


class ControlClient:
    """讀取 control.json，逐次以 TCP loopback 呼叫 bridge 動作。"""

    def __init__(self, info_path: pathlib.Path, *, timeout_s: float = 30.0) -> None:
        self.info_path = pathlib.Path(info_path)
        self.timeout_s = float(timeout_s)
        self._closed = False

    def call(
        self, action: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if self._closed:
            raise ControlError("HARNESS_CONTROL_PROTOCOL_ERROR", "控制 client 已關閉")
        try:
            info = json.loads(self.info_path.read_text(encoding="utf-8"))
            endpoint = urlsplit(info["endpoint"])
            host = endpoint.hostname
            port = endpoint.port
            token = info["token"]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ControlError("HARNESS_CONTROL_PROTOCOL_ERROR", str(exc)) from exc
        if endpoint.scheme != "tcp" or host != "127.0.0.1" or port is None:
            raise ControlError("HARNESS_CONTROL_PROTOCOL_ERROR", "控制端點必須是 loopback TCP")
        action_id = uuid.uuid4().hex
        request = {
            "action": action,
            "action_id": action_id,
            "params": {} if params is None else params,
            "token": token,
        }
        try:
            with socket.create_connection((host, port), timeout=self.timeout_s) as conn:
                conn.settimeout(self.timeout_s)
                conn.sendall(_encode_line(request))
                data = bytearray()
                while len(data) <= _MAX_LINE_BYTES and b"\n" not in data:
                    chunk = conn.recv(min(65536, _MAX_LINE_BYTES + 1 - len(data)))
                    if not chunk:
                        break
                    data.extend(chunk)
        except (OSError, TimeoutError) as exc:
            raise ControlError("HARNESS_CONTROL_PROTOCOL_ERROR", str(exc)) from exc
        line, sep, _rest = bytes(data).partition(b"\n")
        if not sep:
            raise ControlError("HARNESS_CONTROL_PROTOCOL_ERROR", "控制回應未完整")
        try:
            response = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ControlError("HARNESS_CONTROL_PROTOCOL_ERROR", str(exc)) from exc
        if not isinstance(response, dict) or response.get("action_id") != action_id:
            raise ControlError("HARNESS_CONTROL_PROTOCOL_ERROR", "控制回應 action_id 不符")
        if response.get("ok") is not True:
            raise ControlError(
                str(response.get("error", "HARNESS_CONTROL_PROTOCOL_ERROR"))
            )
        result = response.get("result")
        if not isinstance(result, dict):
            raise ControlError("HARNESS_CONTROL_PROTOCOL_ERROR", "控制回應 result 必須是物件")
        return result

    def close(self) -> None:
        self._closed = True


__all__ = [
    "CONTROL_SCHEMA",
    "ControlError",
    "ControlServer",
    "ControlClient",
    "write_control_info",
]
