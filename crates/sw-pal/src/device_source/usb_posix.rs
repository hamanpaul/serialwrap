//! POSIX `UsbTopology`（P7 Task 6；`transport_stall.resolve_usb_busid`）：sysfs tty `device` 連結的 realpath，
//! 由深至淺找第一個 fullmatch `\d+-\d+(\.\d+)*` 的路徑段（不含 interface 後綴 `1-1:1.0`）。任何錯誤回 `None`。
use std::path::{Path, PathBuf};

use crate::traits::UsbTopology;

/// 以 `sysfs_root`（預設 `/sys`）解析 USB busid；測試可注入 tempdir。
pub struct SysfsUsbTopology {
    root: PathBuf,
}

impl SysfsUsbTopology {
    pub fn with_root(root: &Path) -> Self {
        Self {
            root: root.to_path_buf(),
        }
    }
}

impl Default for SysfsUsbTopology {
    fn default() -> Self {
        Self::with_root(Path::new("/sys"))
    }
}

/// `re.fullmatch(r"\d+-\d+(\.\d+)*", seg)`。
fn is_busid(seg: &str) -> bool {
    let Some((head, rest)) = seg.split_once('-') else {
        return false;
    };
    let digits = |s: &str| !s.is_empty() && s.bytes().all(|b| b.is_ascii_digit());
    digits(head) && rest.split('.').all(digits)
}

/// `os.path.realpath(strict=False)`：路徑不存在時不拋錯，解析最長存在的前綴（含其中的 symlink）後接回其餘段。
fn realpath_lenient(path: &Path) -> PathBuf {
    let mut prefix = path.to_path_buf();
    let mut rest: Vec<std::ffi::OsString> = Vec::new();
    loop {
        if let Ok(resolved) = std::fs::canonicalize(&prefix) {
            return rest.iter().rev().fold(resolved, |acc, seg| acc.join(seg));
        }
        match (
            prefix.file_name().map(|n| n.to_os_string()),
            prefix.parent(),
        ) {
            (Some(name), Some(parent)) => {
                rest.push(name);
                prefix = parent.to_path_buf();
            }
            _ => return path.to_path_buf(),
        }
    }
}

impl UsbTopology for SysfsUsbTopology {
    fn usb_busid(&self, real_path: &str) -> Option<String> {
        if real_path.is_empty() {
            return None;
        }
        // `os.path.basename`：最後一個 `/` 之後。
        let name = real_path.rsplit('/').next().unwrap_or("");
        if name.is_empty() {
            return None;
        }
        let link = self.root.join("class/tty").join(name).join("device");
        let resolved = realpath_lenient(&link);
        let text = resolved.to_string_lossy().into_owned();
        text.rsplit('/')
            .find(|seg| is_busid(seg))
            .map(str::to_string)
    }
}
