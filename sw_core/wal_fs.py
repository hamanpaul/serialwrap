from __future__ import annotations

import errno
import os
from typing import BinaryIO, Protocol


class PartialWriteError(OSError):
    """記錄 WAL 寫入失敗前已由作業系統確認的位元組數。"""

    def __init__(self, err_no: int, message: str, *, sent: int = 0) -> None:
        super().__init__(err_no, message)
        self.sent = sent


class WalFile(Protocol):
    def write_all_flush(self, data: bytes) -> None: ...

    def sync(self) -> None: ...

    def identity(self) -> tuple[int, int]: ...

    def unlinked(self) -> bool: ...

    def close(self) -> None: ...


class WalFs(Protocol):
    def open_append(self, path: str) -> WalFile: ...

    def path_identity(self, path: str) -> tuple[int, int] | None: ...

    def size(self, path: str) -> int | None: ...

    def rename_noreplace_or_suffix(self, src: str, base_dst: str) -> str: ...

    def sync_dir(self, path: str) -> None: ...

    def ensure_dir(self, path: str) -> None: ...

    def dir_writable(self, path: str) -> bool: ...


class PosixWalFile:
    """以未緩衝 append handle 寫入 WAL，並提供 handle 身分與同步操作。"""

    def __init__(self, fp: BinaryIO) -> None:
        self._fp = fp
        self._closed = False

    def write_all_flush(self, data: bytes) -> None:
        view = memoryview(data)
        sent = 0
        while sent < len(view):
            try:
                count = self._fp.write(view[sent:])
            except OSError as exc:
                raise PartialWriteError(
                    exc.errno or errno.EIO, exc.strerror or str(exc), sent=sent
                ) from exc
            if count is None or count <= 0:
                raise PartialWriteError(errno.EIO, "short write", sent=sent)
            sent += count

    def sync(self) -> None:
        os.fsync(self._fp.fileno())

    def identity(self) -> tuple[int, int]:
        stat = os.fstat(self._fp.fileno())
        return (stat.st_dev, stat.st_ino)

    def unlinked(self) -> bool:
        return os.fstat(self._fp.fileno()).st_nlink == 0

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._fp.close()


class PosixWalFs:
    """POSIX 檔案系統的 WAL handle 實作。"""

    def open_append(self, path: str) -> PosixWalFile:
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
        fd = os.open(path, flags, 0o666)
        try:
            fp = os.fdopen(fd, "ab", buffering=0)
        except BaseException:
            try:
                os.close(fd)
            except OSError:
                pass
            raise
        return PosixWalFile(fp)

    def path_identity(self, path: str) -> tuple[int, int] | None:
        try:
            stat = os.stat(path)
        except FileNotFoundError:
            return None
        return (stat.st_dev, stat.st_ino)

    def size(self, path: str) -> int | None:
        try:
            return os.stat(path).st_size
        except FileNotFoundError:
            return None

    def rename_noreplace_or_suffix(self, src: str, base_dst: str) -> str:
        raise NotImplementedError("WAL 歸檔命名由 T04 實作")

    def sync_dir(self, path: str) -> None:
        if os.name == "nt":
            return
        dir_fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)

    def ensure_dir(self, path: str) -> None:
        os.makedirs(path, exist_ok=True)

    def dir_writable(self, path: str) -> bool:
        return os.path.isdir(path) and os.access(path, os.W_OK | os.X_OK)


def default_wal_fs() -> WalFs:
    """回傳本階段可用的 WAL 檔案系統；Windows 專用 handle 由後續 Task 接手。"""
    return PosixWalFs()
