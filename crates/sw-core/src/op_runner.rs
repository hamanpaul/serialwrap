//! P5-T14：前景／背景 op 的執行規格與結果（紅燈階段骨架；T11 的 `Effect::StartOp`／`ControlEvent::OpFinished` 使用）。
//!
//! `OpRunner`／`OpCtx` 與執行邏輯由 Task 14 實作者補上。

use std::sync::Arc;

use sw_pal::traits::Clock;
use sw_proto::config_types::{ProfileTemplate, SessionProfile};
use sw_proto::pycompat::PyErr;
use sw_proto::pyjson::PyDict;
use sw_proto::pyre::PyRegex;

use crate::arbiter::QueuedCommand;
use crate::login_fsm::LoginOutcome;
use crate::mailbox::ControlEvent;
use crate::session_core::OpGrant;
use crate::tx_writer::{FenceReject, TxWriterHandle};
use crate::uart_bridge::RxShared;
use crate::wal::WalWriter;

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

/// `OpRunner` 的依賴（Task 14 Produces）。
pub struct OpCtx {
    pub com: String,
    pub sid: String,
    pub profile: SessionProfile,
    pub rx: Arc<RxShared>,
    /// 可被 `OpCtx` clone 持有的 TX 入口（`TxWriterHandle` 的 Sender 不因此阻止 writer 停止，見 Task 9）。
    pub tx: TxWriterHandle,
    pub wal: Arc<WalWriter>,
    pub clock: Arc<dyn Clock>,
    pub control: crossbeam_channel::Sender<ControlEvent>,
    pub rng: Arc<dyn Fn(&mut [u8]) + Send + Sync>,
}

/// 前景 op 執行器（thread pool size 1 per session，ForegroundSlot；Task 14 Produces）。
pub struct OpRunner {
    _stub: (),
}

impl OpRunner {
    pub fn spawn(_ctx: OpCtx) -> OpRunner {
        s21_todo!("T14", "OpRunner::spawn")
    }

    /// 以授權 `grant` 執行 `spec`；結果以 `ControlEvent::OpFinished` 回報給 actor。
    pub fn run(&self, _grant: OpGrant, _spec: OpSpec) {
        s21_todo!("T14", "OpRunner::run")
    }
}

/// 由 raw 輸出抽出命令 stdout（05 §5.2）。
pub fn extract_command_stdout(
    _raw: &str,
    _command: &str,
    _prompt_regex: &PyRegex,
    _completion: Option<&PyRegex>,
) -> String {
    s21_todo!("T14", "extract_command_stdout")
}

/// `interactive.send` 的 payload 編碼（Python 基線 `encoding` 規則；不合法 → `PyErr`）。
pub fn encode_interactive_payload(_data: &str, _encoding: &str) -> Result<Vec<u8>, PyErr> {
    s21_todo!("T14", "encode_interactive_payload")
}
