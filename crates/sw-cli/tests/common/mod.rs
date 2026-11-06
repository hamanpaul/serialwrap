//! 測試通用輔助函式。
use std::path::PathBuf;

/// 取得 repo 根目錄路徑。
///
/// 優先讀取執行期環境變數 `SW_TEST_DATA_DIR`；若未設定則以編譯期 `CARGO_MANIFEST_DIR/../..` 推導。
pub fn data_root() -> PathBuf {
    if let Ok(dir) = std::env::var("SW_TEST_DATA_DIR") {
        return PathBuf::from(dir);
    }
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..")
}
