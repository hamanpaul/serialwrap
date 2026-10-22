//! WAL 檔案層的原生實作（arch §4.5，P5-T6）。`WalFs`／`WalFile` 介面定義於 `crate::traits`。
//!
//! Task 0 骨架：POSIX `O_APPEND` 常駐 handle、Windows share-delete handle；實作由 Task 6 交付。

pub use crate::traits::{FileIdentity, WalFile, WalFs, WalWriteError};
use std::path::{Path, PathBuf};

use crate::traits::{PalResult};

/// 原生 WAL 檔案系統（P5-T6）。
pub struct NativeWalFs;

impl WalFs for NativeWalFs {
    fn open_append(&self, _path: &Path) -> PalResult<Box<dyn WalFile>> {
        // 計畫 06 Task 6 Step 2 的預期訊息為 `PosixWalFs::open_append`（POSIX 實作）。
        crate::s21_todo!("T6", "PosixWalFs::open_append")
    }
    fn path_identity(&self, _path: &Path) -> PalResult<Option<FileIdentity>> {
        crate::s21_todo!("T6", "NativeWalFs::path_identity")
    }
    fn size(&self, _path: &Path) -> PalResult<Option<u64>> {
        crate::s21_todo!("T6", "NativeWalFs::size")
    }
    fn tail_needs_termination(&self, _path: &Path) -> PalResult<bool> {
        crate::s21_todo!("T6", "NativeWalFs::tail_needs_termination")
    }
    fn read_all(&self, _path: &Path) -> PalResult<Option<Vec<u8>>> {
        crate::s21_todo!("T6", "NativeWalFs::read_all")
    }
    fn rename_noreplace_or_suffix(&self, _from: &Path, _base_to: &Path) -> PalResult<PathBuf> {
        crate::s21_todo!("T6", "NativeWalFs::rename_noreplace_or_suffix")
    }
    fn sync_dir(&self, _dir: &Path) -> PalResult<()> {
        crate::s21_todo!("T6", "NativeWalFs::sync_dir")
    }
    fn ensure_dir(&self, _dir: &Path) -> PalResult<()> {
        crate::s21_todo!("T6", "NativeWalFs::ensure_dir")
    }
    fn dir_writable(&self, _dir: &Path) -> bool {
        crate::s21_todo!("T6", "NativeWalFs::dir_writable")
    }
}
