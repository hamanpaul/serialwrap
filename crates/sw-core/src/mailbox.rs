//! P5-T11：Actor mailbox 事件與雙 lane 有界 `Mailbox`。
//!
//! 變體集依 plan 06 Task 11「Produces」凍結（T9 的 `ControlEvent::UartIoError` 形狀保留不變）。
//! 因部分 payload 含 `Sender`／`IoWorker` 等不可比較型別，`ControlEvent` 不再 derive `Clone`／`PartialEq`／`Eq`。
//! 控制事件依賴 RX watermark 時先暫存；獨立控制事件仍可越過暫存項目。

use std::collections::VecDeque;
use std::sync::Arc;
use std::sync::atomic::AtomicBool;
use std::time::Duration;

use crossbeam_channel::{bounded, Receiver, Sender};
use sw_pal::traits::{Clock, HolderReport, PalError};
use sw_proto::pycompat::PyErr;
use sw_proto::pyjson::PyDict;

use crate::drain::{StopId, WorkerStoppedReceipt};
use crate::human_console::human_tx::HumanTxTag;
use crate::human_console::{ConsoleScanSnapshot, ConsoleTransportKind, ScanSource};
use crate::login_fsm::LoginOutcome;
use crate::op_runner::OpOutcome;
use crate::session_core::{FlashReject, FlashReply, ProbeGrant, ProbeId, SessionRequest};
use crate::tx_writer::{OpId, TxReport};
use crate::uart_bridge::{IoWorker, RxShared};
use crate::Mono;

/// RPC 回覆通道（容量 1；接收端已離開時丟棄）。
pub type Reply = Sender<Result<PyDict, PyErr>>;

/// 資料 lane（RX chunk）。
#[derive(Debug)]
pub enum DataEvent {
    UartRxChunk {
        epoch: u64,
        rx_seq: u64,
        wal_seq: u64,
        bytes: Vec<u8>,
    },
    /// P6 增補 #7（07 D-02、D-17(5)）：console 輸入；`input_epoch` 為 hub 每輪 `mux.wait` 前取的快照。不帶 `rx_seq`。
    ConsoleInputBytes { client_id: String, client_lifetime: u64, input_epoch: u64, bytes: Vec<u8> },
    /// P6 增補 #7（07 D-17(3)）：與同 client 輸入走同一條 data lane，不得超車尚未處理的輸入。
    ConsoleDisconnected { client_id: String, client_lifetime: u64 },
}

/// 控制 lane 事件。
pub enum ControlEvent {
    Request {
        req: SessionRequest,
        reply: Reply,
    },
    DeviceAttached {
        by_id: String,
        real_path: String,
    },
    DeviceDetached {
        by_id: String,
        reason: DetachReason,
    },
    /// 裝置掃描後 by_id 不在快照（Python `_mark_missing_devices_locked`；審查 F60）：無 bridge 的非 RELEASED
    /// session 標 DETACHED、`last_error`（None 時）`DEVICE_NOT_FOUND`、`detached_at`（None 時）。
    DeviceMissing,
    /// 全域 background capture 上限的跨 session 淘汰（Python `_evict_background_locked`；審查 F28）。
    EvictCapture {
        cmd_id: String,
    },
    /// Console 擁有者更新目前附掛在 session 的 console 數量；供 release 回報實際關閉數。
    ConsoleCountChanged {
        count: usize,
    },
    BridgeOpened {
        epoch_hint: u64,
        result: Result<OpenedBridge, OpenFailure>,
    },
    UartIoError {
        epoch: u64,
        reason: String,
    },
    /// TX 每個 job 後送出（內部事件，阻塞送）；actor 用於 `last_tx_*`，BC-03 映射仍以 receipt 帳本為準。
    UartTxCompleted {
        epoch: u64,
        op_id: OpId,
        confirmed_bytes: usize,
        unknown: bool,
    },
    /// IoWorker 在 receipt 前等待同 epoch 已排入 data lane 的 RX 完成 actor dispatch。
    DrainDataBarrier {
        epoch: u64,
        rx_seq: u64,
        reply: Sender<()>,
        expired: Arc<AtomicBool>,
    },
    WorkerStopped(WorkerStoppedReceipt),
    WorkerDrainTimeout {
        epoch: u64,
        stop_id: StopId,
    },
    OpFinished {
        op_id: OpId,
        epoch: u64,
        outcome: OpOutcome,
        after_rx_seq: Option<u64>,
    },
    LoginFinished {
        epoch: u64,
        fsm_gen: u64,
        result: LoginOutcome,
        after_rx_seq: Option<u64>,
    },
    Timer(TimerEvent),
    StateCommitted {
        commit_id: u64,
        result: Result<(), PalError>,
    },
    Tick,
    Shutdown {
        reply: Reply,
    },
    /// P6 增補 #8：stream console（TCP／com0com）accept 或 hub 註冊完成。`close_gen`＝accept 當下 hub 已處理的
    /// close-all 次數；小於 core 已送出的 `ConsoleCloseAll` 次數者屬已關閉世代，core 拒收（sol 補審 High #2）。
    ConsoleConnected {
        client_id: String,
        client_lifetime: u64,
        label: String,
        vtty: String,
        kind: ConsoleTransportKind,
        close_gen: u64,
    },
    /// P6 增補 #8（07 D-09）：actor thread 之外取得的 `/proc` 掃描快照。
    ConsoleScan { scan: ConsoleScanSnapshot, source: ScanSource },
    /// P6 增補 #8（07 D-15(5)）：human TX pump 的結算。
    HumanTxCompleted { tag: HumanTxTag, report: TxReport },
    /// P8 Task 8：FlashCoordinator 以 `probe_id` 要求進入認線 probe 窗口（fence `Probe(probe_id)`、human 輸入丟棄）。
    FlashSyncProbeStart { probe_id: ProbeId, reply: FlashReply<Result<ProbeGrant, FlashReject>> },
    /// P8 Task 8：未匹配（逾時、取消、未命中）時解除**同一** probe 的窗口；其他 probe 的事件忽略。
    FlashProbeReleased { probe_id: ProbeId },
    /// P8 Task 8：同一 probe 命中 → FLASHING（同 handle 模式切換：不換 bridge、不 drain）。
    FlashSyncMatched { probe_id: ProbeId, reply: FlashReply<Result<(), FlashReject>> },
    /// P8 Task 8：flasher 離開（holder 歸零）→ exit flashing；舊 probe 的遲到事件只回覆、不改狀態。
    FlashHolderGone { probe_id: ProbeId, reply: FlashReply<()> },
    /// S2-PAR-wp510r R3（設計 §3.1.2 C4、§3.1.4 W3）：`Effect::ClaimForStop` 的立即回饋（executor 於 actor 執行緒在
    /// manager 鎖內處理該 token 的帳本紀錄後推入立即回饋佇列，先於下一個 mailbox 事件處理）。`outcome` 為處理結果：
    /// actor 依此決定是否等待該 token 的 `CustodyTerminal`。T14 分類：立即回饋，Open／Pending／Permanent 皆立即處理。
    CustodyClaimed { token: u64, outcome: CustodyClaimOutcome },
    /// S2-PAR-wp510r R3：session 擁有的帳本紀錄已有關閉終態（關閉工作者或開埠工作在提交點關閉）。`closed:false` 時
    /// 紀錄已轉 CloseFailed 並登記未確認關閉（D1 iii）。T14 分類：生命週期（Open 處理、Pending 延後、Permanent 處理）。
    CustodyTerminal { token: u64, closed: bool, error: Option<String> },
    /// S2-PAR-wp510r: 測試注入 actor panic
    #[cfg(feature = "test-fakes")]
    ArmPanic { count: u8 },
}

/// `ControlEvent::CustodyClaimed` 的處理結果。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CustodyClaimOutcome {
    /// 紀錄已轉 Closing（本次轉入或他人關閉中）：之後會有該 token 的 `CustodyTerminal`。
    Closing,
    /// 開埠工作持有中（`Taken`）：已寫入取消標記，開埠工作在提交點關閉並回報 `CustodyTerminal`。
    Cancelled,
    /// 紀錄已是 CloseFailed（D1 iii）。
    Failed,
    /// 紀錄已提交 adopt（`Bridged`）或已不存在：不會再有 `CustodyTerminal`，由 actor 自身的排空承擔。
    Gone,
}

/// S2-PAR-wp510r R3（編排者 2026-11-11 SPEC-GAP 2 裁決）：actor 受理 release／bind 等待後回給 RPC 執行緒的期限確認。
/// RPC 執行緒以 `deadline`（實時）為通知時點，到期仍未獲回覆即送帶同一身分的通知。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum WaitAck {
    /// release 等待：`stop_id` 為該停止，`deadline` 為首次 release 決定的絕對期限（記在被等待的對象上，之後的 release
    /// 沿用）。
    Release { stop_id: StopId, deadline: std::time::Instant },
    /// bind 等待：`id` 為該 BindWait，`deadline` 為 bind 請求 +3000 ms。
    Bind { id: u64, deadline: std::time::Instant },
}

/// 到期 timer（`Effect::Schedule` 的內容）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum TimerEvent {
    BootQuietExpired { epoch: u64, fsm_gen: u64 },
    LeaseExpiry { lease_id: String, deadline: Mono },
    DrainTimeout { epoch: u64, stop_id: StopId },
    /// P7 Task 9：force recover 第 `n` 次 READY 輪詢（`_force_recover` 的 `time.sleep(1.0)`；`id` 為等待者）。
    ForceRecoverPoll { id: u64, n: u32 },
    /// S2-PAR-wp510r R1: CustodyPoll 本地計時器（每 500 ms 補收或確認終態/offer）。
    CustodyPoll,
    /// S2-PAR-wp510r R3（設計 §3.1.6 BindWait）：`session.bind` 等待 retired drain 終態的期限（請求 +3000 ms）。
    /// T14 分類同其他 `Timer`（生命週期）：Open 處理、Pending 延後並於撤銷後重播、Permanent 處理（只回覆該 bind waiter，
    /// 不啟動新工作）。
    BindWaitTimeout { id: u64 },
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
    /// P6 Task 9 步驟 5：executor 於開埠成功時一併預建的哨兵 primary PTY（PTY 後端；`PtyPair` 留在 executor）。
    pub sentinel: Option<crate::session_core::PtyInfo>,
    /// TCP console 後端（SC-28 tcp／Windows）：executor 於開埠成功時一併綁定的 listener 端點（`"127.0.0.1:<port>"`）；
    /// PTY 後端或綁定失敗為 None（`uart_io.py` `_start_console_listener`）。
    pub console_endpoint: Option<String>,
    /// 本 bridge 專屬的 RX 視窗（Python 每次 attach 新建 `UARTBridge`，視窗／速率／總量歸零；審查 F51）。
    pub rx: Arc<RxShared>,
}

/// bridge 開啟失敗的分類。
#[derive(Debug)]
pub enum OpenFailure {
    Held(HolderReport),
    AccessDenied(HolderReport),
    NotFound,
    Other(PalError),
    /// 正式 bridge 接手前關閉 handle 的回條為 `closed:false`（Python `_finish_attach_op` 關閉失敗保留作業，
    /// `origin/main:sw_core/session_manager.py:4322-4332`）；內含原本要回報的開埠失敗。
    CloseUnconfirmed(Box<OpenFailure>),
    /// 開埠 gate 拒絕、未呼叫 `open_exclusive`：by_id 的關閉未確認（`session_core::UnconfirmedCloses`；Python
    /// `by_id in self._draining_by_ids` 時不開埠，`origin/main:sw_core/session_manager.py:3410-3417`、`:4847-4848`），
    /// 或交給本 session 的預開 handle 已被取消者轉為 Closing（Python 作業 `cancelled`，`_attach_by_id` 中止，`:4933-4944`）。
    CloseBlocked,
    /// S2-PAR-wp510r R3（設計 §3.1.4 W3 → W3′ 的 adopt 提交點）：開埠工作在提交點看到取消（release、bind、shutdown、
    /// 改指），關閉 handle 而不接手（handle 的關閉終態另以 `CustodyTerminal` 回報）；Python attach 見 `cancelled` 中止並經
    /// `_finish_attach_op` 關閉（`origin/main:sw_core/session_manager.py:5701-5712`、`:4320-4363`）。
    Cancelled,
}

/// `Mailbox::recv_next` 的輸出項目。
pub enum MailboxItem {
    Control(ControlEvent),
    Data(DataEvent),
}

/// 有界 mailbox：一般 control 與 safety control 各依 `control_cap` 限制，data 依 `data_cap` 限制；
/// 依賴 RX 的 control 另有 stash、pending 與有界延伸 queue。
pub struct Mailbox {
    control_tx: Sender<ControlEvent>,
    control_rx: Receiver<ControlEvent>,
    safety_tx: Sender<ControlEvent>,
    safety_rx: Receiver<ControlEvent>,
    data_tx: Sender<DataEvent>,
    data_rx: Receiver<DataEvent>,
    stash: VecDeque<ControlEvent>,
    pending_control: Option<ControlEvent>,
    deferred_control: VecDeque<ControlEvent>,
    deferred_control_cap: usize,
    drain_barriers: VecDeque<ControlEvent>,
    stopped: VecDeque<WorkerStoppedReceipt>,
    control_closed: bool,
    safety_closed: bool,
    data_closed: bool,
    #[cfg(feature = "test-fakes")]
    full_backlog_notifier: Option<Sender<()>>,
    #[cfg(feature = "test-fakes")]
    full_backlog_notified: bool,
    #[cfg(feature = "test-fakes")]
    witness: Witness,
}

/// 測試用（方法審閱 [High]，2026-10-29）：逐事件的 data 見證數——事件送入 mailbox 後、被取出前，mailbox 回傳了幾個
/// data item。control／safety lane 以影子佇列（FIFO 位置 ↔ 累計數）計算，`stopped` 中的 receipt 以平行佇列計算；
/// 依賴 RX 的暫存事件（stash／pending／deferred）不追蹤（回 `None`）。
///
/// OD-64（2026-10-29）：receipt 因同 epoch 依賴移入 `stopped`（依賴等待開始）時，另記當下 data lane 中已讀入的筆數，
/// 之後依 FIFO 逐筆核對其 epoch，得「依賴等待前已讀入 mailbox 的同 epoch data」筆數（`stopped_pre`，與 `stopped` 平行）。
#[cfg(feature = "test-fakes")]
#[derive(Default)]
struct Witness {
    control: VecDeque<u64>,
    safety: VecDeque<u64>,
    stopped: VecDeque<u64>,
    stopped_pre: VecDeque<PreDependency>,
    /// 本次 `recv_next` 自 lane 取出之事件的見證數（尚未回傳或移入 `stopped`）。
    current: Option<u64>,
    /// 本次 `recv_next` 取出之事件的依賴等待前已讀入筆數（lane 事件為 0）。
    current_pre: Option<u64>,
    /// 最近一次回傳的 control 項目之見證數（`take_control_witness` 取走）。
    last: Option<u64>,
    /// 最近一次回傳的 control 項目之依賴等待前已讀入筆數（`take_control_pre_dependency_data` 取走）。
    last_pre: Option<u64>,
}

/// OD-64：單一 `stopped` receipt 的依賴等待前已讀入 data 計數。
#[cfg(feature = "test-fakes")]
struct PreDependency {
    epoch: u64,
    /// 依賴等待開始時 data lane 中、尚未回傳的筆數（之後回傳的前 `remaining` 筆即依賴等待前已讀入者）。
    remaining: usize,
    /// 其中屬同 epoch 者。
    same_epoch: u64,
}

#[cfg(feature = "test-fakes")]
fn bump_first(queue: &mut VecDeque<u64>, len: usize) {
    while queue.len() < len {
        queue.push_back(0);
    }
    for w in queue.iter_mut().take(len) {
        *w += 1;
    }
}

impl Mailbox {
    pub fn new(control_cap: usize, data_cap: usize) -> Mailbox {
        let (control_tx, control_rx) = bounded(control_cap);
        let (safety_tx, safety_rx) = bounded(control_cap);
        let (data_tx, data_rx) = bounded(data_cap);
        Mailbox {
            control_tx,
            control_rx,
            safety_tx,
            safety_rx,
            data_tx,
            data_rx,
            stash: VecDeque::with_capacity(crate::resources::MAILBOX_STASH_MAX),
            pending_control: None,
            deferred_control: VecDeque::with_capacity(control_cap.max(1)),
            deferred_control_cap: control_cap.max(1),
            drain_barriers: VecDeque::new(),
            stopped: VecDeque::new(),
            control_closed: false,
            safety_closed: false,
            data_closed: false,
            #[cfg(feature = "test-fakes")]
            full_backlog_notifier: None,
            #[cfg(feature = "test-fakes")]
            full_backlog_notified: false,
            #[cfg(feature = "test-fakes")]
            witness: Witness::default(),
        }
    }

    /// 測試用（延遲探針）：最近一次 `recv_next` 回傳的 control 項目，在 mailbox 中等待期間被回傳的 data item 數。
    /// data 項目、actor 內部事件或依賴暫存事件回 `None`。
    #[cfg(feature = "test-fakes")]
    pub fn take_control_witness(&mut self) -> Option<u64> {
        self.witness.last.take()
    }

    /// 測試用（OD-64 延遲探針）：最近一次 `recv_next` 回傳的 control 項目，於依賴等待開始前已讀入 mailbox 的同 epoch
    /// data 筆數——因同 epoch 依賴移入 `stopped` 的 receipt 為該數；其他 lane 事件為 0；data 項目、actor 內部事件或依賴
    /// 暫存事件回 `None`。drain_receipt 的 l1 界限為「見證數 ≤ 此數＋1」。
    #[cfg(feature = "test-fakes")]
    pub fn take_control_pre_dependency_data(&mut self) -> Option<u64> {
        self.witness.last_pre.take()
    }

    /// 一般控制與依賴 RX watermark 的完成事件使用此有界 lane。
    pub fn control_sender(&self) -> Sender<ControlEvent> {
        self.control_tx.clone()
    }

    /// Release、WorkerStopped 等 safety control 使用獨立有界 lane，避免被依賴 RX 的 backlog 擋住。
    pub fn safety_sender(&self) -> Sender<ControlEvent> {
        self.safety_tx.clone()
    }

    /// 測試用：觀察 data lane 積壓，不取走事件。
    #[cfg(feature = "test-fakes")]
    pub fn data_receiver_for_test(&self) -> Receiver<DataEvent> {
        self.data_rx.clone()
    }

    /// 測試用：觀察 safety lane 積壓，不取走事件。
    #[cfg(feature = "test-fakes")]
    pub fn safety_receiver_for_test(&self) -> Receiver<ControlEvent> {
        self.safety_rx.clone()
    }

    /// 測試用：依賴暫存、pending 與 deferred queue 全滿時通知同步端。
    #[cfg(feature = "test-fakes")]
    pub fn notify_when_dependency_backlog_full(&mut self, notifier: Sender<()>) {
        self.full_backlog_notifier = Some(notifier);
    }

    /// 測試用（Task 12b 延遲探針）：safety／control lane 尚有未取出的事件。
    #[cfg(feature = "test-fakes")]
    pub fn control_waiting(&self) -> bool {
        !self.safety_rx.is_empty() || !self.control_rx.is_empty() || !self.drain_barriers.is_empty() || !self.stopped.is_empty()
    }

    /// 測試用（Task 12b 延遲探針）：data lane 目前積壓筆數。
    #[cfg(feature = "test-fakes")]
    pub fn data_backlog(&self) -> usize {
        self.data_rx.len()
    }

    pub fn data_sender(&self) -> Sender<DataEvent> {
        self.data_tx.clone()
    }

    /// `deadline` 以 `Mono` 表示；實作以不超過 50 ms 的接收輪詢並每次醒來重讀 clock。
    /// `current_epoch` 用於丟棄舊 epoch 依賴。
    pub fn recv_next(
        &mut self,
        processed: (u64, u64),
        current_epoch: Option<u64>,
        deadline: Option<Mono>,
        clock: &dyn Clock,
    ) -> Option<MailboxItem> {
        #[cfg(feature = "test-fakes")]
        {
            self.witness.current = None;
            self.witness.current_pre = None;
            self.witness.last = None;
            self.witness.last_pre = None;
        }
        let item = self.recv_next_inner(processed, current_epoch, deadline, clock);
        #[cfg(feature = "test-fakes")]
        match &item {
            Some(MailboxItem::Data(data)) => {
                // 此 data item 超越了目前仍在 lane／stopped 中等待的每個事件。
                bump_first(&mut self.witness.control, self.control_rx.len());
                bump_first(&mut self.witness.safety, self.safety_rx.len());
                for w in self.witness.stopped.iter_mut() {
                    *w += 1;
                }
                // OD-64：data lane 為 FIFO，依賴等待開始時已在 lane 中的筆數即其後最先回傳的那幾筆。
                // P6 console 事件（ConsoleInputBytes／ConsoleDisconnected）同走 data lane：佔 FIFO 位置，但不是同 epoch RX data。
                let rx_epoch = match data {
                    DataEvent::UartRxChunk { epoch, .. } => Some(*epoch),
                    _ => None,
                };
                for pre in self.witness.stopped_pre.iter_mut().filter(|p| p.remaining > 0) {
                    pre.remaining -= 1;
                    if rx_epoch == Some(pre.epoch) {
                        pre.same_epoch += 1;
                    }
                }
                self.witness.current = None;
                self.witness.current_pre = None;
            }
            Some(MailboxItem::Control(_)) => {
                self.witness.last = self.witness.current.take();
                self.witness.last_pre = self.witness.current_pre.take();
            }
            None => {
                self.witness.current = None;
                self.witness.current_pre = None;
            }
        }
        item
    }

    #[cfg(feature = "test-fakes")]
    fn note_lane_recv(&mut self, safety: bool) {
        let queue = if safety { &mut self.witness.safety } else { &mut self.witness.control };
        self.witness.current = Some(queue.pop_front().unwrap_or(0));
        self.witness.current_pre = Some(0);
    }

    /// receipt 因同 epoch 依賴移入 `stopped`：依賴等待由此開始，記下當下 data lane 已讀入的筆數（OD-64）。
    #[cfg(feature = "test-fakes")]
    fn note_stopped_push(&mut self, epoch: u64) {
        let w = self.witness.current.take().unwrap_or(0);
        self.witness.current_pre = None;
        self.witness.stopped.push_back(w);
        self.witness.stopped_pre.push_back(PreDependency {
            epoch,
            remaining: self.data_rx.len(),
            same_epoch: 0,
        });
    }

    fn recv_next_inner(
        &mut self,
        processed: (u64, u64),
        current_epoch: Option<u64>,
        deadline: Option<Mono>,
        clock: &dyn Clock,
    ) -> Option<MailboxItem> {
        loop {
            if let Some(index) = self
                .stash
                .iter()
                .position(|ev| dependency_ready(ev, processed, current_epoch))
            {
                return self.stash.remove(index).map(MailboxItem::Control);
            }

            if let Some(ev) = self.pending_control.take() {
                if dependency_ready(&ev, processed, current_epoch) {
                    return Some(MailboxItem::Control(ev));
                }
                if self.stash.len() < crate::resources::MAILBOX_STASH_MAX {
                    self.stash.push_back(ev);
                } else {
                    self.pending_control = Some(ev);
                    if let Ok(data) = self.data_rx.try_recv() {
                        return Some(MailboxItem::Data(data));
                    }
                }
            }

            if self.stash.len() < crate::resources::MAILBOX_STASH_MAX {
                if let Some(ev) = self.deferred_control.pop_front() {
                    if dependency_ready(&ev, processed, current_epoch) {
                        return Some(MailboxItem::Control(ev));
                    }
                    self.stash.push_back(ev);
                    continue;
                }
            }

            // OD-69：barrier 帶 reader join 時最後成功入列的 watermark；只等到該 RX 完成 dispatch，
            // 不必排空 reader 停止後仍可能持續收到的其他 data。actor 每次只取一筆，下一輪已更新 processed。
            if !self.drain_barriers.is_empty() {
                let (epoch, rx_seq, expired) = match self.drain_barriers.front() {
                    Some(ControlEvent::DrainDataBarrier {
                        epoch,
                        rx_seq,
                        expired,
                        ..
                    }) => (*epoch, *rx_seq, expired.load(std::sync::atomic::Ordering::Acquire)),
                    _ => unreachable!("drain_barriers 僅存放 DrainDataBarrier"),
                };
                if rx_seq == 0 || processed >= (epoch, rx_seq) || expired {
                    let barrier = self.drain_barriers.pop_front().expect("front 已確認存在");
                    #[cfg(feature = "test-fakes")]
                    {
                        self.witness.current = Some(0);
                        self.witness.current_pre = Some(0);
                    }
                    return Some(MailboxItem::Control(barrier));
                }
                if let Ok(data) = self.data_rx.try_recv() {
                    return Some(MailboxItem::Data(data));
                }
            }

            if let Some(receipt) = self.stopped.front() {
                if let Ok(data) = self.data_rx.try_recv() {
                    return Some(MailboxItem::Data(data));
                }
                if let Some(index) = self
                    .stash
                    .iter()
                    .position(|ev| dependency_epoch(ev) == Some(receipt.epoch))
                {
                    if let Some(ev) = self.stash.remove(index) {
                        return Some(MailboxItem::Control(ev));
                    }
                }
                if self
                    .pending_control
                    .as_ref()
                    .is_some_and(|ev| dependency_epoch(ev) == Some(receipt.epoch))
                {
                    if let Some(ev) = self.pending_control.take() {
                        return Some(MailboxItem::Control(ev));
                    }
                }
                if let Some(index) = self
                    .deferred_control
                    .iter()
                    .position(|ev| dependency_epoch(ev) == Some(receipt.epoch))
                {
                    if let Some(ev) = self.deferred_control.remove(index) {
                        return Some(MailboxItem::Control(ev));
                    }
                }
                if let Some(receipt) = self.stopped.pop_front() {
                    #[cfg(feature = "test-fakes")]
                    {
                        self.witness.current = self.witness.stopped.pop_front();
                        self.witness.current_pre =
                            self.witness.stopped_pre.pop_front().map(|p| p.same_epoch);
                    }
                    return Some(MailboxItem::Control(ControlEvent::WorkerStopped(receipt)));
                }
            }

            let now = clock.mono_ns();
            if deadline.is_some_and(|at| now >= at) {
                return None;
            }
            let poll = Duration::from_millis(50);
            let wait = deadline
                .map(|at| Duration::from_nanos(at.saturating_sub(now)).min(poll))
                .unwrap_or(poll);

            let control_rx = if self.control_closed {
                crossbeam_channel::never()
            } else {
                self.control_rx.clone()
            };
            let safety_rx = if self.safety_closed {
                crossbeam_channel::never()
            } else {
                self.safety_rx.clone()
            };
            let data_rx = if self.data_closed {
                crossbeam_channel::never()
            } else {
                self.data_rx.clone()
            };
            if self.pending_control.is_some()
                && self.stash.len() >= crate::resources::MAILBOX_STASH_MAX
                && self.deferred_control.len() >= self.deferred_control_cap
            {
                #[cfg(feature = "test-fakes")]
                if !self.full_backlog_notified {
                    if let Some(notifier) = &self.full_backlog_notifier {
                        if notifier.try_send(()).is_ok() {
                            self.full_backlog_notified = true;
                        }
                    }
                }
                crossbeam_channel::select_biased! {
                    recv(safety_rx) -> safety => match safety {
                        Ok(ev @ ControlEvent::DrainDataBarrier { .. }) => {
                            #[cfg(feature = "test-fakes")]
                            self.note_lane_recv(true);
                            self.drain_barriers.push_back(ev);
                        }
                        Ok(ControlEvent::WorkerStopped(receipt))
                            if self.has_dependency_epoch(receipt.epoch) =>
                        {
                            #[cfg(feature = "test-fakes")]
                            { self.note_lane_recv(true); self.note_stopped_push(receipt.epoch); }
                            self.stopped.push_back(receipt);
                        }
                        Ok(ev) => {
                            #[cfg(feature = "test-fakes")]
                            self.note_lane_recv(true);
                            return Some(MailboxItem::Control(ev));
                        }
                        Err(_) => {
                            self.safety_closed = true;
                            if self.control_closed && self.data_closed { return None; }
                        }
                    },
                    recv(data_rx) -> data => match data {
                        Ok(data) => return Some(MailboxItem::Data(data)),
                        Err(_) => {
                            self.data_closed = true;
                            if self.control_closed && self.safety_closed { return None; }
                        }
                    },
                    default(wait) => {}
                }
            } else {
                crossbeam_channel::select_biased! {
                    recv(safety_rx) -> safety => match safety {
                        Ok(ev @ ControlEvent::DrainDataBarrier { .. }) => {
                            #[cfg(feature = "test-fakes")]
                            self.note_lane_recv(true);
                            self.drain_barriers.push_back(ev);
                        }
                        Ok(ControlEvent::WorkerStopped(receipt))
                            if self.has_dependency_epoch(receipt.epoch) =>
                        {
                            #[cfg(feature = "test-fakes")]
                            { self.note_lane_recv(true); self.note_stopped_push(receipt.epoch); }
                            self.stopped.push_back(receipt);
                        }
                        Ok(ev) => {
                            #[cfg(feature = "test-fakes")]
                            self.note_lane_recv(true);
                            return Some(MailboxItem::Control(ev));
                        }
                        Err(_) => {
                            self.safety_closed = true;
                            if self.control_closed && self.data_closed { return None; }
                        }
                    },
                    recv(control_rx) -> control => match control {
                        Ok(ev @ ControlEvent::DrainDataBarrier { .. }) => {
                            #[cfg(feature = "test-fakes")]
                            self.note_lane_recv(false);
                            self.drain_barriers.push_back(ev);
                        }
                        Ok(ControlEvent::WorkerStopped(receipt))
                            if self.has_dependency_epoch(receipt.epoch) =>
                        {
                            #[cfg(feature = "test-fakes")]
                            { self.note_lane_recv(false); self.note_stopped_push(receipt.epoch); }
                            self.stopped.push_back(receipt);
                        }
                        Ok(ev) if dependency_waits(&ev, processed, current_epoch) => {
                            #[cfg(feature = "test-fakes")]
                            {
                                self.note_lane_recv(false);
                                self.witness.current = None; // 依賴暫存事件不追蹤
                                self.witness.current_pre = None;
                            }
                            if self.stash.len() < crate::resources::MAILBOX_STASH_MAX {
                                self.stash.push_back(ev);
                            } else if self.pending_control.is_none() {
                                self.pending_control = Some(ev);
                            } else {
                                self.deferred_control.push_back(ev);
                            }
                        }
                        Ok(ev) => {
                            #[cfg(feature = "test-fakes")]
                            self.note_lane_recv(false);
                            return Some(MailboxItem::Control(ev));
                        }
                        Err(_) => {
                            self.control_closed = true;
                            if self.data_closed && self.safety_closed {
                                return None;
                            }
                        }
                    },
                    recv(data_rx) -> data => match data {
                        Ok(data) => return Some(MailboxItem::Data(data)),
                        Err(_) => {
                            self.data_closed = true;
                            if self.control_closed && self.safety_closed {
                                return None;
                            }
                        }
                    },
                    default(wait) => {}
                }
            }
        }
    }

    fn has_dependency_epoch(&self, epoch: u64) -> bool {
        self.stash
            .iter()
            .any(|ev| dependency_epoch(ev) == Some(epoch))
            || self
                .pending_control
                .as_ref()
                .is_some_and(|ev| dependency_epoch(ev) == Some(epoch))
            || self
                .deferred_control
                .iter()
                .any(|ev| dependency_epoch(ev) == Some(epoch))
    }
}

fn dependency_epoch(ev: &ControlEvent) -> Option<u64> {
    match ev {
        ControlEvent::OpFinished {
            epoch,
            after_rx_seq: Some(_),
            ..
        }
        | ControlEvent::LoginFinished {
            epoch,
            after_rx_seq: Some(_),
            ..
        } => Some(*epoch),
        _ => None,
    }
}

fn dependency(ev: &ControlEvent) -> Option<(u64, u64)> {
    match ev {
        ControlEvent::OpFinished {
            epoch,
            after_rx_seq: Some(rx_seq),
            ..
        }
        | ControlEvent::LoginFinished {
            epoch,
            after_rx_seq: Some(rx_seq),
            ..
        } => Some((*epoch, *rx_seq)),
        _ => None,
    }
}

fn dependency_ready(ev: &ControlEvent, processed: (u64, u64), current_epoch: Option<u64>) -> bool {
    let Some((epoch, rx_seq)) = dependency(ev) else {
        return true;
    };
    rx_seq == 0 || current_epoch != Some(epoch) || processed >= (epoch, rx_seq)
}

fn dependency_waits(ev: &ControlEvent, processed: (u64, u64), current_epoch: Option<u64>) -> bool {
    dependency(ev).is_some() && !dependency_ready(ev, processed, current_epoch)
}
