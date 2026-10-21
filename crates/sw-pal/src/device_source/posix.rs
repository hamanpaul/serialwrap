//! POSIX 裝置掃描（arch §4.3，P5-T5）。
//!
//! 【TDD 紅燈 stub】本檔目前只有介面骨架，實作留給 Task 5 Step 3。
use std::path::PathBuf;

use crate::traits::{DeviceInfo, PalResult};

/// 依 `dirs` 順序掃描 by-id／by-path 目錄，回傳 `(by_id 路徑, DeviceInfo)`（stub）。
pub fn posix_scan(
    _dirs: &[PathBuf],
    _allowlist: Option<&[String]>,
) -> PalResult<Vec<(String, DeviceInfo)>> {
    todo!("slice21 T5 posix_scan 尚未實作")
}
