"""在 WSL 建立穩定 PTY slave，雙向連接 Windows localhost COM bridge。"""

from __future__ import annotations

import argparse
import datetime as dt
import errno
import json
import os
from pathlib import Path
import pty
import select
import signal
import socket
import sys
import tempfile
import threading
import time
import tty
from typing import Any

_LOOPBACK = "127.0.0.1"
_READ_SIZE = 4096
_SOCKET_TIMEOUT_S = 0.2


def _now_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds")


class PtyTcpBridge:
    """用單一 PTY master 對接可重連的 TCP client，PTY symlink 在本行程生命週期內不變。"""

    def __init__(
        self,
        *,
        pty_link: str | Path,
        host: str = _LOOPBACK,
        port: int = 48701,
        reconnect_interval_s: float = 1.0,
        status_path: str | Path | None = None,
    ) -> None:
        if os.name != "posix":
            raise RuntimeError("PTY 橋接器只支援 POSIX／WSL")
        if host != _LOOPBACK:
            raise ValueError("WSL 橋接器只允許連接 127.0.0.1")
        if not 1 <= int(port) <= 65535:
            raise ValueError("TCP port 必須介於 1 到 65535")
        if reconnect_interval_s <= 0:
            raise ValueError("reconnect_interval_s 必須大於 0")

        self.pty_link = Path(pty_link)
        if "," in str(self.pty_link):
            raise ValueError("PTY symlink 路徑不得含逗號（socat 位址語法保留字）")
        self.host = _LOOPBACK
        self.port = int(port)
        self.reconnect_interval_s = float(reconnect_interval_s)
        self.status_path = Path(status_path) if status_path is not None else None
        self._lock = threading.RLock()
        self._close_lock = threading.Lock()
        self._stop = threading.Event()
        self._slave_available = threading.Event()
        self._ready = threading.Event()
        self._ever_connected = threading.Event()
        self._active_socket: socket.socket | None = None
        self._master_fd: int | None = None
        self._slave_fd: int | None = None
        self._slave_path: str | None = None
        self._reader_thread: threading.Thread | None = None
        self._tcp_thread: threading.Thread | None = None
        self._closed = False
        self._link_created = False
        self._last_status_write = 0.0
        self._status_dirty = False
        self._status: dict[str, Any] = {
            "pid": os.getpid(),
            "process_name": Path(__file__).name,
            "pty_link": str(self.pty_link),
            "pty_slave": None,
            "listener_host": self.host,
            "listener_port": self.port,
            "connected": False,
            "accepted_connections": 0,
            "bytes_pty_to_tcp": 0,
            "bytes_tcp_to_pty": 0,
            "started_at_utc": _now_utc(),
            "stopped_at_utc": None,
            "last_error": None,
            "updated_at_utc": _now_utc(),
        }

    @property
    def ready(self) -> bool:
        return self._ready.is_set()

    @property
    def connected(self) -> bool:
        with self._lock:
            return self._active_socket is not None

    @property
    def slave_path(self) -> str | None:
        return self._slave_path

    @property
    def bytes_pty_to_tcp(self) -> int:
        with self._lock:
            return int(self._status["bytes_pty_to_tcp"])

    @property
    def bytes_tcp_to_pty(self) -> int:
        with self._lock:
            return int(self._status["bytes_tcp_to_pty"])

    def serve_forever(self) -> None:
        if self._ready.is_set():
            raise RuntimeError("PTY 橋接器不可重複啟動")
        try:
            self._open_pty()
            self._reader_thread = threading.Thread(
                target=self._pty_to_tcp, name="pty-to-tcp", daemon=True
            )
            self._reader_thread.start()
            while not self._stop.is_set():
                # 模仿 socat 的 wait-slave：serialwrapd 尚未開 PTY 時不先接 TCP。
                if not self._slave_available.wait(timeout=0.1):
                    self._flush_status_due()
                    continue
                try:
                    client = socket.create_connection(
                        (self.host, self.port), timeout=min(1.0, self.reconnect_interval_s)
                    )
                except OSError:
                    self._stop.wait(self.reconnect_interval_s)
                    continue
                client.settimeout(_SOCKET_TIMEOUT_S)
                with self._lock:
                    if self._stop.is_set():
                        client.close()
                        break
                    self._active_socket = client
                    self._ever_connected.set()
                    self._status["connected"] = True
                    self._status["accepted_connections"] += 1
                    self._touch_status_locked(force=True)
                self._tcp_thread = threading.Thread(
                    target=self._tcp_to_pty,
                    args=(client,),
                    name="tcp-to-pty",
                    daemon=True,
                )
                self._tcp_thread.start()
                while not self._stop.is_set() and self._tcp_thread.is_alive():
                    self._tcp_thread.join(timeout=0.1)
                    self._flush_status_due()
                self._drop_connection(client)
                if self._tcp_thread is not threading.current_thread():
                    self._tcp_thread.join(timeout=1.0)
                self._tcp_thread = None
        finally:
            self.close()

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
            self._stop.set()
            active = self._active_socket
            reader = self._reader_thread
            tcp = self._tcp_thread

        self._drop_connection(active)
        for worker in (tcp, reader):
            if worker is not None and worker is not threading.current_thread():
                worker.join(timeout=1.5)
        for fd_name in ("_slave_fd", "_master_fd"):
            fd = getattr(self, fd_name)
            setattr(self, fd_name, None)
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        if self._link_created:
            try:
                if self.pty_link.is_symlink() and os.readlink(self.pty_link) == self._slave_path:
                    self.pty_link.unlink()
            except OSError:
                pass
            self._link_created = False
        with self._lock:
            self._status["connected"] = False
            self._status["stopped_at_utc"] = _now_utc()
            self._touch_status_locked(force=True)

    def _open_pty(self) -> None:
        self.pty_link.parent.mkdir(parents=True, exist_ok=True)
        if os.path.lexists(self.pty_link):
            raise FileExistsError(f"PTY symlink 已存在，拒絕覆寫：{self.pty_link}")
        master_fd, slave_fd = pty.openpty()
        try:
            tty.setraw(slave_fd)
            slave_path = os.ttyname(slave_fd)
            os.set_blocking(master_fd, False)
            os.symlink(slave_path, self.pty_link)
            os.close(slave_fd)
            slave_fd = -1
        except BaseException:
            os.close(master_fd)
            if slave_fd >= 0:
                os.close(slave_fd)
            raise
        self._master_fd = master_fd
        self._slave_fd = None
        self._slave_path = slave_path
        self._link_created = True
        with self._lock:
            self._status["pty_slave"] = slave_path
            self._touch_status_locked(force=True)
        self._ready.set()

    def _pty_to_tcp(self) -> None:
        master_fd = self._master_fd
        if master_fd is None:
            return
        poller = select.poll()
        poller.register(master_fd, select.POLLIN | select.POLLHUP | select.POLLERR)
        while not self._stop.is_set():
            try:
                events = poller.poll(100)
            except OSError as exc:
                if not self._stop.is_set():
                    self._record_error(exc)
                return
            if not events:
                if not self._slave_available.is_set():
                    try:
                        payload = os.read(master_fd, _READ_SIZE)
                    except BlockingIOError:
                        # no-slave 時 poll 會報 POLLHUP；HUP 消失後 EAGAIN 證明 slave 已開啟。
                        self._slave_available.set()
                    except OSError as exc:
                        if exc.errno != errno.EIO and not self._stop.is_set():
                            self._record_error(exc)
                            return
                    else:
                        self._slave_available.set()
                        if payload:
                            self._forward_pty_payload(payload)
                self._flush_status_due()
                continue

            flags = events[0][1]
            if flags & select.POLLIN:
                try:
                    payload = os.read(master_fd, _READ_SIZE)
                except BlockingIOError:
                    self._slave_available.set()
                    continue
                except OSError as exc:
                    if exc.errno == errno.EIO:
                        self._slave_available.clear()
                        self._stop.wait(0.05)
                        continue
                    if not self._stop.is_set():
                        self._record_error(exc)
                    return
                self._slave_available.set()
                if payload:
                    self._forward_pty_payload(payload)
            elif flags & (select.POLLHUP | select.POLLERR):
                # HUP 代表目前沒有外部 slave opener；open() 後此事件會消失。
                self._slave_available.clear()
                self._stop.wait(0.05)

    def _forward_pty_payload(self, payload: bytes) -> None:
        deadline = time.monotonic() + 2.0
        while not self._stop.is_set():
            with self._lock:
                client = self._active_socket
            if client is not None:
                break
            if self._ever_connected.is_set() or time.monotonic() >= deadline:
                # TCP 中斷期間不把舊資料留到重連後重播；該次長跑應由 observer 判為失敗。
                return
            self._stop.wait(0.01)
        if client is None:
            return
        try:
            client.sendall(payload)
        except (OSError, TimeoutError):
            self._drop_connection(client)
            return
        self._add_bytes("bytes_pty_to_tcp", len(payload))

    def _tcp_to_pty(self, client: socket.socket) -> None:
        try:
            while not self._stop.is_set():
                try:
                    payload = client.recv(_READ_SIZE)
                except socket.timeout:
                    continue
                except (OSError, TimeoutError):
                    break
                if not payload:
                    break
                if not self._write_pty(payload):
                    break
                self._add_bytes("bytes_tcp_to_pty", len(payload))
        finally:
            self._drop_connection(client)

    def _write_pty(self, payload: bytes) -> bool:
        master_fd = self._master_fd
        if master_fd is None:
            return False
        offset = 0
        while offset < len(payload) and not self._stop.is_set():
            try:
                _readable, writable, _ = select.select([], [master_fd], [], 0.1)
            except (OSError, ValueError):
                return False
            if not writable:
                continue
            try:
                written = os.write(master_fd, payload[offset:])
            except (BlockingIOError, OSError):
                return False
            if written <= 0:
                return False
            offset += written
        return offset == len(payload)

    def _drop_connection(self, client: socket.socket | None) -> None:
        if client is None:
            return
        with self._lock:
            if self._active_socket is client:
                self._active_socket = None
                self._status["connected"] = False
                self._touch_status_locked(force=True)
        try:
            client.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            client.close()
        except OSError:
            pass

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
        if self.status_path is None:
            return
        now = time.monotonic()
        if not force and now - self._last_status_write < 1.0:
            self._status_dirty = True
            return
        try:
            _write_json_atomic(self.status_path, self._status)
            self._last_status_write = now
            self._status_dirty = False
        except OSError as exc:
            self._status_dirty = True
            print(f"PTY 橋接狀態檔寫入失敗：{exc}", file=sys.stderr, flush=True)

    def _flush_status_due(self) -> None:
        with self._lock:
            if self._status_dirty and time.monotonic() - self._last_status_write >= 1.0:
                self._touch_status_locked(force=True)


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="將 Windows loopback COM bridge 接到 WSL PTY")
    parser.add_argument("--pty-link", required=True, type=Path, help="隔離 by-id sandbox 內的 PTY symlink 路徑")
    parser.add_argument("--host", default=_LOOPBACK)
    parser.add_argument("--port", type=int, default=48701)
    parser.add_argument("--reconnect-interval-s", type=float, default=1.0)
    parser.add_argument("--status-file", type=Path, default=None)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        bridge = PtyTcpBridge(
            pty_link=args.pty_link,
            host=args.host,
            port=args.port,
            reconnect_interval_s=args.reconnect_interval_s,
            status_path=args.status_file,
        )
    except (RuntimeError, ValueError) as exc:
        print(f"PTY 橋接參數錯誤：{exc}", file=sys.stderr)
        return 2

    stop = threading.Event()

    def request_stop(_signum: int, _frame: Any) -> None:
        stop.set()
        bridge._stop.set()
        bridge._drop_connection(bridge._active_socket)

    signal.signal(signal.SIGINT, request_stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, request_stop)
    try:
        bridge.serve_forever()
    except (OSError, FileExistsError) as exc:
        print(f"PTY 橋接啟動失敗：{exc}", file=sys.stderr)
        return 2
    finally:
        bridge.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
