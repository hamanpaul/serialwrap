//! P5-T11：sans-IO `SessionCore` 狀態機。
//!
//! 型別與公開簽章依 `docs/rust-refactor/plans/06-slice21-daemon-core.md` Task 11「Produces」與 Task 0 骨架凍結；
//! 事件處理只更新 session 狀態並產生效果，I/O 由 actor executor 執行。

use std::collections::HashMap;
use std::sync::Arc;

use sw_pal::traits::{DevicePath, SerialConfig, SysEnv};
use sw_proto::config_types::SessionProfile;
use sw_proto::constants::PASSTHROUGH_READY_QUIET_S;
use sw_proto::pycompat::{clean_text, py_round, uuid4_hex, PyErr};
use sw_proto::pydict;
use sw_proto::pyjson::{PyDict, PyInt, PyValue};

use crate::arbiter::{CmdOutcome, CommandRegistry, RunDecision, SessionQueue, SubmitReq};
use crate::drain::{StopId, StopKind, StopOp, WorkerStoppedReceipt};
use crate::generations::{EpochAllocator, Generations};
use crate::login_fsm::LoginPlan;
use crate::mailbox::{
    ControlEvent, DataEvent, DetachReason, MailboxItem, OpenFailure, OpenedBridge, Reply,
    TimerEvent,
};
use crate::op_runner::{OpOutcome, OpSpec};
use crate::state_store::StateDelta;
use crate::tx_writer::{Fence, FlashMode, OpId};
use crate::uart_bridge::RxShared;
use crate::wal::WalWriter;
use crate::Mono;

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
    queue: SessionQueue,
    foreground: Option<OpGrant>,
    queue_registered: bool,
    stop: Option<StopOp>,
    next_stop_id: u64,
    next_op_id: u64,
    next_uuid: u64,
    quiet_since: Option<Mono>,
    boot_quiet_until: Option<Mono>,
    ready_reconfirm_pending: bool,
    ready_reconfirm_failed: bool,
    ready_reconfirm_deadline: Option<Mono>,
    boot_banner_tail: String,
    drain_stalled: bool,
    drain_timeout_epoch: Option<u64>,
    released_by: Option<String>,
    released_at: Option<String>,
    released_reason: Option<String>,
    interactive_session_id: Option<String>,
    interactive_owner: Option<String>,
    interactive_deadline: Option<Mono>,
    pending_release: Option<(String, PyValue)>,
    #[cfg(feature = "test-fakes")]
    panic_events_remaining: u8,
}

struct PendingOpenStop {
    kind: StopKind,
    waiters: Vec<Reply>,
    started: Mono,
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
            queue: SessionQueue::new(),
            foreground: None,
            queue_registered: false,
            stop: None,
            next_stop_id: 0,
            next_op_id: 0,
            next_uuid: 0,
            quiet_since: None,
            boot_quiet_until: None,
            ready_reconfirm_pending: false,
            ready_reconfirm_failed: false,
            ready_reconfirm_deadline: None,
            boot_banner_tail: String::new(),
            drain_stalled: false,
            drain_timeout_epoch: None,
            released_by: None,
            released_at: None,
            released_reason: None,
            interactive_session_id: None,
            interactive_owner: None,
            interactive_deadline: None,
            pending_release: None,
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
                let _ = reply;
                s21_todo!("T19", "graceful shutdown after worker drain");
            }
            MailboxItem::Control(ControlEvent::DeviceAttached { by_id, real_path }) => {
                effects
                    .0
                    .extend(self.device_attached(&by_id, &real_path, now));
            }
            MailboxItem::Control(ControlEvent::DeviceDetached { by_id, reason }) => {
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
            MailboxItem::Control(ControlEvent::BridgeOpened { result, .. }) => {
                effects.0.extend(self.bridge_opened(result, now));
            }
            MailboxItem::Control(ControlEvent::UartIoError { epoch, reason }) => {
                if self.generations.conn_epoch == Some(epoch) && self.stop.is_none() {
                    self.set_last_error(Some(format!("BRIDGE_DOWN:{reason}")), None);
                    effects
                        .0
                        .extend(self.begin_stop(StopKind::IoError, None, now).0);
                }
            }
            MailboxItem::Control(ControlEvent::UartTxCompleted { epoch, .. }) => {
                if self.generations.conn_epoch == Some(epoch) {
                    self.last_tx_mono = now;
                    self.last_tx_at = Some((self.deps.wall_iso)());
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
            MailboxItem::Control(ControlEvent::StateCommitted { .. }) => {}
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
            .boot_quiet_until
            .filter(|deadline| *deadline > now)
            .map(|deadline| PyValue::from(py_round((deadline - now) as f64 / 1_000_000_000.0, 1)))
            .unwrap_or(PyValue::Null);
        let reconfirm_remaining = self
            .ready_reconfirm_deadline
            .filter(|_| self.ready_reconfirm_pending)
            .map(|deadline| {
                PyValue::from(py_round(
                    deadline.saturating_sub(now) as f64 / 1_000_000_000.0,
                    1,
                ))
            })
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
            PyValue::from(self.ready_reconfirm_pending),
        );
        out.insert(
            "ready_reconfirm_failed",
            PyValue::from(self.ready_reconfirm_failed),
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
        self.boot_quiet_until = Some(deadline);
    }
}

impl SessionCore {
    /// Two-Phase Drain 第一階段的發起點（Task 12；SC-04、arch §5.5）：`kind` 與 `waiter` 凍結簽章（P6 07 計畫依此呼叫）。
    pub fn begin_stop(
        &mut self,
        kind: crate::drain::StopKind,
        waiter: Option<Reply>,
        now: Mono,
    ) -> Effects {
        let mut effects = Effects(Vec::new());
        if let Some(stop) = &mut self.stop {
            if let Some(waiter) = waiter {
                stop.waiters.push(waiter);
            }
            return effects;
        }
        if let Some(pending) = &mut self.pending_open_stop {
            if kind == pending.kind {
                if let Some(waiter) = waiter {
                    pending.waiters.push(waiter);
                }
            } else if let Some(waiter) = waiter {
                effects.push(Effect::Reply(
                    waiter,
                    Ok(self.error_session("SESSION_RECOVERING", now)),
                ));
            }
            return effects;
        }
        let Some(epoch) = self.generations.conn_epoch else {
            if self.state == SessionState::Attaching {
                self.pending_open_stop = Some(PendingOpenStop {
                    kind: kind.clone(),
                    waiters: waiter.into_iter().collect(),
                    started: now,
                });
                self.generations.owner_gen = self.generations.owner_gen.saturating_add(1);
                self.generations.fsm_gen = self.generations.fsm_gen.saturating_add(1);
                self.deps.fence.with(|f| {
                    f.active_epoch = None;
                    f.frozen = true;
                    f.owner_gen = self.generations.owner_gen;
                    f.fsm_gen = self.generations.fsm_gen;
                });
                self.set_state(SessionState::Recovering, now);
                return effects;
            }
            self.finish_stop_without_worker(kind, waiter, now, &mut effects);
            return effects;
        };

        if kind == StopKind::IoError && self.last_error.is_none() {
            self.set_last_error(Some("BRIDGE_DOWN:SERIAL_READ".to_string()), None);
        }

        self.next_stop_id = self.next_stop_id.saturating_add(1);
        let stop_id = StopId(self.next_stop_id);
        let mut waiters = Vec::new();
        if let Some(waiter) = waiter {
            waiters.push(waiter);
        }
        let aborted_op = self
            .foreground
            .as_ref()
            .and_then(|grant| grant.cmd_id.clone().map(|cmd_id| (grant.op_id, cmd_id)));
        self.stop = Some(StopOp {
            id: stop_id,
            kind,
            epoch,
            waiters,
            started: now,
            stalled: false,
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
        self.set_state(SessionState::Recovering, now);
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
        waiter: Option<Reply>,
        now: Mono,
        effects: &mut Effects,
    ) {
        if kind == StopKind::Release {
            self.finish_release(now);
        } else {
            if kind == StopKind::Clear {
                self.set_last_error(Some("CLEARED".to_string()), None);
            }
            self.set_state(SessionState::Detached, now);
        }
        if let Some(waiter) = waiter {
            effects.push(Effect::Reply(waiter, Ok(self.ok_session(now))));
        }
    }

    /// Drain 進行中（SC-05 attach 與 manager 的 open 前檢查用；Task 12）。
    pub fn drain_in_progress(&self) -> bool {
        self.stop.is_some()
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
                    let mut result = self.error_session("SESSION_RECOVERING", now);
                    result.insert("drain_stalled", PyValue::from(self.drain_stalled));
                    result.insert(
                        "drain_timeout_epoch",
                        self.drain_timeout_epoch
                            .map(|epoch| py_int(epoch as i128))
                            .unwrap_or(PyValue::Null),
                    );
                    return vec![Effect::Reply(reply, Ok(result))];
                }
                if self.stop.is_some() || self.pending_open_stop.is_some() {
                    return self.begin_stop(StopKind::Release, Some(reply), now).0;
                }
                self.pending_release = Some((source, reason));
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
                    vec![Effect::Reply(
                        reply,
                        Ok(self.error_session("SESSION_RECOVERING", now)),
                    )]
                } else {
                    vec![Effect::Reply(
                        reply,
                        Ok(self.error_session("SESSION_NOT_READY", now)),
                    )]
                }
            }
            SessionRequest::DeviceAttach { .. } | SessionRequest::SessionAttach => {
                self.credentials_warned = false;
                s21_todo!("T12", "device/session attach request")
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
            if self.ready_reconfirm_failed {
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
            if let Some(deadline) = self.boot_quiet_until.filter(|at| *at > now) {
                let mut result = pydict! {
                    "ok" => false,
                    "error_code" => "AUTOBOOT_QUIET",
                    "retry_after_s" => PyValue::from(py_round((deadline - now) as f64 / 1_000_000_000.0, 1)),
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
            if self.ready_reconfirm_pending {
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

    fn device_attached(&mut self, by_id: &str, real_path: &str, now: Mono) -> Vec<Effect> {
        if by_id != self.profile.device_by_id || self.state == SessionState::Released {
            return Vec::new();
        }
        if self.state != SessionState::Detached {
            return Vec::new();
        }
        self.set_last_error(None, None);
        self.generations.fsm_gen = self.generations.fsm_gen.saturating_add(1);
        self.set_state(SessionState::Attaching, now);
        let uart = &self.profile.template.uart;
        let cfg = SerialConfig::from_uart_profile(
            pyint_to_u32(&uart.baud, 115_200),
            pyint_to_u8(&uart.data_bits, 8),
            &uart.parity,
            pyint_to_u8(&uart.stop_bits, 1),
            &uart.flow_control,
            uart.xonxoff,
        );
        vec![Effect::OpenBridge {
            dev: DevicePath(real_path.to_string()),
            cfg,
            precheck_holders: false,
        }]
    }

    fn bridge_opened(
        &mut self,
        result: Result<OpenedBridge, OpenFailure>,
        now: Mono,
    ) -> Vec<Effect> {
        match result {
            Ok(opened) => {
                let OpenedBridge {
                    epoch,
                    worker,
                    real_path,
                } = opened;
                if let Some(pending) = self.pending_open_stop.take() {
                    return self.drain_opened_bridge(pending, epoch, worker, real_path, now);
                }
                if self.state != SessionState::Attaching {
                    self.next_stop_id = self.next_stop_id.saturating_add(1);
                    let stop_id = StopId(self.next_stop_id);
                    return vec![
                        Effect::AdoptBridge {
                            epoch,
                            worker,
                            real_path,
                        },
                        Effect::BeginDrain { stop_id, epoch },
                    ];
                }
                self.generations.conn_epoch = Some(epoch);
                self.attached_real_path = Some(real_path.clone());
                self.deps.fence.with(|f| {
                    f.active_epoch = Some(epoch);
                    f.fsm_gen = self.generations.fsm_gen;
                    f.owner_gen = self.generations.owner_gen;
                    f.frozen = false;
                });

                if self.is_passthrough() {
                    self.set_last_error(None, None);
                    self.adopt_bridge(now);
                    self.set_state(SessionState::Attached, now);
                    return vec![Effect::AdoptBridge {
                        epoch,
                        worker,
                        real_path,
                    }];
                }
                if !self.profile.template.command_capable() {
                    self.set_last_error(None, None);
                    self.adopt_bridge(now);
                    self.set_state(SessionState::Attached, now);
                    return vec![Effect::AdoptBridge {
                        epoch,
                        worker,
                        real_path,
                    }];
                }
                if self.boot_quiet_until.is_some_and(|deadline| deadline > now) {
                    self.set_last_error(Some("PROMPT_UNAVAILABLE".to_string()), None);
                    self.adopt_bridge(now);
                    self.set_state(SessionState::Attached, now);
                    return vec![Effect::AdoptBridge {
                        epoch,
                        worker,
                        real_path,
                    }];
                }
                let template = &self.profile.template;
                let requires_login = !template.login_regex.is_empty()
                    || template
                        .console_unlock_env
                        .as_deref()
                        .is_some_and(|value| !value.is_empty());
                if requires_login {
                    if let Some(reason) = self.unresolved_credentials() {
                        self.set_last_error(Some("CREDENTIALS_UNRESOLVED".to_string()), None);
                        self.adopt_bridge(now);
                        self.set_state(SessionState::Attached, now);
                        let emit_warning = !self.credentials_warned;
                        self.credentials_warned = true;
                        let mut effects = vec![Effect::AdoptBridge {
                            epoch,
                            worker,
                            real_path,
                        }];
                        if !emit_warning {
                            return effects;
                        }
                        let env_file = self.profile.template.env_file.as_deref().unwrap_or("");
                        let env_file_display = if env_file.is_empty() { "-" } else { env_file };
                        let mut meta = PyDict::new();
                        meta.insert("event", PyValue::from("credentials_unresolved"));
                        meta.insert("reason", PyValue::from(reason));
                        meta.insert("env_file", PyValue::from(env_file));
                        effects.push(Effect::WalMeta { cmd_id: None, meta });
                        effects.push(Effect::Warn(format!(
                            "session {} 帳密解析未完成（reason={reason}, env_file={env_file_display}）：不對 login prompt 送空帳密，標記 CREDENTIALS_UNRESOLVED 終態，請補齊帳密後手動 attach/recover",
                            self.profile.com
                        )));
                        return effects;
                    }
                }
                vec![
                    Effect::AdoptBridge {
                        epoch,
                        worker,
                        real_path,
                    },
                    Effect::StartLogin {
                        epoch,
                        fsm_gen: self.generations.fsm_gen,
                        plan: LoginPlan::Probe,
                    },
                ]
            }
            Err(failure) => {
                let failure_error = open_failure_last_error(&failure);
                if let Some(pending) = self.pending_open_stop.take() {
                    self.set_last_error(Some(failure_error), None);
                    self.finish_pending_open_stop(pending, now)
                } else if self.state == SessionState::Attaching {
                    self.set_last_error(Some(failure_error), None);
                    self.attached_real_path = None;
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
        epoch: u64,
        worker: crate::uart_bridge::IoWorker,
        real_path: String,
        now: Mono,
    ) -> Vec<Effect> {
        self.generations.conn_epoch = Some(epoch);
        self.attached_real_path = None;
        self.next_stop_id = self.next_stop_id.saturating_add(1);
        let stop_id = StopId(self.next_stop_id);
        self.stop = Some(StopOp {
            id: stop_id,
            kind: pending.kind,
            epoch,
            waiters: pending.waiters,
            started: pending.started,
            stalled: false,
            aborted_op: None,
            post_kind: None,
        });
        self.deps.fence.with(|f| {
            f.active_epoch = None;
            f.frozen = true;
            f.owner_gen = self.generations.owner_gen;
            f.fsm_gen = self.generations.fsm_gen;
        });
        self.set_state(SessionState::Recovering, now);
        vec![
            Effect::AdoptBridge {
                epoch,
                worker,
                real_path,
            },
            Effect::BeginDrain { stop_id, epoch },
            Effect::Schedule {
                at: pending
                    .started
                    .saturating_add(crate::resources::DRAIN_TIMEOUT_MS.saturating_mul(1_000_000)),
                ev: TimerEvent::DrainTimeout { epoch, stop_id },
            },
        ]
    }

    fn finish_pending_open_stop(&mut self, pending: PendingOpenStop, now: Mono) -> Vec<Effect> {
        let mut effects = Effects(Vec::new());
        self.finish_stop_without_worker(pending.kind, None, now, &mut effects);
        for waiter in pending.waiters {
            effects.push(Effect::Reply(waiter, Ok(self.ok_session(now))));
        }
        effects.0
    }

    fn unresolved_credentials(&self) -> Option<&'static str> {
        let template = &self.profile.template;
        let mut file_values = HashMap::new();
        let file_issue = match template
            .env_file
            .as_deref()
            .map(|path| (self.deps.read_env_file)(path))
        {
            Some(Some(Ok(contents))) => {
                file_values = parse_auth_env_file(&contents);
                None
            }
            Some(None) => Some("env_file_missing"),
            Some(Some(Err(_))) => Some("env_file_unreadable"),
            None => None,
        };
        let declared = template
            .user_env
            .as_deref()
            .is_some_and(|value| !value.is_empty())
            || template
                .pass_env
                .as_deref()
                .is_some_and(|value| !value.is_empty())
            || template
                .env_file
                .as_deref()
                .is_some_and(|value| !value.is_empty())
            || template
                .console_unlock_env
                .as_deref()
                .is_some_and(|value| !value.is_empty());
        if !declared {
            return None;
        }
        if let Some(key) = template
            .console_unlock_env
            .as_deref()
            .filter(|value| !value.is_empty())
        {
            return resolved_auth_value(key, &file_values, self.deps.env.as_ref())
                .is_none()
                .then(|| file_issue.unwrap_or("key_absent"));
        }
        let username = template
            .user_env
            .as_deref()
            .and_then(|key| resolved_auth_value(key, &file_values, self.deps.env.as_ref()))
            .or_else(|| template.username.clone().filter(|value| !value.is_empty()));
        let password = template
            .pass_env
            .as_deref()
            .and_then(|key| resolved_auth_value(key, &file_values, self.deps.env.as_ref()));
        if username.is_some() && password.is_some() {
            return None;
        }
        Some(file_issue.unwrap_or("key_absent"))
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
        if self.state != SessionState::Attaching
            || self.generations.conn_epoch != Some(epoch)
            || self.generations.fsm_gen != fsm_gen
        {
            return Vec::new();
        }
        match result {
            crate::login_fsm::LoginOutcome::Ready => {
                self.adopt_bridge(now);
                self.last_probe_at = Some(self.wall_iso());
                self.set_last_error(None, None);
                self.set_state(SessionState::Ready, now);
                self.last_ready_at = Some(self.wall_iso());
                self.queue_registered = true;
                self.recovering = false;
                self.confirm_ready();
                vec![Effect::Notify(ManagerNote::SessionReady {
                    sid: self.sid.clone(),
                })]
            }
            crate::login_fsm::LoginOutcome::NotReady(error) => {
                self.adopt_bridge(now);
                self.last_probe_at = Some(self.wall_iso());
                self.set_last_error(Some(error), None);
                self.set_state(SessionState::Attached, now);
                Vec::new()
            }
            crate::login_fsm::LoginOutcome::Failed(error) => {
                self.set_last_error(Some(error), None);
                self.begin_stop(StopKind::LoginFailed, None, now).0
            }
        }
    }

    fn rx_chunk(&mut self, epoch: u64, bytes: &[u8], now: Mono) -> Vec<Effect> {
        if self.generations.conn_epoch != Some(epoch) || bytes.is_empty() {
            return Vec::new();
        }
        self.last_rx_mono = now;
        self.last_rx_at = Some(self.wall_iso());
        if self.is_passthrough() && self.state == SessionState::Attached {
            self.quiet_since = Some(now);
        } else if !self.is_passthrough() {
            let chunk = clean_text(&String::from_utf8_lossy(bytes));
            let mut window = self.boot_banner_tail.clone();
            window.push_str(&chunk);
            if crate::quiet_gate::BOOT_BANNER_PATTERNS
                .iter()
                .any(|pattern| window.contains(pattern))
            {
                self.boot_banner_tail.clear();
                return self.arm_boot_quiet(now);
            }
            self.boot_banner_tail = window
                .chars()
                .rev()
                .take(256)
                .collect::<Vec<_>>()
                .into_iter()
                .rev()
                .collect();
        }
        Vec::new()
    }

    fn arm_boot_quiet(&mut self, now: Mono) -> Vec<Effect> {
        let Some(epoch) = self.generations.conn_epoch else {
            return Vec::new();
        };
        let until = now.saturating_add(180_000_000_000);
        self.boot_quiet_until = Some(until);
        if !self.ready_reconfirm_pending {
            self.ready_reconfirm_pending = true;
            self.ready_reconfirm_failed = false;
            self.ready_reconfirm_deadline = Some(now.saturating_add(300_000_000_000));
        }
        self.generations.fsm_gen = self.generations.fsm_gen.saturating_add(1);
        self.generations.owner_gen = self.generations.owner_gen.saturating_add(1);
        self.deps.fence.with(|f| {
            f.quiet_active = true;
            f.reconfirm_pending = true;
            f.fsm_gen = self.generations.fsm_gen;
            f.owner_gen = self.generations.owner_gen;
        });
        self.boot_banner_tail.clear();
        vec![Effect::Schedule {
            at: until,
            ev: TimerEvent::BootQuietExpired {
                epoch,
                fsm_gen: self.generations.fsm_gen,
            },
        }]
    }

    fn timer(&mut self, event: TimerEvent, now: Mono) -> Vec<Effect> {
        match event {
            TimerEvent::BootQuietExpired { epoch, fsm_gen }
                if self.generations.conn_epoch == Some(epoch)
                    && self.generations.fsm_gen == fsm_gen
                    && self.boot_quiet_until.is_some_and(|at| at <= now) =>
            {
                self.boot_quiet_until = None;
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
        if self.ready_reconfirm_pending && self.ready_reconfirm_deadline.is_some_and(|at| at <= now)
        {
            self.ready_reconfirm_pending = false;
            self.ready_reconfirm_failed = true;
            self.ready_reconfirm_deadline = None;
            self.deps.fence.with(|f| f.reconfirm_pending = false);
        }
    }

    fn confirm_ready(&mut self) {
        self.boot_quiet_until = None;
        self.ready_reconfirm_pending = false;
        self.ready_reconfirm_failed = false;
        self.ready_reconfirm_deadline = None;
        self.boot_banner_tail.clear();
        self.deps.fence.with(|f| {
            f.quiet_active = false;
            f.reconfirm_pending = false;
            f.active_epoch = self.generations.conn_epoch;
            f.fsm_gen = self.generations.fsm_gen;
        });
    }

    fn mark_drain_timeout(&mut self, epoch: u64, stop_id: StopId, _now: Mono) -> Vec<Effect> {
        if let Some(stop) = &mut self.stop {
            if stop.epoch == epoch && stop.id == stop_id {
                stop.stalled = true;
                self.drain_stalled = true;
                self.drain_timeout_epoch = Some(self.generations.bridge_generation);
            }
        }
        Vec::new()
    }

    fn worker_stopped(&mut self, receipt: WorkerStoppedReceipt, now: Mono) -> Vec<Effect> {
        let matches = self
            .stop
            .as_ref()
            .is_some_and(|stop| stop.id == receipt.stop_id && stop.epoch == receipt.epoch);
        if !matches {
            return Vec::new();
        }
        let stop = self.stop.take().expect("已確認的 stop op");
        let was_release = stop.kind == StopKind::Release;
        let was_stalled = stop.stalled;
        let mut effects = Vec::new();
        if let Some((op_id, cmd_id)) = stop.aborted_op.as_ref() {
            let ledger = receipt.ledger.ops.get(op_id);
            let (confirmed_bytes, unknown) = ledger
                .map(|op| (op.confirmed_bytes, op.unknown))
                .unwrap_or((0, false));
            let class = if confirmed_bytes > 0 {
                crate::arbiter::AbortClass::InFlight
            } else if unknown {
                crate::arbiter::AbortClass::Unknown
            } else {
                crate::arbiter::AbortClass::NotSubmitted
            };
            self.deps
                .registry
                .complete(cmd_id, CmdOutcome::Aborted(class), &self.wall_iso());
            if confirmed_bytes > 0 {
                let payload_len = self
                    .deps
                    .registry
                    .get(cmd_id)
                    .and_then(|record| record.get("command").cloned())
                    .and_then(|value| value.as_str().map(|command| command.len() + 1));
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
        self.generations.conn_epoch = None;
        self.recovering = false;
        self.foreground = None;
        self.attached_real_path = None;
        self.queue_registered = false;
        self.queue = SessionQueue::new();
        self.deps
            .registry
            .flush_session(&self.sid, "FLUSHED_BY_RECOVERY", &self.wall_iso());
        self.clear_interactive();
        self.boot_quiet_until = None;
        self.ready_reconfirm_pending = false;
        self.ready_reconfirm_failed = false;
        self.ready_reconfirm_deadline = None;
        self.deps.fence.with(|f| {
            f.active_epoch = None;
            f.frozen = true;
            f.quiet_active = false;
            f.reconfirm_pending = false;
        });
        if was_release {
            self.finish_release(now);
        } else {
            self.set_state(SessionState::Detached, now);
        }

        effects.push(Effect::Notify(ManagerNote::SessionDetached {
            sid: self.sid.clone(),
            reason: self.last_error.clone(),
        }));
        for waiter in stop.waiters {
            let result = if was_release && was_stalled {
                self.error_session("DRAIN_STALLED", now)
            } else {
                self.ok_session(now)
            };
            effects.push(Effect::Reply(waiter, Ok(result)));
        }
        effects
    }

    fn finish_release(&mut self, now: Mono) {
        let (source, reason) = self
            .pending_release
            .take()
            .unwrap_or_else(|| ("cli".into(), PyValue::Null));
        self.released_by = Some(source);
        self.released_at = Some(self.wall_iso());
        self.released_reason = match reason {
            PyValue::Str(s) => Some(s),
            PyValue::Null => None,
            other => Some(format!("{other:?}")),
        };
        self.set_last_error(None, None);
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

fn parse_auth_env_file(contents: &str) -> HashMap<String, String> {
    let mut values = HashMap::new();
    for raw_line in contents.lines() {
        let line = raw_line.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        let line = line.strip_prefix("export ").map(str::trim).unwrap_or(line);
        let Some((raw_key, raw_value)) = line.split_once('=') else {
            continue;
        };
        let key = raw_key.trim();
        if key.is_empty() {
            continue;
        }
        let value = raw_value.trim();
        let value = if value.len() >= 2
            && ((value.starts_with('\'') && value.ends_with('\''))
                || (value.starts_with('"') && value.ends_with('"')))
        {
            &value[1..value.len() - 1]
        } else {
            value
        };
        values.insert(key.to_string(), value.to_string());
    }
    values
}

fn resolved_auth_value(
    key: &str,
    file_values: &HashMap<String, String>,
    env: &dyn SysEnv,
) -> Option<String> {
    file_values
        .get(key)
        .filter(|value| !value.is_empty())
        .cloned()
        .or_else(|| env.var(key).filter(|value| !value.is_empty()))
}
