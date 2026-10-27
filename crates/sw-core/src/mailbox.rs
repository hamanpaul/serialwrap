//! P5-T11：Actor mailbox 事件與雙 lane 有界 `Mailbox`。
//!
//! 變體集依 plan 06 Task 11「Produces」凍結（T9 的 `ControlEvent::UartIoError` 形狀保留不變）。
//! 因部分 payload 含 `Sender`／`IoWorker` 等不可比較型別，`ControlEvent` 不再 derive `Clone`／`PartialEq`／`Eq`。
//! 控制事件依賴 RX watermark 時先暫存；獨立控制事件仍可越過暫存項目。

use std::collections::VecDeque;
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
use crate::session_core::SessionRequest;
use crate::tx_writer::{OpId, TxReport};
use crate::uart_bridge::IoWorker;
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
    /// P6 增補 #8：stream console（TCP／com0com）accept 或 hub 註冊完成。
    ConsoleConnected { client_id: String, client_lifetime: u64, label: String, vtty: String, kind: ConsoleTransportKind },
    /// P6 增補 #8（07 D-09）：actor thread 之外取得的 `/proc` 掃描快照。
    ConsoleScan { scan: ConsoleScanSnapshot, source: ScanSource },
    /// P6 Task 9 步驟 5：executor 已為 `epoch` 建好哨兵 primary 的 PTY（`token` 對應 executor 暫存的 PtyPair）。
    PrimaryConsoleReady { epoch: u64, token: u64, slave_path: String, client_id: String },
    /// P6 增補 #8（07 D-15(5)）：human TX pump 的結算。
    HumanTxCompleted { tag: HumanTxTag, report: TxReport },
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
    stopped: VecDeque<WorkerStoppedReceipt>,
    control_closed: bool,
    safety_closed: bool,
    data_closed: bool,
    #[cfg(feature = "test-fakes")]
    full_backlog_notifier: Option<Sender<()>>,
    #[cfg(feature = "test-fakes")]
    full_backlog_notified: bool,
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
            stopped: VecDeque::new(),
            control_closed: false,
            safety_closed: false,
            data_closed: false,
            #[cfg(feature = "test-fakes")]
            full_backlog_notifier: None,
            #[cfg(feature = "test-fakes")]
            full_backlog_notified: false,
        }
    }

    /// 一般控制與依賴 RX watermark 的完成事件使用此有界 lane。
    pub fn control_sender(&self) -> Sender<ControlEvent> {
        self.control_tx.clone()
    }

    /// Release、WorkerStopped 等 safety control 使用獨立有界 lane，避免被依賴 RX 的 backlog 擋住。
    pub fn safety_sender(&self) -> Sender<ControlEvent> {
        self.safety_tx.clone()
    }

    /// 測試用：依賴暫存、pending 與 deferred queue 全滿時通知同步端。
    #[cfg(feature = "test-fakes")]
    pub fn notify_when_dependency_backlog_full(&mut self, notifier: Sender<()>) {
        self.full_backlog_notifier = Some(notifier);
    }

    /// 測試用（Task 12b 延遲探針）：safety／control lane 尚有未取出的事件。
    #[cfg(feature = "test-fakes")]
    pub fn control_waiting(&self) -> bool {
        !self.safety_rx.is_empty() || !self.control_rx.is_empty()
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
                        Ok(ControlEvent::WorkerStopped(receipt))
                            if self.has_dependency_epoch(receipt.epoch) =>
                        {
                            self.stopped.push_back(receipt);
                        }
                        Ok(ev) => return Some(MailboxItem::Control(ev)),
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
                        Ok(ControlEvent::WorkerStopped(receipt))
                            if self.has_dependency_epoch(receipt.epoch) =>
                        {
                            self.stopped.push_back(receipt);
                        }
                        Ok(ev) => return Some(MailboxItem::Control(ev)),
                        Err(_) => {
                            self.safety_closed = true;
                            if self.control_closed && self.data_closed { return None; }
                        }
                    },
                    recv(control_rx) -> control => match control {
                        Ok(ControlEvent::WorkerStopped(receipt))
                            if self.has_dependency_epoch(receipt.epoch) =>
                        {
                            self.stopped.push_back(receipt);
                        }
                        Ok(ev) if dependency_waits(&ev, processed, current_epoch) => {
                            if self.stash.len() < crate::resources::MAILBOX_STASH_MAX {
                                self.stash.push_back(ev);
                            } else if self.pending_control.is_none() {
                                self.pending_control = Some(ev);
                            } else {
                                self.deferred_control.push_back(ev);
                            }
                        }
                        Ok(ev) => return Some(MailboxItem::Control(ev)),
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
