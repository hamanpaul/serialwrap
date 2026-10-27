//! console 記憶體替身（P6 Task 8）：`FakeConsoleFactory`、`FakeIo`、`FakeConsoleRef`、記憶體 `ConsoleMux`。
//! 替身不得冒充原生 gate（arch §11.2、OD-08）。
//!
//! 所有替身 io／listener 以行程內唯一 id 登記於全域表；`MuxHandle::Fake(id)` 只能由替身 mux 解讀（native mux 拒絕）。
//! 狀態變化一律喚醒全域條件變數，替身 mux／`read`／`accept` 據此等待。
use std::collections::{BTreeMap, HashMap, HashSet, VecDeque};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Condvar, Mutex, MutexGuard, OnceLock, Weak};
use std::time::{Duration, Instant};

use crate::console_mux::{ConsoleMux, MuxEntry, MuxEvent, MuxHandle, MuxHandleInner, MuxId};
use crate::console_pty::{
    ConsoleIo, ConsoleListener, ConsoleTransportFactory, KeepAlive, PtyPair, PtySlaveRef,
};
use crate::traits::{IoWaker, PalError, PalErrorKind, PalResult, ReadOutcome};
use crate::ConsoleKind;

static NEXT_ID: AtomicU64 = AtomicU64::new(1);

fn next_id() -> u64 {
    NEXT_ID.fetch_add(1, Ordering::Relaxed)
}

fn lock<T>(m: &Mutex<T>) -> MutexGuard<'_, T> {
    m.lock().unwrap_or_else(std::sync::PoisonError::into_inner)
}

/// 全域通知：世代計數＋條件變數（避免遺失喚醒）。
fn notify_cell() -> &'static (Mutex<u64>, Condvar) {
    static CELL: OnceLock<(Mutex<u64>, Condvar)> = OnceLock::new();
    CELL.get_or_init(|| (Mutex::new(0), Condvar::new()))
}

fn bump() {
    let (m, cv) = notify_cell();
    *lock(m) += 1;
    cv.notify_all();
}

fn generation() -> u64 {
    *lock(&notify_cell().0)
}

/// 等到世代變更或期限到（`None` 無限等待）；回 false 表示已逾時。
fn wait_change(seen: u64, deadline: Option<Instant>) -> bool {
    let (m, cv) = notify_cell();
    let mut g = lock(m);
    while *g == seen {
        match deadline {
            None => {
                g = cv
                    .wait(g)
                    .unwrap_or_else(std::sync::PoisonError::into_inner)
            }
            Some(d) => {
                let now = Instant::now();
                if now >= d {
                    return false;
                }
                g = cv
                    .wait_timeout(g, d - now)
                    .unwrap_or_else(std::sync::PoisonError::into_inner)
                    .0;
            }
        }
    }
    true
}

enum Registered {
    Io(Weak<IoShared>),
    Listener(Weak<FactoryShared>),
}

fn registry() -> &'static Mutex<HashMap<u64, Registered>> {
    static REG: OnceLock<Mutex<HashMap<u64, Registered>>> = OnceLock::new();
    REG.get_or_init(|| Mutex::new(HashMap::new()))
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Mode {
    Unbounded,
    Stalled,
    AcceptN(usize),
    FailingRead,
}

struct IoState {
    mode: Mode,
    written: Vec<u8>,
    input: VecDeque<u8>,
    eof: bool,
    closed: bool,
    woken: bool,
    client_id: Option<String>,
    /// PTY 替身的 slave 路徑（`has_external_peer` 依 held 判定）；TCP／com0com 為 `None`。
    pty_path: Option<String>,
}

struct IoShared {
    id: u64,
    st: Mutex<IoState>,
}

impl IoShared {
    fn writable(&self) -> bool {
        let st = lock(&self.st);
        !st.closed
            && match st.mode {
                Mode::Stalled => false,
                Mode::AcceptN(n) => st.written.len() < n,
                Mode::Unbounded | Mode::FailingRead => true,
            }
    }

    fn readable(&self) -> bool {
        let st = lock(&self.st);
        st.mode == Mode::FailingRead || !st.input.is_empty() || st.eof
    }

    /// 非阻塞讀取；`None` 表示目前沒有資料。
    fn read_now(&self, buf: &mut [u8]) -> Option<PalResult<ReadOutcome>> {
        let mut st = lock(&self.st);
        if st.mode == Mode::FailingRead {
            return Some(Err(PalError::plain(
                PalErrorKind::Io,
                "[Errno 5] Input/output error",
            )));
        }
        if !st.input.is_empty() {
            let n = buf.len().min(st.input.len());
            for (dst, src) in buf.iter_mut().zip(st.input.drain(..n)) {
                *dst = src;
            }
            return Some(Ok(ReadOutcome::Data(n)));
        }
        if st.eof {
            return Some(Ok(ReadOutcome::Eof));
        }
        None
    }

    fn write_now(&self, data: &[u8]) -> PalResult<usize> {
        let mut st = lock(&self.st);
        if st.closed {
            return Err(PalError::plain(
                PalErrorKind::BrokenPipe,
                "[Errno 32] Broken pipe",
            ));
        }
        let n = match st.mode {
            Mode::Stalled => 0,
            Mode::AcceptN(cap) => data.len().min(cap.saturating_sub(st.written.len())),
            Mode::Unbounded | Mode::FailingRead => data.len(),
        };
        st.written.extend_from_slice(&data[..n]);
        Ok(n)
    }
}

/// 可注入行為的假 console I/O（停滯、無上限、只收 n bytes、讀取失敗）。drop 視同關閉。
pub struct FakeIo {
    shared: Arc<IoShared>,
}

/// 測試端握有的 console 參照（讀寫出／注入輸入／觀察關閉）。
#[derive(Clone)]
pub struct FakeConsoleRef {
    shared: Arc<IoShared>,
}

impl FakeIo {
    fn with_mode(mode: Mode) -> FakeIo {
        let shared = Arc::new(IoShared {
            id: next_id(),
            st: Mutex::new(IoState {
                mode,
                written: Vec::new(),
                input: VecDeque::new(),
                eof: false,
                closed: false,
                woken: false,
                client_id: None,
                pty_path: None,
            }),
        });
        lock(registry()).insert(shared.id, Registered::Io(Arc::downgrade(&shared)));
        FakeIo { shared }
    }

    /// `try_write` 永遠回 `Ok(0)`（慢速 console）。
    pub fn stalled() -> FakeIo {
        FakeIo::with_mode(Mode::Stalled)
    }

    /// `try_write` 全收。
    pub fn unbounded() -> FakeIo {
        FakeIo::with_mode(Mode::Unbounded)
    }

    /// 總共只收 `n` bytes，之後回 `Ok(0)`。
    pub fn accept_n(n: usize) -> FakeIo {
        FakeIo::with_mode(Mode::AcceptN(n))
    }

    /// `read` 回錯誤（G-01：handler 例外時 console 續行）。
    pub fn failing_read() -> FakeIo {
        FakeIo::with_mode(Mode::FailingRead)
    }

    /// 測試端參照（與此 io 共用狀態）。
    pub fn reference(&self) -> FakeConsoleRef {
        FakeConsoleRef {
            shared: Arc::clone(&self.shared),
        }
    }

    fn set_pty_path(&self, path: &str) {
        lock(&self.shared.st).pty_path = Some(path.to_string());
    }
}

impl Drop for FakeIo {
    fn drop(&mut self) {
        lock(&self.shared.st).closed = true;
        lock(registry()).remove(&self.shared.id);
        bump();
    }
}

impl FakeConsoleRef {
    /// hub 指派的 client_id（測試以 `set_client_id` 或 hub 的 `ConsoleConnected` 事件回填）。
    pub fn client_id(&self) -> String {
        lock(&self.shared.st)
            .client_id
            .clone()
            .expect("FakeConsoleRef：client_id 尚未指派")
    }

    pub fn set_client_id(&self, cid: &str) {
        lock(&self.shared.st).client_id = Some(cid.to_string());
    }

    pub fn written(&self) -> Vec<u8> {
        lock(&self.shared.st).written.clone()
    }

    pub fn written_len(&self) -> usize {
        lock(&self.shared.st).written.len()
    }

    pub fn closed(&self) -> bool {
        lock(&self.shared.st).closed
    }

    /// 對端送入資料（io 端 `read` 可讀）。
    pub fn push_input(&self, b: &[u8]) {
        lock(&self.shared.st).input.extend(b.iter().copied());
        bump();
    }

    /// 對端關閉：輸入讀完後 `read` 回 `Eof`。
    pub fn hangup(&self) {
        lock(&self.shared.st).eof = true;
        bump();
    }
}

struct FakeIoWaker {
    shared: Weak<IoShared>,
}

impl IoWaker for FakeIoWaker {
    fn wake(&self) {
        if let Some(s) = self.shared.upgrade() {
            lock(&s.st).woken = true;
        }
        bump();
    }
}

impl ConsoleIo for FakeIo {
    fn read(&mut self, buf: &mut [u8], timeout: Option<Duration>) -> PalResult<ReadOutcome> {
        let deadline = timeout.map(|t| Instant::now() + t);
        loop {
            let seen = generation();
            {
                let mut st = lock(&self.shared.st);
                if st.woken {
                    st.woken = false;
                    return Ok(ReadOutcome::Woken);
                }
            }
            if let Some(r) = self.shared.read_now(buf) {
                return r;
            }
            if !wait_change(seen, deadline) {
                return Ok(ReadOutcome::TimedOut);
            }
        }
    }

    fn try_write(&mut self, data: &[u8]) -> PalResult<usize> {
        self.shared.write_now(data)
    }

    fn waker(&self) -> Arc<dyn IoWaker> {
        Arc::new(FakeIoWaker {
            shared: Arc::downgrade(&self.shared),
        })
    }

    fn has_external_peer(&self, held: Option<&HashSet<String>>) -> bool {
        match &lock(&self.shared.st).pty_path {
            Some(p) => held.is_none_or(|h| h.contains(p)),
            None => true,
        }
    }

    fn close(self: Box<Self>) {}

    fn mux_handle(&self) -> MuxHandle {
        MuxHandle(MuxHandleInner::Fake(self.shared.id))
    }
}

struct FactoryShared {
    pending: Mutex<VecDeque<FakeIo>>,
}

/// 記憶體 console 工廠（`ConsoleTransportFactory` 替身）。
/// `Clone` 共用全部狀態（P6 Task 9：hub harness 與 `Platform.console` 持有同一個替身）。
#[derive(Clone)]
pub struct FakeConsoleFactory {
    kind: ConsoleKind,
    shared: Arc<FactoryShared>,
    next_port: Arc<AtomicU64>,
    next_pty: Arc<AtomicU64>,
    held: Arc<Mutex<Option<HashSet<String>>>>,
    fail_com0com: Arc<AtomicBool>,
    com0com: Arc<Mutex<Vec<(String, FakeConsoleRef)>>>,
    /// P6 Task 9（#37）：之後幾次 `ConsoleMux::wait` 回錯誤。
    mux_wait_failures: Arc<std::sync::atomic::AtomicUsize>,
}

impl FakeConsoleFactory {
    /// 指定 `kind` 的替身工廠。
    pub fn new(kind: ConsoleKind) -> FakeConsoleFactory {
        FakeConsoleFactory {
            kind,
            shared: Arc::new(FactoryShared {
                pending: Mutex::new(VecDeque::new()),
            }),
            next_port: Arc::new(AtomicU64::new(40000)),
            next_pty: Arc::new(AtomicU64::new(0)),
            held: Arc::new(Mutex::new(None)),
            fail_com0com: Arc::new(AtomicBool::new(false)),
            com0com: Arc::new(Mutex::new(Vec::new())),
            mux_wait_failures: Arc::new(std::sync::atomic::AtomicUsize::new(0)),
        }
    }

    /// 模擬一個 TCP client 連入 listener（下一次 `accept` 取得；全收寫入）。
    pub fn connect(&self) -> FakeConsoleRef {
        self.connect_with(FakeIo::unbounded())
    }

    /// 以指定行為的 io 模擬 TCP client 連入。
    pub fn connect_with(&self, io: FakeIo) -> FakeConsoleRef {
        let r = io.reference();
        lock(&self.shared.pending).push_back(io);
        bump();
        r
    }

    /// 以指定行為的 io 組成 PTY 替身（hub 測試的 `AdoptPty`）。
    pub fn pty_pair_from(&self, io: FakeIo) -> PtyPair {
        let n = self.next_pty.fetch_add(1, Ordering::Relaxed);
        let path = format!("/fake/pts/{n}");
        io.set_pty_path(&path);
        PtyPair {
            master: Box::new(io),
            slave_keepalive: KeepAlive::detached(),
            slave_ref: PtySlaveRef { path: path.clone() },
            slave_path: path,
        }
    }

    /// 下一次 `open_com0com_console` 失敗。
    pub fn fail_next_com0com(&self) {
        self.fail_com0com.store(true, Ordering::SeqCst);
    }

    /// 已開啟的 com0com 替身（port, 參照）。
    pub fn com0com_opened(&self) -> Vec<(String, FakeConsoleRef)> {
        lock(&self.com0com).clone()
    }

    /// P6 Task 9：之後 `n` 次 `ConsoleMux::wait` 回錯誤（#37）。
    pub fn fail_next_mux_waits(&self, n: usize) {
        self.mux_wait_failures.store(n, Ordering::SeqCst);
        bump();
    }

    /// `held_slave_paths` 的回傳值。
    pub fn set_held(&self, held: Option<HashSet<String>>) {
        *lock(&self.held) = held;
    }
}

impl ConsoleTransportFactory for FakeConsoleFactory {
    fn kind(&self) -> ConsoleKind {
        self.kind
    }

    fn create_pty(&self) -> PalResult<PtyPair> {
        Ok(self.pty_pair_from(FakeIo::unbounded()))
    }

    fn bind_tcp_console(&self) -> PalResult<Box<dyn ConsoleListener>> {
        let id = next_id();
        lock(registry()).insert(id, Registered::Listener(Arc::downgrade(&self.shared)));
        Ok(Box::new(FakeListener {
            id,
            shared: Arc::clone(&self.shared),
            endpoint: format!("127.0.0.1:{}", self.next_port.fetch_add(1, Ordering::Relaxed)),
        }))
    }

    fn open_com0com_console(&self, port: &str) -> PalResult<Box<dyn ConsoleIo>> {
        if self.fail_com0com.swap(false, Ordering::SeqCst) {
            return Err(PalError::plain(
                PalErrorKind::AccessDenied,
                "[WinError 5] Access is denied.",
            ));
        }
        let io = FakeIo::unbounded();
        lock(&self.com0com).push((port.to_string(), io.reference()));
        Ok(Box::new(io))
    }

    fn held_slave_paths(&self) -> Option<HashSet<String>> {
        lock(&self.held).clone()
    }

    fn new_mux(&self) -> PalResult<Box<dyn ConsoleMux>> {
        Ok(Box::new(FakeConsoleMux {
            entries: BTreeMap::new(),
            waker: Arc::new(FakeMuxWaker {
                flag: AtomicBool::new(false),
            }),
            wait_failures: Arc::clone(&self.mux_wait_failures),
        }))
    }
}

struct FakeListener {
    id: u64,
    shared: Arc<FactoryShared>,
    endpoint: String,
}

impl Drop for FakeListener {
    fn drop(&mut self) {
        lock(registry()).remove(&self.id);
    }
}

static NEXT_PEER_PORT: AtomicU64 = AtomicU64::new(50000);

impl ConsoleListener for FakeListener {
    fn endpoint(&self) -> String {
        self.endpoint.clone()
    }

    fn accept(&mut self, timeout: Duration) -> PalResult<Option<(Box<dyn ConsoleIo>, String)>> {
        let deadline = Instant::now() + timeout;
        loop {
            let seen = generation();
            if let Some(io) = lock(&self.shared.pending).pop_front() {
                let peer = format!("127.0.0.1:{}", NEXT_PEER_PORT.fetch_add(1, Ordering::Relaxed));
                return Ok(Some((Box::new(io), peer)));
            }
            if !wait_change(seen, Some(deadline)) {
                return Ok(None);
            }
        }
    }

    fn mux_handle(&self) -> MuxHandle {
        MuxHandle(MuxHandleInner::Fake(self.id))
    }
}

struct FakeMuxWaker {
    flag: AtomicBool,
}

impl IoWaker for FakeMuxWaker {
    fn wake(&self) {
        self.flag.store(true, Ordering::SeqCst);
        bump();
    }
}

enum FakeSlot {
    Io(Box<dyn ConsoleIo>, Arc<IoShared>),
    Listener(Box<dyn ConsoleListener>, Arc<FactoryShared>),
}

struct FakeEntry {
    write_interest: bool,
    slot: FakeSlot,
}

/// 記憶體 `ConsoleMux`：以全域條件變數等待替身狀態變化。
struct FakeConsoleMux {
    entries: BTreeMap<MuxId, FakeEntry>,
    waker: Arc<FakeMuxWaker>,
    wait_failures: Arc<std::sync::atomic::AtomicUsize>,
}

fn foreign() -> PalError {
    PalError::plain(PalErrorKind::Unsupported, "ConsoleMux：handle 不屬於此 PAL")
}

fn resolve(handle: MuxHandle) -> PalResult<Registered> {
    let MuxHandleInner::Fake(id) = handle.0 else {
        return Err(foreign());
    };
    match lock(registry()).get(&id) {
        Some(Registered::Io(w)) => Ok(Registered::Io(w.clone())),
        Some(Registered::Listener(w)) => Ok(Registered::Listener(w.clone())),
        None => Err(foreign()),
    }
}

impl FakeConsoleMux {
    fn io_shared(&self, id: MuxId) -> PalResult<Arc<IoShared>> {
        match self.entries.get(&id) {
            Some(FakeEntry {
                slot: FakeSlot::Io(_, s),
                ..
            }) => Ok(Arc::clone(s)),
            _ => Err(PalError::plain(
                PalErrorKind::NotFound,
                format!("ConsoleMux：未登記的 io id {id}"),
            )),
        }
    }

    fn insert(&mut self, id: MuxId, slot: FakeSlot) -> PalResult<()> {
        if self.entries.contains_key(&id) {
            return Err(PalError::plain(
                PalErrorKind::InvalidInput,
                format!("ConsoleMux：id {id} 已登記"),
            ));
        }
        self.entries.insert(
            id,
            FakeEntry {
                write_interest: false,
                slot,
            },
        );
        Ok(())
    }

    fn ready(&self) -> Vec<MuxEvent> {
        let mut out = Vec::new();
        if self.waker.flag.swap(false, Ordering::SeqCst) {
            out.push(MuxEvent::Woken);
        }
        for (id, e) in &self.entries {
            match &e.slot {
                FakeSlot::Io(_, s) => {
                    if s.readable() {
                        out.push(MuxEvent::Readable(*id));
                    }
                    if e.write_interest && s.writable() {
                        out.push(MuxEvent::Writable(*id));
                    }
                }
                FakeSlot::Listener(_, f) => {
                    if !lock(&f.pending).is_empty() {
                        out.push(MuxEvent::Acceptable(*id));
                    }
                }
            }
        }
        out
    }
}

impl ConsoleMux for FakeConsoleMux {
    fn add_io(&mut self, id: MuxId, io: Box<dyn ConsoleIo>) -> PalResult<()> {
        let Registered::Io(w) = resolve(io.mux_handle())? else {
            return Err(foreign());
        };
        let shared = w.upgrade().ok_or_else(foreign)?;
        self.insert(id, FakeSlot::Io(io, shared))
    }

    fn add_listener(&mut self, id: MuxId, l: Box<dyn ConsoleListener>) -> PalResult<()> {
        let Registered::Listener(w) = resolve(l.mux_handle())? else {
            return Err(foreign());
        };
        let shared = w.upgrade().ok_or_else(foreign)?;
        self.insert(id, FakeSlot::Listener(l, shared))
    }

    fn remove(&mut self, id: MuxId) -> Option<MuxEntry> {
        self.entries.remove(&id).map(|e| match e.slot {
            FakeSlot::Io(io, _) => MuxEntry::Io(io),
            FakeSlot::Listener(l, _) => MuxEntry::Listener(l),
        })
    }

    fn set_write_interest(&mut self, id: MuxId, on: bool) {
        if let Some(e) = self.entries.get_mut(&id) {
            e.write_interest = on;
        }
    }

    fn wait(&mut self, timeout: Duration) -> PalResult<Vec<MuxEvent>> {
        if self
            .wait_failures
            .fetch_update(Ordering::SeqCst, Ordering::SeqCst, |n| n.checked_sub(1))
            .is_ok()
        {
            return Err(PalError::plain(
                PalErrorKind::Io,
                "[Errno 9] Bad file descriptor",
            ));
        }
        let deadline = Instant::now() + timeout;
        loop {
            let seen = generation();
            let evs = self.ready();
            if !evs.is_empty() {
                return Ok(evs);
            }
            if !wait_change(seen, Some(deadline)) {
                return Ok(Vec::new());
            }
        }
    }

    fn read(&mut self, id: MuxId, buf: &mut [u8]) -> PalResult<ReadOutcome> {
        let s = self.io_shared(id)?;
        let r = s.read_now(buf);
        r.unwrap_or(Ok(ReadOutcome::TimedOut))
    }

    fn try_write(&mut self, id: MuxId, data: &[u8]) -> PalResult<usize> {
        self.io_shared(id)?.write_now(data)
    }

    fn accept(&mut self, id: MuxId) -> PalResult<Option<(Box<dyn ConsoleIo>, String)>> {
        match self.entries.get_mut(&id) {
            Some(FakeEntry {
                slot: FakeSlot::Listener(l, _),
                ..
            }) => l.accept(Duration::ZERO),
            _ => Err(PalError::plain(
                PalErrorKind::NotFound,
                format!("ConsoleMux：未登記的 listener id {id}"),
            )),
        }
    }

    fn waker(&self) -> Arc<dyn IoWaker> {
        Arc::clone(&self.waker) as Arc<dyn IoWaker>
    }
}
