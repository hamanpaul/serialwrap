//! 序列埠記憶體替身（arch §4.2；Slice 2.1 Task 1）。
//!
//! 本檔為 Task 1 骨架：型別與簽章已定，行為（script、gate、暫停點、TX 記錄）由 Task 1 實作補上。
//! 替身不得冒充原生 gate（OD-08、arch §9.1）。

use std::collections::{HashMap, VecDeque};
use std::sync::atomic::{AtomicBool, AtomicUsize};
use std::sync::{Arc, Condvar, Mutex};
use std::time::Duration;

use crate::traits::{DevicePath, OpenError, PalError, SerialBackend, SerialConfig, SerialHandle};
use crate::traits::{CancelToken, HolderReport, SubmitGate, TxResult, UartExclusive};

/// 寫入腳本（依序消耗；耗盡後恢復 `Accept(全部)`）。
#[derive(Debug, Clone)]
pub enum FakeWriteStep {
    /// 接受前 n bytes 後回 `Completed(累計)`。
    Accept(usize),
    /// 接受 n bytes 後回 `Completed(n)` 加 error。
    AcceptThenError(usize, PalError),
    /// 回 `TxOutcome::Unknown { confirmed }` 加 error。
    Unknown { confirmed: usize, error: PalError },
    /// 經 gate 提交後（已交 OS、in_flight 已設）於 gate 外阻塞直到 `CancelToken::cancel`，回 `Completed(accept_before_cancel)`。
    BlockUntilCancel { accept_before_cancel: usize },
    /// 經 gate 提交後於 gate 外阻塞直到 `FakeDevice::release_block`（模擬 D-state；不持 Fence 鎖）。
    BlockForever,
    /// 0 byte + error。
    Error(PalError),
    /// R0-F1 線性化測試用：`write_gated` 進入後、第一次 `gate.submit` 之前暫停（不持鎖）。
    PauseBeforeSubmit,
    /// R0-F1：gate 已重驗通過、`os_submit` 閉包內（持 Fence 鎖）暫停。
    PauseInsideSubmit,
    /// R0-F1：第一次 `gate.submit` 接受 n bytes，隨後回 WouldBlock 並於 gate 外暫停；`release_pause` 後經 gate 提交剩餘。
    AcceptThenWouldBlock(usize),
}

/// 裝置內部狀態（骨架）。
#[allow(dead_code)]
pub(crate) struct FakeDeviceInner {
    rx: Mutex<VecDeque<u8>>,
    tx: Mutex<Vec<u8>>,
    steps: Mutex<VecDeque<FakeWriteStep>>,
    cond: Condvar,
    open: AtomicBool,
    opens: AtomicUsize,
    closes: AtomicUsize,
    writes: AtomicUsize,
    blocked_release: AtomicBool,
    close_err: Mutex<Option<PalError>>,
    removed: AtomicBool,
}

#[allow(dead_code)] // Task 1 實作前尚未被讀取。
impl FakeDeviceInner {
    fn new() -> Self {
        Self {
            rx: Mutex::new(VecDeque::new()),
            tx: Mutex::new(Vec::new()),
            steps: Mutex::new(VecDeque::new()),
            cond: Condvar::new(),
            open: AtomicBool::new(false),
            opens: AtomicUsize::new(0),
            closes: AtomicUsize::new(0),
            writes: AtomicUsize::new(0),
            blocked_release: AtomicBool::new(false),
            close_err: Mutex::new(None),
            removed: AtomicBool::new(false),
        }
    }
}

/// 記憶體序列埠後端：以路徑鍵控的虛擬裝置表。
pub struct FakeSerialBackend {
    #[allow(dead_code)] // Task 1 實作前尚未被讀取。
    devices: Mutex<HashMap<String, Arc<FakeDeviceInner>>>,
}

impl FakeSerialBackend {
    pub fn new() -> Arc<FakeSerialBackend> {
        Arc::new(FakeSerialBackend {
            devices: Mutex::new(HashMap::new()),
        })
    }

    pub fn add_device(&self, _path: &str) -> FakeDevice {
        s21_todo!("T1", "FakeSerialBackend::add_device")
    }

    /// 下次 `open_exclusive` → `Held(HolderReport{pids, holder_unknown: pids.is_empty()})`。
    pub fn hold_externally(&self, _path: &str, _pids: Vec<u32>) {
        s21_todo!("T1", "FakeSerialBackend::hold_externally")
    }

    /// → `AccessDenied{holder: pids [] / holder_unknown true / Unsupported}`。
    pub fn deny_access(&self, _path: &str) {
        s21_todo!("T1", "FakeSerialBackend::deny_access")
    }

    /// → 之後 open 回 `NotFound`；已開 handle 的 read 回 `Err(kind=Io, "[Errno 5] Input/output error")`。
    pub fn remove_device(&self, _path: &str) {
        s21_todo!("T1", "FakeSerialBackend::remove_device")
    }
}

impl SerialBackend for FakeSerialBackend {
    fn open_exclusive(
        &self,
        _dev: &DevicePath,
        _cfg: &SerialConfig,
    ) -> Result<Box<dyn SerialHandle>, OpenError> {
        s21_todo!("T1", "FakeSerialBackend::open_exclusive")
    }
}

/// 單一虛擬裝置的操作面（與 `FakeSerialBackend` 共享狀態）。
#[derive(Clone)]
pub struct FakeDevice {
    #[allow(dead_code)] // Task 1 實作前尚未被讀取。
    inner: Arc<FakeDeviceInner>,
}

impl FakeDevice {
    /// 注入 RX bytes（reader 可讀）。
    pub fn push_rx(&self, _bytes: &[u8]) {
        s21_todo!("T1", "FakeDevice::push_rx")
    }

    /// 只含已「確認寫出」的 bytes；讀後清空。
    pub fn take_tx(&self) -> Vec<u8> {
        s21_todo!("T1", "FakeDevice::take_tx")
    }

    /// 依序消耗寫入腳本；耗盡後恢復 `Accept(全部)`。
    pub fn script_write(&self, _steps: Vec<FakeWriteStep>) {
        s21_todo!("T1", "FakeDevice::script_write")
    }

    pub fn release_block(&self) {
        s21_todo!("T1", "FakeDevice::release_block")
    }

    /// 之後 `close` 回 `closed:false, excl_cleared:false, error:Some(err)`。
    pub fn fail_close(&self, _err: PalError) {
        s21_todo!("T1", "FakeDevice::fail_close")
    }

    pub fn open_count(&self) -> usize {
        s21_todo!("T1", "FakeDevice::open_count")
    }

    pub fn close_count(&self) -> usize {
        s21_todo!("T1", "FakeDevice::close_count")
    }

    pub fn is_open(&self) -> bool {
        s21_todo!("T1", "FakeDevice::is_open")
    }

    /// `write_gated`／`write` 被呼叫的次數（TxWriter 出列快速拒絕的零 TX 斷言用）。
    pub fn write_calls(&self) -> usize {
        s21_todo!("T1", "FakeDevice::write_calls")
    }

    /// `os_submit` 閉包實際執行（bytes 交給假 OS）的次數；gate 撤銷不計。
    pub fn os_submits(&self) -> usize {
        s21_todo!("T1", "FakeDevice::os_submits")
    }

    /// 每次 `os_submit` 閉包內（持 Fence 鎖時）呼叫；鎖持有斷言用。
    pub fn on_os_submit(&self, _hook: Box<dyn Fn() + Send + Sync>) {
        s21_todo!("T1", "FakeDevice::on_os_submit")
    }

    /// 等 writer 抵達 `Pause*`／`AcceptThenWouldBlock` 的暫停點。
    pub fn wait_paused(&self, _timeout: Duration) -> bool {
        s21_todo!("T1", "FakeDevice::wait_paused")
    }

    pub fn release_pause(&self) {
        s21_todo!("T1", "FakeDevice::release_pause")
    }

    /// 已確認 TX 累積緩衝出現 `trigger` 時推一次 `reply`。
    pub fn on_tx_reply(&self, _trigger: &[u8], _reply: &[u8]) {
        s21_todo!("T1", "FakeDevice::on_tx_reply")
    }

    /// `pattern` 對每個已確認 TX 行比對；`reply` 取得命中的行（random nonce probe 用）。
    /// 注意：arch §1.3 規定 sw-pal 不依賴 sw-proto，`pattern` 的正則引擎需於 Task 1 實作時另行決定。
    pub fn on_tx_match(
        &self,
        _pattern: &str,
        _reply: Box<dyn Fn(&[u8]) -> Vec<u8> + Send + Sync>,
    ) {
        s21_todo!("T1", "FakeDevice::on_tx_match")
    }
}

/// 替身的 `UartExclusive`（Task 1 Produces）：`report` 由測試預先注入，`probe_holders` 回傳對應報告。
pub struct FakeUartExclusive {
    pub report: Mutex<HashMap<String, HolderReport>>,
}

impl UartExclusive for FakeUartExclusive {
    fn probe_holders(&self, _real_path: &str) -> HolderReport {
        s21_todo!("T1", "FakeUartExclusive::probe_holders")
    }
}

// `SubmitGate` 與 `TxResult` 於本骨架僅供 `write_gated` 簽章引用；寫入路徑由 Task 1 實作。
#[allow(dead_code)]
fn _signature_anchor(_g: &dyn SubmitGate, _c: &CancelToken) -> Option<TxResult> {
    None
}
