//! P5-T13：最小登入狀態機（紅燈階段骨架；T11 的 `Effect::StartLogin`／`ControlEvent::LoginFinished` 使用）。
//!
//! 函式本體為 `s21_todo!`，由 Task 13 實作者補上（05 §8）。

use sw_pal::traits::{Clock, SysEnv};
use sw_proto::config_types::SessionProfile;

use crate::session_auth::{AuthResolution, SessionAuth};
use crate::tx_writer::TxClass;
use crate::uart_bridge::RxShared;

pub use crate::quiet_gate::detect_boot_banner;

/// 登入計畫（`Effect::StartLogin` 內容）。
#[derive(Clone)]
pub enum LoginPlan {
    /// passthrough：不登入、不 probe。
    None,
    Probe,
    EnsureReady {
        auth: SessionAuth,
    },
    CredentialsUnresolved {
        reason: &'static str,
        env_file: String,
    },
}

/// 登入結果（`ControlEvent::LoginFinished` 內容）。
#[derive(Debug, Clone, PartialEq)]
pub enum LoginOutcome {
    Ready,
    /// 內含 last_error。
    NotReady(String),
    Failed(String),
}

/// 登入 FSM 所需的 I/O 注入點（TX 經 TxWriter；測試以閉包替身）。
pub struct LoginIo<'a> {
    pub rx: &'a RxShared,
    pub tx: &'a dyn Fn(&[u8], TxClass, &str, bool) -> sw_pal::TxResult,
    pub clock: &'a dyn Clock,
    pub nonce: &'a dyn Fn() -> String,
}

/// 依 profile、待自動登入旗標、已解析帳密與 quiet 狀態選擇登入計畫（05 §8.2）。
/// 回傳 `(計畫, 合成的 last_error)`。
pub fn choose_login_plan(
    _p: &SessionProfile,
    _pending_auto_login: bool,
    _auth: (SessionAuth, AuthResolution),
    _quiet_blocks: bool,
) -> (LoginPlan, Option<&'static str>) {
    s21_todo!("T13", "choose_login_plan")
}

/// 只做 probe：送 `\n` 並等 prompt；失敗時依視窗分類（05 §8.3）。
pub fn probe_ready(_io: &LoginIo, _p: &SessionProfile) -> LoginOutcome {
    s21_todo!("T13", "probe_ready")
}

/// 完整登入：login → password → prompt → nonce 驗證（05 §8.3）。
pub fn ensure_ready(
    _io: &LoginIo,
    _p: &SessionProfile,
    _auth: &SessionAuth,
    _env: &dyn SysEnv,
) -> LoginOutcome {
    s21_todo!("T13", "ensure_ready")
}

/// 依 platform 回傳 prompt 逾時碼（bcm→BCM_PROMPT_TIMEOUT、shell→SHELL_PROMPT_TIMEOUT、其他→PRPL_PROMPT_TIMEOUT）。
pub fn prompt_timeout_error(_platform: &str) -> &'static str {
    s21_todo!("T13", "prompt_timeout_error")
}

/// RX_FLOOD 可遮蔽的失敗碼（login_fsm.py `_FLOOD_MASKABLE_ERRORS` 逐字）。
pub const FLOOD_MASKABLE_ERRORS: [&str; 5] = [
    "PROMPT_UNAVAILABLE",
    "READY_NONCE_TIMEOUT",
    "READY_PROMPT_TIMEOUT",
    "POST_LOGIN_CMD_TIMEOUT",
    "LOGIN_PROMPT_TIMEOUT",
];

/// 需要補 `last_error_detail`（RX tail）的失敗碼（#174）。
pub const LOGIN_FSM_DETAIL_ERRORS: [&str; 12] = [
    "USER_ENV_MISSING",
    "PASS_ENV_REQUIRED",
    "PASS_ENV_MISSING",
    "LOGIN_USER_REQUIRED",
    "LOGIN_PROMPT_TIMEOUT",
    "BCM_PROMPT_TIMEOUT",
    "SHELL_PROMPT_TIMEOUT",
    "PRPL_PROMPT_TIMEOUT",
    "POST_LOGIN_CMD_TIMEOUT",
    "READY_NONCE_TIMEOUT",
    "READY_PROMPT_TIMEOUT",
    "LOGIN_REQUIRED",
];
