//! P8 Task 6：`file.push`／`file.pull` 的 RPC 參數轉型與 session 請求型別。
//!
//! 對照 origin/main `sw_core/service.py` 的 `file.push`／`file.pull` 分支（轉型、`ack_mode` 白名單、
//! `check_source`、必填檢查的順序與回應形狀）。session 閘門與傳輸本體在 `SessionCore`（actor）與
//! `OpRunner` 內執行；本模組只做 RPC 層的純函式轉型，不碰 session。

use sw_proto::pycompat::PyErr;
use sw_proto::pyjson::PyDict;
use sw_proto::source_policy::SourceCheck;

/// 傳輸方向。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FileTransferKind {
    Push,
    Pull,
}

/// RPC 層轉型後的 file.* 請求（`SessionRequest::FileTransfer` 的內容）。
///
/// - `source`：`check_source(params.get("source"), default="agent")` 正規化後的來源（WAL 歸因用）；
///   RPC 路徑一律 `trusted_human=False`，閘門與 lease 判定都比照 agent（OD-74、DF-02）。
/// - `local_path`：push 為非空字串；pull 只有原始值 truthy 時才是 `Some(str(x))`。
/// - `chunk_size`：push 為 `max(1, int(x or 512))`；pull 不使用（固定 512）。
/// - `ack_mode`：push 為驗證過的白名單值；pull 只驗不轉傳（固定 `"auto"`）。
#[derive(Debug, Clone, PartialEq)]
pub struct FileTransferRequest {
    pub kind: FileTransferKind,
    pub selector: String,
    pub source: String,
    pub local_path: Option<String>,
    pub remote_path: String,
    pub chunk_size: i128,
    pub chunk_timeout_s: Option<f64>,
    pub ack_mode: String,
}

/// `from_rpc` 的結果：外層 `Err` 為穿越 RPC 邊界的 Python 例外（framing `EXCEPTION`）；內層 `Err` 為直接回給
/// 呼叫端的錯誤回應（`INVALID_ARGS`、`SOURCE_RESERVED`）。
pub type ParsedFileRpc = Result<Result<(FileTransferRequest, SourceCheck), PyDict>, PyErr>;

impl FileTransferRequest {
    /// 逐行照 `service.py` 的 `file.push`／`file.pull` 分支轉型（順序：數值轉型 → `ack_mode` → `source` → 必填）。
    pub fn from_rpc(kind: FileTransferKind, params: &PyDict) -> ParsedFileRpc {
        let _ = (kind, params);
        todo!("P8-T6")
    }
}
