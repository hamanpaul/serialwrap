//! P5-T11：sans-IO `SessionCore`（紅燈階段骨架）。
//!
//! 型別與公開簽章依 `docs/rust-refactor/plans/06-slice21-daemon-core.md` Task 11「Produces」與 Task 0 骨架凍結；
//! 函式本體為 `s21_todo!`（`unimplemented!`），由 Task 11 實作者補上。

use std::sync::Arc;

use sw_pal::traits::{DevicePath, SerialConfig, SysEnv};
use sw_proto::config_types::SessionProfile;
use sw_proto::pycompat::PyErr;
use sw_proto::pyjson::{PyDict, PyValue};

use crate::arbiter::{CommandRegistry, SubmitReq};
use crate::drain::StopId;
use crate::generations::{EpochAllocator, Generations};
use crate::login_fsm::LoginPlan;
use crate::mailbox::{MailboxItem, Reply, TimerEvent};
use crate::op_runner::OpSpec;
use crate::state_store::StateDelta;
use crate::tx_writer::{Fence, OpId};
use crate::uart_bridge::RxShared;
use crate::wal::WalWriter;
use crate::Mono;

/// daemon stderr／state.json 相關通知（Task 0 骨架型別）。
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum ManagerNote {
    SessionReady { sid: String },
    SessionDetached { sid: String, reason: Option<String> },
    StickyCandidate { sid: String, by_id: String, profile: String, real_path: String },
    InteractiveOpened { sid: String, interactive_id: String },
    InteractiveClosed { interactive_id: String },
}

/// session 狀態（02 §6.1 `state` 欄位）。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SessionState {
    Detached,
    Attaching,
    Attached,
    Ready,
    Recovering,
    Released,
    Flashing,
}

impl SessionState {
    pub fn as_str(&self) -> &'static str {
        s21_todo!("T11", "SessionState::as_str")
    }
}

/// 進入 `SessionCore` 的請求（RPC 分派後的內部表示）。
#[derive(Clone, Debug)]
pub enum SessionRequest {
    GetState,
    Activity,
    Submit(SubmitReq),
    Release { source: String, reason: PyValue },
    DeviceAttach { force: bool },
    SessionAttach,
    Recover { timeout_s: f64, force: bool },
    Clear,
    Bind { by_id: String },
    InteractiveOpen { owner: String, timeout_s: f64, command: String, allow_attached: bool },
    InteractiveSend { interactive_id: String, data: String, encoding: String },
    InteractiveStatus { interactive_id: String, screen_chars: i128 },
    InteractiveClose { interactive_id: String },
    SelfTest { timeout_s: f64, strict_human_lock: bool },
    LogStart,
    LogStop,
    LogStatus,
    ResultTail { cmd_id: String, from_chunk: i128, limit: i128 },
    CancelQueued { cmd_id: String },
}

/// `SessionCore::on_event` 的輸出效果；由 actor executor 執行（sans-IO）。
pub enum Effect {
    Reply(Reply, Result<PyDict, PyErr>),
    OpenBridge { dev: DevicePath, cfg: SerialConfig, precheck_holders: bool },
    BeginDrain { stop_id: StopId, epoch: u64 },
    StartLogin { epoch: u64, fsm_gen: u64, plan: LoginPlan },
    StartOp { grant: OpGrant, spec: OpSpec },
    Schedule { at: Mono, ev: TimerEvent },
    WalMeta { cmd_id: Option<String>, meta: PyDict },
    Persist { commit_id: u64, delta: StateDelta },
    Notify(ManagerNote),
    /// daemon stderr WARNING（GAP-G-01 模板）。
    Warn(String),
    /// R1-F8：op 終態處理點產生；executor 只對現行 worker epoch 呼叫 `tx().settle`（非阻塞）。
    SettleTx { epoch: u64, op_id: OpId },
}

/// `Effect` 的有序容器。
pub struct Effects(pub Vec<Effect>);

impl Effects {
    pub fn push(&mut self, _e: Effect) {
        s21_todo!("T11", "Effects::push")
    }
}

/// 前景命令授權（與 TX permit 的世代比對用）。
#[derive(Clone, Debug)]
pub struct OpGrant {
    pub op_id: OpId,
    pub epoch: u64,
    pub owner_gen: u64,
    pub fsm_gen: u64,
    pub cmd_id: Option<String>,
}

/// `SessionCore` 的注入依賴。
pub struct CoreDeps {
    pub fence: Arc<Fence>,
    pub registry: Arc<CommandRegistry>,
    pub wal: Arc<WalWriter>,
    pub rx: Arc<RxShared>,
    pub epochs: Arc<EpochAllocator>,
    pub wall_iso: Arc<dyn Fn() -> String + Send + Sync>,
    pub rng: Arc<dyn Fn(&mut [u8]) + Send + Sync>,
    /// 帳密解析用 env（T13 增補：`resolve_session_auth` 於 attach／recover 時重新讀取）。
    pub env: Arc<dyn SysEnv>,
    /// 帳密解析用 env 檔讀取（T13 增補；`None`＝檔案不存在，`Some(Err)`＝不可讀）。
    pub read_env_file: Arc<dyn Fn(&str) -> Option<std::io::Result<String>> + Send + Sync>,
}

/// `public()` 需要由 actor 外部提供的欄位（console 計數與 endpoint）。
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PublicCtx {
    pub console_count: i128,
    pub vtty: Option<String>,
    /// `None`：POSIX（不輸出 `console_endpoint`）；`Some(x)`：輸出 `console_endpoint: x`（Windows 2.1 範圍）。
    pub console_endpoint: Option<Option<String>>,
}

/// sans-IO session 狀態機（05 §1.6 `SessionRuntime` 對應；bridge 物件改為 `conn_epoch`）。
pub struct SessionCore {
    _stub: (),
}

impl SessionCore {
    pub fn new(_profile: SessionProfile, _profile_source: &str, _deps: CoreDeps) -> SessionCore {
        s21_todo!("T11", "SessionCore::new")
    }

    /// 處理一個事件；`now` 為 monotonic（Mono）。
    pub fn on_event(&mut self, _ev: MailboxItem, _now: Mono) -> Effects {
        s21_todo!("T11", "SessionCore::on_event")
    }

    /// 02 §6.1 欄位順序的 `SessionPublic`＋SC-04 兩欄。
    pub fn public(&self, _now: Mono, _ctx: &PublicCtx) -> PyDict {
        s21_todo!("T11", "SessionCore::public")
    }

    pub fn state(&self) -> SessionState {
        s21_todo!("T11", "SessionCore::state")
    }

    pub fn session_id(&self) -> &str {
        s21_todo!("T11", "SessionCore::session_id")
    }

    pub fn generations(&self) -> &Generations {
        s21_todo!("T11", "SessionCore::generations")
    }

    /// 只供測試（test-fakes）：直接設定狀態（例如 FLASHING 碼映射）。
    #[cfg(feature = "test-fakes")]
    pub fn force_state(&mut self, _s: SessionState) {
        s21_todo!("T11", "SessionCore::force_state")
    }

    /// 只供測試（test-fakes）：下一個事件處理時 panic（actor panic 隔離測試用）。
    #[cfg(feature = "test-fakes")]
    pub fn arm_panic_on_next_event(&mut self) {
        s21_todo!("T11", "SessionCore::arm_panic_on_next_event")
    }
}
