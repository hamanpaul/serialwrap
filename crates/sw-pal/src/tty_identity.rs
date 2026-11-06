//! 同一 tty 判定（#R2；parity-amendments #40／#64，B:E-109、F-S06）。
//!
//! 與 Python `sw_core.serial_port.same_tty` 同一判準。主判準為 `(st_dev, st_ino)`；readlink 字串相等與
//! `st_rdev` 相等都不足為憑：容器內獨立 devpts instance 的 `/dev/pts/N` 與主機同名、同 rdev（136:N），
//! 實為不同裝置。另允許一種別名：兩邊皆為字元裝置、rdev 相等且非零、且 major 不在 devpts 範圍
//! （例如容器以 mknod 掛入同一實體 UART 節點）。
//!
//! 本模組刻意獨立於 `proc_scan`／`proc_holders`，供 P5 `proc_holders` 與 P6 console reaper 共用，
//! 判定函式只吃 `TtyIdentity` 值，不碰檔案系統（可單元測試、平台無關）。

use std::io;
use std::path::Path;

/// `stat()` 取得的 tty 身分（只保留判定所需欄位）。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct TtyIdentity {
    pub dev: u64,
    pub ino: u64,
    pub rdev: u64,
    pub is_char_device: bool,
}

impl TtyIdentity {
    #[cfg(unix)]
    pub fn from_metadata(meta: &std::fs::Metadata) -> TtyIdentity {
        use std::os::unix::fs::{FileTypeExt, MetadataExt};
        TtyIdentity {
            dev: meta.dev(),
            ino: meta.ino(),
            rdev: meta.rdev(),
            is_char_device: meta.file_type().is_char_device(),
        }
    }
}

/// `path`（跟隨 symlink）的身分；stat 失敗原樣回傳錯誤。非 unix 無 `(dev, ino)` 可用，回 `Unsupported`。
pub fn stat_identity(path: &Path) -> io::Result<TtyIdentity> {
    #[cfg(unix)]
    {
        std::fs::metadata(path).map(|meta| TtyIdentity::from_metadata(&meta))
    }
    #[cfg(not(unix))]
    {
        let _ = path;
        Err(io::Error::from(io::ErrorKind::Unsupported))
    }
}

/// fd 所指 inode（`fd`）是否與 tty（`tty`）為同一裝置（Python `serial_port.same_tty`）。
pub fn same_tty(fd: &TtyIdentity, tty: &TtyIdentity) -> bool {
    if fd.dev == tty.dev && fd.ino == tty.ino {
        return true;
    }
    if !(fd.is_char_device && tty.is_char_device) {
        return false;
    }
    tty.rdev != 0 && fd.rdev == tty.rdev && !is_devpts_major(tty.rdev)
}

/// Linux `dev_t` 的 major（與 glibc `gnu_dev_major` 同一位元運算）落在 UNIX98 devpts 範圍 136–143。
pub fn is_devpts_major(rdev: u64) -> bool {
    let major = ((rdev >> 8) & 0xfff) | ((rdev >> 32) & !0xfff_u64);
    (136..=143).contains(&major)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn chr(dev: u64, ino: u64, rdev: u64) -> TtyIdentity {
        TtyIdentity {
            dev,
            ino,
            rdev,
            is_char_device: true,
        }
    }

    #[test]
    fn same_tty_table() {
        let pts7 = (136 << 8) | 7;
        let ttys0 = (4 << 8) | 64;
        // 同 inode
        assert!(same_tty(&chr(60, 10, pts7), &chr(60, 10, pts7)));
        // 他 devpts instance：rdev 相同、inode 不同
        assert!(!same_tty(&chr(148, 10, pts7), &chr(60, 10, pts7)));
        // 非 devpts mknod 別名
        assert!(same_tty(&chr(140, 55, ttys0), &chr(60, 11, ttys0)));
        // rdev 為 0 不算別名
        assert!(!same_tty(&chr(140, 55, 0), &chr(60, 11, 0)));
        // 非字元裝置不走 rdev
        let mut reg = chr(140, 55, ttys0);
        reg.is_char_device = false;
        assert!(!same_tty(&reg, &chr(60, 11, ttys0)));
        // devpts major 136..=143 邊界
        assert!(is_devpts_major(136 << 8) && is_devpts_major(143 << 8));
        assert!(!is_devpts_major(135 << 8) && !is_devpts_major(144 << 8));
    }
}
