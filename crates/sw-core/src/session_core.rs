//! P5-T11：sans-IO `SessionCore` 狀態機。
//!
//! 型別與公開簽章依 `docs/rust-refactor/plans/06-slice21-daemon-core.md` Task 11「Produces」與 Task 0 骨架凍結；
//! 事件處理只更新 session 狀態並產生效果，I/O 由 actor executor 執行。

use std::collections::VecDeque;
use std::sync::Arc;

use sw_pal::traits::{DevicePath, PalError, SerialConfig, SysEnv};
use sw_proto::config_types::SessionProfile;
use sw_proto::constants::PASSTHROUGH_READY_QUIET_S;
use sw_proto::pycompat::{PyErr, clean_text, py_round, uuid4_hex};
use sw_proto::pydict;
use sw_proto::pyjson::{PyDict, PyInt, PyValue};

use crate::Mono;
use crate::arbiter::{CmdOutcome, CommandRegistry, RunDecision, SessionQueue, SubmitReq};
use crate::drain::{StopId, StopKind, StopOp, WorkerStoppedReceipt};
use crate::generations::{EpochAllocator, Generations};
use crate::login_fsm::{LoginPlan, choose_login_plan, login_error_detail};
use crate::mailbox::{
    ControlEvent, DataEvent, DetachReason, MailboxItem, OpenFailure, OpenedBridge, Reply,
    TimerEvent,
};
use crate::op_runner::{OpOutcome, OpSpec};
use crate::quiet_gate::{
    QuietState, QuietTransition, is_reboot_command, slice21_unsupported_feature,
};
use crate::session_auth::resolve_session_auth;
use crate::state_store::{ReleasedRow, StateDelta};
use crate::tx_writer::{Fence, FlashMode, OpId, TX_LEDGER_LIVE_MAX};
use crate::uart_bridge::RxShared;
use crate::wal::WalWriter;

/// daemon stderr／state.json 相關通知（Task 0 骨架型別）。
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum ManagerNote {
    SessionReady {
        sid: String,
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
    CancelQueued {
        cmd_id: String,
    },
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
    open_generation: u64,
    known_real_path: Option<String>,
    device_present: bool,
    console_count: usize,
    queue: SessionQueue,
    foreground: Option<OpGrant>,
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
    released_reason: Option<String>,
    interactive_session_id: Option<String>,
    interactive_owner: Option<String>,
    interactive_deadline: Option<Mono>,
    pending_release: Option<PendingRelease>,
    pending_auto_login: bool,
    shutdown_requested: bool,
    shutdown_complete: bool,
    #[cfg(feature = "test-fakes")]
    panic_events_remaining: u8,
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
    /// `SessionAttach` 再探的等待者（attach 路徑無）。
    waiters: Vec<Reply>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum LoginRunKind {
    /// bridge 開啟後的 attach 登入（`_attach_by_id`）。
    Attach,
    /// 既有 bridge 再探（`_probe_existing_bridge`）。
    Reprobe,
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
            open_generation: 0,
            known_real_path: None,
            device_present: false,
            console_count: 0,
            queue: SessionQueue::new(),
            foreground: None,
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
            released_reason: None,
            interactive_session_id: None,
            interactive_owner: None,
            interactive_deadline: None,
            pending_release: None,
            pending_auto_login: false,
            shutdown_requested: false,
            shutdown_complete: false,
            #[cfg(feature = "test-fakes")]
            panic_events_remaining: 0,
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
                wal_seq: _,
                bytes,
            }) => {
                effects.0.extend(self.rx_chunk(epoch, &bytes, now));
            }
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
        let outstanding = usize::from(self.foreground.is_some());
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
            option_string(self.interactive_session_id.as_deref()),
        );
        out.insert("foreground_busy", PyValue::from(self.foreground.is_some()));
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
        out.insert("reprobe_attempts", py_int(0));
        out.insert("next_reprobe_at", PyValue::Null);
        out.insert("reprobe_exhausted", PyValue::from(false));
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
            option_string(self.released_reason.as_deref()),
        );
        out.insert("capture", PyValue::Null);
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

    /// 只供測試（test-fakes）：在 attach 前建立仍有效的 boot-quiet deadline。
    #[cfg(feature = "test-fakes")]
    pub fn set_boot_quiet_until_for_test(&mut self, deadline: Mono) {
        self.quiet.boot_quiet_until = deadline;
    }
}

impl SessionCore {
    /// Two-Phase Drain 第一階段的發起點（Task 12；SC-04、arch §5.5）：`kind` 與 `waiter` 凍結簽章（P6 07 計畫依此呼叫）。
    pub fn begin_stop(&mut self, kind: StopKind, waiter: Option<Reply>, now: Mono) -> Effects {
        let mut effects = Effects(Vec::new());
        // 再探進行中被 drain 搶進：等待者照 Python `attach_session` 的 pending drain 分支回 SESSION_RECOVERING。
        if let Some(run) = self.login_run.take() {
            for waiter in run.waiters {
                effects.push(Effect::Reply(waiter, Ok(self.session_recovering_reply(now))));
            }
        }
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
        effects.push(Effect::BeginDrain { stop_id, epoch });
        effects.push(Effect::Schedule {
            at: now.saturating_add(crate::resources::DRAIN_TIMEOUT_MS.saturating_mul(1_000_000)),
            ev: TimerEvent::DrainTimeout { epoch, stop_id },
        });
        effects
    }

    fn finish_stop_without_worker(
        &mut self,
        kind: StopKind,
        waiters: Vec<Reply>,
        now: Mono,
    ) -> Vec<Effect> {
        let mut effects = Vec::new();
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
            self.foreground_reboot = false;
            self.login_run = None;
            self.clear_interactive();
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
                            self.released_reason = None;
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
                    self.released_reason = None;
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
                    self.released_reason = None;
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
        }
        self.last_error = error;
        if detail.is_some() {
            self.last_error_detail = detail;
        }
    }

    fn ok_session(&self, now: Mono) -> PyDict {
        pydict! {
            "ok" => true,
            "session" => self.public(now, &PublicCtx { console_count: 0, vtty: None, console_endpoint: None })
        }
    }

    fn error_session(&self, code: &str, now: Mono) -> PyDict {
        pydict! {
            "ok" => false,
            "error_code" => code,
            "session" => self.public(now, &PublicCtx { console_count: 0, vtty: None, console_endpoint: None })
        }
    }

    fn session_recovering_reply(&self, now: Mono) -> PyDict {
        pydict! {
            "ok" => false,
            "error_code" => "SESSION_RECOVERING",
            "drain_stalled" => self.drain_stalled,
            "drain_timeout_epoch" => self.drain_timeout_epoch.map(|epoch| py_int(epoch as i128)).unwrap_or(PyValue::Null),
            "session" => self.public(now, &PublicCtx { console_count: 0, vtty: None, console_endpoint: None })
        }
    }

    fn drain_stalled_reply(&self, now: Mono) -> PyDict {
        pydict! {
            "ok" => false,
            "error_code" => "DRAIN_STALLED",
            "drain_stalled" => true,
            "drain_timeout_epoch" => self.drain_timeout_epoch.map(|epoch| py_int(epoch as i128)).unwrap_or(PyValue::Null),
            "session" => self.public(now, &PublicCtx { console_count: 0, vtty: None, console_endpoint: None })
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
            "session" => self.public(now, &PublicCtx { console_count: 0, vtty: None, console_endpoint: None }),
            "closed_consoles" => py_int(closed_consoles),
            "aborted_cmd" => aborted_cmd
        }
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
                    aborted_cmd: self.foreground.is_some(),
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
                if self.interactive_session_id.as_deref() == Some(interactive_id.as_str()) {
                    self.clear_interactive();
                    vec![Effect::Reply(reply, Ok(self.ok_session(now)))]
                } else {
                    vec![Effect::Reply(
                        reply,
                        Ok(self.error_session("INTERACTIVE_NOT_FOUND", now)),
                    )]
                }
            }
            SessionRequest::CancelQueued { cmd_id } => {
                self.queue.remove(&cmd_id);
                vec![Effect::Reply(reply, Ok(pydict! { "ok" => true }))]
            }
            SessionRequest::Clear => self.begin_stop(StopKind::Clear, Some(reply), now).0,
            SessionRequest::Recover { .. } => {
                self.credentials_warned = false;
                if self.state == SessionState::Recovering {
                    vec![Effect::Reply(reply, Ok(self.session_recovering_reply(now)))]
                } else {
                    vec![Effect::Reply(
                        reply,
                        Ok(self.error_session("SESSION_NOT_READY", now)),
                    )]
                }
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
            SessionRequest::Bind { .. } => s21_todo!("T15", "bind request"),
            SessionRequest::InteractiveSend { .. } => {
                s21_todo!("T14", "interactive send request")
            }
            SessionRequest::InteractiveStatus { .. } => {
                s21_todo!("T14", "interactive status request")
            }
            SessionRequest::SelfTest { .. } => s21_todo!("T15", "self-test request"),
            SessionRequest::LogStart | SessionRequest::LogStop | SessionRequest::LogStatus => {
                s21_todo!("T15", "log capture request")
            }
            SessionRequest::ResultTail { .. } => s21_todo!("T14", "result-tail request"),
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
                        &PublicCtx {
                            console_count: 0,
                            vtty: None,
                            console_endpoint: None,
                        },
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
                        &PublicCtx {
                            console_count: 0,
                            vtty: None,
                            console_endpoint: None,
                        },
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
            self.expire_reconfirm(now);
            if self.quiet.failed {
                let mut result = pydict! {
                    "ok" => false,
                    "error_code" => "READY_UNCONFIRMED",
                    "hint" => "READY 已逾 300s／5 次未能經 nonce probe 再確認（可能卡在 bootloader）；請 session self-test 取分類後 interactive-open 處理",
                    "recommended_action" => "self_test",
                };
                result.insert(
                    "session",
                    PyValue::Dict(self.public(
                        now,
                        &PublicCtx {
                            console_count: 0,
                            vtty: None,
                            console_endpoint: None,
                        },
                    )),
                );
                return vec![Effect::Reply(reply, Ok(result))];
            }
            if let Some(remaining) = self.quiet.remaining_s(now) {
                let mut result = pydict! {
                    "ok" => false,
                    "error_code" => "AUTOBOOT_QUIET",
                    "retry_after_s" => PyValue::from(remaining),
                    "hint" => "boot quiet window 進行中（疑似板卡自發重開機）；等 session 重新確認 READY 後重送",
                };
                result.insert(
                    "session",
                    PyValue::Dict(self.public(
                        now,
                        &PublicCtx {
                            console_count: 0,
                            vtty: None,
                            console_endpoint: None,
                        },
                    )),
                );
                return vec![Effect::Reply(reply, Ok(result))];
            }
            if self.quiet.pending {
                let mut result = pydict! {
                    "ok" => false,
                    "error_code" => "AUTOBOOT_QUIET",
                    "retry_after_s" => 5.0,
                    "hint" => "boot quiet window 已結束但 READY 尚未重新確認；等 daemon 重新確認 READY 後重送",
                };
                result.insert(
                    "session",
                    PyValue::Dict(self.public(
                        now,
                        &PublicCtx {
                            console_count: 0,
                            vtty: None,
                            console_endpoint: None,
                        },
                    )),
                );
                return vec![Effect::Reply(reply, Ok(result))];
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
                                &PublicCtx {
                                    console_count: 0,
                                    vtty: None,
                                    console_endpoint: None,
                                },
                            )),
                        );
                    }
                    return vec![Effect::Reply(reply, Ok(result))];
                }
            };
        self.queue.push(admitted.queued, &self.deps.registry);
        let mut effects = vec![Effect::Reply(reply, Ok(admitted.response))];
        effects.extend(self.start_next());
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
                    "session" => self.public(now, &PublicCtx { console_count: 0, vtty: None, console_endpoint: None })
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
                let human_lease = self
                    .interactive_owner
                    .as_deref()
                    .is_some_and(|owner| owner.starts_with("human:"));
                if !human_lease && self.state == SessionState::Attached {
                    return self.reprobe_existing_bridge(reply, now);
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
        if self.state != SessionState::Detached {
            return Vec::new();
        }
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
        } = opened;
        self.generations.conn_epoch = Some(epoch);
        self.attached_real_path = Some(real_path.clone());
        self.known_real_path = Some(real_path.clone());
        self.deps.fence.with(|f| {
            f.active_epoch = Some(epoch);
            f.fsm_gen = self.generations.fsm_gen;
            f.owner_gen = self.generations.owner_gen;
            f.frozen = false;
        });
        let mut effects = vec![Effect::AdoptBridge {
            epoch,
            worker,
            real_path,
        }];

        if self.is_passthrough() || !self.profile.template.command_capable() {
            self.set_last_error(None, None);
            self.adopt_bridge(now);
            self.set_state(SessionState::Attached, now);
            return effects;
        }
        let quiet_blocks = self.quiet.quiet_active(now);
        let (plan, synthesized) = self.choose_plan(quiet_blocks);
        match plan {
            LoginPlan::None => {
                if let Some(error) = synthesized {
                    // boot quiet（#130）：掛上 bridge 純收 RX，不送 probe。
                    self.set_last_error(Some(error.to_string()), None);
                    self.adopt_bridge(now);
                    self.set_state(SessionState::Attached, now);
                    return effects;
                }
                // Slice 2.1 能力閘：停 ATTACHED、不自動送 TX；StartLogin{None} 只作稽核點，executor 不執行任何登入。
                self.set_last_error(None, None);
                self.adopt_bridge(now);
                self.set_state(SessionState::Attached, now);
                effects.extend(self.unsupported_warning());
                effects.push(Effect::StartLogin {
                    epoch,
                    fsm_gen: self.generations.fsm_gen,
                    plan: LoginPlan::None,
                });
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
    fn choose_plan(&self, quiet_blocks: bool) -> (LoginPlan, Option<&'static str>) {
        let auth = resolve_session_auth(
            &self.profile,
            self.deps.env.as_ref(),
            self.deps.read_env_file.as_ref(),
        );
        choose_login_plan(&self.profile, self.pending_auto_login, auth, quiet_blocks)
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

    /// Slice 2.1 不支援的 profile 能力：daemon stderr 告警（每次自動登入判定一次）。
    fn unsupported_warning(&self) -> Vec<Effect> {
        slice21_unsupported_feature(&self.profile)
            .map(|feature| {
                Effect::Warn(format!(
                    "slice-2.1 不支援的 profile 能力（{}: {feature}）；不自動登入",
                    self.profile.template.profile_name
                ))
            })
            .into_iter()
            .collect()
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
        let reprobe = self
            .login_run
            .as_ref()
            .is_some_and(|run| run.epoch == epoch && run.kind == LoginRunKind::Reprobe);
        if self.generations.conn_epoch != Some(epoch) {
            return Vec::new();
        }
        if reprobe {
            // 既有 bridge 再探（attach_session ATTACHED 分支）：結果一律回填，quiet 撤銷造成的拒絕亦然（Python 同步 probe）。
            if self.state != SessionState::Attached {
                return Vec::new();
            }
        } else if self.state != SessionState::Attaching || self.generations.fsm_gen != fsm_gen {
            return Vec::new();
        }
        let run = self.login_run.take();
        let waiters = run.map(|run| run.waiters).unwrap_or_default();
        let mut effects = Vec::new();
        match result {
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
                self.confirm_ready();
                effects.push(Effect::Notify(ManagerNote::SessionReady {
                    sid: self.sid.clone(),
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
        }
        for waiter in waiters {
            effects.push(Effect::Reply(waiter, Ok(self.attach_state_result(now))));
        }
        effects
    }

    /// `_attach_state_result`（#94）：command-capable 卡在非 READY／ATTACHING 時回 ok:false＋狀態專用碼。
    fn attach_state_result(&self, now: Mono) -> PyDict {
        let session = PyValue::Dict(self.public(
            now,
            &PublicCtx {
                console_count: 0,
                vtty: None,
                console_endpoint: None,
            },
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
    fn reprobe_existing_bridge(&mut self, reply: Reply, now: Mono) -> Vec<Effect> {
        let Some(epoch) = self.generations.conn_epoch else {
            return vec![Effect::Reply(reply, Ok(self.attach_state_result(now)))];
        };
        if let Some(run) = self.login_run.as_mut() {
            if run.kind == LoginRunKind::Reprobe && run.epoch == epoch {
                run.waiters.push(reply);
                return Vec::new();
            }
        }
        if self.is_passthrough() || !self.profile.template.command_capable() {
            return vec![Effect::Reply(reply, Ok(self.attach_state_result(now)))];
        }
        let quiet_blocks = self.quiet.quiet_active(now);
        if quiet_blocks {
            // #130 Finding 2／#162：不送 probe TX；READY 不以合成碼覆寫 last_error。
            if self.state != SessionState::Ready {
                self.set_last_error(Some("PROMPT_UNAVAILABLE".into()), None);
            }
            return vec![Effect::Reply(reply, Ok(self.attach_state_result(now)))];
        }
        // `_begin_login_run_locked`：新登入世代，舊 run 的 TX 由 fence FsmMismatch 撤銷。
        self.generations.fsm_gen = self.generations.fsm_gen.saturating_add(1);
        self.deps.fence.with(|f| {
            f.fsm_gen = self.generations.fsm_gen;
            f.quiet_active = false;
        });
        let (plan, _) = self.choose_plan(false);
        match plan {
            LoginPlan::CredentialsUnresolved { reason, env_file } => {
                let mut effects = self.credentials_warning(reason, &env_file);
                self.set_last_error(Some("CREDENTIALS_UNRESOLVED".into()), None);
                self.pending_auto_login = false;
                self.set_state(SessionState::Attached, now);
                effects.push(Effect::Reply(reply, Ok(self.attach_state_result(now))));
                effects
            }
            LoginPlan::None => {
                // Slice 2.1 能力閘：不自動送 TX，誠實回報現況。
                let mut effects = self.unsupported_warning();
                effects.push(Effect::Reply(reply, Ok(self.attach_state_result(now))));
                effects
            }
            plan => {
                self.login_run = Some(LoginRun {
                    epoch,
                    kind: LoginRunKind::Reprobe,
                    waiters: vec![reply],
                });
                vec![Effect::StartLogin {
                    epoch,
                    fsm_gen: self.generations.fsm_gen,
                    plan,
                }]
            }
        }
    }

    fn rx_chunk(&mut self, epoch: u64, bytes: &[u8], now: Mono) -> Vec<Effect> {
        if self.generations.conn_epoch != Some(epoch) || bytes.is_empty() {
            return Vec::new();
        }
        self.last_rx_mono = now;
        self.last_rx_at = Some(self.wall_iso());
        if self.is_passthrough() {
            // SC-35：passthrough 不 arm quiet，只重設安靜視窗。
            if self.state == SessionState::Attached {
                self.quiet_since = Some(now);
            }
            return Vec::new();
        }
        let chunk = clean_text(&String::from_utf8_lossy(bytes));
        if chunk.is_empty() {
            return Vec::new();
        }
        match self.quiet.on_rx_text(&chunk, &self.profile, now) {
            QuietTransition::Armed => self.apply_quiet_armed(now),
            QuietTransition::Cleared => {
                // 只解除 TX 靜默維度；reconfirm pending 保留（#162）。
                self.deps.fence.with(|f| f.quiet_active = false);
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
        self.generations.fsm_gen = self.generations.fsm_gen.saturating_add(1);
        self.generations.owner_gen = self.generations.owner_gen.saturating_add(1);
        let pending = self.quiet.pending;
        self.deps.fence.with(|f| {
            f.quiet_active = true;
            f.reconfirm_pending = pending;
            f.fsm_gen = self.generations.fsm_gen;
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
                self.deps.fence.with(|f| f.quiet_active = false);
                Vec::new()
            }
            TimerEvent::LeaseExpiry { lease_id, deadline }
                if self.interactive_session_id.as_deref() == Some(lease_id.as_str())
                    && self.interactive_deadline == Some(deadline)
                    && now >= deadline =>
            {
                self.clear_interactive();
                Vec::new()
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
            }));
        }
        effects
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
        self.queue_registered = false;
        self.queue = SessionQueue::new();
        self.clear_interactive();
        self.quiet = QuietState::default();
        self.login_run = None;
        self.foreground_reboot = false;
        self.deps.fence.with(|f| {
            f.active_epoch = None;
            f.frozen = true;
            f.quiet_active = false;
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
                let open_effects = if matches!(
                    kind,
                    StopKind::IoError | StopKind::Recover | StopKind::Clear
                ) {
                    self.reopen_known_device(now)
                } else {
                    Vec::new()
                };
                effects.extend(open_effects);
                for waiter in stop.waiters {
                    effects.push(Effect::Reply(waiter, Ok(self.ok_session(now))));
                }
            }
            StopKind::Bind => {
                self.set_state(SessionState::Detached, now);
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
                PyValue::from(if payload_len == Some(confirmed_bytes) {
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
        self.released_reason = match reason {
            PyValue::Str(s) => Some(s),
            PyValue::Null => None,
            other => Some(format!("{other:?}")),
        };
        self.set_last_error(Some("RELEASED".into()), None);
        self.set_state(SessionState::Released, now);
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
            return vec![Effect::Reply(
                reply,
                Ok(pydict! {
                    "ok" => false,
                    "error_code" => "SESSION_RECOVERING",
                    "selector" => self.profile.com.clone(),
                }),
            )];
        }
        if self.state == SessionState::Flashing {
            return vec![Effect::Reply(
                reply,
                Ok(pydict! {
                    "ok" => false,
                    "error_code" => "FLASHING_BUSY",
                    "selector" => self.profile.com.clone(),
                }),
            )];
        }
        if self.state != SessionState::Ready
            && !(allow_attached && self.state == SessionState::Attached)
        {
            return vec![Effect::Reply(
                reply,
                Ok(pydict! {
                    "ok" => false,
                    "error_code" => "SESSION_NOT_READY",
                    "selector" => self.profile.com.clone(),
                }),
            )];
        }
        if self.foreground.is_some() {
            return vec![Effect::Reply(
                reply,
                Ok(pydict! {
                    "ok" => false,
                    "error_code" => "SESSION_BUSY",
                    "selector" => self.profile.com.clone(),
                }),
            )];
        }
        if self.interactive_session_id.is_some() {
            let mut result = pydict! {
                "ok" => false,
                "error_code" => "SESSION_INTERACTIVE_BUSY",
            };
            result.insert(
                "interactive_session_id",
                option_string(self.interactive_session_id.as_deref()),
            );
            return vec![Effect::Reply(reply, Ok(result))];
        }
        let lease_id = self.next_uuid();
        let duration_ns = (timeout_s.max(0.0) * 1_000_000_000.0) as u64;
        let deadline = now.saturating_add(duration_ns);
        self.generations.owner_gen = self.generations.owner_gen.saturating_add(1);
        self.deps
            .fence
            .with(|f| f.owner_gen = self.generations.owner_gen);
        self.interactive_session_id = Some(lease_id.clone());
        self.interactive_owner = Some(owner.clone());
        self.interactive_deadline = Some(deadline);
        let start_op = if !command.is_empty() {
            self.next_op_id = self.next_op_id.saturating_add(1);
            let epoch = self.generations.conn_epoch.unwrap_or_default();
            let grant = OpGrant {
                op_id: OpId(self.next_op_id),
                epoch,
                owner_gen: self.generations.owner_gen,
                fsm_gen: self.generations.fsm_gen,
                cmd_id: None,
            };
            self.foreground = Some(grant.clone());
            Some((grant, OpSpec::InteractiveOpenCmd { command, owner }))
        } else {
            None
        };
        let mut effects = vec![
            Effect::Reply(
                reply,
                Ok(pydict! {
                    "ok" => true,
                    "interactive_id" => lease_id.clone(),
                    "session" => self.public(now, &PublicCtx { console_count: 0, vtty: None, console_endpoint: None })
                }),
            ),
            Effect::Schedule {
                at: deadline,
                ev: TimerEvent::LeaseExpiry {
                    lease_id: lease_id.clone(),
                    deadline,
                },
            },
        ];
        if let Some((grant, spec)) = start_op {
            effects.push(Effect::StartOp { grant, spec });
        }
        effects
    }

    fn clear_interactive(&mut self) {
        if self.interactive_session_id.take().is_some() {
            self.interactive_owner = None;
            self.interactive_deadline = None;
            self.generations.owner_gen = self.generations.owner_gen.saturating_add(1);
            self.deps
                .fence
                .with(|f| f.owner_gen = self.generations.owner_gen);
        }
    }

    fn op_finished(
        &mut self,
        op_id: OpId,
        epoch: u64,
        outcome: OpOutcome,
        _now: Mono,
    ) -> Vec<Effect> {
        if self.stop.as_ref().is_some_and(|stop| {
            stop.epoch == epoch
                && stop
                    .aborted_op
                    .as_ref()
                    .is_some_and(|(aborted_id, _)| *aborted_id == op_id)
        }) {
            return Vec::new();
        }
        let Some(grant) = self.foreground.as_ref() else {
            return Vec::new();
        };
        if grant.op_id != op_id || grant.epoch != epoch {
            return Vec::new();
        }
        let cmd_id = grant.cmd_id.clone();
        self.foreground = None;
        self.foreground_reboot = false;
        if let Some(cmd_id) = cmd_id {
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
        let mut effects = vec![Effect::SettleTx { epoch, op_id }];
        effects.extend(self.start_next());
        effects
    }

    fn start_next(&mut self) -> Vec<Effect> {
        if self.foreground.is_some() || self.state != SessionState::Ready {
            return Vec::new();
        }
        loop {
            let Some(command) = self.queue.pop() else {
                return Vec::new();
            };
            if matches!(
                self.deps
                    .registry
                    .mark_running(&command.cmd_id, &self.wall_iso()),
                RunDecision::Skip
            ) {
                continue;
            }
            let Some(epoch) = self.generations.conn_epoch else {
                return Vec::new();
            };
            self.next_op_id = self.next_op_id.saturating_add(1);
            let grant = OpGrant {
                op_id: OpId(self.next_op_id),
                epoch,
                owner_gen: self.generations.owner_gen,
                fsm_gen: self.generations.fsm_gen,
                cmd_id: Some(command.cmd_id.clone()),
            };
            self.foreground = Some(grant.clone());
            self.foreground_reboot =
                !self.is_passthrough() && is_reboot_command(&command.req.command);
            return vec![Effect::StartOp {
                grant,
                spec: OpSpec::Command(command),
            }];
        }
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

fn option_string(value: Option<&str>) -> PyValue {
    value
        .map(|s| PyValue::from(s.to_string()))
        .unwrap_or(PyValue::Null)
}

fn py_int(value: i128) -> PyValue {
    PyValue::Int(PyInt::Small(value))
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
