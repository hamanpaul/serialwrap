"""以非 serialwrapd 行程獨占 Windows COM，透過 loopback TCP 等待 WSL 連線。"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import signal
import socket
import sys
import tempfile
import threading
import time
from typing import Any

_LOOPBACK = "127.0.0.1"
_READ_SIZE = 4096
_SERIAL_READ_TIMEOUT_S = 0.1
_CLIENT_SOCKET_TIMEOUT_S = 0.2


def _now_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds")


def open_windows_serial(port: str, *, serial_module: Any | None = None) -> Any:
    """以 pyserial 開啟 Windows COM；Windows CreateFile 以 share mode 0 獨占裝置。"""
    if serial_module is None:
        try:
            import serial as serial_module  # type: ignore[no-redef]
        except ImportError as exc:
            raise RuntimeError("缺少 pyserial；請使用 Windows Python 安裝 pyserial") from exc
    return serial_module.Serial(
        port=port,
        baudrate=115200,
        bytesize=serial_module.EIGHTBITS,
        parity=serial_module.PARITY_NONE,
        stopbits=serial_module.STOPBITS_ONE,
        timeout=_SERIAL_READ_TIMEOUT_S,
        write_timeout=2.0,
        xonxoff=False,
        rtscts=False,
        dsrdtr=False,
    )


class TcpSerialBridge:
    """單一 TCP client 與同一個序列埠 handle 間的全雙工 byte bridge。"""

    def __init__(
        self,
        serial_port: Any,
        *,
        serial_port_name: str,
        status_path: str | Path,
        host: str = _LOOPBACK,
        port: int = 48701,
        serial_opened_at_utc: str | None = None,
    ) -> None:
        if host != _LOOPBACK:
            raise ValueError("橋接器只允許監聽 127.0.0.1")
        if not 0 <= int(port) <= 65535:
            raise ValueError("TCP port 必須介於 0 到 65535")
        if not serial_port_name:
            raise ValueError("必須指定序列埠名稱")

        self.serial_port = serial_port
        self.serial_port_name = str(serial_port_name)
        self.status_path = Path(status_path)
        self.host = _LOOPBACK
        self.port = int(port)
        self._lock = threading.RLock()
        self._close_lock = threading.Lock()
        self._stop = threading.Event()
        self._listener: socket.socket | None = None
        self._active_client: socket.socket | None = None
        self._client_threads: set[threading.Thread] = set()
        self._closed = False
        self._last_status_write = 0.0
        self._status_dirty = False
        self._status: dict[str, Any] = {
            "pid": os.getpid(),
            "process_name": Path(__file__).name,
            "serial_port": self.serial_port_name,
            "serial_settings": "115200 8N1",
            "serial_opened_at_utc": serial_opened_at_utc or _now_utc(),
            "listener_host": self.host,
            "listener_port": self.port,
            "bytes_serial_to_tcp": 0,
            "bytes_tcp_to_serial": 0,
            "accepted_connections": 0,
            "rejected_connections": 0,
            "connected": False,
            "started_at_utc": _now_utc(),
            "stopped_at_utc": None,
            "last_error": None,
            "updated_at_utc": _now_utc(),
        }

    @property
    def address(self) -> tuple[str, int]:
        with self._lock:
            if self._listener is None:
                raise RuntimeError("橋接器尚未啟動")
            host, port = self._listener.getsockname()[:2]
            return str(host), int(port)

    def start(self) -> tuple[str, int]:
        """建立僅綁 loopback 的 listener，回傳實際位址（測試可用 port 0）。"""
        with self._lock:
            if self._listener is not None:
                return self.address
            if self._closed:
                raise RuntimeError("橋接器已關閉")
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                listener.bind((_LOOPBACK, self.port))
                listener.listen(4)
                listener.settimeout(0.2)
                self._listener = listener
                host, port = listener.getsockname()[:2]
                self._status["listener_host"] = str(host)
                self._status["listener_port"] = int(port)
                self._write_status_locked()
                self._last_status_write = time.monotonic()
                self._status_dirty = False
            except BaseException:
                self._listener = None
                listener.close()
                raise
        return self.address

    def serve_forever(self) -> None:
        if self._listener is None:
            self.start()
        while not self._stop.is_set():
            listener = self._listener
            if listener is None:
                break
            try:
                client, _address = listener.accept()
            except socket.timeout:
                self._flush_status_due()
                continue
            except OSError as exc:
                if self._stop.is_set():
                    break
                self._record_error(exc)
                continue

            client.settimeout(_CLIENT_SOCKET_TIMEOUT_S)
            with self._lock:
                if self._active_client is not None:
                    self._status["rejected_connections"] += 1
                    self._touch_status_locked(force=True)
                    client.close()
                    continue
                self._active_client = client
                self._status["accepted_connections"] += 1
                self._status["connected"] = True
                self._touch_status_locked(force=True)
                worker = threading.Thread(
                    target=self._serve_client,
                    args=(client,),
                    name="com-tcp-client",
                    daemon=True,
                )
                self._client_threads.add(worker)
                worker.start()

    def close(self) -> None:
        """停止 listener、關閉 TCP client 與 COM handle；只執行一次。"""
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
            self._stop.set()
            listener = self._listener
            self._listener = None
            active = self._active_client
            threads = list(self._client_threads)

        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass
        self._shutdown_socket(active)
        for worker in threads:
            if worker is not threading.current_thread():
                worker.join(timeout=2.5)
        try:
            self.serial_port.close()
        finally:
            with self._lock:
                self._active_client = None
                self._status["connected"] = False
                self._status["stopped_at_utc"] = _now_utc()
                self._touch_status_locked(force=True)

    def _serve_client(self, client: socket.socket) -> None:
        client_stop = threading.Event()
        serial_reader = threading.Thread(
            target=self._serial_to_tcp,
            args=(client, client_stop),
            name="com-to-tcp",
            daemon=True,
        )
        serial_reader.start()
        try:
            self._tcp_to_serial(client)
        except Exception as exc:  # noqa: BLE001 - bridge 邊界記錄錯誤並結束該連線
            if not self._stop.is_set():
                self._record_error(exc)
        finally:
            client_stop.set()
            self._shutdown_socket(client)
            serial_reader.join(timeout=2.5)
            try:
                client.close()
            except OSError:
                pass
            with self._lock:
                if self._active_client is client:
                    self._active_client = None
                    self._status["connected"] = False
                    self._touch_status_locked(force=True)
                self._client_threads.discard(threading.current_thread())

    def _serial_to_tcp(self, client: socket.socket, client_stop: threading.Event) -> None:
        try:
            while not self._stop.is_set() and not client_stop.is_set():
                try:
                    payload = self.serial_port.read(_READ_SIZE)
                except Exception as exc:  # noqa: BLE001
                    if not self._stop.is_set():
                        self._record_error(exc)
                    break
                if not payload:
                    continue
                try:
                    client.sendall(payload)
                except (OSError, TimeoutError):
                    break
                self._add_bytes("bytes_serial_to_tcp", len(payload))
        finally:
            self._shutdown_socket(client)

    def _tcp_to_serial(self, client: socket.socket) -> None:
        while not self._stop.is_set():
            try:
                payload = client.recv(_READ_SIZE)
            except socket.timeout:
                continue
            except (OSError, TimeoutError):
                return
            if not payload:
                return

            offset = 0
            while offset < len(payload) and not self._stop.is_set():
                try:
                    written = self.serial_port.write(payload[offset:])
                except Exception as exc:  # noqa: BLE001
                    if not self._stop.is_set():
                        self._record_error(exc)
                    return
                if written is None or int(written) <= 0:
                    self._record_error(IOError("序列埠 write 未寫入任何 bytes"))
                    return
                offset += int(written)
                self._add_bytes("bytes_tcp_to_serial", int(written))

    def _add_bytes(self, key: str, count: int) -> None:
        with self._lock:
            self._status[key] += int(count)
            self._touch_status_locked()

    def _record_error(self, exc: BaseException) -> None:
        with self._lock:
            self._status["last_error"] = f"{type(exc).__name__}: {exc}"
            self._touch_status_locked(force=True)

    def _touch_status_locked(self, *, force: bool = False) -> None:
        self._status["updated_at_utc"] = _now_utc()
        now = time.monotonic()
        if not force and now - self._last_status_write < 1.0:
            self._status_dirty = True
            return
        try:
            self._write_status_locked()
            self._last_status_write = now
            self._status_dirty = False
        except OSError as exc:
            self._status_dirty = True
            print(f"橋接狀態檔寫入失敗：{exc}", file=sys.stderr, flush=True)

    def _flush_status_due(self) -> None:
        with self._lock:
            if self._status_dirty and time.monotonic() - self._last_status_write >= 1.0:
                self._touch_status_locked(force=True)

    def _write_status_locked(self) -> None:
        self.status_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(
            prefix=f".{self.status_path.name}.", suffix=".tmp", dir=self.status_path.parent
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(self._status, stream, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.status_path)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise

    @staticmethod
    def _shutdown_socket(client: socket.socket | None) -> None:
        if client is None:
            return
        try:
            client.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            client.close()
        except OSError:
            pass


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="以 localhost TCP 橋接一個 Windows COM 埠（非 serialwrapd）")
    parser.add_argument("--port", default="COM6", help="獨占開啟的 Windows COM 埠；長跑 OPI 為 COM6")
    parser.add_argument("--tcp-port", type=int, default=48701, help="僅監聽 127.0.0.1 的 TCP 埠")
    parser.add_argument("--status-file", type=Path, required=True, help="PID、開啟時間與位元組計數的 JSON 檔")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if os.name != "nt":
        print("此程式只可在 Windows 執行；WSL 請執行 start_pty_tcp_bridge.sh。", file=sys.stderr)
        return 2
    if not 1 <= args.tcp_port <= 65535:
        print("--tcp-port 必須介於 1 到 65535", file=sys.stderr)
        return 2

    try:
        serial_port = open_windows_serial(args.port)
    except Exception as exc:  # noqa: BLE001 - COM 開啟錯誤需明確留在 stderr
        print(f"無法獨占開啟 {args.port}：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    bridge = TcpSerialBridge(
        serial_port,
        serial_port_name=args.port,
        status_path=args.status_file,
        port=args.tcp_port,
    )

    def stop(_signum: int, _frame: Any) -> None:
        bridge._stop.set()

    signal.signal(signal.SIGINT, stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, stop)
    try:
        host, port = bridge.start()
        print(f"COM bridge PID={os.getpid()} COM={args.port} listener={host}:{port}", flush=True)
        bridge.serve_forever()
    except KeyboardInterrupt:
        pass
    except OSError as exc:
        print(f"橋接器啟動失敗：{exc}", file=sys.stderr)
        return 2
    finally:
        bridge.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
