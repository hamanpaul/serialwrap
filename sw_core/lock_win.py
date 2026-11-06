from __future__ import annotations

import os
import socket
from urllib.parse import urlsplit

from .util import endpoint_path_for_io


def _endpoint_alive(endpoint: str) -> bool:
    """嘗試 TCP connect endpoint；連得上回 True，拒絕/逾時回 False。"""
    parsed = urlsplit(endpoint)
    if parsed.scheme != "tcp" or not parsed.hostname or parsed.port is None:
        return False
    try:
        with socket.create_connection((parsed.hostname, parsed.port), timeout=0.2):
            return True
    except OSError:
        return False


class WindowsSingletonLock:
    """Windows daemon 單例：LockFileEx 獨佔檔鎖 + TCP endpoint 存活探測（#84 PORT-4）。

    語意對齊 POSIX ``SingletonLock``（flock + Unix socket probe）：
    - endpoint 可連 → 既有 daemon 在跑 → raise ``RuntimeError("DAEMON_ALREADY_RUNNING")``
    - endpoint refused/timeout（stale）→ 繼續取 LockFileEx 檔鎖
    - 檔案鎖已被另一程序持有 → raise ``RuntimeError("DAEMON_ALREADY_RUNNING")``
    """

    def __init__(self, lock_path: str, endpoint: str) -> None:
        self.lock_path = endpoint_path_for_io(lock_path)
        self.endpoint = endpoint_path_for_io(endpoint)
        self._fd: int | None = None

    def acquire(self) -> None:
        """取得單例鎖；失敗時 raise RuntimeError("DAEMON_ALREADY_RUNNING")。"""
        if _endpoint_alive(self.endpoint):
            raise RuntimeError("DAEMON_ALREADY_RUNNING")

        from .win32_files import lock_file_ex  # noqa: PLC0415 — Windows API 延遲載入

        parent = os.path.dirname(self.lock_path)
        if parent:
            os.makedirs(parent, exist_ok=True)

        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if not lock_file_ex(fd, exclusive=True, fail_immediately=True):
                raise BlockingIOError("LockFileEx lock is held")
        except OSError:
            os.close(fd)
            raise RuntimeError("DAEMON_ALREADY_RUNNING")

        self._fd = fd
        os.ftruncate(self._fd, 0)
        os.write(self._fd, f"{os.getpid()}\n".encode("ascii"))
        os.fsync(self._fd)  # 與 POSIX 版對齊，確保 PID 寫入持久化

    def release(self) -> None:
        """釋放 LockFileEx 檔鎖並關閉 fd；未取鎖時為 no-op。"""
        if self._fd is None:
            return
        from .win32_files import unlock_file_ex  # noqa: PLC0415 — Windows API 延遲載入

        try:
            unlock_file_ex(self._fd)
        finally:
            os.close(self._fd)
            self._fd = None
