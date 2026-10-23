//! POSIX WAL 常駐 handle 與不覆蓋歸檔操作。

use std::ffi::CString;
use std::fs::{self, File, OpenOptions};
use std::io::{self, Read, Seek, SeekFrom, Write};
use std::os::fd::{FromRawFd, IntoRawFd};
use std::os::unix::ffi::OsStrExt;
use std::os::unix::fs::MetadataExt;
use std::path::{Path, PathBuf};

use crate::traits::{posix_io_error, PalError, PalResult};
use crate::wal_fs::{FileIdentity, WalFile, WalFs, WalWriteError};

/// POSIX WAL 檔案系統。
pub struct PosixWalFs;

struct PosixWalFile {
    file: Option<File>,
}

impl PosixWalFile {
    fn file(&self) -> &File {
        self.file.as_ref().expect("WAL handle 尚未關閉")
    }
}

impl WalFile for PosixWalFile {
    fn write_all_flush(&mut self, bytes: &[u8]) -> Result<(), WalWriteError> {
        let mut sent = 0;
        while sent < bytes.len() {
            match self.file().write(&bytes[sent..]) {
                Ok(0) => {
                    return Err(WalWriteError {
                        sent,
                        error: PalError::from_errno(libc::EIO),
                    });
                }
                Ok(count) => sent += count,
                Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
                Err(error) => {
                    return Err(WalWriteError {
                        sent,
                        error: posix_io_error(error),
                    });
                }
            }
        }
        Ok(())
    }

    fn sync(&mut self) -> PalResult<()> {
        self.file().sync_all().map_err(posix_io_error)
    }

    fn identity(&self) -> PalResult<FileIdentity> {
        let metadata = self.file().metadata().map_err(posix_io_error)?;
        Ok(FileIdentity(metadata.dev() as u128, metadata.ino() as u128))
    }

    fn unlinked(&self) -> PalResult<bool> {
        let metadata = self.file().metadata().map_err(posix_io_error)?;
        Ok(metadata.nlink() == 0)
    }

    fn close(mut self: Box<Self>) -> PalResult<()> {
        let file = self.file.take().expect("WAL handle 尚未關閉");
        let fd = file.into_raw_fd();
        // 安全性：into_raw_fd 已移交唯一 fd 所有權；close 只呼叫一次，避免 EINTR 後重試誤關重用的 fd。
        if unsafe { libc::close(fd) } == 0 {
            Ok(())
        } else {
            Err(posix_io_error(io::Error::last_os_error()))
        }
    }
}

impl WalFs for PosixWalFs {
    fn open_append(&self, path: &Path) -> PalResult<Box<dyn WalFile>> {
        let path = path_cstring(path)?;
        // 安全性：路徑 NUL 結尾且呼叫期間有效；open 成功後 fd 所有權立即交給 File。
        let fd = unsafe {
            libc::open(
                path.as_ptr(),
                libc::O_WRONLY | libc::O_APPEND | libc::O_CREAT | libc::O_CLOEXEC,
                0o666,
            )
        };
        if fd < 0 {
            return Err(posix_io_error(io::Error::last_os_error()));
        }
        // 安全性：fd 是剛取得且尚未交給其他物件的有效描述符。
        let file = unsafe { File::from_raw_fd(fd) };
        Ok(Box::new(PosixWalFile { file: Some(file) }))
    }

    fn path_identity(&self, path: &Path) -> PalResult<Option<FileIdentity>> {
        match fs::metadata(path) {
            Ok(metadata) => Ok(Some(FileIdentity(
                metadata.dev() as u128,
                metadata.ino() as u128,
            ))),
            Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(None),
            Err(error) => Err(posix_io_error(error)),
        }
    }

    fn size(&self, path: &Path) -> PalResult<Option<u64>> {
        match fs::metadata(path) {
            Ok(metadata) => Ok(Some(metadata.len())),
            Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(None),
            Err(error) => Err(posix_io_error(error)),
        }
    }

    fn tail_needs_termination(&self, path: &Path) -> PalResult<bool> {
        let mut file = File::open(path).map_err(posix_io_error)?;
        let size = file.metadata().map_err(posix_io_error)?.len();
        if size == 0 {
            return Ok(false);
        }
        file.seek(SeekFrom::End(-1)).map_err(posix_io_error)?;
        let mut byte = [0u8; 1];
        file.read_exact(&mut byte).map_err(posix_io_error)?;
        Ok(byte[0] != b'\n')
    }

    fn read_all(&self, path: &Path) -> PalResult<Option<Vec<u8>>> {
        match fs::read(path) {
            Ok(bytes) => Ok(Some(bytes)),
            Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(None),
            Err(error) => Err(posix_io_error(error)),
        }
    }

    fn rename_noreplace_or_suffix(&self, from: &Path, base_to: &Path) -> PalResult<PathBuf> {
        for suffix in 0..1000 {
            let to = suffixed_path(base_to, suffix);
            match rename_noreplace(from, &to) {
                Ok(()) => return Ok(to),
                Err(error) if error.raw_os_error() == Some(libc::EEXIST) => continue,
                Err(error) => return Err(posix_io_error(error)),
            }
        }
        Err(PalError::from_errno(libc::EEXIST))
    }

    fn sync_dir(&self, dir: &Path) -> PalResult<()> {
        OpenOptions::new()
            .read(true)
            .open(dir)
            .and_then(|file| file.sync_all())
            .map_err(posix_io_error)
    }

    fn ensure_dir(&self, dir: &Path) -> PalResult<()> {
        fs::create_dir_all(dir).map_err(posix_io_error)
    }

    fn dir_writable(&self, dir: &Path) -> bool {
        if !fs::metadata(dir).is_ok_and(|metadata| metadata.is_dir()) {
            return false;
        }
        let Ok(path) = path_cstring(dir) else {
            return false;
        };
        // 安全性：路徑 NUL 結尾且呼叫期間有效；access 不保留指標。
        unsafe { libc::access(path.as_ptr(), libc::W_OK | libc::X_OK) == 0 }
    }
}

fn path_cstring(path: &Path) -> PalResult<CString> {
    CString::new(path.as_os_str().as_bytes()).map_err(|_| PalError::invalid("path contains NUL"))
}

fn suffixed_path(base: &Path, suffix: usize) -> PathBuf {
    if suffix == 0 {
        return base.to_path_buf();
    }
    let mut value = base.as_os_str().to_os_string();
    value.push(format!(".{suffix}"));
    PathBuf::from(value)
}

fn rename_noreplace(from: &Path, to: &Path) -> io::Result<()> {
    let from = CString::new(from.as_os_str().as_bytes())
        .map_err(|_| io::Error::from_raw_os_error(libc::EINVAL))?;
    let to = CString::new(to.as_os_str().as_bytes())
        .map_err(|_| io::Error::from_raw_os_error(libc::EINVAL))?;
    rename_noreplace_with(&mut NativeRenameOps, &from, &to)
}

trait RenameOps {
    fn atomic_noreplace(&mut self, from: &std::ffi::CStr, to: &std::ffi::CStr) -> io::Result<()>;
    fn link(&mut self, from: &std::ffi::CStr, to: &std::ffi::CStr) -> io::Result<()>;
    fn unlink(&mut self, path: &std::ffi::CStr) -> io::Result<()>;
    fn destination_exists(&mut self, to: &std::ffi::CStr) -> bool;
    fn rename(&mut self, from: &std::ffi::CStr, to: &std::ffi::CStr) -> io::Result<()>;
    fn same_file(&mut self, from: &std::ffi::CStr, to: &std::ffi::CStr) -> bool;
}

struct NativeRenameOps;

impl RenameOps for NativeRenameOps {
    fn atomic_noreplace(&mut self, from: &std::ffi::CStr, to: &std::ffi::CStr) -> io::Result<()> {
        #[cfg(target_os = "linux")]
        {
            // 安全性：兩個 NUL 結尾路徑在 syscall 期間有效；AT_FDCWD 使用呼叫端目前目錄。
            if unsafe {
                libc::renameat2(
                    libc::AT_FDCWD,
                    from.as_ptr(),
                    libc::AT_FDCWD,
                    to.as_ptr(),
                    libc::RENAME_NOREPLACE,
                )
            } == 0
            {
                return Ok(());
            }
            return Err(io::Error::last_os_error());
        }
        #[cfg(not(target_os = "linux"))]
        {
            let _ = (from, to);
            Err(io::Error::from_raw_os_error(libc::ENOSYS))
        }
    }

    fn link(&mut self, from: &std::ffi::CStr, to: &std::ffi::CStr) -> io::Result<()> {
        // 安全性：兩個 NUL 結尾路徑在呼叫期間有效；link 不會取代既有目的名稱。
        if unsafe { libc::link(from.as_ptr(), to.as_ptr()) } == 0 {
            Ok(())
        } else {
            Err(io::Error::last_os_error())
        }
    }

    fn unlink(&mut self, path: &std::ffi::CStr) -> io::Result<()> {
        // 安全性：路徑在 syscall 期間保持有效。
        if unsafe { libc::unlink(path.as_ptr()) } == 0 {
            Ok(())
        } else {
            Err(io::Error::last_os_error())
        }
    }

    fn destination_exists(&mut self, to: &std::ffi::CStr) -> bool {
        let path = Path::new(std::ffi::OsStr::from_bytes(to.to_bytes()));
        fs::symlink_metadata(path).is_ok()
    }

    fn rename(&mut self, from: &std::ffi::CStr, to: &std::ffi::CStr) -> io::Result<()> {
        // 安全性：路徑在 syscall 期間保持有效；此相容降級與 Python 基線一致。
        if unsafe { libc::rename(from.as_ptr(), to.as_ptr()) } == 0 {
            Ok(())
        } else {
            Err(io::Error::last_os_error())
        }
    }

    fn same_file(&mut self, from: &std::ffi::CStr, to: &std::ffi::CStr) -> bool {
        let source = fs::metadata(std::ffi::OsStr::from_bytes(from.to_bytes()));
        let destination = fs::metadata(std::ffi::OsStr::from_bytes(to.to_bytes()));
        match (source, destination) {
            (Ok(source), Ok(destination)) => {
                source.dev() == destination.dev() && source.ino() == destination.ino()
            }
            _ => false,
        }
    }
}

fn rename_noreplace_with(
    ops: &mut impl RenameOps,
    from: &std::ffi::CStr,
    to: &std::ffi::CStr,
) -> io::Result<()> {
    match ops.atomic_noreplace(from, to) {
        Ok(()) => return Ok(()),
        Err(error)
            if [
                Some(libc::EINVAL),
                Some(libc::ENOSYS),
                Some(libc::EOPNOTSUPP),
                Some(libc::ENOTSUP),
            ]
            .contains(&error.raw_os_error()) => {}
        Err(error) => return Err(error),
    }

    if let Err(error) = ops.link(from, to) {
        if error.raw_os_error() == Some(libc::EEXIST) {
            return Err(error);
        }
        if ops.destination_exists(to) {
            return Err(io::Error::from_raw_os_error(libc::EEXIST));
        }
        return ops.rename(from, to);
    }
    if let Err(error) = ops.unlink(from) {
        if ops.same_file(from, to) {
            // 安全性：只有目的名稱仍指向此操作建立的 inode 才回滾硬連結。
            let _ = ops.unlink(to);
        }
        return Err(error);
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use std::ffi::{CStr, CString};
    use std::io;

    use super::{rename_noreplace_with, RenameOps};

    struct FakeRenameOps {
        atomic: Result<(), i32>,
        link: Result<(), i32>,
        source_unlink: Result<(), i32>,
        destination_exists: bool,
        same_file: bool,
        calls: Vec<String>,
    }

    impl RenameOps for FakeRenameOps {
        fn atomic_noreplace(&mut self, _from: &CStr, _to: &CStr) -> io::Result<()> {
            self.calls.push("atomic".to_string());
            self.atomic.map_err(io::Error::from_raw_os_error)
        }

        fn link(&mut self, _from: &CStr, _to: &CStr) -> io::Result<()> {
            self.calls.push("link".to_string());
            self.link.map_err(io::Error::from_raw_os_error)
        }

        fn unlink(&mut self, _path: &CStr) -> io::Result<()> {
            if !self.calls.iter().any(|call| call == "unlink-source") {
                self.calls.push("unlink-source".to_string());
                self.source_unlink.map_err(io::Error::from_raw_os_error)
            } else {
                self.calls.push("rollback-destination".to_string());
                Ok(())
            }
        }

        fn destination_exists(&mut self, _to: &CStr) -> bool {
            self.calls.push("destination-exists".to_string());
            self.destination_exists
        }

        fn rename(&mut self, _from: &CStr, _to: &CStr) -> io::Result<()> {
            self.calls.push("rename".to_string());
            Ok(())
        }

        fn same_file(&mut self, _from: &CStr, _to: &CStr) -> bool {
            self.calls.push("same-file".to_string());
            self.same_file
        }
    }

    fn paths() -> (CString, CString) {
        (CString::new("from").unwrap(), CString::new("to").unwrap())
    }

    fn fallback_ops(link: Result<(), i32>) -> FakeRenameOps {
        FakeRenameOps {
            atomic: Err(libc::EINVAL),
            link,
            source_unlink: Ok(()),
            destination_exists: false,
            same_file: false,
            calls: Vec::new(),
        }
    }

    #[test]
    fn atomic_einval_falls_back_to_hard_link_then_unlink() {
        let (from, to) = paths();
        let mut ops = fallback_ops(Ok(()));

        rename_noreplace_with(&mut ops, &from, &to).unwrap();

        assert_eq!(ops.calls, ["atomic", "link", "unlink-source"]);
    }

    #[test]
    fn link_eperm_with_absent_destination_falls_back_to_rename() {
        let (from, to) = paths();
        let mut ops = fallback_ops(Err(libc::EPERM));

        rename_noreplace_with(&mut ops, &from, &to).unwrap();

        assert_eq!(
            ops.calls,
            ["atomic", "link", "destination-exists", "rename"]
        );
    }

    #[test]
    fn link_eperm_with_existing_destination_returns_exists_for_suffix() {
        let (from, to) = paths();
        let mut ops = fallback_ops(Err(libc::EPERM));
        ops.destination_exists = true;

        let error = rename_noreplace_with(&mut ops, &from, &to).unwrap_err();

        assert_eq!(error.raw_os_error(), Some(libc::EEXIST));
        assert_eq!(ops.calls, ["atomic", "link", "destination-exists"]);
    }

    #[test]
    fn source_unlink_failure_rolls_back_the_created_destination_link() {
        let (from, to) = paths();
        let mut ops = fallback_ops(Ok(()));
        ops.source_unlink = Err(libc::EACCES);
        ops.same_file = true;

        let error = rename_noreplace_with(&mut ops, &from, &to).unwrap_err();

        assert_eq!(error.raw_os_error(), Some(libc::EACCES));
        assert_eq!(
            ops.calls,
            [
                "atomic",
                "link",
                "unlink-source",
                "same-file",
                "rollback-destination"
            ]
        );
    }
}
