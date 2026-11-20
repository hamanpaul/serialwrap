//! sw-daemon 整合測試共用輔助（`CARGO_MANIFEST_DIR` 只允許出現於此檔，見 `scripts/rust/check_cfg_boundary.py`）。

use std::path::PathBuf;

/// 取得 repo 根目錄路徑。
///
/// 優先讀取執行期環境變數 `SW_TEST_DATA_DIR`；若未設定則以編譯期 `CARGO_MANIFEST_DIR/../..` 推導
/// （不依賴 cwd：`cargo_l1_results.py` 等 runner 不在 crate 目錄執行測試 binary）。
pub fn data_root() -> PathBuf {
    if let Ok(dir) = std::env::var("SW_TEST_DATA_DIR") {
        return PathBuf::from(dir);
    }
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..")
}
