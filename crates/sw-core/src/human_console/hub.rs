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
}

impl HubStatus {
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
