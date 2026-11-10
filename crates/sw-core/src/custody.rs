//! S2-PAR-wp510r: 保管帳本基礎（Custody Ledger）與三組執行緒協同。
//!
//! 依定稿設計第 5 版 §3.1.1、§3.1.2（C1～C8）、§3.1.5、§3.1.7（T14 凍結分類）：
//! - 保管帳本：以 by-id 保管尚未被 actor 確認接手的 handle 或 IoWorker。
//! - 關閉工作者：非阻塞關閉 handle，產生終態 Terminal。
//! - 回收者：只消費 reaper_rx 與無界 reaper_cmd，不阻塞 actor lane。
//! - 送達者：try_send 加退避重試，可取消，Pending 暫停、Permanent 丟棄，總時限 30 s。
//! - 守衛：ExitReclaim 與 Reclaim<IoWorker> 的 Drop 只送無界命令，絕不取 manager 鎖。

use std::collections::{HashMap, HashSet};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};
use crossbeam_channel::{Receiver, Sender, TrySendError};

use crate::admission::{AdmissionGate, HandoffFreezeState};
use crate::drain::{StopId, WorkerStoppedReceipt};
use crate::mailbox::ControlEvent;
use crate::session_core::UnconfirmedCloses;
use crate::session_manager::HandleParts;
use crate::uart_bridge::IoWorker;

/// 紀錄持有者；帶有 session actor 世代號 `inc`（每次建構遞增）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CustodyOwner {
    Worker(String),
    Session { sid: String, inc: u64 },
    Parked,
    Reaper,
}

/// 認領方標註。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CustodyTaker {
    Worker(String),
    OpenJob { sid: String, gen: u64 },
    PlaceholderOpen { sid: String, gen: u64 },
    Retire { sid: String },
}

/// 取消原因。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CustodyCancel {
    Release,
    Bind,
    Shutdown,
    Device,
    Freeze,
}

/// 保管生命週期狀態。
pub enum CustodyPhase {
    Opening { taker: CustodyTaker },
    Held { parts: Option<HandleParts> },
    Taken { taker: CustodyTaker },
    Bridged { epoch: u64, worker: Option<IoWorker> },
    BridgeDraining { epoch: u64, stop_id: StopId },
    Closing { job: String },
    CloseFailed { error: String },
}

/// 測試與觀測用的快照狀態（不包含不可 Clone 的 parts 或 worker）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CustodyPhaseSnapshot {
    Opening { taker: CustodyTaker },
    Held,
    Taken { taker: CustodyTaker },
    Bridged { epoch: u64, worker_in_ledger: bool },
    BridgeDraining { epoch: u64, stop_id: StopId },
    Closing { job: String },
    CloseFailed { error: String },
}

/// 保管帳本單一紀錄。
pub struct Custody {
    pub token: u64,
    pub real_path: String,
    pub owner: CustodyOwner,
    pub phase: CustodyPhase,
    pub cancel: Option<CustodyCancel>,
    pub watchers: Vec<String>,
    pub release_deadline: Option<Instant>,
}

impl Custody {
    pub fn snapshot(&self) -> CustodySnapshot {
        CustodySnapshot {
            token: self.token,
            real_path: self.real_path.clone(),
            owner: self.owner.clone(),
            phase: match &self.phase {
                CustodyPhase::Opening { taker } => CustodyPhaseSnapshot::Opening { taker: taker.clone() },
                CustodyPhase::Held { .. } => CustodyPhaseSnapshot::Held,
                CustodyPhase::Taken { taker } => CustodyPhaseSnapshot::Taken { taker: taker.clone() },
                CustodyPhase::Bridged { epoch, worker } => CustodyPhaseSnapshot::Bridged {
                    epoch: *epoch,
                    worker_in_ledger: worker.is_some(),
                },
                CustodyPhase::BridgeDraining { epoch, stop_id } => CustodyPhaseSnapshot::BridgeDraining {
                    epoch: *epoch,
                    stop_id: *stop_id,
                },
                CustodyPhase::Closing { job } => CustodyPhaseSnapshot::Closing { job: job.clone() },
                CustodyPhase::CloseFailed { error } => CustodyPhaseSnapshot::CloseFailed { error: error.clone() },
            },
            cancel: self.cancel,
            release_deadline: self.release_deadline,
            watchers: self.watchers.clone(),
        }
    }
}

/// 觀測用 Custody 快照。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CustodySnapshot {
    pub token: u64,
    pub real_path: String,
    pub owner: CustodyOwner,
    pub phase: CustodyPhaseSnapshot,
    pub cancel: Option<CustodyCancel>,
    pub release_deadline: Option<Instant>,
    pub watchers: Vec<String>,
}

/// 關閉終態（handle 結果與 evidence 分開）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Terminal {
    pub handle: TerminalHandle,
    pub evidence: TerminalEvidence,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum TerminalHandle {
    Closed,
    NoHandle,
    Failed { error: String },
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum TerminalEvidence {
    Complete,
    Incomplete {
        gaps: Vec<String>,
        wal_error: Option<String>,
    },
}

/// 登記開埠失敗原因。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CustodyRegisterError {
    CustodyBusy { token: u64 },
    CloseBlocked,
}

/// 認領 parked 失敗原因。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CustodyClaimParkedError {
    NotFound,
    NotParked,
    PathMismatch,
    NotHeld,
}

/// 結算回收者排空終態失敗原因。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CustodySettleError {
    TokenNotFound,
    AlreadySettled,
    InvalidPhase,
    EpochMismatch { expected: u64, actual: u64 },
    StopIdMismatch { expected: StopId, actual: StopId },
}

/// 資源關閉／排空工作執行錯誤。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum JobExecutionError {
    MissingReaperChannel,
}

/// actor 結束時由帳本產出的資源關閉／排空工作（具所有權）。
pub enum ActorExitedJob {
    CloseHeld {
        token: u64,
        by_id: String,
        parts: Option<HandleParts>,
    },
    DrainBridge {
        token: u64,
        by_id: String,
        epoch: u64,
        stop_id: StopId,
        worker: Option<IoWorker>,
    },
}

impl std::fmt::Debug for ActorExitedJob {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            ActorExitedJob::CloseHeld { token, by_id, parts } => f
                .debug_struct("CloseHeld")
                .field("token", token)
                .field("by_id", by_id)
                .field("has_parts", &parts.is_some())
                .finish(),
            ActorExitedJob::DrainBridge { token, by_id, epoch, stop_id, worker } => f
                .debug_struct("DrainBridge")
                .field("token", token)
                .field("by_id", by_id)
                .field("epoch", epoch)
                .field("stop_id", stop_id)
                .field("has_worker", &worker.is_some())
                .finish(),
        }
    }
}

impl ActorExitedJob {
    pub fn token(&self) -> u64 {
        match self {
            ActorExitedJob::CloseHeld { token, .. } => *token,
            ActorExitedJob::DrainBridge { token, .. } => *token,
        }
    }

    pub fn by_id(&self) -> &str {
        match self {
            ActorExitedJob::CloseHeld { by_id, .. } => by_id,
            ActorExitedJob::DrainBridge { by_id, .. } => by_id,
        }
    }

    pub fn is_drain_bridge(&self) -> bool {
        matches!(self, ActorExitedJob::DrainBridge { .. })
    }

    pub fn has_worker(&self) -> bool {
        match self {
            ActorExitedJob::DrainBridge { worker, .. } => worker.is_some(),
            _ => false,
        }
    }

    /// 執行關閉或啟動排空，並於 CloseHeld 時結算進帳本回傳 terminal。
    /// 若為 DrainBridge 且缺少 reaper_tx，回傳錯誤並完整歸還原工作所有權，不得消耗 worker。
    #[allow(clippy::result_large_err)]
    pub fn execute_and_settle(
        self,
        ledger: &mut CustodyLedger,
        reaper_tx: Option<&Sender<ControlEvent>>,
    ) -> Result<Option<Terminal>, (JobExecutionError, Self)> {
        match self {
            ActorExitedJob::CloseHeld { token, parts, .. } => {
                let receipt = parts.map(|p| p.close());
                let (handle_term, evidence_term) = match receipt {
                    Some(r) if r.closed => (TerminalHandle::Closed, TerminalEvidence::Complete),
                    Some(r) => (
                        TerminalHandle::Failed {
                            error: r
                                .error
                                .map(|e| e.py_message)
                                .unwrap_or_else(|| "handle close failed".into()),
                        },
                        TerminalEvidence::Complete,
                    ),
                    None => (TerminalHandle::Closed, TerminalEvidence::Complete),
                };
                let term = Terminal {
                    handle: handle_term,
                    evidence: evidence_term,
                };
                ledger.settle_close(token, term.clone());
                Ok(Some(term))
            }
            ActorExitedJob::DrainBridge {
                token,
                by_id,
                epoch,
                stop_id,
                worker,
            } => {
                let Some(tx) = reaper_tx else {
                    return Err((
                        JobExecutionError::MissingReaperChannel,
                        ActorExitedJob::DrainBridge {
                            token,
                            by_id,
                            epoch,
                            stop_id,
                            worker,
                        },
                    ));
                };
                if let Some(worker) = worker {
                    worker.finish_async(stop_id, tx.clone());
                }
                Ok(None)
            }
        }
    }
}

/// DeviceAttached 送達義務。
#[derive(Debug, Clone)]
pub struct DeliveryObligation {
    pub event: DeviceAttachedEvent,
    pub bind_gen: u64,
    pub inc: u64,
    pub created_at: Instant,
    pub frozen_at_create: Duration,
    pub next_at: Instant,
    pub backoff: Duration,
    pub attempts: usize,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DeviceAttachedEvent {
    pub by_id: String,
    pub real_path: String,
}

/// 測試用 Mock Worker。
#[derive(Debug, Clone)]
pub struct MockWorker {
    pub epoch: u64,
}

/// 可送交回收者排空的 Worker 包裝。
pub enum ReclaimWorker {
    Real(IoWorker),
    Mock(MockWorker),
}

impl ReclaimWorker {
    pub fn epoch(&self) -> u64 {
        match self {
            ReclaimWorker::Real(w) => w.epoch(),
            ReclaimWorker::Mock(w) => w.epoch,
        }
    }
}

pub trait IntoReclaimWorker: Send + 'static {
    fn into_reclaim_worker(self) -> ReclaimWorker;
}

impl IntoReclaimWorker for IoWorker {
    fn into_reclaim_worker(self) -> ReclaimWorker {
        ReclaimWorker::Real(self)
    }
}

impl IntoReclaimWorker for MockWorker {
    fn into_reclaim_worker(self) -> ReclaimWorker {
        ReclaimWorker::Mock(self)
    }
}

/// 回收者命令（經無界通道送出，Drop 絕不取 manager 鎖）。
pub enum ReaperCmd {
    ActorExited { sid: String, inc: u64 },
    Reclaim { token: u64, epoch: u64, worker: Box<ReclaimWorker> },
}

/// 只在 actor 執行緒 closure 內建立的守衛；結束時送出 ActorExited。
pub struct ExitReclaim {
    sid: String,
    inc: u64,
    cmd_tx: Option<Sender<ReaperCmd>>,
}

impl ExitReclaim {
    pub fn new(sid: String, inc: u64, cmd_tx: Sender<ReaperCmd>) -> Self {
        Self {
            sid,
            inc,
            cmd_tx: Some(cmd_tx),
        }
    }

    pub fn cancel(&mut self) {
        self.cmd_tx = None;
    }
}

impl Drop for ExitReclaim {
    fn drop(&mut self) {
        if let Some(tx) = self.cmd_tx.take() {
            let _ = tx.send(ReaperCmd::ActorExited {
                sid: self.sid.clone(),
                inc: self.inc,
            });
        }
    }
}

/// 保護已從帳本取出但尚未確認接手的 Worker。
pub struct Reclaim<T: IntoReclaimWorker> {
    pub token: u64,
    pub epoch: u64,
    inner: Option<T>,
    cmd_tx: Option<Sender<ReaperCmd>>,
}

impl<T: IntoReclaimWorker> Reclaim<T> {
    pub fn new(token: u64, epoch: u64, inner: T, cmd_tx: Sender<ReaperCmd>) -> Self {
        Self {
            token,
            epoch,
            inner: Some(inner),
            cmd_tx: Some(cmd_tx),
        }
    }

    pub fn defuse(mut self) -> T {
        self.inner.take().expect("Reclaim defused")
    }
}

impl<T: IntoReclaimWorker> Drop for Reclaim<T> {
    fn drop(&mut self) {
        if let Some(inner) = self.inner.take() {
            if let Some(tx) = self.cmd_tx.take() {
                let _ = tx.send(ReaperCmd::Reclaim {
                    token: self.token,
                    epoch: self.epoch,
                    worker: Box::new(inner.into_reclaim_worker()),
                });
            }
        }
    }
}

/// 保管帳本狀態機核心。
pub struct CustodyLedger {
    pub custody: HashMap<String, Custody>,
    pub terminals: HashMap<u64, Terminal>,
    pub tombstones: HashSet<u64>,
    pub unconfirmed: Arc<UnconfirmedCloses>,
    pub session_inc: HashMap<String, u64>,
    next_token: AtomicU64,
}

impl Default for CustodyLedger {
    fn default() -> Self {
        Self::new(UnconfirmedCloses::new())
    }
}

impl CustodyLedger {
    pub fn new(unconfirmed: Arc<UnconfirmedCloses>) -> Self {
        Self {
            custody: HashMap::new(),
            terminals: HashMap::new(),
            tombstones: HashSet::new(),
            unconfirmed,
            session_inc: HashMap::new(),
            next_token: AtomicU64::new(1),
        }
    }

    /// 分配 session actor 世代號（每次建構遞增）。
    pub fn alloc_session_inc(&mut self, sid: &str) -> u64 {
        let entry = self.session_inc.entry(sid.to_string()).or_insert(0);
        *entry += 1;
        *entry
    }

    pub fn current_session_inc(&self, sid: &str) -> u64 {
        self.session_inc.get(sid).copied().unwrap_or(0)
    }

    /// C1 新 token 閘門。
    pub fn register(
        &mut self,
        by_id: &str,
        real_path: &str,
        owner: CustodyOwner,
        taker: CustodyTaker,
    ) -> Result<u64, CustodyRegisterError> {
        if self.unconfirmed.contains(by_id) {
            return Err(CustodyRegisterError::CloseBlocked);
        }
        if let Some(existing) = self.custody.get(by_id) {
            if matches!(existing.phase, CustodyPhase::CloseFailed { .. }) {
                return Err(CustodyRegisterError::CloseBlocked);
            }
            return Err(CustodyRegisterError::CustodyBusy { token: existing.token });
        }
        let token = self.next_token.fetch_add(1, Ordering::Relaxed);
        let custody = Custody {
            token,
            real_path: real_path.to_string(),
            owner,
            phase: CustodyPhase::Opening { taker },
            cancel: None,
            watchers: Vec::new(),
            release_deadline: None,
        };
        self.custody.insert(by_id.to_string(), custody);
        Ok(token)
    }

    /// C1' 同 token 認領（只用於 Held{owner: Parked} 且 real_path 相同）。
    pub fn claim_parked(
        &mut self,
        by_id: &str,
        real_path: &str,
        taker: CustodyTaker,
    ) -> Result<(u64, Option<HandleParts>), CustodyClaimParkedError> {
        let new_owner = match &taker {
            CustodyTaker::OpenJob { sid, .. } => {
                let inc = self.current_session_inc(sid);
                CustodyOwner::Session { sid: sid.clone(), inc }
            }
            CustodyTaker::PlaceholderOpen { sid, .. } => {
                let inc = self.current_session_inc(sid);
                CustodyOwner::Session { sid: sid.clone(), inc }
            }
            CustodyTaker::Worker(op) => CustodyOwner::Worker(op.clone()),
            CustodyTaker::Retire { sid } => {
                let inc = self.current_session_inc(sid);
                CustodyOwner::Session { sid: sid.clone(), inc }
            }
        };

        let rec = self.custody.get_mut(by_id).ok_or(CustodyClaimParkedError::NotFound)?;
        if rec.owner != CustodyOwner::Parked {
            return Err(CustodyClaimParkedError::NotParked);
        }
        if rec.real_path != real_path {
            return Err(CustodyClaimParkedError::PathMismatch);
        }
        match std::mem::replace(&mut rec.phase, CustodyPhase::Taken { taker }) {
            CustodyPhase::Held { parts } => {
                let token = rec.token;
                rec.owner = new_owner;
                Ok((token, parts))
            }
            other => {
                rec.phase = other;
                Err(CustodyClaimParkedError::NotHeld)
            }
        }
    }

    /// 測試輔助：存入 mock parts 轉為 Held。
    pub fn save_held_mock(&mut self, token: u64) -> Result<(), &'static str> {
        for rec in self.custody.values_mut() {
            if rec.token == token {
                rec.phase = CustodyPhase::Held { parts: None };
                return Ok(());
            }
        }
        Err("token not found")
    }

    pub fn get_custody(&self, by_id: &str) -> Option<&Custody> {
        self.custody.get(by_id)
    }

    pub fn get_custody_by_token(&self, token: u64) -> Option<&Custody> {
        self.custody.values().find(|c| c.token == token)
    }

    pub fn mark_bridge_draining(
        &mut self,
        token: u64,
        epoch: u64,
        stop_id: StopId,
    ) -> Result<Option<IoWorker>, &'static str> {
        for rec in self.custody.values_mut() {
            if rec.token == token {
                let worker = match &mut rec.phase {
                    CustodyPhase::Bridged { worker, .. } => worker.take(),
                    _ => None,
                };
                rec.phase = CustodyPhase::BridgeDraining { epoch, stop_id };
                return Ok(worker);
            }
        }
        Err("token not found")
    }

    /// 結算關閉終態（由關閉工作者回報）。
    pub fn settle_close(&mut self, token: u64, terminal: Terminal) {
        self.terminals.insert(token, terminal.clone());
        let by_id_found = self.custody.iter().find_map(|(k, v)| (v.token == token).then(|| k.clone()));
        if let Some(by_id) = by_id_found {
            match &terminal.handle {
                TerminalHandle::Closed | TerminalHandle::NoHandle => {
                    self.custody.remove(&by_id);
                    self.tombstones.insert(token);
                }
                TerminalHandle::Failed { error } => {
                    if let Some(rec) = self.custody.get_mut(&by_id) {
                        rec.phase = CustodyPhase::CloseFailed { error: error.clone() };
                    }
                    self.unconfirmed.insert(&by_id);
                }
            }
        }
    }

    /// 結算回收者排空終態（分開 handle 關閉與證據完整度；完整核對 token、epoch、stop_id 身分配對）。
    /// 注意：Held 的關閉只能由 settle_close 結算，不接受 WorkerStopped（InvalidPhase）。
    pub fn settle_worker_stopped(
        &mut self,
        token: u64,
        receipt: WorkerStoppedReceipt,
    ) -> Result<Terminal, CustodySettleError> {
        if self.terminals.contains_key(&token) || self.tombstones.contains(&token) {
            return Err(CustodySettleError::AlreadySettled);
        }

        let (by_id, expected_epoch, expected_stop_id) = match self.custody.iter().find(|(_, v)| v.token == token) {
            Some((k, v)) => match &v.phase {
                CustodyPhase::BridgeDraining { epoch, stop_id } => (k.clone(), *epoch, *stop_id),
                _ => return Err(CustodySettleError::InvalidPhase),
            },
            None => return Err(CustodySettleError::TokenNotFound),
        };

        if expected_epoch != receipt.epoch {
            return Err(CustodySettleError::EpochMismatch {
                expected: expected_epoch,
                actual: receipt.epoch,
            });
        }

        if expected_stop_id != receipt.stop_id {
            return Err(CustodySettleError::StopIdMismatch {
                expected: expected_stop_id,
                actual: receipt.stop_id,
            });
        }

        let handle_term = if receipt.handle_closed {
            TerminalHandle::Closed
        } else {
            TerminalHandle::Failed {
                error: receipt
                    .close_error
                    .map(|e| e.py_message)
                    .unwrap_or_else(|| "handle_closed: false".into()),
            }
        };

        let evidence_term = if receipt.unrecorded.is_empty() && receipt.wal_error.is_none() {
            TerminalEvidence::Complete
        } else {
            TerminalEvidence::Incomplete {
                gaps: receipt.unrecorded.into_iter().map(|g| format!("{}: {:?}", g.dir, g.error)).collect(),
                wal_error: receipt.wal_error.map(|e| e.py_message),
            }
        };

        let terminal = Terminal {
            handle: handle_term,
            evidence: evidence_term,
        };

        self.terminals.insert(token, terminal.clone());

        // 依 C3 處置紀錄
        match &terminal.handle {
            TerminalHandle::Closed | TerminalHandle::NoHandle => {
                self.custody.remove(&by_id);
                self.tombstones.insert(token);
            }
            TerminalHandle::Failed { error } => {
                if let Some(rec) = self.custody.get_mut(&by_id) {
                    rec.phase = CustodyPhase::CloseFailed { error: error.clone() };
                }
                self.unconfirmed.insert(&by_id);
            }
        }

        Ok(terminal)
    }

    /// panic 守衛結算終態。
    pub fn settle_panic(&mut self, token: u64) -> Terminal {
        let terminal = Terminal {
            handle: TerminalHandle::Failed { error: "unknown".into() },
            evidence: TerminalEvidence::Complete,
        };
        self.terminals.insert(token, terminal.clone());
        let by_id_found = self.custody.iter().find_map(|(k, v)| (v.token == token).then(|| k.clone()));
        if let Some(by_id) = by_id_found {
            if let Some(rec) = self.custody.get_mut(&by_id) {
                rec.phase = CustodyPhase::CloseFailed { error: "unknown".into() };
            }
            self.unconfirmed.insert(&by_id);
        }
        terminal
    }

    /// 處理同世代的 ActorExited，鎖內取出受保管資源並產生具所有權的關閉／排空工作；非同世代的命令不予理會。
    pub fn handle_actor_exited(&mut self, sid: &str, inc: u64) -> Vec<ActorExitedJob> {
        let current_inc = self.current_session_inc(sid);
        if inc != current_inc {
            return Vec::new();
        }

        let mut jobs = Vec::new();
        for (by_id, rec) in self.custody.iter_mut() {
            if let CustodyOwner::Session { sid: s, inc: i } = &rec.owner {
                if s == sid && *i == inc {
                    match &mut rec.phase {
                        CustodyPhase::Held { parts } => {
                            let parts_taken = parts.take();
                            rec.phase = CustodyPhase::Closing { job: "actor_exited".into() };
                            rec.owner = CustodyOwner::Reaper;
                            jobs.push(ActorExitedJob::CloseHeld {
                                token: rec.token,
                                by_id: by_id.clone(),
                                parts: parts_taken,
                            });
                        }
                        CustodyPhase::Bridged { epoch, worker } => {
                            if let Some(w) = worker.take() {
                                let ep = *epoch;
                                let stop_id = StopId(0);
                                rec.phase = CustodyPhase::BridgeDraining { epoch: ep, stop_id };
                                rec.owner = CustodyOwner::Reaper;
                                jobs.push(ActorExitedJob::DrainBridge {
                                    token: rec.token,
                                    by_id: by_id.clone(),
                                    epoch: ep,
                                    stop_id,
                                    worker: Some(w),
                                });
                            }
                            // 若 worker 為 None，表示已由 AdoptBridge 取出，在 Reclaim 守衛保護下，留給 Reclaim 命令處理（C8）
                        }
                        _ => {}
                    }
                }
            }
        }
        jobs
    }
}

/// 送達者單步執行結果。
#[derive(Debug, PartialEq, Eq)]
pub enum DeliveryStepResult {
    Delivered,
    Backoff,
    PausedPending,
    TimedOut,
    Cancelled,
    NoObligation,
}

/// 送達者（Deliverer）：負責 DeviceAttached 投遞義務。
pub struct Deliverer {
    pub obligations: HashMap<String, DeliveryObligation>,
    gate: Arc<AdmissionGate>,
}

impl Default for Deliverer {
    fn default() -> Self {
        Self::new(Arc::new(AdmissionGate::new()))
    }
}

impl Deliverer {
    pub fn new(gate: Arc<AdmissionGate>) -> Self {
        Self {
            obligations: HashMap::new(),
            gate,
        }
    }

    pub fn obligation_count(&self) -> usize {
        self.obligations.len()
    }

    pub fn attempts(&self, sid: &str) -> usize {
        self.obligations.get(sid).map_or(0, |o| o.attempts)
    }

    pub fn add_obligation(&mut self, sid: &str, by_id: &str, real_path: &str, bind_gen: u64, inc: u64) {
        self.add_obligation_at(sid, by_id, real_path, bind_gen, inc, Instant::now());
    }

    pub fn add_obligation_at(
        &mut self,
        sid: &str,
        by_id: &str,
        real_path: &str,
        bind_gen: u64,
        inc: u64,
        now: Instant,
    ) {
        let frozen_at_create = self.gate.frozen_total(now);
        self.obligations.insert(sid.to_string(), DeliveryObligation {
            event: DeviceAttachedEvent {
                by_id: by_id.to_string(),
                real_path: real_path.to_string(),
            },
            bind_gen,
            inc,
            created_at: now,
            frozen_at_create,
            next_at: now,
            backoff: Duration::from_millis(10),
            attempts: 0,
        });
    }

    pub fn cancel_obligation(&mut self, sid: &str, bind_gen: Option<u64>) {
        if let Some(gen) = bind_gen {
            if let Some(ob) = self.obligations.get(sid) {
                if ob.bind_gen == gen {
                    self.obligations.remove(sid);
                }
            }
        } else {
            self.obligations.remove(sid);
        }
    }

    /// 記錄進入凍結轉態通知（喚醒送達者，不再記帳）。
    pub fn notify_freeze(&mut self, _now: Instant) {}

    /// 記錄解除凍結轉態通知（喚醒送達者，不再記帳）。
    pub fn notify_thaw(&mut self, _now: Instant) {}

    /// 單步送達執行。
    pub fn step_delivery(
        &mut self,
        sid: &str,
        actor_tx: &Sender<ControlEvent>,
        now: Instant,
    ) -> DeliveryStepResult {
        let ob = match self.obligations.get_mut(sid) {
            Some(o) => o,
            None => return DeliveryStepResult::NoObligation,
        };

        // 依 T14 檢查凍結狀態
        match self.gate.handoff_freeze_state() {
            HandoffFreezeState::Pending => {
                // Pending 時暫停嘗試、保留義務、不計總時限、attempts 不增
                return DeliveryStepResult::PausedPending;
            }
            HandoffFreezeState::Permanent => {
                // Permanent 丟棄
                self.obligations.remove(sid);
                return DeliveryStepResult::Cancelled;
            }
            HandoffFreezeState::Open => {}
        }

        // 檢查 30 秒總時限（扣除凍結區間）
        let total_elapsed = now.saturating_duration_since(ob.created_at);
        let frozen_elapsed = self.gate.frozen_total(now).saturating_sub(ob.frozen_at_create);
        let effective_open = total_elapsed.saturating_sub(frozen_elapsed);

        if effective_open >= Duration::from_secs(30) {
            eprintln!("[serialwrap:WARNING] 送達義務超逾 30s 總時限，丟棄：sid={}, by_id={}", sid, ob.event.by_id);
            self.obligations.remove(sid);
            return DeliveryStepResult::TimedOut;
        }

        // 嘗試投遞
        match actor_tx.try_send(ControlEvent::DeviceAttached {
            by_id: ob.event.by_id.clone(),
            real_path: ob.event.real_path.clone(),
        }) {
            Ok(()) => {
                self.obligations.remove(sid);
                DeliveryStepResult::Delivered
            }
            Err(TrySendError::Full(_)) => {
                ob.attempts += 1;
                ob.next_at = now + ob.backoff;
                ob.backoff = (ob.backoff * 2).min(Duration::from_millis(500));
                DeliveryStepResult::Backoff
            }
            Err(TrySendError::Disconnected(_)) => {
                self.obligations.remove(sid);
                DeliveryStepResult::Cancelled
            }
        }
    }
}

/// 回收者執行緒：專職消費 DrainDataBarrier 與 WorkerStopped。
pub struct ReaperThread {
    stopped: Arc<AtomicBool>,
    handle: Option<std::thread::JoinHandle<()>>,
}

impl ReaperThread {
    pub fn start(
        _reaper_cmd_tx: Sender<ReaperCmd>,
        reaper_event_rx: Receiver<ControlEvent>,
    ) -> Self {
        let stopped = Arc::new(AtomicBool::new(false));
        let thread_stopped = stopped.clone();

        let handle = std::thread::Builder::new()
            .name("serialwrap-reaper".into())
            .spawn(move || {
                while !thread_stopped.load(Ordering::Relaxed) {
                    match reaper_event_rx.recv_timeout(Duration::from_millis(50)) {
                        Ok(ControlEvent::DrainDataBarrier { expired, reply, .. }) => {
                            // 不確認：設 expired=true 並立即丟棄 reply
                            expired.store(true, Ordering::Release);
                            drop(reply);
                        }
                        Ok(ControlEvent::WorkerStopped(_receipt)) => {
                            // 收到 WorkerStopped，在完整整合線路中會呼叫 ledger 結算
                        }
                        Ok(_) => {}
                        Err(crossbeam_channel::RecvTimeoutError::Timeout) => {}
                        Err(crossbeam_channel::RecvTimeoutError::Disconnected) => break,
                    }
                }
            })
            .expect("建立 reaper thread");

        Self {
            stopped,
            handle: Some(handle),
        }
    }

    pub fn stop(mut self) {
        self.stopped.store(true, Ordering::Relaxed);
        if let Some(h) = self.handle.take() {
            let _ = h.join();
        }
    }
}
