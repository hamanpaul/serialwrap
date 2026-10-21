//! P5-T10／T12：UART reader、RX 視窗與 IoWorker（紅燈階段骨架）。
//!
//! 本檔只宣告 T11 會經由 `OpenedBridge`／`BridgeOpened` 使用的型別與公開簽章；
//! reader、RX 視窗與 IoWorker 行為由 Task 10／12 實作者補上（函式本體為 `s21_todo!`）。

use std::sync::Arc;

use crossbeam_channel::Sender;
use sw_pal::traits::{Clock, SerialHandle};

use crate::drain::StopId;
use crate::mailbox::{ControlEvent, DataEvent};
use crate::tx_writer::{Fence, TxWriterHandle};
use crate::wal::WalWriter;
use crate::Mono;

/// 哨兵 console（POSIX 內部 PTY）的寫入介面（arch §4.7；2.2 承接）。
pub trait ConsoleIo: Send {
    fn try_write(&mut self, data: &[u8]) -> sw_pal::traits::PalResult<usize>;
}

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
        s21_todo!("T10", "IoWorker::start")
    }

    pub fn tx(&self) -> &TxWriterHandle {
        s21_todo!("T9", "IoWorker::tx")
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
