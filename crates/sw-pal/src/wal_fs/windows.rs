//! Windows WAL share-delete handle、FileIdInfo 與 DeletePending 查詢。

use std::fs;
use std::io;
use std::os::windows::ffi::OsStrExt;
use std::path::{Path, PathBuf};

use windows_sys::Win32::Foundation::{
    CloseHandle, GetLastError, ERROR_ACCESS_DENIED, ERROR_ALREADY_EXISTS, ERROR_FILE_EXISTS,
    ERROR_FILE_NOT_FOUND, ERROR_PATH_NOT_FOUND, GENERIC_READ, HANDLE, INVALID_HANDLE_VALUE,
};
use windows_sys::Win32::Storage::FileSystem::{
    CreateFileW, FileIdInfo, FileStandardInfo, FlushFileBuffers, GetFileAttributesW,
    GetFileInformationByHandleEx, MoveFileExW, ReadFile, SetFilePointerEx, WriteFile,
    FILE_APPEND_DATA, FILE_ATTRIBUTE_DIRECTORY, FILE_ATTRIBUTE_NORMAL, FILE_BEGIN,
    FILE_FLAG_BACKUP_SEMANTICS, FILE_ID_INFO, FILE_SHARE_DELETE, FILE_SHARE_READ, FILE_SHARE_WRITE,
    FILE_STANDARD_INFO, OPEN_ALWAYS, OPEN_EXISTING,
};

use crate::traits::{
    windows_crt_errno, windows_crt_error, windows_win32_error, PalError, PalErrorKind, PalResult,
};
use crate::wal_fs::{FileIdentity, WalFile, WalFs, WalWriteError};

/// Windows WAL 檔案系統。
pub struct WindowsWalFs;

struct WindowsWalFile {
    handle: Option<HANDLE>,
}

// 安全性：此 handle 由 WalFile 唯一持有；trait 只要求移轉執行緒，不允許並行存取。
unsafe impl Send for WindowsWalFile {}

impl WindowsWalFile {
    fn handle(&self) -> HANDLE {
        self.handle.expect("WAL handle 尚未關閉")
    }
}

impl WalFile for WindowsWalFile {
    fn write_all_flush(&mut self, bytes: &[u8]) -> Result<(), WalWriteError> {
        let handle = self.handle();
        let mut sent = 0;
        while sent < bytes.len() {
            let count = (bytes.len() - sent).min(u32::MAX as usize) as u32;
            let mut written = 0u32;
            // 安全性：handle 有效，切片涵蓋 count bytes，written 指向可寫入的 u32。
            let ok = unsafe {
                WriteFile(
                    handle,
                    bytes[sent..].as_ptr(),
                    count,
                    &mut written,
                    std::ptr::null_mut(),
                )
            };
            if ok == 0 {
                return Err(WalWriteError {
                    sent,
                    error: last_write_error(),
                });
            }
            if written == 0 {
                return Err(WalWriteError {
                    sent,
                    error: windows_crt_error(5),
                });
            }
            sent += written as usize;
        }
        Ok(())
    }

    fn sync(&mut self) -> PalResult<()> {
        // 安全性：handle 由此 WalFile 持有，FlushFileBuffers 同步呼叫且不保留指標。
        if unsafe { FlushFileBuffers(self.handle()) } == 0 {
            Err(last_error())
        } else {
            Ok(())
        }
    }

    fn identity(&self) -> PalResult<FileIdentity> {
        file_identity(self.handle())
    }

    fn unlinked(&self) -> PalResult<bool> {
        Ok(standard_info(self.handle())?.DeletePending)
    }

    fn close(mut self: Box<Self>) -> PalResult<()> {
        let Some(handle) = self.handle.take() else {
            return Ok(());
        };
        // 安全性：take 後唯一所有權移交給 CloseHandle，Drop 不會再次關閉。
        if unsafe { CloseHandle(handle) } == 0 {
            Err(last_error())
        } else {
            Ok(())
        }
    }
}

impl Drop for WindowsWalFile {
    fn drop(&mut self) {
        if let Some(handle) = self.handle.take() {
            // 安全性：Drop 只關閉仍由此結構持有的 handle，且 take 保證只關閉一次。
            unsafe { CloseHandle(handle) };
        }
    }
}

impl WalFs for WindowsWalFs {
    fn open_append(&self, path: &Path) -> PalResult<Box<dyn WalFile>> {
        let path = wide_path(path)?;
        // 安全性：路徑 NUL 結尾且呼叫期間有效；handle 成功後交由 WindowsWalFile 唯一持有。
        let handle = unsafe {
            CreateFileW(
                path.as_ptr(),
                FILE_APPEND_DATA | windows_sys::Win32::Storage::FileSystem::SYNCHRONIZE,
                FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                std::ptr::null(),
                OPEN_ALWAYS,
                FILE_ATTRIBUTE_NORMAL,
                std::ptr::null_mut(),
            )
        };
        if handle == INVALID_HANDLE_VALUE {
            return Err(last_error());
        }
        Ok(Box::new(WindowsWalFile {
            handle: Some(handle),
        }))
    }

    fn path_identity(&self, path: &Path) -> PalResult<Option<FileIdentity>> {
        let path = wide_path(path)?;
        // 安全性：路徑 NUL 結尾且呼叫期間有效；查詢 handle 由本函式唯一關閉。
        let handle = unsafe {
            CreateFileW(
                path.as_ptr(),
                0,
                FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                std::ptr::null(),
                OPEN_EXISTING,
                FILE_FLAG_BACKUP_SEMANTICS,
                std::ptr::null_mut(),
            )
        };
        if handle == INVALID_HANDLE_VALUE {
            let code = unsafe { GetLastError() };
            return match code {
                ERROR_FILE_NOT_FOUND | ERROR_PATH_NOT_FOUND => Ok(None),
                ERROR_ACCESS_DENIED => Ok(Some(FileIdentity(0, 0))),
                _ => Err(wal_win32_error(code)),
            };
        }
        let identity = file_identity(handle);
        // 安全性：此函式擁有成功開啟的查詢 handle。
        unsafe { CloseHandle(handle) };
        identity.map(Some)
    }

    fn size(&self, path: &Path) -> PalResult<Option<u64>> {
        match fs::metadata(path) {
            Ok(metadata) => Ok(Some(metadata.len())),
            Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(None),
            Err(error) => Err(file_error(error)),
        }
    }

    fn tail_needs_termination(&self, path: &Path) -> PalResult<bool> {
        let path = wide_path(path)?;
        // 安全性：路徑 NUL 結尾且呼叫期間有效；唯讀 handle 允許其他 WAL handle 讀、寫與刪除。
        let handle = unsafe {
            CreateFileW(
                path.as_ptr(),
                GENERIC_READ,
                FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                std::ptr::null(),
                OPEN_EXISTING,
                FILE_ATTRIBUTE_NORMAL,
                std::ptr::null_mut(),
            )
        };
        if handle == INVALID_HANDLE_VALUE {
            return Err(last_error());
        }
        let result = (|| {
            let size = standard_info(handle)?.EndOfFile;
            if size == 0 {
                return Ok(false);
            }
            // 安全性：handle 有效；偏移量取自剛查得的非零 EOF。
            if unsafe { SetFilePointerEx(handle, size - 1, std::ptr::null_mut(), FILE_BEGIN) } == 0
            {
                return Err(last_error());
            }
            let mut byte = [0u8; 1];
            let mut read = 0u32;
            // 安全性：byte 與 read 均為有效輸出緩衝區，handle 保持開啟。
            if unsafe {
                ReadFile(
                    handle,
                    byte.as_mut_ptr(),
                    1,
                    &mut read,
                    std::ptr::null_mut(),
                )
            } == 0
            {
                return Err(last_error());
            }
            Ok(read == 0 || byte[0] != b'\n')
        })();
        // 安全性：此函式擁有成功開啟的唯讀 handle。
        unsafe { CloseHandle(handle) };
        result
    }

    fn read_all(&self, path: &Path) -> PalResult<Option<Vec<u8>>> {
        match fs::read(path) {
            Ok(bytes) => Ok(Some(bytes)),
            Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(None),
            Err(error) => Err(file_error(error)),
        }
    }

    fn rename_noreplace_or_suffix(&self, from: &Path, base_to: &Path) -> PalResult<PathBuf> {
        let from = wide_path(from)?;
        for suffix in 0..1000 {
            let to = wide_path(&suffixed_path(base_to, suffix))?;
            // 安全性：兩個路徑均為 NUL 結尾 UTF-16；flags=0 不允許取代目的檔。
            if unsafe { MoveFileExW(from.as_ptr(), to.as_ptr(), 0) } != 0 {
                return Ok(suffixed_path(base_to, suffix));
            }
            let code = unsafe { GetLastError() };
            if code == ERROR_ALREADY_EXISTS || code == ERROR_FILE_EXISTS {
                continue;
            }
            return Err(wal_win32_error(code));
        }
        Err(windows_win32_error(ERROR_ALREADY_EXISTS))
    }

    fn sync_dir(&self, _dir: &Path) -> PalResult<()> {
        Ok(())
    }

    fn ensure_dir(&self, dir: &Path) -> PalResult<()> {
        fs::create_dir_all(dir).map_err(file_error)
    }

    fn dir_writable(&self, dir: &Path) -> bool {
        let Ok(path) = wide_path(dir) else {
            return false;
        };
        // 安全性：路徑 NUL 結尾且呼叫期間有效；GetFileAttributesW 不保留指標。
        let attributes = unsafe { GetFileAttributesW(path.as_ptr()) };
        attributes != u32::MAX && attributes & FILE_ATTRIBUTE_DIRECTORY != 0
    }
}

fn wide_path(path: &Path) -> PalResult<Vec<u16>> {
    let value: Vec<u16> = path.as_os_str().encode_wide().collect();
    if value.contains(&0) {
        return Err(PalError::invalid("path contains NUL"));
    }
    Ok(value.into_iter().chain(std::iter::once(0)).collect())
}

fn file_identity(handle: HANDLE) -> PalResult<FileIdentity> {
    let mut info = FILE_ID_INFO::default();
    // 安全性：輸出緩衝區大小符合 FILE_ID_INFO，handle 由呼叫端保持有效。
    if unsafe {
        GetFileInformationByHandleEx(
            handle,
            FileIdInfo,
            (&mut info as *mut FILE_ID_INFO).cast(),
            std::mem::size_of::<FILE_ID_INFO>() as u32,
        )
    } == 0
    {
        return Err(last_error());
    }
    Ok(FileIdentity(
        info.VolumeSerialNumber as u128,
        u128::from_le_bytes(info.FileId.Identifier),
    ))
}

fn standard_info(handle: HANDLE) -> PalResult<FILE_STANDARD_INFO> {
    let mut info = FILE_STANDARD_INFO::default();
    // 安全性：輸出緩衝區大小符合 FILE_STANDARD_INFO，handle 由呼叫端保持有效。
    if unsafe {
        GetFileInformationByHandleEx(
            handle,
            FileStandardInfo,
            (&mut info as *mut FILE_STANDARD_INFO).cast(),
            std::mem::size_of::<FILE_STANDARD_INFO>() as u32,
        )
    } == 0
    {
        return Err(last_error());
    }
    Ok(info)
}

fn last_error() -> PalError {
    wal_win32_error(unsafe { GetLastError() })
}

fn last_write_error() -> PalError {
    write_error(unsafe { GetLastError() })
}

fn write_error(code: u32) -> PalError {
    windows_crt_error(windows_crt_errno(code))
}

fn wal_win32_error(code: u32) -> PalError {
    let mut error = windows_win32_error(code);
    if code == ERROR_ACCESS_DENIED {
        error.kind = PalErrorKind::AccessDenied;
    }
    error
}

fn file_error(error: io::Error) -> PalError {
    let code = error.raw_os_error().unwrap_or(5) as u32;
    windows_crt_error(windows_crt_errno(code))
}

fn suffixed_path(base: &Path, suffix: usize) -> PathBuf {
    if suffix == 0 {
        return base.to_path_buf();
    }
    let mut value = base.as_os_str().to_os_string();
    value.push(format!(".{suffix}"));
    PathBuf::from(value)
}

#[cfg(test)]
mod tests {
    use super::write_error;

    #[test]
    fn write_error_uses_python_crt_errno_format() {
        let error = write_error(112);

        assert_eq!(error.os_code, Some(28));
        assert_eq!(error.py_message, "[Errno 28] No space left on device");
    }
}
