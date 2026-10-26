//! P5-T11：sans-IO `SessionCore`（紅燈階段骨架）。
//!
//! 型別與公開簽章依 `docs/rust-refactor/plans/06-slice21-daemon-core.md` Task 11「Produces」與 Task 0 骨架凍結；
//! 函式本體為 `s21_todo!`（`unimplemented!`），由 Task 11 實作者補上。

use std::sync::atomic::AtomicU64;
use std::sync::Arc;

use sw_pal::traits::{DevicePath, SerialConfig, SysEnv};
use sw_proto::config_types::SessionProfile;
use sw_proto::pycompat::PyErr;
use sw_proto::pyjson::{PyDict, PyValue};

use crate::arbiter::{CommandRegistry, SubmitReq};
use crate::drain::StopId;
use crate::deferred::InvalidateReason;
use crate::generations::{EpochAllocator, Generations};
use crate::human_console::human_tx::HumanTxTag;
use crate::human_console::out_ring::OutItem;
use crate::login_fsm::LoginPlan;
use crate::mailbox::{MailboxItem, Reply, TimerEvent};
use crate::op_runner::OpSpec;
use crate::state_store::StateDelta;
use crate::tx_writer::{Fence, OpId, TxPermit};
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
    /// P7 Task 0（08 §0.3）：請 manager 對 `by_id` 排 attach（依 06 T16 inflight 去重；Task 5 reprobe 使用）。
    RequestAttach { by_id: String },
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
    /// P6 增補 #9（07 Task 10）：`pty` 為 RPC thread 預建 PTY 的資料（實際 `PtyPair` 留在 executor）。
    ConsoleAttach { label: Option<String>, pty: Option<PtyInfo> },
    /// P6 增補 #9。
    ConsoleDetach { client_id: String },
    /// P6 增補 #9。
    ConsoleList,
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
    /// P6 增補 #10：送往某 console 的出向資料（hub `try_send`，Display 滿載丟棄）。
    ConsoleOut { client_id: String, item: OutItem },
    /// P6 增補 #10：關閉單一 console（channel 滿時走 `HubCritical`）。
    ConsoleClose { client_id: String },
    /// P6 增補 #10：release 時關閉全部 console。
    ConsoleCloseAll,
    /// P6 增補 #10（07 Task 10）：executor 把 `pending_pty[token]` 交給 hub（`HubCmd::AdoptPty`）。
    ConsoleAdopt { token: u64, client_id: String, client_lifetime: u64, label: String },
    /// P6 增補 #10：executor drop `pending_pty[token]`。
    ConsoleDiscard { token: u64 },
    /// P6 增補 #10（07 D-15）：交給 human TX pump（`try_send`）。
    HumanTx { tag: HumanTxTag, permit: TxPermit, payload: Vec<u8> },
    /// P6 增補 #10（07 Task 7）：全域 `interactive_id → session_id` 索引；同批 Effects 中一律排在 `Reply` 之前。
    LeaseIndex(LeaseIndexOp),
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
    /// 帳密解析用 env（P5 Task 0 雙審修正：計畫 Task 11 Produces 補記；供 Task 13 `resolve_session_auth(p, env, read)` 於 attach／recover 時重新讀取）。
    pub env: Arc<dyn SysEnv>,
    /// 帳密解析用 env 檔讀取（P5 Task 0 雙審修正：計畫 Task 11 Produces 補記；`None`＝檔案不存在，`Some(Err)`＝不可讀）。
    pub read_env_file: Arc<dyn Fn(&str) -> Option<std::io::Result<String>> + Send + Sync>,
    /// P6 增補 #2（07 D-02）：hub 讀取用的 `input_epoch`；只在 `Fence::with` 內寫入。
    pub input_epoch_pub: Arc<AtomicU64>,
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

impl SessionCore {
    /// Two-Phase Drain 第一階段的發起點（Task 12；SC-04、arch §5.5）：`kind` 與 `waiter` 凍結簽章（P6 07 計畫依此呼叫）。
    pub fn begin_stop(
        &mut self,
        _kind: crate::drain::StopKind,
        _waiter: Option<Reply>,
        _now: Mono,
    ) -> Effects {
        crate::s21_todo!("T12", "SessionCore::begin_stop")
    }

    /// Drain 進行中（SC-05 attach 與 manager 的 open 前檢查用；Task 12）。
    pub fn drain_in_progress(&self) -> bool {
        crate::s21_todo!("T12", "SessionCore::drain_in_progress")
    }
}

/// P6 增補 #9（07 Task 7／10）：actor 層持有實際 `PtyPair`，core 只見資料。
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PtyInfo {
    pub token: u64,
    pub slave_path: String,
}

/// P6 增補 #10（07 Task 7）：全域 LeaseIndex 的異動。
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum LeaseIndexOp {
    Insert { interactive_id: String },
    Remove { interactive_id: String },
}

/// P6 Task 6：`fg_begin` 的結果（`fg_finish` 依此決定是否 resume）。
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct FgAdmission {
    pub suspended_human: bool,
}

impl SessionCore {
    /// P6 Task 6：admission 關；agent 且有 human lease 時 suspend；先撤銷 `replay_auth`（07 D-22(4)）。
    pub fn fg_begin(&mut self, _source_is_agent: bool, _fx: &mut Effects) -> FgAdmission {
        todo!("P6 Task 6")
    }

    /// P6 Task 6：resume（若 suspend）→ admission 開；依序回放（建立 07 D-22 hold）。
    pub fn fg_finish(&mut self, _adm: FgAdmission, _fx: &mut Effects) {
        todo!("P6 Task 6")
    }

    /// P6 Task 6：只 suspend（self-test、P8 file transfer）；先撤銷 `replay_auth`（07 D-22(4)）。
    pub fn human_suspend_begin(&mut self, _fx: &mut Effects) -> bool {
        todo!("P6 Task 6")
    }

    /// P6 Task 6：回放時建立 07 D-22 hold。
    pub fn human_suspend_end(&mut self, _suspended: bool, _fx: &mut Effects) {
        todo!("P6 Task 6")
    }

    /// P6 Task 6：BC-02 唯一入口（07 D-02）。
    pub fn invalidate_human_input(&mut self, _reason: InvalidateReason, _fx: &mut Effects) {
        todo!("P6 Task 6")
    }

    /// P6 Task 6（增補 #26）：`foreground.is_some()` 或存在未撤銷的 `replay_hold`（07 D-22(2)）。
    pub fn foreground_busy(&self) -> bool {
        todo!("P6 Task 6")
    }
}
