//! Windows 檔案讀寫、連結與跨行程鎖。

use crate::fs_ops::{FileLockGuard, FsOps, LinkKind};
use crate::traits::{
    windows_crt_errno, windows_crt_error, windows_message, windows_win32_error, FsRead, PalError,
    PalErrorKind, PalResult,
};
use std::fs::{self, File, OpenOptions};
use std::io::{self, Read, Write};
use std::os::windows::ffi::OsStrExt;
use std::os::windows::fs::FileTypeExt;
use std::os::windows::io::AsRawHandle;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::Duration;
use windows_sys::Win32::Foundation::{GetLastError, HANDLE};
use windows_sys::Win32::Storage::FileSystem::{
    CreateSymbolicLinkW, GetFileAttributesW, LockFileEx, MoveFileExW, SetFileAttributesW,
    FILE_ATTRIBUTE_READONLY, INVALID_FILE_ATTRIBUTES, LOCKFILE_EXCLUSIVE_LOCK,
    LOCKFILE_FAIL_IMMEDIATELY, MOVEFILE_REPLACE_EXISTING, MOVEFILE_WRITE_THROUGH,
    SYMBOLIC_LINK_FLAG_ALLOW_UNPRIVILEGED_CREATE, SYMBOLIC_LINK_FLAG_DIRECTORY,
};
use windows_sys::Win32::System::IO::OVERLAPPED;

/// `ERROR_PRIVILEGE_NOT_HELD`：未啟用開發者模式且非提權時建立 symlink 的錯誤碼。
const ERROR_PRIVILEGE_NOT_HELD: u32 = 1314;

/// 依建立 symlink 的結果決定連結或回退為遞迴複製（DF-16）；`create` 失敗時回傳 `GetLastError()`。
fn link_or_copy(
    link: &Path,
    target: &Path,
    create: impl FnOnce() -> Result<(), u32>,
) -> PalResult<LinkKind> {
    match create() {
        Ok(()) => Ok(LinkKind::Symlink),
        Err(ERROR_PRIVILEGE_NOT_HELD) => {
            copy_tree(target, link)?;
            Ok(LinkKind::Copied)
        }
        Err(code) => Err(windows_win32_error(code)),
    }
}

/// 測試用：以 `ERROR_PRIVILEGE_NOT_HELD` 取代實際建立 symlink，驗證回退複製路徑，不依賴主機是否啟用開發者模式。
#[cfg(feature = "test-fakes")]
#[doc(hidden)]
pub fn symlink_or_copy_dir_privilege_not_held(link: &Path, target: &Path) -> PalResult<LinkKind> {
    fs::create_dir_all(parent_dir(link)).map_err(file_error)?;
    remove_existing(link)?;
    link_or_copy(link, target, || Err(ERROR_PRIVILEGE_NOT_HELD))
}

#[derive(Clone, Copy, Debug, Default)]
pub struct NativeFsRead;

#[derive(Clone, Copy, Debug, Default)]
pub struct NativeFsOps;

impl FsRead for NativeFsOps {
    fn read(&self, path: &Path) -> PalResult<Vec<u8>> {
        NativeFsRead.read(path)
    }

    fn exists(&self, path: &Path) -> bool {
        NativeFsRead.exists(path)
    }

    fn is_dir(&self, path: &Path) -> bool {
        NativeFsRead.is_dir(path)
    }

    fn is_file(&self, path: &Path) -> bool {
        NativeFsRead.is_file(path)
    }

    fn list_dir(&self, path: &Path) -> PalResult<Vec<String>> {
        NativeFsRead.list_dir(path)
    }
}

impl FsOps for NativeFsOps {
    fn atomic_write(&self, path: &Path, data: &[u8], _mode: Option<u32>) -> PalResult<()> {
        let (mut file, temp_path) = create_temp_file(path)?;
        let mut cleanup = TempPath::new(temp_path.clone());
        file.write_all(data).map_err(file_error)?;
        file.sync_all().map_err(file_error)?;
        drop(file);

        replace_with_permission_retries(|| self.replace(&temp_path, path), std::thread::sleep)?;
        cleanup.disarm();
        Ok(())
    }

    fn write_file(&self, path: &Path, data: &[u8]) -> PalResult<()> {
        fs::write(path, data).map_err(file_error)
    }

    fn mkdirs(&self, path: &Path) -> PalResult<()> {
        fs::create_dir_all(path).map_err(file_error)
    }

    fn replace(&self, from: &Path, to: &Path) -> PalResult<()> {
        let from = wide_path(from);
        let to = wide_path(to);
        // SAFETY: 路徑均為 NUL 結尾 UTF-16；MoveFileExW 不保留指標。
        let moved = unsafe {
            MoveFileExW(
                from.as_ptr(),
                to.as_ptr(),
                MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH,
            )
        };
        if moved == 0 {
            Err(last_win32_error())
        } else {
            Ok(())
        }
    }

    fn remove_file(&self, path: &Path) -> PalResult<()> {
        fs::remove_file(path).map_err(file_error)
    }

    fn remove_dir(&self, path: &Path) -> PalResult<()> {
        fs::remove_dir(path).map_err(file_error)
    }

    fn copy_preserving(&self, from: &Path, to: &Path) -> PalResult<()> {
        fs::copy(from, to).map_err(file_error)?;
        copy_file_metadata(from, to)
    }

    fn sync_file(&self, path: &Path) -> PalResult<()> {
        OpenOptions::new()
            .write(true)
            .open(path)
            .and_then(|file| file.sync_all())
            .map_err(file_error)
    }

    fn copy_tree(&self, from: &Path, to: &Path) -> PalResult<()> {
        copy_tree(from, to)
    }

    fn remove_tree(&self, path: &Path) -> PalResult<()> {
        remove_path(path)
    }

    fn read_link(&self, path: &Path) -> PalResult<Option<PathBuf>> {
        match fs::symlink_metadata(path) {
            Ok(metadata) if metadata.file_type().is_symlink() => {
                fs::read_link(path).map(Some).map_err(file_error)
            }
            Ok(_) => Ok(None),
            Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(None),
            Err(error) => Err(file_error(error)),
        }
    }

    fn set_mode(&self, path: &Path, mode: u32) -> PalResult<()> {
        let mut permissions = fs::metadata(path).map_err(file_error)?.permissions();
        permissions.set_readonly(mode & 0o222 == 0);
        fs::set_permissions(path, permissions).map_err(file_error)
    }

    fn sync_dir(&self, _path: &Path) -> PalResult<()> {
        Ok(())
    }

    fn symlink_file(&self, _link: &Path, _target: &Path) -> PalResult<()> {
        Err(PalError::plain(
            PalErrorKind::Unsupported,
            "Windows 不支援 serialwrap 檔案 symlink",
        ))
    }

    fn symlink_or_copy_dir(&self, link: &Path, target: &Path) -> PalResult<LinkKind> {
        fs::create_dir_all(parent_dir(link)).map_err(file_error)?;
        remove_existing(link)?;
        let link_w = wide_path(link);
        let target_w = wide_path(target);
        link_or_copy(link, target, || {
            // SAFETY: 路徑均為 NUL 結尾 UTF-16；旗標啟用目錄 symlink 與免提權建立。
            let created = unsafe {
                CreateSymbolicLinkW(
                    link_w.as_ptr(),
                    target_w.as_ptr(),
                    SYMBOLIC_LINK_FLAG_DIRECTORY | SYMBOLIC_LINK_FLAG_ALLOW_UNPRIVILEGED_CREATE,
                )
            };
            if created {
                Ok(())
            } else {
                Err(unsafe { GetLastError() })
            }
        })
    }

    fn file_lock_exclusive(&self, path: &Path, blocking: bool) -> PalResult<FileLockGuard> {
        let file = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .open(path)
            .map_err(file_error)?;
        let mut overlapped = OVERLAPPED::default();
        let flags = LOCKFILE_EXCLUSIVE_LOCK
            | if blocking {
                0
            } else {
                LOCKFILE_FAIL_IMMEDIATELY
            };
        // SAFETY: OVERLAPPED 與檔案 handle 在同步取得鎖期間有效；鎖定範圍為 offset 0 的 1 byte。
        let locked = unsafe {
            LockFileEx(
                file.as_raw_handle() as HANDLE,
                flags,
                0,
                1,
                0,
                &mut overlapped,
            )
        };
        if locked == 0 {
            let code = unsafe { GetLastError() };
            if code == 33 {
                return Err(PalError::new(
                    PalErrorKind::WouldBlock,
                    Some(11),
                    Some(code as i32),
                    windows_message(code),
                    "[Errno 11] Resource temporarily unavailable",
                ));
            }
            return Err(windows_win32_error(code));
        }
        Ok(FileLockGuard::from_file(file))
    }

    // ---- P7 Task 6：寫入側增補（counter／registry／agent log）----
    fn open_append(&self, p: &Path) -> PalResult<Box<dyn crate::fs_ops::AppendFile>> {
        let file = OpenOptions::new()
            .append(true)
            .create(true)
            .open(p)
            .map_err(|e| crate::fs_ops::with_filename(file_error(e), p))?;
        Ok(Box::new(crate::fs_ops::StdAppendFile {
            file,
            map: file_error,
        }))
    }

    fn create_truncate(&self, p: &Path, bytes: &[u8]) -> PalResult<()> {
        let mut file = OpenOptions::new()
            .write(true)
            .create(true)
            .truncate(true)
            .open(p)
            .map_err(|e| crate::fs_ops::with_filename(file_error(e), p))?;
        file.write_all(bytes).map_err(file_error)
    }

    fn file_size(&self, p: &Path) -> PalResult<Option<u64>> {
        match fs::metadata(p) {
            Ok(meta) => Ok(Some(meta.len())),
            Err(e) if e.kind() == io::ErrorKind::NotFound => Ok(None),
            Err(e) => Err(crate::fs_ops::with_filename(file_error(e), p)),
        }
    }

    fn remove_file_if_exists(&self, p: &Path) -> PalResult<bool> {
        match fs::remove_file(p) {
            Ok(()) => Ok(true),
            Err(e) if e.kind() == io::ErrorKind::NotFound => Ok(false),
            Err(e) => Err(crate::fs_ops::with_filename(file_error(e), p)),
        }
    }
}

impl FsRead for NativeFsRead {
    fn read(&self, path: &Path) -> PalResult<Vec<u8>> {
        if path.is_dir() && !has_trailing_separator(path) {
            return Err(windows_crt_error(13));
        }
        let mut file = File::open(path).map_err(file_error)?;
        let mut data = Vec::new();
        file.read_to_end(&mut data).map_err(file_error)?;
        Ok(data)
    }

    fn exists(&self, path: &Path) -> bool {
        path.exists()
    }

    fn is_dir(&self, path: &Path) -> bool {
        path.is_dir()
    }

    fn is_file(&self, path: &Path) -> bool {
        fs::symlink_metadata(path).is_ok_and(|metadata| metadata.file_type().is_file())
    }

    fn list_dir(&self, path: &Path) -> PalResult<Vec<String>> {
        fs::read_dir(path)
            .map_err(win32_error)?
            .map(|entry| {
                entry
                    .map(|value| value.file_name().to_string_lossy().into_owned())
                    .map_err(win32_error)
            })
            .collect()
    }
}

static TEMP_COUNTER: AtomicU64 = AtomicU64::new(0);

fn create_temp_file(path: &Path) -> PalResult<(File, PathBuf)> {
    let parent = parent_dir(path);
    let name = path
        .file_name()
        .ok_or_else(|| PalError::plain(PalErrorKind::InvalidInput, "原子寫入路徑缺少檔名"))?;
    for _ in 0..128 {
        let mut temp_name = name.to_os_string();
        temp_name.push(format!(
            ".tmp.{}.{}",
            std::process::id(),
            TEMP_COUNTER.fetch_add(1, Ordering::Relaxed)
        ));
        let temp_path = parent.join(temp_name);
        match OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&temp_path)
        {
            Ok(file) => return Ok((file, temp_path)),
            Err(error) if error.kind() == io::ErrorKind::AlreadyExists => continue,
            Err(error) => return Err(file_error(error)),
        }
    }
    Err(PalError::plain(
        PalErrorKind::Io,
        "無法配置唯一的原子寫入暫存檔",
    ))
}

fn parent_dir(path: &Path) -> &Path {
    path.parent()
        .filter(|parent| !parent.as_os_str().is_empty())
        .unwrap_or_else(|| Path::new("."))
}

struct TempPath {
    path: PathBuf,
    armed: bool,
}

impl TempPath {
    fn new(path: PathBuf) -> Self {
        Self { path, armed: true }
    }

    fn disarm(&mut self) {
        self.armed = false;
    }
}

impl Drop for TempPath {
    fn drop(&mut self) {
        if self.armed {
            let _ = fs::remove_file(&self.path);
        }
    }
}

fn copy_tree(from: &Path, to: &Path) -> PalResult<()> {
    match fs::symlink_metadata(to) {
        Ok(_) => {
            return Err(PalError::plain(
                PalErrorKind::InvalidInput,
                "copy_tree 目的路徑已存在",
            ));
        }
        Err(error) if error.kind() == io::ErrorKind::NotFound => {}
        Err(error) => return Err(file_error(error)),
    }
    let metadata = fs::metadata(from).map_err(file_error)?;
    if !metadata.is_dir() {
        return Err(PalError::plain(
            PalErrorKind::InvalidInput,
            "copy_tree 來源不是目錄",
        ));
    }
    fs::create_dir(to).map_err(file_error)?;
    for entry in fs::read_dir(from).map_err(win32_error)? {
        let source = entry.map_err(win32_error)?.path();
        let destination = to.join(source.file_name().expect("目錄項目具有檔名"));
        if source.is_dir() {
            copy_tree(&source, &destination)?;
        } else {
            fs::copy(&source, &destination).map_err(file_error)?;
            copy_file_metadata(&source, &destination)?;
        }
    }
    copy_directory_metadata(from, to)
}

fn replace_with_permission_retries(
    mut replace: impl FnMut() -> PalResult<()>,
    mut sleep: impl FnMut(Duration),
) -> PalResult<()> {
    let mut delay = Duration::from_millis(10);
    for attempt in 0..6 {
        match replace() {
            Ok(()) => return Ok(()),
            Err(error) if error.os_code == Some(13) && attempt < 5 => {
                sleep(delay);
                delay *= 2;
            }
            Err(error) => return Err(error),
        }
    }
    unreachable!("六次 replace 嘗試必須回傳成功或最後一個錯誤")
}

fn copy_file_metadata(from: &Path, to: &Path) -> PalResult<()> {
    let metadata = fs::metadata(from).map_err(file_error)?;
    clear_readonly(to)?;
    let mut times = fs::FileTimes::new();
    if let Ok(accessed) = metadata.accessed() {
        times = times.set_accessed(accessed);
    }
    if let Ok(modified) = metadata.modified() {
        times = times.set_modified(modified);
    }
    OpenOptions::new()
        .write(true)
        .open(to)
        .and_then(|file| file.set_times(times))
        .map_err(file_error)?;
    fs::set_permissions(to, metadata.permissions()).map_err(file_error)
}

fn copy_directory_metadata(from: &Path, to: &Path) -> PalResult<()> {
    let metadata = fs::metadata(from).map_err(file_error)?;
    let mut permissions = fs::metadata(to).map_err(file_error)?.permissions();
    permissions.set_readonly(metadata.permissions().readonly());
    fs::set_permissions(to, permissions).map_err(file_error)
}

fn remove_existing(path: &Path) -> PalResult<()> {
    match fs::symlink_metadata(path) {
        Ok(_) => remove_path(path),
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(()),
        Err(error) => Err(file_error(error)),
    }
}

fn remove_path(path: &Path) -> PalResult<()> {
    let metadata = match fs::symlink_metadata(path) {
        Ok(metadata) => metadata,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(()),
        Err(error) => return Err(file_error(error)),
    };
    let file_type = metadata.file_type();
    if file_type.is_symlink_dir() {
        fs::remove_dir(path).map_err(file_error)
    } else if file_type.is_symlink() || !metadata.is_dir() {
        clear_readonly(path)?;
        fs::remove_file(path).map_err(file_error)
    } else {
        for entry in fs::read_dir(path).map_err(win32_error)? {
            remove_path(&entry.map_err(win32_error)?.path())?;
        }
        clear_readonly(path)?;
        fs::remove_dir(path).map_err(file_error)
    }
}

fn clear_readonly(path: &Path) -> PalResult<()> {
    let wide = wide_path(path);
    // SAFETY: 路徑為 NUL 結尾 UTF-16。
    let attributes = unsafe { GetFileAttributesW(wide.as_ptr()) };
    if attributes == INVALID_FILE_ATTRIBUTES {
        return Err(last_win32_error());
    }
    if attributes & FILE_ATTRIBUTE_READONLY != 0 {
        // SAFETY: 只清除唯讀位元，保留檔案的其他屬性旗標。
        let changed =
            unsafe { SetFileAttributesW(wide.as_ptr(), attributes & !FILE_ATTRIBUTE_READONLY) };
        if changed == 0 {
            return Err(last_win32_error());
        }
    }
    Ok(())
}

fn wide_path(path: &Path) -> Vec<u16> {
    path.as_os_str()
        .encode_wide()
        .chain(std::iter::once(0))
        .collect()
}

fn file_error(error: io::Error) -> PalError {
    let code = error.raw_os_error().unwrap_or(5) as u32;
    windows_crt_error(windows_crt_errno(code))
}

fn win32_error(error: io::Error) -> PalError {
    windows_win32_error(error.raw_os_error().unwrap_or(5) as u32)
}

fn last_win32_error() -> PalError {
    windows_win32_error(unsafe { GetLastError() })
}

fn has_trailing_separator(path: &Path) -> bool {
    path.as_os_str()
        .encode_wide()
        .last()
        .is_some_and(|unit| unit == b'\\' as u16 || unit == b'/' as u16)
}

#[cfg(test)]
mod tests {
    use super::replace_with_permission_retries;
    use crate::traits::{PalError, PalErrorKind};
    use std::time::Duration;

    #[test]
    fn atomic_replace_retries_all_permission_error_win32_codes() {
        let mut attempts = 0;
        let mut delays = Vec::new();
        replace_with_permission_retries(
            || {
                attempts += 1;
                if attempts < 3 {
                    let winerror = if attempts == 1 { 5 } else { 32 };
                    Err(PalError::new(
                        PalErrorKind::PermissionDenied,
                        Some(13),
                        Some(winerror),
                        "busy",
                        "busy",
                    ))
                } else {
                    Ok(())
                }
            },
            |delay| delays.push(delay),
        )
        .unwrap();
        assert_eq!(attempts, 3);
        assert_eq!(
            delays,
            [Duration::from_millis(10), Duration::from_millis(20)]
        );
    }

    #[test]
    fn atomic_replace_stops_after_six_attempts() {
        let mut attempts = 0;
        let mut delays = Vec::new();
        let result = replace_with_permission_retries(
            || {
                attempts += 1;
                Err(PalError::new(
                    PalErrorKind::PermissionDenied,
                    Some(13),
                    Some(32),
                    "busy",
                    "busy",
                ))
            },
            |delay| delays.push(delay),
        );
        assert!(result.is_err());
        assert_eq!(attempts, 6);
        assert_eq!(delays.len(), 5);
        assert_eq!(delays[0], Duration::from_millis(10));
        assert_eq!(delays[4], Duration::from_millis(160));
    }
}
