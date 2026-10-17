//! POSIX 唯讀檔案操作。

use crate::fs_ops::{FileLockGuard, FsOps, LinkKind};
use crate::traits::{posix_io_error, FsRead, PalResult};
use std::fs;
use std::path::Path;

#[derive(Clone, Copy, Debug, Default)]
pub struct NativeFsRead;

/// POSIX 寫入面與跨行程鎖（P4-T5 承接；本階段為紅燈骨架）。
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

    fn list_dir(&self, path: &Path) -> PalResult<Vec<String>> {
        NativeFsRead.list_dir(path)
    }
}

impl FsOps for NativeFsOps {
    fn copy_tree(&self, _from: &Path, _to: &Path) -> PalResult<()> {
        unimplemented!("P4-T5")
    }

    fn remove_tree(&self, _p: &Path) -> PalResult<()> {
        unimplemented!("P4-T5")
    }

    fn symlink_or_copy_dir(&self, _link: &Path, _target: &Path) -> PalResult<LinkKind> {
        unimplemented!("P4-T5")
    }

    fn file_lock_exclusive(&self, _p: &Path, _blocking: bool) -> PalResult<FileLockGuard> {
        unimplemented!("P4-T5")
    }
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
