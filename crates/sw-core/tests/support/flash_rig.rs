//! P8 Task 9：`/dev/ttyMCU` 端點與認線協調的測試支援（`docs/rust-refactor/plans/09-slice24-transfer-flash.md` Task 9
//! Files 的 `FlashRig`／`Mcu`）。
//!
//! 組裝：真 `SessionManager`（FakeSerialBackend 開真 `IoWorker`：reader→WAL→RX observer、唯一 TxWriter）＋真
//! `FlashCoordinator`＋真 `FlashEndpoint`（端點 thread）。替身只在邊界：
//! - 端點 PTY 的 master 是腳本化的 [`RigMaster`]（flasher 寫入＝master 可讀、記錄每次寫給 flasher 的 bytes）；
//! - MCU 是 [`Mcu`] 模擬器（觀察 FakeDevice 實際寫出的 bytes，依 TI SBL 子集回 ACK）；
//! - 端點 slave 的 holder 掃描是 [`ScriptedExcl`]（依序回 `holder_sequence`）；
//! - coordinator 的 Clock 是 threaded FakeClock 外包 [`RigClock`]（登記等待者）：`drive_until`／`advance_secs` 只在
//!   I/O 靜止（MCU 已處理全部 TX、回應已被 coordinator 的 observer 看見）且等待者穩定後，才把時間推到最近的期限，
//!   使 20 ms probe 輪詢與 0.5 s holder 輪詢可決定性地逐步推進。
//!
//! 外部 oracle：「送了什麼給 MCU」以 FakeDevice 實際寫出的 bytes 為準（`serial_tx` 另與 WAL TX 逐筆對帳），
//! 「送了什麼給 flasher」以 master 的寫入紀錄為準。manager 自己的 Clock 與 coordinator 的 Clock 分開（manager 用
//! 非 threaded FakeClock，與 `ManagerRig` 相同）。

use std::collections::{BTreeMap, HashMap, VecDeque};
use std::ops::Deref;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Condvar, Mutex, MutexGuard, OnceLock};
use std::thread::JoinHandle;
use std::time::{Duration, Instant};

use crossbeam_channel::Receiver;
use sw_core::arbiter::CommandRegistry;
use sw_core::flash_endpoint::coordinator::FlashEventRecord;
use sw_core::flash_endpoint::{EndpointShutdown, EndpointStopReceipt, FlashCoordinator, FlashEndpoint, ENDPOINT_GRACE};
use sw_core::mcu_patterns::McuPatternRegistry;
use sw_core::session_manager::{FlashCandidate, ManagerDeps, SessionManager};
use sw_core::state_store::{LoadedState, StateStore};
use sw_core::tx_writer::{Fence, FlashMode};
use sw_core::uart_bridge::RxObserver;
use sw_core::wal::{WalConfig, WalWriter};
use sw_pal::console_mux::MuxHandle;
use sw_pal::console_pty::{ConsoleIo, ConsoleTransportFactory, ConsoleWriteHalf, PtyPair, Readiness, TermSpeed};
use sw_pal::fakes::clock::FakeClock;
use sw_pal::fakes::console::{FakeConsoleFactory, FakeConsoleIo, FakeIo};
use sw_pal::fakes::env::FakeEnv;
use sw_pal::fakes::serial::{FakeDevice, FakeLineControl, FakeSerialBackend};
use sw_pal::fakes::wal_fs::FakeWalFs;
use sw_pal::traits::{
    Clock, DeviceInfo, HolderReport, HolderScanStatus, IoWaker, PalError, PalErrorKind, PalResult, ReadOutcome,
    UartExclusive,
};
use sw_pal::ConsoleKind;
use sw_proto::config_types::{load_profiles, ProfileFs};
use sw_proto::pycompat::PyOsError;
use sw_proto::pyjson::{dumps_dict, loads, PyDict, PyValue, RPC_WIRE};
use sw_proto::pypath::PathFlavor;

use super::RigFs;

/// TI SBL 的 auto-baud sync 與 ACK（`tests/contract_blackbox/fakemcu.py`）。
pub const SYNC: &[u8] = b"\x55\x55";
pub const ACK: &[u8] = b"\x00\xcc";
/// 端點 symlink（記憶體替身；`RUN_DIR/dev/ttyMCU` 的對應）。
pub const LINK: &str = "/fake/run/dev/ttyMCU";

const PROFILE_DIR: &str = "/fake/flash-profiles";
const STATE_PATH: &str = "/fake/flash-state/state.json";
const WAL_DIR: &str = "/fake/flash-wal";
const START_NS: u64 = 5_000_000_000;
const START_WALL_US: i64 = 1_790_000_000_000_000;
/// `drive_until`／`wait_*` 的真實時間上限。
const DRIVE_MAX: Duration = Duration::from_secs(20);
/// settle：等待者登記表須穩定這麼久（真實時間）才視為所有 clock 使用者都已回到等待。
const SETTLE_STABLE: Duration = Duration::from_millis(15);
const SETTLE_MAX: Duration = Duration::from_secs(3);

fn lock<T>(m: &Mutex<T>) -> MutexGuard<'_, T> {
    m.lock().unwrap_or_else(|e| e.into_inner())
}

/// 等條件成立（真實時間；逾時 panic）。
pub fn wait_real(what: &str, mut cond: impl FnMut() -> bool) {
    let deadline = Instant::now() + DRIVE_MAX;
    while !cond() {
        assert!(Instant::now() < deadline, "等待逾時：{what}");
        std::thread::sleep(Duration::from_millis(1));
    }
}

// ---------------------------------------------------------------------------
// RigClock：登記等待者的 threaded FakeClock
// ---------------------------------------------------------------------------

#[derive(Default)]
struct WaitReg {
    next: u64,
    map: BTreeMap<u64, u64>,
    changed: Option<Instant>,
}

/// coordinator 的 Clock：threaded FakeClock（只由測試推進）＋等待者登記（期限）。
pub struct RigClock {
    inner: Arc<FakeClock>,
    reg: Mutex<WaitReg>,
}

impl RigClock {
    fn new() -> Arc<RigClock> {
        Arc::new(RigClock {
            inner: FakeClock::at(START_NS, START_WALL_US),
            reg: Mutex::new(WaitReg::default()),
        })
    }

    fn register(&self, deadline: u64) -> u64 {
        let mut r = lock(&self.reg);
        r.next += 1;
        let id = r.next;
        r.map.insert(id, deadline);
        r.changed = Some(Instant::now());
        id
    }

    fn unregister(&self, id: u64) {
        let mut r = lock(&self.reg);
        r.map.remove(&id);
        r.changed = Some(Instant::now());
    }

    pub fn now(&self) -> u64 {
        self.inner.mono_ns()
    }

    /// 目前登記中的最早期限。
    pub fn min_deadline(&self) -> Option<u64> {
        lock(&self.reg).map.values().min().copied()
    }

    pub fn waiters(&self) -> usize {
        lock(&self.reg).map.len()
    }

    fn stable_and_future(&self) -> bool {
        let now = self.now();
        let r = lock(&self.reg);
        r.map.values().all(|d| *d > now) && r.changed.is_none_or(|c| c.elapsed() >= SETTLE_STABLE)
    }

    pub fn advance(&self, d: Duration) {
        self.inner.advance(d);
    }
}

impl Clock for RigClock {
    fn mono_ns(&self) -> u64 {
        self.inner.mono_ns()
    }

    fn sleep(&self, d: Duration) {
        let id = self.register(self.now().saturating_add(d.as_nanos() as u64));
        self.inner.sleep(d);
        self.unregister(id);
    }

    fn wait_until_or_cancel(&self, deadline_ns: u64, cancel: &crossbeam_channel::Receiver<()>) -> bool {
        let id = self.register(deadline_ns);
        let r = self.inner.wait_until_or_cancel(deadline_ns, cancel);
        self.unregister(id);
        r
    }

    fn wall_unix_us(&self) -> i64 {
        self.inner.wall_unix_us()
    }

    fn local_utc_offset_s(&self) -> i32 {
        self.inner.local_utc_offset_s()
    }
}

// ---------------------------------------------------------------------------
// 端點 PTY master 替身
// ---------------------------------------------------------------------------

#[derive(Default)]
struct MasterState {
    input: VecDeque<u8>,
    eof_once: bool,
    woken: bool,
    fail_next_read: bool,
    fail_wait: bool,
    zero_writes: bool,
    writes: Vec<Vec<u8>>,
    write_attempts: u64,
    closed: bool,
    last_flasher: Vec<u8>,
}

#[derive(Default)]
struct MasterShared {
    st: Mutex<MasterState>,
    cv: Condvar,
}

impl MasterShared {
    fn record_write(&self, data: &[u8]) -> PalResult<usize> {
        let mut st = lock(&self.st);
        st.write_attempts += 1;
        if st.closed {
            return Err(PalError::plain(PalErrorKind::BrokenPipe, "[Errno 32] Broken pipe"));
        }
        if st.zero_writes || data.is_empty() {
            return Ok(0);
        }
        st.writes.push(data.to_vec());
        Ok(data.len())
    }

    fn notify(&self) {
        self.cv.notify_all();
    }
}

/// 端點 master（`ConsoleIo`）：flasher 寫入即可讀；`try_write`／寫入分身記錄每次寫給 flasher 的 bytes。
struct RigMaster {
    sh: Arc<MasterShared>,
    /// 只為 `mux_handle`（PAL 私有建構；flash 路徑不使用）。
    mux: FakeConsoleIo,
}

impl RigMaster {
    fn wait(&self, timeout: Option<Duration>, consume: bool, buf: &mut [u8]) -> PalResult<Result<ReadOutcome, Readiness>> {
        let deadline = timeout.map(|t| Instant::now() + t);
        let mut st = lock(&self.sh.st);
        loop {
            if !consume && st.fail_wait {
                return Err(PalError::plain(PalErrorKind::Io, "[Errno 9] Bad file descriptor"));
            }
            if st.woken {
                st.woken = false;
                return Ok(if consume { Ok(ReadOutcome::Woken) } else { Err(Readiness::Woken) });
            }
            if consume && st.fail_next_read {
                st.fail_next_read = false;
                return Err(PalError::plain(PalErrorKind::Io, "[Errno 5] Input/output error"));
            }
            if !st.input.is_empty() {
                if !consume {
                    return Ok(Err(Readiness::Ready));
                }
                let n = buf.len().min(st.input.len());
                for (dst, src) in buf.iter_mut().zip(st.input.drain(..n)) {
                    *dst = src;
                }
                return Ok(Ok(ReadOutcome::Data(n)));
            }
            if st.eof_once || st.fail_next_read {
                if !consume {
                    return Ok(Err(Readiness::Ready));
                }
                st.eof_once = false;
                return Ok(Ok(ReadOutcome::Eof));
            }
            match deadline {
                None => st = self.sh.cv.wait(st).unwrap_or_else(|e| e.into_inner()),
                Some(d) => {
                    let now = Instant::now();
                    if now >= d {
                        return Ok(if consume { Ok(ReadOutcome::TimedOut) } else { Err(Readiness::TimedOut) });
                    }
                    st = self.sh.cv.wait_timeout(st, d - now).unwrap_or_else(|e| e.into_inner()).0;
                }
            }
        }
    }
}

struct RigWaker(Arc<MasterShared>);

impl IoWaker for RigWaker {
    fn wake(&self) {
        lock(&self.0.st).woken = true;
        self.0.notify();
    }
}

struct RigWriter(Arc<MasterShared>);

impl ConsoleWriteHalf for RigWriter {
    fn try_write(&mut self, data: &[u8]) -> PalResult<usize> {
        self.0.record_write(data)
    }
}

impl ConsoleIo for RigMaster {
    fn read(&mut self, buf: &mut [u8], timeout: Option<Duration>) -> PalResult<ReadOutcome> {
        match self.wait(timeout, true, buf)? {
            Ok(o) => Ok(o),
            Err(_) => unreachable!("consume=true 只回 ReadOutcome"),
        }
    }

    fn try_write(&mut self, data: &[u8]) -> PalResult<usize> {
        self.sh.record_write(data)
    }

    fn waker(&self) -> Arc<dyn IoWaker> {
        Arc::new(RigWaker(self.sh.clone()))
    }

    fn has_external_peer(&self, _held: Option<&std::collections::HashSet<String>>) -> bool {
        false
    }

    fn close(self: Box<Self>) {
        lock(&self.sh.st).closed = true;
        self.sh.notify();
    }

    fn mux_handle(&self) -> MuxHandle {
        self.mux.mux_handle()
    }

    fn clone_writer(&self) -> PalResult<Box<dyn ConsoleWriteHalf>> {
        Ok(Box::new(RigWriter(self.sh.clone())))
    }

    fn wait_readable(&mut self, timeout: Option<Duration>) -> PalResult<Readiness> {
        let mut none: [u8; 0] = [];
        match self.wait(timeout, false, &mut none)? {
            Err(r) => Ok(r),
            Ok(_) => unreachable!("consume=false 只回 Readiness"),
        }
    }
}

impl Drop for RigMaster {
    fn drop(&mut self) {
        lock(&self.sh.st).closed = true;
        self.sh.notify();
    }
}

// ---------------------------------------------------------------------------
// console 工廠
// ---------------------------------------------------------------------------

/// FlashEndpoint 的 console 工廠：`create_pty` 回腳本化 master（slave 路徑沿用 FakeConsoleFactory 的 `/fake/pts/<n>`）。
pub struct RigFlashConsole {
    fake: FakeConsoleFactory,
    master: Mutex<Option<Arc<MasterShared>>>,
    slave_path: Mutex<Option<String>>,
    links: Mutex<HashMap<PathBuf, String>>,
    raw: Mutex<Vec<String>>,
    creates: Mutex<usize>,
}

impl RigFlashConsole {
    fn new() -> Arc<RigFlashConsole> {
        Arc::new(RigFlashConsole {
            fake: FakeConsoleFactory::new(ConsoleKind::Pty),
            master: Mutex::new(None),
            slave_path: Mutex::new(None),
            links: Mutex::new(HashMap::new()),
            raw: Mutex::new(Vec::new()),
            creates: Mutex::new(0),
        })
    }

    fn master(&self) -> Arc<MasterShared> {
        lock(&self.master).clone().expect("端點 PTY 尚未建立")
    }
}

impl ConsoleTransportFactory for RigFlashConsole {
    fn kind(&self) -> ConsoleKind {
        ConsoleKind::Pty
    }

    fn create_pty(&self) -> PalResult<PtyPair> {
        *lock(&self.creates) += 1;
        let mut pair = self.fake.pty_pair_from(FakeIo::unbounded());
        let sh = Arc::new(MasterShared::default());
        pair.master = Box::new(RigMaster {
            sh: sh.clone(),
            mux: FakeConsoleIo::new().0,
        });
        *lock(&self.master) = Some(sh);
        *lock(&self.slave_path) = Some(pair.slave_path.clone());
        Ok(pair)
    }

    fn slave_path_exists(&self, path: &str) -> bool {
        self.fake.slave_path_exists(path)
    }

    fn link(&self, slave_path: &str, link: &Path) -> PalResult<()> {
        lock(&self.links).insert(link.to_path_buf(), slave_path.to_string());
        Ok(())
    }

    fn unlink(&self, link: &Path) -> PalResult<()> {
        lock(&self.links).remove(link);
        Ok(())
    }

    fn tty_setraw(&self, slave: &sw_pal::console_pty::PtySlaveRef) -> PalResult<()> {
        lock(&self.raw).push(slave.path.clone());
        Ok(())
    }
}

/// manager 用的 console 工廠（只有 `kind`：不建哨兵、不建 hub；與 `ManagerRig` 的 `RigConsole` 相同）。
struct MgrConsole;

impl ConsoleTransportFactory for MgrConsole {
    fn kind(&self) -> ConsoleKind {
        ConsoleKind::Pty
    }
}

// ---------------------------------------------------------------------------
// holder 掃描替身
// ---------------------------------------------------------------------------

struct ExclState {
    seq: VecDeque<Vec<u32>>,
    default: Vec<u32>,
    status: HolderScanStatus,
    calls: Vec<String>,
}

/// 端點 slave 的 holder 掃描：依序回 `holder_sequence`，耗盡後回 `default`（預設空＝永不 armed）。
pub struct ScriptedExcl {
    st: Mutex<ExclState>,
}

impl UartExclusive for ScriptedExcl {
    fn probe_holders(&self, real_path: &str) -> HolderReport {
        let mut st = lock(&self.st);
        st.calls.push(real_path.to_string());
        let pids = st.seq.pop_front().unwrap_or_else(|| st.default.clone());
        HolderReport {
            pids,
            holder_unknown: st.status != HolderScanStatus::Ok,
            status: st.status,
        }
    }
}

/// manager 開埠時的 holder 診斷（一律無 holder）。
struct NoHolders;

impl UartExclusive for NoHolders {
    fn probe_holders(&self, _real_path: &str) -> HolderReport {
        HolderReport {
            pids: Vec::new(),
            holder_unknown: false,
            status: HolderScanStatus::Ok,
        }
    }
}

struct NoopWaker;

impl IoWaker for NoopWaker {
    fn wake(&self) {}
}

// ---------------------------------------------------------------------------
// MCU 模擬器
// ---------------------------------------------------------------------------

/// MCU 線行為（TI SBL 子集；`tests/contract_blackbox/fakemcu.py` 的同等規則）。
#[derive(Clone, Debug)]
pub struct Mcu {
    sync_reply: Option<Vec<u8>>,
    packet_ack: bool,
    delay: Duration,
    flood: usize,
}

impl Mcu {
    /// 收到 `55 55` 回 `00 cc`；同步後每個封包（首 byte＝總長）回 `00 cc`；封包邊界再見 `55 55` 視為重新同步。
    pub fn ti_sbl() -> Mcu {
        Mcu { sync_reply: Some(ACK.to_vec()), packet_ack: true, delay: Duration::ZERO, flood: 0 }
    }

    /// 永不回應。
    pub fn silent() -> Mcu {
        Mcu { sync_reply: None, packet_ack: false, delay: Duration::ZERO, flood: 0 }
    }

    /// 收到 sync 回 `reply`（其餘同 `ti_sbl`）。
    pub fn replying(reply: &[u8]) -> Mcu {
        Mcu { sync_reply: Some(reply.to_vec()), ..Mcu::ti_sbl() }
    }

    /// sync 回應延遲 `d`（coordinator Clock 的時間）。
    pub fn delayed(mut self, d: Duration) -> Mcu {
        self.delay = d;
        self
    }

    /// sync 回應前先送 `n` bytes 雜訊（`0x41`）。
    pub fn flood_before_ack(mut self, n: usize) -> Mcu {
        self.flood = n;
        self
    }
}

struct McuDev {
    com: String,
    dev: FakeDevice,
    mcu: Mcu,
    processed: usize,
    synced: bool,
    pending: Vec<u8>,
    delayed: Vec<(u64, Vec<u8>)>,
    pushed: u64,
}

impl McuDev {
    fn sync_reply(&self) -> Vec<u8> {
        let mut out = vec![0x41u8; self.mcu.flood];
        out.extend_from_slice(self.mcu.sync_reply.as_deref().unwrap_or_default());
        out
    }

    /// 解析新 TX，回傳應回的回應（fakemcu.py `_consume`）。
    fn feed(&mut self, data: &[u8]) -> Vec<Vec<u8>> {
        if self.mcu.sync_reply.is_none() {
            return Vec::new();
        }
        self.pending.extend_from_slice(data);
        let mut out = Vec::new();
        loop {
            if !self.synced {
                match self.pending.windows(2).position(|w| w == SYNC) {
                    Some(i) => {
                        self.pending.drain(..i + 2);
                        self.synced = true;
                        out.push(self.sync_reply());
                        continue;
                    }
                    None => {
                        let keep = self.pending.len().min(1);
                        let cut = self.pending.len() - keep;
                        self.pending.drain(..cut);
                        return out;
                    }
                }
            }
            if self.pending.len() >= 2 && &self.pending[..2] == SYNC {
                self.pending.drain(..2);
                out.push(self.sync_reply());
                continue;
            }
            let Some(&size) = self.pending.first() else {
                return out;
            };
            let size = size as usize;
            if size < 2 {
                self.pending.drain(..1);
                continue;
            }
            if self.pending.len() < size {
                return out;
            }
            self.pending.drain(..size);
            if self.mcu.packet_ack {
                out.push(ACK.to_vec());
            }
        }
    }
}

struct McuBank {
    devs: Mutex<Vec<McuDev>>,
    clock: Arc<RigClock>,
}

impl McuBank {
    fn step(&self) {
        let now = self.clock.now();
        let mut devs = lock(&self.devs);
        for d in devs.iter_mut() {
            let new = d.dev.tx_since(d.processed);
            d.processed += new.len();
            for reply in d.feed(&new) {
                if d.mcu.delay.is_zero() {
                    d.dev.push_rx(&reply);
                    d.pushed += reply.len() as u64;
                } else {
                    d.delayed.push((now + d.mcu.delay.as_nanos() as u64, reply));
                }
            }
            let mut i = 0;
            while i < d.delayed.len() {
                if d.delayed[i].0 <= now {
                    let (_, reply) = d.delayed.remove(i);
                    d.dev.push_rx(&reply);
                    d.pushed += reply.len() as u64;
                } else {
                    i += 1;
                }
            }
        }
    }

    fn quiescent(&self, seen: &SeenCounter) -> bool {
        let now = self.clock.now();
        lock(&self.devs).iter().all(|d| {
            d.processed == d.dev.tx_len() && d.delayed.iter().all(|(due, _)| *due > now) && seen.get(&d.com) == d.pushed
        })
    }
}

/// manager 上第二個 RX observer（登記在 coordinator 之後）：計數 → 已被 coordinator 看見的 RX bytes。
#[derive(Default)]
struct SeenCounter {
    map: Mutex<HashMap<String, u64>>,
}

impl SeenCounter {
    fn get(&self, com: &str) -> u64 {
        lock(&self.map).get(com).copied().unwrap_or(0)
    }
}

impl RxObserver for SeenCounter {
    fn on_rx(&self, com: &str, data: &[u8], _wal_seq: u64) {
        *lock(&self.map).entry(com.to_string()).or_insert(0) += data.len() as u64;
    }
}

// ---------------------------------------------------------------------------
// FlashRig
// ---------------------------------------------------------------------------

struct MemProfiles {
    yaml: String,
}

impl ProfileFs for MemProfiles {
    fn flavor(&self) -> PathFlavor {
        PathFlavor::Posix
    }
    fn is_dir(&self, p: &str) -> bool {
        p == PROFILE_DIR
    }
    fn list_dir(&self, _p: &str) -> Result<Vec<String>, PyOsError> {
        Ok(vec!["flash.yaml".to_string()])
    }
    fn read_bytes(&self, _p: &str) -> Result<Vec<u8>, PyOsError> {
        Ok(self.yaml.clone().into_bytes())
    }
    fn expanduser(&self, p: &str) -> String {
        p.to_string()
    }
}

const TEMPLATES: &str = "profiles:\n  mcu-pt:\n    platform: passthrough\n    prompt_regex: '(?m)^MCU> '\n    ready_probe: ''\n";

struct Target {
    com: String,
    by_id: String,
    mcu: Mcu,
}

struct Started {
    mgr: Arc<SessionManager>,
    coord: Arc<FlashCoordinator>,
    shutdown: EndpointShutdown,
    endpoint: Arc<Mutex<Option<FlashEndpoint>>>,
    devs: HashMap<String, FakeDevice>,
    bank: Arc<McuBank>,
    seen: Arc<SeenCounter>,
    emu_stop: Arc<AtomicBool>,
    emu: Mutex<Option<JoinHandle<()>>>,
    wal: Arc<WalWriter>,
    wfs: Arc<FakeWalFs>,
    warnings: Arc<Mutex<Vec<String>>>,
}

/// `/dev/ttyMCU` 端點測試台（見模組說明）。目標以 `add_passthrough` 登記，第一個操作時才組裝並 attach。
pub struct FlashRig {
    targets: Mutex<Vec<Target>>,
    flash_caps: bool,
    pub clock: Arc<RigClock>,
    excl: Arc<ScriptedExcl>,
    console: Arc<RigFlashConsole>,
    /// `Some(link)`：端點改用真 PTY（`NativeConsoleFactory`），symlink 建在真檔案系統的 `link`（`cap_posix_pty`）。
    real_pty_link: Option<PathBuf>,
    started: OnceLock<Started>,
}

/// `FlashRig::endpoint()` 的借用（端點已停止則 panic）。
pub struct EndpointRef<'a>(MutexGuard<'a, Option<FlashEndpoint>>);

impl Deref for EndpointRef<'_> {
    type Target = FlashEndpoint;
    fn deref(&self) -> &FlashEndpoint {
        self.0.as_ref().expect("端點已停止")
    }
}

/// `stop_endpoint_on_next_run_registered` 的回條等待端。
pub struct Stopper {
    rx: Receiver<EndpointStopReceipt>,
}

impl Stopper {
    pub fn join_within(self, d: Duration) -> EndpointStopReceipt {
        self.rx.recv_timeout(d).expect("stop() 未在期限內返回")
    }
}

/// WAL TX 列。
#[derive(Debug, Clone)]
pub struct WalTx {
    pub source: String,
    pub payload: Vec<u8>,
}

fn b64_decode(s: &str) -> Vec<u8> {
    const T: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut out = Vec::new();
    let mut buf = 0u32;
    let mut bits = 0u32;
    for c in s.bytes() {
        if c == b'=' {
            break;
        }
        let Some(v) = T.iter().position(|&t| t == c) else {
            continue;
        };
        buf = (buf << 6) | v as u32;
        bits += 6;
        if bits >= 8 {
            bits -= 8;
            out.push((buf >> bits) as u8);
        }
    }
    out
}

impl FlashRig {
    fn with_caps(flash_caps: bool) -> FlashRig {
        FlashRig {
            targets: Mutex::new(Vec::new()),
            flash_caps,
            clock: RigClock::new(),
            excl: Arc::new(ScriptedExcl {
                st: Mutex::new(ExclState {
                    seq: VecDeque::new(),
                    default: Vec::new(),
                    status: HolderScanStatus::Ok,
                    calls: Vec::new(),
                }),
            }),
            console: RigFlashConsole::new(),
            real_pty_link: None,
            started: OnceLock::new(),
        }
    }

    /// POSIX 能力（`caps.flash_endpoint=true`）：FakeSerial＋腳本化 PTY master＋FakeClock。
    pub fn posix_fake() -> FlashRig {
        FlashRig::with_caps(true)
    }

    /// Windows 能力（`caps.flash_endpoint=false`）。
    pub fn windows_fake() -> FlashRig {
        FlashRig::with_caps(false)
    }

    /// 真 PTY 端點（P8 Task 13，T9 裁決的 `cap_posix_pty__endpoint_slave_never_exclusive`）：端點的 PTY、raw、symlink
    /// 走 `NativeConsoleFactory`，symlink 建在真檔案系統的 `link`；flasher 以 `sw_pal::testutil::PlainTty` 一般 open
    /// `link`。MCU、holder 掃描、Clock 仍為替身。腳本化 master 的觀察／注入函式（`master_writes`、`flasher_write`、
    /// `pump_once`、`run_until_flashing`、`endpoint_slave_path`、`ttymcu_link_exists` 等）不適用。
    pub fn posix_real_pty(link: &Path) -> FlashRig {
        let mut rig = FlashRig::with_caps(true);
        rig.real_pty_link = Some(link.to_path_buf());
        rig
    }

    /// 偵測候選（純函式測試用）。
    pub fn cand(com: &str, by_id: Option<&str>, command_capable: bool) -> FlashCandidate {
        FlashCandidate {
            com: com.to_string(),
            by_id: by_id.map(str::to_string),
            real_path: None,
            command_capable,
        }
    }

    /// 登記一條 passthrough explicit target（attach 零 TX、停在 ATTACHED）與其 MCU 行為。
    pub fn add_passthrough(&self, com: &str, by_id: &str, mcu: Mcu) {
        assert!(self.started.get().is_none(), "rig 已啟動，不能再加 target");
        lock(&self.targets).push(Target { com: com.into(), by_id: by_id.into(), mcu });
    }

    fn s(&self) -> &Started {
        self.started.get_or_init(|| self.start_now())
    }

    fn start_now(&self) -> Started {
        let targets = std::mem::take(&mut *lock(&self.targets));
        let mut yaml = TEMPLATES.to_string();
        if !targets.is_empty() {
            yaml.push_str("targets:\n");
            for (i, t) in targets.iter().enumerate() {
                yaml.push_str(&format!(
                    "  - {{com: {}, profile: mcu-pt, act_no: {}, alias: m+{}, device_by_id: '{}'}}\n",
                    t.com,
                    i + 1,
                    i + 1,
                    t.by_id
                ));
            }
        }
        let load = load_profiles(PROFILE_DIR, &MemProfiles { yaml }).expect("flash rig YAML 應可載入");
        let serial = FakeSerialBackend::new();
        let mut devs = HashMap::new();
        let mut snapshot = Vec::new();
        for t in &targets {
            let real = format!("/fake/tty-{}", t.com);
            devs.insert(t.com.clone(), serial.add_device(&real));
            snapshot.push((t.by_id.clone(), DeviceInfo { by_id: t.by_id.clone(), real_path: real }));
        }
        let mclock = FakeClock::new();
        let wfs = FakeWalFs::new();
        let wal = Arc::new(WalWriter::open(
            WalConfig { dir: PathBuf::from(WAL_DIR), rotate_bytes: 67_108_864 },
            wfs.clone(),
            mclock.clone(),
        ));
        let warnings = Arc::new(Mutex::new(Vec::new()));
        let sink = warnings.clone();
        let fs = RigFs::new();
        let mgr = SessionManager::new(ManagerDeps {
            serial: serial.clone(),
            uart_excl: Arc::new(NoHolders),
            console: Arc::new(MgrConsole),
            caps: sw_pal::fakes::posix_caps(),
            wal: wal.clone(),
            registry: CommandRegistry::new(),
            state: StateStore::spawn(PathBuf::from(STATE_PATH), fs.clone()),
            loaded: LoadedState::default(),
            load,
            clock: mclock.clone(),
            env: Arc::new(FakeEnv::default()),
            fs,
            warn: Arc::new(move |msg: String| lock(&sink).push(msg)),
        });
        let shutdown = EndpointShutdown::new(Arc::new(NoopWaker));
        let coord = FlashCoordinator::new(
            mgr.clone(),
            Arc::new(McuPatternRegistry::default_registry()),
            self.clock.clone(),
            self.excl.clone(),
            self.console.clone(),
            shutdown.clone(),
        );
        // 第二個 observer（coordinator 之後）：RX 已被 coordinator 看見的計數（settle 用）。
        let seen = Arc::new(SeenCounter::default());
        mgr.add_rx_observer(seen.clone());
        mgr.update_devices(&snapshot);
        mgr.bootstrap_attach();
        for t in &targets {
            let com = t.com.clone();
            wait_real(&format!("{com} ATTACHED"), || state_of(&mgr, &com).as_deref() == Some("ATTACHED"));
        }
        let bank = Arc::new(McuBank {
            devs: Mutex::new(
                targets
                    .iter()
                    .map(|t| McuDev {
                        com: t.com.clone(),
                        dev: devs[&t.com].clone(),
                        mcu: t.mcu.clone(),
                        processed: 0,
                        synced: false,
                        pending: Vec::new(),
                        delayed: Vec::new(),
                        pushed: 0,
                    })
                    .collect(),
            ),
            clock: self.clock.clone(),
        });
        let emu_stop = Arc::new(AtomicBool::new(false));
        let emu = {
            let bank = bank.clone();
            let stop = emu_stop.clone();
            std::thread::Builder::new()
                .name("flash-rig-mcu".into())
                .spawn(move || {
                    while !stop.load(Ordering::SeqCst) {
                        bank.step();
                        std::thread::sleep(Duration::from_millis(1));
                    }
                })
                .expect("MCU 模擬器 thread")
        };
        let mut caps = sw_pal::fakes::posix_caps();
        caps.flash_endpoint = self.flash_caps;
        let (console, link): (Arc<dyn ConsoleTransportFactory>, PathBuf) = match &self.real_pty_link {
            Some(link) => (Arc::new(sw_pal::console_pty::NativeConsoleFactory::new(ConsoleKind::Pty)), link.clone()),
            None => (self.console.clone(), PathBuf::from(LINK)),
        };
        let pal = sw_pal::fakes::platform().console(console).caps(caps).build();
        let endpoint = FlashEndpoint::start(&pal, &link, coord.clone(), ENDPOINT_GRACE)
            .expect("FlashEndpoint::start");
        Started {
            mgr,
            coord,
            shutdown,
            endpoint: Arc::new(Mutex::new(endpoint)),
            devs,
            bank,
            seen,
            emu_stop,
            emu: Mutex::new(Some(emu)),
            wal,
            wfs,
            warnings,
        }
    }

    // ------------------------------------------------------------ 時間推進

    fn io_quiescent(&self) -> bool {
        let s = self.s();
        s.bank.quiescent(&s.seen)
    }

    /// 等所有 clock 使用者回到等待（期限都在未來、登記表穩定）且 I/O 靜止（best-effort，上限 3 s）。
    pub fn settle(&self) {
        let deadline = Instant::now() + SETTLE_MAX;
        loop {
            if self.clock.stable_and_future() && self.io_quiescent() {
                return;
            }
            if Instant::now() >= deadline {
                return;
            }
            std::thread::sleep(Duration::from_millis(1));
        }
    }

    /// 反覆 settle → 推進到最近的等待期限，直到 `cond` 成立（無等待者時不推進時間）。
    pub fn drive_until(&self, what: &str, mut cond: impl FnMut(&FlashRig) -> bool) {
        let _ = self.s();
        let deadline = Instant::now() + DRIVE_MAX;
        loop {
            if cond(self) {
                return;
            }
            assert!(Instant::now() < deadline, "drive 逾時：{what}；warnings={:?}", self.warnings());
            self.settle();
            if cond(self) {
                return;
            }
            let now = self.clock.now();
            match self.clock.min_deadline() {
                Some(min) if min > now => self.clock.advance(Duration::from_nanos(min - now)),
                _ => std::thread::sleep(Duration::from_millis(2)),
            }
        }
    }

    /// 把 coordinator 的時間推進 `secs`：逐一推到期限 ≤ 目標的等待者（每步 settle），最後推到目標。
    pub fn advance_secs(&self, secs: f64) {
        let _ = self.s();
        let target = self.clock.now() + Duration::from_secs_f64(secs).as_nanos() as u64;
        loop {
            self.settle();
            let now = self.clock.now();
            if now >= target {
                break;
            }
            let next = self.clock.min_deadline().filter(|d| *d > now && *d <= target).unwrap_or(target);
            self.clock.advance(Duration::from_nanos(next - now));
        }
        self.settle();
    }

    /// 一次跳過 `d`（不逐步；之後 settle）。
    pub fn advance_raw(&self, d: Duration) {
        let _ = self.s();
        self.clock.advance(d);
        self.settle();
    }

    // ------------------------------------------------------------ flasher／master

    fn master(&self) -> Arc<MasterShared> {
        let _ = self.s();
        self.console.master()
    }

    /// flasher 寫入端點（master 可讀）。
    pub fn flasher_write(&self, b: &[u8]) {
        let m = self.master();
        {
            let mut st = lock(&m.st);
            st.input.extend(b.iter().copied());
            st.last_flasher = b.to_vec();
        }
        m.notify();
    }

    /// flasher 端關閉（master 讀到一次 `Eof`）。
    pub fn flasher_hangup_once(&self) {
        let m = self.master();
        lock(&m.st).eof_once = true;
        m.notify();
    }

    /// 下一次 master 讀取失敗（可讀但 `read` 回錯誤）。
    pub fn master_fail_next_read(&self) {
        let m = self.master();
        lock(&m.st).fail_next_read = true;
        m.notify();
    }

    /// master 的 `wait_readable` 之後一律回錯誤。
    pub fn master_fail_wait(&self) {
        let m = self.master();
        lock(&m.st).fail_wait = true;
        m.notify();
    }

    /// master 寫入分身之後一律回 `Ok(0)`（EAGAIN）。
    pub fn master_writer_zero(&self, on: bool) {
        lock(&self.master().st).zero_writes = on;
    }

    /// 寫給 flasher 的 bytes（每次 `try_write` 一筆；`Ok(0)` 不列入）。
    pub fn master_writes(&self) -> Vec<Vec<u8>> {
        lock(&self.master().st).writes.clone()
    }

    pub fn master_write_attempts(&self) -> u64 {
        lock(&self.master().st).write_attempts
    }

    pub fn master_closed(&self) -> bool {
        lock(&self.master().st).closed
    }

    /// flasher 寫入尚未被端點讀走的 bytes 數。
    pub fn master_unread(&self) -> usize {
        lock(&self.master().st).input.len()
    }

    /// 端點 slave 路徑（`/fake/pts/<n>`）。
    pub fn endpoint_slave_path(&self) -> String {
        let _ = self.s();
        lock(&self.console.slave_path).clone().expect("端點 PTY 尚未建立")
    }

    pub fn pty_creates(&self) -> usize {
        let _ = self.s();
        *lock(&self.console.creates)
    }

    pub fn raw_calls(&self) -> Vec<String> {
        let _ = self.s();
        lock(&self.console.raw).clone()
    }

    // ------------------------------------------------------------ holder

    /// 之後 holder 掃描依序回傳的 pid 列（耗盡後回 default）。
    pub fn holder_sequence(&self, seq: &[Vec<u32>]) {
        lock(&self.excl.st).seq = seq.iter().cloned().collect();
    }

    /// 序列耗盡後的回傳值。
    pub fn holder_default(&self, pids: Vec<u32>) {
        lock(&self.excl.st).default = pids;
    }

    pub fn holder_status(&self, status: HolderScanStatus) {
        lock(&self.excl.st).status = status;
    }

    pub fn holder_calls(&self) -> Vec<String> {
        lock(&self.excl.st).calls.clone()
    }

    // ------------------------------------------------------------ 驅動到狀態

    /// 驅動到 `com` 進 FLASHING、MCU 的回應已回放給 flasher（master 寫入筆數增加），且 holder watch thread 已登記第一次
    /// 等待（其 1800 s 期限自 thread 啟動起算；thread 尚未起跑就推進時間會被它錯過）。
    pub fn run_until_flashing(&self, com: &str) {
        let before = self.master_writes().len();
        self.drive_until(&format!("{com} FLASHING＋ACK 回放"), |r| {
            r.state(com) == "FLASHING" && r.master_writes().len() > before
        });
        wait_real("holder watch thread 登記第一次等待", || self.clock.waiters() > 0);
        self.settle();
    }

    /// 驅動直到下一次 `on_flash_open` 返回。
    pub fn run_detection(&self) {
        let before = self.coord().opens_completed_for_test();
        self.drive_until("on_flash_open 返回", |r| r.coord().opens_completed_for_test() > before);
    }

    /// 等 `com` 到達 `state`（不推進時間）。
    pub fn run_until_state(&self, com: &str, state: &str) {
        wait_real(&format!("{com} state={state}"), || self.state(com) == state);
    }

    /// 等最近一次 flasher 寫入被 pump 讀走並寫到 MCU 線。
    pub fn pump_once(&self) {
        let last = lock(&self.master().st).last_flasher.clone();
        wait_real("pump 讀走 flasher 寫入", || self.master_unread() == 0);
        if !last.is_empty() {
            wait_real("pump 送到 MCU 線", || {
                self.s().devs.values().any(|d| d.tx_since(0).ends_with(&last))
            });
        }
        self.settle();
    }

    // ------------------------------------------------------------ 觀察

    pub fn coord(&self) -> Arc<FlashCoordinator> {
        self.s().coord.clone()
    }

    pub fn mgr(&self) -> Arc<SessionManager> {
        self.s().mgr.clone()
    }

    pub fn endpoint(&self) -> EndpointRef<'_> {
        EndpointRef(lock(&self.s().endpoint))
    }

    pub fn endpoint_started(&self) -> bool {
        lock(&self.s().endpoint).is_some()
    }

    pub fn endpoint_shutdown(&self) -> EndpointShutdown {
        self.s().shutdown.clone()
    }

    pub fn device(&self, com: &str) -> FakeDevice {
        self.s().devs[com].clone()
    }

    pub fn line_control(&self, com: &str) -> Arc<FakeLineControl> {
        self.device(com).line_control_probe()
    }

    pub fn fence(&self, com: &str) -> Arc<Fence> {
        self.s().mgr.fence_for_test(com).expect("session fence")
    }

    pub fn flash_mode_of(&self, com: &str) -> FlashMode {
        self.fence(com).with(|f| f.flash_mode)
    }

    /// `"Off"`／`"Probe(n)"`／`"Flashing"`。
    pub fn flash_mode(&self, com: &str) -> String {
        format!("{:?}", self.flash_mode_of(com))
    }

    pub fn flash_critical(&self, com: &str) -> bool {
        self.s().mgr.flash_critical(com)
    }

    pub fn state(&self, com: &str) -> String {
        state_of(&self.s().mgr, com).unwrap_or_default()
    }

    pub fn events_of(&self, com: &str, kind: &str) -> Vec<FlashEventRecord> {
        self.coord()
            .flash_events_for_test()
            .into_iter()
            .filter(|e| e.com == com && e.kind == kind)
            .collect()
    }

    pub fn last_detect_json(&self) -> String {
        match self.coord().last_detect() {
            Some(d) => dumps_dict(&d.to_py(), &RPC_WIRE),
            None => "null".to_string(),
        }
    }

    pub fn ttymcu_link_exists(&self) -> bool {
        let _ = self.s();
        lock(&self.console.links).contains_key(Path::new(LINK))
    }

    pub fn warnings(&self) -> Vec<String> {
        lock(&self.s().warnings).clone()
    }

    fn wal_rows(&self) -> Vec<PyDict> {
        let s = self.s();
        let bytes = s.wfs.contents(&s.wal.wal_path()).unwrap_or_default();
        String::from_utf8_lossy(&bytes)
            .lines()
            .filter(|l| !l.trim().is_empty())
            .filter_map(|l| match loads(l) {
                Ok(PyValue::Dict(d)) => Some(d),
                _ => None,
            })
            .collect()
    }

    /// `com` 的 WAL TX 列（依序）。
    pub fn wal_tx(&self, com: &str) -> Vec<WalTx> {
        self.wal_rows()
            .iter()
            .filter(|r| {
                r.get("dir").and_then(PyValue::as_str) == Some("TX") && r.get("com").and_then(PyValue::as_str) == Some(com)
            })
            .map(|r| WalTx {
                source: r.get("source").and_then(PyValue::as_str).unwrap_or("").to_string(),
                payload: b64_decode(r.get("payload_b64").and_then(PyValue::as_str).unwrap_or("")),
            })
            .collect()
    }

    /// 送到 MCU 線的每筆 TX（WAL 逐筆；並斷言與 FakeDevice 實際寫出的 bytes 逐位元組相同——外部 oracle）。
    pub fn serial_tx(&self, com: &str) -> Vec<Vec<u8>> {
        self.settle();
        let rows: Vec<Vec<u8>> = self.wal_tx(com).into_iter().map(|r| r.payload).collect();
        let wire = self.device(com).tx_since(0);
        assert_eq!(rows.concat(), wire, "{com}：WAL TX 與 MCU 線實際收到的 bytes 不一致");
        rows
    }

    // ------------------------------------------------------------ 停止

    pub fn stop_endpoint(&self) -> EndpointStopReceipt {
        let ep = lock(&self.s().endpoint).take().expect("端點已停止");
        ep.stop()
    }

    /// 只 drop 端點（不呼叫 stop）。
    pub fn drop_endpoint(&self) {
        let ep = lock(&self.s().endpoint).take().expect("端點已停止");
        drop(ep);
    }

    /// 下一次 `on_flash_open` 登記 `current_run` 之後、第一個 probe 之前，另一 thread 呼叫 `stop()`；本 hook 等到
    /// `EndpointShutdown` 已設定才返回（使 stop 確實先於 probe 生效）。
    pub fn stop_endpoint_on_next_run_registered(&self) -> Stopper {
        let s = self.s();
        let ep = s.endpoint.clone();
        let shutdown = s.shutdown.clone();
        let (tx, rx) = crossbeam_channel::bounded(1);
        s.coord.set_run_registered_hook_for_test(Box::new(move || {
            std::thread::spawn(move || {
                let e = lock(&ep).take().expect("端點");
                let _ = tx.send(e.stop());
            });
            let deadline = Instant::now() + Duration::from_secs(2);
            while !shutdown.is_set() && Instant::now() < deadline {
                std::thread::sleep(Duration::from_millis(1));
            }
        }));
        Stopper { rx }
    }
}

impl Drop for FlashRig {
    fn drop(&mut self) {
        let Some(s) = self.started.get() else {
            return;
        };
        if let Some(ep) = lock(&s.endpoint).take() {
            let _ = ep.stop();
        }
        s.emu_stop.store(true, Ordering::SeqCst);
        if let Some(h) = lock(&s.emu).take() {
            let _ = h.join();
        }
    }
}

fn state_of(mgr: &SessionManager, com: &str) -> Option<String> {
    let sid = mgr.resolve(com)?;
    mgr.list_sessions()
        .into_iter()
        .find(|s| s.get("session_id").and_then(PyValue::as_str) == Some(sid.as_str()))
        .and_then(|s| s.get("state").and_then(PyValue::as_str).map(str::to_string))
}

/// `TermSpeed` 便捷建構（原始 `speed_t` 代碼）。
pub fn speed(code: u32) -> PalResult<TermSpeed> {
    Ok(TermSpeed { ispeed: code, ospeed: code })
}
