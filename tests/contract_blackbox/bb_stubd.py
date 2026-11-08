"""黑箱用 stub daemon。

以 socketserver 接收連線、逐行 NDJSON 請求、依 script 劇本回覆，
並將每筆請求原樣寫入 requests.log。
stdlib-only，不得 import sw_core／serialwrap／serialwrapd。
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import socketserver
import threading
import time
from pathlib import Path
from typing import Any, Mapping


class _StubHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        daemon_inst: StubDaemon = self.server.daemon_inst  # type: ignore[attr-defined]
        buf = b""
        while not daemon_inst._stopped:
            try:
                chunk = self.request.recv(4096)
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                req_line = line + b"\n"
                daemon_inst._record_request(req_line)
                should_close = daemon_inst._dispatch(self.request, req_line)
                if should_close:
                    return


if hasattr(socketserver, "UnixStreamServer"):
    class _ThreadingUnixStreamServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
        daemon_threads = True
else:
    _ThreadingUnixStreamServer = None  # type: ignore[assignment,misc]


class _ThreadingTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True


class StubDaemon:
    def __init__(self, workdir: Path, script: Mapping[str, Any], *, transport: str = "unix") -> None:
        self.workdir = Path(workdir).resolve()
        self.script = dict(script)
        self.transport = transport
        if self.transport == "unix" and os.name == "nt":
            raise ValueError("Windows 平台不支援 unix transport")

        self.socket_path: Path | None = (self.workdir / "stubd.sock") if self.transport == "unix" else None
        self._server: socketserver.BaseServer | None = None
        self._thread: threading.Thread | None = None
        self._endpoint: str = ""
        self._requests: list[bytes] = []
        self._lock = threading.Lock()
        self._method_step_indices: dict[str, int] = {}
        self._default_step_index = 0
        self._started = False
        self._stopped = False

    def start(self, timeout_s: float = 10.0) -> None:
        if self._started:
            return
        self.workdir.mkdir(parents=True, exist_ok=True)
        if self.transport == "unix":
            if _ThreadingUnixStreamServer is None:
                raise ValueError("本平台不支援 unix transport")
            assert self.socket_path is not None
            if self.socket_path.exists():
                self.socket_path.unlink()
            server = _ThreadingUnixStreamServer(str(self.socket_path), _StubHandler)
            self._endpoint = str(self.socket_path)
        else:
            server = _ThreadingTCPServer(("127.0.0.1", 0), _StubHandler)
            port = server.server_address[1]
            self._endpoint = f"tcp://127.0.0.1:{port}"

        server.daemon_inst = self  # type: ignore[attr-defined]
        self._server = server
        self._thread = threading.Thread(target=server.serve_forever, daemon=True)
        self._thread.start()

        # 寫入 ready 檔
        (self.workdir / "ready").write_text(self._endpoint, encoding="utf-8")
        self._started = True

    @property
    def endpoint(self) -> str:
        if not self._endpoint:
            if self.transport == "unix" and self.socket_path is not None:
                return str(self.socket_path)
        return self._endpoint

    def requests(self) -> list[bytes]:
        with self._lock:
            return list(self._requests)

    def _record_request(self, req_line: bytes) -> None:
        with self._lock:
            self._requests.append(req_line)
            try:
                with open(self.workdir / "requests.log", "a", encoding="ascii") as f:
                    f.write(base64.b64encode(req_line).decode("ascii") + "\n")
            except OSError:
                pass

    def _dispatch(self, sock: Any, req_line: bytes) -> bool:
        try:
            req = json.loads(req_line.decode("utf-8"))
            method = req.get("method", "")
        except Exception:
            method = ""

        with self._lock:
            methods = self.script.get("methods", {})
            if method in methods and methods[method]:
                steps = methods[method]
                idx = self._method_step_indices.get(method, 0)
                step = steps[idx]
                if idx + 1 < len(steps):
                    self._method_step_indices[method] = idx + 1
            elif "default" in self.script and self.script["default"]:
                steps = self.script["default"]
                idx = self._default_step_index
                step = steps[idx]
                if idx + 1 < len(steps):
                    self._default_step_index = idx + 1
            else:
                step = {"reply": {"ok": False, "error_code": "METHOD_NOT_FOUND"}}

        if "delay_s" in step:
            time.sleep(float(step["delay_s"]))

        if step.get("close"):
            return True

        if "reply" in step:
            payload = json.dumps(step["reply"], ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
            try:
                sock.sendall(payload)
            except OSError:
                return True
        elif "reply_raw" in step:
            text = step["reply_raw"]
            payload = text.encode("utf-8")
            try:
                sock.sendall(payload)
            except OSError:
                return True
        elif "partial_then_close" in step:
            text = step["partial_then_close"]
            try:
                sock.sendall(text.encode("utf-8"))
            except OSError:
                pass
            return True
        elif "split_reply" in step:
            chunks = step["split_reply"]
            gap = float(step.get("gap_s", 0.05))
            for i, chunk in enumerate(chunks):
                if i > 0 and gap > 0:
                    time.sleep(gap)
                try:
                    sock.sendall(chunk.encode("utf-8") if isinstance(chunk, str) else chunk)
                except OSError:
                    return True
        return False

    def stop(self) -> None:
        self._stopped = True
        if self._server:
            try:
                self._server.shutdown()
                self._server.server_close()
            except Exception:
                pass
            self._server = None
        if self._thread:
            self._thread.join(timeout=3.0)
            self._thread = None
        if self.socket_path:
            try:
                self.socket_path.unlink(missing_ok=True)
            except OSError:
                pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Stub Daemon for Serialwrap Blackbox Testing")
    parser.add_argument("--script", required=True, help="Path to JSON script file")
    parser.add_argument("--workdir", required=True, help="Work directory path")
    parser.add_argument("--tcp", action="store_true", help="Use TCP loopback instead of Unix domain socket")
    args = parser.parse_args()

    script_data = json.loads(Path(args.script).read_text(encoding="utf-8"))
    transport = "tcp" if args.tcp else "unix"
    d = StubDaemon(Path(args.workdir), script_data, transport=transport)
    d.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        d.stop()


if __name__ == "__main__":
    main()
