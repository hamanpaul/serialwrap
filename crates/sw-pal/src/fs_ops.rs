//! `FsOps: FsRead`：寫入面、目錄連結與跨行程檔案鎖（arch §4.1）。
//! 平台實作放在 `posix`／`windows` 子模組，依目標平台擇一編譯。
//!
//! P4-T1 只交付簽章（主體 `unimplemented!("P4-T5")`）；實作由 P4-T5 承接。
use std::path::{Path, PathBuf};

use crate::traits::{FsRead, PalResult};

#[cfg(unix)]
pub mod posix;
#[cfg(windows)]
pub mod windows;

/// `symlink_or_copy_dir` 的結果（DF-16）。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum LinkKind {
    /// 建立了目錄 symlink。
    Symlink,
    /// 權限不足（Windows 非開發者模式）而改為遞迴複製。
    Copied,
}

/// 跨行程檔案鎖守衛。持有鎖檔的 handle；drop 時 handle 關閉，OS 隨之釋放 flock／LockFileEx。
///
/// 只有 `FsOps::file_lock_exclusive` 的實作（P4-T5 原生、P4-T1 測試替身）可建立；`from_file` 為其共用入口。
#[derive(Debug)]
pub struct FileLockGuard {
    file: Option<std::fs::File>,
}

impl FileLockGuard {
    /// 以已取得鎖的檔案 handle 建立守衛（handle 關閉即釋放鎖）。
    pub fn from_file(file: std::fs::File) -> Self {
        Self { file: Some(file) }
    }

    /// 守衛是否仍持有鎖檔 handle。
    pub fn is_held(&self) -> bool {
        self.file.is_some()
    }
}

/// 寫入面（P4-T1 補齊簽章；P3 呼叫端僅使用 `FsRead` 方法，`FsOps: FsRead` 為 supertrait）。
#[allow(unused_variables)]
pub trait FsOps: FsRead {
    /// 原子寫入（tmp＋rename；Windows 以 `MoveFileExW` 取代）。`mode` 只對 POSIX 生效。
    fn atomic_write(&self, p: &Path, data: &[u8], mode: Option<u32>) -> PalResult<()> {
        unimplemented!("P4-T5")
    }
    /// 直接寫入（不經 tmp）。
    fn write_file(&self, p: &Path, data: &[u8]) -> PalResult<()> {
        unimplemented!("P4-T5")
    }
    /// 遞迴建立目錄（`mkdir -p` 語意）。
    fn mkdirs(&self, p: &Path) -> PalResult<()> {
        unimplemented!("P4-T5")
    }
    /// 原子取代（`os.replace` 語意；Windows 為 `MoveFileExW(MOVEFILE_REPLACE_EXISTING)`）。
    fn replace(&self, from: &Path, to: &Path) -> PalResult<()> {
        unimplemented!("P4-T5")
    }
    /// 刪除單一檔案（不存在由呼叫端判斷）。
    fn remove_file(&self, p: &Path) -> PalResult<()> {
        unimplemented!("P4-T5")
    }
    /// 複製單一檔案並保留 mode。
    fn copy_preserving(&self, from: &Path, to: &Path) -> PalResult<()> {
        unimplemented!("P4-T5")
    }
    /// 遞迴複製（保留相對結構與 mode）；`to` 已存在時回 `InvalidInput`。
    fn copy_tree(&self, from: &Path, to: &Path) -> PalResult<()> {
        unimplemented!("P4-T5")
    }
    /// 遞迴刪除；不跟隨 symlink（symlink 本身 unlink）。Windows 對唯讀屬性先清除再刪。
    fn remove_tree(&self, p: &Path) -> PalResult<()> {
        unimplemented!("P4-T5")
    }
    /// `readlink`；非 symlink 回 `None`。
    fn read_link(&self, p: &Path) -> PalResult<Option<PathBuf>> {
        unimplemented!("P4-T5")
    }
    /// `chmod`（Windows 為 no-op 語意由實作決定）。
    fn set_mode(&self, p: &Path, mode: u32) -> PalResult<()> {
        unimplemented!("P4-T5")
    }
    /// fsync 目錄。
    fn sync_dir(&self, p: &Path) -> PalResult<()> {
        unimplemented!("P4-T5")
    }
    /// 建立檔案 symlink（先移除既有 `link`）。
    fn symlink_file(&self, link: &Path, target: &Path) -> PalResult<()> {
        unimplemented!("P4-T5")
    }
    /// 先移除既有 `link`，再建立目錄 symlink；失敗（Windows `ERROR_PRIVILEGE_NOT_HELD`）時改為遞迴複製（DF-16）。
    fn symlink_or_copy_dir(&self, link: &Path, target: &Path) -> PalResult<LinkKind> {
        unimplemented!("P4-T5")
    }
    /// 取得跨行程獨占鎖：POSIX `flock`，Windows `LockFileEx`；非阻塞取不到回 `WouldBlock`。
    fn file_lock_exclusive(&self, p: &Path, blocking: bool) -> PalResult<FileLockGuard> {
        unimplemented!("P4-T5")
    }
}
