//! P5-T9：TxWriter＋Fence（送出前檢查、BC-03 三態、SC-07）。
//!
//! `EvidenceGap` 為 Task 12 的型別，因 `TxOpLedger` 先需要而定義於此。

use std::cell::Cell;
use std::collections::VecDeque;
use std::sync::atomic::{AtomicBool, AtomicU64, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex, TryLockError};
use std::thread::JoinHandle;

use crossbeam_channel::{Receiver, Sender};
use indexmap::IndexMap;
use sw_pal::{
    CancelToken, PalError, PalErrorKind, SerialWriter, SubmitGate, SubmitRevoked, SubmitStep,
    TxOutcome, TxResult,
};
use sw_proto::pyjson::{PyDict, PyInt, PyValue};

use crate::mailbox::ControlEvent;
use crate::wal::{WalAppend, WalAppendReq, WalHealthSink, WalWriter};

/// 未結算 op 上限；滿時新 op 一律不提交（見 saturation）。
pub const TX_LEDGER_LIVE_MAX: usize = 256;
/// 已結算 op 遺留的 WAL 證據缺口上限；溢位合併最舊者進 gaps_overflow。
pub const TX_LEDGER_UNRESOLVED_GAPS_MAX: usize = 256;
/// 單一 op 的 wal_gaps 上限；溢位合併進該 op 的 wal_gaps_overflow。
pub const TX_OP_WAL_GAPS_MAX: usize = 16;
/// settle channel 容量。
pub const TX_SETTLE_QUEUE_MAX: usize = 256;

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub struct OpId(pub u64);

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum TxClass {
    Flash,
    FlashProbe,
    LeaseOwner,
    HumanRaw,
    HumanReplay,
    LoginProbe,
    ReadyReconfirm,
    Command,
    FileTransfer,
    SystemRecoverKey,
    SelfTestProbe,
    InteractiveOpenCmd,
}

#[derive(Clone, Debug)]
pub struct TxPermit {
    pub op_id: OpId,
    pub epoch: u64,
    pub owner_gen: u64,
    pub fsm_gen: u64,
    pub class: TxClass,
    pub source: String,
    pub cmd_id: Option<String>,
    pub log: bool,
    /// P6 增補 #3（07 D-03）：只有 HumanRaw／HumanReplay 為 `Some`（擷取當下的 `input_epoch`），06 既有建構點一律 `None`。
    pub input_epoch: Option<u64>,
    /// P6 增補 #3（07 D-22）：只有 HumanReplay 為 `Some(hold_id)`，其餘一律 `None`。
    pub replay_hold: Option<u64>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum FlashMode {
    Off,
    Probe(u64),
    Flashing,
}

#[derive(Debug)]
pub struct InFlight {
    pub op_id: OpId,
    /// 已知交給 OS 的 byte 數；`Pending` 未提供長度時以 0 表示未知。
    pub len: usize,
}

#[derive(Debug)]
pub struct FenceState {
    pub active_epoch: Option<u64>,
    pub owner_gen: u64,
    pub fsm_gen: u64,
    pub frozen: bool,
    pub quiet_active: bool,
    pub reconfirm_pending: bool,
    pub flash_mode: FlashMode,
    pub wal_unhealthy: bool,
    pub cancelled: CancelledOps,
    pub in_flight: Option<InFlight>,
    /// P6 增補 #4（07 D-03）：human admission 關閉（前景操作期間）；HumanRaw 被擋為 `HumanAdmissionClosed`。
    pub human_admission_blocked: bool,
    /// P6 增補 #4（07 D-02）：與 `Generations.input_epoch` 同一次 `Fence::with` 內更新。
    pub input_epoch: u64,
    /// P6 增補 #4（07 D-22）：目前有效的回放授權（`ReplayHold.hold_id`）。
    pub replay_auth: Option<u64>,
    /// 前景 reboot 命令的 op（#55）：其 Command TX 已提交（confirmed>0）或結果不明時，TxWriter 在記錄結果的同一次
    /// `Fence::with` 內設 `quiet_active`／`reconfirm_pending` 並清除本欄——對齊 Python `_handle_reboot_command` 在
    /// `_write_lock` 內 arm，下一個 TX job 的出列檢查前 quiet 已生效（審查 F14）。actor 之後只同步 `QuietState`／世代。
    pub reboot_arm_op: Option<OpId>,
}

/// 撤銷 op 的 tombstone 上限（Python `uart_io._TX_ACCOUNT_MAX`；parity-amendments-2026-10-26 #38）。
pub const CANCELLED_OPS_MAX: usize = 512;

/// 已撤銷的 op（Python `UARTBridge._revoked_ops` 的 `OrderedDict`）：上限 [`CANCELLED_OPS_MAX`]、FIFO 淘汰最舊；
/// 重複撤銷既有 id 時移到最新（`pop` 後重新插入）。
#[derive(Debug, Default)]
pub struct CancelledOps(indexmap::IndexSet<OpId>);

impl CancelledOps {
    /// 登記撤銷（Python `_revoke_ops_locked` 的單一 id 步驟）。
    pub fn insert(&mut self, op: OpId) {
        self.0.shift_remove(&op);
        self.0.insert(op);
        while self.0.len() > CANCELLED_OPS_MAX {
            self.0.shift_remove_index(0);
        }
    }

    pub fn contains(&self, op: &OpId) -> bool {
        self.0.contains(op)
    }

    pub fn len(&self) -> usize {
        self.0.len()
    }

    pub fn is_empty(&self) -> bool {
        self.0.is_empty()
    }

    pub fn clear(&mut self) {
        self.0.clear();
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum FenceReject {
    StaleEpoch,
    Frozen,
    Quiet,
    ReconfirmPending,
    WalUnhealthy,
    Flash,
    OwnerMismatch,
    FsmMismatch,
    Cancelled,
    /// P6 增補 #4（07 D-03）：HumanRaw 遇 `human_admission_blocked`。
    HumanAdmissionClosed,
    /// P6 增補 #4（07 D-03）：HumanRaw／HumanReplay 的 `input_epoch` 不等於現值。
    InputStale,
    /// P6 增補 #4（07 D-22）：HumanReplay 的 `replay_hold` 不等於 `replay_auth`（已被撤銷）。
    ReplayRevoked,
}

impl FenceState {
    /// 撤銷 tombstone 數（測試觀察上限用）。
    pub fn cancelled_len(&self) -> usize {
        self.cancelled.len()
    }
}

pub struct Fence(Mutex<FenceState>);

impl Fence {
    pub fn new() -> Arc<Fence> {
        Arc::new(Fence(Mutex::new(FenceState {
            active_epoch: None,
            owner_gen: 0,
            fsm_gen: 0,
            frozen: false,
            quiet_active: false,
            reconfirm_pending: false,
            flash_mode: FlashMode::Off,
            wal_unhealthy: false,
            cancelled: CancelledOps::default(),
            in_flight: None,
            human_admission_blocked: false,
            input_epoch: 0,
            replay_auth: None,
            reboot_arm_op: None,
        })))
    }

    /// 撤銷 op（Python `UARTBridge.revoke_ops`）：之後以這些 op 提交的 TX 一律 `Cancelled`、零 TX；
    /// 已在 gate 外等待的寫入，下一次 `gate.submit` 重驗即拒絕尾段（#52）。
    pub fn revoke_ops(&self, ops: &[OpId]) {
        self.with(|state| {
            for op in ops {
                state.cancelled.insert(*op);
            }
        });
    }

    pub fn with<R>(&self, f: impl FnOnce(&mut FenceState) -> R) -> R {
        let mut state = self
            .0
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        f(&mut state)
    }

    pub fn admit(st: &FenceState, p: &TxPermit) -> Result<(), FenceReject> {
        if st.frozen {
            return Err(FenceReject::Frozen);
        }
        if st.active_epoch != Some(p.epoch) {
            return Err(FenceReject::StaleEpoch);
        }
        if st.cancelled.contains(&p.op_id) {
            return Err(FenceReject::Cancelled);
        }

        match (p.class, st.flash_mode) {
            (TxClass::Flash, FlashMode::Flashing) => {}
            // TxPermit 尚無獨立 probe_id；Slice 2.1 暫不放行 FlashProbe，不能以 op_id 代替。
            (TxClass::FlashProbe, FlashMode::Probe(_)) => return Err(FenceReject::Flash),
            (TxClass::Flash | TxClass::FlashProbe, _) => return Err(FenceReject::Flash),
            (_, FlashMode::Off) => {}
            (_, FlashMode::Probe(_) | FlashMode::Flashing) => return Err(FenceReject::Flash),
        }

        match p.class {
            TxClass::Command | TxClass::FileTransfer => {
                if st.quiet_active {
                    return Err(FenceReject::Quiet);
                }
                if st.reconfirm_pending {
                    return Err(FenceReject::ReconfirmPending);
                }
            }
            TxClass::LoginProbe
            | TxClass::ReadyReconfirm
            | TxClass::SystemRecoverKey
            | TxClass::SelfTestProbe => {
                if st.quiet_active {
                    return Err(FenceReject::Quiet);
                }
                if st.wal_unhealthy {
                    return Err(FenceReject::WalUnhealthy);
                }
            }
            _ => {}
        }

        match p.class {
            // 07 D-03：HumanRaw 依序 input_epoch → owner_gen → admission。
            TxClass::HumanRaw => {
                if p.input_epoch != Some(st.input_epoch) {
                    return Err(FenceReject::InputStale);
                }
                if p.owner_gen != st.owner_gen {
                    return Err(FenceReject::OwnerMismatch);
                }
                if st.human_admission_blocked {
                    return Err(FenceReject::HumanAdmissionClosed);
                }
            }
            // 07 D-03／D-22：HumanReplay 只比對 input_epoch 與專屬回放授權，不比對 owner_gen／admission。
            TxClass::HumanReplay => {
                if p.input_epoch != Some(st.input_epoch) {
                    return Err(FenceReject::InputStale);
                }
                if p.replay_hold.is_none() || p.replay_hold != st.replay_auth {
                    return Err(FenceReject::ReplayRevoked);
                }
            }
            TxClass::LeaseOwner | TxClass::InteractiveOpenCmd if p.owner_gen != st.owner_gen => {
                return Err(FenceReject::OwnerMismatch);
            }
            TxClass::LoginProbe | TxClass::ReadyReconfirm if p.fsm_gen != st.fsm_gen => {
                return Err(FenceReject::FsmMismatch);
            }
            _ => {}
        }

        Ok(())
    }

    /// try_lock；鎖被持有時回 `None`（鎖持有斷言用）。
    pub fn try_with<R>(&self, f: impl FnOnce(&mut FenceState) -> R) -> Option<R> {
        match self.0.try_lock() {
            Ok(mut state) => Some(f(&mut state)),
            Err(TryLockError::Poisoned(poisoned)) => {
                let mut state = poisoned.into_inner();
                Some(f(&mut state))
            }
            Err(TryLockError::WouldBlock) => None,
        }
    }
}

impl WalHealthSink for Fence {
    fn set_wal_unhealthy(&self, unhealthy: bool) {
        self.with(|state| state.wal_unhealthy = unhealthy);
    }
}

/// OS 提交的唯一序列化點（arch §4.2／§5.4 第 3 點，R0-F1）。
pub struct FenceSubmitGate<'a> {
    pub fence: &'a Fence,
    pub permit: &'a TxPermit,
    pub last_reject: Cell<Option<FenceReject>>,
}

impl SubmitGate for FenceSubmitGate<'_> {
    fn submit(
        &self,
        os_submit: &mut dyn FnMut() -> SubmitStep,
    ) -> Result<SubmitStep, SubmitRevoked> {
        let mut state = self
            .fence
            .0
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        if let Err(reason) = Fence::admit(&state, self.permit) {
            self.last_reject.set(Some(reason));
            return Err(SubmitRevoked);
        }

        let step = os_submit();
        let in_flight_len = match &step {
            SubmitStep::Accepted(len) if *len > 0 => Some(*len),
            // PAL 的 Pending 表示 OS 已接手；提交 API 不回報其長度。
            SubmitStep::Pending => Some(0),
            _ => None,
        };
        if let Some(len) = in_flight_len {
            state.in_flight = Some(InFlight {
                op_id: self.permit.op_id,
                len,
            });
        }
        Ok(step)
    }
}

pub struct TxJob {
    pub permit: TxPermit,
    pub payload: Vec<u8>,
    pub reply: crossbeam_channel::Sender<TxReport>,
}

#[derive(Debug, Clone)]
pub struct TxReport {
    pub op_id: OpId,
    pub epoch: u64,
    pub result: TxResult,
    /// TX WAL 寫入結果（seq／ok／loss_flag）。
    pub wal: Option<WalAppend>,
    pub reject: Option<FenceReject>,
}

/// 證據缺口（Task 12 的 Produces；T9 的帳本先行使用）。
#[derive(Debug, Clone)]
pub struct EvidenceGap {
    pub op_id: Option<OpId>,
    pub dir: &'static str,
    pub len: usize,
    pub error: PalError,
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct GapOverflow {
    pub gaps: u64,
    pub bytes: u64,
    pub first_op: Option<OpId>,
    pub last_op: Option<OpId>,
}

#[derive(Debug, Clone, Default)]
pub struct TxOpLedger {
    pub confirmed_bytes: usize,
    pub unknown: bool,
    pub last_error: Option<PalError>,
    pub wal_ok: bool,
    /// 已寫出但 TX WAL 失敗；≤ TX_OP_WAL_GAPS_MAX。
    pub wal_gaps: Vec<EvidenceGap>,
    pub wal_gaps_overflow: GapOverflow,
    pub reply_lost: bool,
    pub not_submitted: usize,
}

/// epoch 範圍的 TX 帳本（R1-F8：只保留未結算 op）。
#[derive(Debug, Clone, Default)]
pub struct TxLedger {
    pub epoch: u64,
    /// 只含未結算 op，≤ TX_LEDGER_LIVE_MAX。
    pub ops: IndexMap<OpId, TxOpLedger>,
    /// 已結算 op 的 wal_gaps 移入此處，≤ TX_LEDGER_UNRESOLVED_GAPS_MAX，FIFO 合併。
    pub unresolved_gaps: VecDeque<EvidenceGap>,
    /// 被合併的缺口計數與 bytes 總和（證據不靜默丟棄，規格 §5.4）。
    pub gaps_overflow: GapOverflow,
    pub settled_total: u64,
    pub saturated: bool,
}

/// writer thread 每次變更帳本後發布；供測試與 daemon status 診斷讀取（不經 writer thread）。
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct TxLedgerGauge {
    pub live_ops: usize,
    pub unresolved_gaps: usize,
    pub gaps_overflow: u64,
    pub settled_total: u64,
    pub settle_dropped: u64,
    pub saturated: bool,
}

#[derive(Default)]
struct LedgerGaugeCells {
    live_ops: AtomicUsize,
    unresolved_gaps: AtomicUsize,
    gaps_overflow: AtomicU64,
    settled_total: AtomicU64,
    settle_dropped: AtomicU64,
    saturated: AtomicBool,
    /// P5-T14：writer thread 已結束收尾（之後送入的 job 不會再被回覆；crossbeam array channel 在仍有 Sender 時不丟棄緩衝訊息）。
    closed: AtomicBool,
}

impl LedgerGaugeCells {
    fn load(&self) -> TxLedgerGauge {
        TxLedgerGauge {
            live_ops: self.live_ops.load(Ordering::Acquire),
            unresolved_gaps: self.unresolved_gaps.load(Ordering::Acquire),
            gaps_overflow: self.gaps_overflow.load(Ordering::Acquire),
            settled_total: self.settled_total.load(Ordering::Acquire),
            settle_dropped: self.settle_dropped.load(Ordering::Acquire),
            saturated: self.saturated.load(Ordering::Acquire),
        }
    }

    fn publish(&self, ledger: &TxLedger) {
        self.live_ops.store(ledger.ops.len(), Ordering::Release);
        self.unresolved_gaps
            .store(ledger.unresolved_gaps.len(), Ordering::Release);
        self.gaps_overflow
            .store(ledger.gaps_overflow.gaps, Ordering::Release);
        self.settled_total
            .store(ledger.settled_total, Ordering::Release);
        self.saturated.store(ledger.saturated, Ordering::Release);
    }
}

#[derive(Clone)]
pub struct TxWriterHandle {
    /// bounded(64)；可由 OpCtx 複製持有，writer 結束後送出會因接收端關閉而失敗。
    pub jobs: Sender<TxJob>,
    settle: Sender<OpId>,
    gauge: Arc<LedgerGaugeCells>,
}

impl TxWriterHandle {
    /// 非阻塞（actor executor 呼叫）：try_send；Full 時回 false 並 settle_dropped+=1。
    pub fn settle(&self, op_id: OpId) -> bool {
        match self.settle.try_send(op_id) {
            Ok(()) => true,
            Err(crossbeam_channel::TrySendError::Full(_)) => {
                self.gauge.settle_dropped.fetch_add(1, Ordering::AcqRel);
                false
            }
            Err(crossbeam_channel::TrySendError::Disconnected(_)) => false,
        }
    }

    pub fn ledger_gauge(&self) -> TxLedgerGauge {
        self.gauge.load()
    }

    /// P5-T14：等待某筆 job 的 `TxReport`；writer 已結束（stop 後才排入、未被回覆）時回 `None`，不會永久阻塞。
    pub fn wait_report(&self, report: &crossbeam_channel::Receiver<TxReport>) -> Option<TxReport> {
        loop {
            match report.recv_timeout(std::time::Duration::from_millis(20)) {
                Ok(report) => return Some(report),
                Err(crossbeam_channel::RecvTimeoutError::Disconnected) => return None,
                Err(crossbeam_channel::RecvTimeoutError::Timeout) => {
                    if self.gauge.closed.load(Ordering::Acquire) {
                        return report.try_recv().ok();
                    }
                }
            }
        }
    }
}

/// 只有 IoWorker 持有；send 後 writer 不再接新 job。
pub struct TxWriterStop(crossbeam_channel::Sender<()>);

impl TxWriterStop {
    pub fn send(&self) {
        let _ = self.0.try_send(());
    }
}

struct TxWriter {
    com: String,
    writer: Box<dyn SerialWriter>,
    cancel: CancelToken,
    fence: Arc<Fence>,
    wal: Arc<WalWriter>,
    epoch: u64,
    control: Sender<ControlEvent>,
    jobs: Receiver<TxJob>,
    settle: Receiver<OpId>,
    stop: Receiver<()>,
    gauge: Arc<LedgerGaugeCells>,
}

/// 0 byte 即中止的寫入（`Completed(0)`、無 fence 拒絕原因，PAL 於提交前檢查到 cancel）重驗授權後的收斂：
/// fence 已拒絕（drain 先凍結再 cancel）→ `NotSubmitted`＋該原因，對齊 Python 提交點的 `_tx_frozen` 檢查
/// （`TxIncompleteError("not_submitted", 0, N, "draining")`）；否則原樣保留（審查 F92：self-test 與 release 競態下
/// 曾回 `tx incomplete: completed 0/23 [Errno 125] Operation canceled`，與 golden `not_submitted … draining` 不符）。
pub fn reclassify_unsubmitted(
    result: TxResult,
    reject: Option<FenceReject>,
    recheck: Result<(), FenceReject>,
) -> (TxResult, Option<FenceReject>) {
    match (&result.outcome, reject, recheck) {
        (TxOutcome::Completed(0), None, Err(reason)) => (
            TxResult {
                outcome: TxOutcome::NotSubmitted,
                error: None,
            },
            Some(reason),
        ),
        _ => (result, reject),
    }
}

impl TxWriter {
    fn publish_gauge(&self, ledger: &TxLedger) {
        self.gauge.publish(ledger);
    }

    fn wal_append_error(&self, append: &WalAppend) -> Option<PalError> {
        append.loss_flag.then(|| {
            let message = self
                .wal
                .health()
                .last_write_error
                .unwrap_or_else(|| format!("WAL append {} failed", append.seq));
            PalError::plain(PalErrorKind::Io, message)
        })
    }

    fn run(mut self) -> (Box<dyn SerialWriter>, TxLedger) {
        let mut ledger = TxLedger {
            epoch: self.epoch,
            ..TxLedger::default()
        };
        loop {
            let job = crossbeam_channel::select_biased! {
                recv(self.stop) -> _ => break,
                recv(self.settle) -> settled => match settled {
                    Ok(op_id) => {
                        ledger.settle(op_id);
                        self.publish_gauge(&ledger);
                        continue;
                    }
                    Err(_) => break,
                },
                recv(self.jobs) -> queued => match queued {
                    Ok(job) => job,
                    Err(_) => break,
                },
            };

            if !ledger.ops.contains_key(&job.permit.op_id) && ledger.ops.len() >= TX_LEDGER_LIVE_MAX
            {
                if !ledger.saturated {
                    ledger.saturated = true;
                    let _ = self.control.send(ControlEvent::UartIoError {
                        epoch: self.epoch,
                        reason: "TX_LEDGER_SATURATED".into(),
                    });
                }
                let _ = job.reply.send(TxReport {
                    op_id: job.permit.op_id,
                    epoch: self.epoch,
                    result: TxResult {
                        outcome: TxOutcome::NotSubmitted,
                        error: None,
                    },
                    wal: None,
                    reject: Some(FenceReject::Frozen),
                });
                self.publish_gauge(&ledger);
                continue;
            }

            // 出列檢查用來快速拒絕；真正授權仍由每次 OS 提交的 gate 在 Fence 鎖內重驗。
            let precheck = self.fence.with(|state| Fence::admit(state, &job.permit));
            let (result, reject) = match precheck {
                Err(reason) => (
                    TxResult {
                        outcome: TxOutcome::NotSubmitted,
                        error: None,
                    },
                    Some(reason),
                ),
                Ok(()) => {
                    let gate = FenceSubmitGate {
                        fence: &self.fence,
                        permit: &job.permit,
                        last_reject: Cell::new(None),
                    };
                    let result = self.writer.write_gated(&job.payload, &self.cancel, &gate);
                    // 0 byte 即被 cancel（drain 先凍結 fence 再 cancel）時，重驗授權並以 fence 原因收斂。
                    let recheck = self.fence.with(|state| Fence::admit(state, &job.permit));
                    let (result, reject) =
                        reclassify_unsubmitted(result, gate.last_reject.get(), recheck);
                    gate.last_reject.set(reject);
                    let submitted = confirmed_bytes(&result.outcome) > 0
                        || matches!(result.outcome, TxOutcome::Unknown { .. });
                    self.fence.with(|state| {
                        if state.in_flight.as_ref().map(|flight| flight.op_id)
                            == Some(job.permit.op_id)
                        {
                            state.in_flight = None;
                        }
                        if submitted
                            && job.permit.class == TxClass::Command
                            && state.reboot_arm_op == Some(job.permit.op_id)
                        {
                            state.reboot_arm_op = None;
                            state.quiet_active = true;
                            state.reconfirm_pending = true;
                        }
                    });
                    (result, gate.last_reject.get())
                }
            };

            let confirmed = confirmed_bytes(&result.outcome);
            let tx_wal = if job.permit.log && confirmed > 0 {
                let confirmed_payload_len = confirmed.min(job.payload.len());
                Some(self.wal.append(WalAppendReq {
                    com: &self.com,
                    dir: "TX",
                    source: &job.permit.source,
                    payload: &job.payload[..confirmed_payload_len],
                    cmd_id: job.permit.cmd_id.as_deref(),
                    loss_flag: false,
                    meta: None,
                }))
            } else {
                None
            };
            let unknown_meta_wal = if job.permit.log {
                if let TxOutcome::Unknown { confirmed } = &result.outcome {
                    let requested = job.payload.len();
                    let confirmed = (*confirmed).min(requested);
                    let error = result
                        .error
                        .as_ref()
                        .map(|error| error.py_message.as_str())
                        .filter(|error| !error.is_empty())
                        .unwrap_or("unknown");
                    let mut meta = PyDict::new();
                    meta.insert("event", PyValue::from("tx_outcome_unknown"));
                    meta.insert(
                        "requested_bytes",
                        PyValue::Int(PyInt::Small(requested as i128)),
                    );
                    meta.insert(
                        "confirmed_bytes",
                        PyValue::Int(PyInt::Small(confirmed as i128)),
                    );
                    meta.insert(
                        "unknown_range",
                        PyValue::from(vec![
                            PyValue::Int(PyInt::Small(confirmed as i128)),
                            PyValue::Int(PyInt::Small(requested as i128)),
                        ]),
                    );
                    meta.insert("error", PyValue::from(error));
                    Some(self.wal.append(WalAppendReq {
                        com: &self.com,
                        dir: "META",
                        source: &job.permit.source,
                        payload: b"",
                        cmd_id: job.permit.cmd_id.as_deref(),
                        loss_flag: false,
                        meta: Some(meta),
                    }))
                } else {
                    None
                }
            } else {
                None
            };

            let op_id = job.permit.op_id;
            let op_ledger = ledger.ops.entry(op_id).or_default();
            op_ledger.record(&result);
            if let Some(append) = tx_wal.as_ref() {
                op_ledger.record_wal_append(
                    op_id,
                    "TX",
                    confirmed.min(job.payload.len()),
                    append,
                    self.wal_append_error(append),
                );
            }
            if let Some(append) = unknown_meta_wal.as_ref() {
                op_ledger.record_wal_append(
                    op_id,
                    "META",
                    0,
                    append,
                    self.wal_append_error(append),
                );
            }

            let _ = self.control.send(ControlEvent::UartTxCompleted {
                epoch: self.epoch,
                op_id,
                confirmed_bytes: confirmed,
                unknown: matches!(result.outcome, TxOutcome::Unknown { .. }),
            });
            let report = TxReport {
                op_id,
                epoch: self.epoch,
                result,
                wal: tx_wal,
                reject,
            };
            if job.reply.send(report).is_err() {
                op_ledger.reply_lost = true;
            }
            self.publish_gauge(&ledger);
        }

        // stop 前已抵達的結算先套用，再把尚在佇列的 job 以 Frozen 收斂。
        for op_id in self.settle.try_iter() {
            ledger.settle(op_id);
        }
        // P5-T14：先標記 closed，再收斂佇列；之後才送入的 job 由 `wait_report` 依 closed 旗標收尾。
        self.gauge.closed.store(true, Ordering::Release);
        for job in self.jobs.try_iter() {
            let op_id = job.permit.op_id;
            let known = ledger.ops.contains_key(&op_id);
            if known || ledger.ops.len() < TX_LEDGER_LIVE_MAX {
                ledger.ops.entry(op_id).or_default().not_submitted += 1;
            }
            if job
                .reply
                .send(TxReport {
                    op_id,
                    epoch: self.epoch,
                    result: TxResult {
                        outcome: TxOutcome::NotSubmitted,
                        error: None,
                    },
                    wal: None,
                    reject: Some(FenceReject::Frozen),
                })
                .is_err()
            {
                if let Some(op_ledger) = ledger.ops.get_mut(&op_id) {
                    op_ledger.reply_lost = true;
                }
            }
        }
        self.publish_gauge(&ledger);
        (self.writer, ledger)
    }
}

impl TxOpLedger {
    fn record(&mut self, result: &TxResult) {
        match &result.outcome {
            TxOutcome::NotSubmitted => self.not_submitted = self.not_submitted.saturating_add(1),
            TxOutcome::Completed(bytes) => {
                self.confirmed_bytes = self.confirmed_bytes.saturating_add(*bytes);
            }
            TxOutcome::Unknown { confirmed } => {
                self.confirmed_bytes = self.confirmed_bytes.saturating_add(*confirmed);
                self.unknown = true;
            }
        }
        if let Some(error) = &result.error {
            self.last_error = Some(error.clone());
        }
    }

    fn record_wal_append(
        &mut self,
        op_id: OpId,
        dir: &'static str,
        len: usize,
        append: &WalAppend,
        error: Option<PalError>,
    ) {
        self.wal_ok = (self.wal_ok || !append.loss_flag)
            && self.wal_gaps.is_empty()
            && self.wal_gaps_overflow.gaps == 0;
        if append.loss_flag {
            let gap = EvidenceGap {
                op_id: Some(op_id),
                dir,
                len,
                error: error
                    .unwrap_or_else(|| PalError::plain(PalErrorKind::Io, "WAL append failed")),
            };
            if self.wal_gaps.len() < TX_OP_WAL_GAPS_MAX {
                self.wal_gaps.push(gap);
            } else {
                self.wal_gaps_overflow.absorb_gap(&gap);
            }
            self.wal_ok = false;
        }
    }
}

impl TxLedger {
    /// 結算後移轉缺口，保留最新項目並將淘汰證據彙總。
    fn settle(&mut self, op_id: OpId) {
        let Some(op) = self.ops.shift_remove(&op_id) else {
            return;
        };
        self.settled_total = self.settled_total.saturating_add(1);
        self.gaps_overflow.absorb(&op.wal_gaps_overflow);
        for gap in op.wal_gaps {
            if self.unresolved_gaps.len() == TX_LEDGER_UNRESOLVED_GAPS_MAX {
                if let Some(removed) = self.unresolved_gaps.pop_front() {
                    self.gaps_overflow.absorb_gap(&removed);
                }
            }
            self.unresolved_gaps.push_back(gap);
        }
    }
}

impl GapOverflow {
    fn absorb_gap(&mut self, gap: &EvidenceGap) {
        self.gaps = self.gaps.saturating_add(1);
        self.bytes = self.bytes.saturating_add(gap.len as u64);
        if self.first_op.is_none() {
            self.first_op = gap.op_id;
        }
        if gap.op_id.is_some() {
            self.last_op = gap.op_id;
        }
    }

    fn absorb(&mut self, other: &GapOverflow) {
        if other.gaps == 0 {
            return;
        }
        self.gaps = self.gaps.saturating_add(other.gaps);
        self.bytes = self.bytes.saturating_add(other.bytes);
        if self.first_op.is_none() {
            self.first_op = other.first_op;
        }
        if other.last_op.is_some() {
            self.last_op = other.last_op;
        }
    }
}

fn confirmed_bytes(outcome: &TxOutcome) -> usize {
    match outcome {
        TxOutcome::NotSubmitted => 0,
        TxOutcome::Completed(bytes) => *bytes,
        TxOutcome::Unknown { confirmed } => *confirmed,
    }
}

/// 啟動 TX writer thread（唯一呼叫 `SerialWriter::write_gated` 的地方）。
pub fn spawn_tx_writer(
    com: String,
    writer: Box<dyn SerialWriter>,
    cancel: CancelToken,
    fence: Arc<Fence>,
    wal: Arc<WalWriter>,
    epoch: u64,
    control: Sender<ControlEvent>,
) -> (
    TxWriterHandle,
    TxWriterStop,
    JoinHandle<(Box<dyn SerialWriter>, TxLedger)>,
) {
    let (job_tx, job_rx) = crossbeam_channel::bounded(64);
    let (settle_tx, settle_rx) = crossbeam_channel::bounded(TX_SETTLE_QUEUE_MAX);
    let (stop_tx, stop_rx) = crossbeam_channel::bounded(1);
    let gauge = Arc::new(LedgerGaugeCells::default());
    let handle = TxWriterHandle {
        jobs: job_tx,
        settle: settle_tx,
        gauge: gauge.clone(),
    };
    let stop = TxWriterStop(stop_tx);
    let worker = TxWriter {
        com,
        writer,
        cancel,
        fence,
        wal,
        epoch,
        control,
        jobs: job_rx,
        settle: settle_rx,
        stop: stop_rx,
        gauge,
    };
    let join = std::thread::spawn(move || worker.run());
    (handle, stop, join)
}
