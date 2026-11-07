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
use std::panic::{catch_unwind, resume_unwind, AssertUnwindSafe};
use std::sync::atomic::{AtomicBool, AtomicU64, AtomicU8, Ordering};
use std::sync::{Arc, Mutex, MutexGuard, Weak};
use std::thread::JoinHandle;
use std::time::{Duration, Instant};

use crossbeam_channel::{bounded, Receiver, Sender};
use sw_pal::console_pty::{ConsoleIo, ConsoleTransportFactory, ConsoleWriteHalf, PtySlaveRef};
use sw_pal::traits::{Clock, IoWaker, TxOutcome, UartExclusive};
use sw_proto::pydict;
use sw_proto::pyjson::{PyDict, PyValue};

use crate::mailbox::ControlEvent;
use crate::mcu_patterns::McuPatternRegistry;
use crate::session_core::{ProbeGrant, ProbeId};
use crate::session_manager::{FlashCandidate, SessionManager};
use crate::tx_writer::{OpId, TxClass, TxJob, TxPermit};
use crate::uart_bridge::RxObserver;

use super::detect::{detect_mcu_line, DetectStatus, ProbeTransport};
use super::holder_watch::watch_flasher_disconnect;
use super::pump::pump_endpoint_to_sink;

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

fn opt_str(v: &Option<String>) -> PyValue {
    match v {
        Some(s) => PyValue::from(s.clone()),
        None => PyValue::Null,
    }
}

impl LastDetect {
    /// Python 鍵序 `{"status","com","family","hits":[{"by_id","com"}]}`。
    pub fn to_py(&self) -> PyDict {
        let hits: Vec<PyValue> = self
            .hits
            .iter()
            .map(|(by_id, com)| PyValue::Dict(pydict! { "by_id" => by_id.clone(), "com" => opt_str(com) }))
            .collect();
        pydict! {
            "status" => self.status.as_str(),
            "com" => opt_str(&self.com),
            "family" => opt_str(&self.family),
            "hits" => PyValue::List(hits)
        }
    }
}

fn lock<T>(m: &Mutex<T>) -> MutexGuard<'_, T> {
    m.lock().unwrap_or_else(|e| e.into_inner())
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
        EndpointShutdown {
            inner: Arc::new(ShutdownInner {
                flag: AtomicBool::new(false),
                wakers: Mutex::new(vec![waker]),
                runs: Mutex::new(Vec::new()),
            }),
        }
    }

    /// 設定停止旗標：喚醒端點 thread 的等待，並取消所有仍存活的單次 flash。冪等。
    pub fn set(&self) {
        // 先在 runs 鎖內設旗標：之後才登記的 run 一律看到已設定（建立即為取消），不遺失取消。
        let runs: Vec<Arc<RunInner>> = {
            let runs = lock(&self.inner.runs);
            self.inner.flag.store(true, Ordering::SeqCst);
            runs.iter().filter_map(Weak::upgrade).collect()
        };
        for run in runs {
            run.cancel();
        }
        let wakers: Vec<Arc<dyn IoWaker>> = lock(&self.inner.wakers).clone();
        for w in wakers {
            w.wake();
        }
    }

    pub fn is_set(&self) -> bool {
        self.inner.flag.load(Ordering::SeqCst)
    }

    /// 端點 PTY 建立後登記 master 的 waker（`set` 時喚醒端點 thread 的 `wait_readable`）。
    pub(crate) fn add_waker(&self, waker: Arc<dyn IoWaker>) {
        lock(&self.inner.wakers).push(waker);
        if self.is_set() {
            // `set` 已發生：補喚醒，避免端點 thread 等滿一個 grace。
            let wakers: Vec<Arc<dyn IoWaker>> = lock(&self.inner.wakers).clone();
            for w in wakers {
                w.wake();
            }
        }
    }
}

struct RunInner {
    flag: AtomicBool,
    waker: Arc<dyn IoWaker>,
    /// 取消時先送一個 `()` 再丟棄送端：等在 [`Clock::wait_until_or_cancel`] 的 thread 立即醒來。
    tx: Mutex<Option<Sender<()>>>,
    rx: Receiver<()>,
}

impl RunInner {
    fn cancel(&self) {
        self.flag.store(true, Ordering::SeqCst);
        if let Some(tx) = lock(&self.tx).take() {
            let _ = tx.try_send(());
        }
        self.waker.wake();
    }
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
        let (tx, rx) = bounded(1);
        let inner = Arc::new(RunInner {
            flag: AtomicBool::new(false),
            waker,
            tx: Mutex::new(Some(tx)),
            rx,
        });
        {
            let mut runs = lock(&shutdown.inner.runs);
            runs.retain(|w| w.strong_count() > 0);
            if shutdown.inner.flag.load(Ordering::SeqCst) {
                inner.cancel();
            } else {
                runs.push(Arc::downgrade(&inner));
            }
        }
        FlashRunCancel {
            inner,
            shutdown: shutdown.clone(),
        }
    }

    /// 取消本次 flash：設旗標、喚醒 Clock 等待與 master 的 `wait_readable`。**不**設 `EndpointShutdown`。
    pub fn cancel(&self) {
        self.inner.cancel();
    }

    /// 本身已取消或 `EndpointShutdown` 已設定。
    pub fn is_cancelled(&self) -> bool {
        self.inner.flag.load(Ordering::SeqCst) || self.shutdown.is_set()
    }

    /// 經 `clock` 等到 `deadline_ns` 或取消；回 true＝期限到、false＝已取消。
    pub fn wait_until(&self, clock: &dyn Clock, deadline_ns: u64) -> bool {
        if self.is_cancelled() {
            return false;
        }
        let reached = clock.wait_until_or_cancel(deadline_ns, &self.inner.rx);
        reached && !self.is_cancelled()
    }

    /// 取消訊號的接收端（等 actor 回覆時與回覆 channel 一起 select）。
    fn cancel_rx(&self) -> &Receiver<()> {
        &self.inner.rx
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
    pub probe_id: ProbeId,
}

/// 只供測試：事件紀錄上限（有界；超過丟最舊）。
#[cfg(feature = "test-fakes")]
const TEST_EVENTS_MAX: usize = 4096;

#[cfg(feature = "test-fakes")]
#[derive(Default)]
struct TestHooks {
    events: Mutex<std::collections::VecDeque<FlashEventRecord>>,
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

/// 命中後保持 gate 的候選（Python `_BridgeProbe.held`）。
#[derive(Debug, Clone)]
struct Held {
    com: String,
    probe_id: ProbeId,
    epoch: u64,
}

/// 單次 `on_flash_open` 的偵測狀態（Python `_BridgeProbe` 的 `acks`／`held`）。
#[derive(Default)]
struct OpenState {
    acks: HashMap<String, Vec<u8>>,
    held: Vec<Held>,
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

/// `SessionManager` 的 RX observer 轉接（弱參照，避免 manager↔coordinator 循環持有）。
struct CoordRx(Weak<FlashCoordinator>);

impl RxObserver for CoordRx {
    fn on_rx(&self, com: &str, data: &[u8], _wal_seq: u64) {
        if let Some(coord) = self.0.upgrade() {
            coord.rx_observer(com, data);
        }
    }
}

/// detection 期間的 [`ProbeTransport`]（Python `_BridgeProbe`）。
struct BridgeProbe<'a> {
    coord: &'a FlashCoordinator,
    run: &'a FlashRunCancel,
    by_id_to_com: &'a HashMap<String, String>,
    sync_bytes: &'a [u8],
    st: &'a mut OpenState,
}

impl ProbeTransport for BridgeProbe<'_> {
    fn probe(&mut self, by_id: &str, probe_bytes: &[u8], expect: &[u8], timeout_ms: u64) -> bool {
        let coord = self.coord;
        // daemon 停止與本次 open 同時發生：不送 FlashSyncProbeStart、零 TX。
        if self.run.is_cancelled() {
            return false;
        }
        let Some(com) = self.by_id_to_com.get(by_id) else {
            return false;
        };
        let probe_id = coord.next_probe_id.fetch_add(1, Ordering::SeqCst) + 1;
        let Some(grant) = coord.probe_start(com, probe_id, self.run) else {
            return false;
        };
        lock(&coord.shared).rx_buffers.insert(com.clone(), Vec::new());
        // 關鍵（真機修正）：送 flasher 自己的 sync bytes，無則退回 pattern probe；不另注入 sync（double-sync）。
        let payload = if self.sync_bytes.is_empty() { probe_bytes } else { self.sync_bytes };
        if !coord.send_tx(com, grant.epoch, TxClass::FlashProbe, Some(probe_id), payload) {
            coord.release(com, probe_id);
            return false;
        }
        let deadline = coord.clock.mono_ns().saturating_add(timeout_ms.saturating_mul(1_000_000));
        let poll_ns = PROBE_POLL.as_nanos() as u64;
        while coord.clock.mono_ns() < deadline {
            if self.run.is_cancelled() {
                break;
            }
            let buf = lock(&coord.shared).rx_buffers.get(com).cloned().unwrap_or_default();
            if contains(&buf, expect) {
                // 記下 MCU 回應（含 ACK）供回放；命中者保持 gate（不送 Released）直到偵測完成（RACE-2，#83）。
                self.st.acks.insert(by_id.to_string(), buf);
                self.st.held.push(Held {
                    com: com.clone(),
                    probe_id,
                    epoch: grant.epoch,
                });
                return true;
            }
            let next = coord.clock.mono_ns().saturating_add(poll_ns);
            if !self.run.wait_until(&*coord.clock, next) {
                break;
            }
        }
        coord.release(com, probe_id);
        false
    }
}

/// Python `expect in buf`（空 `expect` 恆為真）。
fn contains(buf: &[u8], expect: &[u8]) -> bool {
    expect.is_empty() || buf.windows(expect.len()).any(|w| w == expect)
}

/// 等 `handle` 結束（真實時間上限 `budget`）；回 true 表示已 join。
fn join_within(handle: JoinHandle<()>, budget: Duration) -> bool {
    let deadline = Instant::now() + budget;
    while !handle.is_finished() {
        if Instant::now() >= deadline {
            return false;
        }
        std::thread::sleep(Duration::from_millis(2));
    }
    let _ = handle.join();
    true
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
        let coord = Arc::new(FlashCoordinator {
            mgr: mgr.clone(),
            registry,
            clock,
            uart_excl,
            _console: console,
            endpoint_shutdown,
            shared: Mutex::new(FlashShared::default()),
            master_writer: Mutex::new(None),
            next_probe_id: AtomicU64::new(0),
            next_op: AtomicU64::new(0),
            stalled: Mutex::new(Vec::new()),
            phase: AtomicU8::new(PHASE_IDLE),
            #[cfg(feature = "test-fakes")]
            test: TestHooks::default(),
        });
        mgr.add_rx_observer(Arc::new(CoordRx(Arc::downgrade(&coord))));
        coord
    }

    /// Python `_on_flash_open(master_fd, slave_fd, first_bytes)`（`service.py:381-462`）。
    pub fn on_flash_open(&self, io: &mut FlashPtyIo, first: &[u8]) {
        let run = {
            let mut sh = lock(&self.shared);
            let run = FlashRunCancel::new(&self.endpoint_shutdown, io.master.waker());
            sh.current_run = Some(run.clone());
            run
        };
        self.phase.store(PHASE_DETECT, Ordering::SeqCst);
        let mut st = OpenState::default();
        let mut probe_coms: Vec<String> = Vec::new();
        let body = catch_unwind(AssertUnwindSafe(|| {
            #[cfg(feature = "test-fakes")]
            if let Some(hook) = lock(&self.test.run_registered).take() {
                hook();
            }
            // RACE-2 final（#83）：原子收集候選並標記 flash-critical。
            let candidates = self.mgr.collect_flash_candidates_and_mark();
            probe_coms = candidates.iter().map(|c| c.com.clone()).collect();
            self.open_body(io, &run, first, &candidates, &mut st);
        }));
        // 外層 finally：釋放仍持有但未交接的 gate、解除全部候選的 flash-critical、清 current_run。
        self.phase.store(PHASE_FINALLY, Ordering::SeqCst);
        self.release_all(&mut st.held);
        for com in &probe_coms {
            self.mgr.unmark_flash_critical(com);
        }
        {
            let mut sh = lock(&self.shared);
            for com in &probe_coms {
                sh.rx_buffers.remove(com);
            }
            sh.current_run = None;
        }
        self.phase.store(PHASE_IDLE, Ordering::SeqCst);
        #[cfg(feature = "test-fakes")]
        self.test.opens_completed.fetch_add(1, Ordering::SeqCst);
        if let Err(panic) = body {
            resume_unwind(panic);
        }
    }

    /// `on_flash_open` 的本體（偵測 → matched 時進 FLASHING 並 pump）；外層 finally 由呼叫端負責。
    fn open_body(
        &self,
        io: &mut FlashPtyIo,
        run: &FlashRunCancel,
        first: &[u8],
        candidates: &[FlashCandidate],
        st: &mut OpenState,
    ) {
        let by_id_to_com: HashMap<String, String> = candidates
            .iter()
            .filter_map(|c| c.by_id.clone().map(|b| (b, c.com.clone())))
            .collect();
        let result = {
            let mut probe = BridgeProbe {
                coord: self,
                run,
                by_id_to_com: &by_id_to_com,
                sync_bytes: first,
                st,
            };
            detect_mcu_line(candidates, &self.registry, &mut probe)
        };
        let com = result.by_id.as_ref().and_then(|b| by_id_to_com.get(b).cloned());
        lock(&self.shared).last_detect = Some(LastDetect {
            status: result.status.clone(),
            com: com.clone(),
            family: result.family.clone(),
            hits: result
                .hits
                .iter()
                .map(|h| (h.clone(), by_id_to_com.get(h).cloned()))
                .collect(),
        });
        // 沒命中／多義／本次已取消：保持沉默（不回放、不進 FLASHING），讓 flasher 自身 retry／timeout。
        if result.status != DetectStatus::Matched || run.is_cancelled() {
            return;
        }
        let (Some(com), Some(by_id), Some(family)) = (com, result.by_id, result.family) else {
            return;
        };
        let Some(pos) = st.held.iter().position(|h| h.com == com) else {
            return;
        };
        let held = st.held[pos].clone();
        // 命中 pattern 的 registry baud，供 termios 鏡射失敗時 fallback（I3）。
        let fallback_baud = self.registry.get(&family).map(|p| p.baud);
        let ack = st.acks.get(&by_id).cloned().unwrap_or_default();
        match self.matched(&com, held.probe_id, run) {
            Matched::Ok => {}
            // actor 拒絕：held 仍在，外層 finally 一併 Released。
            Matched::Rejected => return,
            // 未知是否已 Matched：以 HolderGone 收回（同一 probe 的 HolderGone 同時清 Probe 與 FLASHING）。
            Matched::Unknown => {
                st.held.remove(pos);
                self.holder_gone(&com, held.probe_id);
                return;
            }
        }
        // 命中線交接 flash 生命週期；其餘 held 一併釋放，並立即解除全部候選的 flash-critical。
        st.held.remove(pos);
        self.release_all(&mut st.held);
        for c in candidates {
            self.mgr.unmark_flash_critical(&c.com);
        }
        self.flash_session(io, run, &com, held, fallback_baud, &ack);
    }

    /// matched 之後：holder watch、baud 鏡射、ACK 回放、pump；finally 送 `FlashHolderGone`。
    fn flash_session(
        &self,
        io: &mut FlashPtyIo,
        run: &FlashRunCancel,
        com: &str,
        held: Held,
        fallback_baud: Option<u32>,
        ack: &[u8],
    ) {
        let writer = match io.master.clone_writer() {
            Ok(w) => Some(w),
            Err(e) => {
                self.mgr.warn(format!(
                    "serialwrap: /dev/ttyMCU 端點無法取得寫入分身（com={com}）：{}",
                    e.py_message
                ));
                None
            }
        };
        let holder = if writer.is_some() {
            *lock(&self.master_writer) = writer;
            lock(&self.shared).active_com = Some(com.to_string());
            // daemon 同持 PTY master＋slave，flasher 關閉端點時 master 收不到 EOF：以 holder-probe 偵測斷線。
            let excl = self.uart_excl.clone();
            let clock = self.clock.clone();
            let watch_run = run.clone();
            let slave_path = io.slave_path.clone();
            match std::thread::Builder::new()
                .name("serialwrap-ttyMCU-holder".into())
                .spawn(move || watch_flasher_disconnect(&*excl, &slave_path, &watch_run, &*clock, HOLDER_POLL, HOLDER_MAX))
            {
                Ok(h) => Some(h),
                Err(e) => {
                    self.mgr.warn(format!("serialwrap: /dev/ttyMCU holder watch thread 啟動失敗（com={com}）：{e}"));
                    None
                }
            }
        } else {
            None
        };
        let body = catch_unwind(AssertUnwindSafe(|| {
            if holder.is_none() {
                return;
            }
            if let Some(lc) = self.mgr.line_control_for(com) {
                // Task 7：B0 視為未設 → fallback；錯誤忽略（Python mirror_termios_from 內部吸收）。
                let _ = lc.mirror_termios_from(&io.slave_ref, fallback_baud);
            }
            if !ack.is_empty() {
                if let Some(w) = lock(&self.master_writer).as_mut() {
                    let _ = w.try_write(ack); // 讓 flasher 看到自己 sync 的回應（錯誤與部分寫入忽略）
                }
            }
            self.phase.store(PHASE_PUMP, Ordering::SeqCst);
            // first 已在 probe 階段送給 MCU，這裡不可重送。
            let mut sink = |data: &[u8]| self.send_tx(com, held.epoch, TxClass::Flash, None, data);
            pump_endpoint_to_sink(io.master.as_mut(), &mut sink, run, PUMP_CHUNK, PUMP_SELECT);
        }));
        self.phase.store(PHASE_FINALLY, Ordering::SeqCst);
        run.cancel();
        if let Some(h) = holder {
            if !join_within(h, HOLDER_JOIN) {
                self.mgr.warn(format!(
                    "serialwrap: /dev/ttyMCU holder watch thread 未在 {:.1}s 內結束（com={com}）",
                    HOLDER_JOIN.as_secs_f64()
                ));
                let mut stalled = lock(&self.stalled);
                if !stalled.contains(&"holder") {
                    stalled.push("holder"); // 去重：清單有界（stop 時取出）
                }
            }
        }
        {
            let mut sh = lock(&self.shared);
            sh.active_com = None;
            sh.rx_buffers.remove(com);
        }
        *lock(&self.master_writer) = None;
        self.holder_gone(com, held.probe_id);
        if let Err(panic) = body {
            resume_unwind(panic);
        }
    }

    /// `FlashSyncProbeStart{probe_id}`：回授權；拒絕、送不出或逾時回 `None`（逾時／取消時補送 Released 收回）。
    fn probe_start(&self, com: &str, probe_id: ProbeId, run: &FlashRunCancel) -> Option<ProbeGrant> {
        let (reply, rx) = bounded(1);
        if !self.mgr.send_control(com, ControlEvent::FlashSyncProbeStart { probe_id, reply }) {
            return None;
        }
        self.record(com, "FlashSyncProbeStart", probe_id);
        crossbeam_channel::select! {
            recv(rx) -> r => match r {
                Ok(Ok(grant)) => Some(grant),
                _ => None,
            },
            recv(run.cancel_rx()) -> _ => {
                self.release(com, probe_id);
                None
            },
            default(REPLY_WAIT) => {
                self.release(com, probe_id);
                None
            },
        }
    }

    /// `FlashSyncMatched{probe_id}`。
    fn matched(&self, com: &str, probe_id: ProbeId, run: &FlashRunCancel) -> Matched {
        let (reply, rx) = bounded(1);
        if !self.mgr.send_control(com, ControlEvent::FlashSyncMatched { probe_id, reply }) {
            return Matched::Rejected;
        }
        self.record(com, "FlashSyncMatched", probe_id);
        crossbeam_channel::select! {
            recv(rx) -> r => match r {
                Ok(Ok(())) => Matched::Ok,
                Ok(Err(_)) => Matched::Rejected,
                Err(_) => Matched::Unknown,
            },
            recv(run.cancel_rx()) -> _ => Matched::Unknown,
            default(REPLY_WAIT) => Matched::Unknown,
        }
    }

    /// `FlashProbeReleased{probe_id}`（無回覆）。
    fn release(&self, com: &str, probe_id: ProbeId) {
        if self.mgr.send_control(com, ControlEvent::FlashProbeReleased { probe_id }) {
            self.record(com, "FlashProbeReleased", probe_id);
        }
    }

    fn release_all(&self, held: &mut Vec<Held>) {
        for h in held.drain(..) {
            self.release(&h.com, h.probe_id);
        }
    }

    /// `FlashHolderGone{probe_id}`（等回覆，真實時間上限 `REPLY_WAIT`）。
    fn holder_gone(&self, com: &str, probe_id: ProbeId) {
        let (reply, rx) = bounded(1);
        if self.mgr.send_control(com, ControlEvent::FlashHolderGone { probe_id, reply }) {
            self.record(com, "FlashHolderGone", probe_id);
            let _ = rx.recv_timeout(REPLY_WAIT);
        }
    }

    /// 經 COM 目前 bridge 的 TxWriter 送一筆 flash TX（`source:"flash"`、記 WAL）；完整送出才回 true。
    fn send_tx(&self, com: &str, epoch: u64, class: TxClass, probe_id: Option<ProbeId>, payload: &[u8]) -> bool {
        let Some(handle) = self.mgr.tx_for(com) else {
            return false;
        };
        let op_id = OpId(FLASH_OP_ID_BASE | (self.next_op.fetch_add(1, Ordering::SeqCst) + 1));
        let (reply, report) = bounded(1);
        let job = TxJob {
            permit: TxPermit {
                op_id,
                epoch,
                owner_gen: 0,
                fsm_gen: 0,
                class,
                source: "flash".to_string(),
                cmd_id: None,
                log: true,
                input_epoch: None,
                replay_hold: None,
                probe_id,
            },
            payload: payload.to_vec(),
            reply,
            aggregate: None,
        };
        if handle.jobs.send(job).is_err() {
            return false;
        }
        let result = handle.wait_report(&report);
        let _ = handle.settle(op_id);
        result.is_some_and(|r| {
            r.result.error.is_none() && matches!(r.result.outcome, TxOutcome::Completed(n) if n == payload.len())
        })
    }

    fn record(&self, com: &str, kind: &'static str, probe_id: ProbeId) {
        #[cfg(feature = "test-fakes")]
        {
            let mut ev = lock(&self.test.events);
            if ev.len() >= TEST_EVENTS_MAX {
                ev.pop_front();
            }
            ev.push_back(FlashEventRecord {
                com: com.to_string(),
                kind,
                probe_id,
            });
        }
        #[cfg(not(feature = "test-fakes"))]
        let _ = (com, kind, probe_id);
    }

    /// `FlashEndpoint::stop`／`Drop`：同一 shared 鎖內設 `EndpointShutdown` 並取消本次 run（與 `on_flash_open` 的
    /// 登記互斥，無遺失喚醒）。
    pub fn request_shutdown(&self) {
        let sh = lock(&self.shared);
        self.endpoint_shutdown.set();
        if let Some(run) = sh.current_run.as_ref() {
            run.cancel();
        }
    }

    #[cfg(feature = "test-fakes")]
    pub fn current_run_for_test(&self) -> Option<FlashRunCancel> {
        lock(&self.shared).current_run.clone()
    }

    /// Python `_flash_rx_observer(com, data, wal_seq)`（reader thread；非阻塞）。
    pub fn rx_observer(&self, com: &str, data: &[u8]) {
        let active = {
            let mut sh = lock(&self.shared);
            if let Some(buf) = sh.rx_buffers.get_mut(com) {
                buf.extend_from_slice(data);
                // 限制 buffer 大小，避免長時間累積佔用記憶體（保留最後 4096）。
                if buf.len() > RX_BUFFER_MAX {
                    let cut = buf.len() - RX_BUFFER_MAX;
                    buf.drain(..cut);
                }
            }
            sh.active_com.as_deref() == Some(com)
        };
        if active {
            if let Some(w) = lock(&self.master_writer).as_mut() {
                let _ = w.try_write(data); // device RX → endpoint（flasher 讀）；錯誤忽略、不重試、不阻塞
            }
        }
    }

    /// 目前 flash 中的 COM（Python `_flash_active_com`）。
    pub fn active_com(&self) -> Option<String> {
        lock(&self.shared).active_com.clone()
    }

    /// 最近一次偵測結果（Python `_flash_last_detect`）。
    pub fn last_detect(&self) -> Option<LastDetect> {
        lock(&self.shared).last_detect.clone()
    }

    /// 端點停止旗標（`FlashEndpoint::start` 登記 master waker 用）。
    pub(crate) fn endpoint_shutdown(&self) -> &EndpointShutdown {
        &self.endpoint_shutdown
    }

    /// 端點 thread 目前階段（`stop` 逾時時決定 stalled 名稱）。
    pub(crate) fn phase(&self) -> u8 {
        self.phase.load(Ordering::SeqCst)
    }

    /// 取出（並清空）join 逾時的 thread 名稱。
    pub(crate) fn take_stalled(&self) -> Vec<&'static str> {
        std::mem::take(&mut *lock(&self.stalled))
    }

    /// 經 manager 的 WARNING 出口。
    pub(crate) fn warn(&self, msg: String) {
        self.mgr.warn(msg);
    }

    /// 只供測試：送出的 flash 事件（依序）。
    #[cfg(feature = "test-fakes")]
    pub fn flash_events_for_test(&self) -> Vec<FlashEventRecord> {
        lock(&self.test.events).iter().cloned().collect()
    }

    /// 只供測試：下一次 `on_flash_open` 登記 `current_run` 之後、第一個 probe 之前呼叫 `hook`（一次性）。
    #[cfg(feature = "test-fakes")]
    pub fn set_run_registered_hook_for_test(&self, hook: Box<dyn FnOnce() + Send>) {
        *lock(&self.test.run_registered) = Some(hook);
    }

    /// 只供測試：已返回的 `on_flash_open` 次數。
    #[cfg(feature = "test-fakes")]
    pub fn opens_completed_for_test(&self) -> u64 {
        self.test.opens_completed.load(Ordering::SeqCst)
    }
}

/// `FlashSyncMatched` 的結果。
enum Matched {
    Ok,
    Rejected,
    Unknown,
}
