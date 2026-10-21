//! P5-T14：前景／背景 op 的執行規格與結果（紅燈階段骨架；T11 的 `Effect::StartOp`／`ControlEvent::OpFinished` 使用）。
//!
//! `OpRunner`／`OpCtx` 與執行邏輯由 Task 14 實作者補上。

use sw_proto::config_types::ProfileTemplate;
use sw_proto::pyjson::PyDict;

use crate::arbiter::QueuedCommand;
use crate::login_fsm::LoginOutcome;
use crate::tx_writer::FenceReject;

/// 前景 op 的種類（`Effect::StartOp` 內容）。
pub enum OpSpec {
    Command(QueuedCommand),
    Recover { timeout_s: f64, force: bool },
    SelfTestProbe { timeout_s: f64, probe: String, nonce: String },
    InteractiveOpenCmd { command: String, owner: String },
    InteractiveSend { payload: Vec<u8>, owner: String },
    RebootRecovery { hard_timeout_s: f64 },
    DetectTemplate { templates: Vec<ProfileTemplate> },
}

/// op 完成結果（`ControlEvent::OpFinished` 內容）。
pub enum OpOutcome {
    CommandResult(PyDict),
    SendFailed,
    Recovered { key: &'static str, stdout: String },
    PromptTimeout { stdout: String },
    ToAttached { reason: &'static str },
    RebootExpected,
    RebootRecoveryDone(LoginOutcome),
    SelfTest(PyDict),
    /// R5-F2：nonce probe 的 TxReport.reject == Some(Quiet)；不等 nonce，由 actor 組 AUTOBOOT_QUIET 回應。
    SelfTestRefused(FenceReject),
    Sent { bytes: usize },
    Detected(Option<String>),
}
