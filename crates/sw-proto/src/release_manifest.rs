//! release-manifest 解析與退役守門（P3／P4 介面，P9-T16 新增退役欄位）。
//!
//! 紅燈階段替身：所有函式本體為 `todo!()`／`unimplemented!()`，僅保留計畫指定的公開簽章，
//! 使 `tests/release_manifest_retirement.rs` 能編譯並於執行期失敗。P3／P4 合入後以真實實作取代。
use serde_json::Value;

/// manifest 解析錯誤（P3 既有三種，Task 16 新增 `RetirementInconsistent`）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ManifestError {
    /// JSON 格式錯誤。
    InvalidJson(String),
    /// 不符合 schema 必填欄位或型別。
    Schema(String),
    /// 退役標記與其他欄位不一致（例如 retired 卻帶 python daemon）。
    RetirementInconsistent(&'static str),
}

/// `components`／頂層 `retirement` 標記；只接受 `"removed"`。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Retirement {
    pub python_daemon: String,
}

/// 已解析的 release manifest。
#[derive(Debug, Clone)]
pub struct ReleaseManifest {
    pub schema_version: u32,
    pub bundle_version: String,
    pub stage: String,
    pub target: String,
    pub git_revision: String,
    /// P3 的 `components.python_cli_delegate.entry`；退役 manifest 沒有委派元件時為 `None`。
    pub python_cli_delegate_entry: Option<Vec<String>>,
    /// 原始 JSON（供 launcher 等讀取 `components.daemon.*`）。
    pub raw: Value,
}

impl ReleaseManifest {
    /// 解析並驗證（成功解析後呼叫 `validate_retirement`）。
    pub fn parse(_text: &str) -> Result<ReleaseManifest, ManifestError> {
        todo!("P9-T16 紅燈：P3 解析＋validate_retirement 尚未實作")
    }

    /// 退役標記（只接受 `"removed"`）。
    pub fn retirement(&self) -> Option<&Retirement> {
        unimplemented!("P9-T16 紅燈：retirement() 尚未實作")
    }

    /// `retirement.python_daemon == "removed"`。
    pub fn python_daemon_retired(&self) -> bool {
        unimplemented!("P9-T16 紅燈：python_daemon_retired() 尚未實作")
    }
}

/// 退役一致性規則：
/// - retired ⇒ stage == "3"、`components.daemon.implementation == "rust"`、無 `python_cli_delegate`、
///   daemon 無 `entry`／`runtime`／`package`；
/// - stage == "3" ⇒ retired；
/// - `python_cli_delegate_entry` 為 `None` ⇒ retired（否則 `Schema`，維持 P3 行為）。
pub fn validate_retirement(_m: &ReleaseManifest) -> Result<(), ManifestError> {
    unimplemented!("P9-T16 紅燈：validate_retirement 尚未實作")
}
