//! POSIX 裝置掃描（arch §4.3，P5-T5）。
use std::collections::HashSet;
use std::fs;
use std::path::PathBuf;

use crate::traits::{DeviceInfo, DeviceSource, PalError, PalResult};

/// 依 `dirs` 順序掃描 by-id／by-path 目錄，回傳 `(by_id 路徑, DeviceInfo)`。
pub fn posix_scan(
    dirs: &[PathBuf],
    allowlist: Option<&[String]>,
) -> PalResult<Vec<(String, DeviceInfo)>> {
    let mut devices = Vec::new();
    let mut seen_real = HashSet::new();

    for dir in dirs {
        if !dir.is_dir() {
            continue;
        }

        let entries = fs::read_dir(dir).map_err(map_io_error)?;
        let mut paths = entries
            .map(|entry| entry.map(|entry| entry.path()).map_err(map_io_error))
            .collect::<PalResult<Vec<_>>>()?;
        paths.sort();

        for path in paths {
            let by_id = path.display().to_string();
            // 對應 Python `os.path.exists` 後的 `os.path.realpath`；斷鏈或掃描期間消失的項目略過。
            let Ok(real_path) = fs::canonicalize(&path) else {
                continue;
            };
            let real_path = real_path.display().to_string();
            if !seen_real.insert(real_path.clone()) {
                continue;
            }
            if allowlist.is_some_and(|allowed| !allowed.iter().any(|item| item == &by_id)) {
                continue;
            }

            devices.push((by_id.clone(), DeviceInfo { by_id, real_path }));
        }
    }

    Ok(devices)
}

/// 持有掃描目錄設定的 POSIX 裝置來源。
pub(crate) struct PosixDeviceSource {
    dirs: Vec<PathBuf>,
    allowlist: Option<Vec<String>>,
}

impl PosixDeviceSource {
    pub(crate) fn new(dirs: Vec<PathBuf>, allowlist: Option<Vec<String>>) -> Self {
        Self { dirs, allowlist }
    }
}

impl DeviceSource for PosixDeviceSource {
    fn scan(&mut self) -> PalResult<Vec<(String, DeviceInfo)>> {
        posix_scan(&self.dirs, self.allowlist.as_deref())
    }
}

fn map_io_error(error: std::io::Error) -> PalError {
    #[cfg(unix)]
    {
        crate::traits::posix_io_error(error)
    }
    #[cfg(not(unix))]
    {
        PalError::plain(crate::traits::PalErrorKind::Io, error.to_string())
    }
}
