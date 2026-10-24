//! P5-T9：TxWriter＋Fence（送出前檢查、BC-03 三態、SC-07）。
//!
//! 紅燈階段骨架：型別與公開簽章依 `docs/rust-refactor/plans/06-slice21-daemon-core.md` Task 9「Produces」凍結，
//! 函式本體為 `unimplemented!`，由 Task 9 實作者補上。`EvidenceGap` 為 Task 12 的 Produces，因 `TxOpLedger` 先需要而定義於此。

use std::cell::Cell;
use std::collections::{HashSet, VecDeque};
use std::sync::{Arc, Mutex};
use std::thread::JoinHandle;

use indexmap::IndexMap;
use sw_pal::{CancelToken, PalError, SerialWriter, SubmitGate, SubmitRevoked, SubmitStep, TxResult};

use crate::mailbox::ControlEvent;
use crate::wal::{WalAppend, WalHealthSink, WalWriter};

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
    pub cancelled: HashSet<OpId>,
    pub in_flight: Option<InFlight>,
    /// P6 增補 #4（07 D-03）：human admission 關閉（前景操作期間）；HumanRaw 被擋為 `HumanAdmissionClosed`。
    pub human_admission_blocked: bool,
    /// P6 增補 #4（07 D-02）：與 `Generations.input_epoch` 同一次 `Fence::with` 內更新。
    pub input_epoch: u64,
    /// P6 增補 #4（07 D-22）：目前有效的回放授權（`ReplayHold.hold_id`）。
    pub replay_auth: Option<u64>,
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

#[allow(dead_code)]
pub struct Fence(Mutex<FenceState>);

impl Fence {
    pub fn new() -> Arc<Fence> {
        s21_todo!("T9", "Fence::new")
    }

    pub fn with<R>(&self, _f: impl FnOnce(&mut FenceState) -> R) -> R {
        s21_todo!("T9", "Fence::with")
    }

    pub fn admit(_st: &FenceState, _p: &TxPermit) -> Result<(), FenceReject> {
        s21_todo!("T9", "Fence::admit")
    }

    /// try_lock；鎖被持有時回 `None`（鎖持有斷言用）。
    pub fn try_with<R>(&self, _f: impl FnOnce(&mut FenceState) -> R) -> Option<R> {
        s21_todo!("T9", "Fence::try_with")
    }
}

impl WalHealthSink for Fence {
    fn set_wal_unhealthy(&self, _unhealthy: bool) {
        s21_todo!("T9", "Fence::set_wal_unhealthy")
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
        _os_submit: &mut dyn FnMut() -> SubmitStep,
    ) -> Result<SubmitStep, SubmitRevoked> {
        s21_todo!("T9", "FenceSubmitGate::submit")
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

#[allow(dead_code)]
#[derive(Clone)]
pub struct TxWriterHandle {
    /// bounded(64)；可被 OpCtx clone 持有。
    pub jobs: crossbeam_channel::Sender<TxJob>,
    settle: crossbeam_channel::Sender<OpId>,
}

impl TxWriterHandle {
    /// 非阻塞（actor executor 呼叫）：try_send；Full 時回 false 並 settle_dropped+=1。
    pub fn settle(&self, _op_id: OpId) -> bool {
        s21_todo!("T9", "TxWriterHandle::settle")
    }

    pub fn ledger_gauge(&self) -> TxLedgerGauge {
        s21_todo!("T9", "TxWriterHandle::ledger_gauge")
    }
}

/// 只有 IoWorker 持有；send 後 writer 不再接新 job。
#[allow(dead_code)]
pub struct TxWriterStop(crossbeam_channel::Sender<()>);

impl TxWriterStop {
    pub fn send(&self) {
        s21_todo!("T9", "TxWriterStop::send")
    }
}

/// 啟動 TX writer thread（唯一呼叫 `SerialWriter::write_gated` 的地方）。
pub fn spawn_tx_writer(
    _com: String,
    _writer: Box<dyn SerialWriter>,
    _cancel: CancelToken,
    _fence: Arc<Fence>,
    _wal: Arc<WalWriter>,
    _epoch: u64,
    _control: crossbeam_channel::Sender<ControlEvent>,
) -> (
    TxWriterHandle,
    TxWriterStop,
    JoinHandle<(Box<dyn SerialWriter>, TxLedger)>,
) {
    s21_todo!("T9", "spawn_tx_writer")
}
