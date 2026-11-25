//! 版本資訊（P3-T7）。
//!
//! 版本字串預設取自倉庫根目錄的 `VERSION` 檔；bundle 的舊版測試 artifact 由 build-time override 配對 CLI 與 daemon。

/// 目前版本號；正式 bundle 未覆寫時取 `VERSION` 去除首尾空白。
pub fn version() -> &'static str {
    match option_env!("SW_BUNDLE_VERSION") {
        Some(version) => version,
        None => include_str!("../../../VERSION").trim(),
    }
}

/// `serialwrap <version>`（與 Python `--version` 首行相同）。
pub fn version_line() -> String {
    format!("serialwrap {}", version())
}
