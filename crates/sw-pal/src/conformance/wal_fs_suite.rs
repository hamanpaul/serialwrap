//! WalFs conformance（arch §9.1、計畫 Task 6）：同一套斷言跑在 fakes 與原生檔案系統上。
//!
//! Task 0 骨架：簽章已凍結，斷言本體由 Task 6 測試作者補上。

use std::path::Path;
use std::sync::Arc;

use crate::fakes::wal_fs::FakeWalFs;
use crate::traits::WalFs;

/// 外部行為者（另一行程或 std::fs）：用來在 WAL 寫入期間模擬 unlink、覆寫與常駐 reader。
pub trait ExternalFsOps {
    fn unlink(&self, p: &Path);
    fn replace_with(&self, p: &Path, bytes: &[u8]);
    fn hold_reader(&self, p: &Path) -> Box<dyn Send>;
}

/// 原生外部行為者（`std::fs` 實作；Task 6）。
pub struct NativeExternalFs;

impl ExternalFsOps for NativeExternalFs {
    fn unlink(&self, _p: &Path) {
        crate::s21_todo!("T6", "NativeExternalFs::unlink")
    }
    fn replace_with(&self, _p: &Path, _bytes: &[u8]) {
        crate::s21_todo!("T6", "NativeExternalFs::replace_with")
    }
    fn hold_reader(&self, _p: &Path) -> Box<dyn Send> {
        crate::s21_todo!("T6", "NativeExternalFs::hold_reader")
    }
}

/// fake 外部行為者（包住 `FakeWalFs`；Task 6）。
pub struct FakeExternalFs(pub Arc<FakeWalFs>);

impl ExternalFsOps for FakeExternalFs {
    fn unlink(&self, _p: &Path) {
        crate::s21_todo!("T6", "FakeExternalFs::unlink")
    }
    fn replace_with(&self, _p: &Path, _bytes: &[u8]) {
        crate::s21_todo!("T6", "FakeExternalFs::replace_with")
    }
    fn hold_reader(&self, _p: &Path) -> Box<dyn Send> {
        crate::s21_todo!("T6", "FakeExternalFs::hold_reader")
    }
}

/// WalFs 共用斷言（Task 6）。
pub fn wal_fs_suite(_fs: Arc<dyn WalFs>, _dir: &Path, _external: &dyn ExternalFsOps) {
    crate::s21_todo!("T6", "wal_fs_suite")
}
