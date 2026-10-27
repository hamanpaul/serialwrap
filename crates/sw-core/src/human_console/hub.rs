//! 每 session 一條不阻塞的 ConsoleHub thread（P6 Task 9；07 D-05、D-12、D-17、D-18）。
//!
//! RX fan-out、Telnet 協商、peer 掃描、跨 `conn_epoch` 保留 console；平台差異只在 `sw-pal` 的
//! `ConsoleTransportFactory`／`ConsoleMux`。hub 對 mailbox 一律 `try_send`（滿載進有界 `pending` 並暫停讀取）；
//! actor 對 hub 只 `try_send` 或寫 `critical` 旁路，雙方都不阻塞。

use std::collections::hash_map::RandomState;
use std::collections::{BTreeMap, HashMap, HashSet, VecDeque};
use std::hash::BuildHasher;
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex, OnceLock};
use std::thread::JoinHandle;
use std::time::Duration;

use crossbeam_channel::{bounded, Receiver, Sender, TrySendError};
use sw_pal::console_mux::{ConsoleMux, MuxEntry, MuxEvent, MuxId};
use sw_pal::console_pty::{ConsoleListener, ConsoleTransportFactory, KeepAlive};
use sw_pal::traits::{Clock, IoWaker, ReadOutcome};
use sw_pal::{ConsoleKind, Platform, PtyPair};
use sw_proto::constants::telnet::TELNET_GREETING;
use sw_proto::constants::{
    CONSOLE_CONTROL_MAX_BYTES, CONSOLE_HUB_CTRL_QUEUE, CONSOLE_HUB_LOOP_QUOTA,
    CONSOLE_HUB_PENDING_EVENTS, CONSOLE_HUB_RX_QUEUE, CONSOLE_OUT_RING_BYTES, HUMAN_PEER_GRACE_S,
    STALE_CONSOLE_GRACE_S, TELNET_CONSOLES_PER_SESSION,
};

use crate::human_console::out_ring::{OutItem, OutRing, PushOutcome};
use crate::human_console::{ConsoleScanSnapshot, ConsoleTransportKind, ScanSource};
use crate::mailbox::{ControlEvent, DataEvent};
use crate::telnet_console::{escape_iac, TelnetFilter};
use crate::Mono;

/// hub→mailbox control lane 送端。
pub type ControlSender = Sender<ControlEvent>;
/// hub→mailbox data lane 送端（`ConsoleInputBytes`／`ConsoleDisconnected` 同一條 FIFO，07 D-17(3)）。
pub type DataSender = Sender<DataEvent>;

/// `ConsoleIo` 每次讀取的上限（`SERIAL_READ_CHUNK`）。
const SERIAL_READ_CHUNK: usize = 8192;
/// `HubStatus.warnings` 保留筆數。
const HUB_WARNINGS_MAX: usize = 256;
/// `mux.wait` 錯誤後的退避（`uart_io.py:1386-1390`，parity-amendments #37）。
const MUX_ERROR_BACKOFF: Duration = Duration::from_millis(20);

/// actor→hub 控制命令（bounded `CONSOLE_HUB_CTRL_QUEUE`）。
pub enum HubCmd {
    AdoptPty {
        client_id: String,
        client_lifetime: u64,
        label: String,
        pair: PtyPair,
    },
    /// TcpTelnet：bridge 起來時。
    StartListener,
    /// bridge 停止時（ephemeral port，UIO-12.8-c）。
    StopListener,
    /// 07 D-12。
    OpenCom0com {
        port: String,
    },
    Out {
        client_id: String,
        item: OutItem,
    },
    /// 測試用：hub 在下一輪頂端（取 `input_epoch` 快照之後、`mux.wait` 之前）停住直到 gate 釋放。
    #[cfg(feature = "test-fakes")]
    TestStall(Arc<StallGate>),
}

/// 測試用：`HubCmd::TestStall` 的閘門。
#[cfg(feature = "test-fakes")]
#[derive(Default)]
pub struct StallGate {
    pub released: Mutex<bool>,
    pub cond: std::sync::Condvar,
    /// hub 已進入停住狀態。
    pub entered: AtomicBool,
}

/// channel 滿時的關閉旁路（07 D-17(4)）。
#[derive(Default)]
pub struct HubCritical {
    /// ≤ console 數。
    pub close: Vec<String>,
    pub close_all: bool,
    pub shutdown: bool,
}

/// hub 對外可讀的狀態。
pub struct HubStatus {
    pub endpoint: Mutex<Option<String>>,
    pub listener_up: AtomicBool,
    pub rx_dropped_chunks: AtomicU64,
    pub out_dropped: AtomicU64,
    /// reader 端：rx channel 目前是否已滿（每次由未滿轉滿寫一筆 WARNING，D-18）。
    pub rx_full: AtomicBool,
    /// hub 迴圈輪數（測試與診斷）。
    pub loops: AtomicU64,
    /// hub 的 `pending` 長度（測試與診斷）。
    pub pending: AtomicU64,
    /// 最近的 WARNING（上限 `HUB_WARNINGS_MAX`，同時寫 stderr）。
    pub warnings: Mutex<VecDeque<String>>,
    /// 各 console 出向 ring 的 Display bytes（每輪更新，診斷用）。
    pub ring_display: Mutex<HashMap<String, usize>>,
    /// WARNING 模板用的 COM（`spawn_hub` 設定）。
    pub com: OnceLock<String>,
    /// hub thread 已結束。
    pub exited: AtomicBool,
    /// 測試用：下一個 console 輸入處理 panic（#89）。
    #[cfg(feature = "test-fakes")]
    pub panic_next_input: AtomicBool,
}

impl Default for HubStatus {
    fn default() -> Self {
        HubStatus::new()
    }
}

impl HubStatus {
    pub fn new() -> HubStatus {
        HubStatus {
            endpoint: Mutex::new(None),
            listener_up: AtomicBool::new(false),
            rx_dropped_chunks: AtomicU64::new(0),
            out_dropped: AtomicU64::new(0),
            rx_full: AtomicBool::new(false),
            loops: AtomicU64::new(0),
            pending: AtomicU64::new(0),
            warnings: Mutex::new(VecDeque::new()),
            ring_display: Mutex::new(HashMap::new()),
            com: OnceLock::new(),
            exited: AtomicBool::new(false),
            #[cfg(feature = "test-fakes")]
            panic_next_input: AtomicBool::new(false),
        }
    }

    /// POSIX：primary 的 vtty 路徑存在；TcpTelnet：`listener_up`（06 §7.7）。
    pub fn vtty_alive(
        &self,
        primary_vtty: Option<&str>,
        fs_exists: &dyn Fn(&str) -> bool,
        kind: ConsoleKind,
    ) -> bool {
        match kind {
            ConsoleKind::Pty => primary_vtty.is_some_and(fs_exists),
            ConsoleKind::TcpTelnet => self.listener_up.load(Ordering::Acquire),
        }
    }

    fn com(&self) -> &str {
        self.com.get().map(String::as_str).unwrap_or("")
    }

    /// 記錄並輸出一筆 WARNING。
    pub fn warn(&self, line: String) {
        eprintln!("{line}");
        let mut w = self.warnings.lock().unwrap_or_else(|e| e.into_inner());
        if w.len() >= HUB_WARNINGS_MAX {
            w.pop_front();
        }
        w.push_back(line);
    }
}

/// actor 持有的 hub 句柄；所有方法永不阻塞。
pub struct HubHandle {
    /// bounded(`CONSOLE_HUB_CTRL_QUEUE`)。
    pub cmd: Sender<HubCmd>,
    /// bounded(`CONSOLE_HUB_RX_QUEUE`)。
    pub rx: Sender<Arc<[u8]>>,
    pub critical: Arc<Mutex<HubCritical>>,
    pub waker: Arc<dyn IoWaker>,
    pub status: Arc<HubStatus>,
    /// = `CoreDeps.input_epoch_pub`。
    pub input_epoch: Arc<AtomicU64>,
    /// hub thread（`shutdown` 後可 join）。
    pub join: Mutex<Option<JoinHandle<()>>>,
}

impl HubHandle {
    fn critical(&self, f: impl FnOnce(&mut HubCritical)) {
        f(&mut self.critical.lock().unwrap_or_else(|e| e.into_inner()));
        self.waker.wake();
    }

    /// 寫 critical＋wake，永不阻塞。
    pub fn close(&self, cid: &str) {
        self.critical(|c| {
            if !c.close.iter().any(|x| x == cid) {
                c.close.push(cid.to_string());
            }
        });
    }
    pub fn close_all(&self) {
        self.critical(|c| c.close_all = true);
    }
    pub fn shutdown(&self) {
        self.critical(|c| c.shutdown = true);
    }

    /// reader 用的 tap（clone `rx`／`waker`／`status`）。
    pub fn tap(&self) -> HubTap {
        HubTap {
            rx: self.rx.clone(),
            waker: Arc::clone(&self.waker),
            status: Arc::clone(&self.status),
        }
    }

    /// actor 的 `ConsoleOut`：`try_send`，滿載只丟棄並計數（D-17(4)）。
    pub fn out(&self, client_id: String, item: OutItem) {
        match self.cmd.try_send(HubCmd::Out { client_id, item }) {
            Ok(()) => self.waker.wake(),
            Err(_) => {
                self.status.out_dropped.fetch_add(1, Ordering::AcqRel);
            }
        }
    }
}

/// reader 對 hub 的 RX 入口（增補 #21）：WAL 之後 `try_send`，滿載只計數＋WARNING，永不阻塞。
pub struct HubTap {
    pub rx: Sender<Arc<[u8]>>,
    pub waker: Arc<dyn IoWaker>,
    pub status: Arc<HubStatus>,
}

impl HubTap {
    /// 回 true＝已送進 hub。
    pub fn push(&self, bytes: &[u8], warning_sink: &dyn Fn(&str)) -> bool {
        match self.rx.try_send(Arc::from(bytes)) {
            Ok(()) => {
                self.status.rx_full.store(false, Ordering::Release);
                self.waker.wake();
                true
            }
            Err(TrySendError::Full(_)) => {
                let dropped = self.status.rx_dropped_chunks.fetch_add(1, Ordering::AcqRel) + 1;
                if !self.status.rx_full.swap(true, Ordering::AcqRel) {
                    let line = format!(
                        "console fan-out 佇列已滿，丟棄顯示資料（com={} dropped={dropped}）",
                        self.status.com()
                    );
                    warning_sink(&line);
                }
                false
            }
            Err(TrySendError::Disconnected(_)) => false,
        }
    }
}

/// thread 名 `serialwrap-console-<COM>`。
pub fn spawn_hub(
    com: String,
    platform: &Platform,
    control: ControlSender,
    data: DataSender,
    clock: Arc<dyn Clock>,
    input_epoch: Arc<AtomicU64>,
) -> HubHandle {
    spawn_hub_with(com, Arc::clone(&platform.console), control, data, clock, input_epoch)
        .expect("ConsoleMux 建立失敗")
}

/// 同 `spawn_hub`，直接取 console 工廠；`new_mux` 失敗（例如 Windows WSAPoll 尚未實作，LOCAL-WINDOWS-TODO）回 `Err`。
pub fn spawn_hub_with(
    com: String,
    factory: Arc<dyn sw_pal::console_pty::ConsoleTransportFactory>,
    control: ControlSender,
    data: DataSender,
    clock: Arc<dyn Clock>,
    input_epoch: Arc<AtomicU64>,
) -> sw_pal::traits::PalResult<HubHandle> {
    let mux = factory.new_mux()?;
    let waker = mux.waker();
    let (cmd_tx, cmd_rx) = bounded::<HubCmd>(CONSOLE_HUB_CTRL_QUEUE);
    let (rx_tx, rx_rx) = bounded::<Arc<[u8]>>(CONSOLE_HUB_RX_QUEUE);
    let critical = Arc::new(Mutex::new(HubCritical::default()));
    let status = Arc::new(HubStatus::new());
    let _ = status.com.set(com.clone());
    let now = clock.mono_ns();
    let hub = Hub {
        com: com.clone(),
        factory,
        mux,
        clock,
        control,
        data,
        cmd_rx,
        rx_rx,
        critical: Arc::clone(&critical),
        status: Arc::clone(&status),
        input_epoch: Arc::clone(&input_epoch),
        clients: BTreeMap::new(),
        by_cid: HashMap::new(),
        listener: None,
        parked_listener: None,
        next_id: 1,
        pending: VecDeque::new(),
        pending_full_warned: false,
        next_scan: now.saturating_add(secs_ns(HUMAN_PEER_GRACE_S)),
        stream_lifetime: 1 << 40,
        id_seq: 0,
        id_hash: RandomState::new(),
        #[cfg(feature = "test-fakes")]
        stall_next: None,
    };
    let join = std::thread::Builder::new()
        .name(format!("serialwrap-console-{com}"))
        .spawn(move || hub.run())
        .expect("建立 ConsoleHub thread");
    Ok(HubHandle {
        cmd: cmd_tx,
        rx: rx_tx,
        critical,
        waker,
        status,
        input_epoch,
        join: Mutex::new(Some(join)),
    })
}

fn secs_ns(s: f64) -> u64 {
    (s.max(0.0) * 1_000_000_000.0) as u64
}

struct HubClient {
    cid: String,
    lifetime: u64,
    kind: ConsoleTransportKind,
    ring: OutRing,
    telnet: Option<TelnetFilter>,
    attached_at: Mono,
    pty_path: Option<String>,
    _keepalive: Option<KeepAlive>,
}

enum Pending {
    Data(DataEvent),
    Control(ControlEvent),
}

struct Hub {
    com: String,
    factory: Arc<dyn ConsoleTransportFactory>,
    mux: Box<dyn ConsoleMux>,
    clock: Arc<dyn Clock>,
    control: ControlSender,
    data: DataSender,
    cmd_rx: Receiver<HubCmd>,
    rx_rx: Receiver<Arc<[u8]>>,
    critical: Arc<Mutex<HubCritical>>,
    status: Arc<HubStatus>,
    input_epoch: Arc<AtomicU64>,
    clients: BTreeMap<MuxId, HubClient>,
    by_cid: HashMap<String, MuxId>,
    listener: Option<MuxId>,
    /// 達 Telnet 上限時暫停 accept：listener 自 mux 移出暫存於此（SC-31(d)）。
    parked_listener: Option<Box<dyn ConsoleListener>>,
    next_id: MuxId,
    pending: VecDeque<Pending>,
    pending_full_warned: bool,
    next_scan: Mono,
    stream_lifetime: u64,
    id_seq: u64,
    id_hash: RandomState,
    #[cfg(feature = "test-fakes")]
    stall_next: Option<Arc<StallGate>>,
}

impl Hub {
    fn warn(&self, line: String) {
        self.status.warn(line);
    }

    fn run(mut self) {
        loop {
            // (a) D-17(5)：每輪 wait 之前取一次 input_epoch 快照。
            let epoch_snapshot = self.input_epoch.load(Ordering::Acquire);
            #[cfg(feature = "test-fakes")]
            if let Some(gate) = self.stall_next.take() {
                gate.entered.store(true, Ordering::Release);
                let mut released = gate.released.lock().unwrap_or_else(|e| e.into_inner());
                while !*released {
                    released = gate.cond.wait(released).unwrap_or_else(|e| e.into_inner());
                }
            }
            // (b)
            let now = self.clock.mono_ns();
            let to_scan = Duration::from_nanos(self.next_scan.saturating_sub(now));
            let idle = if self.pending.is_empty() {
                Duration::from_millis(200)
            } else {
                Duration::from_millis(10)
            };
            // D-17(1)：pending 已滿時暫停讀取，背壓留在 OS；不呼叫 level-triggered 的 `mux.wait`（fd 仍可讀時會立即
            // 返回而空轉），改以 10 ms 逾時重試，期間照常處理 critical、cmd、rx 與 pending flush。
            if self.pending.len() >= CONSOLE_HUB_PENDING_EVENTS {
                std::thread::sleep(to_scan.min(Duration::from_millis(10)));
                if self.process_critical() {
                    break;
                }
                for _ in 0..CONSOLE_HUB_LOOP_QUOTA {
                    match self.cmd_rx.try_recv() {
                        Ok(cmd) => self.process_cmd(cmd),
                        Err(_) => break,
                    }
                }
                for _ in 0..CONSOLE_HUB_LOOP_QUOTA {
                    match self.rx_rx.try_recv() {
                        Ok(chunk) => self.fanout(&chunk),
                        Err(_) => break,
                    }
                }
                self.maybe_scan();
                self.flush_pending();
                self.flush_rings();
                self.publish();
                self.status.loops.fetch_add(1, Ordering::AcqRel);
                continue;
            }
            let events = match self.mux.wait(to_scan.min(idle)) {
                Ok(events) => events,
                Err(_) => {
                    if self.take_critical_shutdown() {
                        break;
                    }
                    std::thread::sleep(MUX_ERROR_BACKOFF);
                    self.status.loops.fetch_add(1, Ordering::AcqRel);
                    continue;
                }
            };
            // (c) critical 旁路
            if self.process_critical() {
                break;
            }
            // (d) cmd 與 rx 各最多 quota 筆
            for _ in 0..CONSOLE_HUB_LOOP_QUOTA {
                match self.cmd_rx.try_recv() {
                    Ok(cmd) => self.process_cmd(cmd),
                    Err(_) => break,
                }
            }
            for _ in 0..CONSOLE_HUB_LOOP_QUOTA {
                match self.rx_rx.try_recv() {
                    Ok(chunk) => self.fanout(&chunk),
                    Err(_) => break,
                }
            }
            // (e) mux 事件
            for ev in events {
                self.process_mux_event(ev, epoch_snapshot);
            }
            // peer scan（以注入的 Clock 計算到期）
            self.maybe_scan();
            // (f) pending FIFO
            self.flush_pending();
            // (g) 出向 ring
            self.flush_rings();
            self.publish();
            self.status.loops.fetch_add(1, Ordering::AcqRel);
        }
        self.close_everything();
        if let Some(id) = self.listener.take() {
            let _ = self.mux.remove(id);
        }
        self.status.listener_up.store(false, Ordering::Release);
        self.status.exited.store(true, Ordering::Release);
    }

    fn take_critical_shutdown(&self) -> bool {
        self.critical
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .shutdown
    }

    /// 回 true＝shutdown。
    fn process_critical(&mut self) -> bool {
        let taken = std::mem::take(&mut *self.critical.lock().unwrap_or_else(|e| e.into_inner()));
        if taken.shutdown {
            return true;
        }
        if taken.close_all {
            self.close_everything();
        }
        for cid in taken.close {
            if let Some(id) = self.by_cid.get(&cid).copied() {
                self.remove_client(id, false);
            }
        }
        false
    }

    fn close_everything(&mut self) {
        let ids: Vec<MuxId> = self.clients.keys().copied().collect();
        for id in ids {
            self.remove_client(id, false);
        }
    }

    fn alloc_id(&mut self) -> MuxId {
        let id = self.next_id;
        self.next_id += 1;
        id
    }

    /// `uuid.uuid4().hex[:12]` 等價的 12-hex client id。
    fn new_client_id(&mut self) -> String {
        self.id_seq += 1;
        loop {
            let v = self.id_hash.hash_one((self.id_seq, self.clock.mono_ns()));
            let cid = format!("{:012x}", v & 0xffff_ffff_ffff);
            if !self.by_cid.contains_key(&cid) {
                return cid;
            }
            self.id_seq += 1;
        }
    }

    fn insert_client(&mut self, id: MuxId, client: HubClient) {
        self.by_cid.insert(client.cid.clone(), id);
        self.clients.insert(id, client);
    }

    /// 移除並關閉 console；`notify` 時以 data lane 送 `ConsoleDisconnected`（與輸入同序）。
    fn remove_client(&mut self, id: MuxId, notify: bool) {
        let Some(client) = self.clients.remove(&id) else {
            return;
        };
        self.by_cid.remove(&client.cid);
        if let Some(MuxEntry::Io(io)) = self.mux.remove(id) {
            io.close();
        }
        if notify {
            self.pending
                .push_back(Pending::Data(DataEvent::ConsoleDisconnected {
                    client_id: client.cid.clone(),
                    client_lifetime: client.lifetime,
                }));
        }
        self.status
            .ring_display
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .remove(&client.cid);
        self.maybe_resume_accept();
    }

    fn tcp_count(&self) -> usize {
        self.clients
            .values()
            .filter(|c| c.kind == ConsoleTransportKind::TcpTelnet)
            .count()
    }

    fn process_cmd(&mut self, cmd: HubCmd) {
        match cmd {
            HubCmd::AdoptPty {
                client_id,
                client_lifetime,
                label: _,
                pair,
            } => {
                let id = self.alloc_id();
                let PtyPair {
                    master,
                    slave_keepalive,
                    slave_path,
                    ..
                } = pair;
                if let Err(e) = self.mux.add_io(id, master) {
                    self.warn(format!(
                        "console RX handler 例外（com={}），略過此塊、console 續行：{}",
                        self.com, e.py_message
                    ));
                    return;
                }
                let now = self.clock.mono_ns();
                self.insert_client(
                    id,
                    HubClient {
                        cid: client_id,
                        lifetime: client_lifetime,
                        kind: ConsoleTransportKind::Pty,
                        ring: OutRing::new(CONSOLE_OUT_RING_BYTES),
                        telnet: None,
                        attached_at: now,
                        pty_path: Some(slave_path),
                        _keepalive: Some(slave_keepalive),
                    },
                );
            }
            HubCmd::StartListener => self.start_listener(),
            HubCmd::StopListener => {
                if let Some(id) = self.listener.take() {
                    let _ = self.mux.remove(id);
                }
                self.parked_listener = None;
                self.status.listener_up.store(false, Ordering::Release);
            }
            HubCmd::OpenCom0com { port } => match self.factory.open_com0com_console(&port) {
                Ok(io) => {
                    let id = self.alloc_id();
                    if let Err(e) = self.mux.add_io(id, io) {
                        self.warn(format!(
                            "serial console 開啟失敗（com={} port={port}）：{}",
                            self.com, e.py_message
                        ));
                        return;
                    }
                    let cid = self.new_client_id();
                    self.stream_lifetime += 1;
                    let lifetime = self.stream_lifetime;
                    let now = self.clock.mono_ns();
                    self.insert_client(
                        id,
                        HubClient {
                            cid: cid.clone(),
                            lifetime,
                            kind: ConsoleTransportKind::Com0com,
                            ring: OutRing::new(CONSOLE_OUT_RING_BYTES),
                            telnet: None,
                            attached_at: now,
                            pty_path: None,
                            _keepalive: None,
                        },
                    );
                    self.pending
                        .push_back(Pending::Control(ControlEvent::ConsoleConnected {
                            client_id: cid,
                            client_lifetime: lifetime,
                            label: format!("serial:{port}"),
                            vtty: port,
                            kind: ConsoleTransportKind::Com0com,
                        }));
                }
                Err(e) => self.warn(format!(
                    "serial console 開啟失敗（com={} port={port}）：{}",
                    self.com, e.py_message
                )),
            },
            HubCmd::Out { client_id, item } => {
                let Some(id) = self.by_cid.get(&client_id).copied() else {
                    return;
                };
                let telnet = self.clients.get(&id).is_some_and(|c| c.telnet.is_some());
                let item = match item {
                    OutItem::Display {
                        bytes,
                        iac_escaped: false,
                    } if telnet => OutItem::Display {
                        bytes: escape_iac(&bytes).into_owned(),
                        iac_escaped: true,
                    },
                    other => other,
                };
                self.push_item(id, item);
            }
            #[cfg(feature = "test-fakes")]
            HubCmd::TestStall(gate) => self.stall_next = Some(gate),
        }
    }

    fn start_listener(&mut self) {
        if self.listener.is_some() {
            return;
        }
        match self.factory.bind_tcp_console() {
            Ok(listener) => {
                let endpoint = listener.endpoint();
                let id = self.alloc_id();
                if self.tcp_count() >= TELNET_CONSOLES_PER_SESSION {
                    self.parked_listener = Some(listener);
                } else if let Err(e) = self.mux.add_listener(id, listener) {
                    self.warn(format!(
                        "console listener 建立失敗（com={}）：{}",
                        self.com, e.py_message
                    ));
                    return;
                }
                self.listener = Some(id);
                *self
                    .status
                    .endpoint
                    .lock()
                    .unwrap_or_else(|e| e.into_inner()) = Some(endpoint);
                self.status.listener_up.store(true, Ordering::Release);
            }
            Err(e) => self.warn(format!(
                "console listener 建立失敗（com={}）：{}",
                self.com, e.py_message
            )),
        }
    }

    /// 達上限時把 listener 移出 mux（不再 Acceptable）並寫一筆 WARNING（D-18 pause_accept）。
    fn maybe_pause_accept(&mut self) {
        let Some(id) = self.listener else {
            return;
        };
        if self.parked_listener.is_some() || self.tcp_count() < TELNET_CONSOLES_PER_SESSION {
            return;
        }
        if let Some(MuxEntry::Listener(l)) = self.mux.remove(id) {
            self.parked_listener = Some(l);
            self.warn(format!(
                "console 數已達 32，暫停接受新的 Telnet 連線（com={}）",
                self.com
            ));
        }
    }

    fn maybe_resume_accept(&mut self) {
        let Some(id) = self.listener else {
            return;
        };
        if self.tcp_count() >= TELNET_CONSOLES_PER_SESSION {
            return;
        }
        if let Some(l) = self.parked_listener.take() {
            if self.mux.add_listener(id, l).is_err() {
                self.listener = None;
                self.status.listener_up.store(false, Ordering::Release);
            }
        }
    }

    fn accept(&mut self, listener_id: MuxId) {
        loop {
            if self.tcp_count() >= TELNET_CONSOLES_PER_SESSION {
                self.maybe_pause_accept();
                return;
            }
            let accepted = match self.mux.accept(listener_id) {
                Ok(Some(pair)) => pair,
                Ok(None) | Err(_) => return,
            };
            let (io, peer) = accepted;
            let id = self.alloc_id();
            if self.mux.add_io(id, io).is_err() {
                continue;
            }
            let cid = self.new_client_id();
            self.stream_lifetime += 1;
            let lifetime = self.stream_lifetime;
            let now = self.clock.mono_ns();
            let mut ring = OutRing::new(CONSOLE_OUT_RING_BYTES);
            // #131：greeting 先於加入 fan-out 集合並立即嘗試寫出，避免與 RX bytes 交錯。
            ring.push(OutItem::Control(TELNET_GREETING.to_vec()));
            let mut client = HubClient {
                cid: cid.clone(),
                lifetime,
                kind: ConsoleTransportKind::TcpTelnet,
                ring,
                telnet: Some(TelnetFilter::new()),
                attached_at: now,
                pty_path: None,
                _keepalive: None,
            };
            let _ = Self::write_ring(&mut *self.mux, id, &mut client.ring);
            self.insert_client(id, client);
            let endpoint = self
                .status
                .endpoint
                .lock()
                .unwrap_or_else(|e| e.into_inner())
                .clone()
                .unwrap_or_else(|| peer.clone());
            self.pending
                .push_back(Pending::Control(ControlEvent::ConsoleConnected {
                    client_id: cid,
                    client_lifetime: lifetime,
                    label: peer,
                    vtty: endpoint,
                    kind: ConsoleTransportKind::TcpTelnet,
                }));
        }
    }

    fn fanout(&mut self, chunk: &[u8]) {
        let ids: Vec<MuxId> = self.clients.keys().copied().collect();
        let mut escaped: Option<Vec<u8>> = None;
        for id in ids {
            let telnet = self.clients.get(&id).is_some_and(|c| c.telnet.is_some());
            let item = if telnet {
                let bytes = escaped
                    .get_or_insert_with(|| escape_iac(chunk).into_owned())
                    .clone();
                OutItem::Display {
                    bytes,
                    iac_escaped: true,
                }
            } else {
                OutItem::Display {
                    bytes: chunk.to_vec(),
                    iac_escaped: false,
                }
            };
            self.push_item(id, item);
        }
    }

    fn push_item(&mut self, id: MuxId, item: OutItem) {
        let Some(client) = self.clients.get_mut(&id) else {
            return;
        };
        let control = matches!(item, OutItem::Control(_));
        match client.ring.push(item) {
            PushOutcome::Queued => {}
            PushOutcome::DroppedOldest { bytes } => {
                let line = format!(
                    "console 出向佇列溢位，丟棄最舊顯示資料（com={} client_id={} dropped={bytes}）",
                    self.com, client.cid
                );
                self.status.warn(line);
            }
            PushOutcome::DroppedNewest { bytes } => {
                let line = format!(
                    "console 出向佇列溢位，丟棄最新顯示資料（com={} client_id={} dropped={bytes}）",
                    self.com, client.cid
                );
                self.status.warn(line);
            }
        }
        if control && client.ring.control_bytes() > CONSOLE_CONTROL_MAX_BYTES {
            // D-05／SC-31(c)：只送不讀的 client 讓協商回覆無限累積 → 關閉並走一般斷線流程。
            let line = format!(
                "console 協商回覆累積超過 65536 bytes，關閉該 console（com={} client_id={}）",
                self.com, client.cid
            );
            self.status.warn(line);
            self.remove_client(id, true);
        }
    }

    fn process_mux_event(&mut self, ev: MuxEvent, epoch_snapshot: u64) {
        match ev {
            MuxEvent::Woken => {}
            MuxEvent::Acceptable(id) if Some(id) == self.listener => self.accept(id),
            MuxEvent::Acceptable(_) => {}
            MuxEvent::Readable(id) => {
                if !self.clients.contains_key(&id) {
                    return;
                }
                if self.pending.len() >= CONSOLE_HUB_PENDING_EVENTS {
                    // 背壓留在 OS buffer（D-17(1)）。
                    return;
                }
                let outcome =
                    catch_unwind(AssertUnwindSafe(|| self.read_client(id, epoch_snapshot)));
                if outcome.is_err() {
                    self.warn(format!(
                        "console RX handler 例外（com={}），略過此塊、console 續行",
                        self.com
                    ));
                }
            }
            MuxEvent::Writable(id) => {
                if let Some(client) = self.clients.get_mut(&id) {
                    if Self::write_ring(&mut *self.mux, id, &mut client.ring).is_err() {
                        self.remove_client(id, true);
                    }
                }
            }
            MuxEvent::Hangup(id) => self.remove_client(id, true),
        }
    }

    fn read_client(&mut self, id: MuxId, epoch_snapshot: u64) {
        let mut buf = vec![0u8; SERIAL_READ_CHUNK];
        let outcome = self.mux.read(id, &mut buf);
        match outcome {
            Ok(ReadOutcome::Data(n)) if n > 0 => {
                #[cfg(feature = "test-fakes")]
                if self.status.panic_next_input.swap(false, Ordering::AcqRel) {
                    panic!("測試注入的 console RX handler panic");
                }
                let data = &buf[..n];
                let Some(client) = self.clients.get_mut(&id) else {
                    return;
                };
                let (bytes, reply) = match client.telnet.as_mut() {
                    Some(filter) => {
                        let feed = filter.feed(data);
                        (feed.uart, feed.reply)
                    }
                    None => (data.to_vec(), Vec::new()),
                };
                let (cid, lifetime) = (client.cid.clone(), client.lifetime);
                if !reply.is_empty() {
                    self.push_item(id, OutItem::Control(reply));
                    if !self.clients.contains_key(&id) {
                        return; // Control 溢位已關閉
                    }
                }
                if !bytes.is_empty() {
                    self.pending
                        .push_back(Pending::Data(DataEvent::ConsoleInputBytes {
                            client_id: cid,
                            client_lifetime: lifetime,
                            input_epoch: epoch_snapshot,
                            bytes,
                        }));
                }
            }
            Ok(ReadOutcome::Data(_) | ReadOutcome::TimedOut | ReadOutcome::Woken) => {}
            Ok(ReadOutcome::Eof) | Err(_) => self.remove_client(id, true),
        }
    }

    fn maybe_scan(&mut self) {
        let now = self.clock.mono_ns();
        if now < self.next_scan {
            return;
        }
        while self.next_scan <= now {
            self.next_scan = self.next_scan.saturating_add(secs_ns(HUMAN_PEER_GRACE_S));
        }
        let held: Option<HashSet<String>> = self.factory.held_slave_paths();
        // D-05：無外部 holder 且已過 grace 的 PTY console 清空 Display（Control 保留）。
        if let Some(h) = held.as_ref() {
            for client in self.clients.values_mut() {
                if client.kind != ConsoleTransportKind::Pty {
                    continue;
                }
                let held_now = client.pty_path.as_ref().is_some_and(|p| h.contains(p));
                if !held_now
                    && now.saturating_sub(client.attached_at) >= secs_ns(STALE_CONSOLE_GRACE_S)
                {
                    client.ring.clear_display();
                }
            }
        }
        let scan = Pending::Control(ControlEvent::ConsoleScan {
            scan: ConsoleScanSnapshot { held, taken_at: now },
            source: ScanSource::Hub,
        });
        // D-18：pending 有界。尚未送出的 hub 掃描以新快照就地取代（只需最新者）；pending 已滿則略過本次。
        let queued = self.pending.iter().position(|p| {
            matches!(p, Pending::Control(ControlEvent::ConsoleScan { source: ScanSource::Hub, .. }))
        });
        match queued {
            Some(i) => self.pending[i] = scan,
            None if self.pending.len() < CONSOLE_HUB_PENDING_EVENTS => self.pending.push_back(scan),
            None => {}
        }
    }

    fn flush_pending(&mut self) {
        while let Some(front) = self.pending.pop_front() {
            let result = match front {
                Pending::Data(ev) => self.data.try_send(ev).map_err(|e| match e {
                    TrySendError::Full(ev) => Some(Pending::Data(ev)),
                    TrySendError::Disconnected(_) => None,
                }),
                Pending::Control(ev) => self.control.try_send(ev).map_err(|e| match e {
                    TrySendError::Full(ev) => Some(Pending::Control(ev)),
                    TrySendError::Disconnected(_) => None,
                }),
            };
            match result {
                Ok(()) => {}
                Err(Some(back)) => {
                    self.pending.push_front(back);
                    break;
                }
                Err(None) => {}
            }
        }
        if self.pending.len() >= CONSOLE_HUB_PENDING_EVENTS {
            if !self.pending_full_warned {
                self.pending_full_warned = true;
                self.warn(format!(
                    "console 輸入暫停讀取：session mailbox 已滿（com={} pending={}）",
                    self.com,
                    self.pending.len()
                ));
            }
        } else if self.pending.is_empty() {
            self.pending_full_warned = false;
        }
        self.status
            .pending
            .store(self.pending.len() as u64, Ordering::Release);
    }

    /// 寫出 ring front；`Err` 表示 io 已失效。
    fn write_ring(mux: &mut dyn ConsoleMux, id: MuxId, ring: &mut OutRing) -> Result<(), ()> {
        while let Some(front) = ring.front() {
            match mux.try_write(id, front) {
                Ok(0) => break,
                Ok(n) => ring.consume(n),
                Err(_) => return Err(()),
            }
        }
        mux.set_write_interest(id, !ring.is_empty());
        Ok(())
    }

    fn flush_rings(&mut self) {
        let ids: Vec<MuxId> = self.clients.keys().copied().collect();
        for id in ids {
            let failed = match self.clients.get_mut(&id) {
                Some(client) if !client.ring.is_empty() => {
                    Self::write_ring(&mut *self.mux, id, &mut client.ring).is_err()
                }
                _ => false,
            };
            if failed {
                self.remove_client(id, true);
            }
        }
    }

    fn publish(&self) {
        let mut m = self
            .status
            .ring_display
            .lock()
            .unwrap_or_else(|e| e.into_inner());
        for c in self.clients.values() {
            m.insert(c.cid.clone(), c.ring.display_bytes());
        }
    }
}
