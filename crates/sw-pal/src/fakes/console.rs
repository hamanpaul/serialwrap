//! human console／flash 端點替身（arch §4.7；P8-T7）。

use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use crate::console_pty::{ConsoleIo, ConsoleTransportFactory, ConsoleWriteHalf, PtySlaveRef, Readiness, TermSpeed};
use crate::traits::{IoWaker, PalResult, ReadOutcome};
use crate::ConsoleKind;

/// 記憶體 console 工廠：記錄 `link`／`unlink`／`tty_setraw`，`tcgetattr_speed` 回預先設定的速率。
pub struct FakeConsoleFactory {
    kind: ConsoleKind,
    flash: bool,
    links: Mutex<HashMap<PathBuf, String>>,
    raw: Mutex<Vec<String>>,
    speeds: Mutex<HashMap<String, TermSpeed>>,
}

impl FakeConsoleFactory {
    /// `flash=false` 模擬 Windows：flash 端點方法全回 `Unsupported`。
    pub fn new(kind: ConsoleKind, flash: bool) -> Arc<FakeConsoleFactory> {
        Arc::new(FakeConsoleFactory {
            kind,
            flash,
            links: Mutex::new(HashMap::new()),
            raw: Mutex::new(Vec::new()),
            speeds: Mutex::new(HashMap::new()),
        })
    }

    pub fn set_speed(&self, path: &str, speed: TermSpeed) {
        self.speeds.lock().unwrap_or_else(|e| e.into_inner()).insert(path.to_string(), speed);
    }

    /// 目前的 symlink 表（link 路徑 → slave 路徑）。
    pub fn links(&self) -> HashMap<PathBuf, String> {
        self.links.lock().unwrap_or_else(|e| e.into_inner()).clone()
    }

    /// 已呼叫 `tty_setraw` 的 slave 路徑（依序）。
    pub fn raw_calls(&self) -> Vec<String> {
        self.raw.lock().unwrap_or_else(|e| e.into_inner()).clone()
    }
}

impl ConsoleTransportFactory for FakeConsoleFactory {
    fn kind(&self) -> ConsoleKind {
        self.kind
    }

    fn held_slave_paths(&self) -> Option<HashSet<String>> {
        None
    }

    fn link(&self, _slave_path: &str, _link: &Path) -> PalResult<()> {
        todo!("P8-T7")
    }

    fn unlink(&self, _link: &Path) -> PalResult<()> {
        todo!("P8-T7")
    }

    fn tty_setraw(&self, _slave: &PtySlaveRef) -> PalResult<()> {
        todo!("P8-T7")
    }

    fn tcgetattr_speed(&self, _slave: &PtySlaveRef) -> PalResult<TermSpeed> {
        todo!("P8-T7")
    }
}

struct Shared {
    rx: Mutex<std::collections::VecDeque<u8>>,
    cond: std::sync::Condvar,
    woken: Mutex<bool>,
    tx: Mutex<Vec<u8>>,
    tx_capacity: Mutex<usize>,
}

/// 測試端的操作面：注入 RX、讀出 TX、限制 TX 容量（模擬 EAGAIN）。
#[derive(Clone)]
pub struct FakeConsoleHandle {
    shared: Arc<Shared>,
}

impl FakeConsoleHandle {
    pub fn push_rx(&self, _bytes: &[u8]) {
        todo!("P8-T7")
    }

    pub fn take_tx(&self) -> Vec<u8> {
        todo!("P8-T7")
    }

    /// TX 緩衝上限（預設無限制以外的小值由測試設定）；滿了之後 `try_write` 回 `Ok(0)`。
    pub fn set_tx_capacity(&self, _cap: usize) {
        todo!("P8-T7")
    }
}

/// 記憶體 `ConsoleIo`。
pub struct FakeConsoleIo {
    shared: Arc<Shared>,
}

impl FakeConsoleIo {
    pub fn new() -> (FakeConsoleIo, FakeConsoleHandle) {
        let shared = Arc::new(Shared {
            rx: Mutex::new(std::collections::VecDeque::new()),
            cond: std::sync::Condvar::new(),
            woken: Mutex::new(false),
            tx: Mutex::new(Vec::new()),
            tx_capacity: Mutex::new(usize::MAX),
        });
        (FakeConsoleIo { shared: Arc::clone(&shared) }, FakeConsoleHandle { shared })
    }
}

struct FakeWriter {
    shared: Arc<Shared>,
}

impl ConsoleWriteHalf for FakeWriter {
    fn try_write(&mut self, _data: &[u8]) -> PalResult<usize> {
        todo!("P8-T7")
    }
}

struct FakeWaker {
    shared: Arc<Shared>,
}

impl IoWaker for FakeWaker {
    fn wake(&self) {
        todo!("P8-T7")
    }
}

impl ConsoleIo for FakeConsoleIo {
    fn read(&mut self, _buf: &mut [u8], _timeout: Option<Duration>) -> PalResult<ReadOutcome> {
        todo!("P8-T7")
    }
    fn try_write(&mut self, _data: &[u8]) -> PalResult<usize> {
        todo!("P8-T7")
    }
    fn waker(&self) -> Arc<dyn IoWaker> {
        Arc::new(FakeWaker { shared: Arc::clone(&self.shared) })
    }
    fn has_external_peer(&self, _held: Option<&HashSet<String>>) -> bool {
        false
    }
    fn close(self: Box<Self>) {}
    fn clone_writer(&self) -> PalResult<Box<dyn ConsoleWriteHalf>> {
        Ok(Box::new(FakeWriter { shared: Arc::clone(&self.shared) }))
    }
    fn wait_readable(&mut self, _timeout: Option<Duration>) -> PalResult<Readiness> {
        todo!("P8-T7")
    }
}
