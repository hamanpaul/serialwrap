"""Windows WAL 檔案操作；模組可在 POSIX 匯入，呼叫時才載入 Windows API。"""

from __future__ import annotations

import ctypes
import errno
import io
import os
from dataclasses import dataclass
from typing import Any, Callable, NoReturn

GENERIC_WRITE = 0x40000000
FILE_APPEND_DATA = 0x0004
SYNCHRONIZE = 0x00100000
DELETE = 0x00010000
FILE_SHARE_ALL = 0x00000007
OPEN_ALWAYS = 4
OPEN_EXISTING = 3
FILE_ATTRIBUTE_NORMAL = 0x00000080
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
LOCKFILE_EXCLUSIVE_LOCK = 0x00000002
LOCKFILE_FAIL_IMMEDIATELY = 0x00000001
FILE_STANDARD_INFO = 1
FILE_ID_INFO = 18
FILE_DISPOSITION_INFO = 4
FILE_DISPOSITION_INFO_EX = 21

_ERROR_FILE_NOT_FOUND = 2
_ERROR_PATH_NOT_FOUND = 3
_ERROR_ACCESS_DENIED = 5
_ERROR_LOCK_VIOLATION = 33
_FILE_DISPOSITION_FLAG_DELETE = 0x00000001
_FILE_DISPOSITION_FLAG_POSIX_SEMANTICS = 0x00000002
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


@dataclass
class WinApi:
    """可注入的 Windows API 與 CRT 函式集合。"""

    CreateFileW: Callable[..., Any]
    CloseHandle: Callable[[int], int]
    GetFileInformationByHandleEx: Callable[..., int]
    LockFileEx: Callable[..., int]
    UnlockFileEx: Callable[..., int]
    SetFileInformationByHandle: Callable[..., int]
    FlushFileBuffers: Callable[[int], int]
    GetLastError: Callable[[], int]
    open_osfhandle: Callable[[int, int], int]
    get_osfhandle: Callable[[int], int]


class _FileStandardInfo(ctypes.Structure):
    _fields_ = [
        ("AllocationSize", ctypes.c_longlong),
        ("EndOfFile", ctypes.c_longlong),
        ("NumberOfLinks", ctypes.c_uint32),
        ("DeletePending", ctypes.c_ubyte),
        ("Directory", ctypes.c_ubyte),
    ]


class _FileIdInfo(ctypes.Structure):
    _fields_ = [
        ("VolumeSerialNumber", ctypes.c_uint64),
        ("FileId", ctypes.c_ubyte * 16),
    ]


class _OVERLAPPED(ctypes.Structure):
    _fields_ = [
        ("Internal", ctypes.c_void_p),
        ("InternalHigh", ctypes.c_void_p),
        ("Offset", ctypes.c_uint32),
        ("OffsetHigh", ctypes.c_uint32),
        ("hEvent", ctypes.c_void_p),
    ]


class _FileDispositionInfo(ctypes.Structure):
    _fields_ = [("DeleteFile", ctypes.c_ubyte)]


class _FileDispositionInfoEx(ctypes.Structure):
    _fields_ = [("Flags", ctypes.c_uint32)]


def load_api() -> WinApi:
    """載入 kernel32 與 msvcrt；POSIX 上以 ENOSYS 明確拒絕。"""
    if os.name != "nt":
        raise OSError(errno.ENOSYS, "Windows WAL API 僅能在 Windows 使用")

    import msvcrt

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = ctypes.c_void_p
    dword = ctypes.c_uint32
    bool_type = ctypes.c_int

    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        ctypes.c_wchar_p,
        dword,
        dword,
        ctypes.c_void_p,
        dword,
        dword,
        handle,
    ]
    create_file.restype = handle

    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [handle]
    close_handle.restype = bool_type

    file_info = kernel32.GetFileInformationByHandleEx
    file_info.argtypes = [handle, ctypes.c_int, ctypes.c_void_p, dword]
    file_info.restype = bool_type

    lock_file = kernel32.LockFileEx
    lock_file.argtypes = [handle, dword, dword, dword, dword, ctypes.POINTER(_OVERLAPPED)]
    lock_file.restype = bool_type

    unlock_file = kernel32.UnlockFileEx
    unlock_file.argtypes = [handle, dword, dword, dword, ctypes.POINTER(_OVERLAPPED)]
    unlock_file.restype = bool_type

    set_file_info = kernel32.SetFileInformationByHandle
    set_file_info.argtypes = [handle, ctypes.c_int, ctypes.c_void_p, dword]
    set_file_info.restype = bool_type

    flush_file = kernel32.FlushFileBuffers
    flush_file.argtypes = [handle]
    flush_file.restype = bool_type

    return WinApi(
        CreateFileW=create_file,
        CloseHandle=close_handle,
        GetFileInformationByHandleEx=file_info,
        LockFileEx=lock_file,
        UnlockFileEx=unlock_file,
        SetFileInformationByHandle=set_file_info,
        FlushFileBuffers=flush_file,
        GetLastError=ctypes.get_last_error,
        open_osfhandle=msvcrt.open_osfhandle,
        get_osfhandle=msvcrt.get_osfhandle,
    )


def _invalid_handle(handle: Any) -> bool:
    if handle is None:
        return True
    value = getattr(handle, "value", handle)
    if value is None:
        return True
    try:
        numeric = int(value)
    except (TypeError, ValueError):
        return False
    return numeric in (-1, _INVALID_HANDLE_VALUE)


def _raise_windows_error(api: WinApi, code: int | None = None) -> NoReturn:
    error_code = int(api.GetLastError()) if code is None else int(code)
    win_error = getattr(ctypes, "WinError", None)
    if win_error is not None:
        raise win_error(error_code)
    error_number = error_code or errno.EIO
    exc = OSError(error_number, f"Windows API 錯誤 {error_code}")
    exc.winerror = error_code
    raise exc


def _open_query_handle(path: str, api: WinApi) -> Any:
    handle = api.CreateFileW(
        path,
        0,
        FILE_SHARE_ALL,
        None,
        OPEN_EXISTING,
        FILE_FLAG_BACKUP_SEMANTICS,
        None,
    )
    if _invalid_handle(handle):
        _raise_windows_error(api)
    return handle


def _file_identity_for_handle(handle: Any, api: WinApi) -> tuple[int, int]:
    info = _FileIdInfo()
    ok = api.GetFileInformationByHandleEx(
        handle,
        FILE_ID_INFO,
        ctypes.byref(info),
        ctypes.sizeof(info),
    )
    if not ok:
        _raise_windows_error(api)
    file_id = int.from_bytes(bytes(info.FileId), "little")
    return (int(info.VolumeSerialNumber), file_id)


def _close_handle(api: WinApi, handle: Any) -> None:
    if not api.CloseHandle(handle):
        _raise_windows_error(api)


def open_shared_append(path: str, *, api: WinApi | None = None) -> io.FileIO:
    """以 append-only 與 share-delete 權限開啟檔案，並轉移 handle 所有權。"""
    win_api = api if api is not None else load_api()
    handle = win_api.CreateFileW(
        path,
        FILE_APPEND_DATA | SYNCHRONIZE,
        FILE_SHARE_ALL,
        None,
        OPEN_ALWAYS,
        FILE_ATTRIBUTE_NORMAL,
        None,
    )
    if _invalid_handle(handle):
        _raise_windows_error(win_api)

    raw_handle = getattr(handle, "value", handle)
    flags = os.O_APPEND | os.O_WRONLY | getattr(os, "O_BINARY", 0)
    try:
        fd = win_api.open_osfhandle(int(raw_handle), flags)
    except BaseException:
        try:
            win_api.CloseHandle(handle)
        except BaseException:
            pass
        raise

    try:
        return io.FileIO(fd, "ab", closefd=True)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        raise


def delete_pending(fd: int, *, api: WinApi | None = None) -> bool:
    """查詢檔案 handle 的 DeletePending 狀態。"""
    win_api = api if api is not None else load_api()
    handle = win_api.get_osfhandle(fd)
    info = _FileStandardInfo()
    ok = win_api.GetFileInformationByHandleEx(
        handle,
        FILE_STANDARD_INFO,
        ctypes.byref(info),
        ctypes.sizeof(info),
    )
    if not ok:
        _raise_windows_error(win_api)
    return bool(info.DeletePending)


def file_identity(fd: int, *, api: WinApi | None = None) -> tuple[int, int]:
    """由 CRT fd 對應的 Windows handle 取得 volume serial 與 128-bit FileId。"""
    win_api = api if api is not None else load_api()
    return _file_identity_for_handle(win_api.get_osfhandle(fd), win_api)


def path_identity(path: str, *, api: WinApi | None = None) -> tuple[int, int] | None:
    """開唯讀查詢 handle 取得路徑 FileId；辨識不存在與 delete-pending 名稱。"""
    win_api = api if api is not None else load_api()
    try:
        handle = _open_query_handle(path, win_api)
    except OSError as exc:
        error_code = getattr(exc, "winerror", None)
        code = int(error_code) if error_code is not None else int(win_api.GetLastError())
        if code in (_ERROR_FILE_NOT_FOUND, _ERROR_PATH_NOT_FOUND):
            return None
        if code == _ERROR_ACCESS_DENIED:
            return (0, 0)
        _raise_windows_error(win_api, code)

    try:
        identity = _file_identity_for_handle(handle, win_api)
    except BaseException:
        try:
            win_api.CloseHandle(handle)
        except BaseException:
            pass
        raise
    _close_handle(win_api, handle)
    return identity


def lock_file_ex(
    fd: int,
    *,
    exclusive: bool = True,
    fail_immediately: bool = True,
    api: WinApi | None = None,
) -> bool:
    """以 LockFileEx 鎖定 offset 0 起始的一個 byte。"""
    win_api = api if api is not None else load_api()
    flags = 0
    if exclusive:
        flags |= LOCKFILE_EXCLUSIVE_LOCK
    if fail_immediately:
        flags |= LOCKFILE_FAIL_IMMEDIATELY
    overlapped = _OVERLAPPED()
    if win_api.LockFileEx(
        win_api.get_osfhandle(fd),
        flags,
        0,
        1,
        0,
        ctypes.byref(overlapped),
    ):
        return True
    code = int(win_api.GetLastError())
    if fail_immediately and code == _ERROR_LOCK_VIOLATION:
        return False
    _raise_windows_error(win_api, code)


def unlock_file_ex(fd: int, *, api: WinApi | None = None) -> None:
    """釋放 offset 0 起始的一個 byte 鎖。"""
    win_api = api if api is not None else load_api()
    overlapped = _OVERLAPPED()
    if not win_api.UnlockFileEx(
        win_api.get_osfhandle(fd),
        0,
        1,
        0,
        ctypes.byref(overlapped),
    ):
        _raise_windows_error(win_api)


def _open_delete_handle(path: str, api: WinApi) -> Any:
    handle = api.CreateFileW(
        path,
        DELETE,
        FILE_SHARE_ALL,
        None,
        OPEN_EXISTING,
        FILE_FLAG_BACKUP_SEMANTICS,
        None,
    )
    if _invalid_handle(handle):
        _raise_windows_error(api)
    return handle


def delete_classic(path: str, *, api: WinApi | None = None) -> None:
    """以 FileDispositionInfo 觸發 classic delete-pending 語意。"""
    win_api = api if api is not None else load_api()
    handle = _open_delete_handle(path, win_api)
    info = _FileDispositionInfo()
    info.DeleteFile = 1
    try:
        ok = win_api.SetFileInformationByHandle(
            handle,
            FILE_DISPOSITION_INFO,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not ok:
            _raise_windows_error(win_api)
    except BaseException:
        try:
            win_api.CloseHandle(handle)
        except BaseException:
            pass
        raise
    _close_handle(win_api, handle)


def delete_posix_semantics(path: str, *, api: WinApi | None = None) -> None:
    """以 FileDispositionInfoEx 的 POSIX_SEMANTICS 旗標刪除名稱。"""
    win_api = api if api is not None else load_api()
    handle = _open_delete_handle(path, win_api)
    info = _FileDispositionInfoEx()
    info.Flags = _FILE_DISPOSITION_FLAG_DELETE | _FILE_DISPOSITION_FLAG_POSIX_SEMANTICS
    try:
        ok = win_api.SetFileInformationByHandle(
            handle,
            FILE_DISPOSITION_INFO_EX,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not ok:
            _raise_windows_error(win_api)
    except BaseException:
        try:
            win_api.CloseHandle(handle)
        except BaseException:
            pass
        raise
    _close_handle(win_api, handle)
