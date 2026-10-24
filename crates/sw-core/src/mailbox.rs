//! P5-T11：Actor mailbox 事件與有界 `Mailbox`（紅燈階段骨架）。
//!
//! 變體集依 plan 06 Task 11「Produces」凍結（T9 的 `ControlEvent::UartIoError` 形狀保留不變）。
//! 因部分 payload 含 `Sender`／`IoWorker` 等不可比較型別，`ControlEvent` 不再 derive `Clone`／`PartialEq`／`Eq`。
//! 函式本體為 `s21_todo!`，由 Task 11 實作者補上。

use crossbeam_channel::Sender;
use sw_pal::traits::{Clock, HolderReport, PalError};
use sw_proto::pycompat::PyErr;
use sw_proto::pyjson::PyDict;

use crate::drain::{StopId, WorkerStoppedReceipt};
use crate::human_console::human_tx::HumanTxTag;
use crate::human_console::{ConsoleScanSnapshot, ConsoleTransportKind, ScanSource};
use crate::login_fsm::LoginOutcome;
use crate::op_runner::OpOutcome;
use crate::session_core::SessionRequest;
use crate::tx_writer::{OpId, TxReport};
use crate::uart_bridge::IoWorker;
use crate::Mono;

/// RPC 回覆通道（容量 1；接收端已離開時丟棄）。
pub type Reply = Sender<Result<PyDict, PyErr>>;

/// 資料 lane（RX chunk）。
#[derive(Debug)]
pub enum DataEvent {
    UartRxChunk { epoch: u64, rx_seq: u64, wal_seq: u64, bytes: Vec<u8> },
    /// P6 增補 #7（07 D-02、D-17(5)）：console 輸入；`input_epoch` 為 hub 每輪 `mux.wait` 前取的快照。不帶 `rx_seq`。
    ConsoleInputBytes { client_id: String, client_lifetime: u64, input_epoch: u64, bytes: Vec<u8> },
    /// P6 增補 #7（07 D-17(3)）：與同 client 輸入走同一條 data lane，不得超車尚未處理的輸入。
    ConsoleDisconnected { client_id: String, client_lifetime: u64 },
}

/// 控制 lane 事件。
pub enum ControlEvent {
    Request { req: SessionRequest, reply: Reply },
    DeviceAttached { by_id: String, real_path: String },
    DeviceDetached { by_id: String, reason: DetachReason },
    BridgeOpened { epoch_hint: u64, result: Result<OpenedBridge, OpenFailure> },
    UartIoError { epoch: u64, reason: String },
    /// TX 每個 job 後送出（內部事件，阻塞送）；actor 用於 `last_tx_*`，BC-03 映射仍以 receipt 帳本為準。
    UartTxCompleted { epoch: u64, op_id: OpId, confirmed_bytes: usize, unknown: bool },
    WorkerStopped(WorkerStoppedReceipt),
    WorkerDrainTimeout { epoch: u64, stop_id: StopId },
    OpFinished { op_id: OpId, epoch: u64, outcome: OpOutcome, after_rx_seq: Option<u64> },
    LoginFinished { epoch: u64, fsm_gen: u64, result: LoginOutcome, after_rx_seq: Option<u64> },
    Timer(TimerEvent),
    StateCommitted { commit_id: u64, result: Result<(), PalError> },
    Tick,
    Shutdown { reply: Reply },
    /// P6 增補 #8：stream console（TCP／com0com）accept 或 hub 註冊完成。
    ConsoleConnected { client_id: String, client_lifetime: u64, label: String, vtty: String, kind: ConsoleTransportKind },
    /// P6 增補 #8（07 D-09）：actor thread 之外取得的 `/proc` 掃描快照。
    ConsoleScan { scan: ConsoleScanSnapshot, source: ScanSource },
    /// P6 增補 #8（07 D-15(5)）：human TX pump 的結算。
    HumanTxCompleted { tag: HumanTxTag, report: TxReport },
    /// P6 增補 #8（07 Task 10）：actor 層 envelope；`PtyPair` 由 executor 保管，不進 `SessionCore`。
    ConsoleAttachReq { label: Option<String>, pty: Option<sw_pal::PtyPair>, reply: Reply },
}

/// 到期 timer（`Effect::Schedule` 的內容）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum TimerEvent {
    BootQuietExpired { epoch: u64, fsm_gen: u64 },
    LeaseExpiry { lease_id: String, deadline: Mono },
    DrainTimeout { epoch: u64, stop_id: StopId },
}

/// `DeviceDetached` 原因（→ `DEVICE_REMOVED`／`DEVICE_REBOUND_REQUIRED`）。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DetachReason {
    Removed,
    Rebound,
}

/// bridge 開啟成功的結果（executor 在 `epochs.next()` 配號後送出）。
pub struct OpenedBridge {
    pub epoch: u64,
    pub worker: IoWorker,
    pub real_path: String,
}

/// bridge 開啟失敗的分類。
#[derive(Debug)]
pub enum OpenFailure {
    Held(HolderReport),
    AccessDenied(HolderReport),
    NotFound,
    Other(PalError),
}

/// `Mailbox::recv_next` 的輸出項目。
pub enum MailboxItem {
    Control(ControlEvent),
    Data(DataEvent),
}

/// 有界兩 lane mailbox（control 256、data 1024）＋依賴 RX 的 control stash（上限 16，`block_sender`）。
pub struct Mailbox {
    _stub: (),
}

impl Mailbox {
    pub fn new(_control_cap: usize, _data_cap: usize) -> Mailbox {
        s21_todo!("T11", "Mailbox::new")
    }

    pub fn control_sender(&self) -> Sender<ControlEvent> {
        s21_todo!("T11", "Mailbox::control_sender")
    }

    pub fn data_sender(&self) -> Sender<DataEvent> {
        s21_todo!("T11", "Mailbox::data_sender")
    }

    /// `deadline` 以 `Mono` 表示；實作以 `recv_timeout(min(deadline - now, 50 ms))` 迴圈並每次醒來重讀 clock，
    /// test-fakes 下另 select `FakeClock::subscribe` 使 advance 立即喚醒。`current_epoch` 用於丟棄舊 epoch 依賴。
    pub fn recv_next(
        &mut self,
        _processed: (u64, u64),
        _current_epoch: Option<u64>,
        _deadline: Option<Mono>,
        _clock: &dyn Clock,
    ) -> Option<MailboxItem> {
        s21_todo!("T11", "Mailbox::recv_next")
    }
}
