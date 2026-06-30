from __future__ import annotations

import os
import socket
from urllib.parse import urlsplit


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
    """Windows daemon 單例：msvcrt 獨佔檔鎖 + TCP endpoint 存活探測（#84 PORT-4）。

    語意對齊 POSIX ``SingletonLock``（flock + Unix socket probe）：
    - endpoint 可連 → 既有 daemon 在跑 → raise ``RuntimeError("DAEMON_ALREADY_RUNNING")``
    - endpoint refused/timeout（stale）→ 繼續取 msvcrt 檔鎖
    - 檔鎖已被另一程序持有 → raise ``RuntimeError("DAEMON_ALREADY_RUNNING")``
    """

    def __init__(self, lock_path: str, endpoint: str) -> None:
        self.lock_path = lock_path
        self.endpoint = endpoint
        self._fd: int | None = None

    def acquire(self) -> None:
        """取得單例鎖；失敗時 raise RuntimeError("DAEMON_ALREADY_RUNNING")。"""
        if _endpoint_alive(self.endpoint):
            raise RuntimeError("DAEMON_ALREADY_RUNNING")

        import msvcrt  # noqa: PLC0415 — Windows-only，延遲 import 讓模組可在非 Windows import

        parent = os.path.dirname(self.lock_path)
        if parent:
            os.makedirs(parent, exist_ok=True)

        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            os.close(fd)
            raise RuntimeError("DAEMON_ALREADY_RUNNING")

        self._fd = fd
        os.ftruncate(self._fd, 0)
        os.write(self._fd, f"{os.getpid()}\n".encode("ascii"))
        os.fsync(self._fd)  # 與 POSIX 版對齊，確保 PID 寫入持久化

    def release(self) -> None:
        """釋放 msvcrt 檔鎖並關閉 fd；未取鎖時為 no-op。"""
        if self._fd is None:
            return
        import msvcrt  # noqa: PLC0415

        try:
            os.lseek(self._fd, 0, os.SEEK_SET)
            msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
        finally:
            os.close(self._fd)
            self._fd = None
