//! InstanceLock conformance（arch §4.4、計畫 Task 4）。Task 0 骨架：簽章凍結，斷言本體由 Task 4 測試作者補上。

use std::path::Path;
use std::sync::Arc;

use crate::lock::InstanceLockFactory;

/// 實例鎖共用斷言：二次取得回 `AlreadyRunning`、stale socket 可接管、路徑正規化（SC-10）（Task 4）。
pub fn lock_suite(_factory: Arc<dyn InstanceLockFactory>, _dir: &Path) {
    crate::s21_todo!("T4", "lock_suite")
}
