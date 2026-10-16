"""WAL 的檔案系統接縫；Windows 無目錄 fsync 對應操作，持久性依 NTFS journal。"""

from __future__ import annotations

import errno
import io
import os
from typing import TYPE_CHECKING, BinaryIO, Protocol

if TYPE_CHECKING:
    from .win32_files import WinApi


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


class WindowsWalFile:
    """包裝 share-delete 的 Windows WAL handle。"""

    def __init__(self, fp: io.FileIO, api: WinApi | None = None) -> None:
        self._fp = fp
        self._api = api
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
        # FileIO 不留使用者層緩衝；每次 write 已直接交給作業系統。
        self._fp.flush()

    def sync(self) -> None:
        # Windows CPython 將 fsync(fd) 對應到 _commit／FlushFileBuffers。
        os.fsync(self._fp.fileno())

    def identity(self) -> tuple[int, int]:
        from . import win32_files

        return win32_files.file_identity(self._fp.fileno(), api=self._api)

    def unlinked(self) -> bool:
        from . import win32_files

        return win32_files.delete_pending(self._fp.fileno(), api=self._api)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._fp.close()


class WindowsWalFs:
    """以 CreateFileW share-delete handle 實作 Windows WAL 檔案操作。"""

    def __init__(self, api: WinApi | None = None) -> None:
        self._api = api

    def open_append(self, path: str) -> WindowsWalFile:
        from . import win32_files

        return WindowsWalFile(
            win32_files.open_shared_append(path, api=self._api), api=self._api
        )

    def path_identity(self, path: str) -> tuple[int, int] | None:
        from . import win32_files

        return win32_files.path_identity(path, api=self._api)

    def size(self, path: str) -> int | None:
        try:
            return os.stat(path).st_size
        except FileNotFoundError:
            return None

    def rename_noreplace_or_suffix(self, src: str, base_dst: str) -> str:
        for suffix in range(1000):
            dst = base_dst if suffix == 0 else f"{base_dst}.{suffix}"
            if os.path.exists(dst):
                continue
            try:
                # Windows os.rename 不會覆蓋既有目標；競態下由 FileExistsError 重試。
                os.rename(src, dst)
            except FileExistsError:
                continue
            return dst
        raise FileExistsError(errno.EEXIST, "無可用的 WAL 歸檔名稱", base_dst)

    def sync_dir(self, path: str) -> None:
        """Windows 沒有目錄 fsync 對應操作；rename 持久性依 NTFS journal。"""

    def ensure_dir(self, path: str) -> None:
        os.makedirs(path, exist_ok=True)

    def dir_writable(self, path: str) -> bool:
        return os.path.isdir(path) and os.access(path, os.W_OK | os.X_OK)


def default_wal_fs() -> WalFs:
    """依作業系統回傳 WAL 檔案系統實作。"""
    if os.name == "nt":
        return WindowsWalFs()
    return PosixWalFs()
