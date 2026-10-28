//! P5-T11：sans-IO `SessionCore` 狀態機。
//!
//! 型別與公開簽章依 `docs/rust-refactor/plans/06-slice21-daemon-core.md` Task 11「Produces」與 Task 0 骨架凍結；
//! 事件處理只更新 session 狀態並產生效果，I/O 由 actor executor 執行。

use std::collections::{HashMap, VecDeque};
use std::sync::atomic::AtomicU64;
use std::sync::Arc;

use indexmap::IndexMap;
use sw_pal::traits::{DevicePath, PalError, SerialConfig, SysEnv};
use sw_proto::config_types::SessionProfile;
use sw_proto::constants::PASSTHROUGH_READY_QUIET_S;
use sw_proto::pycompat::{PyErr, clean_text, py_round, strftime_local, uuid4_dashed, uuid4_hex};
use sw_proto::pydict;
use sw_proto::pyjson::{PyDict, PyInt, PyValue};
use sw_proto::pyre::valid_completion_regex;

use crate::Mono;
use crate::arbiter::{
    CmdOutcome, CommandRegistry, QueuedCommand, RunDecision, SessionQueue, SubmitReq,
};
use crate::capture::{
    AgentLogCapture, BG_CAPTURE_MAX_BYTES, BackgroundCapture, CaptureEnv,
};
use crate::deferred::InvalidateReason;
use crate::drain::{StopId, StopKind, StopOp, WorkerStoppedReceipt};
use crate::generations::{EpochAllocator, Generations};
use crate::human_console::human_tx::HumanTxTag;
use crate::human_console::out_ring::OutItem;
use crate::lease::{InteractiveLease, Tombstones};
use crate::login_fsm::{
    LoginPlan, choose_login_plan, detect_boot_banner, login_error_detail,
    matches_login_or_password,
};
use crate::mailbox::{
    ControlEvent, DataEvent, DetachReason, MailboxItem, OpenFailure, OpenedBridge, Reply,
    TimerEvent,
};
use crate::op_runner::{OpOutcome, OpSpec, encode_interactive_payload, reject_reason};
use crate::quiet_gate::{
    boot_quiet_blocks_probe, is_reboot_command, matches_any_bootloader_prompt, QuietState,
    QuietTransition,
};
use crate::self_test::{NeedProbe, SelfTestBase, SelfTestCtx, SelfTestProbes};
use crate::session_auth::resolve_session_auth;
use crate::state_store::{ReleasedRow, StateDelta};
use crate::tx_writer::{Fence, FenceReject, FlashMode, OpId, TX_LEDGER_LIVE_MAX, TxPermit};
use crate::uart_bridge::RxShared;
use crate::wal::WalWriter;

/// daemon stderr／state.json 相關通知（Task 0 骨架型別）。
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum ManagerNote {
    /// 全域 background capture 淘汰到其他 session（manager 以 COM 解析擁有者後送 `EvictCapture`；審查 F28）。
    EvictCapture {
        com: String,
        cmd_id: String,
    },
    SessionReady {
        sid: String,
        /// 由 attach 登入 run 本身到 READY（Python `_attach_by_id_dynamic` 的 sticky 寫入點；reprobe／passthrough
        /// 安靜升 READY／reboot recovery 為 false，審查 F65）。
        via_attach: bool,
    },
    SessionDetached {
        sid: String,
        reason: Option<String>,
    },
    StickyCandidate {
        sid: String,
        by_id: String,
        profile: String,
        real_path: String,
    },
    InteractiveOpened {
        sid: String,
        interactive_id: String,
    },
    InteractiveClosed {
        interactive_id: String,
    },
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
        match self {
            SessionState::Detached => "DETACHED",
            SessionState::Attaching => "ATTACHING",
            SessionState::Attached => "ATTACHED",
            SessionState::Ready => "READY",
            SessionState::Recovering => "RECOVERING",
            SessionState::Released => "RELEASED",
            SessionState::Flashing => "FLASHING",
        }
    }
}

/// 進入 `SessionCore` 的請求（RPC 分派後的內部表示）。
#[derive(Clone, Debug)]
pub enum SessionRequest {
    GetState,
    Activity,
    Submit(SubmitReq),
    Release {
        source: String,
        reason: PyValue,
    },
    DeviceAttach {
        force: bool,
    },
    SessionAttach,
    Recover {
        timeout_s: f64,
        force: bool,
    },
    Clear,
    Bind {
        by_id: String,
    },
    InteractiveOpen {
        owner: String,
        timeout_s: f64,
        command: String,
        allow_attached: bool,
    },
    InteractiveSend {
        interactive_id: String,
        data: String,
        encoding: String,
    },
    InteractiveStatus {
        interactive_id: String,
        screen_chars: i128,
    },
    InteractiveClose {
        interactive_id: String,
    },
    SelfTest {
        timeout_s: f64,
        strict_human_lock: bool,
    },
    LogStart,
    LogStop,
    LogStatus,
    ResultTail {
        cmd_id: String,
        from_chunk: i128,
        limit: i128,
    },
    /// 只查本 session 的 background capture（Python `get_background_result` 的單 session 部分；
    /// 查無回 CMD_NOT_FOUND、不走登錄表 fallback，審查 F76）。
    BackgroundResult {
        cmd_id: String,
        from_chunk: i128,
        limit: i128,
    },
    CancelQueued {
        cmd_id: String,
    },
    /// P6 增補 #9（07 Task 10）：`pty` 為 RPC thread 預建 PTY 的資料（實際 `PtyPair` 留在 executor）。
    ConsoleAttach { label: Option<String>, pty: Option<PtyInfo> },
    /// P6 增補 #9。
    ConsoleDetach { client_id: String },
    /// P6 增補 #9。
    ConsoleList,
    /// P5-T16：manager 的 profile 再解析（#181 `_rematerialize_profile_locked`）：無 bridge 時原地換 template，
    /// `session_id` 依新 `profile_name` 重算；有 bridge 或 drain 中回 `SESSION_RECOVERING`（manager 不在此時呼叫）。
    SetProfile {
        profile: SessionProfile,
        profile_source: String,
    },
    /// bind 等待舊 by_id 的 attach op 逾時（#26；Python `bind_session` 直接寫活 session）：state RECOVERING、
    /// `drain_stalled=true`、`drain_timeout_epoch=epoch`，回 SESSION_RECOVERING 回應（審查 F64）。
    MarkAttachStalled {
        epoch: u64,
    },
    /// 同 `ControlEvent::DeviceAttached`，但經請求通道並回覆套用後的活 session（bind 改綁到在線裝置時，
    /// Python 於回覆前即 ATTACHING＋`last_error=None`；審查 F70）。
    DeviceOnline {
        by_id: String,
        real_path: String,
    },
    /// 只供測試（test-fakes）：直接設定狀態（FLASHING／RECOVERING 等無法由 RPC 到達者；Task 16 `force_session_state`）。
    #[cfg(feature = "test-fakes")]
    ForceStateForTest(SessionState),
    /// 只供測試（test-fakes）：佇列長度 `{len, physical_len}`（審查 F74：RPC cancel 的實體移除）。
    #[cfg(feature = "test-fakes")]
    QueueLensForTest,
}

/// `SessionCore::on_event` 的輸出效果；由 actor executor 執行（sans-IO）。
pub enum Effect {
    Reply(Reply, Result<PyDict, PyErr>),
    /// 成功開啟的 worker 交由 actor executor 持有，供後續依 epoch drain。
    AdoptBridge {
        epoch: u64,
        worker: crate::uart_bridge::IoWorker,
        real_path: String,
    },
    OpenBridge {
        dev: DevicePath,
        cfg: SerialConfig,
        precheck_holders: bool,
    },
    /// 交 manager 依 Python `_spawn_attach` 重新 attach（開埠前再解析 pin／fallback 偵測）；
    /// manager 完成後以 `DeviceAttached` 回送（審查 F61／F62）。
    Reattach {
        by_id: String,
        real_path: String,
    },
    BeginDrain {
        stop_id: StopId,
        epoch: u64,
    },
    StartLogin {
        epoch: u64,
        fsm_gen: u64,
        plan: LoginPlan,
    },
    StartOp {
        grant: OpGrant,
        spec: OpSpec,
    },
    Schedule {
        at: Mono,
        ev: TimerEvent,
    },
    WalMeta {
        cmd_id: Option<String>,
        meta: PyDict,
    },
    Persist {
        commit_id: u64,
        delta: StateDelta,
    },
    Notify(ManagerNote),
    /// daemon stderr WARNING（GAP-G-01 模板）。
    Warn(String),
    /// R1-F8：op 終態處理點產生；executor 只對現行 worker epoch 呼叫 `tx().settle`（非阻塞）。
    SettleTx {
        epoch: u64,
        op_id: OpId,
    },
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
    pub fn push(&mut self, e: Effect) {
        self.0.push(e)
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
    /// 全 daemon 共用的 background capture 索引（`BG_CAPTURE_MAX_COUNT` 為全域上限，審查 F28）。
    pub bg_index: Arc<crate::capture::BackgroundIndex>,
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
    profile: SessionProfile,
    /// 最近一次發布到 fence 的 `foreground_busy`（P7 Task 16-wire）。
    published_foreground_busy: bool,
    sid: String,
    profile_source: String,
    deps: CoreDeps,
    state: SessionState,
    generations: Generations,
    last_error: Option<String>,
    last_error_detail: Option<String>,
    credentials_warned: bool,
    detached_at: Option<String>,
    last_ready_at: Option<String>,
    attached_real_path: Option<String>,
    last_state_change_at: Option<String>,
    last_rx_at: Option<String>,
    last_tx_at: Option<String>,
    last_probe_at: Option<String>,
    last_rx_mono: Mono,
    last_tx_mono: Mono,
    recovering: bool,
    pending_open_stop: Option<PendingOpenStop>,
    pending_attach: Option<PendingAttach>,
    open_real_path: Option<String>,
    /// clear 收尾後等 manager 再解析（Python `_spawn_attach` → `_reresolve_profile_on_reattach`）再送
    /// `DeviceAttached`：期間 state 為 ATTACHING、無開埠（審查 F61／F62）。
    reattach_pending: bool,
    open_generation: u64,
    known_real_path: Option<String>,
    device_present: bool,
    /// P7 Task 5：readiness reprobe 進度（`reprobe_attempts`／`next_reprobe_at`／`reprobe_exhausted`、single-flight）。
    reprobe: crate::reprobe::ReprobeState,
    console_count: usize,
    /// 現行 bridge 的哨兵 PTY slave 路徑（`public()["vtty"]`、self-test `attached_vtty`）。
    vtty: Option<String>,
    queue: SessionQueue,
    foreground: Option<OpGrant>,
    /// 與前景並行的 op（READY recover、self-test probe；審查 F25）：不是 Python 的前景 operation，
    /// 不佔前景槽、不設 `foreground_busy`；epoch 結束時清空，遲到的完成事件以 stale 回覆。
    side_ops: HashMap<OpId, OpGrant>,
    queue_registered: bool,
    stop: Option<StopOp>,
    next_stop_id: u64,
    next_commit_id: u64,
    pending_commit: Option<PendingCommit>,
    release_persist_pending: bool,
    retired_stops: VecDeque<RetiredStop>,
    next_op_id: u64,
    next_uuid: u64,
    quiet_since: Option<Mono>,
    /// boot quiet／Ready Reconfirm（P5-T13 `QuietState`；fence 旗標於同一次 `Fence::with` 同步）。
    quiet: QuietState,
    /// 進行中的登入 run（StartLogin 已發出、LoginFinished 未到）。
    login_run: Option<LoginRun>,
    /// 前景命令為 reboot 類且尚未因 TX 結算 arm quiet（parity-amendments-2026-10-26 #55）。
    foreground_reboot: bool,
    drain_stalled: bool,
    drain_timeout_epoch: Option<u64>,
    released_by: Option<String>,
    released_at: Option<String>,
    /// Python `released_reason`：RPC `device.release` 的 `reason` 原樣（任意 JSON；未帶為 null）。
    released_reason: PyValue,
    /// agent Interactive Lease（P5-T14；`interactive_session_id` 由此導出）。
    lease: Option<InteractiveLease>,
    /// 已到期 lease 的 tombstone（每 session FIFO 16，#54）。
    tombstones: Tombstones,
    /// 前景 op 的待辦（以 op_id 索引；回覆與狀態副作用於 `OpFinished` 套用）。
    pending_ops: HashMap<OpId, PendingOp>,
    /// background capture（`command.result_tail`；05 §1.1）。
    background: IndexMap<String, BackgroundCapture>,
    background_cmd_ids: Vec<String>,
    background_seq: u64,
    /// agent log capture（`session.log_start`）。
    log_capture: Option<AgentLogCapture>,
    /// P7 Task 10：reader 端 agent log 寫入者（掛在現行 bridge 的 `RxShared`；不經 actor 佇列）。
    capture_observer: Arc<crate::capture::CaptureObserver>,
    capture_env: CaptureEnv,
    /// reboot 後自動恢復（`_spawn_reboot_recovery` 的 actor 驅動版）。
    reboot_recovery: Option<RebootRecovery>,
    /// force recover（clear＋reattach＋等待 READY 10 s）的等待者。
    force_recover: Option<ForceRecover>,
    /// self-test 的外部觀測注入（RELEASED holder 掃描、哨兵 PTY 存活）。
    self_test_probes: SelfTestProbes,
    /// 最近一筆 RX 的 WAL seq（capture `last_seq`）。
    last_rx_wal_seq: u64,
    pending_release: Option<PendingRelease>,
    pending_auto_login: bool,
    shutdown_requested: bool,
    shutdown_complete: bool,
    /// P5-T16：`StopKind::Bind` drain 完成後要改綁的 by_id（Python `_DrainJob.target_by_id`）。
    pending_bind: Option<String>,
    #[cfg(feature = "test-fakes")]
    panic_events_remaining: u8,
    /// 只供測試（test-fakes）：`last_error` 每次變更為非 None 時的觀察者（Task 16 `last_error_history`）。
    #[cfg(feature = "test-fakes")]
    last_error_observer: Option<Box<dyn Fn(&str) + Send>>,
}

struct PendingOpenStop {
    id: StopId,
    generation: u64,
    kind: StopKind,
    waiters: Vec<Reply>,
    started: Mono,
    stalled: bool,
    already_released: bool,
    post_kind: Option<StopKind>,
}

/// 進行中的登入 run（`Effect::StartLogin` 已發出）。
struct LoginRun {
    epoch: u64,
    kind: LoginRunKind,
    /// 啟動當下的 `fsm_gen`（審查 F15）：quiet arm 會令 core 的 `fsm_gen` 前進，但不是新的 login run；
    /// 完成事件以 run 自己的世代比對（Python attach thread 照常寫終態）。
    fsm_gen: u64,
    /// `SessionAttach`／`Recover` 再探的等待者（attach 路徑無）。
    waiters: Vec<(Reply, ReprobeWaiter)>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum LoginRunKind {
    /// bridge 開啟後的 attach 登入（`_attach_by_id`）。
    Attach,
    /// 既有 bridge 再探（`_probe_existing_bridge`）。
    Reprobe,
    /// reboot 後自動恢復（`_spawn_reboot_recovery` 的 `ensure_ready`）。
    RebootRecovery,
    /// P7 Task 5：1 s tick 的自動 readiness reprobe（`_spawn_reprobe_probe` → `_probe_existing_bridge`）。
    AutoReprobe,
}

/// 再探等待者的回應形狀。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum ReprobeWaiter {
    /// `attach_session`：`_attach_state_result`。
    Attach,
    /// `recover_session` ATTACHED 分支：REPROBE payload（SC-34）。
    Recover { force: bool },
}

struct PendingRelease {
    source: String,
    reason: PyValue,
    closed_consoles: i128,
    aborted_cmd: bool,
}

struct PendingAttach {
    generation: u64,
    by_id: String,
    real_path: String,
    reply: Reply,
    previous_state: SessionState,
}

enum PendingCommit {
    Release {
        id: u64,
        owner: Option<(Reply, bool)>,
        waiters: Vec<(Reply, bool)>,
    },
    Attach {
        id: u64,
        opened: OpenedBridge,
        reply: Reply,
        previous_state: SessionState,
    },
    AttachFailed {
        id: u64,
        reply: Reply,
        previous_state: SessionState,
    },
}

struct RetiredStop {
    epoch: u64,
    id: StopId,
    aborted_op: Option<(OpId, String)>,
}

impl SessionCore {
    pub fn new(profile: SessionProfile, profile_source: &str, deps: CoreDeps) -> SessionCore {
        let sid = format!("{}:{}", profile.template.profile_name, profile.com);
        SessionCore {
            profile,
            published_foreground_busy: false,
            sid,
            profile_source: profile_source.to_string(),
            deps,
            state: SessionState::Detached,
            generations: Generations::default(),
            last_error: None,
            last_error_detail: None,
            credentials_warned: false,
            detached_at: None,
            last_ready_at: None,
            attached_real_path: None,
            last_state_change_at: None,
            last_rx_at: None,
            last_tx_at: None,
            last_probe_at: None,
            last_rx_mono: 0,
            last_tx_mono: 0,
            recovering: false,
            pending_open_stop: None,
            pending_attach: None,
            open_real_path: None,
            reattach_pending: false,
            open_generation: 0,
            known_real_path: None,
            device_present: false,
            reprobe: crate::reprobe::ReprobeState::default(),
            console_count: 0,
            vtty: None,
            queue: SessionQueue::new(),
            foreground: None,
            side_ops: HashMap::new(),
            queue_registered: false,
            stop: None,
            next_stop_id: 0,
            next_commit_id: 0,
            pending_commit: None,
            release_persist_pending: false,
            retired_stops: VecDeque::new(),
            next_op_id: 0,
            next_uuid: 0,
            quiet_since: None,
            quiet: QuietState::default(),
            login_run: None,
            foreground_reboot: false,
            drain_stalled: false,
            drain_timeout_epoch: None,
            released_by: None,
            released_at: None,
            released_reason: PyValue::Null,
            lease: None,
            tombstones: Tombstones::new(),
            pending_ops: HashMap::new(),
            background: IndexMap::new(),
            background_cmd_ids: Vec::new(),
            background_seq: 0,
            log_capture: None,
            capture_observer: crate::capture::CaptureObserver::new(),
            capture_env: CaptureEnv::default(),
            reboot_recovery: None,
            force_recover: None,
            self_test_probes: SelfTestProbes::default(),
            last_rx_wal_seq: 0,
            pending_release: None,
            pending_auto_login: false,
            shutdown_requested: false,
            shutdown_complete: false,
            pending_bind: None,
            #[cfg(feature = "test-fakes")]
            panic_events_remaining: 0,
            #[cfg(feature = "test-fakes")]
            last_error_observer: None,
        }
    }

    /// 處理一個事件；`now` 為 monotonic（Mono）。
    pub fn on_event(&mut self, ev: MailboxItem, now: Mono) -> Effects {
        #[cfg(feature = "test-fakes")]
        if self.panic_events_remaining > 0 {
            self.panic_events_remaining -= 1;
            panic!("測試注入的 session actor panic");
        }

        let mut effects = Effects(Vec::new());
        match ev {
            MailboxItem::Control(ControlEvent::Request { req, reply }) => {
                effects.0.extend(self.handle_request(req, reply, now));
            }
            MailboxItem::Control(ControlEvent::Shutdown { reply }) => {
                effects
                    .0
                    .extend(self.begin_stop(StopKind::Shutdown, Some(reply), now).0);
            }
            MailboxItem::Control(ControlEvent::DeviceAttached { by_id, real_path }) => {
                effects
                    .0
                    .extend(self.device_attached(&by_id, &real_path, now));
            }
            MailboxItem::Control(ControlEvent::DeviceDetached { by_id, reason }) => {
                if by_id == self.profile.device_by_id {
                    self.device_present = false;
                    self.known_real_path = None;
                }
                if by_id == self.profile.device_by_id
                    && self.state != SessionState::Released
                    && self.state != SessionState::Detached
                {
                    let (kind, error) = match reason {
                        DetachReason::Removed if self.state == SessionState::Attaching => {
                            (StopKind::Removed, "DEVICE_REMOVED_DURING_ATTACH")
                        }
                        DetachReason::Removed => (StopKind::Removed, "DEVICE_REMOVED"),
                        DetachReason::Rebound => (StopKind::Rebound, "DEVICE_REBOUND_REQUIRED"),
                    };
                    self.set_last_error(Some(error.to_string()), None);
                    effects.0.extend(self.begin_stop(kind, None, now).0);
                }
            }
            MailboxItem::Control(ControlEvent::DeviceMissing) => {
                self.mark_missing_device(now);
            }
            MailboxItem::Control(ControlEvent::EvictCapture { cmd_id }) => {
                self.drop_background_capture(&cmd_id);
            }
            MailboxItem::Control(ControlEvent::ConsoleCountChanged { count }) => {
                self.console_count = count;
            }
            MailboxItem::Control(ControlEvent::BridgeOpened { epoch_hint, result }) => {
                effects
                    .0
                    .extend(self.bridge_opened(epoch_hint, result, now));
            }
            MailboxItem::Control(ControlEvent::UartIoError { epoch, reason }) => {
                if self.generations.conn_epoch == Some(epoch) {
                    if self.stop.is_none() {
                        self.set_last_error(Some(format!("BRIDGE_DOWN:{reason}")), None);
                    }
                    if reason == "TX_LEDGER_SATURATED" {
                        effects.0.push(Effect::Warn(format!(
                            "session {} TX 帳本未結算 op 達 {TX_LEDGER_LIVE_MAX} 上限，以 IoError drain 收斂（R1-F8）",
                            self.profile.com,
                        )));
                    }
                    effects
                        .0
                        .extend(self.begin_stop(StopKind::IoError, None, now).0);
                }
            }
            MailboxItem::Control(ControlEvent::UartTxCompleted {
                epoch,
                op_id,
                confirmed_bytes,
                unknown,
            }) => {
                if self.generations.conn_epoch == Some(epoch) {
                    self.last_tx_mono = now;
                    self.last_tx_at = Some((self.deps.wall_iso)());
                    effects.0.extend(self.reboot_tx_settled(
                        epoch,
                        op_id,
                        confirmed_bytes,
                        unknown,
                        now,
                    ));
                }
            }
            MailboxItem::Control(ControlEvent::WorkerStopped(receipt)) => {
                effects.0.extend(self.worker_stopped(receipt, now));
            }
            MailboxItem::Control(ControlEvent::WorkerDrainTimeout { epoch, stop_id }) => {
                effects
                    .0
                    .extend(self.mark_drain_timeout(epoch, stop_id, now));
            }
            MailboxItem::Control(ControlEvent::OpFinished {
                op_id,
                epoch,
                outcome,
                ..
            }) => {
                effects
                    .0
                    .extend(self.op_finished(op_id, epoch, outcome, now));
            }
            MailboxItem::Control(ControlEvent::LoginFinished {
                epoch,
                fsm_gen,
                result,
                ..
            }) => {
                effects
                    .0
                    .extend(self.login_finished(epoch, fsm_gen, result, now));
            }
            MailboxItem::Control(ControlEvent::Timer(timer)) => {
                effects.0.extend(self.timer(timer, now));
            }
            MailboxItem::Control(ControlEvent::Tick) => {
                effects.0.extend(self.tick(now));
            }
            MailboxItem::Control(ControlEvent::StateCommitted { commit_id, result }) => {
                effects
                    .0
                    .extend(self.state_committed(commit_id, result, now));
            }
            MailboxItem::Data(DataEvent::UartRxChunk {
                epoch,
                rx_seq: _,
                wal_seq,
                bytes,
            }) => {
                effects.0.extend(self.rx_chunk(epoch, &bytes, wal_seq, now));
            }
            // P6 Task 1：Slice 2.2 新增的 mailbox 事件（行為由 P6 Task 6／9／10 交付）。
            ev @ (MailboxItem::Data(DataEvent::ConsoleInputBytes { .. })
            | MailboxItem::Data(DataEvent::ConsoleDisconnected { .. })
            | MailboxItem::Control(ControlEvent::ConsoleConnected { .. })
            | MailboxItem::Control(ControlEvent::ConsoleScan { .. })
            | MailboxItem::Control(ControlEvent::HumanTxCompleted { .. })
            | MailboxItem::Control(ControlEvent::ConsoleAttachReq { .. })) => {
                effects.0.extend(self.console_event(ev, now));
            }
        }
        effects.0.extend(self.poll_force_recover(now));
        // P7 Task 10：reader 端 agent log 的前景 gate（Python `session.foreground_busy`，只計命令，EVT-D-L1）。
        self.capture_observer
            .set_foreground_busy(self.foreground_busy());
        // P7 Task 16-wire：發布 foreground_busy 供 `BridgeQueries::active_cmd_id_for`（不經 actor mailbox）。
        let busy = self.foreground.is_some();
        if busy != self.published_foreground_busy {
            self.published_foreground_busy = busy;
            self.deps.fence.with(|f| f.foreground_busy = busy);
        }
        effects
    }

    /// 02 §6.1 欄位順序的 `SessionPublic`＋SC-04 兩欄。
    pub fn public(&self, now: Mono, ctx: &PublicCtx) -> PyDict {
        let (rx_bytes, rx_rate) = if self.generations.conn_epoch.is_some() {
            let (bytes, rate) = self.deps.rx.rx_stats(now);
            (Some(bytes), Some(rate))
        } else {
            (None, None)
        };
        let idle_for_ms = self
            .idle_ms(now)
            .map(|n| PyValue::Int(PyInt::Small(n as i128)))
            .unwrap_or(PyValue::Null);
        let boot_quiet_remaining = self
            .quiet
            .remaining_s(now)
            .map(PyValue::from)
            .unwrap_or(PyValue::Null);
        let reconfirm_remaining = self
            .quiet
            .reconfirm_remaining_s(now)
            .map(PyValue::from)
            .unwrap_or(PyValue::Null);
        // Python：`len(background_cmd_ids) + (1 if foreground_busy else 0)`（審查 F27）。
        let outstanding = self.background_cmd_ids.len() + usize::from(self.foreground_busy());
        let expected_duration = self
            .foreground
            .as_ref()
            .and_then(|grant| {
                grant
                    .cmd_id
                    .as_deref()
                    .and_then(|id| self.deps.registry.get(id))
                    .and_then(|record| record.get("expected_duration_s").cloned())
                    .filter(|v| !v.is_null())
            })
            .unwrap_or(PyValue::Null);

        let mut out = PyDict::new();
        out.insert("session_id", PyValue::from(self.sid.clone()));
        out.insert(
            "profile",
            PyValue::from(self.profile.template.profile_name.clone()),
        );
        out.insert("com", PyValue::from(self.profile.com.clone()));
        out.insert("alias", PyValue::from(self.profile.alias.clone()));
        out.insert("act_no", PyValue::from(self.profile.act_no.clone()));
        out.insert(
            "device_by_id",
            PyValue::from(self.profile.device_by_id.clone()),
        );
        out.insert(
            "platform",
            PyValue::from(self.profile.template.platform.clone()),
        );
        out.insert("profile_source", PyValue::from(self.profile_source.clone()));
        out.insert(
            "command_capable",
            PyValue::from(self.profile.template.command_capable()),
        );
        out.insert("state", PyValue::from(self.state.as_str()));
        out.insert("last_error", option_string(self.last_error.as_deref()));
        out.insert(
            "last_error_detail",
            option_string(self.last_error_detail.as_deref()),
        );
        out.insert("detached_at", option_string(self.detached_at.as_deref()));
        out.insert(
            "last_ready_at",
            option_string(self.last_ready_at.as_deref()),
        );
        out.insert("vtty", option_string(ctx.vtty.as_deref()));
        out.insert(
            "attached_real_path",
            option_string(self.attached_real_path.as_deref()),
        );
        out.insert(
            "bridge_generation",
            py_int(self.generations.bridge_generation as i128),
        );
        out.insert("recovering", PyValue::from(self.recovering));
        out.insert(
            "interactive_session_id",
            option_string(self.lease.as_ref().map(|l| l.interactive_id.as_str())),
        );
        out.insert("foreground_busy", PyValue::from(self.foreground_busy()));
        out.insert("fg_cmd_expected_duration_s", expected_duration);
        out.insert("console_count", py_int(ctx.console_count));
        out.insert(
            "last_state_change_at",
            option_string(self.last_state_change_at.as_deref()),
        );
        out.insert("last_rx_at", option_string(self.last_rx_at.as_deref()));
        out.insert("last_tx_at", option_string(self.last_tx_at.as_deref()));
        out.insert(
            "last_probe_at",
            option_string(self.last_probe_at.as_deref()),
        );
        out.insert(
            "reprobe_attempts",
            py_int(i128::from(self.reprobe.attempts)),
        );
        // Python `next_reprobe_at` 為 `time.monotonic()` 秒（float）。
        out.insert(
            "next_reprobe_at",
            self.reprobe.next_reprobe_at.map_or(PyValue::Null, |m| {
                PyValue::Float(m as f64 / 1_000_000_000.0)
            }),
        );
        out.insert("reprobe_exhausted", PyValue::from(self.reprobe.exhausted));
        out.insert("boot_quiet_remaining_s", boot_quiet_remaining);
        out.insert(
            "ready_reconfirm_pending",
            PyValue::from(self.quiet.pending),
        );
        out.insert(
            "ready_reconfirm_failed",
            PyValue::from(self.quiet.failed),
        );
        out.insert("ready_reconfirm_remaining_s", reconfirm_remaining);
        out.insert("idle_for_ms", idle_for_ms);
        out.insert("last_rx_age_s", age_s(self.last_rx_mono, now));
        out.insert("last_tx_age_s", age_s(self.last_tx_mono, now));
        out.insert(
            "rx_bytes_last_10s",
            rx_bytes.map(|n| py_int(n as i128)).unwrap_or(PyValue::Null),
        );
        out.insert(
            "rx_rate_bps",
            rx_rate.map(|n| py_int(n as i128)).unwrap_or(PyValue::Null),
        );
        out.insert("outstanding_commands", py_int(outstanding as i128));
        out.insert(
            "activity_classification",
            PyValue::from(self.activity_classification(now)),
        );
        out.insert("released_by", option_string(self.released_by.as_deref()));
        out.insert("released_at", option_string(self.released_at.as_deref()));
        out.insert(
            "released_reason",
            self.released_reason.clone(),
        );
        out.insert(
            "capture",
            match &self.log_capture {
                Some(cap) => {
                    let mut d = PyDict::new();
                    d.insert("capture_id", PyValue::from(cap.capture_id.clone()));
                    d.insert("log_path", PyValue::from(cap.log_path.clone()));
                    d.insert("status", PyValue::from(cap.status));
                    d.insert("line_count", py_int(cap.line_count as i128));
                    d.insert("byte_count", py_int(cap.byte_count as i128));
                    PyValue::Dict(d)
                }
                None => PyValue::Null,
            },
        );
        out.insert("drain_stalled", PyValue::from(self.drain_stalled));
        out.insert(
            "drain_timeout_epoch",
            self.drain_timeout_epoch
                .map(|n| py_int(n as i128))
                .unwrap_or(PyValue::Null),
        );
        if let Some(endpoint) = &ctx.console_endpoint {
            out.insert("console_endpoint", option_string(endpoint.as_deref()));
        }
        out
    }

    pub fn state(&self) -> SessionState {
        self.state
    }

    pub fn session_id(&self) -> &str {
        &self.sid
    }

    pub fn generations(&self) -> &Generations {
        &self.generations
    }

    /// 只供測試（test-fakes）：直接設定狀態（例如 FLASHING 碼映射）。
    #[cfg(feature = "test-fakes")]
    pub fn force_state(&mut self, s: SessionState) {
        self.set_state(s, 0);
    }

    /// 只供測試（test-fakes）：下一個事件處理時 panic（actor panic 隔離測試用）。
    #[cfg(feature = "test-fakes")]
    pub fn arm_panic_on_next_event(&mut self) {
        self.panic_events_remaining = 1;
    }

    /// 只供測試（test-fakes）：連續注入多次 actor 事件 panic。
    #[cfg(feature = "test-fakes")]
    pub fn arm_panic_on_next_events(&mut self, count: u8) {
        self.panic_events_remaining = count;
    }

    /// 只供測試（test-fakes）：建立 last_error/detail 組合以驗證值改變時的清除語意。
    #[cfg(feature = "test-fakes")]
    pub fn set_last_error_for_test(&mut self, error: Option<&str>, detail: Option<&str>) {
        self.set_last_error(error.map(str::to_string), detail.map(str::to_string));
    }

    /// 只供測試（test-fakes）：讀 boot quiet／Ready Reconfirm 狀態（`ready_reconfirm_attempts` 不在 public 投影）。
    #[cfg(feature = "test-fakes")]
    pub fn quiet_for_test(&self) -> &crate::quiet_gate::QuietState {
        &self.quiet
    }

    /// 只供測試（test-fakes）：在 attach 前建立仍有效的 boot-quiet deadline。
    #[cfg(feature = "test-fakes")]
    pub fn set_boot_quiet_until_for_test(&mut self, deadline: Mono) {
        self.quiet.boot_quiet_until = deadline;
    }

    /// 只供測試（test-fakes）：`last_error` 變更觀察者（spawn 前設定；Task 16 `last_error_history`）。
    #[cfg(feature = "test-fakes")]
    pub fn set_last_error_observer(&mut self, observer: Box<dyn Fn(&str) + Send>) {
        self.last_error_observer = Some(observer);
    }

    /// P5-T16：manager 啟動時還原 `state.json` 的 RELEASED 列（spawn 前呼叫；Python `__init__` 的 released 實體化）。
    pub fn init_released(
        &mut self,
        released_by: Option<String>,
        released_at: Option<String>,
        reason: PyValue,
    ) {
        self.state = SessionState::Released;
        self.released_by = released_by;
        self.released_at = released_at;
        self.released_reason = reason;
    }

    /// P5-T16：還原 RELEASED 動態 session 時沿用 `state.json` 的 session_id（Python `session_id_override`；
    /// template 經 pin／sticky 改變時 sid 不隨之重算）。spawn 前呼叫。
    pub fn init_session_id(&mut self, sid: String) {
        self.sid = sid;
    }

    /// P5-T16：動態 attach 開埠失敗時保留可診斷的 DETACHED session（spawn 前呼叫；`_record_dynamic_open_failure`）。
    pub fn init_detached_error(&mut self, last_error: String, detached_at: String) {
        self.state = SessionState::Detached;
        self.last_error = Some(last_error);
        self.detached_at = Some(detached_at);
    }
}

impl SessionCore {
    /// Two-Phase Drain 第一階段的發起點（Task 12；SC-04、arch §5.5）：`kind` 與 `waiter` 凍結簽章（P6 07 計畫依此呼叫）。
    pub fn begin_stop(&mut self, kind: StopKind, waiter: Option<Reply>, now: Mono) -> Effects {
        let mut effects = Effects(Vec::new());
        // 再探進行中被 drain 搶進：等待者照 Python `attach_session` 的 pending drain 分支回 SESSION_RECOVERING。
        if let Some(run) = self.login_run.take() {
            for (waiter, _) in run.waiters {
                effects.push(Effect::Reply(waiter, Ok(self.session_recovering_reply(now))));
            }
        }
        self.reboot_recovery = None;
        let already_released = kind == StopKind::Release && self.state == SessionState::Released;
        if kind == StopKind::Shutdown {
            self.shutdown_requested = true;
        }
        if let Some(stop) = &mut self.stop {
            if kind == stop.kind || kind == StopKind::Shutdown {
                if kind == StopKind::Release && stop.stalled {
                    if let Some(waiter) = waiter {
                        effects.push(Effect::Reply(waiter, Ok(self.drain_stalled_reply(now))));
                    }
                } else if let Some(waiter) = waiter {
                    stop.waiters.push(waiter);
                }
            } else if matches!(
                kind,
                StopKind::Removed | StopKind::Rebound | StopKind::IoError
            ) {
                stop.post_kind = Some(kind);
            } else if let Some(waiter) = waiter {
                effects.push(Effect::Reply(
                    waiter,
                    Ok(self.session_recovering_reply(now)),
                ));
            }
            return effects;
        }
        if let Some(pending) = &mut self.pending_open_stop {
            if kind == pending.kind || kind == StopKind::Shutdown {
                if kind == StopKind::Release && pending.stalled {
                    if let Some(waiter) = waiter {
                        effects.push(Effect::Reply(waiter, Ok(self.drain_stalled_reply(now))));
                    }
                } else if let Some(waiter) = waiter {
                    pending.waiters.push(waiter);
                }
            } else if matches!(
                kind,
                StopKind::Removed | StopKind::Rebound | StopKind::IoError
            ) {
                pending.post_kind = Some(kind);
            } else if let Some(waiter) = waiter {
                effects.push(Effect::Reply(
                    waiter,
                    Ok(self.session_recovering_reply(now)),
                ));
            }
            return effects;
        }
        let Some(epoch) = self.generations.conn_epoch else {
            if self.state == SessionState::Attaching
                || self.open_real_path.is_some()
                || matches!(&self.pending_commit, Some(PendingCommit::Attach { .. }))
            {
                self.next_stop_id = self.next_stop_id.saturating_add(1);
                let id = StopId(self.next_stop_id);
                let generation = self.generations.bridge_generation;
                self.pending_open_stop = Some(PendingOpenStop {
                    id,
                    generation,
                    kind: kind.clone(),
                    waiters: waiter.into_iter().collect(),
                    started: now,
                    stalled: false,
                    already_released,
                    post_kind: None,
                });
                self.generations.owner_gen = self.generations.owner_gen.saturating_add(1);
                self.generations.fsm_gen = self.generations.fsm_gen.saturating_add(1);
                self.open_generation = self.generations.fsm_gen;
                self.deps.fence.with(|f| {
                    f.active_epoch = None;
                    f.frozen = true;
                    f.owner_gen = self.generations.owner_gen;
                    f.fsm_gen = self.generations.fsm_gen;
                });
                if !already_released {
                    self.set_state(SessionState::Recovering, now);
                }
                self.phase1_detach(&kind, already_released);
                if kind == StopKind::Release {
                    if let Some(pending_attach) = self.pending_attach.take() {
                        effects.push(Effect::Reply(
                            pending_attach.reply,
                            Ok(self.session_recovering_reply(now)),
                        ));
                    }
                }
                effects.push(Effect::Schedule {
                    at: now.saturating_add(
                        crate::resources::DRAIN_TIMEOUT_MS.saturating_mul(1_000_000),
                    ),
                    ev: TimerEvent::DrainTimeout {
                        epoch: generation,
                        stop_id: id,
                    },
                });
                return effects;
            }
            return Effects(self.finish_stop_without_worker(
                kind,
                waiter.into_iter().collect(),
                now,
            ));
        };

        if kind == StopKind::IoError && self.last_error.is_none() {
            self.set_last_error(Some("BRIDGE_DOWN:SERIAL_READ".to_string()), None);
        }

        self.next_stop_id = self.next_stop_id.saturating_add(1);
        let stop_id = StopId(self.next_stop_id);
        let waiters = waiter.into_iter().collect();
        let aborted_op = self
            .foreground
            .as_ref()
            .and_then(|grant| grant.cmd_id.clone().map(|cmd_id| (grant.op_id, cmd_id)));
        self.stop = Some(StopOp {
            id: stop_id,
            kind: kind.clone(),
            epoch,
            waiters,
            started: now,
            stalled: false,
            already_released,
            aborted_op,
            post_kind: None,
        });
        self.generations.owner_gen = self.generations.owner_gen.saturating_add(1);
        self.generations.fsm_gen = self.generations.fsm_gen.saturating_add(1);
        self.deps.fence.with(|f| {
            f.frozen = true;
            f.active_epoch = None;
            f.owner_gen = self.generations.owner_gen;
            f.fsm_gen = self.generations.fsm_gen;
        });
        let code = if kind == StopKind::Shutdown {
            "FLUSHED_BY_SHUTDOWN"
        } else {
            "FLUSHED_BY_RECOVERY"
        };
        let _ = self.queue.drain_all();
        self.deps
            .registry
            .flush_session(&self.sid, code, &self.wall_iso());
        self.clear_interactive();
        if !already_released {
            self.set_state(SessionState::Recovering, now);
        }
        self.phase1_detach(&kind, already_released);
        effects.push(Effect::BeginDrain { stop_id, epoch });
        effects.push(Effect::Schedule {
            at: now.saturating_add(crate::resources::DRAIN_TIMEOUT_MS.saturating_mul(1_000_000)),
            ev: TimerEvent::DrainTimeout { epoch, stop_id },
        });
        effects
    }

    /// Python `_begin_drain_locked` Phase 1（等回條前）：非 shutdown 設 `last_error=<reason>`（Removed／Rebound／
    /// IoError 已由呼叫端設定），清前景欄位並停 log／background capture；`aborted_op` 已抄進 stop，BC-03 不受影響
    /// （審查 F41）。
    fn phase1_detach(&mut self, kind: &StopKind, already_released: bool) {
        let reason = match kind {
            StopKind::Release if !already_released => Some("RELEASED"),
            StopKind::Clear => Some("CLEARED"),
            StopKind::Bind => Some("REBOUND"),
            _ => None,
        };
        if let Some(reason) = reason {
            self.set_last_error(Some(reason.to_string()), None);
        }
        self.foreground = None;
        self.detach_captures();
    }

    fn finish_stop_without_worker(
        &mut self,
        kind: StopKind,
        waiters: Vec<Reply>,
        now: Mono,
    ) -> Vec<Effect> {
        let mut effects = Vec::new();
        self.reattach_pending = false;
        if kind == StopKind::Release {
            self.finish_release(now);
            effects.extend(
                self.persist_release(waiters.into_iter().map(|reply| (reply, false)).collect()),
            );
        } else {
            self.attached_real_path = None;
            self.generations.conn_epoch = None;
            self.queue_registered = false;
            self.foreground = None;
            self.side_ops.clear();
            self.vtty = None;
            self.console_count = 0;
            self.foreground_reboot = false;
            self.login_run = None;
            self.clear_interactive();
            self.detach_captures();
            match &kind {
                StopKind::Clear => self.set_last_error(Some("CLEARED".into()), None),
                StopKind::Removed => {
                    self.device_present = false;
                    self.known_real_path = None;
                    self.set_last_error(Some("DEVICE_REMOVED".into()), None)
                }
                StopKind::Rebound => {
                    self.set_last_error(Some("DEVICE_REBOUND_REQUIRED".into()), None)
                }
                StopKind::IoError if !self.pending_auto_login => self.set_last_error(None, None),
                _ => {}
            }
            self.set_state(SessionState::Detached, now);
            if kind == StopKind::Clear {
                // Python `clear_session` 無 bridge 分支：裝置在線 → 回覆前即 ATTACHING＋`last_error=None`，
                // 再 `_spawn_attach`（審查 F61）；不在線維持 DETACHED＋CLEARED。
                effects.extend(self.request_reattach(now));
                if self.state == SessionState::Attaching {
                    self.set_last_error(None, None);
                }
            }
            for waiter in waiters {
                effects.push(Effect::Reply(waiter, Ok(self.ok_session(now))));
            }
            if kind == StopKind::Shutdown {
                self.shutdown_complete = true;
            }
        }
        effects
    }

    /// Drain 進行中（SC-05 attach 與 manager 的 open 前檢查用；Task 12）。
    pub fn drain_in_progress(&self) -> bool {
        self.stop.is_some() || self.pending_open_stop.is_some()
    }

    /// 只供測試（test-fakes）：session 的 `background_cmd_ids`（#45 D-94 淘汰同步）。
    #[cfg(feature = "test-fakes")]
    pub fn background_cmd_ids_for_test(&self) -> Vec<String> {
        self.background_cmd_ids.clone()
    }

    /// 只供測試（test-fakes）：`pending_auto_login` 旗標（reboot 進 RECOVERING 斷言）。
    #[cfg(feature = "test-fakes")]
    pub fn pending_auto_login_for_test(&self) -> bool {
        self.pending_auto_login
    }

    #[cfg(feature = "test-fakes")]
    pub fn release_persist_pending_for_test(&self) -> bool {
        self.release_persist_pending
    }

    #[cfg(feature = "test-fakes")]
    pub fn set_pending_auto_login_for_test(&mut self, value: bool) {
        self.pending_auto_login = value;
    }

    pub fn shutdown_complete(&self) -> bool {
        self.shutdown_complete
    }

    fn next_commit(&mut self) -> u64 {
        self.next_commit_id = self.next_commit_id.saturating_add(1);
        self.next_commit_id
    }

    fn persist_release(&mut self, waiters: Vec<(Reply, bool)>) -> Vec<Effect> {
        if let Some(PendingCommit::Release {
            waiters: pending, ..
        }) = &mut self.pending_commit
        {
            pending.extend(waiters);
            return Vec::new();
        }
        let mut waiters = waiters.into_iter();
        let owner = waiters.next();
        let waiters = waiters.collect();
        let id = self.next_commit();
        let release = self.pending_release.as_ref();
        let row = ReleasedRow {
            by_id: PyValue::from(self.profile.device_by_id.clone()),
            released_by: PyValue::from(
                release
                    .map(|value| value.source.clone())
                    .unwrap_or_else(|| "cli".into()),
            ),
            released_at: PyValue::from(self.released_at.clone().unwrap_or_else(|| self.wall_iso())),
            reason: release
                .map(|value| value.reason.clone())
                .unwrap_or(PyValue::Null),
        };
        self.pending_commit = Some(PendingCommit::Release {
            id,
            owner,
            waiters,
        });
        vec![Effect::Persist {
            commit_id: id,
            delta: StateDelta::Released {
                sid: self.sid.clone(),
                row,
            },
        }]
    }

    fn retry_release_persistence(&mut self, reply: Reply) -> Vec<Effect> {
        self.persist_release(vec![(reply, true)])
    }

    fn state_committed(
        &mut self,
        commit_id: u64,
        result: Result<(), PalError>,
        now: Mono,
    ) -> Vec<Effect> {
        let Some(pending) = self.pending_commit.take() else {
            return Vec::new();
        };
        let matches = match &pending {
            PendingCommit::Release { id, .. }
            | PendingCommit::Attach { id, .. }
            | PendingCommit::AttachFailed { id, .. } => *id == commit_id,
        };
        if !matches {
            self.pending_commit = Some(pending);
            return Vec::new();
        }

        match pending {
            PendingCommit::Release {
                owner, waiters, ..
            } => match result {
                Ok(()) => {
                    self.release_persist_pending = false;
                    if self.shutdown_requested {
                        self.shutdown_complete = true;
                    }
                    owner
                        .into_iter()
                        .chain(waiters)
                        .map(|(reply, already_released)| {
                            Effect::Reply(
                                reply,
                                Ok(self.release_success_reply(now, already_released)),
                            )
                        })
                        .collect()
                }
                Err(error) => {
                    self.release_persist_pending = true;
                    let mut effects: Vec<_> = owner
                        .into_iter()
                        .map(|(reply, _)| {
                            Effect::Reply(
                                reply,
                                Err(PyErr::value_error(format!(
                                    "state.json commit failed: {error:?}"
                                ))),
                            )
                        })
                        .collect();
                    let mut waiters = waiters.into_iter();
                    if let Some(next_owner) = waiters.next() {
                        effects.extend(self.persist_release(
                            std::iter::once(next_owner).chain(waiters).collect(),
                        ));
                    } else if self.shutdown_requested {
                        self.shutdown_complete = true;
                    }
                    effects
                }
            },
            PendingCommit::Attach {
                opened,
                reply,
                previous_state,
                ..
            } => match result {
                Ok(()) => {
                    if let Some(pending_stop) = self.pending_open_stop.take() {
                        if pending_stop.kind == StopKind::Release && pending_stop.already_released {
                            self.release_persist_pending = true;
                        } else {
                            self.release_persist_pending = false;
                            self.released_by = None;
                            self.released_at = None;
                            self.released_reason = PyValue::Null;
                            if pending_stop.kind != StopKind::Release {
                                self.pending_release = None;
                            }
                        }
                        self.pending_attach = None;
                        let mut effects = self.drain_opened_bridge(pending_stop, opened, now);
                        effects.push(Effect::Reply(
                            reply,
                            Ok(self.session_recovering_reply(now)),
                        ));
                        return effects;
                    }
                    self.release_persist_pending = false;
                    self.released_by = None;
                    self.released_at = None;
                    self.released_reason = PyValue::Null;
                    self.pending_release = None;
                    self.pending_attach = None;
                    self.state = SessionState::Attaching;
                    let mut effects = self.adopt_opened_bridge(opened, now);
                    let reply = Effect::Reply(reply, Ok(self.ok_session(now)));
                    let start_login = effects
                        .iter()
                        .position(|effect| matches!(effect, Effect::StartLogin { .. }));
                    effects.insert(start_login.unwrap_or(effects.len()), reply);
                    effects
                }
                Err(error) => {
                    if let Some(pending_stop) = self.pending_open_stop.take() {
                        self.pending_attach = None;
                        let mut effects = self.drain_opened_bridge(pending_stop, opened, now);
                        effects.push(Effect::Reply(
                            reply,
                            Err(PyErr::value_error(format!(
                                "state.json commit failed: {error:?}"
                            ))),
                        ));
                        return effects;
                    }
                    self.pending_attach = None;
                    self.state = previous_state;
                    let mut effects = self.retire_opened_bridge(opened);
                    effects.push(Effect::Reply(
                        reply,
                        Err(PyErr::value_error(format!(
                            "state.json commit failed: {error:?}"
                        ))),
                    ));
                    effects
                }
            },
            PendingCommit::AttachFailed {
                reply,
                previous_state,
                ..
            } => match result {
                Ok(()) => {
                    self.release_persist_pending = false;
                    self.released_by = None;
                    self.released_at = None;
                    self.released_reason = PyValue::Null;
                    self.pending_release = None;
                    self.set_state(SessionState::Detached, now);
                    vec![Effect::Reply(reply, Ok(self.ok_session(now)))]
                }
                Err(error) => {
                    self.state = previous_state;
                    vec![Effect::Reply(
                        reply,
                        Err(PyErr::value_error(format!(
                            "state.json commit failed: {error:?}"
                        ))),
                    )]
                }
            },
        }
    }
}

impl SessionCore {
    fn wall_iso(&self) -> String {
        (self.deps.wall_iso)()
    }

    fn is_passthrough(&self) -> bool {
        self.profile.template.platform == "passthrough"
    }

    /// Python `_mark_missing_devices_locked`：非 RELEASED、無 bridge（含 drain 中）時才標記。
    fn mark_missing_device(&mut self, now: Mono) {
        self.device_present = false;
        if self.state == SessionState::Released
            || self.generations.conn_epoch.is_some()
            || self.stop.is_some()
        {
            return;
        }
        self.reattach_pending = false;
        self.set_state(SessionState::Detached, now);
        if self.last_error.is_none() {
            self.set_last_error(Some("DEVICE_NOT_FOUND".into()), None);
        }
        if self.detached_at.is_none() {
            self.detached_at = Some(self.wall_iso());
        }
    }

    fn set_state(&mut self, state: SessionState, now: Mono) {
        if self.state != state {
            self.last_state_change_at = Some(self.wall_iso());
            if state == SessionState::Detached {
                self.detached_at = Some(self.wall_iso());
            }
        }
        if state == SessionState::Attached && self.is_passthrough() {
            if self.state != SessionState::Attached || self.quiet_since.is_none() {
                self.quiet_since = Some(now);
            }
        } else if state != SessionState::Attached {
            self.quiet_since = None;
        }
        self.state = state;
    }

    fn set_last_error(&mut self, error: Option<String>, detail: Option<String>) {
        if self.last_error != error {
            self.last_error_detail = None;
            #[cfg(feature = "test-fakes")]
            if let (Some(observer), Some(value)) = (self.last_error_observer.as_ref(), error.as_deref()) {
                observer(value);
            }
        }
        self.last_error = error;
        if detail.is_some() {
            self.last_error_detail = detail;
        }
    }

    fn ok_session(&self, now: Mono) -> PyDict {
        pydict! {
            "ok" => true,
            "session" => self.public(now, &self.public_ctx())
        }
    }

    fn error_session(&self, code: &str, now: Mono) -> PyDict {
        pydict! {
            "ok" => false,
            "error_code" => code,
            "session" => self.public(now, &self.public_ctx())
        }
    }

    fn session_recovering_reply(&self, now: Mono) -> PyDict {
        pydict! {
            "ok" => false,
            "error_code" => "SESSION_RECOVERING",
            "drain_stalled" => self.drain_stalled,
            "drain_timeout_epoch" => self.drain_timeout_epoch.map(|epoch| py_int(epoch as i128)).unwrap_or(PyValue::Null),
            "session" => self.public(now, &self.public_ctx())
        }
    }

    fn drain_stalled_reply(&self, now: Mono) -> PyDict {
        pydict! {
            "ok" => false,
            "error_code" => "DRAIN_STALLED",
            "drain_stalled" => true,
            "drain_timeout_epoch" => self.drain_timeout_epoch.map(|epoch| py_int(epoch as i128)).unwrap_or(PyValue::Null),
            "session" => self.public(now, &self.public_ctx())
        }
    }

    fn release_success_reply(&self, now: Mono, already_released: bool) -> PyDict {
        if already_released {
            let mut result = self.ok_session(now);
            result.insert("already_released", PyValue::from(true));
            return result;
        }
        let (closed_consoles, aborted_cmd) = self
            .pending_release
            .as_ref()
            .map(|release| (release.closed_consoles, release.aborted_cmd))
            .unwrap_or((0, false));
        pydict! {
            "ok" => true,
            "session" => self.public(now, &self.public_ctx()),
            "closed_consoles" => py_int(closed_consoles),
            "aborted_cmd" => aborted_cmd
        }
    }

    /// P6 Task 1：Slice 2.2 console mailbox 事件（P6 Task 6／9／10 交付本體）。
    fn console_event(&mut self, _ev: MailboxItem, _now: Mono) -> Vec<Effect> {
        todo!("P6 Task 9")
    }

    /// P6 增補 #9：console RPC（P6 Task 10 交付本體）。
    fn console_request(&mut self, _req: SessionRequest, _reply: Reply, _now: Mono) -> Vec<Effect> {
        todo!("P6 Task 10")
    }

    fn handle_request(&mut self, req: SessionRequest, reply: Reply, now: Mono) -> Vec<Effect> {
        match req {
            SessionRequest::GetState => vec![Effect::Reply(reply, Ok(self.ok_session(now)))],
            SessionRequest::Activity => {
                let mut result = self.ok_session(now);
                result.insert(
                    "activity_classification",
                    PyValue::from(self.activity_classification(now)),
                );
                vec![Effect::Reply(reply, Ok(result))]
            }
            SessionRequest::Submit(req) => self.submit(req, reply, now),
            // P6 增補 #9：console RPC（行為由 P6 Task 10 交付）。
            req @ (SessionRequest::ConsoleAttach { .. }
            | SessionRequest::ConsoleDetach { .. }
            | SessionRequest::ConsoleList) => self.console_request(req, reply, now),
            SessionRequest::Release { source, reason } => {
                if self.state == SessionState::Released {
                    if self
                        .pending_open_stop
                        .as_ref()
                        .is_some_and(|pending| pending.already_released)
                    {
                        return self.begin_stop(StopKind::Release, Some(reply), now).0;
                    }
                    if matches!(
                        &self.pending_commit,
                        Some(PendingCommit::Attach { .. })
                    ) {
                        return self.begin_stop(StopKind::Release, Some(reply), now).0;
                    }
                    if self.pending_attach.is_some() {
                        let mut effects = Vec::new();
                        if let Some(pending) = self.pending_attach.take() {
                            effects.push(Effect::Reply(
                                pending.reply,
                                Ok(self.session_recovering_reply(now)),
                            ));
                        }
                        effects.extend(self.begin_stop(StopKind::Release, Some(reply), now).0);
                        return effects;
                    }
                    if self.release_persist_pending
                        || matches!(&self.pending_commit, Some(PendingCommit::Release { .. }))
                    {
                        return self.retry_release_persistence(reply);
                    }
                    let mut result = self.ok_session(now);
                    result.insert("already_released", PyValue::from(true));
                    return vec![Effect::Reply(reply, Ok(result))];
                }
                if self.state == SessionState::Flashing {
                    let mut result = self.error_session("FLASHING_BUSY", now);
                    result.insert("selector", PyValue::from(self.profile.com.clone()));
                    return vec![Effect::Reply(reply, Ok(result))];
                }
                let another_drain_is_active = self
                    .stop
                    .as_ref()
                    .is_some_and(|stop| stop.kind != StopKind::Release)
                    || self
                        .pending_open_stop
                        .as_ref()
                        .is_some_and(|pending| pending.kind != StopKind::Release);
                if another_drain_is_active {
                    return vec![Effect::Reply(reply, Ok(self.session_recovering_reply(now)))];
                }
                if self.stop.is_some() || self.pending_open_stop.is_some() {
                    return self.begin_stop(StopKind::Release, Some(reply), now).0;
                }
                self.pending_release = Some(PendingRelease {
                    source,
                    reason,
                    closed_consoles: self.console_count as i128,
                    aborted_cmd: self.foreground_busy(),
                });
                self.console_count = 0;
                self.begin_stop(StopKind::Release, Some(reply), now).0
            }
            SessionRequest::InteractiveOpen {
                owner,
                timeout_s,
                command,
                allow_attached,
            } => self.interactive_open(owner, timeout_s, command, allow_attached, reply, now),
            SessionRequest::InteractiveClose { interactive_id } => {
                vec![Effect::Reply(reply, Ok(self.interactive_close(&interactive_id)))]
            }
            SessionRequest::CancelQueued { cmd_id } => {
                self.queue.remove(&cmd_id);
                // Python `revoke_tx_op`：取消的命令若正是前景 op，撤銷其 TX permit——之後以該 op 提交的
                // TX（含 gate 外等待中的尾段）一律 Cancelled、零 TX（parity-amendments-2026-10-26 #52）。
                if let Some(grant) = self
                    .foreground
                    .as_ref()
                    .filter(|grant| grant.cmd_id.as_deref() == Some(cmd_id.as_str()))
                {
                    self.deps.fence.revoke_ops(&[grant.op_id]);
                }
                vec![Effect::Reply(reply, Ok(pydict! { "ok" => true }))]
            }
            SessionRequest::Clear => self.begin_stop(StopKind::Clear, Some(reply), now).0,
            SessionRequest::Recover { timeout_s, force } => {
                self.recover_request(timeout_s, force, reply, now)
            }
            SessionRequest::DeviceAttach { force } => {
                self.credentials_warned = false;
                self.attach_request(force, false, reply, now)
            }
            SessionRequest::SessionAttach => {
                self.credentials_warned = false;
                self.attach_request(false, true, reply, now)
            }
            SessionRequest::Bind { .. } if self.drain_in_progress() => {
                vec![Effect::Reply(reply, Ok(self.session_recovering_reply(now)))]
            }
            SessionRequest::Bind { by_id } => self.bind_request(by_id, reply, now),
            SessionRequest::SetProfile {
                profile,
                profile_source,
            } => {
                if self.generations.conn_epoch.is_some()
                    || self.drain_in_progress()
                    || self.open_real_path.is_some()
                {
                    return vec![Effect::Reply(reply, Ok(self.session_recovering_reply(now)))];
                }
                self.sid = format!("{}:{}", profile.template.profile_name, profile.com);
                self.profile = profile;
                self.profile_source = profile_source;
                vec![Effect::Reply(reply, Ok(self.ok_session(now)))]
            }
            SessionRequest::DeviceOnline { by_id, real_path } => {
                // 回覆先於 OpenBridge（executor 依序執行；開埠可能阻塞）。
                let opened = self.device_attached(&by_id, &real_path, now);
                let mut effects = vec![Effect::Reply(reply, Ok(self.ok_session(now)))];
                effects.extend(opened);
                effects
            }
            SessionRequest::MarkAttachStalled { epoch } => {
                self.set_state(SessionState::Recovering, now);
                self.drain_stalled = true;
                self.drain_timeout_epoch = Some(epoch);
                let mut response = self.drain_stalled_reply(now);
                response.insert("error_code", PyValue::from("SESSION_RECOVERING"));
                vec![Effect::Reply(reply, Ok(response))]
            }
            #[cfg(feature = "test-fakes")]
            SessionRequest::QueueLensForTest => vec![Effect::Reply(
                reply,
                Ok(pydict! {
                    "len" => PyValue::from(self.queue.len() as i64),
                    "physical_len" => PyValue::from(self.queue.physical_len() as i64),
                }),
            )],
            #[cfg(feature = "test-fakes")]
            SessionRequest::ForceStateForTest(state) => {
                self.set_state(state, now);
                vec![Effect::Reply(reply, Ok(self.ok_session(now)))]
            }
            SessionRequest::InteractiveSend {
                interactive_id,
                data,
                encoding,
            } => self.interactive_send(interactive_id, &data, &encoding, reply, now),
            SessionRequest::InteractiveStatus {
                interactive_id,
                screen_chars,
            } => vec![Effect::Reply(
                reply,
                Ok(self.interactive_status(&interactive_id, screen_chars, now)),
            )],
            SessionRequest::SelfTest {
                timeout_s,
                strict_human_lock,
            } => self.self_test_request(timeout_s, strict_human_lock, reply, now),
            SessionRequest::LogStart => vec![Effect::Reply(reply, self.log_start(now))],
            SessionRequest::LogStop => vec![Effect::Reply(reply, Ok(self.log_stop(now)))],
            SessionRequest::LogStatus => vec![Effect::Reply(reply, Ok(self.log_status(now)))],
            SessionRequest::BackgroundResult {
                cmd_id,
                from_chunk,
                limit,
            } => vec![Effect::Reply(
                reply,
                match self.background.get_mut(&cmd_id) {
                    Some(capture) => {
                        let result = capture.result(from_chunk, limit, now);
                        self.sync_bg(&cmd_id);
                        result
                    }
                    None => Ok(
                        pydict! { "ok" => false, "error_code" => "CMD_NOT_FOUND", "cmd_id" => cmd_id },
                    ),
                },
            )],
            SessionRequest::ResultTail {
                cmd_id,
                from_chunk,
                limit,
            } => vec![Effect::Reply(
                reply,
                self.result_tail(&cmd_id, from_chunk, limit, now),
            )],
        }
    }

    fn submit(&mut self, mut req: SubmitReq, reply: Reply, now: Mono) -> Vec<Effect> {
        if self.state == SessionState::Recovering {
            return vec![Effect::Reply(
                reply,
                Ok(self.error_session("SESSION_RECOVERING", now)),
            )];
        }
        if self.state == SessionState::Flashing {
            return vec![Effect::Reply(
                reply,
                Ok(self.error_session("FLASHING_BUSY", now)),
            )];
        }
        if self.state != SessionState::Ready {
            if self.state == SessionState::Attached
                && !self.is_passthrough()
                && !self.profile.template.command_capable()
            {
                let mut result =
                    pydict! { "ok" => false, "error_code" => "PROFILE_NOT_COMMAND_CAPABLE" };
                result.insert(
                    "hint",
                    PyValue::from("此 profile 僅支援 console。command_capable 純粹由 profile 的 ready_probe 決定，與 session state 或 console 是否被佔用無關。要下命令：先 `serialwrap session self-test --selector <COM>` 看有無 suggested_profile，再 `serialwrap session pin --selector <COM> --profile <template>` 釘定、`serialwrap session clear --selector <COM>` 套用；或為該 profile 設定 ready_probe。"),
                );
                result.insert(
                    "session",
                    PyValue::Dict(self.public(
                        now,
                        &self.public_ctx(),
                    )),
                );
                return vec![Effect::Reply(reply, Ok(result))];
            }
            if self.state == SessionState::Attached && self.is_passthrough() {
                let elapsed = self
                    .quiet_since
                    .map(|t| now.saturating_sub(t) as f64 / 1_000_000_000.0)
                    .unwrap_or(0.0);
                let ready_in = py_round((PASSTHROUGH_READY_QUIET_S - elapsed).max(0.0), 1);
                let mut result = pydict! { "ok" => false, "error_code" => "SESSION_NOT_READY" };
                result.insert("passthrough_ready_in_s", PyValue::from(ready_in));
                result.insert(
                    "session",
                    PyValue::Dict(self.public(
                        now,
                        &self.public_ctx(),
                    )),
                );
                return vec![Effect::Reply(reply, Ok(result))];
            }
            return vec![Effect::Reply(
                reply,
                Ok(self.error_session("SESSION_NOT_READY", now)),
            )];
        }

        if !self.is_passthrough() {
            // `service._resolve_session_id` 的 gate 段（RPC 的 `human:` source 一律視同 agent，DF-02）。
            self.expire_reconfirm(now);
            if self.quiet.failed || self.quiet.quiet_active(now) || self.quiet.pending {
                let public = self.public(now, &self.public_ctx());
                if let Some(result) = crate::quiet_gate::submit_gate_reject(&public, &req.source) {
                    return vec![Effect::Reply(reply, Ok(result))];
                }
            }
        }

        req.session_id = self.sid.clone();
        let cmd_id = self.next_uuid();
        let admitted =
            match self
                .deps
                .registry
                .admit(req, self.queue_registered, cmd_id, &self.wall_iso())
            {
                Ok(admitted) => admitted,
                Err(mut result) => {
                    if result.get("session").is_none() {
                        result.insert(
                            "session",
                            PyValue::Dict(self.public(
                                now,
                                &self.public_ctx(),
                            )),
                        );
                    }
                    return vec![Effect::Reply(reply, Ok(result))];
                }
            };
        self.queue.push(admitted.queued, &self.deps.registry);
        let mut effects = vec![Effect::Reply(reply, Ok(admitted.response))];
        effects.extend(self.start_next(now));
        effects
    }

    fn attach_request(
        &mut self,
        force: bool,
        session_attach: bool,
        reply: Reply,
        now: Mono,
    ) -> Vec<Effect> {
        if self.shutdown_requested {
            return vec![Effect::Reply(
                reply,
                Ok(self.session_recovering_reply(now)),
            )];
        }
        // P5-T16：`attach_session` 的未綁定早退（Python 同樣只在 device_by_id 為空時；config 會丟棄無 by_id 的 target）。
        if session_attach && self.profile.device_by_id.is_empty() {
            return vec![Effect::Reply(
                reply,
                Ok(self.error_session("DEVICE_NOT_BOUND", now)),
            )];
        }
        if session_attach
            && self.state == SessionState::Released
            && !self
                .pending_open_stop
                .as_ref()
                .is_some_and(|pending| pending.already_released)
        {
            return vec![Effect::Reply(
                reply,
                Ok(pydict! {
                    "ok" => true,
                    "released" => true,
                    "recommended_action" => "device_attach",
                    "session" => self.public(now, &self.public_ctx())
                }),
            )];
        }
        if self.state == SessionState::Flashing {
            let mut response = self.error_session("FLASHING_BUSY", now);
            response.insert("selector", PyValue::from(self.profile.com.clone()));
            return vec![Effect::Reply(reply, Ok(response))];
        }
        if self.drain_in_progress()
            || self.pending_attach.is_some()
            || self.open_real_path.is_some()
            || self.state == SessionState::Recovering
            || (self.state == SessionState::Attaching && self.generations.conn_epoch.is_none())
            || matches!(
                &self.pending_commit,
                Some(PendingCommit::Release { .. } | PendingCommit::Attach { .. })
            )
        {
            return vec![Effect::Reply(reply, Ok(self.session_recovering_reply(now)))];
        }
        if self.generations.conn_epoch.is_some() {
            if session_attach {
                if self.is_passthrough() && self.state == SessionState::Attached {
                    return vec![Effect::Reply(reply, Ok(self.ok_session(now)))];
                }
                let human_lease = self.lease.as_ref().is_some_and(InteractiveLease::is_human);
                if !human_lease && self.state == SessionState::Attached {
                    return self.reprobe_existing_bridge(reply, ReprobeWaiter::Attach, now);
                }
                if self.is_passthrough() {
                    return vec![Effect::Reply(reply, Ok(self.ok_session(now)))];
                }
                return vec![Effect::Reply(reply, Ok(self.attach_state_result(now)))];
            }
            let mut response = self.ok_session(now);
            response.insert("already_attached", PyValue::Bool(true));
            return vec![Effect::Reply(reply, Ok(response))];
        }
        if self.release_persist_pending {
            return vec![Effect::Reply(reply, Ok(self.session_recovering_reply(now)))];
        }
        if !self.device_present && session_attach {
            // P5-T16：`attach_session` 無 bridge 且裝置不在快照 → DETACHED＋DEVICE_NOT_FOUND（Python 1:1）。
            self.set_last_error(Some("DEVICE_NOT_FOUND".into()), None);
            self.detached_at = Some(self.wall_iso());
            self.set_state(SessionState::Detached, now);
            return vec![Effect::Reply(
                reply,
                Ok(self.error_session("DEVICE_NOT_FOUND", now)),
            )];
        }
        if !self.device_present {
            return vec![Effect::Reply(
                reply,
                Ok(pydict! {
                    "ok" => false,
                    "error_code" => "DEVICE_NOT_PRESENT",
                    "selector" => self.profile.com.clone(),
                    "device_by_id" => self.profile.device_by_id.clone()
                }),
            )];
        }
        let real_path = self
            .known_real_path
            .clone()
            .unwrap_or_else(|| self.profile.device_by_id.clone());
        let previous_state = self.state;
        self.generations.fsm_gen = self.generations.fsm_gen.saturating_add(1);
        self.open_generation = self.generations.fsm_gen;
        self.open_real_path = Some(real_path.clone());
        self.pending_attach = Some(PendingAttach {
            generation: self.open_generation,
            by_id: self.profile.device_by_id.clone(),
            real_path: real_path.clone(),
            reply,
            previous_state,
        });
        vec![Effect::OpenBridge {
            dev: DevicePath(real_path),
            cfg: self.serial_config(),
            precheck_holders: !force,
        }]
    }

    /// P5-T16：`bind_session`（session 層）：同值冪等、有 bridge 先 `StopKind::Bind` drain、否則立即改綁。
    /// 改綁後一律 DETACHED＋`DEVICE_NOT_FOUND`；裝置在線時由 manager 隨後送 `DeviceAttached` 進 ATTACHING。
    fn bind_request(&mut self, by_id: String, reply: Reply, now: Mono) -> Vec<Effect> {
        if self.state == SessionState::Flashing {
            // Python `bind_session` 鍵序：ok／error_code／selector／session。
            return vec![Effect::Reply(
                reply,
                Ok(pydict! {
                    "ok" => false,
                    "error_code" => "FLASHING_BUSY",
                    "selector" => self.profile.com.clone(),
                    "session" => self.public(now, &self.public_ctx())
                }),
            )];
        }
        if self.generations.conn_epoch.is_some()
            && self.profile.device_by_id == by_id
            && matches!(self.state, SessionState::Ready | SessionState::Attached)
        {
            return vec![Effect::Reply(
                reply,
                Ok(pydict! {
                    "ok" => true,
                    "already_bound" => true,
                    "session" => self.public(now, &self.public_ctx())
                }),
            )];
        }
        if self.open_real_path.is_some()
            || self.pending_attach.is_some()
            || matches!(&self.pending_commit, Some(PendingCommit::Attach { .. }))
        {
            return vec![Effect::Reply(reply, Ok(self.session_recovering_reply(now)))];
        }
        if self.generations.conn_epoch.is_some() {
            self.pending_bind = Some(by_id);
            return self.begin_stop(StopKind::Bind, Some(reply), now).0;
        }
        self.apply_bind(by_id, now);
        vec![Effect::Reply(reply, Ok(self.ok_session(now)))]
    }

    /// 改綁的狀態套用（離開 RELEASED、清 released metadata、裝置視為未在線）。
    fn apply_bind(&mut self, target: String, now: Mono) {
        if self.state == SessionState::Released {
            self.release_persist_pending = false;
            self.pending_release = None;
        }
        self.released_by = None;
        self.released_at = None;
        self.released_reason = PyValue::Null;
        self.profile.device_by_id = target;
        self.device_present = false;
        self.known_real_path = None;
        self.set_last_error(Some("DEVICE_NOT_FOUND".into()), None);
        self.set_state(SessionState::Detached, now);
    }

    fn serial_config(&self) -> SerialConfig {
        let uart = &self.profile.template.uart;
        SerialConfig::from_uart_profile(
            pyint_to_u32(&uart.baud, 115_200),
            pyint_to_u8(&uart.data_bits, 8),
            &uart.parity,
            pyint_to_u8(&uart.stop_bits, 1),
            &uart.flow_control,
            uart.xonxoff,
        )
    }

    fn device_attached(&mut self, by_id: &str, real_path: &str, now: Mono) -> Vec<Effect> {
        if by_id != self.profile.device_by_id {
            return Vec::new();
        }
        self.device_present = true;
        self.known_real_path = Some(real_path.to_string());
        if self.shutdown_requested {
            return Vec::new();
        }
        if self.state == SessionState::Released {
            return Vec::new();
        }
        let awaiting = self.reattach_pending && self.state == SessionState::Attaching;
        if self.state != SessionState::Detached && !awaiting {
            return Vec::new();
        }
        self.reattach_pending = false;
        self.set_last_error(None, None);
        self.generations.fsm_gen = self.generations.fsm_gen.saturating_add(1);
        self.open_generation = self.generations.fsm_gen;
        self.open_real_path = Some(real_path.to_string());
        self.set_state(SessionState::Attaching, now);
        vec![Effect::OpenBridge {
            dev: DevicePath(real_path.to_string()),
            cfg: self.serial_config(),
            precheck_holders: false,
        }]
    }

    fn bridge_opened(
        &mut self,
        _epoch_hint: u64,
        result: Result<OpenedBridge, OpenFailure>,
        now: Mono,
    ) -> Vec<Effect> {
        match result {
            Ok(opened) => {
                if let Some(pending) = self.pending_open_stop.take() {
                    return self.drain_opened_bridge(pending, opened, now);
                }

                let attach_matches = self.pending_attach.as_ref().is_none_or(|pending| {
                    pending.generation == self.open_generation
                        && pending.real_path == opened.real_path
                        && pending.by_id == self.profile.device_by_id
                });
                let path_matches = self
                    .open_real_path
                    .as_deref()
                    .is_some_and(|path| path == opened.real_path)
                    && self
                        .known_real_path
                        .as_deref()
                        .is_none_or(|path| path == opened.real_path);
                if !attach_matches
                    || !path_matches
                    || self.state != SessionState::Attaching && self.pending_attach.is_none()
                {
                    let mut effects = self.retire_opened_bridge(opened);
                    if let Some(pending) = self.pending_attach.take() {
                        self.open_real_path = None;
                        effects.push(Effect::Reply(
                            pending.reply,
                            Ok(self.session_recovering_reply(now)),
                        ));
                    } else if self.state == SessionState::Attaching
                        && !path_matches
                        && !self.shutdown_requested
                    {
                        // #50（P5-T16）：attach 收尾時仍需 attach（Python `_spawn_attach` finally 再 spawn）：
                        // 裝置於開啟途中改指 → 以目前 known_real_path 再送一次 OpenBridge；裝置已不在則 DETACHED。
                        self.open_real_path = None;
                        effects.extend(self.reopen_known_device(now));
                    }
                    return effects;
                }

                self.open_real_path = None;
                if let Some(pending) = self.pending_attach.take() {
                    let id = self.next_commit();
                    self.pending_commit = Some(PendingCommit::Attach {
                        id,
                        opened,
                        reply: pending.reply,
                        previous_state: pending.previous_state,
                    });
                    return vec![Effect::Persist {
                        commit_id: id,
                        delta: StateDelta::Unreleased {
                            sid: self.sid.clone(),
                        },
                    }];
                }
                self.adopt_opened_bridge(opened, now)
            }
            Err(failure) => {
                let failure_error = open_failure_last_error(&failure);
                if let Some(pending) = self.pending_open_stop.take() {
                    self.open_real_path = None;
                    self.finish_pending_open_stop(pending, now)
                } else if let Some(pending) = self.pending_attach.take() {
                    self.open_real_path = None;
                    self.attach_open_failed(pending, failure, failure_error, now)
                } else if self.state == SessionState::Attaching {
                    self.set_last_error(Some(failure_error), None);
                    self.attached_real_path = None;
                    self.open_real_path = None;
                    self.set_state(SessionState::Detached, now);
                    Vec::new()
                } else {
                    Vec::new()
                }
            }
        }
    }

    fn drain_opened_bridge(
        &mut self,
        pending: PendingOpenStop,
        opened: OpenedBridge,
        now: Mono,
    ) -> Vec<Effect> {
        let id = pending.id;
        let kind = pending.kind.clone();
        let waiters = pending.waiters;
        let started = pending.started;
        let stalled = pending.stalled;
        let already_released = pending.already_released;
        let post_kind = pending.post_kind;
        let OpenedBridge {
            epoch,
            worker,
            real_path,
            ..
        } = opened;
        self.generations.conn_epoch = Some(epoch);
        self.attached_real_path = None;
        self.open_real_path = None;
        self.stop = Some(StopOp {
            id,
            kind,
            epoch,
            waiters,
            started,
            stalled,
            already_released,
            aborted_op: None,
            post_kind,
        });
        self.deps.fence.with(|f| {
            f.active_epoch = None;
            f.frozen = true;
            f.owner_gen = self.generations.owner_gen;
            f.fsm_gen = self.generations.fsm_gen;
        });
        if !already_released {
            self.set_state(SessionState::Recovering, now);
        }
        vec![
            Effect::AdoptBridge {
                epoch,
                worker,
                real_path,
            },
            Effect::BeginDrain {
                stop_id: id,
                epoch,
            },
            Effect::Schedule {
                at: started.saturating_add(
                    crate::resources::DRAIN_TIMEOUT_MS.saturating_mul(1_000_000),
                ),
                ev: TimerEvent::DrainTimeout { epoch, stop_id: id },
            },
        ]
    }

    fn finish_pending_open_stop(&mut self, pending: PendingOpenStop, now: Mono) -> Vec<Effect> {
        self.drain_stalled = false;
        if pending.kind == StopKind::Release {
            if pending.already_released {
                self.set_state(SessionState::Released, now);
                return pending
                    .waiters
                    .into_iter()
                    .map(|waiter| {
                        Effect::Reply(waiter, Ok(self.release_success_reply(now, true)))
                    })
                    .collect();
            }
            self.finish_release(now);
            return self.persist_release(
                pending
                    .waiters
                    .into_iter()
                    .map(|reply| (reply, false))
                    .collect(),
            );
        }
        self.attached_real_path = None;
        self.generations.conn_epoch = None;
        self.queue_registered = false;
        let kind = pending.post_kind.unwrap_or(pending.kind);
        match kind {
            StopKind::Removed => {
                self.device_present = false;
                self.known_real_path = None;
                self.set_last_error(Some("DEVICE_REMOVED".into()), None)
            }
            StopKind::Rebound => {
                self.set_last_error(Some("DEVICE_REBOUND_REQUIRED".into()), None)
            }
            StopKind::IoError if !self.pending_auto_login => self.set_last_error(None, None),
            StopKind::Clear => self.set_last_error(Some("CLEARED".into()), None),
            _ => {}
        }
        self.set_state(SessionState::Detached, now);
        let effects: Vec<_> = pending
            .waiters
            .into_iter()
            .map(|waiter| Effect::Reply(waiter, Ok(self.ok_session(now))))
            .collect();
        if self.shutdown_requested {
            self.shutdown_complete = true;
        }
        effects
    }

    fn retire_opened_bridge(&mut self, opened: OpenedBridge) -> Vec<Effect> {
        self.next_stop_id = self.next_stop_id.saturating_add(1);
        let stop_id = StopId(self.next_stop_id);
        let epoch = opened.epoch;
        self.retired_stops.push_back(RetiredStop {
            epoch,
            id: stop_id,
            aborted_op: None,
        });
        while self.retired_stops.len() > 32 {
            self.retired_stops.pop_front();
        }
        vec![
            Effect::AdoptBridge {
                epoch,
                worker: opened.worker,
                real_path: opened.real_path,
            },
            Effect::BeginDrain { stop_id, epoch },
        ]
    }

    fn attach_open_failed(
        &mut self,
        pending: PendingAttach,
        failure: OpenFailure,
        failure_error: String,
        now: Mono,
    ) -> Vec<Effect> {
        match failure {
            OpenFailure::Held(holder) | OpenFailure::AccessDenied(holder) => {
                self.state = pending.previous_state;
                let holder_unknown = holder.pids.is_empty();
                let mut response = self.error_session("DEVICE_STILL_HELD", now);
                response.insert(
                    "pids",
                    PyValue::List(
                        holder
                            .pids
                            .into_iter()
                            .map(|pid| py_int(pid as i128))
                            .collect(),
                    ),
                );
                response.insert("holder_unknown", PyValue::Bool(holder_unknown));
                response.insert("selector", PyValue::from(self.profile.com.clone()));
                vec![Effect::Reply(pending.reply, Ok(response))]
            }
            OpenFailure::NotFound => {
                self.state = pending.previous_state;
                let mut response = self.error_session("DEVICE_NOT_PRESENT", now);
                response.insert("selector", PyValue::from(self.profile.com.clone()));
                response.insert("device_by_id", PyValue::from(pending.by_id));
                vec![Effect::Reply(pending.reply, Ok(response))]
            }
            OpenFailure::Other(_) => {
                self.open_real_path = None;
                self.attached_real_path = None;
                self.set_last_error(Some(failure_error), None);
                self.set_state(SessionState::Detached, now);
                let id = self.next_commit();
                self.pending_commit = Some(PendingCommit::AttachFailed {
                    id,
                    reply: pending.reply,
                    previous_state: pending.previous_state,
                });
                vec![Effect::Persist {
                    commit_id: id,
                    delta: StateDelta::Unreleased {
                        sid: self.sid.clone(),
                    },
                }]
            }
        }
    }

    fn adopt_opened_bridge(&mut self, opened: OpenedBridge, now: Mono) -> Vec<Effect> {
        let OpenedBridge {
            epoch,
            worker,
            real_path,
            rx,
            vtty,
        } = opened;
        self.generations.conn_epoch = Some(epoch);
        // 新 bridge 的 RX 視窗與哨兵（Python 每次 attach 新建 `UARTBridge`；POSIX 哨兵 primary 算 1 個 console）。
        // agent log 的 reader 端寫入者跟著現行 bridge（detach 時已停止 capture，此處只是防禦性搬移）。
        self.deps.rx.set_capture(None);
        if self.log_capture.is_some() {
            rx.set_capture(Some(self.capture_observer.clone()));
        }
        self.deps.rx = rx;
        self.console_count = usize::from(vtty.is_some());
        self.vtty = vtty;
        self.attached_real_path = Some(real_path.clone());
        self.known_real_path = Some(real_path.clone());
        if self.is_passthrough() {
            // `reset_passthrough_boot_gate`（`session_manager.py:644-654`）：清前一 profile 留下的 quiet／READY gate。
            self.quiet = QuietState::default();
        }
        let quiet_active = self.quiet.quiet_active(now);
        let blocks_probe = boot_quiet_blocks_probe(&self.quiet, &self.profile, now);
        let reconfirm_pending = self.quiet.pending;
        self.deps.fence.with(|f| {
            f.active_epoch = Some(epoch);
            f.fsm_gen = self.generations.fsm_gen;
            f.owner_gen = self.generations.owner_gen;
            f.frozen = false;
            // Python `_push_tx_gate`：新 bridge 帶回 session 的 quiet 與 reconfirm 狀態（審查 F63）。
            f.quiet_active = quiet_active;
            f.probe_quiet_exempt = quiet_active && !blocks_probe;
            f.reconfirm_pending = reconfirm_pending;
        });
        let mut effects = vec![Effect::AdoptBridge {
            epoch,
            worker,
            real_path,
        }];
        if quiet_active {
            // 跨 detach 保留的 quiet：為新 epoch 排到期計時器，到期解除 fence（Python 時間式 gate；審查 92e0223_76de0cf bea2d8e-1）。
            effects.push(Effect::Schedule {
                at: self.quiet.boot_quiet_until,
                ev: TimerEvent::BootQuietExpired {
                    epoch,
                    fsm_gen: self.generations.fsm_gen,
                },
            });
        }

        if self.is_passthrough() || !self.profile.template.command_capable() {
            self.set_last_error(None, None);
            self.adopt_bridge(now);
            self.set_state(SessionState::Attached, now);
            return effects;
        }
        let quiet_blocks = boot_quiet_blocks_probe(&self.quiet, &self.profile, now);
        let (plan, synthesized) = self.choose_plan(quiet_blocks, self.pending_auto_login);
        match plan {
            LoginPlan::None => {
                if let Some(error) = synthesized {
                    // boot quiet（#130）：掛上 bridge 純收 RX，不送 probe。
                    self.set_last_error(Some(error.to_string()), None);
                    self.adopt_bridge(now);
                    self.set_state(SessionState::Attached, now);
                    return effects;
                }
                // 防禦（P7 Task 3 退役 Slice 2.1 能力閘後不應到達：None 只來自 passthrough／非 command-capable／quiet，
                // 前兩者已於上方提早返回）：停 ATTACHED、不自動送 TX。
                self.set_last_error(None, None);
                self.adopt_bridge(now);
                self.set_state(SessionState::Attached, now);
                effects
            }
            LoginPlan::CredentialsUnresolved { reason, env_file } => {
                self.set_last_error(Some("CREDENTIALS_UNRESOLVED".into()), None);
                self.adopt_bridge(now);
                self.set_state(SessionState::Attached, now);
                effects.extend(self.credentials_warning(reason, &env_file));
                effects
            }
            plan => {
                self.login_run = Some(LoginRun {
                    epoch,
                    kind: LoginRunKind::Attach,
                    fsm_gen: self.generations.fsm_gen,
                    waiters: Vec::new(),
                });
                effects.push(Effect::StartLogin {
                    epoch,
                    fsm_gen: self.generations.fsm_gen,
                    plan,
                });
                effects
            }
        }
    }

    /// 重新解析帳密並選擇登入計畫（attach／既有 bridge 再探共用）。
    ///
    /// 帳密只在 Python 會呼叫 `resolve_session_auth` 處解析（`session_manager.py:4717-4742`、`:4465-4466`）：
    /// boot quiet 或 `require_login` 為假時不解析，不讀 env_file、不寫 `env_file 不存在` WARNING。
    /// `pending_auto_login`：attach 傳 session 旗標；既有 bridge 再探傳 `false`（Python 只看 `login_regex or console_unlock_env`）。
    fn choose_plan(
        &self,
        quiet_blocks: bool,
        pending_auto_login: bool,
    ) -> (LoginPlan, Option<&'static str>) {
        let t = &self.profile.template;
        let require_login = t.platform != "passthrough"
            && (pending_auto_login
                || !t.login_regex.is_empty()
                || t.console_unlock_env
                    .as_deref()
                    .is_some_and(|v| !v.is_empty()));
        let auth = if quiet_blocks || !require_login {
            (
                crate::session_auth::SessionAuth::default(),
                crate::session_auth::AuthResolution {
                    reason: "not_configured",
                    env_file_path: None,
                },
            )
        } else {
            resolve_session_auth(
                &self.profile,
                self.deps.env.as_ref(),
                self.deps.read_env_file.as_ref(),
            )
        };
        choose_login_plan(&self.profile, pending_auto_login, auth, quiet_blocks)
    }

    /// `_emit_credentials_unresolved_warning`：`credentials_warned` 去重；WAL META＋daemon stderr。
    fn credentials_warning(&mut self, reason: &'static str, env_file: &str) -> Vec<Effect> {
        if self.credentials_warned {
            return Vec::new();
        }
        self.credentials_warned = true;
        let env_file_display = if env_file.is_empty() { "-" } else { env_file };
        let mut meta = PyDict::new();
        meta.insert("event", PyValue::from("credentials_unresolved"));
        meta.insert("reason", PyValue::from(reason));
        meta.insert("env_file", PyValue::from(env_file));
        vec![
            Effect::WalMeta { cmd_id: None, meta },
            Effect::Warn(format!(
                "session {} 帳密解析未完成（reason={reason}, env_file={env_file_display}）：不對 login prompt 送空帳密，標記 CREDENTIALS_UNRESOLVED 終態，請補齊帳密後手動 attach/recover",
                self.profile.com
            )),
        ]
    }

    fn adopt_bridge(&mut self, now: Mono) {
        self.generations.bridge_generation = self.generations.bridge_generation.saturating_add(1);
        self.last_probe_at = Some(self.wall_iso());
        if self.is_passthrough() {
            self.quiet_since = Some(now);
        }
    }

    fn login_finished(
        &mut self,
        epoch: u64,
        fsm_gen: u64,
        result: crate::login_fsm::LoginOutcome,
        now: Mono,
    ) -> Vec<Effect> {
        if self
            .login_run
            .as_ref()
            .is_some_and(|run| run.epoch == epoch && run.kind == LoginRunKind::AutoReprobe)
        {
            return self.auto_reprobe_finished(epoch, fsm_gen, result, now);
        }
        if self
            .login_run
            .as_ref()
            .is_some_and(|run| run.epoch == epoch && run.kind == LoginRunKind::RebootRecovery)
        {
            return self.reboot_login_finished(epoch, result, now);
        }
        let reprobe = self
            .login_run
            .as_ref()
            .is_some_and(|run| run.epoch == epoch && run.kind == LoginRunKind::Reprobe);
        if self.generations.conn_epoch != Some(epoch) {
            return Vec::new();
        }
        if reprobe && self.state == SessionState::Attached {
            if let Some((_, message)) = login_exception(&result) {
                // `_probe_existing_bridge` 的例外穿出 manager：session 不變，attach／recover 的 RPC 回 framing EXCEPTION
                // （message＝`str(exc)`，審查 F16）。
                // 只有發起 probe 的第一個呼叫收到例外；併發的後續 attach／recover 在 Python 取得 probe 鎖後自行重新 probe
                // （審查 92e0223_76de0cf bea2d8e-2）：以新世代重派。
                let mut waiters = self
                    .login_run
                    .take()
                    .map(|run| run.waiters)
                    .unwrap_or_default()
                    .into_iter();
                let mut effects: Vec<Effect> = waiters
                    .next()
                    .map(|(reply, _)| Effect::Reply(reply, Err(PyErr::value_error(message))))
                    .into_iter()
                    .collect();
                for (reply, kind) in waiters {
                    effects.extend(self.reprobe_existing_bridge(reply, kind, now));
                }
                return effects;
            }
        }
        let result = legacy_login_outcome(result);
        if reprobe {
            // 既有 bridge 再探（attach_session ATTACHED 分支）：結果一律回填，quiet 撤銷造成的拒絕亦然（Python 同步 probe）。
            if self.state != SessionState::Attached {
                return Vec::new();
            }
        } else if self.state != SessionState::Attaching
            || !self.login_run.as_ref().is_some_and(|run| {
                run.epoch == epoch && run.kind == LoginRunKind::Attach && run.fsm_gen == fsm_gen
            })
        {
            // 只丟棄「不屬於現行 attach run」的事件（已被新 run 取代或世代不符）；quiet arm 推進的 core
            // `fsm_gen` 不使現行 run 失效（審查 F15；Python `_attach_by_id` 照常寫 ATTACHED／READY／DETACHED）。
            return Vec::new();
        }
        let run = self.login_run.take();
        let mut waiters = run.map(|run| run.waiters).unwrap_or_default();
        let mut effects = Vec::new();
        match result {
            // 審查 F16：非 quiet／非 stale_login_gen 的 TX 失敗＝Python 例外上拋。
            crate::login_fsm::LoginOutcome::Raised { exc, message } => {
                if reprobe {
                    // `_probe_existing_bridge` 例外穿出 attach_session／recover_session：state、last_error 不變，
                    // RPC 回 `EXCEPTION`（message＝`str(exc)`）。
                    for (waiter, _) in std::mem::take(&mut waiters) {
                        effects.push(Effect::Reply(
                            waiter,
                            Err(sw_proto::pycompat::PyErr::value_error(message.clone())),
                        ));
                    }
                } else {
                    // `_attach_by_id` 外層 except：關 bridge、DETACHED、`ATTACH_FAILED:<ExcType>`。
                    self.set_last_error(Some(format!("ATTACH_FAILED:{exc}")), None);
                    effects.extend(self.begin_stop(StopKind::LoginFailed, None, now).0);
                }
            }
            crate::login_fsm::LoginOutcome::Ready => {
                if !reprobe {
                    self.adopt_bridge(now);
                }
                self.last_probe_at = Some(self.wall_iso());
                self.set_last_error(None, None);
                self.set_state(SessionState::Ready, now);
                self.last_ready_at = Some(self.wall_iso());
                self.queue_registered = true;
                self.recovering = false;
                if reprobe {
                    self.pending_auto_login = false;
                }
                // `_reset_reprobe_progress_locked`（READY 轉移點）。
                crate::reprobe::reset(&mut self.reprobe);
                self.confirm_ready();
                effects.push(Effect::Notify(ManagerNote::SessionReady {
                    sid: self.sid.clone(),
                    via_attach: !reprobe,
                }));
            }
            crate::login_fsm::LoginOutcome::NotReady(error) => {
                if !reprobe {
                    self.adopt_bridge(now);
                }
                self.last_probe_at = Some(self.wall_iso());
                let detail = login_error_detail(&self.deps.rx, &error);
                self.set_last_error(Some(error), detail);
                if reprobe {
                    self.pending_auto_login = false;
                }
                self.set_state(SessionState::Attached, now);
            }
            crate::login_fsm::LoginOutcome::Failed(error) => {
                let detail = login_error_detail(&self.deps.rx, &error);
                self.set_last_error(Some(error), detail);
                if reprobe {
                    self.pending_auto_login = false;
                    // `_probe_existing_bridge`：ensure_ready 失敗仍停 ATTACHED（不 drain）。
                    self.last_probe_at = Some(self.wall_iso());
                    self.set_state(SessionState::Attached, now);
                } else {
                    // `_attach_by_id`：ensure_ready 失敗 → 關 bridge、DETACHED（05 T5）。
                    effects.extend(self.begin_stop(StopKind::LoginFailed, None, now).0);
                }
            }
            // 已由 `legacy_login_outcome` 收斂，不會到達（P7 Task 7 以收斂表取代）。
            crate::login_fsm::LoginOutcome::Aborted(_) => {}
        }
        for (waiter, kind) in waiters {
            effects.extend(self.reprobe_reply(waiter, kind, now));
        }
        effects
    }

    /// `_attach_state_result`（#94）：command-capable 卡在非 READY／ATTACHING 時回 ok:false＋狀態專用碼。
    fn attach_state_result(&self, now: Mono) -> PyDict {
        let session = PyValue::Dict(self.public(
            now,
            &self.public_ctx(),
        ));
        if self.profile.template.command_capable()
            && !matches!(self.state, SessionState::Ready | SessionState::Attaching)
        {
            return match self.state {
                SessionState::Flashing => pydict! {
                    "ok" => false,
                    "error_code" => "FLASHING_BUSY",
                    "session" => session
                },
                SessionState::Recovering => pydict! {
                    "ok" => false,
                    "error_code" => "SESSION_RECOVERING",
                    "session" => session
                },
                _ => pydict! {
                    "ok" => false,
                    "error_code" => "SESSION_NOT_READY",
                    "last_error" => option_string(self.last_error.as_deref()),
                    "last_error_detail" => option_string(self.last_error_detail.as_deref()),
                    "session" => session
                },
            };
        }
        pydict! { "ok" => true, "session" => session }
    }

    /// `attach_session` 的 ATTACHED 分支：`_probe_existing_bridge`（quiet 單點 gate、重新解析帳密、同步 probe）。
    fn reprobe_existing_bridge(
        &mut self,
        reply: Reply,
        kind: ReprobeWaiter,
        now: Mono,
    ) -> Vec<Effect> {
        let Some(epoch) = self.generations.conn_epoch else {
            return self.reprobe_reply(reply, kind, now);
        };
        if let Some(run) = self.login_run.as_mut() {
            if run.kind == LoginRunKind::Reprobe && run.epoch == epoch {
                run.waiters.push((reply, kind));
                return Vec::new();
            }
            // 自動重探進行中：Python 手動 attach／recover 會等 auto worker 釋放 probe 鎖後再自行 probe——
            // 先掛在 auto run 上，auto 收斂後再以新世代重跑（`auto_reprobe_finished`）。
            if run.kind == LoginRunKind::AutoReprobe && run.epoch == epoch {
                run.waiters.push((reply, kind));
                return Vec::new();
            }
        }
        if self.is_passthrough() || !self.profile.template.command_capable() {
            return self.reprobe_reply(reply, kind, now);
        }
        let quiet_blocks = boot_quiet_blocks_probe(&self.quiet, &self.profile, now);
        if quiet_blocks {
            // #130 Finding 2／#162：不送 probe TX；READY 不以合成碼覆寫 last_error。
            if self.state != SessionState::Ready {
                self.set_last_error(Some("PROMPT_UNAVAILABLE".into()), None);
            }
            return self.reprobe_reply(reply, kind, now);
        }
        // `_begin_login_run_locked`：新登入世代，舊 run 的 TX 由 fence FsmMismatch 撤銷。
        self.begin_login_generation(now);
        let (plan, _) = self.choose_plan(false, false);
        match plan {
            LoginPlan::CredentialsUnresolved { reason, env_file } => {
                let mut effects = self.credentials_warning(reason, &env_file);
                self.set_last_error(Some("CREDENTIALS_UNRESOLVED".into()), None);
                self.pending_auto_login = false;
                self.set_state(SessionState::Attached, now);
                effects.extend(self.reprobe_reply(reply, kind, now));
                effects
            }
            LoginPlan::None => {
                // 防禦（能力閘已退役，quiet 已於上方處理）：不自動送 TX，誠實回報現況。
                self.reprobe_reply(reply, kind, now)
            }
            plan => {
                self.login_run = Some(LoginRun {
                    epoch,
                    kind: LoginRunKind::Reprobe,
                    fsm_gen: self.generations.fsm_gen,
                    waiters: vec![(reply, kind)],
                });
                vec![Effect::StartLogin {
                    epoch,
                    fsm_gen: self.generations.fsm_gen,
                    plan,
                }]
            }
        }
    }

    fn rx_chunk(&mut self, epoch: u64, bytes: &[u8], wal_seq: u64, now: Mono) -> Vec<Effect> {
        if self.generations.conn_epoch != Some(epoch) || bytes.is_empty() {
            return Vec::new();
        }
        self.last_rx_mono = now;
        self.last_rx_at = Some(self.wall_iso());
        self.last_rx_wal_seq = wal_seq;
        let chunk = clean_text(&String::from_utf8_lossy(bytes));
        if self.is_passthrough() {
            // SC-35：passthrough 不 arm quiet，只重設安靜視窗。
            if self.state == SessionState::Attached {
                self.quiet_since = Some(now);
            }
            if !chunk.is_empty() {
                self.capture_rx(&chunk, wal_seq, now);
            }
            return Vec::new();
        }
        if chunk.is_empty() {
            return Vec::new();
        }
        let transition = self.quiet.on_rx_text(&chunk, &self.profile, now);
        self.capture_rx(&chunk, wal_seq, now);
        match transition {
            QuietTransition::Armed => self.apply_quiet_armed(now),
            QuietTransition::Cleared => {
                // 只解除 TX 靜默維度；reconfirm pending 保留（#162）。
                self.deps.fence.with(|f| {
                    f.quiet_active = false;
                    f.probe_quiet_exempt = false;
                });
                Vec::new()
            }
            QuietTransition::None => Vec::new(),
        }
    }

    /// quiet 已 arm（`QuietState::arm`）後的同步：世代遞增、fence 旗標、到期 timer。
    fn apply_quiet_armed(&mut self, now: Mono) -> Vec<Effect> {
        let Some(epoch) = self.generations.conn_epoch else {
            return Vec::new();
        };
        let until = self.quiet.boot_quiet_until;
        // P7 Task 4：arm 一律遞增 fsm_gen（隔離 quiet timer）；只有 `boot_quiet_blocks_probe` 為真時才推進 fence 世代
        // 撤銷在途 login run（Python `_update_boot_quiet_locked` 對 console unlock 不遞增 login_gen、probe gate quiet_until=0）。
        let blocks_probe = boot_quiet_blocks_probe(&self.quiet, &self.profile, now);
        self.generations.fsm_gen = self.generations.fsm_gen.saturating_add(1);
        self.generations.owner_gen = self.generations.owner_gen.saturating_add(1);
        let pending = self.quiet.pending;
        self.deps.fence.with(|f| {
            f.quiet_active = true;
            f.probe_quiet_exempt = !blocks_probe;
            f.reconfirm_pending = pending;
            if blocks_probe {
                f.fsm_gen = self.generations.fsm_gen;
            }
            f.owner_gen = self.generations.owner_gen;
        });
        let _ = now;
        vec![Effect::Schedule {
            at: until,
            ev: TimerEvent::BootQuietExpired {
                epoch,
                fsm_gen: self.generations.fsm_gen,
            },
        }]
    }

    /// reboot 命令 TX 結算（parity-amendments-2026-10-26 #55；`_handle_reboot_command`）：
    /// 已提交（`Completed(n≥1)`）或結果不明（`Unknown`）時才 arm quiet；`NotSubmitted` 一律不 arm、不進 RECOVERING。
    fn reboot_tx_settled(
        &mut self,
        epoch: u64,
        op_id: OpId,
        confirmed_bytes: usize,
        unknown: bool,
        now: Mono,
    ) -> Vec<Effect> {
        let is_foreground = self
            .foreground
            .as_ref()
            .is_some_and(|grant| grant.op_id == op_id && grant.epoch == epoch);
        if !self.foreground_reboot || !is_foreground {
            return Vec::new();
        }
        if confirmed_bytes == 0 && !unknown {
            return Vec::new();
        }
        self.foreground_reboot = false;
        self.quiet.arm(now);
        self.apply_quiet_armed(now)
    }

    fn timer(&mut self, event: TimerEvent, now: Mono) -> Vec<Effect> {
        match event {
            TimerEvent::BootQuietExpired { epoch, .. }
                if self.generations.conn_epoch == Some(epoch)
                    && self.quiet.boot_quiet_until != 0
                    && self.quiet.boot_quiet_until <= now =>
            {
                // 照 Python：到期只令 quiet 失效（`boot_quiet_until` 保留），fence 解除 TX 靜默。
                self.deps.fence.with(|f| {
                    f.quiet_active = false;
                    f.probe_quiet_exempt = false;
                });
                Vec::new()
            }
            TimerEvent::LeaseExpiry { lease_id, deadline }
                if self.lease.as_ref().is_some_and(|lease| {
                    lease.interactive_id == lease_id
                        && !lease.is_human()
                        && lease.deadline() == deadline
                }) && now >= deadline =>
            {
                // Python `expired()`：`now - last > timeout`（嚴格大於；審查 F36）。恰在期限仍 active，
                // 於期限後 1 ns 再檢查一次。
                if now > deadline {
                    self.close_lease(true);
                    Vec::new()
                } else {
                    vec![Effect::Schedule {
                        at: deadline.saturating_add(1),
                        ev: TimerEvent::LeaseExpiry { lease_id, deadline },
                    }]
                }
            }
            TimerEvent::DrainTimeout { epoch, stop_id } => {
                self.mark_drain_timeout(epoch, stop_id, now)
            }
            _ => Vec::new(),
        }
    }

    fn tick(&mut self, now: Mono) -> Vec<Effect> {
        let mut effects = Vec::new();
        self.expire_reconfirm(now);
        // SC-13：1 s tick 主動收回到期的 agent lease（`_expire_interactive_leases_tick`，留 tombstone）。
        if self
            .lease
            .as_ref()
            .is_some_and(|lease| !lease.is_human() && lease.expired(now))
        {
            self.close_lease(true);
        }
        effects.extend(self.reboot_recovery_tick(now));
        effects.extend(self.reprobe_tick(now));
        let flash_off = self.deps.fence.with(|f| f.flash_mode == FlashMode::Off);
        if self.is_passthrough()
            && self.state == SessionState::Attached
            && self.generations.conn_epoch.is_some()
            && self.stop.is_none()
            && flash_off
            && self.quiet_since.is_some_and(|since| {
                now.saturating_sub(since) as f64 / 1_000_000_000.0 >= PASSTHROUGH_READY_QUIET_S
            })
        {
            self.set_last_error(None, None);
            self.set_state(SessionState::Ready, now);
            self.last_ready_at = Some(self.wall_iso());
            self.queue_registered = true;
            effects.push(Effect::Notify(ManagerNote::SessionReady {
                sid: self.sid.clone(),
                via_attach: false,
            }));
        }
        effects
    }

    /// `_begin_login_run_locked`＋`_push_probe_gate`：新登入世代（舊 run 的 TX 由 fence FsmMismatch 撤銷），fence 的 quiet
    /// 依現況重設——console unlock 在 quiet 中只豁免登入 probe（`_gate_probe_quiet_until=0`），救援鍵／self-test 仍擋
    /// （`_gate_self_test_quiet_until` 不變，`uart_io.py:635-649`）。
    fn begin_login_generation(&mut self, now: Mono) {
        self.generations.fsm_gen = self.generations.fsm_gen.saturating_add(1);
        let quiet_active = self.quiet.quiet_active(now);
        let blocks_probe = boot_quiet_blocks_probe(&self.quiet, &self.profile, now);
        self.deps.fence.with(|f| {
            f.fsm_gen = self.generations.fsm_gen;
            f.quiet_active = quiet_active;
            f.probe_quiet_exempt = quiet_active && !blocks_probe;
        });
    }

    /// P7 Task 5：`reconcile_readiness` 的單一 session 部分（`_prepare_reprobe_locked`＋worker 寫入前最終驗證）。
    fn reprobe_tick(&mut self, now: Mono) -> Vec<Effect> {
        if self.shutdown_requested
            || self.stop.is_some()
            || self.pending_open_stop.is_some()
            || self.pending_attach.is_some()
            || self.open_real_path.is_some()
        {
            return Vec::new();
        }
        // 手動 attach／recover（或其他 login run）持有 probe：Python `_prepare_reprobe_locked` 照常排程（前移
        // `next_reprobe_at`），worker 取不到 probe 鎖才跳過（審查 c11aa89_92e0223 L-3）。
        let probe_busy = self.login_run.is_some() || self.foreground.is_some();
        let state = self.state.as_str();
        let last_error = self.last_error.clone();
        // human_active：Python `_human_active_locked`（human lease＋外部 peer＋60 s 內有輸入）；human lease 由 P6
        // console 建立（07 T7 `lease_context` 骨架），P6 合入前不可能成立。
        let human_active = false;
        let input = crate::reprobe::ReprobeInput {
            state,
            by_id: &self.profile.device_by_id,
            released: self.state == SessionState::Released,
            device_present: self.device_present,
            last_error: last_error.as_deref(),
            last_rx_mono: (self.last_rx_mono != 0).then_some(self.last_rx_mono),
            human_active,
            has_bridge: self.generations.conn_epoch.is_some(),
            profile: &self.profile,
        };
        let was_failed = self.quiet.failed;
        let action = crate::reprobe::prepare(&mut self.reprobe, &mut self.quiet, &input, now);
        if !was_failed && self.quiet.failed {
            self.deps.fence.with(|f| f.reconfirm_pending = false);
        }
        if probe_busy && action.is_some() {
            self.reprobe.inflight = false;
            return Vec::new();
        }
        match action {
            None => Vec::new(),
            Some(crate::reprobe::ReprobeAction::Attach) => {
                // DETACHED 分支：ATTACHING＋last_error=None 後走既有開埠（`_spawn_attach`）。
                // `prepare` 只在 device_present 時回 Attach；device_present 與 known_real_path 一律成對設定／清除
                // （DeviceAttached／DeviceDetached），故此處必有路徑（審查 c11aa89_92e0223 L-4）。
                debug_assert!(
                    self.known_real_path.is_some(),
                    "device_present 隱含 known_real_path"
                );
                let Some(real_path) = self.known_real_path.clone() else {
                    return Vec::new();
                };
                self.set_last_error(None, None);
                self.generations.fsm_gen = self.generations.fsm_gen.saturating_add(1);
                self.open_generation = self.generations.fsm_gen;
                self.open_real_path = Some(real_path.clone());
                self.set_state(SessionState::Attaching, now);
                vec![Effect::OpenBridge {
                    dev: DevicePath(real_path),
                    cfg: self.serial_config(),
                    precheck_holders: false,
                }]
            }
            Some(crate::reprobe::ReprobeAction::Probe) => {
                let tail = self.deps.rx.tail(BOOTLOADER_RX_TAIL_CHARS);
                let valid_input = crate::reprobe::ValidInput {
                    same_bridge: true,
                    by_id: &self.profile.device_by_id,
                    released: self.state == SessionState::Released,
                    device_present: self.device_present,
                    state: self.state.as_str(),
                    last_error: last_error.as_deref(),
                    human_active,
                    rx_tail_512: &tail,
                    profile: &self.profile,
                };
                let was_failed = self.quiet.failed;
                let valid = crate::reprobe::target_still_valid(
                    &self.reprobe,
                    &mut self.quiet,
                    &valid_input,
                    now,
                );
                if !was_failed && self.quiet.failed {
                    self.deps.fence.with(|f| f.reconfirm_pending = false);
                }
                if !valid {
                    self.reprobe.inflight = false;
                    return Vec::new();
                }
                self.start_auto_reprobe(now)
            }
        }
    }

    /// `_probe_existing_bridge` 的自動重探版（無 RPC 等待者）：新登入世代、重新解析帳密（既有 bridge 路徑不看 pending_auto_login）。
    fn start_auto_reprobe(&mut self, now: Mono) -> Vec<Effect> {
        let Some(epoch) = self.generations.conn_epoch else {
            self.reprobe.inflight = false;
            return Vec::new();
        };
        self.begin_login_generation(now);
        let (plan, _) = self.choose_plan(false, false);
        match plan {
            LoginPlan::CredentialsUnresolved { reason, env_file } => {
                // `_probe_existing_bridge`：帳密終態 → ATTACHED＋CREDENTIALS_UNRESOLVED；`_finish_probe_reprobe` 記一次。
                let mut effects = self.credentials_warning(reason, &env_file);
                self.recovering = false;
                self.pending_auto_login = false;
                self.set_last_error(Some("CREDENTIALS_UNRESOLVED".into()), None);
                self.set_state(SessionState::Attached, now);
                effects.extend(self.finish_auto_reprobe(false, now));
                effects
            }
            LoginPlan::None => {
                self.reprobe.inflight = false;
                Vec::new()
            }
            plan => {
                self.login_run = Some(LoginRun {
                    epoch,
                    kind: LoginRunKind::AutoReprobe,
                    waiters: Vec::new(),
                    fsm_gen: self.generations.fsm_gen,
                });
                vec![Effect::StartLogin {
                    epoch,
                    fsm_gen: self.generations.fsm_gen,
                    plan,
                }]
            }
        }
    }

    /// 自動重探的 `LoginFinished`：`_probe_existing_bridge` 收斂（READY 被 quiet 撤銷時保留 READY；例外 → `REPROBE_FAILED:`）
    /// 後接 `_finish_probe_reprobe`。
    fn auto_reprobe_finished(
        &mut self,
        epoch: u64,
        fsm_gen: u64,
        result: crate::login_fsm::LoginOutcome,
        now: Mono,
    ) -> Vec<Effect> {
        if self.generations.conn_epoch != Some(epoch)
            || !self
                .login_run
                .as_ref()
                .is_some_and(|run| run.epoch == epoch && run.fsm_gen == fsm_gen)
        {
            return Vec::new();
        }
        // 結果一律回填（不得丟棄，否則 run／inflight 永久殘留、等待者永不回覆；審查 c11aa89_92e0223 H-1）。
        // Python `_probe_existing_bridge` 只依 probe 實際結果收斂、不看 login_gen：probe 期間 banner 才 arm 仍可回 ok → READY
        // （#139）；後續 TX 被 fence 撤銷時 FSM 本就回 `Aborted(Refused|Cancelled)`（審查 92e0223_76de0cf d5b2dd6-1）。
        let deferred = self
            .login_run
            .take()
            .map(|run| run.waiters)
            .unwrap_or_default();
        let mut effects = self.auto_reprobe_converge(result, now);
        // 等在 auto run 上的手動 attach／recover：以新世代重新 probe（第一個起 run，其餘加入等待）。
        for (reply, kind) in deferred {
            effects.extend(self.reprobe_existing_bridge(reply, kind, now));
        }
        effects
    }

    fn auto_reprobe_converge(
        &mut self,
        result: crate::login_fsm::LoginOutcome,
        now: Mono,
    ) -> Vec<Effect> {
        use crate::login_fsm::{LoginAbort, LoginOutcome};
        if !matches!(self.state, SessionState::Attached | SessionState::Ready) {
            self.reprobe.inflight = false;
            return Vec::new();
        }
        let mut effects = Vec::new();
        if let Some((exc, _)) = login_exception(&result) {
            // `_spawn_reprobe_probe` 的 except：ATTACHED＋REPROBE_FAILED:<型別>、記一次 attempt（不經 finish）。
            self.reprobe.inflight = false;
            self.set_last_error(Some(format!("REPROBE_FAILED:{exc}")), None);
            self.set_state(SessionState::Attached, now);
            crate::reprobe::record_attempt(&mut self.reprobe, now);
            return effects;
        }
        match result {
            // login_exception_name 已於上方收斂（不會到達）。
            LoginOutcome::Raised { .. } | LoginOutcome::Aborted(LoginAbort::TxIncomplete) => {
                return effects
            }
            LoginOutcome::Aborted(_) => {
                // quiet／新世代撤銷（`_login_tx_refused`）：READY 不變；其餘 ATTACHED＋PROMPT_UNAVAILABLE。
                self.recovering = false;
                self.pending_auto_login = false;
                if self.state != SessionState::Ready {
                    self.set_last_error(Some("PROMPT_UNAVAILABLE".into()), None);
                    self.set_state(SessionState::Attached, now);
                } else {
                    self.last_error_detail = None;
                }
            }
            LoginOutcome::Ready => {
                self.recovering = false;
                self.pending_auto_login = false;
                self.last_probe_at = Some(self.wall_iso());
                self.set_last_error(None, None);
                self.set_state(SessionState::Ready, now);
                self.last_ready_at = Some(self.wall_iso());
                self.queue_registered = true;
                self.confirm_ready();
                // 自動重探不是動態 attach 本身（P5 6aafe8f：sticky 只在 attach 到 READY 時寫入）。
                effects.push(Effect::Notify(ManagerNote::SessionReady {
                    sid: self.sid.clone(),
                    via_attach: false,
                }));
            }
            LoginOutcome::NotReady(error) | LoginOutcome::Failed(error) => {
                self.recovering = false;
                self.pending_auto_login = false;
                self.last_probe_at = Some(self.wall_iso());
                let detail = login_error_detail(&self.deps.rx, &error);
                self.set_last_error(Some(error), detail);
                self.set_state(SessionState::Attached, now);
            }
        }
        let became_ready = self.state == SessionState::Ready;
        effects.extend(self.finish_auto_reprobe(became_ready, now));
        effects
    }

    /// `_finish_probe_reprobe`：計次／退避、pending 中確認失敗累加，RX 尾行為 bootloader → BOOTLOADER_STUCK 終態。
    fn finish_auto_reprobe(&mut self, became_ready: bool, now: Mono) -> Vec<Effect> {
        let matched = if became_ready {
            None
        } else {
            self.bootloader_evidence()
        };
        let was_failed = self.quiet.failed;
        if let Some(fin) = crate::reprobe::finish_probe(
            &mut self.reprobe,
            &mut self.quiet,
            became_ready,
            matched,
            now,
        ) {
            if fin.last_error.is_some() {
                self.set_last_error(fin.last_error, fin.last_error_detail);
            }
        }
        if !was_failed && self.quiet.failed {
            self.deps.fence.with(|f| f.reconfirm_pending = false);
        }
        Vec::new()
    }

    fn expire_reconfirm(&mut self, now: Mono) {
        if self.quiet.expire(now, false) {
            self.deps.fence.with(|f| f.reconfirm_pending = false);
        }
    }

    fn confirm_ready(&mut self) {
        self.quiet.confirm_ready();
        self.deps.fence.with(|f| {
            f.quiet_active = false;
            f.probe_quiet_exempt = false;
            f.reconfirm_pending = false;
            f.active_epoch = self.generations.conn_epoch;
            f.fsm_gen = self.generations.fsm_gen;
        });
    }

    fn mark_drain_timeout(&mut self, epoch: u64, stop_id: StopId, now: Mono) -> Vec<Effect> {
        let stalled_epoch = self.generations.bridge_generation;
        if let Some(stop) = &mut self.stop {
            if stop.epoch == epoch && stop.id == stop_id && !stop.stalled {
                stop.stalled = true;
                self.drain_stalled = true;
                self.drain_timeout_epoch = Some(stalled_epoch);
                if stop.kind == StopKind::Release {
                    let waiters: Vec<_> = stop.waiters.drain(..).collect();
                    let response = self.drain_stalled_reply(now);
                    return waiters
                        .into_iter()
                        .map(|waiter| Effect::Reply(waiter, Ok(response.clone())))
                        .collect();
                }
            }
            return Vec::new();
        }
        if let Some(pending) = &mut self.pending_open_stop {
            if pending.generation == epoch && pending.id == stop_id && !pending.stalled {
                pending.stalled = true;
                self.drain_stalled = true;
                self.drain_timeout_epoch = Some(stalled_epoch);
                if pending.kind == StopKind::Release {
                    let waiters: Vec<_> = pending.waiters.drain(..).collect();
                    let response = self.drain_stalled_reply(now);
                    return waiters
                        .into_iter()
                        .map(|waiter| Effect::Reply(waiter, Ok(response.clone())))
                        .collect();
                }
            }
        }
        Vec::new()
    }

    fn worker_stopped(&mut self, receipt: WorkerStoppedReceipt, now: Mono) -> Vec<Effect> {
        let mut effects = Vec::new();
        let matching_stop = self
            .stop
            .as_ref()
            .is_some_and(|stop| stop.id == receipt.stop_id && stop.epoch == receipt.epoch);
        if !matching_stop {
            let stale = self
                .retired_stops
                .iter()
                .position(|stop| stop.id == receipt.stop_id && stop.epoch == receipt.epoch)
                .and_then(|index| self.retired_stops.remove(index));
            if let Some(stale) = stale {
                self.apply_aborted_receipt(stale.aborted_op.as_ref(), &receipt, &mut effects);
            }
            append_receipt_warnings(&receipt, &mut effects, &self.profile.com);
            return effects;
        }

        if !receipt.handle_closed {
            let mut reply_waiters = Vec::new();
            if let Some(stop) = &mut self.stop {
                stop.stalled = true;
                if stop.kind == StopKind::Release {
                    reply_waiters.extend(stop.waiters.drain(..));
                }
            }
            self.drain_stalled = true;
            self.drain_timeout_epoch = Some(self.generations.bridge_generation);
            append_receipt_warnings(&receipt, &mut effects, &self.profile.com);
            if let Some(error) = receipt.close_error.as_ref() {
                effects.push(Effect::Warn(format!(
                    "WARNING session {} UART handle 關閉失敗：{error:?}",
                    self.profile.com
                )));
            }
            effects.extend(
                reply_waiters
                    .into_iter()
                    .map(|waiter| Effect::Reply(waiter, Ok(self.drain_stalled_reply(now)))),
            );
            return effects;
        }

        let stop = self.stop.take().expect("已確認的 stop op");
        self.apply_aborted_receipt(stop.aborted_op.as_ref(), &receipt, &mut effects);
        append_receipt_warnings(&receipt, &mut effects, &self.profile.com);
        self.retired_stops.push_back(RetiredStop {
            epoch: stop.epoch,
            id: stop.id,
            aborted_op: stop.aborted_op.clone(),
        });
        while self.retired_stops.len() > 32 {
            self.retired_stops.pop_front();
        }

        self.generations.conn_epoch = None;
        self.attached_real_path = None;
        self.foreground = None;
        self.side_ops.clear();
        self.vtty = None;
        self.console_count = 0;
        self.queue_registered = false;
        self.queue = SessionQueue::new();
        self.clear_interactive();
        self.detach_captures();
        // Python detach 不清 boot quiet／READY reconfirm（只有 passthrough 重掛時 `reset_passthrough_boot_gate` 清）：
        // DUT reboot 期間 re-enumerate 的重插仍受 quiet 保護（#130）。fence 隨 epoch 失效，重開時依現況同步。
        // boot quiet／reconfirm 掛在 session 而非 bridge（Python `SessionRuntime.boot_quiet_until`；`_complete_drain`
        // 只 `reset_reprobe_progress`）：drain 不清，新 bridge adopt 時再推回 fence（審查 F63）。
        self.login_run = None;
        self.foreground_reboot = false;
        self.deps.fence.with(|f| {
            f.active_epoch = None;
            f.frozen = true;
            f.quiet_active = false;
            f.probe_quiet_exempt = false;
            f.reconfirm_pending = false;
        });
        effects.push(Effect::Notify(ManagerNote::SessionDetached {
            sid: self.sid.clone(),
            reason: self.last_error.clone(),
        }));

        self.drain_stalled = false;
        self.recovering = false;
        let kind = if stop.kind == StopKind::Release {
            StopKind::Release
        } else {
            stop.post_kind.clone().unwrap_or(stop.kind.clone())
        };
        match kind {
            StopKind::Release => {
                if stop.already_released {
                    if self.release_persist_pending {
                        effects.extend(self.persist_release(
                            stop.waiters
                                .into_iter()
                                .map(|reply| (reply, true))
                                .collect(),
                        ));
                    } else {
                        effects.extend(stop.waiters.into_iter().map(|reply| {
                            Effect::Reply(reply, Ok(self.release_success_reply(now, true)))
                        }));
                    }
                } else {
                    self.finish_release(now);
                    effects.extend(
                        self.persist_release(
                            stop.waiters
                                .into_iter()
                                .map(|reply| (reply, false))
                                .collect(),
                        ),
                    );
                }
            }
            StopKind::Shutdown => {
                self.set_state(SessionState::Detached, now);
                for waiter in stop.waiters {
                    effects.push(Effect::Reply(waiter, Ok(self.ok_session(now))));
                }
            }
            StopKind::Removed => {
                self.device_present = false;
                self.known_real_path = None;
                self.set_last_error(Some("DEVICE_REMOVED".into()), None);
                self.set_state(SessionState::Detached, now);
                for waiter in stop.waiters {
                    effects.push(Effect::Reply(waiter, Ok(self.ok_session(now))));
                }
            }
            StopKind::Rebound => {
                self.set_last_error(Some("DEVICE_REBOUND_REQUIRED".into()), None);
                self.set_state(SessionState::Detached, now);
                let mut waiters = stop.waiters;
                effects.extend(self.reopen_known_device(now));
                for waiter in waiters.drain(..) {
                    effects.push(Effect::Reply(waiter, Ok(self.ok_session(now))));
                }
            }
            StopKind::IoError if self.pending_auto_login => {
                self.set_last_error(None, None);
                self.recovering = true;
                self.set_state(SessionState::Recovering, now);
                for waiter in stop.waiters {
                    effects.push(Effect::Reply(
                        waiter,
                        Ok(self.session_recovering_reply(now)),
                    ));
                }
            }
            StopKind::IoError | StopKind::Recover | StopKind::Clear | StopKind::LoginFailed => {
                match kind {
                    StopKind::IoError => self.set_last_error(None, None),
                    StopKind::Clear => self.set_last_error(Some("CLEARED".into()), None),
                    StopKind::LoginFailed => {}
                    _ => {}
                }
                self.set_state(SessionState::Detached, now);
                let open_effects = match kind {
                    StopKind::IoError | StopKind::Recover => self.reopen_known_device(now),
                    StopKind::Clear => self.request_reattach(now),
                    _ => Vec::new(),
                };
                effects.extend(open_effects);
                // Clear：回應帶 Python `_complete_drain` 的 `ATTACHING`＋`CLEARED`；`last_error` 於再解析後接受
                // `DeviceAttached`（開埠前，Python `_attach_by_id`）才清除（審查 F97）。
                for waiter in stop.waiters {
                    effects.push(Effect::Reply(waiter, Ok(self.ok_session(now))));
                }
            }
            StopKind::Bind => {
                self.set_last_error(Some("REBOUND".into()), None);
                self.set_state(SessionState::Detached, now);
                if let Some(target) = self.pending_bind.take() {
                    self.apply_bind(target, now);
                }
                for waiter in stop.waiters {
                    effects.push(Effect::Reply(waiter, Ok(self.ok_session(now))));
                }
            }
        }
        if self.shutdown_requested && self.pending_commit.is_none() {
            self.shutdown_complete = true;
        }
        effects
    }

    fn apply_aborted_receipt(
        &self,
        aborted_op: Option<&(OpId, String)>,
        receipt: &WorkerStoppedReceipt,
        effects: &mut Vec<Effect>,
    ) {
        let Some((op_id, cmd_id)) = aborted_op else {
            return;
        };
        let Some(record) = self.deps.registry.get(cmd_id) else {
            return;
        };
        let terminal = record.get("done_at").is_some_and(|value| !value.is_null())
            || matches!(
                record.get("status").and_then(PyValue::as_str),
                Some("done" | "error" | "canceled")
            );
        if terminal {
            return;
        }
        let op_ledger = receipt.ledger.ops.get(op_id);
        let confirmed_bytes = op_ledger.map_or(0, |op| op.confirmed_bytes);
        let class = if confirmed_bytes > 0 {
            crate::arbiter::AbortClass::InFlight
        } else if op_ledger.is_some_and(|op| op.unknown) {
            crate::arbiter::AbortClass::Unknown
        } else {
            crate::arbiter::AbortClass::NotSubmitted
        };
        self.deps
            .registry
            .complete(cmd_id, CmdOutcome::Aborted(class), &self.wall_iso());
        if confirmed_bytes > 0 {
            let payload_len = record
                .get("command")
                .and_then(PyValue::as_str)
                .map(|command| command.len() + 1);
            let mut meta = PyDict::new();
            meta.insert("event", PyValue::from("command_aborted_in_flight"));
            meta.insert("confirmed_tx_bytes", py_int(confirmed_bytes as i128));
            meta.insert(
                "tx_outcome",
                // Python `_map_aborted_result`：帳本 unknown 優先於 complete／partial。
                PyValue::from(if op_ledger.is_some_and(|op| op.unknown) {
                    "unknown"
                } else if payload_len == Some(confirmed_bytes) {
                    "complete"
                } else {
                    "partial"
                }),
            );
            effects.push(Effect::WalMeta {
                cmd_id: Some(cmd_id.clone()),
                meta,
            });
        }
    }

    /// clear 收尾的重新 attach：裝置在線時 ATTACHING 並交 manager 再解析（`Effect::Reattach`），
    /// 不在線（或 shutdown 中）維持 DETACHED。
    fn request_reattach(&mut self, now: Mono) -> Vec<Effect> {
        let by_id = self.profile.device_by_id.clone();
        let real_path = match self.known_real_path.clone() {
            Some(path) if self.device_present && !self.shutdown_requested && !by_id.is_empty() => {
                path
            }
            _ => {
                self.set_state(SessionState::Detached, now);
                return Vec::new();
            }
        };
        self.open_real_path = None;
        self.reattach_pending = true;
        self.set_state(SessionState::Attaching, now);
        vec![Effect::Reattach { by_id, real_path }]
    }

    fn reopen_known_device(&mut self, now: Mono) -> Vec<Effect> {
        if self.shutdown_requested || !self.device_present {
            self.set_state(SessionState::Detached, now);
            return Vec::new();
        }
        let Some(real_path) = self.known_real_path.clone() else {
            self.set_state(SessionState::Detached, now);
            return Vec::new();
        };
        self.generations.fsm_gen = self.generations.fsm_gen.saturating_add(1);
        self.open_generation = self.generations.fsm_gen;
        self.open_real_path = Some(real_path.clone());
        self.set_state(SessionState::Attaching, now);
        vec![Effect::OpenBridge {
            dev: DevicePath(real_path),
            cfg: self.serial_config(),
            precheck_holders: false,
        }]
    }

    fn finish_release(&mut self, now: Mono) {
        let (source, reason) = self
            .pending_release
            .as_ref()
            .map(|release| (release.source.clone(), release.reason.clone()))
            .unwrap_or_else(|| ("cli".into(), PyValue::Null));
        self.released_by = Some(source);
        self.released_at = Some(self.wall_iso());
        self.released_reason = reason;
        self.set_last_error(Some("RELEASED".into()), None);
        self.set_state(SessionState::Released, now);
    }

    // -----------------------------------------------------------------------
    // P5-T14：命令執行（出列 execute-time gate、OpFinished 結算）、agent Interactive Lease、
    // recover／force recover、reboot recovery、self-test、background／agent log capture。
    // -----------------------------------------------------------------------

    fn session_value(&self, now: Mono) -> PyValue {
        PyValue::Dict(self.session_dict(now))
    }

    fn session_dict(&self, now: Mono) -> PyDict {
        self.public(now, &self.public_ctx())
    }

    fn selector_error(&self, code: &str) -> PyDict {
        pydict! {
            "ok" => false,
            "error_code" => code,
            "selector" => self.profile.com.clone(),
        }
    }

    /// `_lease_context`：4 個 LeaseContext 鍵（Slice 2.1 無 human 鍵入追蹤，`human_active` 恆 false）。
    fn lease_ctx(&self) -> Vec<(&'static str, PyValue)> {
        let owner = self.lease.as_ref().map(|lease| lease.owner.clone());
        let human_attached = owner.as_deref().is_some_and(|o| o.starts_with("human:"));
        vec![
            (
                "interactive_owner",
                owner.map(PyValue::from).unwrap_or(PyValue::Null),
            ),
            ("human_attached", PyValue::Bool(human_attached)),
            ("human_active", PyValue::Bool(false)),
            (
                "recovery_mode",
                PyValue::Bool(self.lease.as_ref().is_some_and(|lease| lease.recovery_mode)),
            ),
        ]
    }

    fn extend_pairs(out: &mut PyDict, pairs: Vec<(&'static str, PyValue)>) {
        for (key, value) in pairs {
            out.insert(key, value);
        }
    }

    // ---------------- lease ----------------

    /// 開新 lease：owner_gen 遞增（舊 LeaseOwner job 由 fence `OwnerMismatch` 撤銷）。
    fn open_lease(&mut self, owner: &str, timeout_s: f64, recovery_mode: bool, now: Mono) -> String {
        let id = self.next_uuid();
        let lease = InteractiveLease::new(
            id.clone(),
            owner.to_string(),
            self.wall_iso(),
            timeout_s,
            now,
            recovery_mode,
        );
        self.lease = Some(lease);
        self.bump_owner_gen();
        id
    }

    fn bump_owner_gen(&mut self) {
        self.generations.owner_gen = self.generations.owner_gen.saturating_add(1);
        self.deps
            .fence
            .with(|f| f.owner_gen = self.generations.owner_gen);
    }

    /// 關閉目前 lease；`expired` 時留 tombstone（`keep_tombstone=True`）。
    fn close_lease(&mut self, expired: bool) {
        if let Some(lease) = self.lease.take() {
            if expired {
                self.tombstones.push(lease.interactive_id, "expired");
            }
            self.bump_owner_gen();
        }
    }

    fn clear_interactive(&mut self) {
        self.close_lease(false);
    }

    /// `_refresh_interactive_locked`：agent lease lazy 到期 → expired tombstone。
    fn refresh_lease(&mut self, now: Mono) {
        if self
            .lease
            .as_ref()
            .is_some_and(|lease| !lease.is_human() && lease.expired(now))
        {
            self.close_lease(true);
        }
    }

    fn lease_expiry_timer(&self) -> Option<Effect> {
        self.lease.as_ref().filter(|l| !l.is_human()).map(|lease| Effect::Schedule {
            at: lease.deadline(),
            ev: TimerEvent::LeaseExpiry {
                lease_id: lease.interactive_id.clone(),
                deadline: lease.deadline(),
            },
        })
    }

    fn interactive_open(
        &mut self,
        owner: String,
        timeout_s: f64,
        command: String,
        allow_attached: bool,
        reply: Reply,
        now: Mono,
    ) -> Vec<Effect> {
        if self.state == SessionState::Recovering {
            return vec![Effect::Reply(reply, Ok(self.selector_error("SESSION_RECOVERING")))];
        }
        if self.state == SessionState::Flashing {
            return vec![Effect::Reply(reply, Ok(self.selector_error("FLASHING_BUSY")))];
        }
        if self.generations.conn_epoch.is_none() {
            return vec![Effect::Reply(reply, Ok(self.selector_error("SESSION_NOT_READY")))];
        }
        let mut result;
        if self.state == SessionState::Ready {
            if self.foreground.is_some() {
                return vec![Effect::Reply(reply, Ok(self.selector_error("SESSION_BUSY")))];
            }
            self.refresh_lease(now);
            let mut soft_preempted = false;
            if let Some(existing) = self.lease.as_ref() {
                // #53 soft preempt：human lease 閒置時 agent 可取得控制權（Slice 2.1 無 human console 回放，直接替換）。
                if existing.is_human() && !owner.starts_with("human:") {
                    self.lease = None;
                    soft_preempted = true;
                } else {
                    let mut busy = pydict! { "ok" => false, "error_code" => "SESSION_INTERACTIVE_BUSY" };
                    busy.insert(
                        "interactive_session_id",
                        PyValue::from(existing.interactive_id.clone()),
                    );
                    return vec![Effect::Reply(reply, Ok(busy))];
                }
            }
            let id = self.open_lease(&owner, timeout_s, false, now);
            result = pydict! {
                "ok" => true,
                "interactive_id" => id,
                "session" => self.session_value(now),
                "recovery_mode" => false,
            };
            if soft_preempted {
                result.insert("soft_preempted", PyValue::Bool(true));
            }
        } else if !allow_attached {
            return vec![Effect::Reply(reply, Ok(self.selector_error("SESSION_NOT_READY")))];
        } else if self.state == SessionState::Attached {
            if self.vtty_alive() == Some(false) {
                return vec![Effect::Reply(reply, Ok(self.selector_error("SESSION_NOT_READY")))];
            }
            let tail = clean_text(&self.deps.rx.tail(BOOTLOADER_RX_TAIL_CHARS));
            let matched =
                matches_any_bootloader_prompt(&tail, &self.profile.template.bootloader_prompts);
            let mut boot_interrupt = false;
            let mut login_required = false;
            if matched.is_none() {
                if detect_boot_banner(&tail) {
                    boot_interrupt = true;
                } else if matches_login_or_password(&tail, &self.profile) {
                    login_required = true;
                } else {
                    let mut out = self.selector_error("SESSION_NOT_READY");
                    out.insert("error_detail", PyValue::from("NOT_BOOTLOADER"));
                    return vec![Effect::Reply(reply, Ok(out))];
                }
            }
            self.refresh_lease(now);
            let clamped = timeout_s.min(MAX_RECOVERY_LEASE_S);
            if let Some(existing) = self.lease.as_ref() {
                if existing.is_human() {
                    self.lease = None;
                } else {
                    let mut busy = pydict! { "ok" => false, "error_code" => "SESSION_INTERACTIVE_BUSY" };
                    busy.insert(
                        "interactive_session_id",
                        PyValue::from(existing.interactive_id.clone()),
                    );
                    return vec![Effect::Reply(reply, Ok(busy))];
                }
            }
            let id = self.open_lease(&owner, clamped, true, now);
            result = pydict! {
                "ok" => true,
                "interactive_id" => id,
                "session" => self.session_value(now),
                "recovery_mode" => true,
            };
            if boot_interrupt {
                result.insert("boot_interrupt", PyValue::Bool(true));
            }
            if login_required {
                result.insert("login_required", PyValue::Bool(true));
            }
        } else {
            return vec![Effect::Reply(reply, Ok(self.selector_error("SESSION_NOT_READY")))];
        }
        let interactive_id = self
            .lease
            .as_ref()
            .map(|lease| lease.interactive_id.clone())
            .unwrap_or_default();
        let mut effects = vec![Effect::Notify(ManagerNote::InteractiveOpened {
            sid: self.sid.clone(),
            interactive_id,
        })];
        effects.extend(self.lease_expiry_timer());
        if command.is_empty() || self.foreground.is_some() {
            effects.push(Effect::Reply(reply, Ok(result)));
            return effects;
        }
        let grant = self.new_grant(None);
        self.pending_ops.insert(
            grant.op_id,
            PendingOp::InteractiveOpen {
                reply,
                result,
            },
        );
        self.foreground = Some(grant.clone());
        effects.push(Effect::StartOp {
            grant,
            spec: OpSpec::InteractiveOpenCmd { command, owner },
        });
        effects
    }

    fn new_grant(&mut self, cmd_id: Option<String>) -> OpGrant {
        self.next_op_id = self.next_op_id.saturating_add(1);
        OpGrant {
            op_id: OpId(self.next_op_id),
            epoch: self.generations.conn_epoch.unwrap_or_default(),
            owner_gen: self.generations.owner_gen,
            fsm_gen: self.generations.fsm_gen,
            cmd_id,
        }
    }

    fn lease_lookup_error(&self, interactive_id: &str) -> PyDict {
        let code = if self.tombstones.status_of(interactive_id) == Some("expired") {
            "INTERACTIVE_EXPIRED"
        } else {
            "INTERACTIVE_NOT_FOUND"
        };
        pydict! { "ok" => false, "error_code" => code, "interactive_id" => interactive_id }
    }

    fn lease_matches(&self, interactive_id: &str) -> bool {
        self.lease
            .as_ref()
            .is_some_and(|lease| lease.interactive_id == interactive_id)
    }

    fn interactive_send(
        &mut self,
        interactive_id: String,
        data: &str,
        encoding: &str,
        reply: Reply,
        now: Mono,
    ) -> Vec<Effect> {
        if !self.lease_matches(&interactive_id) {
            return vec![Effect::Reply(reply, Ok(self.lease_lookup_error(&interactive_id)))];
        }
        let id = interactive_id.as_str();
        let expired = self
            .lease
            .as_ref()
            .is_some_and(|lease| !lease.is_human() && lease.expired(now));
        if expired {
            self.close_lease(true);
            return vec![Effect::Reply(
                reply,
                Ok(pydict! { "ok" => false, "error_code" => "INTERACTIVE_EXPIRED", "interactive_id" => id }),
            )];
        }
        let code = if self.generations.conn_epoch.is_none() {
            Some("SESSION_NOT_READY")
        } else if self.state == SessionState::Flashing {
            Some("FLASHING_BUSY")
        } else if self.foreground.is_some() {
            Some("SESSION_BUSY")
        } else {
            None
        };
        if let Some(code) = code {
            return vec![Effect::Reply(
                reply,
                Ok(pydict! { "ok" => false, "error_code" => code, "interactive_id" => id }),
            )];
        }
        let payload = match encode_interactive_payload(data, encoding) {
            Ok(payload) => payload,
            Err(error) => return vec![Effect::Reply(reply, Err(error))],
        };
        let owner = self
            .lease
            .as_ref()
            .map(|lease| lease.owner.clone())
            .unwrap_or_default();
        let grant = self.new_grant(None);
        self.pending_ops.insert(
            grant.op_id,
            PendingOp::InteractiveSend {
                reply,
                interactive_id: interactive_id.clone(),
            },
        );
        self.foreground = Some(grant.clone());
        vec![Effect::StartOp {
            grant,
            spec: OpSpec::InteractiveSend { payload, owner },
        }]
    }

    fn interactive_status(&mut self, interactive_id: &str, screen_chars: i128, now: Mono) -> PyDict {
        if !self.lease_matches(interactive_id) {
            return self.lease_lookup_error(interactive_id);
        }
        if self.generations.conn_epoch.is_none() {
            return pydict! { "ok" => false, "error_code" => "SESSION_NOT_READY", "interactive_id" => interactive_id };
        }
        if self
            .lease
            .as_ref()
            .is_some_and(|lease| !lease.is_human() && lease.expired(now))
        {
            self.close_lease(true);
            return pydict! { "ok" => false, "error_code" => "INTERACTIVE_EXPIRED", "interactive_id" => interactive_id };
        }
        let lease = self.lease.as_mut().expect("已確認 lease");
        lease.touch(now);
        let (owner, status, recovery_mode) =
            (lease.owner.clone(), lease.status, lease.recovery_mode);
        let screen = clean_text(&py_tail(&self.deps.rx.tail(0), screen_chars));
        pydict! {
            "ok" => true,
            "interactive_id" => interactive_id,
            "owner" => owner,
            "status" => status,
            "recovery_mode" => recovery_mode,
            "screen" => screen,
            "session" => self.session_value(now),
        }
    }

    fn interactive_close(&mut self, interactive_id: &str) -> PyDict {
        if !self.lease_matches(interactive_id) {
            return self.lease_lookup_error(interactive_id);
        }
        self.close_lease(false);
        pydict! { "ok" => true, "interactive_id" => interactive_id }
    }

    // ---------------- 出列與 execute-time gate ----------------

    /// 05 §5.1 execute-time 順序（completion regex → FLASHING → 非 READY → gate → recovering → lease）。
    fn execute_time_reject(&mut self, command: &QueuedCommand, now: Mono) -> Option<PyDict> {
        let completion = command
            .req
            .completion_regex
            .as_ref()
            .map(|pattern| PyValue::from(pattern.clone()));
        if !valid_completion_regex(completion.as_ref()) {
            return Some(pydict! { "ok" => false, "error_code" => "INVALID_COMPLETION_REGEX" });
        }
        let mode = normalized_mode(&command.req.mode);
        if self.state == SessionState::Flashing {
            return Some(self.selector_error("FLASHING_BUSY"));
        }
        if self.state != SessionState::Ready || self.generations.conn_epoch.is_none() {
            return Some(pydict! { "ok" => false, "error_code" => "SESSION_NOT_READY" });
        }
        let human = command.req.source.starts_with("human:");
        if !human && !self.is_passthrough() {
            // `_agent_gate_reject_locked`＋SC-03（execute 層）；expire 翻轉時同步 fence。
            let was_failed = self.quiet.failed;
            let reject =
                crate::quiet_gate::execute_gate_reject(&mut self.quiet, &self.profile.com, now);
            if !was_failed && self.quiet.failed {
                self.deps.fence.with(|f| f.reconfirm_pending = false);
            }
            if let Some(out) = reject {
                return Some(out);
            }
        }
        if self.recovering {
            return Some(pydict! { "ok" => false, "error_code" => "SESSION_RECOVERING" });
        }
        self.refresh_lease(now);
        if let Some(lease) = self.lease.as_ref() {
            if mode != "interactive" && !(!human && lease.is_human()) {
                let mut out = pydict! { "ok" => false, "error_code" => "SESSION_INTERACTIVE_BUSY" };
                out.insert(
                    "interactive_session_id",
                    PyValue::from(lease.interactive_id.clone()),
                );
                return Some(out);
            }
        }
        None
    }

    fn start_next(&mut self, now: Mono) -> Vec<Effect> {
        let mut effects = Vec::new();
        if self.foreground.is_some() || self.state != SessionState::Ready {
            return effects;
        }
        loop {
            let Some(command) = self.queue.pop() else {
                return effects;
            };
            if matches!(
                self.deps
                    .registry
                    .mark_running(&command.cmd_id, &self.wall_iso()),
                RunDecision::Skip
            ) {
                continue;
            }
            if let Some(reject) = self.execute_time_reject(&command, now) {
                self.deps.registry.complete(
                    &command.cmd_id,
                    CmdOutcome::Result(reject),
                    &self.wall_iso(),
                );
                continue;
            }
            let mode = normalized_mode(&command.req.mode).to_string();
            // #45 D-44：同 session 非 background 命令開始前，終結既有 background capture。
            if mode != "background" {
                for id in self.background_cmd_ids.clone() {
                    if let Some(capture) = self.background.get_mut(&id) {
                        capture.status = "done";
                    }
                    self.sync_bg(&id);
                }
            }
            let grant_cmd = Some(command.cmd_id.clone());
            if mode == "interactive" {
                let timeout = command
                    .req
                    .timeout_s
                    .max(self.profile.template.hard_timeout_s);
                let lease_id = self.open_lease(&command.req.source, timeout, false, now);
                effects.push(Effect::Notify(ManagerNote::InteractiveOpened {
                    sid: self.sid.clone(),
                    interactive_id: lease_id.clone(),
                }));
                effects.extend(self.lease_expiry_timer());
                let result = pydict! {
                    "ok" => true,
                    "execution_mode" => "interactive",
                    "interactive_session_id" => lease_id.clone(),
                    "stdout" => "",
                    "status" => "interactive",
                };
                if command.req.command.is_empty() {
                    self.deps.registry.complete(
                        &command.cmd_id,
                        CmdOutcome::Result(result),
                        &self.wall_iso(),
                    );
                    continue;
                }
                let grant = self.new_grant(grant_cmd);
                self.pending_ops.insert(
                    grant.op_id,
                    PendingOp::InteractiveCmd {
                        cmd_id: command.cmd_id.clone(),
                        result,
                    },
                );
                self.foreground = Some(grant.clone());
                let owner = command.req.source.clone();
                effects.push(Effect::StartOp {
                    grant,
                    spec: OpSpec::InteractiveOpenCmd {
                        command: command.req.command.clone(),
                        owner,
                    },
                });
                return effects;
            }
            if mode == "background" {
                self.create_background_capture(&command.cmd_id, now);
                self.background_cmd_ids.push(command.cmd_id.clone());
            }
            let reboot = !self.is_passthrough() && is_reboot_command(&command.req.command);
            let grant = self.new_grant(grant_cmd);
            self.pending_ops.insert(
                grant.op_id,
                PendingOp::Command {
                    cmd_id: command.cmd_id.clone(),
                    mode,
                    reboot,
                },
            );
            self.foreground = Some(grant.clone());
            self.foreground_reboot = reboot;
            // 審查 F14：reboot 命令 TX 一經提交，由 TxWriter 在同一次 Fence 臨界區內 arm quiet（不等 actor）。
            self.deps.fence.with(|f| {
                f.reboot_arm_op = reboot.then_some(grant.op_id);
            });
            effects.push(Effect::StartOp {
                grant,
                spec: OpSpec::Command(command),
            });
            return effects;
        }
    }

    // ---------------- background capture ----------------

    fn create_background_capture(&mut self, cmd_id: &str, now: Mono) {
        self.background_seq = self.background_seq.saturating_add(1);
        let current = self.deps.wal.current_seq();
        let capture = BackgroundCapture::new(
            cmd_id.to_string(),
            self.sid.clone(),
            current.saturating_add(1),
            current,
            self.profile.template.quiet_window_s,
            self.wall_iso(),
            now,
            self.background_seq,
        );
        let created_at = capture.created_at.clone();
        self.background.insert(cmd_id.to_string(), capture);
        // `_evict_background_locked`：全域（manager 層）上限，依 `created_at` 淘汰最舊的已終結 capture；
        // 擁有者以 COM 標識（rematerialize 不變），本 session 者就地移除，其餘交擁有者（審查 F28）。
        let owner = self.profile.com.clone();
        for (victim_owner, id) in self.deps.bg_index.register(cmd_id, &owner, &created_at) {
            if victim_owner == owner {
                self.drop_background_capture(&id);
            } else {
                self.deps.bg_index.evict_remote(&victim_owner, &id);
            }
        }
    }

    /// 丟棄被淘汰的 capture，並同步移除 `background_cmd_ids`（#45 D-94）。
    fn drop_background_capture(&mut self, id: &str) {
        self.background.shift_remove(id);
        self.background_cmd_ids.retain(|existing| existing != id);
    }

    /// 將本地 capture 的 `status` 同步到全域索引（淘汰只考慮已終結者）。
    fn sync_bg(&self, id: &str) {
        if let Some(capture) = self.background.get(id) {
            self.deps.bg_index.set_active(id, capture.status == "active");
        }
    }

    /// `_set_terminal_capture_locked`：未掛載者建立並回填 chunks；設定終態。
    fn set_terminal_capture(
        &mut self,
        cmd_id: &str,
        chunk: Option<String>,
        error_code: Option<&str>,
        now: Mono,
    ) {
        if cmd_id.is_empty() {
            return;
        }
        if !self.background.contains_key(cmd_id) {
            self.create_background_capture(cmd_id, now);
            if let Some(chunk) = chunk.filter(|c| !c.is_empty()) {
                if let Some(capture) = self.background.get_mut(cmd_id) {
                    capture.add_chunk(&chunk, BG_CAPTURE_MAX_BYTES);
                }
            }
        }
        let current = self.deps.wal.current_seq();
        if let Some(capture) = self.background.get_mut(cmd_id) {
            capture.last_seq = current;
            capture.last_activity = now;
            capture.status = if error_code.is_some() { "error" } else { "done" };
            capture.error_code = error_code.map(str::to_string);
        }
        self.sync_bg(cmd_id);
    }

    /// RX 進入 background capture（`_on_bridge_rx`）；agent log 由 reader 端 `CaptureObserver` 寫入（EVT-D-L1 前景
    /// gate 由 `publish_capture_foreground` 發布）。
    fn capture_rx(&mut self, chunk: &str, wal_seq: u64, now: Mono) {
        for id in &self.background_cmd_ids {
            if let Some(capture) = self.background.get_mut(id) {
                if capture.status != "active" {
                    continue;
                }
                capture.add_chunk(chunk, BG_CAPTURE_MAX_BYTES);
                capture.last_activity = now;
                capture.last_seq = wal_seq;
            }
        }
        // agent log 改由 reader 端 `CaptureObserver` 同步寫入（P7 Task 10；Python `_on_bridge_rx` 於 reader 執行緒寫檔）。
    }

    fn result_tail(
        &mut self,
        cmd_id: &str,
        from_chunk: i128,
        limit: i128,
        now: Mono,
    ) -> Result<PyDict, PyErr> {
        if let Some(capture) = self.background.get_mut(cmd_id) {
            let result = capture.result(from_chunk, limit, now);
            self.sync_bg(cmd_id);
            return result;
        }
        Ok(bg_fallback_from_registry(&self.deps.registry, cmd_id, from_chunk))
    }

    /// session detach：停止 agent log、終結 background capture、清除 `background_cmd_ids`。
    fn detach_captures(&mut self) {
        self.stop_log_capture();
        for id in std::mem::take(&mut self.background_cmd_ids) {
            if let Some(capture) = self.background.get_mut(&id) {
                capture.status = "done";
            }
            self.sync_bg(&id);
        }
    }

    // ---------------- agent log capture ----------------

    /// 測試／manager 注入 log 檔系統、時鐘與預設目錄。
    pub fn set_capture_env(&mut self, env: CaptureEnv) {
        self.capture_env = env;
    }

    /// self-test 的外部觀測注入（RELEASED holder 掃描、哨兵 PTY 存活）。
    pub fn set_self_test_probes(&mut self, probes: SelfTestProbes) {
        self.self_test_probes = probes;
    }

    fn vtty_alive(&self) -> Option<bool> {
        if let Some(probe) = self.self_test_probes.vtty_alive.as_ref() {
            return probe();
        }
        // Python `snapshot()["vtty_alive"] = bool(primary and os.path.exists(primary))`。
        self.current_vtty()
            .map(|path| std::path::Path::new(path).exists())
    }

    /// Python `_suggest_profile_from_rx`：`clean_text(rx_tail(512))` 依序比對非 passthrough template 的
    /// `prompt_regex`（`re.search`；不合法 regex 略過），回第一個命中的 template 名。
    fn suggest_profile_from_rx(&self) -> Option<String> {
        let templates = &self.self_test_probes.templates;
        if templates.is_empty() {
            return None;
        }
        let snapshot = clean_text(&self.deps.rx.tail(BOOTLOADER_RX_TAIL_CHARS));
        if snapshot.is_empty() {
            return None;
        }
        templates
            .iter()
            .filter(|tpl| tpl.platform != "passthrough")
            .find(|tpl| {
                sw_proto::pyre::compile(&tpl.prompt_regex, sw_proto::pyre::PyFlags(0))
                    .is_ok_and(|re| re.search(&snapshot).is_some())
            })
            .map(|tpl| tpl.profile_name.clone())
    }

    /// 現行 bridge 的哨兵 vtty（無 bridge 時為 `None`）。
    fn current_vtty(&self) -> Option<&str> {
        self.generations.conn_epoch.and(self.vtty.as_deref())
    }

    /// `public()` 的 console 欄位（Python `to_public_dict`：有 bridge 時 `len(list_consoles())` 與 primary vtty）。
    fn public_ctx(&self) -> PublicCtx {
        match self.generations.conn_epoch {
            Some(_) => PublicCtx {
                console_count: self.console_count as i128,
                vtty: self.vtty.clone(),
                console_endpoint: None,
            },
            None => PublicCtx {
                console_count: 0,
                vtty: None,
                console_endpoint: None,
            },
        }
    }

    fn resolve_log_dir(&self) -> String {
        if let Some(dir) = self.profile.template.log_dir.as_deref().filter(|d| !d.is_empty()) {
            return dir.to_string();
        }
        if let Some(dir) = self.capture_env.default_log_dir.as_deref() {
            return dir.to_string();
        }
        if let Some(dir) = self.deps.env.var("SERIALWRAP_LOG_DIR").filter(|d| !d.is_empty()) {
            return dir;
        }
        let home = self
            .deps
            .env
            .var("HOME")
            .or_else(|| self.deps.env.var("USERPROFILE"))
            .unwrap_or_else(|| ".".into());
        std::path::Path::new(&home)
            .join("b-log")
            .to_string_lossy()
            .into_owned()
    }

    fn log_timestamp(&self) -> String {
        let (wall_us, offset) = match &self.capture_env.clock {
            Some(clock) => (clock.wall_unix_us(), clock.local_utc_offset_s()),
            None => (
                std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)
                    .map(|d| d.as_micros() as i64)
                    .unwrap_or(0),
                0,
            ),
        };
        strftime_local(wall_us, offset, "%y%m%d-%H%M%S")
    }

    fn log_start(&mut self, now: Mono) -> Result<PyDict, PyErr> {
        if let Some(cap) = self.log_capture.as_ref().filter(|c| c.status == "active") {
            return Ok(crate::capture::log_start_reply(cap, true, self.session_dict(now)));
        }
        let log_dir = self.resolve_log_dir();
        // Python `os.makedirs` 在 try 之外：失敗為例外（framing EXCEPTION），不是 LOG_OPEN_FAILED。
        self.capture_env
            .fs
            .makedirs(&log_dir)
            .map_err(|error| PyErr::value_error(os_error_text(&error, &log_dir)))?;
        let filename = format!("{}_{}.log", self.profile.com, self.log_timestamp());
        let log_path = std::path::Path::new(&log_dir)
            .join(filename)
            .to_string_lossy()
            .into_owned();
        let rng = self.deps.rng.clone();
        let mut fill = |bytes: &mut [u8]| rng(bytes);
        let capture_id = uuid4_dashed(&mut fill);
        let file = match self.capture_env.fs.open_append(&log_path) {
            Ok(file) => file,
            Err(error) => {
                return Ok(crate::capture::log_open_failed_reply(&os_error_text(
                    &error, &log_path,
                )));
            }
        };
        let cap = AgentLogCapture {
            capture_id: capture_id.clone(),
            log_path,
            started_at: self.wall_iso(),
            line_count: 0,
            byte_count: 0,
            status: "active",
        };
        self.capture_observer.install(capture_id, file);
        self.deps.rx.set_capture(Some(self.capture_observer.clone()));
        let reply = crate::capture::log_start_reply(&cap, false, self.session_dict(now));
        self.log_capture = Some(cap);
        Ok(reply)
    }

    /// 停止 capture（Python `_stop_capture_locked`）；回停止前最後的 `(line_count, byte_count)`。
    fn stop_log_capture(&mut self) -> (u64, u64) {
        let counts = self
            .log_capture
            .take()
            .and_then(|cap| self.capture_observer.remove(&cap.capture_id))
            .unwrap_or((0, 0));
        self.deps.rx.set_capture(None);
        counts
    }

    fn log_stop(&mut self, now: Mono) -> PyDict {
        let Some(cap) = self.log_capture.clone() else {
            return crate::capture::no_active_capture_reply(self.session_dict(now));
        };
        let (line_count, byte_count) = self.stop_log_capture();
        crate::capture::log_stop_reply(&cap, line_count, byte_count)
    }

    fn log_status(&self, now: Mono) -> PyDict {
        let Some(cap) = self.log_capture.as_ref() else {
            return crate::capture::log_status_reply(None, self.session_dict(now));
        };
        let (line_count, byte_count) = self
            .capture_observer
            .counts(&cap.capture_id)
            .unwrap_or((0, 0));
        crate::capture::log_status_reply(Some((cap, line_count, byte_count)), self.session_dict(now))
    }

    // ---------------- OpFinished ----------------

    /// Python `foreground_busy`：只有命令（含 mode=interactive 命令）是前景 operation；recover、self-test、
    /// interactive send 不設此旗標（審查 F27）。
    /// merge P5 348eb2f：取代 P6 Task 6（增補 #26）的 `todo!` 骨架；P6 的 `replay_hold` 條件由 P6 合入時補上。
    pub fn foreground_busy(&self) -> bool {
        self.foreground.as_ref().is_some_and(|grant| {
            matches!(
                self.pending_ops.get(&grant.op_id),
                Some(PendingOp::Command { .. } | PendingOp::InteractiveCmd { .. })
            )
        })
    }

    fn op_finished(
        &mut self,
        op_id: OpId,
        epoch: u64,
        outcome: OpOutcome,
        now: Mono,
    ) -> Vec<Effect> {
        if self.stop.as_ref().is_some_and(|stop| {
            stop.epoch == epoch
                && stop
                    .aborted_op
                    .as_ref()
                    .is_some_and(|(aborted_id, _)| *aborted_id == op_id)
        }) {
            self.pending_ops.remove(&op_id);
            return Vec::new();
        }
        let pending = self.pending_ops.remove(&op_id);
        if self
            .side_ops
            .get(&op_id)
            .is_some_and(|grant| grant.epoch == epoch)
        {
            // 並行 op（READY recover／self-test）：不動前景槽。
            self.side_ops.remove(&op_id);
            let mut effects = match pending {
                Some(PendingOp::Recover { reply }) => self.finish_recover(reply, outcome, now),
                Some(other) => self.answer_op(other, outcome, now, true),
                None => Vec::new(),
            };
            effects.push(Effect::SettleTx { epoch, op_id });
            effects.extend(self.start_next(now));
            return effects;
        }
        let is_current = self
            .foreground
            .as_ref()
            .is_some_and(|grant| grant.op_id == op_id && grant.epoch == epoch);
        if !is_current {
            // 前景已被 drain／重連清除：只回覆仍在等待的 RPC，不再套用 session 副作用。
            return match pending {
                Some(pending) => self.answer_stale_op(pending, outcome, now),
                None => Vec::new(),
            };
        }
        let grant = self.foreground.take().expect("已確認前景");
        self.foreground_reboot = false;
        let mut effects = Vec::new();
        match pending {
            Some(PendingOp::Command {
                cmd_id,
                mode,
                reboot,
            }) => effects.extend(self.finish_command(&cmd_id, &mode, reboot, outcome, now)),
            Some(PendingOp::InteractiveCmd { cmd_id, result }) => {
                let outcome = match outcome {
                    OpOutcome::Sent { .. } => CmdOutcome::Result(result),
                    OpOutcome::SendRefused(reject) => CmdOutcome::Result(pydict! {
                        "ok" => false,
                        "error_code" => lease_refused_command_code(reject),
                    }),
                    _ => CmdOutcome::SendFailed,
                };
                self.deps.registry.complete(&cmd_id, outcome, &self.wall_iso());
            }
            Some(PendingOp::Recover { reply }) => {
                effects.extend(self.finish_recover(reply, outcome, now));
            }
            Some(other) => effects.extend(self.answer_op(other, outcome, now, true)),
            None => {
                if let Some(cmd_id) = grant.cmd_id {
                    let result = match outcome {
                        OpOutcome::CommandResult(result) | OpOutcome::SelfTest(result) => {
                            CmdOutcome::Result(result)
                        }
                        OpOutcome::SendFailed => CmdOutcome::SendFailed,
                        _ => CmdOutcome::Aborted(crate::arbiter::AbortClass::Unknown),
                    };
                    self.deps
                        .registry
                        .complete(&cmd_id, result, &self.wall_iso());
                }
            }
        }
        effects.push(Effect::SettleTx { epoch, op_id });
        effects.extend(self.start_next(now));
        effects
    }

    fn answer_stale_op(&mut self, pending: PendingOp, outcome: OpOutcome, now: Mono) -> Vec<Effect> {
        match pending {
            PendingOp::Command { .. } | PendingOp::InteractiveCmd { .. } => Vec::new(),
            PendingOp::Recover { reply } => {
                let result = match outcome {
                    OpOutcome::Recovered { key, stdout } => Ok(pydict! {
                        "ok" => true,
                        "error_code" => "PROMPT_TIMEOUT_RECOVERED",
                        "stdout" => stdout,
                        "partial" => true,
                        "recovery_action" => key,
                    }),
                    OpOutcome::CommandResult(result) => Ok(result),
                    OpOutcome::Error(error) => Err(error),
                    _ => Ok(prompt_timeout_none()),
                };
                vec![Effect::Reply(reply, result)]
            }
            other => self.answer_op(other, outcome, now, false),
        }
    }

    /// 回覆 self-test／interactive send／interactive open 的 op 結果。
    fn answer_op(
        &mut self,
        pending: PendingOp,
        outcome: OpOutcome,
        now: Mono,
        current: bool,
    ) -> Vec<Effect> {
        match pending {
            PendingOp::InteractiveSend {
                reply,
                interactive_id,
            } => {
                let id = interactive_id.as_str();
                let result = match outcome {
                    OpOutcome::Sent { bytes } => {
                        if let Some(lease) = self.lease.as_mut().filter(|l| l.interactive_id == id) {
                            lease.touch(now);
                        }
                        Ok(pydict! { "ok" => true, "interactive_id" => id, "bytes" => py_int(bytes as i128) })
                    }
                    OpOutcome::SendRefused(reject) => match lease_refused_send_code(reject) {
                        Some(code) => Ok(pydict! { "ok" => false, "error_code" => code, "interactive_id" => id }),
                        None => Err(PyErr::value_error(format!(
                            "tx incomplete: not_submitted 0/0 {}",
                            reject_reason(reject)
                        ))),
                    },
                    OpOutcome::Error(error) => Err(error),
                    _ => Err(PyErr::value_error("interactive send 未完成")),
                };
                vec![Effect::Reply(reply, result)]
            }
            PendingOp::InteractiveOpen { reply, result } => {
                let result = match outcome {
                    OpOutcome::Sent { .. } => Ok(result),
                    OpOutcome::SendRefused(FenceReject::OwnerMismatch) => {
                        Ok(pydict! { "ok" => false, "error_code" => "INTERACTIVE_EXPIRED" })
                    }
                    OpOutcome::SendRefused(reject) => Err(PyErr::value_error(format!(
                        "tx incomplete: not_submitted 0/0 {}",
                        reject_reason(reject)
                    ))),
                    OpOutcome::Error(error) => Err(error),
                    _ => Err(PyErr::value_error("interactive open 命令未完成")),
                };
                vec![Effect::Reply(reply, result)]
            }
            PendingOp::SelfTest {
                reply,
                probe_len,
                base,
                lease_ctx,
            } => {
                let result = match outcome {
                    OpOutcome::SelfTestRefused(FenceReject::Quiet | FenceReject::ReconfirmPending) => {
                        Ok(self.finish_self_test(self.autoboot_quiet_self_test(now)))
                    }
                    OpOutcome::SelfTestRefused(reject) => Err(PyErr::value_error(format!(
                        "tx incomplete: not_submitted 0/{probe_len} {}",
                        reject_reason(reject)
                    ))),
                    OpOutcome::SelfTest(probe) => {
                        let ok = probe.get("probe_ok").and_then(PyValue::as_bool) == Some(true);
                        if ok && current {
                            self.confirm_ready();
                        }
                        Ok(self.finish_self_test(self.probe_self_test_result(ok, &probe, &base, lease_ctx, now)))
                    }
                    OpOutcome::Error(error) => Err(error),
                    _ => Err(PyErr::value_error("self-test probe 未完成")),
                };
                vec![Effect::Reply(reply, result)]
            }
            PendingOp::Recover { reply } => vec![Effect::Reply(reply, Ok(prompt_timeout_none()))],
            PendingOp::Command { .. } | PendingOp::InteractiveCmd { .. } => Vec::new(),
        }
    }

    /// 命令 op 的結算（registry 終態＋session 副作用）。
    fn finish_command(
        &mut self,
        cmd_id: &str,
        mode: &str,
        reboot: bool,
        outcome: OpOutcome,
        now: Mono,
    ) -> Vec<Effect> {
        let mut effects = Vec::new();
        let result = match outcome {
            OpOutcome::CommandResult(result) => {
                if reboot && result.get_truthy("ok") {
                    // #139／#162：prompt 2 s 內回來只清 TX 靜默維度，reconfirm pending 保留。
                    self.quiet.clear_quiet();
                    self.deps.fence.with(|f| {
                        f.quiet_active = false;
                        f.probe_quiet_exempt = false;
                    });
                }
                CmdOutcome::Result(result)
            }
            OpOutcome::SendFailed => CmdOutcome::SendFailed,
            OpOutcome::Recovered { key, stdout } => {
                self.set_terminal_capture(
                    cmd_id,
                    Some(stdout.clone()),
                    Some("PROMPT_TIMEOUT_RECOVERED"),
                    now,
                );
                self.flush_queue();
                CmdOutcome::Result(pydict! {
                    "ok" => true,
                    "error_code" => "PROMPT_TIMEOUT_RECOVERED",
                    "stdout" => stdout,
                    "partial" => true,
                    "recovery_action" => key,
                })
            }
            OpOutcome::PromptTimeout { stdout } => {
                self.set_terminal_capture(cmd_id, Some(stdout), Some("PROMPT_TIMEOUT"), now);
                if !self.is_passthrough() {
                    self.transition_to_attached("PROMPT_TIMEOUT", now);
                }
                CmdOutcome::Result(prompt_timeout_none())
            }
            OpOutcome::RebootExpected => {
                self.pending_auto_login = true;
                self.recovering = true;
                self.set_state(SessionState::Recovering, now);
                self.set_last_error(None, None);
                self.on_detached_flush();
                let timeout_ns = secs_ns(self.profile.template.hard_timeout_s);
                self.reboot_recovery = Some(RebootRecovery {
                    deadline: now.saturating_add(timeout_ns),
                    cap: now
                        .saturating_add(BOOT_QUIET_WINDOW_NS)
                        .saturating_add(timeout_ns),
                    timeout_ns,
                    next_attempt: now,
                });
                CmdOutcome::Result(pydict! {
                    "ok" => true,
                    "execution_mode" => mode,
                    "stdout" => "",
                    "partial" => true,
                    "status" => "recovering",
                    "recovery_action" => "EXPECT_REBOOT",
                })
            }
            // Python：救援路徑例外穿出 → `_map_aborted_result`：命令已確認 bytes > 0 時由 OpRunner 直接回
            // COMMAND_ABORTED_IN_FLIGHT＋META（審查 F26）；此處只剩 0 byte（未有命令 TX 帳）→ SEND_FAILED。
            OpOutcome::Error(_) => CmdOutcome::Result(pydict! { "ok" => false, "error_code" => "SEND_FAILED" }),
            _ => CmdOutcome::SendFailed,
        };
        self.deps.registry.complete(cmd_id, result, &self.wall_iso());
        if self.reboot_recovery.is_some() {
            effects.extend(self.reboot_recovery_tick(now));
        }
        effects
    }

    /// `_on_command_flush`／`_on_detached` 的佇列終結（FLUSHED_BY_RECOVERY）。
    fn flush_queue(&mut self) {
        let _ = self.queue.drain_all();
        self.deps
            .registry
            .flush_session(&self.sid, "FLUSHED_BY_RECOVERY", &self.wall_iso());
    }

    /// `_on_detached`：arbiter `unregister_session`（佇列 flush＋解除註冊直到重新 READY）。
    fn on_detached_flush(&mut self) {
        self.flush_queue();
        self.queue_registered = false;
    }

    /// `_transition_to_attached`：READY → ATTACHED（drain 中或 FLASHING／RELEASED 不覆寫）。
    fn transition_to_attached(&mut self, reason: &str, now: Mono) {
        if self.stop.is_some() || self.pending_open_stop.is_some() {
            return;
        }
        if matches!(self.state, SessionState::Flashing | SessionState::Released) {
            return;
        }
        let was_ready = self.state == SessionState::Ready;
        self.set_state(SessionState::Attached, now);
        self.set_last_error(Some(reason.to_string()), None);
        self.recovering = false;
        self.pending_auto_login = false;
        if was_ready {
            self.on_detached_flush();
        }
    }

    // ---------------- recover ----------------

    fn recover_request(&mut self, timeout_s: f64, force: bool, reply: Reply, now: Mono) -> Vec<Effect> {
        if self.drain_in_progress() {
            return vec![Effect::Reply(reply, Ok(self.session_recovering_reply(now)))];
        }
        if self.state == SessionState::Released {
            return vec![Effect::Reply(
                reply,
                Ok(pydict! {
                    "ok" => true,
                    "released" => true,
                    "recommended_action" => "device_attach",
                    "session" => self.session_value(now),
                }),
            )];
        }
        let flash_busy = self.state == SessionState::Flashing
            || self.deps.fence.with(|f| f.flash_mode != FlashMode::Off);
        if flash_busy {
            let mut out = self.selector_error("FLASHING_BUSY");
            out.insert("session", self.session_value(now));
            return vec![Effect::Reply(reply, Ok(out))];
        }
        self.credentials_warned = false;
        if self.generations.conn_epoch.is_none() {
            if self.device_present && self.open_real_path.is_none() && self.pending_attach.is_none()
            {
                let real_path = self
                    .known_real_path
                    .clone()
                    .unwrap_or_else(|| self.profile.device_by_id.clone());
                self.set_last_error(None, None);
                self.generations.fsm_gen = self.generations.fsm_gen.saturating_add(1);
                self.open_generation = self.generations.fsm_gen;
                self.open_real_path = Some(real_path.clone());
                self.set_state(SessionState::Attaching, now);
                return vec![
                    Effect::OpenBridge {
                        dev: DevicePath(real_path),
                        cfg: self.serial_config(),
                        precheck_holders: false,
                    },
                    Effect::Reply(
                        reply,
                        Ok(pydict! {
                            "ok" => true,
                            "recovering" => false,
                            "action" => "REATTACH",
                            "session" => self.session_value(now),
                        }),
                    ),
                ];
            }
            if self.device_present {
                // attach 已在進行（開檔中）：Python `_spawn_attach` 對 in-flight attach 直接返回，回應仍是
                // REATTACH；不重複發 `OpenBridge`（審查 F29）。
                self.set_last_error(None, None);
                self.set_state(SessionState::Attaching, now);
                return vec![Effect::Reply(
                    reply,
                    Ok(pydict! {
                        "ok" => true,
                        "recovering" => false,
                        "action" => "REATTACH",
                        "session" => self.session_value(now),
                    }),
                )];
            }
            return vec![Effect::Reply(reply, Ok(self.error_session("SESSION_NOT_READY", now)))];
        }
        match self.state {
            SessionState::Attached => {
                if force {
                    return self.start_force_recover(reply, ForceKind::Direct, now);
                }
                self.reprobe_existing_bridge(reply, ReprobeWaiter::Recover { force: false }, now)
            }
            SessionState::Ready => {
                // Python `recover_session` READY 分支不看 `foreground_busy`：救援鍵與在途命令並行（審查 F25）。
                let grant = self.new_grant(None);
                self.pending_ops
                    .insert(grant.op_id, PendingOp::Recover { reply });
                self.side_ops.insert(grant.op_id, grant.clone());
                vec![Effect::StartOp {
                    grant,
                    spec: OpSpec::Recover { timeout_s, force },
                }]
            }
            _ => {
                if force {
                    return self.start_force_recover(reply, ForceKind::Direct, now);
                }
                vec![Effect::Reply(reply, Ok(self.error_session("SESSION_NOT_READY", now)))]
            }
        }
    }

    /// READY recover 的 `_recover_after_failure` 結算（source `system:recover`、cmd_id 空）。
    fn finish_recover(&mut self, reply: Reply, outcome: OpOutcome, now: Mono) -> Vec<Effect> {
        match outcome {
            OpOutcome::Recovered { key, stdout } => {
                self.flush_queue();
                vec![Effect::Reply(
                    reply,
                    Ok(pydict! {
                        "ok" => true,
                        "error_code" => "PROMPT_TIMEOUT_RECOVERED",
                        "stdout" => stdout,
                        "partial" => true,
                        "recovery_action" => key,
                    }),
                )]
            }
            OpOutcome::PromptTimeout { .. } => {
                if self.is_passthrough() {
                    return vec![Effect::Reply(reply, Ok(prompt_timeout_none()))];
                }
                self.transition_to_attached("PROMPT_TIMEOUT", now);
                self.start_force_recover(reply, ForceKind::AfterPromptTimeout, now)
            }
            OpOutcome::CommandResult(result) => vec![Effect::Reply(reply, Ok(result))],
            OpOutcome::Error(error) => vec![Effect::Reply(reply, Err(error))],
            _ => vec![Effect::Reply(reply, Ok(prompt_timeout_none()))],
        }
    }

    /// `_force_recover`：clear＋reattach，最多等 10 s READY。
    fn start_force_recover(&mut self, reply: Reply, kind: ForceKind, now: Mono) -> Vec<Effect> {
        if let Some(previous) = self.force_recover.take() {
            // 既有等待者以逾時收尾（同一 session 不疊加 force recover）。
            let mut effects = self.answer_force_recover(previous, false, now);
            effects.extend(self.start_force_recover(reply, kind, now));
            return effects;
        }
        self.force_recover = Some(ForceRecover {
            reply,
            deadline: now.saturating_add(FORCE_RECOVER_WAIT_NS),
            kind,
        });
        self.begin_stop(StopKind::Clear, None, now).0
    }

    fn answer_force_recover(&mut self, pending: ForceRecover, recovered: bool, now: Mono) -> Vec<Effect> {
        let session = self.session_value(now);
        let result = match (pending.kind, recovered) {
            (ForceKind::Direct, true) => pydict! {
                "ok" => true,
                "recovering" => false,
                "action" => "FORCE_CLEAR_REATTACH",
                "recovered" => true,
                "session" => session,
            },
            (ForceKind::Direct, false) => pydict! {
                "ok" => false,
                "error_code" => "FORCE_RECOVER_TIMEOUT",
                "action" => "FORCE_CLEAR_REATTACH",
                "recovered" => false,
                "session" => session,
            },
            (ForceKind::AfterPromptTimeout, true) => pydict! {
                "ok" => true,
                "error_code" => "PROMPT_TIMEOUT_FORCE_RECOVERED",
                "partial" => true,
                "recovery_action" => "FORCE_CLEAR_REATTACH",
                "session" => session,
            },
            (ForceKind::AfterPromptTimeout, false) => prompt_timeout_none(),
        };
        vec![Effect::Reply(pending.reply, Ok(result))]
    }

    /// 每個事件後檢查 force recover 等待者（READY 或 10 s 到期）。
    fn poll_force_recover(&mut self, now: Mono) -> Vec<Effect> {
        let Some(pending) = self.force_recover.as_ref() else {
            return Vec::new();
        };
        let ready = self.state == SessionState::Ready && !self.drain_in_progress();
        if !ready && now < pending.deadline {
            return Vec::new();
        }
        let pending = self.force_recover.take().expect("已確認等待者");
        self.answer_force_recover(pending, ready, now)
    }

    /// 再探等待者的回應（attach：`_attach_state_result`；recover：REPROBE payload，SC-34）。
    fn reprobe_reply(&mut self, reply: Reply, kind: ReprobeWaiter, now: Mono) -> Vec<Effect> {
        match kind {
            ReprobeWaiter::Attach => vec![Effect::Reply(reply, Ok(self.attach_state_result(now)))],
            ReprobeWaiter::Recover { force } => {
                if matches!(self.state, SessionState::Flashing | SessionState::Released) {
                    if force {
                        return self.start_force_recover(reply, ForceKind::Direct, now);
                    }
                    let mut out = pydict! { "ok" => false, "error_code" => "STATE_CHANGED" };
                    out.insert("session", self.session_value(now));
                    return vec![Effect::Reply(reply, Ok(out))];
                }
                let recovered = self.state == SessionState::Ready;
                let mut payload = pydict! {
                    "ok" => true,
                    "recovering" => false,
                    "action" => "REPROBE",
                    "recovered" => recovered,
                    "session" => self.session_value(now),
                };
                if !recovered {
                    payload.insert("error_code", PyValue::from("SESSION_NOT_READY"));
                    payload.insert("last_error", option_string(self.last_error.as_deref()));
                    payload.insert(
                        "last_error_detail",
                        option_string(self.last_error_detail.as_deref()),
                    );
                    let matched = if self.is_passthrough() {
                        None
                    } else {
                        self.bootloader_evidence()
                    };
                    if matched.is_some() || self.last_error.as_deref() == Some("BOOTLOADER_STUCK") {
                        payload.insert("classification", PyValue::from("BOOTLOADER"));
                        payload.insert("recommended_action", PyValue::from("recover_interactive"));
                        if let Some(matched) = matched {
                            payload.insert("matched_prompt", PyValue::from(matched));
                        }
                    }
                    if force {
                        return self.start_force_recover(reply, ForceKind::Direct, now);
                    }
                }
                vec![Effect::Reply(reply, Ok(payload))]
            }
        }
    }

    /// `_bootloader_evidence`：RX tail 尾行命中 bootloader prompt（缺配置時退回 `UBOOT_FALLBACK_PROMPTS`）。
    fn bootloader_evidence(&self) -> Option<String> {
        self.generations.conn_epoch?;
        let tail = clean_text(&self.deps.rx.tail(BOOTLOADER_RX_TAIL_CHARS));
        matches_any_bootloader_prompt(&tail, &self.bootloader_patterns())
    }

    fn bootloader_patterns(&self) -> Vec<String> {
        if self.profile.template.bootloader_prompts.is_empty() {
            UBOOT_FALLBACK_PROMPTS.iter().map(|p| p.to_string()).collect()
        } else {
            self.profile.template.bootloader_prompts.clone()
        }
    }

    // ---------------- reboot recovery ----------------

    /// `_spawn_reboot_recovery` 的 actor 驅動版：quiet 期間被動等待（延伸期限至 cap）、其後每 1 s 以
    /// `ensure_ready` 嘗試；期限到 → ATTACHED／`RECOVERY_TIMEOUT`。
    fn reboot_recovery_tick(&mut self, now: Mono) -> Vec<Effect> {
        let Some(rr) = self.reboot_recovery.as_mut() else {
            return Vec::new();
        };
        if self.state != SessionState::Recovering || !self.recovering {
            self.reboot_recovery = None;
            return Vec::new();
        }
        if self.stop.is_some() || self.pending_open_stop.is_some() || self.login_run.is_some() {
            return Vec::new();
        }
        // `_reboot_recovery`：quiet_active＝`_boot_quiet_blocks_probe`（console unlock 不延長、不等待）。
        let quiet_blocks = boot_quiet_blocks_probe(&self.quiet, &self.profile, now);
        if quiet_blocks {
            let extended = rr
                .deadline
                .max(self.quiet.boot_quiet_until.saturating_add(rr.timeout_ns));
            rr.deadline = extended.min(rr.cap);
        }
        if now >= rr.deadline {
            return self.finish_reboot_recovery_timeout(now);
        }
        if quiet_blocks || now < rr.next_attempt {
            return Vec::new();
        }
        rr.next_attempt = now.saturating_add(1_000_000_000);
        let Some(epoch) = self.generations.conn_epoch else {
            return Vec::new();
        };
        let auth = resolve_session_auth(
            &self.profile,
            self.deps.env.as_ref(),
            self.deps.read_env_file.as_ref(),
        );
        if crate::session_auth::credentials_declared_but_unresolved(&self.profile, &auth.1) {
            let reason = auth.1.reason;
            let env_file = auth.1.env_file_path.clone().unwrap_or_default();
            let mut effects = self.credentials_warning(reason, &env_file);
            self.set_last_error(Some("CREDENTIALS_UNRESOLVED".into()), None);
            effects.extend(self.finish_reboot_recovery_timeout(now));
            return effects;
        }
        self.begin_login_generation(now);
        self.login_run = Some(LoginRun {
            epoch,
            kind: LoginRunKind::RebootRecovery,
            fsm_gen: self.generations.fsm_gen,
            waiters: Vec::new(),
        });
        vec![Effect::StartLogin {
            epoch,
            fsm_gen: self.generations.fsm_gen,
            plan: LoginPlan::EnsureReady { auth: auth.0 },
        }]
    }

    fn reboot_login_finished(
        &mut self,
        epoch: u64,
        result: crate::login_fsm::LoginOutcome,
        now: Mono,
    ) -> Vec<Effect> {
        self.login_run = None;
        if self.generations.conn_epoch != Some(epoch) || self.state != SessionState::Recovering {
            return Vec::new();
        }
        // `_spawn_reboot_recovery`：TX 例外／其他例外 → RECOVERY_ERROR＋WARNING、續迴圈；quiet／新世代撤銷 → continue。
        if let Some((exc, _)) = login_exception(&result) {
            // Python `except TxIncompleteError`（含子類別 TxUnknownError／WalUnavailableError）與 `except Exception` 分開告警。
            let tx_exception = matches!(
                exc.as_str(),
                "TxIncompleteError" | "TxUnknownError" | "WalUnavailableError"
            );
            self.set_last_error(Some("RECOVERY_ERROR".into()), None);
            return vec![Effect::Warn(if tx_exception {
                format!(
                    "reboot-recovery ensure_ready TX 例外（session={}）",
                    self.sid
                )
            } else {
                format!(
                    "reboot-recovery ensure_ready 例外（session={}），續迴圈待逾時收尾",
                    self.sid
                )
            })];
        }
        match result {
            crate::login_fsm::LoginOutcome::Ready => {
                self.set_state(SessionState::Ready, now);
                self.confirm_ready();
                self.set_last_error(None, None);
                self.last_ready_at = Some(self.wall_iso());
                self.last_probe_at = Some(self.wall_iso());
                self.recovering = false;
                self.pending_auto_login = false;
                self.queue_registered = true;
                self.reboot_recovery = None;
                vec![Effect::Notify(ManagerNote::SessionReady {
                    sid: self.sid.clone(),
                    via_attach: false,
                })]
            }
            crate::login_fsm::LoginOutcome::NotReady(error)
            | crate::login_fsm::LoginOutcome::Failed(error) => {
                self.set_last_error(Some(error), None);
                Vec::new()
            }
            // quiet／新世代撤銷（`_login_tx_refused`）：continue，不記錯（例外已於上方收斂）。
            crate::login_fsm::LoginOutcome::Aborted(_) | crate::login_fsm::LoginOutcome::Raised { .. } => {
                Vec::new()
            }
        }
    }

    fn finish_reboot_recovery_timeout(&mut self, now: Mono) -> Vec<Effect> {
        self.reboot_recovery = None;
        self.recovering = false;
        self.pending_auto_login = false;
        if self.generations.conn_epoch.is_none() {
            self.set_state(SessionState::Detached, now);
            self.set_last_error(Some("RECOVERY_TIMEOUT".into()), None);
        } else {
            self.set_state(SessionState::Attached, now);
            if self.last_error.is_none() {
                self.set_last_error(Some("RECOVERY_TIMEOUT".into()), None);
            }
        }
        Vec::new()
    }

    // ---------------- self-test ----------------

    fn finish_self_test(&self, mut result: PyDict) -> PyDict {
        if result.get("command_capable").is_none() {
            result.insert(
                "command_capable",
                PyValue::from(self.profile.template.command_capable()),
            );
        }
        result
    }

    fn autoboot_quiet_self_test(&self, now: Mono) -> PyDict {
        let mut out = pydict! {
            "ok" => true,
            "classification" => "AUTOBOOT_QUIET",
            "session" => self.session_value(now),
            "recommended_action" => "wait",
        };
        Self::extend_pairs(&mut out, self.lease_ctx());
        out
    }

    fn probe_self_test_result(
        &self,
        ok: bool,
        probe: &PyDict,
        base: &SelfTestBase,
        lease_ctx: Vec<(&'static str, PyValue)>,
        now: Mono,
    ) -> PyDict {
        let attached = option_string(base.attached_real_path.as_deref());
        let current = option_string(base.current_real_path.as_deref());
        let mut out;
        if ok {
            out = pydict! {
                "ok" => true,
                "classification" => "OK",
                "session" => self.session_value(now),
                "attached_real_path" => attached,
                "current_real_path" => current,
                "attached_vtty" => option_string(base.attached_vtty.as_deref()),
                "bridge_generation" => py_int(base.bridge_generation as i128),
                "probe_ok" => true,
                "recommended_action" => "none",
            };
        } else {
            let flood = probe
                .get("rx_bytes_last_10s")
                .cloned()
                .unwrap_or(PyValue::Null);
            let flooded = matches!(&flood, PyValue::Int(PyInt::Small(n)) if *n >= RX_FLOOD_BYTES_PER_10S as i128);
            if flooded {
                out = pydict! {
                    "ok" => true,
                    "classification" => "RX_FLOOD",
                    "session" => self.session_value(now),
                    "attached_real_path" => attached,
                    "current_real_path" => current,
                    "probe_ok" => false,
                    "rx_bytes_last_10s" => flood,
                    "recommended_action" => "wait",
                };
            } else {
                out = pydict! {
                    "ok" => true,
                    "classification" => "TARGET_UNRESPONSIVE",
                    "session" => self.session_value(now),
                    "attached_real_path" => attached,
                    "current_real_path" => current,
                    "probe_ok" => false,
                    "recommended_action" => "recover",
                };
            }
        }
        Self::extend_pairs(&mut out, lease_ctx);
        out
    }

    fn self_test_request(
        &mut self,
        timeout_s: f64,
        strict_human_lock: bool,
        reply: Reply,
        now: Mono,
    ) -> Vec<Effect> {
        if self.state != SessionState::Released && !self.recovering && self.state != SessionState::Recovering {
            self.refresh_lease(now);
        }
        let holder = if self.state == SessionState::Released {
            match (&self.self_test_probes.holders, self.known_real_path.as_deref()) {
                (Some(probe), Some(path)) => Some(probe.probe_holders(path)),
                _ => None,
            }
        } else {
            None
        };
        let ctx = SelfTestCtx {
            device_present: self.device_present,
            holder,
            by_id_realpath: self.known_real_path.clone(),
            vtty_alive: self.vtty_alive(),
            bridge: self.generations.conn_epoch.and_then(|epoch| {
                self.self_test_probes
                    .bridge_snapshot
                    .as_ref()
                    .and_then(|snapshot| snapshot(epoch))
            }),
            strict_human_lock,
            now,
        };
        match self.self_test_pre_probe(&ctx) {
            Ok(result) => vec![Effect::Reply(reply, Ok(self.finish_self_test(result)))],
            Err(need) => {
                // Python `self_test` 不看前景：nonce probe 與在途命令並行（審查 F25）。
                let rng = self.deps.rng.clone();
                let sequence = self.next_op_id;
                let mut fill = |bytes: &mut [u8]| {
                    rng(bytes);
                    for (index, byte) in bytes.iter_mut().take(8).enumerate() {
                        *byte ^= ((sequence >> (index * 8)) & 0xff) as u8;
                    }
                };
                let nonce: String = uuid4_hex(&mut fill).chars().take(8).collect();
                let probe = self.profile.template.ready_probe.replace("${nonce}", &nonce);
                self.last_probe_at = Some(self.wall_iso());
                let probe_len = probe.len() + usize::from(!probe.ends_with('\n'));
                let grant = self.new_grant(None);
                self.pending_ops.insert(
                    grant.op_id,
                    PendingOp::SelfTest {
                        reply,
                        probe_len,
                        base: need.base,
                        lease_ctx: self.lease_ctx(),
                    },
                );
                self.side_ops.insert(grant.op_id, grant.clone());
                vec![Effect::StartOp {
                    grant,
                    spec: OpSpec::SelfTestProbe {
                        timeout_s,
                        probe,
                        nonce,
                    },
                }]
            }
        }
    }

    /// 05 §11 順序 0–15 的 probe 前分類（`classify_pre_probe` 的本體）。
    pub(crate) fn self_test_pre_probe(&self, ctx: &SelfTestCtx) -> Result<PyDict, NeedProbe> {
        let now = ctx.now;
        let session = self.session_value(now);
        if self.state == SessionState::Released {
            let (pids, unknown) = match &ctx.holder {
                Some(holder) => (holder.pids.clone(), holder.holder_unknown),
                None => (Vec::new(), false),
            };
            // BC-04：holder 不明不得當作安全證明。
            let reclaimable = pids.is_empty() && !unknown;
            let external = if pids.is_empty() {
                PyValue::from("none")
            } else {
                PyValue::List(pids.iter().map(|p| py_int(*p as i128)).collect())
            };
            let mut out = pydict! {
                "ok" => true,
                "classification" => "RELEASED",
                "session" => session,
                "released_by" => option_string(self.released_by.as_deref()),
                "released_at" => option_string(self.released_at.as_deref()),
                "reason" => self.released_reason.clone(),
                "external_holder" => external,
                "reclaimable" => reclaimable,
                "recommended_action" => if reclaimable { "device_attach" } else { "wait_external_flash" },
            };
            Self::extend_pairs(&mut out, Self::empty_lease_ctx());
            return Ok(out);
        }
        let lease_ctx = self.lease_ctx();
        let simple = |classification: &str, action: &str, extra: Vec<(&'static str, PyValue)>| {
            let mut out = pydict! {
                "ok" => true,
                "classification" => classification,
                "session" => session.clone(),
            };
            Self::extend_pairs(&mut out, extra);
            out.insert("recommended_action", PyValue::from(action));
            Self::extend_pairs(&mut out, lease_ctx.clone());
            out
        };
        if self.recovering || self.state == SessionState::Recovering {
            return Ok(simple("SESSION_RECOVERING", "wait", Vec::new()));
        }
        if ctx.strict_human_lock {
            if let Some(lease) = self.lease.as_ref().filter(|l| l.is_human()) {
                let mut out = pydict! {
                    "ok" => true,
                    "classification" => "HUMAN_INTERACTIVE_ACTIVE",
                    "interactive_id" => lease.interactive_id.clone(),
                    "session" => session.clone(),
                    "recommended_action" => "wait_or_detach_console",
                };
                Self::extend_pairs(&mut out, lease_ctx.clone());
                return Ok(out);
            }
        }
        if !ctx.device_present {
            return Ok(simple("DEVICE_MISSING", "check_cable_or_bind", Vec::new()));
        }
        let current = option_string(ctx.by_id_realpath.as_deref());
        if let Some(attached) = self.attached_real_path.as_deref() {
            if ctx.by_id_realpath.as_deref() != Some(attached) {
                return Ok(simple(
                    "DEVICE_REBOUND_REQUIRED",
                    "reattach",
                    vec![
                        ("attached_real_path", PyValue::from(attached)),
                        ("current_real_path", current),
                    ],
                ));
            }
        }
        if self.generations.conn_epoch.is_none() {
            return Ok(simple(
                "BRIDGE_DOWN",
                "attach",
                vec![("current_real_path", current)],
            ));
        }
        // Python：bridge 物件仍在但 `snapshot()` 的 running／serial_alive 為假 → BRIDGE_DOWN／recover，零 probe。
        if ctx
            .bridge
            .is_some_and(|snapshot| !snapshot.running || !snapshot.serial_alive)
        {
            return Ok(simple(
                "BRIDGE_DOWN",
                "recover",
                vec![("current_real_path", current)],
            ));
        }
        let attached_vtty = option_string(self.current_vtty());
        if ctx.vtty_alive == Some(false) {
            return Ok(simple(
                "VTTY_STALE",
                "console_attach",
                vec![("attached_vtty", attached_vtty)],
            ));
        }
        let attached = option_string(self.attached_real_path.as_deref());
        let bridge_generation = py_int(self.generations.bridge_generation as i128);
        let bridge_keys = || {
            vec![
                ("attached_real_path", attached.clone()),
                ("current_real_path", current.clone()),
                ("attached_vtty", attached_vtty.clone()),
                ("bridge_generation", bridge_generation.clone()),
            ]
        };
        if self.is_passthrough() && self.state == SessionState::Ready {
            return Ok(simple("PASSTHROUGH", "none", bridge_keys()));
        }
        if self.state == SessionState::Attached {
            let mut extra: Vec<(&'static str, PyValue)> = Vec::new();
            let (classification, action) = if self.is_passthrough() {
                let elapsed = self
                    .quiet_since
                    .map(|t| now.saturating_sub(t) as f64 / 1_000_000_000.0)
                    .unwrap_or(0.0);
                extra.push((
                    "passthrough_ready_in_s",
                    PyValue::from(py_round((PASSTHROUGH_READY_QUIET_S - elapsed).max(0.0), 1)),
                ));
                // #181（Python `_suggest_profile_from_rx`）：fallback 為未經量測的 catch-all——RX 命中某個非
                // passthrough template 的 prompt 時給出可執行的出口（審查 F31）。
                match (self.profile_source == "fallback")
                    .then(|| self.suggest_profile_from_rx())
                    .flatten()
                {
                    Some(suggested) => {
                        let com = self.profile.com.clone();
                        extra.push(("suggested_profile", PyValue::from(suggested.clone())));
                        extra.push((
                            "hint",
                            PyValue::from(format!(
                                "此 session 為未經量測的 fallback 分類，RX 內容匹配 {suggested}。套用：serialwrap session pin --selector {com} --profile {suggested} 之後 serialwrap session clear --selector {com}。"
                            )),
                        ));
                        ("PASSTHROUGH", "pin_profile")
                    }
                    None => ("PASSTHROUGH", "console_attach"),
                }
            } else if self.last_error.as_deref() == Some("LOGIN_REQUIRED") {
                ("LOGIN_REQUIRED", "console_attach")
            } else if self.last_error.as_deref() == Some("REBOOTING") {
                ("REBOOTING", "wait_or_console_attach")
            } else {
                let tail = clean_text(&self.deps.rx.tail(BOOTLOADER_RX_TAIL_CHARS));
                let mut matched = matches_any_bootloader_prompt(&tail, &self.bootloader_patterns());
                if matched.is_none() && self.last_error.as_deref() == Some("BOOTLOADER_STUCK") {
                    let detail = self
                        .last_error_detail
                        .as_deref()
                        .unwrap_or("")
                        .strip_prefix("matched_prompt=")
                        .map(str::to_string)
                        .unwrap_or_else(|| self.last_error_detail.clone().unwrap_or_default());
                    matched = Some(if detail.is_empty() {
                        "BOOTLOADER_STUCK".to_string()
                    } else {
                        detail
                    });
                }
                if let Some(matched) = matched {
                    extra.push(("matched_prompt", PyValue::from(matched)));
                    extra.push(("rx_tail", PyValue::from(tail)));
                    ("BOOTLOADER", "recover_interactive")
                } else {
                    let (flood, _) = self.deps.rx.rx_stats(now);
                    if self.last_error.as_deref() == Some("RX_FLOOD")
                        || flood >= RX_FLOOD_BYTES_PER_10S
                    {
                        extra.push(("rx_bytes_last_10s", py_int(flood as i128)));
                        ("RX_FLOOD", "wait")
                    } else {
                        ("ATTACHED_NOT_READY", "console_attach")
                    }
                }
            };
            let mut out = pydict! {
                "ok" => true,
                "classification" => classification,
                "session" => session.clone(),
            };
            Self::extend_pairs(&mut out, bridge_keys());
            out.insert("recommended_action", PyValue::from(action));
            Self::extend_pairs(&mut out, extra);
            Self::extend_pairs(&mut out, lease_ctx.clone());
            return Ok(out);
        }
        if self.quiet.quiet_active(now) {
            return Ok(simple("AUTOBOOT_QUIET", "wait", Vec::new()));
        }
        Err(NeedProbe {
            probe: String::new(),
            nonce: String::new(),
            timeout_s: 0.0,
            partial: PyDict::new(),
            base: SelfTestBase {
                attached_real_path: self.attached_real_path.clone(),
                current_real_path: ctx.by_id_realpath.clone(),
                bridge_generation: self.generations.bridge_generation,
                attached_vtty: self.current_vtty().map(str::to_string),
            },
        })
    }

    fn empty_lease_ctx() -> Vec<(&'static str, PyValue)> {
        vec![
            ("interactive_owner", PyValue::Null),
            ("human_attached", PyValue::Bool(false)),
            ("human_active", PyValue::Bool(false)),
            ("recovery_mode", PyValue::Bool(false)),
        ]
    }

    fn next_uuid(&mut self) -> String {
        self.next_uuid = self.next_uuid.saturating_add(1);
        let sequence = self.next_uuid;
        let rng = self.deps.rng.clone();
        let mut fill = |bytes: &mut [u8]| {
            rng(bytes);
            for (index, byte) in bytes.iter_mut().take(6).enumerate() {
                *byte ^= ((sequence >> (index * 8)) & 0xff) as u8;
            }
        };
        uuid4_hex(&mut fill)
    }

    fn idle_ms(&self, now: Mono) -> Option<u64> {
        let last = self.last_rx_mono.max(self.last_tx_mono);
        (last != 0).then(|| now.saturating_sub(last) / 1_000_000)
    }

    fn activity_classification(&self, now: Mono) -> &'static str {
        if !matches!(self.state, SessionState::Ready | SessionState::Attached) {
            return "offline";
        }
        let Some(idle) = self.idle_ms(now) else {
            return "newly-attached";
        };
        if idle < 5_000 {
            "active"
        } else if idle < 60_000 {
            "idle-healthy"
        } else {
            "quiet-suspicious"
        }
    }
}

/// 前景 op 的待辦（`pending_ops`；`OpFinished` 時依種類結算）。
enum PendingOp {
    Command {
        cmd_id: String,
        mode: String,
        reboot: bool,
    },
    /// `mode == "interactive"` 的命令：lease 已開，命令 TX 完成後以 `result` 終結紀錄。
    InteractiveCmd {
        cmd_id: String,
        result: PyDict,
    },
    Recover {
        reply: Reply,
    },
    SelfTest {
        reply: Reply,
        probe_len: usize,
        base: SelfTestBase,
        lease_ctx: Vec<(&'static str, PyValue)>,
    },
    InteractiveSend {
        reply: Reply,
        interactive_id: String,
    },
    InteractiveOpen {
        reply: Reply,
        result: PyDict,
    },
}

/// reboot 後自動恢復的期限（`_spawn_reboot_recovery`：`deadline_cap = start + 180 + timeout`）。
struct RebootRecovery {
    deadline: Mono,
    cap: Mono,
    timeout_ns: u64,
    next_attempt: Mono,
}

/// force recover 的等待者（`_force_recover`：clear 後最多 10 次 × 1 s 等 READY）。
struct ForceRecover {
    reply: Reply,
    deadline: Mono,
    kind: ForceKind,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum ForceKind {
    /// `recover_session` 直接呼叫 `_force_recover`。
    Direct,
    /// `_recover_after_failure` 救援鍵失敗後的 auto-fallback（`PROMPT_TIMEOUT_FORCE_RECOVERED`）。
    AfterPromptTimeout,
}

/// `MAX_RECOVERY_LEASE_S`。
const MAX_RECOVERY_LEASE_S: f64 = 120.0;
/// `BOOTLOADER_RX_TAIL_BYTES`（`rx_tail` 以字元計）。
const BOOTLOADER_RX_TAIL_CHARS: usize = 512;
/// `RX_FLOOD_BYTES_PER_10S`。
const RX_FLOOD_BYTES_PER_10S: u64 = 20_000;
/// `UBOOT_FALLBACK_PROMPTS`。
const UBOOT_FALLBACK_PROMPTS: [&str; 3] = ["^=> $", "^U-Boot> $", "^CFE> $"];
/// `_force_recover` 的等待上限（10 × 1 s）。
const FORCE_RECOVER_WAIT_NS: Mono = 10_000_000_000;
/// `BOOT_QUIET_WINDOW_S`。
const BOOT_QUIET_WINDOW_NS: Mono = 180_000_000_000;

fn normalized_mode(mode: &str) -> &str {
    match mode {
        "fg" => "line",
        "bg" => "background",
        other => other,
    }
}

fn prompt_timeout_none() -> PyDict {
    pydict! {
        "ok" => false,
        "error_code" => "PROMPT_TIMEOUT",
        "partial" => true,
        "recovery_action" => "NONE",
    }
}

fn secs_ns(value: f64) -> u64 {
    (value.max(0.0) * 1_000_000_000.0) as u64
}

/// `interactive_send` 提交點拒絕碼（#36；`session_manager.py:7224-7233`）；`None` 表示例外穿出。
fn lease_refused_send_code(reject: FenceReject) -> Option<&'static str> {
    match reject {
        FenceReject::OwnerMismatch => Some("INTERACTIVE_EXPIRED"),
        FenceReject::Quiet | FenceReject::ReconfirmPending => Some("AUTOBOOT_QUIET"),
        FenceReject::Frozen
        | FenceReject::Cancelled
        | FenceReject::StaleEpoch
        | FenceReject::Flash => Some("SESSION_BUSY"),
        _ => None,
    }
}

/// interactive 模式命令的 TX 被拒：`execute_command` 的 `_map_aborted_result` 映射。
fn lease_refused_command_code(reject: FenceReject) -> &'static str {
    match reject {
        FenceReject::Quiet | FenceReject::ReconfirmPending => "AUTOBOOT_QUIET",
        FenceReject::Cancelled | FenceReject::StaleEpoch | FenceReject::Frozen => {
            "FLUSHED_BY_RECOVERY"
        }
        _ => "SEND_FAILED",
    }
}

/// Python `text[-n:]`（n == 0 → 全部；n < 0 → 去掉前 |n| 字元）。
fn py_tail(text: &str, n: i128) -> String {
    let count = text.chars().count() as i128;
    let skip = if n > 0 {
        (count - n).max(0)
    } else if n < 0 {
        (-n).min(count)
    } else {
        0
    };
    text.chars().skip(skip as usize).collect()
}

/// Python `str(OSError)`：`[Errno N] strerror: 'path'`。
fn os_error_text(error: &std::io::Error, path: &str) -> String {
    let message = error.to_string();
    let strerror = message
        .split(" (os error")
        .next()
        .unwrap_or(message.as_str())
        .to_string();
    match error.raw_os_error() {
        Some(code) => format!("[Errno {code}] {strerror}: '{path}'"),
        None => format!("{strerror}: '{path}'"),
    }
}

fn option_string(value: Option<&str>) -> PyValue {
    value
        .map(|s| PyValue::from(s.to_string()))
        .unwrap_or(PyValue::Null)
}

fn py_int(value: i128) -> PyValue {
    PyValue::Int(PyInt::Small(value))
}

/// Python `_bg_fallback_from_arbiter`：capture 尚未建立（或已不在任何 session）時以命令紀錄合成 result_tail 回應；
/// 非 background、done、已開始的 canceled 回 CMD_NOT_FOUND（審查 F76：service 於全域 capture 查無後才呼叫）。
pub fn bg_fallback_from_registry(registry: &CommandRegistry, cmd_id: &str, from_chunk: i128) -> PyDict {
    let not_found = pydict! { "ok" => false, "error_code" => "CMD_NOT_FOUND", "cmd_id" => cmd_id };
    let Some(record) = registry.get(cmd_id) else {
        return not_found;
    };
    if record.get("execution_mode").and_then(PyValue::as_str) != Some("background") {
        return not_found;
    }
    let status = record
        .get("status")
        .cloned()
        .unwrap_or(PyValue::Null);
    if status.as_str() == Some("done") {
        return not_found;
    }
    if status.as_str() == Some("canceled")
        && record.get("started_at").is_some_and(|v| !v.is_null())
    {
        return not_found;
    }
    pydict! {
        "ok" => true,
        "cmd_id" => cmd_id,
        "status" => status,
        "error_code" => record.get("error_code").cloned().unwrap_or(PyValue::Null),
        "from_seq" => py_int(0),
        "last_seq" => py_int(0),
        "from_chunk" => py_int(from_chunk),
        "next_chunk" => py_int(from_chunk),
        "chunks" => PyValue::List(Vec::new()),
    }
}

fn age_s(last: Mono, now: Mono) -> PyValue {
    if last == 0 {
        PyValue::Null
    } else {
        PyValue::from(py_round(
            now.saturating_sub(last) as f64 / 1_000_000_000.0,
            1,
        ))
    }
}

fn pyint_to_u32(value: &PyInt, default: u32) -> u32 {
    match value {
        PyInt::Small(n) => u32::try_from(*n).unwrap_or(default),
        PyInt::Big(n) => n.parse().unwrap_or(default),
    }
}

fn pyint_to_u8(value: &PyInt, default: u8) -> u8 {
    match value {
        PyInt::Small(n) => u8::try_from(*n).unwrap_or(default),
        PyInt::Big(n) => n.parse().unwrap_or(default),
    }
}

fn open_failure_last_error(failure: &OpenFailure) -> String {
    let class = match failure {
        OpenFailure::Held(_) => "DeviceHeldError",
        OpenFailure::AccessDenied(_) => "PermissionError",
        OpenFailure::NotFound => "FileNotFoundError",
        OpenFailure::Other(error) => error.py_exc_name(),
    };
    format!("ATTACH_FAILED:{class}")
}

fn append_receipt_warnings(receipt: &WorkerStoppedReceipt, effects: &mut Vec<Effect>, com: &str) {
    for gap in &receipt.unrecorded {
        effects.push(Effect::Warn(format!(
            "WARNING session {com} drain 有未落盤證據（dir={}, len={}, op_id={:?}, error={:?}）",
            gap.dir, gap.len, gap.op_id, gap.error
        )));
    }
    if let Some(error) = receipt.wal_error.as_ref() {
        effects.push(Effect::Warn(format!(
            "WARNING session {com} drain WAL 不健康：{error:?}"
        )));
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

}

/// login run 結果中「Python 會以例外穿出」者的（例外型別名, `str(exc)`）（`_login_tx_refused` 只認 quiet／stale_login_gen）。
/// login FSM 自 merge P5 F16 起直接回 `Raised { exc, message }`（`classify_login_tx_failure`）；下列 `Aborted`
/// 變體 login FSM 已不產生，保留為防禦性收斂（訊息無 TxResult 可取，以型別名代之）：部分寫出 → `TxIncompleteError`；
/// WAL 不健康 → `WalUnavailableError`；draining／revoked op → `TxIncompleteError`。quiet／新世代撤銷與 `Cancelled`
/// 回 `None`（撤銷語意）。
fn login_exception(result: &crate::login_fsm::LoginOutcome) -> Option<(String, String)> {
    use crate::login_fsm::{LoginAbort, LoginOutcome};
    let synthetic = |exc: &str| Some((exc.to_string(), exc.to_string()));
    match result {
        LoginOutcome::Raised { exc, message } => Some((exc.clone(), message.clone())),
        LoginOutcome::Aborted(LoginAbort::TxIncomplete) => synthetic("TxIncompleteError"),
        LoginOutcome::Aborted(LoginAbort::Refused(FenceReject::WalUnhealthy)) => {
            synthetic("WalUnavailableError")
        }
        LoginOutcome::Aborted(LoginAbort::Refused(
            FenceReject::Frozen | FenceReject::Cancelled,
        )) => synthetic("TxIncompleteError"),
        _ => None,
    }
}

/// P7 Task 3 過渡：Task 7 的 login 收斂表（`login_run.rs`）落地前，attach 路徑的新 variant 依 Python 收斂——
/// 例外（`login_exception`）一律以 `Raised { exc, message }` 交給 attach 的 `Raised` 分支（`_attach_by_id` 外層
/// except：關 bridge、DETACHED、`ATTACH_FAILED:<型別>`，`session_manager.py:4873-4909`）；
/// 其餘 `Aborted`（quiet／新世代撤銷）同 06 `refused()`（`NotReady("PROMPT_UNAVAILABLE")`）。
/// 只供 attach（與既有 bridge 再探的非例外結果）使用；reboot recovery／自動重探各自收斂。
fn legacy_login_outcome(result: crate::login_fsm::LoginOutcome) -> crate::login_fsm::LoginOutcome {
    use crate::login_fsm::LoginOutcome;
    if let Some((exc, message)) = login_exception(&result) {
        return LoginOutcome::Raised { exc, message };
    }
    match result {
        LoginOutcome::Aborted(_) => LoginOutcome::NotReady("PROMPT_UNAVAILABLE".into()),
        other => other,
    }
}
