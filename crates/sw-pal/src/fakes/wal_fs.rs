//! `WalFs` 記憶體替身（feature `test-fakes`）。
//!
//! slice21 Task 7 測試骨架：`new()` 只回傳空殼（非 panic），讓失敗落在 `WalWriter`；
//! 其餘方法為 `todo!()`，由 Task 1 實作。
//! Task 7 測試需要、Task 1 介面表未列的補充方法（`seed`、`open_attempts`、`fail_identity_queries`、`force_path_identity`）
//! 以標註「API 補充」保留，待 Task 1 一併確認。

use std::path::{Path, PathBuf};
use std::sync::Arc;

use crate::traits::{FileIdentity, PalError, PalResult, WalFile, WalFs};

/// 外部 reader 的 handle；Drop 時釋放 pending-delete（Windows 語意）。
pub struct ExternalHandle {
    _stub: (),
}

pub struct FakeWalFs {
    _stub: (),
}

impl FakeWalFs {
    pub fn new() -> Arc<FakeWalFs> {
        Arc::new(FakeWalFs { _stub: () })
    }

    pub fn contents(&self, _path: &Path) -> Option<Vec<u8>> {
        todo!("slice21 T1 FakeWalFs::contents")
    }

    /// API 補充：預置檔案內容（啟動掃描與既有歸檔測試用）。
    pub fn seed(&self, _path: &Path, _bytes: &[u8]) {
        todo!("slice21 T1 FakeWalFs::seed（API 補充）")
    }

    pub fn tail_needs_termination(&self, _path: &Path) -> bool {
        todo!("slice21 T1 FakeWalFs::tail_needs_termination")
    }

    /// 排序後回傳目錄下所有路徑。
    pub fn list_dir(&self, _dir: &Path) -> Vec<PathBuf> {
        todo!("slice21 T1 FakeWalFs::list_dir")
    }

    /// POSIX 語意：路徑消失、既有 handle 存活、`unlinked()` 為 true。
    pub fn external_unlink(&self, _path: &Path) {
        todo!("slice21 T1 FakeWalFs::external_unlink")
    }

    /// 新 identity 佔用路徑。
    pub fn external_replace(&self, _path: &Path, _bytes: &[u8]) {
        todo!("slice21 T1 FakeWalFs::external_replace")
    }

    /// Windows pending-delete 語意：unlink 後路徑保留到 handle drop。
    pub fn hold_external_reader(&self, _path: &Path) -> ExternalHandle {
        todo!("slice21 T1 FakeWalFs::hold_external_reader")
    }

    /// 接下來 `n` 次 `write_all_flush` 不寫任何 byte，回 `sent:0`。
    pub fn fail_next_writes(&self, _path: &Path, _n: usize, _err: PalError) {
        todo!("slice21 T1 FakeWalFs::fail_next_writes")
    }

    /// 下一次 `write_all_flush` 只附加前 `min(accept, len-1)` bytes，回 `sent` 為實際附加數。
    pub fn fail_next_write_partial(&self, _path: &Path, _accept: usize, _err: PalError) {
        todo!("slice21 T1 FakeWalFs::fail_next_write_partial")
    }

    /// 依序記錄每次 `write_all_flush` 收到的 bytes。
    pub fn write_calls(&self, _path: &Path) -> Vec<Vec<u8>> {
        todo!("slice21 T1 FakeWalFs::write_calls")
    }

    /// `open_append` 成功次數。
    pub fn open_calls(&self, _path: &Path) -> usize {
        todo!("slice21 T1 FakeWalFs::open_calls")
    }

    /// API 補充：`open_append` 嘗試次數（含失敗），供重試排程斷言。
    pub fn open_attempts(&self, _path: &Path) -> usize {
        todo!("slice21 T1 FakeWalFs::open_attempts（API 補充）")
    }

    pub fn fail_next_renames(&self, _n: usize, _err: PalError) {
        todo!("slice21 T1 FakeWalFs::fail_next_renames")
    }

    pub fn fail_next_opens(&self, _path: &Path, _n: usize, _err: PalError) {
        todo!("slice21 T1 FakeWalFs::fail_next_opens")
    }

    /// API 補充（持續性）：之後所有 `path_identity` 查詢回 `err`，直到替身重置。
    pub fn fail_identity_queries(&self, _path: &Path, _err: PalError) {
        todo!("slice21 T1 FakeWalFs::fail_identity_queries（API 補充）")
    }

    /// API 補充：強制 `path_identity` 回傳指定值（`None` 表示路徑不存在），用於 `(0,0)` sentinel 測試。
    pub fn force_path_identity(&self, _path: &Path, _id: Option<FileIdentity>) {
        todo!("slice21 T1 FakeWalFs::force_path_identity（API 補充）")
    }

    pub fn sync_calls(&self, _path: &Path) -> usize {
        todo!("slice21 T1 FakeWalFs::sync_calls")
    }

    pub fn set_dir_writable(&self, _dir: &Path, _writable: bool) {
        todo!("slice21 T1 FakeWalFs::set_dir_writable")
    }
}

impl WalFs for FakeWalFs {
    fn open_append(&self, _path: &Path) -> PalResult<Box<dyn WalFile>> {
        todo!("slice21 T6/T1 FakeWalFs::open_append")
    }

    fn path_identity(&self, _path: &Path) -> PalResult<Option<FileIdentity>> {
        todo!("slice21 T1 FakeWalFs::path_identity")
    }

    fn size(&self, _path: &Path) -> PalResult<Option<u64>> {
        todo!("slice21 T1 FakeWalFs::size")
    }

    fn tail_needs_termination(&self, _path: &Path) -> PalResult<bool> {
        todo!("slice21 T1 FakeWalFs::tail_needs_termination (WalFs)")
    }

    fn rename_noreplace_or_suffix(&self, _from: &Path, _base_to: &Path) -> PalResult<PathBuf> {
        todo!("slice21 T1 FakeWalFs::rename_noreplace_or_suffix")
    }

    fn sync_dir(&self, _dir: &Path) -> PalResult<()> {
        todo!("slice21 T1 FakeWalFs::sync_dir")
    }

    fn ensure_dir(&self, _dir: &Path) -> PalResult<()> {
        todo!("slice21 T1 FakeWalFs::ensure_dir")
    }

    fn dir_writable(&self, _dir: &Path) -> bool {
        todo!("slice21 T1 FakeWalFs::dir_writable")
    }
    // 註：`read_all`（plan Task 0 要求的讀取 API）尚未進入 WalFs 介面，待 Task 0／Task 6 補上後於此補實作。
}
