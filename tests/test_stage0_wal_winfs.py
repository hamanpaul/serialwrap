# tests/test_stage0_wal_winfs.py
"""P1 Task T03：BC-01b Windows CreateFileW share-delete handle 與 win32_files 測試。

驗證 Windows WAL 常駐 handle 的 FILE_SHARE_READ|FILE_SHARE_WRITE|FILE_SHARE_DELETE 旗標、
open_osfhandle 所有權轉移、失敗清理、唯一 close、DeletePending 狀態查詢、
FileId 識別碼查詢、LockFileEx 鎖定包裝，以及 WindowsWalFs 的實作行為。

依據：規格 §8.2、§6.2；arch §4.4、§4.5；02-stage0-python-bc.md Task T03。
依 02-stage0-python-bc.md 規則：不在模組頂層 import 本 Phase 新增的模組，一律於測試函式內延遲載入。
依 P0 T14 marker 規則：本檔混合不同平台與資源類型，模組層不宣告 pytestmark，每個測試 item 恰有一個平台 marker 與至少一個資源 marker。
"""
from __future__ import annotations

import importlib
import os
from typing import Any

import pytest


def _load_w32() -> Any:
    """延遲載入 sw_core.win32_files 模組；尚未實作時拋出 ImportError。"""
    try:
        return importlib.import_module("sw_core.win32_files")
    except ModuleNotFoundError:
        raise ImportError("cannot import name 'win32_files' from 'sw_core'")


def _load_wal_fs() -> Any:
    """延遲載入 sw_core.wal_fs 模組；尚未實作時拋出 ImportError。"""
    try:
        return importlib.import_module("sw_core.wal_fs")
    except ModuleNotFoundError:
        raise ImportError("cannot import name 'wal_fs' from 'sw_core'")


class FakeApi:
    """用於模擬 Windows kernel32 與 msvcrt 呼叫的替身。"""

    def __init__(
        self,
        *,
        fail_osfhandle: bool = False,
        create_error: int | None = None,
        file_info_error: int | None = None,
        delete_pending: bool = False,
        volume_serial: int = 0x12345678,
        file_id: int = int.from_bytes(bytes(range(16)), "little"),
        lock_succeeds: bool = True,
        last_error: int = 0,
        error_sequence: list[int] | None = None,
    ) -> None:
        self.closed: list[int] = []
        self.fail_osfhandle = fail_osfhandle
        self.create_args: list[tuple] = []
        self.info_classes: list[int] = []
        self.create_error = create_error
        self.file_info_error = file_info_error
        self.delete_pending = delete_pending
        self.volume_serial = volume_serial
        self.file_id = file_id
        self.lock_succeeds = lock_succeeds
        self.last_error = last_error
        self.error_sequence = list(error_sequence or [])
        self.get_last_error_calls = 0

    def CreateFileW(self, *a: Any) -> int:
        self.create_args.append(a)
        if self.create_error is not None:
            self.last_error = self.create_error
            return -1
        return 1234

    def CloseHandle(self, h: int) -> int:
        self.closed.append(h)
        return 1

    def open_osfhandle(self, h: int, flags: int) -> int:
        if self.fail_osfhandle:
            raise OSError(24, "Too many open files")
        return os.open(os.devnull, os.O_WRONLY)

    def GetLastError(self) -> int:
        self.get_last_error_calls += 1
        if self.error_sequence:
            return self.error_sequence.pop(0)
        return self.last_error

    def get_osfhandle(self, fd: int) -> int:
        return 1234

    def GetFileInformationByHandleEx(
        self, handle: Any, info_class: int, info_ptr: Any, size: int
    ) -> int:
        self.info_classes.append(info_class)
        if self.file_info_error is not None:
            self.last_error = self.file_info_error
            return 0
        info = info_ptr._obj
        if info_class == 1:
            info.DeletePending = int(self.delete_pending)
        elif info_class == 18:
            info.VolumeSerialNumber = self.volume_serial
            for index, byte in enumerate(self.file_id.to_bytes(16, "little")):
                info.FileId[index] = byte
        return 1

    def LockFileEx(self, *a: Any) -> int:
        return int(self.lock_succeeds)

    def UnlockFileEx(self, *a: Any) -> int:
        return 1

    def SetFileInformationByHandle(self, *a: Any) -> int:
        return 1

    def FlushFileBuffers(self, h: int) -> int:
        return 1


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_module_imports_on_posix_without_windll():
    """win32_files 模組在 POSIX 上可直接 import，且定義了 open_shared_append。"""
    w32 = _load_w32()
    assert hasattr(w32, "open_shared_append")


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_create_uses_share_all_and_append_access(tmp_path):
    """open_shared_append 必須使用 FILE_APPEND_DATA|SYNCHRONIZE、FILE_SHARE_ALL (0x7) 與 OPEN_ALWAYS。"""
    w32 = _load_w32()
    api = FakeApi()
    fp = w32.open_shared_append(str(tmp_path / "w"), api=api)
    fp.close()
    _, access, share, _, disposition, _, _ = api.create_args[0]
    assert access == w32.FILE_APPEND_DATA | w32.SYNCHRONIZE
    assert share == w32.FILE_SHARE_ALL == 0x7
    assert disposition == w32.OPEN_ALWAYS


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_open_osfhandle_failure_closes_raw_handle_exactly_once(tmp_path):
    """open_osfhandle 失敗時，必須確實呼叫 CloseHandle(h) 恰好一次以釋放資源。"""
    w32 = _load_w32()
    api = FakeApi(fail_osfhandle=True)
    with pytest.raises(OSError):
        w32.open_shared_append(str(tmp_path / "w"), api=api)
    assert api.closed == [1234]


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_success_path_never_calls_closehandle_directly(tmp_path):
    """成功建立 FileIO 後，handle 所有權轉移至 CRT fd，不得直接呼叫 CloseHandle。"""
    w32 = _load_w32()
    api = FakeApi()
    fp = w32.open_shared_append(str(tmp_path / "w"), api=api)
    fp.close()
    assert api.closed == []


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_windows_wal_fs_uses_injected_api_and_delete_pending_only():
    """WindowsWalFs 將 API 傳給 handle，unlinked 只查 DeletePending。"""
    w32 = _load_w32()
    wal_fs = _load_wal_fs()
    api = FakeApi(delete_pending=True)
    fs = wal_fs.WindowsWalFs(api=api)
    wf = fs.open_append("injected-wal")
    expected_identity = (api.volume_serial, api.file_id)
    try:
        assert wf.identity() == expected_identity
        assert wf.unlinked() is True
        assert api.info_classes == [w32.FILE_ID_INFO, w32.FILE_STANDARD_INFO]
        assert len(api.create_args) == 1  # unlinked 不得再開路徑查名稱 identity
    finally:
        wf.close()

    assert api.closed == []  # open_osfhandle 已接手 handle 所有權
    assert fs.path_identity("injected-wal") == expected_identity
    assert api.closed == [1234]  # path_identity 的查詢 handle 恰關一次


@pytest.mark.parametrize(
    ("error_code", "expected"),
    [(2, None), (3, None), (5, (0, 0))],
)
@pytest.mark.core_portable
@pytest.mark.memory_only
def test_path_identity_maps_missing_and_access_denied(error_code, expected):
    """路徑不存在回 None，delete-pending 的 access denied 回 identity 哨兵。"""
    w32 = _load_w32()
    api = FakeApi(create_error=error_code, last_error=error_code)
    assert w32.path_identity("query-path", api=api) == expected


@pytest.mark.parametrize("error_code", [32, 123])
@pytest.mark.core_portable
@pytest.mark.memory_only
def test_path_identity_propagates_unexpected_winerror(error_code):
    """未定義的 Windows 路徑錯誤必須保留原始 winerror。"""
    w32 = _load_w32()
    api = FakeApi(create_error=error_code, last_error=error_code)
    with pytest.raises(OSError) as caught:
        w32.path_identity("query-path", api=api)
    assert getattr(caught.value, "winerror", None) == error_code


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_path_identity_uses_exception_winerror_without_rereading_last_error():
    """錯誤碼取自例外，避免第二次 GetLastError 讀到已變動的值。"""
    w32 = _load_w32()
    api = FakeApi(create_error=2, error_sequence=[2, 123])
    assert w32.path_identity("missing-path", api=api) is None
    assert api.get_last_error_calls == 1


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_path_identity_closes_query_handle_once_when_file_info_fails():
    """FileId 查詢失敗時，path_identity 必須恰好關閉查詢 handle 一次。"""
    w32 = _load_w32()
    api = FakeApi(file_info_error=123, last_error=123)
    with pytest.raises(OSError):
        w32.path_identity("query-path", api=api)
    assert api.closed == [1234]


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_lock_file_ex_maps_lock_violation_and_propagates_other_errors():
    """鎖衝突回 False，其他 LockFileEx 錯誤照常擲出。"""
    w32 = _load_w32()
    fd = os.open(os.devnull, os.O_RDONLY)
    try:
        assert w32.lock_file_ex(fd, api=FakeApi(lock_succeeds=False, last_error=33)) is False
        api = FakeApi(lock_succeeds=False, last_error=5)
        with pytest.raises(OSError) as caught:
            w32.lock_file_ex(fd, api=api)
        assert getattr(caught.value, "winerror", None) == 5
    finally:
        os.close(fd)


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_windows_wal_fs_rename_suffix_and_size(tmp_path):
    """WindowsWalFs 依序選擇可用後綴，並回報檔案大小。"""
    wal_fs = _load_wal_fs()
    fs = wal_fs.WindowsWalFs()
    src = tmp_path / "source.wal"
    base = tmp_path / "archive.wal"
    src.write_bytes(b"payload")
    base.write_bytes(b"first")
    (tmp_path / "archive.wal.1").write_bytes(b"second")

    assert fs.size(str(src)) == len(b"payload")
    assert fs.size(str(tmp_path / "missing.wal")) is None
    assert fs.rename_noreplace_or_suffix(str(src), str(base)) == str(
        tmp_path / "archive.wal.2"
    )
    assert (tmp_path / "archive.wal.2").read_bytes() == b"payload"


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_windows_wal_fs_raises_when_all_archive_suffixes_exist(tmp_path):
    """歸檔本名與 .1 至 .999 全占用時必須回報 FileExistsError。"""
    wal_fs = _load_wal_fs()
    fs = wal_fs.WindowsWalFs()
    src = tmp_path / "source.wal"
    src.write_bytes(b"payload")
    base = tmp_path / "archive.wal"
    for suffix in range(1000):
        path = base if suffix == 0 else tmp_path / f"archive.wal.{suffix}"
        path.write_bytes(b"existing")

    with pytest.raises(FileExistsError):
        fs.rename_noreplace_or_suffix(str(src), str(base))
    assert src.read_bytes() == b"payload"


@pytest.mark.posix_only
@pytest.mark.memory_only
def test_load_api_on_posix_raises_enosys():
    """在非 Windows (POSIX) 平台呼叫 load_api() 應拋出 OSError(ENOSYS)。"""
    w32 = _load_w32()
    if os.name == "nt":
        pytest.skip("POSIX only")
    with pytest.raises(OSError) as ei:
        w32.load_api()
    import errno
    assert ei.value.errno == errno.ENOSYS


@pytest.mark.windows_native
@pytest.mark.memory_only
def test_native_handle_allows_rename_and_delete_while_open(tmp_path):
    """Windows 原生 handle 帶 FILE_SHARE_DELETE 時，開啟期間允許外部 rename 與 delete。"""
    w32 = _load_w32()
    p = str(tmp_path / "raw.wal.ndjson")
    fp = w32.open_shared_append(p)
    fp.write(b"x\n")
    os.replace(p, p + ".1")  # 缺 FILE_SHARE_DELETE 時 PermissionError
    w32.delete_classic(p + ".1")  # 明確 classic 語意（os.remove 在新版 Windows 可能走 POSIX semantics）
    assert os.path.exists(p + ".1") and w32.path_identity(p + ".1") == (0, 0)  # classic 已生效，否則 FAIL
    assert w32.delete_pending(fp.fileno()) is True
    fp.close()
    assert not os.path.exists(p + ".1")


@pytest.mark.windows_native
@pytest.mark.memory_only
def test_native_identity_changes_after_replace(tmp_path):
    """Windows 原生路徑被 replace 並新建後，path_identity 必須反映不同的 FileId。"""
    w32 = _load_w32()
    p = str(tmp_path / "a")
    fp = w32.open_shared_append(p)
    ident = w32.file_identity(fp.fileno())
    assert w32.path_identity(p) == ident
    os.replace(p, p + ".old")
    open(p, "wb").close()
    assert w32.path_identity(p) != ident
    fp.close()


@pytest.mark.windows_native
@pytest.mark.process_rpc
def test_native_lockfileex_conflicts_with_msvcrt_locking(tmp_path):
    """LockFileEx 與 msvcrt.locking 在 position 0 長度 1 互斥相容。"""
    w32 = _load_w32()
    import msvcrt, subprocess, sys
    p = str(tmp_path / "lock")
    fd = os.open(p, os.O_RDWR | os.O_CREAT, 0o600)
    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    code = ("import os,sys; from sw_core import win32_files as w; "
            f"fd=os.open({p!r}, os.O_RDWR); sys.exit(0 if w.lock_file_ex(fd) is False else 1)")
    assert subprocess.run([sys.executable, "-c", code], cwd=os.getcwd()).returncode == 0


@pytest.mark.windows_native
@pytest.mark.memory_only
def test_native_windows_walfs_unlinked_reports_delete_pending(tmp_path):
    """WindowsWalFs.open_append 開啟後經 delete_classic 刪除，unlinked() 應回傳 True。"""
    w32 = _load_w32()
    wal_fs = _load_wal_fs()
    p = str(tmp_path / "wal.ndjson")
    fs = wal_fs.WindowsWalFs()
    wf = fs.open_append(p)
    try:
        assert wf.unlinked() is False
        w32.delete_classic(p)
        assert wf.unlinked() is True
    finally:
        wf.close()


@pytest.mark.windows_native
@pytest.mark.memory_only
def test_native_os_remove_semantics_recorded(tmp_path, record_property):
    """記錄 os.remove 在本機是 classic 或 POSIX semantics，且兩種情況皆不使偵測失準。"""
    w32 = _load_w32()
    wal_fs = _load_wal_fs()
    p = str(tmp_path / "os_remove_target.ndjson")
    fs = wal_fs.WindowsWalFs()
    wf = fs.open_append(p)
    try:
        wf.write_all_flush(b"entry\n")
        wf.sync()
        os.remove(p)
        exists = os.path.exists(p)
        ident = w32.path_identity(p)
        unlinked = wf.unlinked()
        record_property("os_remove_semantics", "classic" if exists else "posix")
        if exists:
            assert unlinked is True
            assert ident == (0, 0)
        else:
            assert ident is None
            assert unlinked is True
    finally:
        wf.close()


@pytest.mark.windows_native
@pytest.mark.memory_only
def test_native_sync_on_append_handle(tmp_path):
    """WindowsWalFs 在 FILE_APPEND_DATA|SYNCHRONIZE handle 上呼叫 sync() 不得拋出異常。"""
    w32 = _load_w32()
    wal_fs = _load_wal_fs()
    p = str(tmp_path / "sync_target.ndjson")
    fs = wal_fs.WindowsWalFs()
    wf = fs.open_append(p)
    try:
        wf.write_all_flush(b"data\n")
        wf.sync()
    finally:
        wf.close()


@pytest.mark.windows_native
@pytest.mark.memory_only
def test_native_sync_dir_is_documented_noop(tmp_path, monkeypatch):
    """WindowsWalFs.sync_dir 為 no-op 且不開啟任何 handle，並於 docstring 記錄 NTFS journal。"""
    w32 = _load_w32()
    wal_fs = _load_wal_fs()
    fs = wal_fs.WindowsWalFs()
    calls: list[tuple] = []
    real_load_api = getattr(w32, "load_api", None)
    if real_load_api is not None:
        try:
            api = real_load_api()
            orig_create = api.CreateFileW

            def spy_create(*args: Any, **kwargs: Any) -> Any:
                calls.append(args)
                return orig_create(*args, **kwargs)

            api.CreateFileW = spy_create
            monkeypatch.setattr(w32, "load_api", lambda: api)
        except OSError:
            pass
    fs.sync_dir(str(tmp_path))
    assert len(calls) == 0
    doc = (getattr(wal_fs, "__doc__", "") or "") + (getattr(fs.sync_dir, "__doc__", "") or "")
    assert "NTFS journal" in doc


@pytest.mark.windows_native
@pytest.mark.memory_only
def test_native_path_identity_missing_returns_none(tmp_path):
    """path_identity 查詢不存在的路徑時應回傳 None。"""
    w32 = _load_w32()
    p = str(tmp_path / "nonexistent_file.txt")
    assert w32.path_identity(p) is None


@pytest.mark.windows_native
@pytest.mark.memory_only
def test_native_reader_without_share_delete_blocks_rename(tmp_path):
    """標準 Python open() 缺少 FILE_SHARE_DELETE，開啟時外部 os.replace 應拋出 PermissionError。"""
    w32 = _load_w32()
    p = str(tmp_path / "reader_target.txt")
    q = str(tmp_path / "reader_target.txt.1")
    with open(p, "wb") as f:
        f.write(b"data\n")
    with open(p, "rb"):
        with pytest.raises(PermissionError):
            os.replace(p, q)
