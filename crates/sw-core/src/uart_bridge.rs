//! P5-T10／T12：UART reader、RX 視窗與 IoWorker（紅燈階段骨架）。
//!
//! 本檔只宣告 T11 會經由 `OpenedBridge`／`BridgeOpened` 使用的型別與公開簽章；
//! reader、RX 視窗與 IoWorker 行為由 Task 10／12 實作者補上（函式本體為 `s21_todo!`）。

use std::sync::atomic::AtomicBool;
use std::sync::Arc;
use std::time::Duration;

use crossbeam_channel::Sender;
use sw_pal::traits::{Clock, PalError, SerialHandle, SerialReader};
use sw_proto::pyre::PyRegex;

use crate::drain::StopId;
use crate::mailbox::{ControlEvent, DataEvent};
use crate::tx_writer::{Fence, TxWriterHandle};
use crate::wal::WalWriter;
use crate::Mono;

/// 哨兵 console（POSIX 內部 PTY）的 I/O 介面：與 PAL 的 `ConsoleIo`（arch §4.7）為同一型別。
pub use sw_pal::traits::ConsoleIo;

/// RX 視窗（絕對偏移；跨 epoch 的 watermark 以 `(epoch, rx_seq)` 表示）。
pub struct RxShared {
    _stub: (),
}

impl RxShared {
    pub fn new(_max_chars: usize, _created_mono: u64) -> Arc<RxShared> {
        s21_todo!("T10", "RxShared::new")
    }

    pub fn append(&self, _data: &[u8], _now: Mono, _epoch: u64, _rx_seq: u64) {
        s21_todo!("T10", "RxShared::append")
    }

    pub fn watermark(&self) -> (u64, u64) {
        s21_todo!("T10", "RxShared::watermark")
    }

    pub fn clear(&self) {
        s21_todo!("T10", "RxShared::clear")
    }

    pub fn snapshot_len(&self) -> u64 {
        s21_todo!("T10", "RxShared::snapshot_len")
    }

    pub fn text_from(&self, _from: u64) -> String {
        s21_todo!("T10", "RxShared::text_from")
    }

    pub fn tail(&self, _max_chars: usize) -> String {
        s21_todo!("T10", "RxShared::tail")
    }

    pub fn rx_stats(&self, _now: Mono) -> (u64, u64) {
        s21_todo!("T10", "RxShared::rx_stats")
    }

    pub fn total_bytes(&self) -> u64 {
        s21_todo!("T10", "RxShared::total_bytes")
    }

    pub fn idle_s(&self, _now: Mono) -> f64 {
        s21_todo!("T10", "RxShared::idle_s")
    }

    pub fn last_rx_mono(&self) -> u64 {
        s21_todo!("T10", "RxShared::last_rx_mono")
    }

    pub fn trimmed(&self) -> u64 {
        s21_todo!("T10", "RxShared::trimmed")
    }

    /// 以 condvar 等待 `re` 命中（非輪詢）；逾時回 `None`。
    pub fn wait_for_regex(&self, _re: &PyRegex, _timeout: Duration, _clock: &dyn Clock) -> Option<RxMatch> {
        s21_todo!("T10", "RxShared::wait_for_regex")
    }

    /// 同 `wait_for_regex`，但只比對絕對偏移 `from` 之後的文字。
    pub fn wait_for_regex_from(
        &self,
        _re: &PyRegex,
        _from: u64,
        _timeout: Duration,
        _clock: &dyn Clock,
    ) -> Option<RxMatch> {
        s21_todo!("T10", "RxShared::wait_for_regex_from")
    }
}

/// 命中當下（同一把鎖）的 watermark；`OpFinished`／`LoginFinished` 的 `after_rx_seq` 一律取此值。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct RxMatch {
    pub epoch: u64,
    pub rx_seq: u64,
}

/// reader thread 的依賴（Task 10 Produces）。
pub struct ReaderCtx {
    pub com: String,
    pub epoch: u64,
    pub wal: Arc<WalWriter>,
    pub rx: Arc<RxShared>,
    pub sentinel: Option<Box<dyn ConsoleIo>>,
    pub data: Sender<crate::mailbox::DataEvent>,
    pub control: Sender<crate::mailbox::ControlEvent>,
    pub clock: Arc<dyn Clock>,
}

/// reader thread 結束時交回的資源（讀取錯誤與 reader 本體，供 IoWorker 收尾）。
pub struct ReaderExit {
    pub reader: Box<dyn SerialReader>,
    pub sentinel: Option<Box<dyn ConsoleIo>>,
    pub error: Option<PalError>,
    pub rx_seq: u64,
}

/// 啟動 reader thread（Task 10）：讀取 → WAL → RX 視窗 → data lane；`stop` 為協作式停止旗標。
pub fn spawn_reader(
    _reader: Box<dyn SerialReader>,
    _ctx: ReaderCtx,
    _stop: Arc<AtomicBool>,
) -> std::thread::JoinHandle<ReaderExit> {
    s21_todo!("T10", "spawn_reader")
}

/// `IoWorker::start` 的參數（reader＋唯一 TxWriter 的依賴）。
pub struct IoWorkerArgs {
    pub com: String,
    pub epoch: u64,
    pub wal: Arc<WalWriter>,
    pub rx: Arc<RxShared>,
    pub fence: Arc<Fence>,
    pub sentinel: Option<Box<dyn ConsoleIo>>,
    pub data: Sender<DataEvent>,
    pub control: Sender<ControlEvent>,
    pub clock: Arc<dyn Clock>,
}

/// 單一 bridge 的 I/O worker（reader thread＋TxWriter thread）。
pub struct IoWorker {
    _stub: (),
}

impl IoWorker {
    pub fn start(_handle: Box<dyn SerialHandle>, _args: IoWorkerArgs) -> IoWorker {
        s21_todo!("T12", "IoWorker::start")
    }

    pub fn tx(&self) -> &TxWriterHandle {
        s21_todo!("T12", "IoWorker::tx")
    }

    /// reader stop flag＋TxWriterStop::send＋cancel.cancel()＋waker.wake()。
    pub fn begin_stop(&self) {
        s21_todo!("T12", "IoWorker::begin_stop")
    }

    /// thread：join writer（取 TxLedger）→ join reader → close → `WorkerStopped`。
    pub fn finish_async(self, _stop_id: StopId, _control: Sender<ControlEvent>) {
        s21_todo!("T12", "IoWorker::finish_async")
    }
}

// ── P7 Task 0（08-slice23-login-events.md）凍結的新增公開項；本體由標示的 Task 實作 ──

/// login run 的取消旗標（SC-30）：`Arc<AtomicBool>`＋喚醒所屬 `RxShared` 的等待者；不得使用 PAL `CancelToken`。
#[derive(Clone)]
pub struct RunCancel {
    _flag: Arc<AtomicBool>,
}

impl RunCancel {
    pub fn new(_rx: &Arc<RxShared>) -> Self {
        todo!("P7 Task 7")
    }
    pub fn trigger(&self) {
        todo!("P7 Task 7")
    }
    pub fn is_triggered(&self) -> bool {
        todo!("P7 Task 7")
    }
}

/// `wait_for_regex_cancellable` 的結果。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum WaitOutcome {
    Matched,
    TimedOut,
    Cancelled,
}

impl RxShared {
    pub fn wait_for_regex_cancellable(
        &self,
        _re: &PyRegex,
        _from: Option<u64>,
        _timeout: Duration,
        _clock: &dyn Clock,
        _cancel: &RunCancel,
    ) -> WaitOutcome {
        todo!("P7 Task 7")
    }
    /// 最後一個 append 的 `rx_seq`（`LoginFinished.after_rx_seq` 來源）。
    pub fn last_rx_seq(&self) -> u64 {
        todo!("P7 Task 7")
    }
}

/// reader 在 WAL append 後依序呼叫的 RX 觀察者（DF-15）；不得阻塞於跨 thread 等待、不得回呼 SessionManager。
/// `ReaderCtx.observers` 欄位隨 Task 16-wire 接線時加入（Task 0 不改 06 `ReaderCtx`）。
pub trait RxObserver: Send + Sync {
    fn on_rx(&self, com: &str, data: &[u8], wal_seq: u64);
}
