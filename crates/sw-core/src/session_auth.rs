//! P5-T13：per-session 帳密（紅燈階段骨架；`LoginPlan::EnsureReady` 使用）。
//!
//! `parse_env_file` 與帳密解析函式由 Task 13 實作者補上（05 §9）。

use indexmap::IndexMap;
use sw_pal::traits::SysEnv;
use sw_proto::config_types::SessionProfile;

/// 已解析的帳密（05 §9 逐字；不得輸出到日誌或 RPC 回應）。
#[derive(Clone, Default)]
pub struct SessionAuth {
    pub username: Option<String>,
    pub password: Option<String>,
    pub console_unlock: Option<String>,
}

/// 帳密解析結果的分類（`last_error` 與 detail 的來源）。
#[derive(Clone, Debug, PartialEq)]
pub struct AuthResolution {
    pub reason: &'static str,
    pub env_file_path: Option<String>,
}

/// 解析 env 檔（`export A='x'`、`B = "y"`；略過註解與無 `=` 的行）。
pub fn parse_env_file(_text: &str) -> IndexMap<String, String> {
    s21_todo!("T13", "parse_env_file")
}

/// 由 profile 的 `env_file`／`user_env`／`pass_env` 與 env 解析帳密。
/// `read` 回 `None` 表示檔案不存在，`Some(Err)` 表示不可讀。
pub fn resolve_session_auth(
    _p: &SessionProfile,
    _env: &dyn SysEnv,
    _read: &dyn Fn(&str) -> Option<std::io::Result<String>>,
) -> (SessionAuth, AuthResolution) {
    s21_todo!("T13", "resolve_session_auth")
}

/// profile 宣告了需要帳密、但解析未完成（→ `CREDENTIALS_UNRESOLVED` 終態）。
pub fn credentials_declared_but_unresolved(_p: &SessionProfile, _r: &AuthResolution) -> bool {
    s21_todo!("T13", "credentials_declared_but_unresolved")
}
