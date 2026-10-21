//! 真實檔案系統替身（feature `test-fakes`；只供 tempdir 內的測試使用，不是產品路徑）。
//!
//! 所有 `FsRead`／`FsOps` 方法皆以 `std::fs` 實作（測試用，非原子保證、非產品鎖語意）；跨行程鎖以 `flock`（Unix）
//! 取得，Windows 測試替身沿用 P4-T5 原生 `LockFileEx`。產品路徑請勿引用本模組。

use std::path::{Path, PathBuf};
use std::sync::Arc;

use crate::fs_ops::{FileLockGuard, FsOps, LinkKind};
use crate::traits::{FsRead, PalError, PalErrorKind, PalResult};

#[derive(Debug, Default)]
pub struct RealFs;

fn io_error(e: std::io::Error) -> PalError {
    // 保留「不存在」分類：Windows 的 ERROR_PATH_NOT_FOUND（os error 3）訊息不含 POSIX 字樣，只靠字串判斷會誤判。
    let kind = if e.kind() == std::io::ErrorKind::NotFound {
        PalErrorKind::NotFound
    } else {
        PalErrorKind::Io
    };
    PalError::plain(kind, e.to_string())
}

fn is_real_dir(p: &Path) -> bool {
    std::fs::symlink_metadata(p)
        .map(|m| m.is_dir())
        .unwrap_or(false)
}

fn is_symlink(p: &Path) -> bool {
    std::fs::symlink_metadata(p)
        .map(|m| m.file_type().is_symlink())
        .unwrap_or(false)
}

#[cfg(windows)]
fn is_symlink_dir(p: &Path) -> bool {
    use std::os::windows::fs::FileTypeExt;
    std::fs::symlink_metadata(p)
        .map(|metadata| metadata.file_type().is_symlink_dir())
        .unwrap_or(false)
}

#[cfg(not(windows))]
fn is_symlink_dir(_p: &Path) -> bool {
    false
}

/// 遞迴複製目錄樹；來源 symlink 依其目標內容複製，與 native `copy_tree` 一致。
fn copy_dir_recursive(from: &Path, to: &Path) -> std::io::Result<()> {
    std::fs::create_dir(to)?;
    for entry in std::fs::read_dir(from)? {
        let entry = entry?;
        let src = entry.path();
        let dst = to.join(entry.file_name());
        if src.is_dir() {
            copy_dir_recursive(&src, &dst)?;
        } else {
            std::fs::copy(&src, &dst)?;
        }
    }
    Ok(())
}

#[cfg(unix)]
fn make_symlink(target: &Path, link: &Path, _is_dir: bool) -> std::io::Result<()> {
    std::os::unix::fs::symlink(target, link)
}

#[cfg(windows)]
fn make_symlink(target: &Path, link: &Path, is_dir: bool) -> std::io::Result<()> {
    if is_dir {
        std::os::windows::fs::symlink_dir(target, link)
    } else {
        std::os::windows::fs::symlink_file(target, link)
    }
}

impl FsRead for RealFs {
    fn read(&self, p: &Path) -> PalResult<Vec<u8>> {
        std::fs::read(p).map_err(io_error)
    }
    fn exists(&self, p: &Path) -> bool {
        p.exists()
    }
    fn is_dir(&self, p: &Path) -> bool {
        p.is_dir()
    }
    fn is_file(&self, p: &Path) -> bool {
        std::fs::symlink_metadata(p).is_ok_and(|metadata| metadata.file_type().is_file())
    }
    fn list_dir(&self, p: &Path) -> PalResult<Vec<String>> {
        let mut out = Vec::new();
        for entry in std::fs::read_dir(p).map_err(io_error)? {
            out.push(
                entry
                    .map_err(io_error)?
                    .file_name()
                    .to_string_lossy()
                    .into_owned(),
            );
        }
        Ok(out)
    }
}

impl FsOps for RealFs {
    fn atomic_write(&self, p: &Path, data: &[u8], mode: Option<u32>) -> PalResult<()> {
        // 測試用：同目錄 tmp＋rename（不做 fsync）；mode 只對 Unix 生效。
        let tmp: PathBuf = {
            let mut name = p.file_name().map(|n| n.to_os_string()).unwrap_or_default();
            name.push(".tmp-fake");
            p.with_file_name(name)
        };
        std::fs::write(&tmp, data).map_err(io_error)?;
        #[cfg(unix)]
        if let Some(m) = mode {
            use std::os::unix::fs::PermissionsExt;
            std::fs::set_permissions(&tmp, std::fs::Permissions::from_mode(m)).map_err(io_error)?;
        }
        #[cfg(not(unix))]
        let _ = mode;
        std::fs::rename(&tmp, p).map_err(io_error)
    }
    fn write_file(&self, p: &Path, data: &[u8]) -> PalResult<()> {
        std::fs::write(p, data).map_err(io_error)
    }
    fn mkdirs(&self, p: &Path) -> PalResult<()> {
        std::fs::create_dir_all(p).map_err(io_error)
    }
    fn replace(&self, from: &Path, to: &Path) -> PalResult<()> {
        std::fs::rename(from, to).map_err(io_error)
    }
    fn remove_file(&self, p: &Path) -> PalResult<()> {
        std::fs::remove_file(p).map_err(io_error)
    }
    fn copy_preserving(&self, from: &Path, to: &Path) -> PalResult<()> {
        std::fs::copy(from, to).map(|_| ()).map_err(io_error)
    }
    fn sync_file(&self, path: &Path) -> PalResult<()> {
        std::fs::OpenOptions::new()
            .write(true)
            .open(path)
            .and_then(|file| file.sync_all())
            .map_err(io_error)
    }
    fn copy_tree(&self, from: &Path, to: &Path) -> PalResult<()> {
        match std::fs::symlink_metadata(to) {
            Ok(_) => {
                return Err(PalError::plain(
                    PalErrorKind::InvalidInput,
                    "copy_tree 目的路徑已存在",
                ));
            }
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
            Err(error) => return Err(io_error(error)),
        }
        if !std::fs::metadata(from).map_err(io_error)?.is_dir() {
            return Err(PalError::plain(
                PalErrorKind::InvalidInput,
                "copy_tree 來源不是目錄",
            ));
        }
        copy_dir_recursive(from, to).map_err(io_error)
    }
    fn remove_tree(&self, p: &Path) -> PalResult<()> {
        if is_symlink_dir(p) {
            std::fs::remove_dir(p).map_err(io_error)
        } else if is_real_dir(p) {
            std::fs::remove_dir_all(p).map_err(io_error)
        } else if std::fs::symlink_metadata(p).is_ok() {
            // symlink（含指向目錄者）與一般檔：僅移除連結本身，不跟隨。
            std::fs::remove_file(p).map_err(io_error)
        } else {
            Ok(())
        }
    }
    fn read_link(&self, p: &Path) -> PalResult<Option<PathBuf>> {
        if is_symlink(p) {
            std::fs::read_link(p).map(Some).map_err(io_error)
        } else {
            Ok(None)
        }
    }
    fn set_mode(&self, p: &Path, mode: u32) -> PalResult<()> {
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            std::fs::set_permissions(p, std::fs::Permissions::from_mode(mode)).map_err(io_error)
        }
        #[cfg(not(unix))]
        {
            let _ = (p, mode);
            Ok(())
        }
    }
    fn sync_dir(&self, p: &Path) -> PalResult<()> {
        #[cfg(unix)]
        {
            std::fs::File::open(p)
                .and_then(|f| f.sync_all())
                .map_err(io_error)
        }
        #[cfg(not(unix))]
        {
            let _ = p;
            Ok(())
        }
    }
    fn symlink_file(&self, link: &Path, target: &Path) -> PalResult<()> {
        make_symlink(target, link, false).map_err(io_error)
    }
    fn symlink_or_copy_dir(&self, link: &Path, target: &Path) -> PalResult<LinkKind> {
        match make_symlink(target, link, true) {
            Ok(()) => Ok(LinkKind::Symlink),
            Err(_) => {
                // 權限不足（Windows 非開發者模式）→ 遞迴複製（DF-16）。
                copy_dir_recursive(target, link).map_err(io_error)?;
                Ok(LinkKind::Copied)
            }
        }
    }
    fn file_lock_exclusive(&self, p: &Path, blocking: bool) -> PalResult<FileLockGuard> {
        #[cfg(unix)]
        {
            use std::os::unix::io::AsRawFd;
            let file = std::fs::OpenOptions::new()
                .create(true)
                .read(true)
                .write(true)
                .open(p)
                .map_err(io_error)?;
            let op = libc::LOCK_EX | if blocking { 0 } else { libc::LOCK_NB };
            // SAFETY：fd 由上方 `file` 持有且在本呼叫期間有效；flock 不取得記憶體所有權。
            let rc = unsafe { libc::flock(file.as_raw_fd(), op) };
            if rc != 0 {
                let err = std::io::Error::last_os_error();
                if !blocking && err.raw_os_error() == Some(libc::EWOULDBLOCK) {
                    return Err(PalError::plain(
                        PalErrorKind::WouldBlock,
                        "file lock held by another process",
                    ));
                }
                return Err(io_error(err));
            }
            Ok(FileLockGuard::from_file(file))
        }
        #[cfg(windows)]
        {
            // Windows 測試替身直接沿用 P4-T5 原生 `LockFileEx`，使跨行程鎖語意與產品一致。
            crate::fs_ops::windows::NativeFsOps.file_lock_exclusive(p, blocking)
        }
        #[cfg(not(any(unix, windows)))]
        {
            let _ = (p, blocking);
            Err(PalError::plain(
                PalErrorKind::Unsupported,
                "test fake file lock is unsupported on this platform",
            ))
        }
    }
}

/// 真實檔案系統（測試用）。
pub fn real_fs() -> Arc<dyn FsOps> {
    Arc::new(RealFs)
}

/// 讀取檔案權限位（`mode & 0o7777`）；平台無 POSIX 權限位（Windows）時回 `None`。
/// 供 `crates/sw-pal` 以外的測試在不寫平台 `cfg` 的前提下斷言 mode（arch §2.1 cfg 邊界）。
pub fn file_mode(p: &Path) -> Option<u32> {
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::metadata(p)
            .ok()
            .map(|m| m.permissions().mode() & 0o7777)
    }
    #[cfg(not(unix))]
    {
        let _ = p;
        None
    }
}
