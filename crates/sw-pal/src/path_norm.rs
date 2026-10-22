//! 端點與路徑的比對正規化（arch §4.4，P5-T4）。
//!
//! Task 0 骨架：簽章已凍結，本體由 Task 4 交付（`s21_todo!`）。

use std::path::Path;

/// 去 `unix://` 前綴 → `expanduser`（對齊 P1 `normalize_endpoint_path`）→ abspath → normpath；
/// Windows 另轉小寫並統一 `\`。用於實例鎖的 `(lock_path, socket)` 比對。
pub fn normalize_for_compare(_p: &str, _cwd: &Path, _home: Option<&Path>) -> String {
    s21_todo!("T4", "path_norm::normalize_for_compare")
}
