//! POSIX 檔案讀寫、連結與跨行程鎖。

use crate::fs_ops::{FileLockGuard, FsOps, LinkKind};
use crate::traits::{posix_io_error, FsRead, PalError, PalErrorKind, PalResult};
use std::ffi::{CStr, CString};
use std::fs::{self, File, OpenOptions};
use std::io::{self, Write};
use std::os::fd::{AsRawFd, FromRawFd};
use std::os::unix::ffi::{OsStrExt, OsStringExt};
use std::os::unix::fs::{symlink, OpenOptionsExt, PermissionsExt};
use std::path::{Path, PathBuf};

#[derive(Clone, Copy, Debug, Default)]
pub struct NativeFsRead;

/// POSIX 寫入面與跨行程鎖。
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
    fn atomic_write(&self, p: &Path, data: &[u8], mode: Option<u32>) -> PalResult<()> {
        let parent = parent_dir(p);
        let (mut file, temp_path) = create_temp_file(p)?;
        let mut cleanup = TempPath::new(temp_path.clone());
        let requested_mode = mode.unwrap_or(0o600);
        file.set_permissions(fs::Permissions::from_mode(requested_mode))
            .map_err(posix_io_error)?;
        file.write_all(data).map_err(posix_io_error)?;
        file.sync_all().map_err(posix_io_error)?;
        drop(file);
        fs::rename(&temp_path, p).map_err(posix_io_error)?;
        cleanup.disarm();
        self.sync_dir(parent)
    }

    fn write_file(&self, p: &Path, data: &[u8]) -> PalResult<()> {
        fs::write(p, data).map_err(posix_io_error)
    }

    fn mkdirs(&self, p: &Path) -> PalResult<()> {
        fs::create_dir_all(p).map_err(posix_io_error)
    }

    fn replace(&self, from: &Path, to: &Path) -> PalResult<()> {
        fs::rename(from, to).map_err(posix_io_error)
    }

    fn remove_file(&self, p: &Path) -> PalResult<()> {
        fs::remove_file(p).map_err(posix_io_error)
    }

    fn copy_preserving(&self, from: &Path, to: &Path) -> PalResult<()> {
        fs::copy(from, to).map_err(posix_io_error)?;
        copy_file_metadata(from, to)
    }

    fn copy_tree(&self, from: &Path, to: &Path) -> PalResult<()> {
        copy_tree(from, to)
    }

    fn remove_tree(&self, p: &Path) -> PalResult<()> {
        remove_path(p)
    }

    fn read_link(&self, p: &Path) -> PalResult<Option<PathBuf>> {
        match fs::symlink_metadata(p) {
            Ok(metadata) if metadata.file_type().is_symlink() => {
                fs::read_link(p).map(Some).map_err(posix_io_error)
            }
            Ok(_) => Ok(None),
            Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(None),
            Err(error) => Err(posix_io_error(error)),
        }
    }

    fn set_mode(&self, p: &Path, mode: u32) -> PalResult<()> {
        fs::set_permissions(p, fs::Permissions::from_mode(mode)).map_err(posix_io_error)
    }

    fn sync_dir(&self, p: &Path) -> PalResult<()> {
        File::open(p)
            .and_then(|directory| directory.sync_all())
            .map_err(posix_io_error)
    }

    fn symlink_file(&self, link: &Path, target: &Path) -> PalResult<()> {
        remove_existing(link)?;
        symlink(target, link).map_err(posix_io_error)
    }

    fn symlink_or_copy_dir(&self, link: &Path, target: &Path) -> PalResult<LinkKind> {
        fs::create_dir_all(parent_dir(link)).map_err(posix_io_error)?;
        remove_existing(link)?;
        symlink(target, link)
            .map(|()| LinkKind::Symlink)
            .map_err(posix_io_error)
    }

    fn file_lock_exclusive(&self, p: &Path, blocking: bool) -> PalResult<FileLockGuard> {
        let file = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .mode(0o600)
            .open(p)
            .map_err(posix_io_error)?;
        let mut operation = libc::LOCK_EX;
        if !blocking {
            operation |= libc::LOCK_NB;
        }
        loop {
            // SAFETY: fd 由仍存活的 `file` 持有；flock 對此開啟檔案描述符設定獨占鎖。
            if unsafe { libc::flock(file.as_raw_fd(), operation) } == 0 {
                return Ok(FileLockGuard::from_file(file));
            }
            let error = io::Error::last_os_error();
            if error.kind() == io::ErrorKind::Interrupted {
                continue;
            }
            return Err(posix_io_error(error));
        }
    }
}

fn parent_dir(path: &Path) -> &Path {
    path.parent()
        .filter(|parent| !parent.as_os_str().is_empty())
        .unwrap_or_else(|| Path::new("."))
}

fn create_temp_file(path: &Path) -> PalResult<(File, PathBuf)> {
    let parent = parent_dir(path);
    let file_name = path
        .file_name()
        .ok_or_else(|| PalError::plain(PalErrorKind::InvalidInput, "原子寫入路徑缺少檔名"))?;
    let mut leaf = file_name.as_bytes().to_vec();
    leaf.extend_from_slice(b".tmp.XXXXXX");
    let mut template = parent.as_os_str().as_bytes().to_vec();
    if !template.ends_with(b"/") {
        template.push(b'/');
    }
    template.extend_from_slice(&leaf);
    let mut template = CString::new(template)
        .map_err(|_| PalError::plain(PalErrorKind::InvalidInput, "原子寫入路徑含 NUL"))?
        .into_bytes_with_nul();
    // SAFETY: 建立函式會就地改寫可寫且 NUL 結尾的 template，並建立唯一新檔案。
    #[cfg(target_os = "linux")]
    let fd = unsafe { libc::mkostemp(template.as_mut_ptr().cast(), libc::O_CLOEXEC) };
    #[cfg(not(target_os = "linux"))]
    let fd = unsafe { libc::mkstemp(template.as_mut_ptr().cast()) };
    if fd < 0 {
        return Err(posix_io_error(io::Error::last_os_error()));
    }
    // SAFETY: mkstemp 成功後，此處接管唯一回傳的 fd。
    let file = unsafe { File::from_raw_fd(fd) };
    let temp_path = PathBuf::from(std::ffi::OsString::from_vec(
        CStr::from_bytes_with_nul(&template)
            .expect("mkstemp template 維持 NUL 結尾")
            .to_bytes()
            .to_vec(),
    ));
    #[cfg(not(target_os = "linux"))]
    if let Err(error) = set_cloexec(file.as_raw_fd()) {
        drop(file);
        let _ = fs::remove_file(&temp_path);
        return Err(posix_io_error(error));
    }
    Ok((file, temp_path))
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

fn remove_existing(path: &Path) -> PalResult<()> {
    match fs::symlink_metadata(path) {
        Ok(metadata) if metadata.file_type().is_symlink() || !metadata.is_dir() => {
            fs::remove_file(path).map_err(posix_io_error)
        }
        Ok(_) => remove_path(path),
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(()),
        Err(error) => Err(posix_io_error(error)),
    }
}

fn remove_path(path: &Path) -> PalResult<()> {
    let metadata = match fs::symlink_metadata(path) {
        Ok(metadata) => metadata,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(()),
        Err(error) => return Err(posix_io_error(error)),
    };
    if metadata.file_type().is_symlink() || !metadata.is_dir() {
        fs::remove_file(path).map_err(posix_io_error)
    } else {
        for entry in fs::read_dir(path).map_err(posix_io_error)? {
            remove_path(&entry.map_err(posix_io_error)?.path())?;
        }
        fs::remove_dir(path).map_err(posix_io_error)
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
        Err(error) => return Err(posix_io_error(error)),
    }
    let metadata = fs::metadata(from).map_err(posix_io_error)?;
    if !metadata.is_dir() {
        return Err(PalError::plain(
            PalErrorKind::InvalidInput,
            "copy_tree 來源不是目錄",
        ));
    }
    fs::create_dir(to).map_err(posix_io_error)?;
    for entry in fs::read_dir(from).map_err(posix_io_error)? {
        let source = entry.map_err(posix_io_error)?.path();
        let destination = to.join(source.file_name().expect("目錄項目具有檔名"));
        if source.is_dir() {
            copy_tree(&source, &destination)?;
        } else {
            fs::copy(&source, &destination).map_err(posix_io_error)?;
            copy_file_metadata(&source, &destination)?;
        }
    }
    fs::set_permissions(to, metadata.permissions()).map_err(posix_io_error)
}

fn copy_file_metadata(from: &Path, to: &Path) -> PalResult<()> {
    let metadata = fs::metadata(from).map_err(posix_io_error)?;
    let mut times = fs::FileTimes::new();
    if let Ok(accessed) = metadata.accessed() {
        times = times.set_accessed(accessed);
    }
    if let Ok(modified) = metadata.modified() {
        times = times.set_modified(modified);
    }
    OpenOptions::new()
        .read(true)
        .open(to)
        .and_then(|file| file.set_times(times))
        .map_err(posix_io_error)?;
    fs::set_permissions(to, metadata.permissions()).map_err(posix_io_error)
}

#[cfg(not(target_os = "linux"))]
fn set_cloexec(fd: std::os::fd::RawFd) -> io::Result<()> {
    // SAFETY: fd 由目前 File 持有；F_GETFD 與 F_SETFD 僅修改此 fd 的描述符旗標。
    let flags = unsafe { libc::fcntl(fd, libc::F_GETFD) };
    if flags == -1 {
        return Err(io::Error::last_os_error());
    }
    // SAFETY: fd 仍由 File 持有，僅加上 FD_CLOEXEC。
    if unsafe { libc::fcntl(fd, libc::F_SETFD, flags | libc::FD_CLOEXEC) } == -1 {
        return Err(io::Error::last_os_error());
    }
    Ok(())
}

impl FsRead for NativeFsRead {
    fn read(&self, path: &Path) -> PalResult<Vec<u8>> {
        fs::read(path).map_err(posix_io_error)
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
            .map_err(posix_io_error)?
            .map(|entry| {
                entry
                    .map(|value| value.file_name().to_string_lossy().into_owned())
                    .map_err(posix_io_error)
            })
            .collect()
    }
}

#[cfg(test)]
mod tests {
    use super::create_temp_file;
    use std::os::fd::AsRawFd;

    #[test]
    fn atomic_write_temp_file_is_close_on_exec() {
        let tmp = tempfile::tempdir().unwrap();
        let target = tmp.path().join("state.json");
        let (file, temp_path) = create_temp_file(&target).unwrap();
        // SAFETY: fd 由仍存活的 File 持有；F_GETFD 僅查詢描述符旗標。
        let flags = unsafe { libc::fcntl(file.as_raw_fd(), libc::F_GETFD) };
        assert_ne!(flags & libc::FD_CLOEXEC, 0, "tmp fd 必須設為 close-on-exec");
        drop(file);
        std::fs::remove_file(temp_path).unwrap();
    }
}
