//! `benches.yaml` 載入與 bench 代號解析（P4-T17；逐字對齊 inventory §9.1 的 Python 基線 `sw_core/bench_registry.py`）。
//!
//! 紅燈 stub：行為尚未實作。錯誤訊息須逐字與 `inventory/07-platform-mgmt.md` §9.1 相同。

use std::collections::BTreeMap;

/// 一個 bench 代號的已驗證內容。欄位以 `inventory/07` §9.1 為準（`remote_socket` 必填、不接受 `remote_port`）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BenchEntry {
    pub code: String,
    pub target: String,
    pub remote_socket: Option<String>,
    pub remote_port: Option<u16>,
    pub local_port: u16,
    pub ssh_opts: Vec<String>,
    pub autossh: bool,
}

/// bench 設定錯誤：`code` 為 §9.2 既有錯誤碼（`INVALID_BENCH_CONFIG`／`UNKNOWN_BENCH_CODE`），`message` 逐字照 Python 基線。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BenchConfigError {
    pub code: &'static str,
    pub message: String,
}

/// 解析 `benches.yaml` 全文（YAML 文字）。
pub fn load_benches(_text: &str) -> Result<BTreeMap<String, BenchEntry>, BenchConfigError> {
    unimplemented!("P4-T17")
}

/// 依代號取出 bench；未知代號 → `UNKNOWN_BENCH_CODE`，訊息 `unknown bench code: <code>`。
pub fn resolve<'a>(
    _benches: &'a BTreeMap<String, BenchEntry>,
    _code: &str,
) -> Result<&'a BenchEntry, BenchConfigError> {
    unimplemented!("P4-T17")
}
