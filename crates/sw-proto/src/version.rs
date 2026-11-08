//! 版本資訊（P3-T7）。
//!
//! 版本字串取自倉庫根目錄的 `VERSION` 檔（編譯期內嵌），與 `pyproject.toml` 的 dynamic version 同源。

/// 目前版本號（`VERSION` 去除首尾空白）。
pub fn version() -> &'static str {
    include_str!("../../../VERSION").trim()
}

/// `serialwrap <version>`（與 Python `--version` 首行相同）。
pub fn version_line() -> String {
    format!("serialwrap {}", version())
}
