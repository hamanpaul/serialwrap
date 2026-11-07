//! P8 Task 9：flash 認線協調（Python `service.py` `_BridgeProbe`、`_flash_rx_observer`、`_on_flash_open`、
//! `_watch_flasher_disconnect` 的組裝）。
//!
//! 兩層停止旗標（R2-F5）：
//! - [`EndpointShutdown`]：端點停止旗標，整個 daemon 生命週期一個，只由 `FlashEndpoint::stop`／`Drop`（經
//!   [`FlashCoordinator::request_shutdown`]）設定；端點 thread 迴圈只看它（Python `flash_endpoint.py` 的 `self._stop`）。
//! - [`FlashRunCancel`]：單次 flash 的取消旗標，`on_flash_open` 進入時新建（Python `service.py` 每次 flash 的局部
//!   `threading.Event`），detection probe、pump、holder watcher 共用；`is_cancelled()` 同時反映 `EndpointShutdown`，
//!   holder watcher 結束只 `cancel()` 本次 run，絕不設 `EndpointShutdown`。
//!
//! 真機三坑（CLAUDE.md「MCU flash 真機驗證手法」）：端點未 bridge 時 master 零寫入；認線 probe 只送 flasher 自己的
//! sync bytes（`first`，為空才退回 pattern probe），命中後把 MCU 回應整段回放給 flasher 一次、不重送 `first`；flasher
//! 斷線以 holder-probe（armed 後歸零）偵測。實體 MCU UART 全程由既有 bridge handle 持有（不 close／reopen）。

use std::collections::HashMap;
use std::sync::atomic::{AtomicBool, AtomicU64, AtomicU8};
use std::sync::{Arc, Mutex, Weak};
use std::time::Duration;

use crossbeam_channel::{Receiver, Sender};
use sw_pal::console_pty::{ConsoleIo, ConsoleTransportFactory, ConsoleWriteHalf, PtySlaveRef};
use sw_pal::traits::{Clock, IoWaker, UartExclusive};
use sw_proto::pyjson::PyDict;

use crate::mcu_patterns::McuPatternRegistry;
use crate::session_manager::SessionManager;

use super::detect::DetectStatus;

/// 認線 probe 等 ACK 的輪詢間隔（Python `time.sleep(0.02)`；經 [`Clock`]）。
pub const PROBE_POLL: Duration = Duration::from_millis(20);
/// 每 COM probe RX 緩衝保留的最後 bytes 數（Python `del buf[:-4096]`）。
pub const RX_BUFFER_MAX: usize = 4096;
/// holder watch 輪詢間隔（Python `poll_s=0.5`）。
pub const HOLDER_POLL: Duration = Duration::from_millis(500);
/// holder watch 上限（Python `max_s=1800.0`）。
pub const HOLDER_MAX: Duration = Duration::from_secs(1800);
/// pump 每次讀取上限（Python `chunk=4096`）。
pub const PUMP_CHUNK: usize = 4096;
/// pump 等可讀的上限（Python `select(…, 0.2)`）。
pub const PUMP_SELECT: Duration = Duration::from_millis(200);
/// 等 actor 回覆 flash 事件的上限（真實時間；actor 不經 Clock）。
pub const REPLY_WAIT: Duration = Duration::from_secs(2);
/// join 本次 holder watch thread 的上限（計畫 Task 9 第 7 點：2.0 s）。
pub const HOLDER_JOIN: Duration = Duration::from_secs(2);

/// flash TX 的 op_id 區段（與 SessionCore 遞增序號、登入 `1 << 62` 區段不重疊）。
pub(crate) const FLASH_OP_ID_BASE: u64 = 1 << 61;

/// 端點 thread 目前所在階段（stop 逾時時決定 `stalled_threads` 名稱）。
pub(crate) const PHASE_IDLE: u8 = 0;
pub(crate) const PHASE_DETECT: u8 = 1;
pub(crate) const PHASE_PUMP: u8 = 2;
pub(crate) const PHASE_FINALLY: u8 = 3;

/// 最近一次偵測結果（Python `_flash_last_detect`；`mcu.status` 的 `last_detect`）。
#[derive(Debug, Clone, PartialEq)]
pub struct LastDetect {
    pub status: DetectStatus,
    pub com: Option<String>,
    pub family: Option<String>,
    /// `(by_id, com)`；Python `[{"by_id": h, "com": by_id_to_com.get(h)}]`。
    pub hits: Vec<(String, Option<String>)>,
}

impl LastDetect {
    /// Python 鍵序 `{"status","com","family","hits":[{"by_id","com"}]}`。
    pub fn to_py(&self) -> PyDict {
        todo!("P8-T9")
    }
}

struct ShutdownInner {
    flag: AtomicBool,
    wakers: Mutex<Vec<Arc<dyn IoWaker>>>,
    /// 已登記的單次 flash 取消旗標（`set` 時一併取消；弱參照，失效者於登記時清除）。
    runs: Mutex<Vec<Weak<RunInner>>>,
}

/// 端點停止旗標（見模組說明）。
#[derive(Clone)]
pub struct EndpointShutdown {
    inner: Arc<ShutdownInner>,
}

impl EndpointShutdown {
    /// `waker` 於 `set` 時喚醒（端點 PTY 建立後 `FlashEndpoint::start` 另登記 master 的 waker）。
    pub fn new(waker: Arc<dyn IoWaker>) -> Self {
        let _ = waker;
        todo!("P8-T9")
    }

    pub fn set(&self) {
        todo!("P8-T9")
    }

    pub fn is_set(&self) -> bool {
        todo!("P8-T9")
    }
}

struct RunInner {
    flag: AtomicBool,
    waker: Arc<dyn IoWaker>,
    /// 取消時先送一個 `()` 再丟棄送端：等在 [`Clock::wait_until_or_cancel`] 的 thread 立即醒來。
    tx: Mutex<Option<Sender<()>>>,
    rx: Receiver<()>,
}

/// 單次 flash 的取消旗標（見模組說明）。
#[derive(Clone)]
pub struct FlashRunCancel {
    inner: Arc<RunInner>,
    shutdown: EndpointShutdown,
}

impl FlashRunCancel {
    /// 新建並登記到 `shutdown`（之後 `shutdown.set()` 一併取消；`shutdown` 已設定則建立即為取消）。
    pub fn new(shutdown: &EndpointShutdown, waker: Arc<dyn IoWaker>) -> Self {
        let _ = (shutdown, waker);
        todo!("P8-T9")
    }

    /// 取消本次 flash：設旗標、喚醒 Clock 等待與 master 的 `wait_readable`。**不**設 `EndpointShutdown`。
    pub fn cancel(&self) {
        todo!("P8-T9")
    }

    /// 本身已取消或 `EndpointShutdown` 已設定。
    pub fn is_cancelled(&self) -> bool {
        todo!("P8-T9")
    }

    /// 經 `clock` 等到 `deadline_ns` 或取消；回 true＝期限到、false＝已取消。
    pub fn wait_until(&self, clock: &dyn Clock, deadline_ns: u64) -> bool {
        let _ = (clock, deadline_ns);
        todo!("P8-T9")
    }
}

/// 端點 PTY 的 I/O（端點 thread 持有；`on_flash_open` 借用）。
pub struct FlashPtyIo {
    pub master: Box<dyn ConsoleIo>,
    pub slave_path: String,
    pub slave_ref: PtySlaveRef,
}

/// 只供測試：coordinator 送出的 flash 事件紀錄。
#[cfg(feature = "test-fakes")]
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FlashEventRecord {
    pub com: String,
    /// `"FlashSyncProbeStart"`／`"FlashProbeReleased"`／`"FlashSyncMatched"`／`"FlashHolderGone"`。
    pub kind: &'static str,
    pub probe_id: crate::session_core::ProbeId,
}

#[cfg(feature = "test-fakes")]
#[derive(Default)]
struct TestHooks {
    events: Mutex<Vec<FlashEventRecord>>,
    run_registered: Mutex<Option<Box<dyn FnOnce() + Send>>>,
    opens_completed: AtomicU64,
}

#[derive(Default)]
struct FlashShared {
    current_run: Option<FlashRunCancel>,
    rx_buffers: HashMap<String, Vec<u8>>,
    active_com: Option<String>,
    last_detect: Option<LastDetect>,
}

/// flash 認線協調者（見模組說明）。
pub struct FlashCoordinator {
    mgr: Arc<SessionManager>,
    registry: Arc<McuPatternRegistry>,
    clock: Arc<dyn Clock>,
    uart_excl: Arc<dyn UartExclusive>,
    _console: Arc<dyn ConsoleTransportFactory>,
    endpoint_shutdown: EndpointShutdown,
    shared: Mutex<FlashShared>,
    /// active flash 期間的 master 寫入分身（`clone_writer`；RX observer 與 ACK 回放共用）。
    master_writer: Mutex<Option<Box<dyn ConsoleWriteHalf>>>,
    next_probe_id: AtomicU64,
    next_op: AtomicU64,
    /// join 逾時的 thread 名稱（`FlashEndpoint::stop` 併入 `EndpointStopReceipt.stalled_threads`）。
    stalled: Mutex<Vec<&'static str>>,
    phase: AtomicU8,
    #[cfg(feature = "test-fakes")]
    test: TestHooks,
}

impl FlashCoordinator {
    /// 建立並把 RX observer 登記到 `mgr`（Python `add_rx_observer(self._flash_rx_observer)`）。observer 於 bridge
    /// 建立時取快照，故 daemon 必須在 bootstrap attach **之前**建立 coordinator（與 Python `__init__` 相同時點）。
    pub fn new(
        mgr: Arc<SessionManager>,
        registry: Arc<McuPatternRegistry>,
        clock: Arc<dyn Clock>,
        uart_excl: Arc<dyn UartExclusive>,
        console: Arc<dyn ConsoleTransportFactory>,
        endpoint_shutdown: EndpointShutdown,
    ) -> Arc<Self> {
        let _ = (mgr, registry, clock, uart_excl, console, endpoint_shutdown);
        todo!("P8-T9")
    }

    /// Python `_on_flash_open(master_fd, slave_fd, first_bytes)`（`service.py:381-462`）。
    pub fn on_flash_open(&self, io: &mut FlashPtyIo, first: &[u8]) {
        let _ = (io, first);
        todo!("P8-T9")
    }

    /// `FlashEndpoint::stop`／`Drop`：同一 shared 鎖內設 `EndpointShutdown` 並取消本次 run（與 `on_flash_open` 的
    /// 登記互斥，無遺失喚醒）。
    pub fn request_shutdown(&self) {
        todo!("P8-T9")
    }

    #[cfg(feature = "test-fakes")]
    pub fn current_run_for_test(&self) -> Option<FlashRunCancel> {
        todo!("P8-T9")
    }

    /// Python `_flash_rx_observer(com, data, wal_seq)`（reader thread；非阻塞）。
    pub fn rx_observer(&self, com: &str, data: &[u8]) {
        let _ = (com, data);
        todo!("P8-T9")
    }

    /// 目前 flash 中的 COM（Python `_flash_active_com`）。
    pub fn active_com(&self) -> Option<String> {
        todo!("P8-T9")
    }

    /// 最近一次偵測結果（Python `_flash_last_detect`）。
    pub fn last_detect(&self) -> Option<LastDetect> {
        todo!("P8-T9")
    }

    /// 只供測試：送出的 flash 事件（依序）。
    #[cfg(feature = "test-fakes")]
    pub fn flash_events_for_test(&self) -> Vec<FlashEventRecord> {
        todo!("P8-T9")
    }

    /// 只供測試：下一次 `on_flash_open` 登記 `current_run` 之後、第一個 probe 之前呼叫 `hook`（一次性）。
    #[cfg(feature = "test-fakes")]
    pub fn set_run_registered_hook_for_test(&self, hook: Box<dyn FnOnce() + Send>) {
        let _ = hook;
        todo!("P8-T9")
    }

    /// 只供測試：已返回的 `on_flash_open` 次數。
    #[cfg(feature = "test-fakes")]
    pub fn opens_completed_for_test(&self) -> u64 {
        todo!("P8-T9")
    }
}
