//! 每 session 一條不阻塞的 ConsoleHub thread（P6 Task 9；07 D-17）。
//!
//! RX fan-out、Telnet 協商、peer 掃描、跨 `conn_epoch` 保留 console；平台差異只在 `sw-pal` 的
//! `ConsoleTransportFactory`／`ConsoleMux`。Task 1 骨架：型別與簽章凍結，函式本體為 `todo!("P6 Task 9")`。

use std::sync::atomic::{AtomicBool, AtomicU64};
use std::sync::{Arc, Mutex};

use crossbeam_channel::Sender;
use sw_pal::traits::{Clock, IoWaker};
use sw_pal::{ConsoleKind, Platform, PtyPair};

use crate::human_console::out_ring::OutItem;
use crate::mailbox::{ControlEvent, DataEvent};

/// hub→mailbox control lane 送端。
pub type ControlSender = Sender<ControlEvent>;
/// hub→mailbox data lane 送端（`ConsoleInputBytes`／`ConsoleDisconnected` 同一條 FIFO，07 D-17(3)）。
pub type DataSender = Sender<DataEvent>;

/// actor→hub 控制命令（bounded `CONSOLE_HUB_CTRL_QUEUE`）。
pub enum HubCmd {
    AdoptPty { client_id: String, client_lifetime: u64, label: String, pair: PtyPair },
    /// TcpTelnet：bridge 起來時。
    StartListener,
    /// bridge 停止時（ephemeral port，UIO-12.8-c）。
    StopListener,
    /// 07 D-12。
    OpenCom0com { port: String },
    Out { client_id: String, item: OutItem },
    /// 測試用：hub 在本輪停住直到 gate 釋放（模擬 hub 卡住、驗證有界佇列）。
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
    /// 最近的 WARNING（上限 `HUB_WARNINGS_MAX`，供診斷與測試；同時寫 stderr）。
    pub warnings: Mutex<std::collections::VecDeque<String>>,
    /// 各 console 出向 ring 的 Display bytes（每輪更新，診斷用）。
    pub ring_display: Mutex<std::collections::HashMap<String, usize>>,
    /// 測試用：下一個 console 輸入處理 panic（#89）。
    #[cfg(feature = "test-fakes")]
    pub panic_next_input: AtomicBool,
}

impl HubStatus {
    pub fn new() -> HubStatus {
        todo!("P6 Task 9")
    }
    /// POSIX：primary 的 vtty 路徑存在；TcpTelnet：`listener_up`（06 §7.7）。
    pub fn vtty_alive(&self, _primary_vtty: Option<&str>, _fs_exists: &dyn Fn(&str) -> bool, _kind: ConsoleKind) -> bool {
        todo!("P6 Task 9")
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
}

impl HubHandle {
    /// 寫 critical＋wake，永不阻塞。
    pub fn close(&self, _cid: &str) {
        todo!("P6 Task 9")
    }
    pub fn close_all(&self) {
        todo!("P6 Task 9")
    }
    pub fn shutdown(&self) {
        todo!("P6 Task 9")
    }
}

/// thread 名 `serialwrap-console-<COM>`。
pub fn spawn_hub(
    _com: String,
    _platform: &Platform,
    _control: ControlSender,
    _data: DataSender,
    _clock: Arc<dyn Clock>,
    _input_epoch: Arc<AtomicU64>,
) -> HubHandle {
    todo!("P6 Task 9")
}

/// reader 對 hub 的 RX 入口（增補 #21）：WAL 之後 `try_send`，滿載只計數＋WARNING，永不阻塞。
pub struct HubTap {
    pub rx: Sender<Arc<[u8]>>,
    pub waker: Arc<dyn IoWaker>,
    pub status: Arc<HubStatus>,
}

impl HubTap {
    /// 回 true＝已送進 hub。
    pub fn push(&self, _bytes: &[u8], _warning_sink: &dyn Fn(&str)) -> bool {
        todo!("P6 Task 9")
    }
}

impl HubHandle {
    /// reader 用的 tap（clone `rx`／`waker`／`status`）。
    pub fn tap(&self) -> HubTap {
        todo!("P6 Task 9")
    }
}
