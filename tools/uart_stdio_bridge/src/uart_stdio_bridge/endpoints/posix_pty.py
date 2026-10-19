"""以 PTY master 作為 bridge 對端 B 的 POSIX 端點。"""

from __future__ import annotations

import errno
import math
import os
import pathlib
import pty
import select
import stat
import threading
import time
import tty
from typing import Any

from .base import EndpointError


class PosixPtyEndpoint:
    """建立 raw PTY，只保留 master fd，讓受測端自行開啟 slave。

    PTY master 在 peer 關閉後仍可能保留尚未讀取的 bytes；讀取時會先排空
    `POLLIN`，再把後續 opener 視為新的觀測上下文。`disconnect(remove_link=False)`
    會留下指向已釋放 pts 路徑的 symlink，因此只可在隔離的測試目錄使用。
    `peer_open()` 是即時狀態取樣；外部監看器若以固定週期輪詢，短於該週期的
    關閉與重開可能不會被觀察到。
    """

    kind = "posix-pty"
    transport_mode = "virtual_pty"

    def __init__(self, by_id_link: pathlib.Path, *, peer_gate: bool = True) -> None:
        self.by_id_link = pathlib.Path(by_id_link)
        self.peer_gate = bool(peer_gate)
        self.slave_path: str | None = None
        self.generation = 0
        self._master_fd: int | None = None
        self._lock = threading.RLock()
        self._closed = False

    def open(self) -> None:
        """建立新 PTY 世代並以 by-id symlink 公開 slave。"""
        with self._lock:
            if self._closed:
                raise EndpointError("HARNESS_ENDPOINT_IO_ERROR: PTY 已關閉")
            if self._master_fd is not None:
                return

            master_fd: int | None = None
            slave_fd: int | None = None
            try:
                master_fd, slave_fd = pty.openpty()
                tty.setraw(slave_fd)
                slave_path = os.ttyname(slave_fd)
                closing_slave_fd = slave_fd
                slave_fd = None
                os.close(closing_slave_fd)
                os.set_blocking(master_fd, False)
                self.by_id_link.parent.mkdir(parents=True, exist_ok=True)
                self._unlink_link_path()
                os.symlink(slave_path, self.by_id_link)
            except Exception as exc:
                if slave_fd is not None:
                    try:
                        os.close(slave_fd)
                    except OSError:
                        pass
                if master_fd is not None:
                    try:
                        os.close(master_fd)
                    except OSError:
                        pass
                raise EndpointError(f"HARNESS_ENDPOINT_OPEN_FAILED: 無法開啟 POSIX PTY：{exc}") from exc

            self._master_fd = master_fd
            self.slave_path = slave_path
            self.generation += 1

    def read(self, max_bytes: int, timeout_s: float) -> bytes:
        """等待 master 可讀；斷線、無 peer 或 timeout 時回傳空 bytes。"""
        with self._lock:
            self._ensure_io_allowed_locked()
            start_generation = self.generation
        if max_bytes <= 0:
            return b""

        deadline = time.monotonic() + max(0.0, timeout_s)
        while True:
            wait_without_poll = False
            poll_waited = False
            with self._lock:
                self._ensure_io_allowed_locked()
                if self.generation != start_generation:
                    return b""
                master_fd = self._master_fd
                if master_fd is None:
                    wait_without_poll = True
                else:
                    remaining = max(0.0, deadline - time.monotonic())
                    poller = select.poll()
                    poller.register(master_fd, select.POLLIN | select.POLLHUP | select.POLLERR)
                    try:
                        events = poller.poll(self._timeout_ms(min(remaining, 0.01)))
                        poll_waited = not events
                    except OSError as exc:
                        if exc.errno == errno.EINTR:
                            events = []
                            poll_waited = True
                        else:
                            raise self._io_error("讀取 PTY", exc) from exc

                    if events:
                        event = events[0][1]
                        # 有資料時先讀，即使同一個 poll event 也帶 POLLHUP。
                        if event & select.POLLIN:
                            try:
                                return os.read(master_fd, max_bytes)
                            except BlockingIOError:
                                wait_without_poll = True
                            except OSError as exc:
                                if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK, errno.EINTR):
                                    wait_without_poll = True
                                elif exc.errno == errno.EIO:
                                    wait_without_poll = True
                                else:
                                    raise self._io_error("讀取 PTY", exc) from exc
                        elif event & (select.POLLHUP | select.POLLERR):
                            # HUP 是 level-triggered；節流後等滿呼叫者的 timeout。
                            wait_without_poll = True
                        elif time.monotonic() >= deadline:
                            return b""

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return b""
            if wait_without_poll:
                time.sleep(min(0.01, remaining))
            elif not poll_waited:
                time.sleep(min(0.001, remaining))
            else:
                # poll 是在 self._lock 內等待；逾時後若立即重取鎖，threading.Lock 不保證公平，
                # 同一端點的 write_some／peer_open 會被餓到 read() 的整個 timeout（50 ms）才拿到鎖，
                # RX 寫回受測端被量化成 50 ms 一筆。於鎖外短暫讓出，讓等待中的執行緒取得鎖。
                time.sleep(min(0.0005, remaining))

    def write_some(self, data: bytes, timeout_s: float) -> int:
        """寫入一段 bytes，回報本次由核心接受的長度。"""
        with self._lock:
            self._ensure_io_allowed_locked()
            start_generation = self.generation
        if not data:
            return 0

        deadline = time.monotonic() + max(0.0, timeout_s)
        while True:
            wait_without_poll = False
            with self._lock:
                self._ensure_io_allowed_locked()
                if self.generation != start_generation:
                    return 0
                master_fd = self._master_fd
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return 0
                if master_fd is None:
                    wait_without_poll = True
                elif self.peer_gate and not self._peer_open_for_fd(master_fd):
                    wait_without_poll = True
                else:
                    try:
                        # 每次 os.write 前都重驗 peer gate，包含 EAGAIN 與 EIO 後的重試。
                        return os.write(master_fd, data)
                    except InterruptedError:
                        wait_without_poll = True
                    except BlockingIOError:
                        wait_without_poll = True
                    except OSError as exc:
                        if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK, errno.EINTR):
                            wait_without_poll = True
                        elif exc.errno == errno.EIO and self.peer_gate:
                            wait_without_poll = True
                        else:
                            raise self._io_error("寫入 PTY", exc) from exc

                    if wait_without_poll and self._master_fd == master_fd:
                        remaining = max(0.0, deadline - time.monotonic())
                        if remaining > 0:
                            poller = select.poll()
                            poller.register(
                                master_fd,
                                select.POLLOUT | select.POLLHUP | select.POLLERR,
                            )
                            try:
                                poller.poll(self._timeout_ms(min(remaining, 0.01)))
                            except OSError as exc:
                                if exc.errno != errno.EINTR:
                                    raise self._io_error("等待 PTY 可寫", exc) from exc

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return 0
            if wait_without_poll:
                time.sleep(min(0.01, remaining))

    def peer_open(self) -> bool:
        """以 master 的 POLLHUP 狀態取樣 slave 端目前是否有 opener。"""
        with self._lock:
            if self._closed or self._master_fd is None:
                return False
            return self._peer_open_for_fd(self._master_fd)

    def capabilities(self) -> dict[str, bool]:
        return {"transport_disconnect": True, "by_id_link": True, "peer_state": True}

    def disconnect(self, *, remove_link: bool) -> None:
        with self._lock:
            self._ensure_not_closed_locked()
            if remove_link:
                self._unlink_link_path()
            self._close_master_locked()

    def reconnect(self) -> int:
        with self._lock:
            self._ensure_not_closed_locked()
            old_master_fd = self._master_fd
            old_slave_path = self.slave_path
            try:
                master_fd, slave_path = self._create_pty()
            except Exception as exc:
                raise EndpointError(f"HARNESS_ENDPOINT_OPEN_FAILED: 無法重建 POSIX PTY：{exc}") from exc
            reserved_master_fd: int | None = None
            if slave_path == old_slave_path:
                # 斷線後核心可能立刻重用舊 pts 編號；暫留該 PTY，再配置下一個編號。
                reserved_master_fd = master_fd
                try:
                    master_fd, slave_path = self._create_pty()
                except Exception as exc:
                    try:
                        os.close(reserved_master_fd)
                    except OSError:
                        pass
                    raise EndpointError(f"HARNESS_ENDPOINT_OPEN_FAILED: 無法重建 POSIX PTY：{exc}") from exc
                os.close(reserved_master_fd)

            try:
                self.by_id_link.parent.mkdir(parents=True, exist_ok=True)
                self._unlink_link_path()
                os.symlink(slave_path, self.by_id_link)
            except Exception as exc:
                try:
                    os.close(master_fd)
                except OSError:
                    pass
                if old_master_fd is not None and old_slave_path is not None and not os.path.lexists(self.by_id_link):
                    try:
                        os.symlink(old_slave_path, self.by_id_link)
                    except OSError:
                        pass
                if isinstance(exc, EndpointError):
                    raise
                raise self._io_error("重建 by-id symlink", exc) from exc

            self._master_fd = master_fd
            self.slave_path = slave_path
            self.generation += 1
            if old_master_fd is not None:
                try:
                    os.close(old_master_fd)
                except OSError:
                    pass
            return self.generation

    def link_remove(self) -> None:
        """移除穩定名稱，但不關閉 PTY 或已開啟的 peer。"""
        with self._lock:
            self._unlink_link_path()

    def link_add(self) -> None:
        """將穩定名稱重新指向目前世代的 slave。"""
        with self._lock:
            self._ensure_not_closed_locked()
            if self._master_fd is None or self.slave_path is None:
                raise EndpointError("HARNESS_ENDPOINT_IO_ERROR: PTY 尚未開啟")
            self.by_id_link.parent.mkdir(parents=True, exist_ok=True)
            self._unlink_link_path()
            try:
                os.symlink(self.slave_path, self.by_id_link)
            except OSError as exc:
                raise self._io_error("建立 by-id symlink", exc) from exc

    def close(self) -> None:
        """關閉 master 並移除 adapter 建立的 symlink；可重複呼叫。"""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                self._unlink_link_path()
            finally:
                self._close_master_locked()

    def describe(self) -> dict[str, Any]:
        with self._lock:
            return {
                "kind": self.kind,
                "transport_mode": self.transport_mode,
                "by_id_link": str(self.by_id_link),
                "slave_path": self.slave_path,
                "generation": self.generation,
                "peer_gate": self.peer_gate,
            }

    def _ensure_not_closed_locked(self) -> None:
        if self._closed:
            raise EndpointError("HARNESS_ENDPOINT_IO_ERROR: PTY 已關閉")

    def _ensure_io_allowed_locked(self) -> None:
        self._ensure_not_closed_locked()
        if self.generation == 0:
            raise EndpointError("HARNESS_ENDPOINT_IO_ERROR: PTY 尚未開啟")

    def _close_master_locked(self) -> None:
        master_fd = self._master_fd
        self._master_fd = None
        if master_fd is not None:
            try:
                os.close(master_fd)
            except OSError:
                pass

    @staticmethod
    def _peer_open_for_fd(master_fd: int) -> bool:
        poller = select.poll()
        try:
            poller.register(master_fd, select.POLLIN | select.POLLHUP | select.POLLERR)
            events = poller.poll(0)
        except OSError as exc:
            raise EndpointError(f"HARNESS_ENDPOINT_IO_ERROR: 檢查 PTY peer 失敗：{exc}") from exc
        return not any(event & select.POLLHUP for _, event in events)

    def _unlink_link_path(self) -> None:
        try:
            mode = os.lstat(self.by_id_link).st_mode
        except FileNotFoundError:
            return
        if not stat.S_ISLNK(mode):
            raise EndpointError(f"HARNESS_ENDPOINT_IO_ERROR: by-id 路徑不是 symlink：{self.by_id_link}")
        try:
            self.by_id_link.unlink()
        except OSError as exc:
            raise self._io_error("移除 by-id symlink", exc) from exc

    @staticmethod
    def _create_pty() -> tuple[int, str]:
        master_fd: int | None = None
        slave_fd: int | None = None
        try:
            master_fd, slave_fd = pty.openpty()
            tty.setraw(slave_fd)
            slave_path = os.ttyname(slave_fd)
            closing_slave_fd = slave_fd
            slave_fd = None
            os.close(closing_slave_fd)
            os.set_blocking(master_fd, False)
            return master_fd, slave_path
        except Exception:
            if slave_fd is not None:
                try:
                    os.close(slave_fd)
                except OSError:
                    pass
            if master_fd is not None:
                try:
                    os.close(master_fd)
                except OSError:
                    pass
            raise

    @staticmethod
    def _timeout_ms(timeout_s: float) -> int:
        if math.isinf(timeout_s) and timeout_s > 0:
            return -1
        if math.isnan(timeout_s) or timeout_s <= 0:
            return 0
        return math.ceil(timeout_s * 1000)

    @staticmethod
    def _io_error(action: str, exc: OSError) -> EndpointError:
        return EndpointError(f"HARNESS_ENDPOINT_IO_ERROR: {action}失敗：{exc}")


__all__ = ["PosixPtyEndpoint"]
