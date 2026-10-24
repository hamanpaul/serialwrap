//! sw-core 整合測試共用輔助（`CARGO_MANIFEST_DIR` 只允許出現於此檔，見 `scripts/rust/check_cfg_boundary.py`）。

use std::path::{Path, PathBuf};

/// 以儲存庫根目錄為基準解析相對路徑（例如 `contracts/golden/wal`）。
pub fn repo_path(rel: &str) -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../..").join(rel)
}

/// P6 golden TSV 工具（07 Task 2）。
pub mod golden;
