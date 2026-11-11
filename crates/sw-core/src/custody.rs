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

use crate::admission::{AdmissionGate, GateSnapshot, HandoffFreezeState};
use crate::drain::{StopId, WorkerStoppedReceipt};
use crate::mailbox::ControlEvent;
use crate::session_core::UnconfirmedCloses;
use crate::session_manager::HandleParts;
use crate::uart_bridge::IoWorker;

/// actor 的全域唯一身分；manager 建立 actor 時配置，生命週期內不可變。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct ActorId(u64);

impl ActorId {
    /// 配置不重複的 process-wide actor 身分。
    pub fn allocate() -> Self {
        static NEXT_ACTOR_ID: AtomicU64 = AtomicU64::new(1);
        let id = NEXT_ACTOR_ID
            .fetch_update(Ordering::Relaxed, Ordering::Relaxed, |next| next.checked_add(1))
            .expect("ActorId 空間耗盡");
        Self(id)
    }

    #[cfg(feature = "test-fakes")]
    pub const fn for_test(value: u64) -> Self {
        Self(value)
    }
}

/// 保管紀錄的持有者；session actor 一律以不可變 ActorId 識別。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CustodyOwner {
    Worker(String),
    Session { actor: ActorId },
    Parked,
    Reaper,
}

/// 認領方標註。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CustodyTaker {
    Worker(String),
    OpenJob { actor: ActorId },
    PlaceholderOpen { actor: ActorId },
    Retire { actor: ActorId },
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
    Bridged { epoch: u64, worker: Option<ReclaimWorker> },
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
    /// S2-PAR-wp510r R4（設計 §3.1.4 W6 第 2 步）：預配的 token 已有 tombstone（停止者在登記前先取消：`ClaimForStop`
    /// 查無紀錄時寫入），不得登記，worker 中止、不開埠。
    Tombstoned,
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
        worker: Option<ReclaimWorker>,
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
    pub actor: ActorId,
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

    pub fn finish_async(self, stop_id: StopId, control: Sender<ControlEvent>) -> bool {
        match self {
            ReclaimWorker::Real(w) => {
                w.finish_async(stop_id, control);
                true
            }
            ReclaimWorker::Mock(_) => false,
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
    ActorExited { actor: ActorId },
    Reclaim { actor: ActorId, token: u64, epoch: u64, worker: Box<ReclaimWorker> },
}

/// 只在 actor 執行緒 closure 內建立的守衛；結束時送出 ActorExited。
pub struct ExitReclaim {
    actor: ActorId,
    cmd_tx: Option<Sender<ReaperCmd>>,
}

impl ExitReclaim {
    pub fn new(actor: ActorId, cmd_tx: Sender<ReaperCmd>) -> Self {
        Self {
            actor,
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
            let _ = tx.send(ReaperCmd::ActorExited { actor: self.actor });
        }
    }
}

/// 保護已從帳本取出但尚未確認接手的 Worker。
pub struct Reclaim<T: IntoReclaimWorker> {
    pub actor: ActorId,
    pub token: u64,
    pub epoch: u64,
    inner: Option<T>,
    cmd_tx: Option<Sender<ReaperCmd>>,
}

impl<T: IntoReclaimWorker> Reclaim<T> {
    pub fn new(actor: ActorId, token: u64, epoch: u64, inner: T, cmd_tx: Sender<ReaperCmd>) -> Self {
        Self {
            actor,
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
                send_reclaim_or_finish(&tx, self.actor, self.token, self.epoch, inner.into_reclaim_worker(), None);
            }
        }
    }
}

/// 把未被接手的 worker 送交回收者排空（不阻塞、不取 manager 鎖）。回收者已停止、命令通道斷線時（審查 P8-6a679fb-4：
/// `shutdown` 第二階段停掉回收者之後）不得直接丟棄 worker——IoWorker 沒有 Drop 收尾，丟棄即留下開著的 handle 與讀寫
/// 執行緒——退回就地 `finish_async`：handle 由 worker 自身的收尾關閉一次，回條送往 `control`（未提供時送往立即丟棄的
/// 通道；此時已沒有帳本結算者）。回傳是否已送交回收者。
pub fn send_reclaim_or_finish(
    cmd_tx: &Sender<ReaperCmd>,
    actor: ActorId,
    token: u64,
    epoch: u64,
    worker: ReclaimWorker,
    control: Option<&Sender<ControlEvent>>,
) -> bool {
    match cmd_tx.send(ReaperCmd::Reclaim {
        actor,
        token,
        epoch,
        worker: Box::new(worker),
    }) {
        Ok(()) => true,
        Err(crossbeam_channel::SendError(ReaperCmd::Reclaim { worker, .. })) => {
            let control = control
                .cloned()
                .unwrap_or_else(|| crossbeam_channel::unbounded().0);
            worker.finish_async(StopId(0), control);
            false
        }
        Err(_) => false,
    }
}

/// 保管帳本狀態機核心。
pub struct CustodyLedger {
    pub custody: HashMap<String, Custody>,
    pub terminals: HashMap<u64, Terminal>,
    pub tombstones: HashSet<u64>,
    pub unconfirmed: Arc<UnconfirmedCloses>,
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
            next_token: AtomicU64::new(1),
        }
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

    /// S2-PAR-wp510r R4（設計 §3.1.4 W6 第 1 步）：預配 token。W6 worker 先以此 token 送 `BeginExternalAttach`，actor 接受
    /// 後才以 [`CustodyLedger::register_with_token`] 登記（C7：owner＝Session 的紀錄只在 actor 已知 token 時存在）。
    pub fn alloc_token(&self) -> u64 {
        self.next_token.fetch_add(1, Ordering::Relaxed)
    }

    /// S2-PAR-wp510r R4（設計 §3.1.4 W6 第 2 步）：以預配的 `token` 登記（C1 閘門同 [`CustodyLedger::register`]）。token 已有
    /// tombstone（先取消、後登記）時回 `Tombstoned`，紀錄不建立。
    pub fn register_with_token(
        &mut self,
        by_id: &str,
        token: u64,
        real_path: &str,
        owner: CustodyOwner,
        taker: CustodyTaker,
    ) -> Result<(), CustodyRegisterError> {
        if self.tombstones.contains(&token) {
            return Err(CustodyRegisterError::Tombstoned);
        }
        if self.unconfirmed.contains(by_id) {
            return Err(CustodyRegisterError::CloseBlocked);
        }
        if let Some(existing) = self.custody.get(by_id) {
            if matches!(existing.phase, CustodyPhase::CloseFailed { .. }) {
                return Err(CustodyRegisterError::CloseBlocked);
            }
            return Err(CustodyRegisterError::CustodyBusy { token: existing.token });
        }
        self.custody.insert(
            by_id.to_string(),
            Custody {
                token,
                real_path: real_path.to_string(),
                owner,
                phase: CustodyPhase::Opening { taker },
                cancel: None,
                watchers: Vec::new(),
                release_deadline: None,
            },
        );
        Ok(())
    }

    /// C1' 同 token 認領（只用於 Held{owner: Parked} 且 real_path 相同）。
    pub fn claim_parked(
        &mut self,
        by_id: &str,
        real_path: &str,
        taker: CustodyTaker,
    ) -> Result<(u64, Option<HandleParts>), CustodyClaimParkedError> {
        let new_owner = match &taker {
            CustodyTaker::OpenJob { actor } => CustodyOwner::Session { actor: *actor },
            CustodyTaker::PlaceholderOpen { actor } => CustodyOwner::Session { actor: *actor },
            CustodyTaker::Worker(op) => CustodyOwner::Worker(op.clone()),
            CustodyTaker::Retire { actor } => CustodyOwner::Session { actor: *actor },
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

    /// W3': adopt 提交時將 IoWorker 放入帳本。
    pub fn adopt_bridged(&mut self, token: u64, epoch: u64, worker: IoWorker) -> Result<(), &'static str> {
        for rec in self.custody.values_mut() {
            if rec.token == token {
                rec.phase = CustodyPhase::Bridged {
                    epoch,
                    worker: Some(ReclaimWorker::Real(worker)),
                };
                return Ok(());
            }
        }
        Err("token not found")
    }

    /// 測試輔助：以 MockWorker 轉為 Bridged。
    #[cfg(feature = "test-fakes")]
    pub fn adopt_bridged_mock(&mut self, token: u64, epoch: u64, mock: MockWorker) -> Result<(), &'static str> {
        for rec in self.custody.values_mut() {
            if rec.token == token {
                rec.phase = CustodyPhase::Bridged {
                    epoch,
                    worker: Some(ReclaimWorker::Mock(mock)),
                };
                return Ok(());
            }
        }
        Err("token not found")
    }

    /// W3': AdoptBridge 在 actor 執行緒上自帳本取出 IoWorker。
    pub fn claim_bridged_worker(&mut self, token: u64, epoch: u64) -> Result<IoWorker, &'static str> {
        for rec in self.custody.values_mut() {
            if rec.token == token {
                match &mut rec.phase {
                    CustodyPhase::Bridged { epoch: ep, worker } if *ep == epoch => {
                        let w = worker.take().ok_or("worker already claimed")?;
                        match w {
                            ReclaimWorker::Real(real) => return Ok(real),
                            ReclaimWorker::Mock(_) => return Err("mock worker cannot be converted to IoWorker"),
                        }
                    }
                    _ => return Err("invalid phase or epoch mismatch"),
                }
            }
        }
        Err("token not found")
    }

    /// W3': 放入 actor 的 workers 後自帳本移除紀錄。
    pub fn remove_bridged(&mut self, token: u64) {
        let by_id_found = self.custody.iter().find_map(|(k, v)| (v.token == token).then(|| k.clone()));
        if let Some(by_id) = by_id_found {
            self.custody.remove(&by_id);
            self.tombstones.insert(token);
        }
    }

    /// D4 / DR-7: release 認領未消費的 parked 紀錄並轉 Closing 由關閉工作者關閉。
    /// 先檢查 token 與 phase，全部成立才改寫 owner、cancel 與 phase；回錯誤時紀錄原樣保留。
    pub fn claim_for_release(&mut self, by_id: &str, token: u64) -> Result<Option<HandleParts>, &'static str> {
        let rec = self.custody.get_mut(by_id).ok_or("not found")?;
        if rec.token != token {
            return Err("token mismatch");
        }
        if !matches!(rec.phase, CustodyPhase::Held { .. }) {
            return Err("not held");
        }
        let CustodyPhase::Held { parts } =
            std::mem::replace(&mut rec.phase, CustodyPhase::Closing { job: "release".into() })
        else {
            unreachable!("上方已確認 phase 為 Held");
        };
        rec.owner = CustodyOwner::Reaper;
        rec.cancel = Some(CustodyCancel::Release);
        Ok(parts)
    }

    /// C1′ 同 token 認領的資源已關閉（成功接手以外的收尾）：紀錄仍是該 token、且資源已離開帳本（`Taken` 或
    /// `Closing`）時依回條結算（closed:true 移除並記終態，closed:false 轉 CloseFailed 並登記未確認）；資源仍在帳本
    /// （`Held`、`Bridged`）或已被其他流程結算者不動。回傳是否結算。
    pub fn settle_claimed_close(&mut self, by_id: &str, token: u64, terminal: Terminal) -> bool {
        let claimed = self.custody.get(by_id).is_some_and(|rec| {
            rec.token == token
                && matches!(rec.phase, CustodyPhase::Taken { .. } | CustodyPhase::Closing { .. })
        });
        if claimed {
            self.settle_close(token, terminal);
        }
        claimed
    }

    /// 同 [`CustodyLedger::settle_claimed_close`]，但只結算 `Taken`：認領方（開埠工作、attach 作業）自己持有並關閉的
    /// handle。`Closing` 由轉入 Closing 的關閉者結算，不受其他持有者的關閉影響。
    pub fn settle_taken_close(&mut self, by_id: &str, token: u64, terminal: Terminal) -> bool {
        let taken = self
            .custody
            .get(by_id)
            .is_some_and(|rec| rec.token == token && matches!(rec.phase, CustodyPhase::Taken { .. }));
        if taken {
            self.settle_close(token, terminal);
        }
        taken
    }

    /// C8 / DR-9: actor 建構失敗時清理並產出關閉工作。
    pub fn handle_spawn_failure(&mut self, actor: ActorId) -> Vec<ActorExitedJob> {
        self.handle_actor_exited(actor)
    }

    /// session 停止機制（ClaimForStop / begin_stop）收尾帳本紀錄。
    pub fn claim_for_stop(&mut self, actor: ActorId, cancel: CustodyCancel) -> Vec<ActorExitedJob> {
        let mut jobs = Vec::new();
        for (by_id, rec) in self.custody.iter_mut() {
            if let CustodyOwner::Session { actor: owner } = &rec.owner {
                if *owner == actor {
                    rec.cancel = Some(cancel);
                    match &mut rec.phase {
                        CustodyPhase::Held { parts } => {
                            let parts_taken = parts.take();
                            rec.phase = CustodyPhase::Closing { job: "stop".into() };
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

    /// 取得或設定首次 release 期限（now + 3000 ms）。
    pub fn get_or_set_release_deadline(&mut self, by_id: &str, now: Instant) -> Instant {
        if let Some(rec) = self.custody.get_mut(by_id) {
            if let Some(dl) = rec.release_deadline {
                dl
            } else {
                let dl = now + Duration::from_millis(3000);
                rec.release_deadline = Some(dl);
                dl
            }
        } else {
            now + Duration::from_millis(3000)
        }
    }

    /// C1 by-id 閘門檢查。
    pub fn is_open_blocked(&self, by_id: &str) -> bool {
        if self.unconfirmed.contains(by_id) {
            return true;
        }
        if let Some(rec) = self.custody.get(by_id) {
            matches!(
                rec.phase,
                CustodyPhase::CloseFailed { .. }
                    | CustodyPhase::Closing { .. }
                    | CustodyPhase::BridgeDraining { .. }
                    | CustodyPhase::Opening { .. }
                    | CustodyPhase::Taken { .. }
                    | CustodyPhase::Bridged { .. }
            )
        } else {
            false
        }
    }

    pub fn get_custody(&self, by_id: &str) -> Option<&Custody> {
        self.custody.get(by_id)
    }

    pub fn get_custody_by_token(&self, token: u64) -> Option<&Custody> {
        self.custody.values().find(|c| c.token == token)
    }

    pub fn mark_bridge_draining(
        &mut self,
        actor: ActorId,
        token: u64,
        epoch: u64,
        stop_id: StopId,
    ) -> Result<Option<ReclaimWorker>, &'static str> {
        for rec in self.custody.values_mut() {
            if rec.token == token {
                let actor_owns_record = match &rec.owner {
                    CustodyOwner::Session { actor: owner } => *owner == actor,
                    _ => false,
                };
                if !actor_owns_record {
                    return Err("actor owner mismatch");
                }
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

    /// 處理指定 ActorId 的 ActorExited，鎖內取出受保管資源並產生具所有權的關閉／排空工作；其他 actor 的通知不予理會。
    pub fn handle_actor_exited(&mut self, actor: ActorId) -> Vec<ActorExitedJob> {
        let mut jobs = Vec::new();
        for (by_id, rec) in self.custody.iter_mut() {
            if let CustodyOwner::Session { actor: owner } = &rec.owner {
                if *owner == actor {
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
    pub gate: Arc<AdmissionGate>,
    /// 只供測試（test-fakes）：各 sid 實際送達（`try_send` 成功）的累計次數（驗證容量恢復後只送達一次）。正式路徑
    /// 不記帳，避免 session 移除後殘留（審查 P8-03333f7-3）。
    #[cfg(feature = "test-fakes")]
    delivered: HashMap<String, usize>,
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
            #[cfg(feature = "test-fakes")]
            delivered: HashMap::new(),
        }
    }

    /// 只供測試（test-fakes）：sid 累計實際送達次數。
    #[cfg(feature = "test-fakes")]
    pub fn delivered_count(&self, sid: &str) -> usize {
        self.delivered.get(sid).copied().unwrap_or(0)
    }

    pub fn obligation_count(&self) -> usize {
        self.obligations.len()
    }

    pub fn attempts(&self, sid: &str) -> usize {
        self.obligations.get(sid).map_or(0, |o| o.attempts)
    }

    pub fn add_obligation(&mut self, sid: &str, by_id: &str, real_path: &str, bind_gen: u64, actor: ActorId) {
        let snap = self.gate.snapshot();
        self.add_obligation_with_snapshot(sid, by_id, real_path, bind_gen, actor, snap);
    }

    pub fn add_obligation_at(
        &mut self,
        sid: &str,
        by_id: &str,
        real_path: &str,
        bind_gen: u64,
        actor: ActorId,
        now: Instant,
    ) {
        let snap = self.gate.snapshot_at(now);
        self.add_obligation_with_snapshot(sid, by_id, real_path, bind_gen, actor, snap);
    }

    /// 使用給定之閘門快照建立送達義務（保證時刻、凍結狀態與累積凍結時間一致）。
    pub fn add_obligation_with_snapshot(
        &mut self,
        sid: &str,
        by_id: &str,
        real_path: &str,
        bind_gen: u64,
        actor: ActorId,
        snap: GateSnapshot,
    ) {
        self.obligations.insert(sid.to_string(), DeliveryObligation {
            event: DeviceAttachedEvent {
                by_id: by_id.to_string(),
                real_path: real_path.to_string(),
            },
            bind_gen,
            actor,
            created_at: snap.now,
            frozen_at_create: snap.frozen_total,
            next_at: snap.now,
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

    pub fn cancel_obligations_for_actor(&mut self, actor: ActorId) {
        self.obligations.retain(|_, obligation| obligation.actor != actor);
    }

    pub fn rematerialize_obligation(&mut self, old_sid: &str, new_sid: &str, actor: ActorId) {
        if old_sid == new_sid {
            return;
        }
        if let Some(obligation) = self.obligations.remove(old_sid) {
            if obligation.actor == actor {
                self.obligations.insert(new_sid.to_string(), obligation);
            } else {
                self.obligations.insert(old_sid.to_string(), obligation);
            }
        }
    }

    /// 單步送達執行（以指定時間取樣快照）。
    pub fn step_delivery(
        &mut self,
        sid: &str,
        actor: ActorId,
        actor_tx: &Sender<ControlEvent>,
        now: Instant,
    ) -> DeliveryStepResult {
        let snap = self.gate.snapshot_at(now);
        self.step_delivery_with_snapshot(sid, actor, actor_tx, snap)
    }

    /// 單步送達執行（使用給定之一致快照，避免取樣時間差導致誤扣除未來凍結）。
    pub fn step_delivery_with_snapshot(
        &mut self,
        sid: &str,
        actor: ActorId,
        actor_tx: &Sender<ControlEvent>,
        snap: GateSnapshot,
    ) -> DeliveryStepResult {
        if self.obligations.get(sid).is_some_and(|obligation| obligation.actor != actor) {
            self.obligations.remove(sid);
            return DeliveryStepResult::Cancelled;
        }
        let ob = match self.obligations.get_mut(sid) {
            Some(o) => o,
            None => return DeliveryStepResult::NoObligation,
        };

        // 依 T14 檢查凍結狀態
        match snap.freeze_state {
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

        // 檢查 30 秒總時限（扣除截至取樣時刻之凍結區間）
        let total_elapsed = snap.now.saturating_duration_since(ob.created_at);
        let frozen_elapsed = snap.frozen_total.saturating_sub(ob.frozen_at_create);
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
                #[cfg(feature = "test-fakes")]
                {
                    *self.delivered.entry(sid.to_string()).or_insert(0) += 1;
                }
                DeliveryStepResult::Delivered
            }
            Err(TrySendError::Full(_)) => {
                ob.attempts += 1;
                ob.next_at = snap.now + ob.backoff;
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

/// 回收者命令與事件回呼介面。
pub trait ReaperCallback: Send + Sync + 'static {
    /// 執行命令並回報本次啟動、預期送回的 `WorkerStopped` 數量。
    fn handle_cmd(&self, cmd: ReaperCmd, event_tx: &Sender<ControlEvent>) -> usize;
    fn handle_worker_stopped(&self, receipt: WorkerStoppedReceipt);
}

/// 回收者執行緒：專職消費 DrainDataBarrier 與 WorkerStopped。
pub struct ReaperThread {
    stopped: Arc<AtomicBool>,
    allow_exit: Arc<AtomicBool>,
    wake: Sender<()>,
    command_rx: Receiver<ReaperCmd>,
    event_rx: Receiver<ControlEvent>,
    pending_receipts: Arc<std::sync::atomic::AtomicUsize>,
    active: Arc<AtomicBool>,
    handle: Option<std::thread::JoinHandle<()>>,
    detached: bool,
}

impl ReaperThread {
    pub fn start(
        reaper_cmd_rx: Receiver<ReaperCmd>,
        reaper_event_rx: Receiver<ControlEvent>,
        reaper_event_tx: Sender<ControlEvent>,
        callback: Arc<dyn ReaperCallback>,
    ) -> Self {
        let stopped = Arc::new(AtomicBool::new(false));
        let thread_stopped = stopped.clone();
        let allow_exit = Arc::new(AtomicBool::new(false));
        let thread_allow_exit = allow_exit.clone();
        let (wake, wake_rx) = crossbeam_channel::bounded(1);
        let command_state_rx = reaper_cmd_rx.clone();
        let event_state_rx = reaper_event_rx.clone();
        let pending_receipts = Arc::new(std::sync::atomic::AtomicUsize::new(0));
        let thread_pending_receipts = pending_receipts.clone();
        let active = Arc::new(AtomicBool::new(false));
        let thread_active = active.clone();

        let handle = std::thread::Builder::new()
            .name("serialwrap-reaper".into())
            .spawn(move || {
                let mut pending_receipts = 0usize;
                let mut stopping = false;
                loop {
                    stopping |= thread_stopped.load(Ordering::Acquire);
                    if stopping {
                        match reaper_cmd_rx.try_recv() {
                            Ok(cmd) => {
                                thread_active.store(true, Ordering::Release);
                                pending_receipts = pending_receipts
                                    .saturating_add(callback.handle_cmd(cmd, &reaper_event_tx));
                                thread_pending_receipts.store(pending_receipts, Ordering::Release);
                                thread_active.store(false, Ordering::Release);
                                continue;
                            }
                            Err(crossbeam_channel::TryRecvError::Empty)
                                if pending_receipts == 0
                                    && thread_allow_exit.load(Ordering::Acquire) =>
                            {
                                break;
                            }
                            Err(crossbeam_channel::TryRecvError::Disconnected)
                                if pending_receipts == 0 =>
                            {
                                break;
                            }
                            Err(_) => {}
                        }
                    }
                    crossbeam_channel::select! {
                        recv(wake_rx) -> _ => {}
                        recv(reaper_cmd_rx) -> msg => {
                            match msg {
                                Ok(cmd) => {
                                    thread_active.store(true, Ordering::Release);
                                    pending_receipts = pending_receipts
                                        .saturating_add(callback.handle_cmd(cmd, &reaper_event_tx));
                                    thread_pending_receipts.store(pending_receipts, Ordering::Release);
                                    thread_active.store(false, Ordering::Release);
                                }
                                Err(_) => stopping = true,
                            }
                        }
                        recv(reaper_event_rx) -> msg => {
                            match msg {
                                Ok(ControlEvent::DrainDataBarrier { expired, reply, .. }) => {
                                    // 不確認：設 expired=true 並立即丟棄 reply
                                    expired.store(true, Ordering::Release);
                                    drop(reply);
                                }
                                Ok(ControlEvent::WorkerStopped(receipt)) => {
                                    thread_active.store(true, Ordering::Release);
                                    callback.handle_worker_stopped(receipt);
                                    pending_receipts = pending_receipts.saturating_sub(1);
                                    thread_pending_receipts.store(pending_receipts, Ordering::Release);
                                    thread_active.store(false, Ordering::Release);
                                }
                                Ok(_) => {}
                                Err(_) => stopping = true,
                            }
                        }
                        default(Duration::from_millis(50)) => {}
                    }
                }
            })
            .expect("建立 reaper thread");

        Self {
            stopped,
            allow_exit,
            wake,
            command_rx: command_state_rx,
            event_rx: event_state_rx,
            pending_receipts,
            active,
            handle: Some(handle),
            detached: false,
        }
    }

    pub fn stop(&mut self) {
        self.request_stop();
        self.allow_exit();
        if let Some(h) = self.handle.take() {
            let _ = h.join();
        }
    }

    /// 開始停止並排空已受理命令；可先於共用期限的 join 等待發出。
    pub fn request_stop(&self) {
        self.stopped.store(true, Ordering::Release);
        let _ = self.wake.try_send(());
    }

    /// 允許回收者在命令與回條排空後退出；關機 Phase 1 前不得開啟此門。
    pub fn allow_exit(&self) {
        self.allow_exit.store(true, Ordering::Release);
        let _ = self.wake.try_send(());
    }

    /// 有其他生產者仍可能送交 ActorExited 時，放下 join 所有權並讓背景執行緒繼續收尾。
    pub fn detach_for_cleanup(&mut self) {
        self.request_stop();
        self.detached = true;
        self.handle.take();
    }

    #[cfg(feature = "test-fakes")]
    pub fn is_finished_for_test(&self) -> bool {
        self.handle
            .as_ref()
            .is_some_and(std::thread::JoinHandle::is_finished)
    }

    /// 回收者已無工作待做；供共用關機期限剛到但執行緒只差排程退出時判定。
    pub fn is_quiescent(&self) -> bool {
        self.command_rx.is_empty()
            && self.event_rx.is_empty()
            && self.pending_receipts.load(Ordering::Acquire) == 0
            && !self.active.load(Ordering::Acquire)
    }

    /// 在共用關機期限內等待回收者；逾時後 detach JoinHandle，讓回收執行緒繼續持有並完成收尾工作。
    pub fn stop_until(&mut self, deadline: Instant) -> bool {
        self.request_stop();
        self.allow_exit();
        let Some(handle) = self.handle.as_ref() else {
            return true;
        };
        while !handle.is_finished() {
            let now = Instant::now();
            if now >= deadline {
                self.handle.take();
                return self.is_quiescent();
            }
            std::thread::sleep(Duration::from_millis(1).min(deadline - now));
        }
        if let Some(handle) = self.handle.take() {
            let _ = handle.join();
        }
        true
    }
}

impl Drop for ReaperThread {
    fn drop(&mut self) {
        if !self.detached {
            self.stop();
        }
    }
}

/// 送達者回呼介面。
pub trait DelivererCallback: Send + Sync + 'static {
    fn step_delivery(&self);
}

/// 送達者背景執行緒：定期以退避策略執行 DeviceAttached 送達義務。
pub struct DelivererThread {
    stopped: Arc<AtomicBool>,
    handle: Option<std::thread::JoinHandle<()>>,
}

impl DelivererThread {
    pub fn start(callback: Arc<dyn DelivererCallback>) -> Self {
        let stopped = Arc::new(AtomicBool::new(false));
        let thread_stopped = stopped.clone();

        let handle = std::thread::Builder::new()
            .name("serialwrap-deliverer".into())
            .spawn(move || {
                while !thread_stopped.load(Ordering::Relaxed) {
                    callback.step_delivery();
                    std::thread::sleep(Duration::from_millis(20));
                }
            })
            .expect("建立 deliverer thread");

        Self {
            stopped,
            handle: Some(handle),
        }
    }

    pub fn stop(&mut self) {
        self.request_stop();
        if let Some(h) = self.handle.take() {
            let _ = h.join();
        }
    }

    /// 通知送達者停止；提供關機 Phase 1 與 session drain 並行收斂的入口。
    pub fn request_stop(&self) {
        self.stopped.store(true, Ordering::Relaxed);
    }

    /// 在共用關機期限內等待送達者；逾時後 detach JoinHandle，保留執行緒中的回呼所有權。
    pub fn stop_until(&mut self, deadline: Instant) -> bool {
        self.request_stop();
        let Some(handle) = self.handle.as_ref() else {
            return true;
        };
        while !handle.is_finished() {
            let now = Instant::now();
            if now >= deadline {
                self.handle.take();
                return false;
            }
            std::thread::sleep(Duration::from_millis(1).min(deadline - now));
        }
        if let Some(handle) = self.handle.take() {
            let _ = handle.join();
        }
        true
    }
}

impl Drop for DelivererThread {
    fn drop(&mut self) {
        self.stop();
    }
}
