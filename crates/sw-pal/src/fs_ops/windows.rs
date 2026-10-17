//! Windows 唯讀檔案操作。

use crate::traits::{
    windows_crt_errno, windows_crt_error, windows_win32_error, FsRead, PalError, PalResult,
};
use std::fs;
use std::io::Read;
use std::os::windows::ffi::OsStrExt;
use std::path::Path;

#[derive(Clone, Copy, Debug, Default)]
pub struct NativeFsRead;

impl FsRead for NativeFsRead {
    fn read(&self, path: &Path) -> PalResult<Vec<u8>> {
        if path.is_dir() && !has_trailing_separator(path) {
            return Err(windows_crt_error(13));
        }
        let mut file = fs::File::open(path).map_err(file_error)?;
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

fn file_error(error: std::io::Error) -> PalError {
    let code = error.raw_os_error().unwrap_or(5) as u32;
    windows_crt_error(windows_crt_errno(code))
}

fn win32_error(error: std::io::Error) -> PalError {
    windows_win32_error(error.raw_os_error().unwrap_or(5) as u32)
}

fn has_trailing_separator(path: &Path) -> bool {
    path.as_os_str()
        .encode_wide()
        .last()
        .is_some_and(|unit| unit == b'\\' as u16 || unit == b'/' as u16)
}
