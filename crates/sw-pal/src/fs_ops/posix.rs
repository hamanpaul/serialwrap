//! POSIX 唯讀檔案操作。

use crate::traits::{posix_io_error, FsRead, PalResult};
use std::fs;
use std::path::Path;

#[derive(Clone, Copy, Debug, Default)]
pub struct NativeFsRead;

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
