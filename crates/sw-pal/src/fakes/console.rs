//! human console／flash 端點替身（arch §4.7；P8-T7）。

use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use crate::console_pty::{ConsoleIo, ConsoleTransportFactory, ConsoleWriteHalf, PtySlaveRef, Readiness, TermSpeed};
use crate::traits::{IoWaker, PalError, PalErrorKind, PalResult, ReadOutcome};
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

    fn check(&self, name: &str) -> PalResult<()> {
        if self.flash {
            Ok(())
        } else {
            Err(crate::console_pty::unsupported_flash(name))
        }
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

    fn link(&self, slave_path: &str, link: &Path) -> PalResult<()> {
        self.check("link")?;
        self.links.lock().unwrap_or_else(|e| e.into_inner()).insert(link.to_path_buf(), slave_path.to_string());
        Ok(())
    }

    fn unlink(&self, link: &Path) -> PalResult<()> {
        self.check("unlink")?;
        self.links.lock().unwrap_or_else(|e| e.into_inner()).remove(link);
        Ok(())
    }

    fn tty_setraw(&self, slave: &PtySlaveRef) -> PalResult<()> {
        self.check("tty_setraw")?;
        self.raw.lock().unwrap_or_else(|e| e.into_inner()).push(slave.path.clone());
        Ok(())
    }

    fn tcgetattr_speed(&self, slave: &PtySlaveRef) -> PalResult<TermSpeed> {
        self.check("tcgetattr_speed")?;
        self.speeds
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .get(&slave.path)
            .copied()
            .ok_or_else(|| PalError::plain(PalErrorKind::Io, "tcgetattr failed"))
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
    pub fn push_rx(&self, bytes: &[u8]) {
        self.shared.rx.lock().unwrap_or_else(|e| e.into_inner()).extend(bytes);
        self.shared.cond.notify_all();
    }

    pub fn take_tx(&self) -> Vec<u8> {
        std::mem::take(&mut *self.shared.tx.lock().unwrap_or_else(|e| e.into_inner()))
    }

    /// TX 緩衝總容量（預設無限制）；滿了之後 `try_write` 回 `Ok(0)`（模擬 EAGAIN）。
    pub fn set_tx_capacity(&self, cap: usize) {
        *self.shared.tx_capacity.lock().unwrap_or_else(|e| e.into_inner()) = cap;
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

impl FakeConsoleIo {
    /// 喚醒優先（與 PTY 實作一致），其次可讀；喚醒旗標被消耗。
    fn wait(&self, timeout: Option<Duration>) -> Readiness {
        let deadline = timeout.map(|t| std::time::Instant::now() + t);
        let mut woken = self.shared.woken.lock().unwrap_or_else(|e| e.into_inner());
        loop {
            if *woken {
                *woken = false;
                return Readiness::Woken;
            }
            if !self.shared.rx.lock().unwrap_or_else(|e| e.into_inner()).is_empty() {
                return Readiness::Ready;
            }
            match deadline {
                None => woken = self.shared.cond.wait(woken).unwrap_or_else(|e| e.into_inner()),
                Some(d) => {
                    let now = std::time::Instant::now();
                    if now >= d {
                        return Readiness::TimedOut;
                    }
                    woken = self.shared.cond.wait_timeout(woken, d - now).unwrap_or_else(|e| e.into_inner()).0;
                }
            }
        }
    }
}

struct FakeWriter {
    shared: Arc<Shared>,
}

fn shared_write(shared: &Shared, data: &[u8]) -> usize {
    let cap = *shared.tx_capacity.lock().unwrap_or_else(|e| e.into_inner());
    let mut tx = shared.tx.lock().unwrap_or_else(|e| e.into_inner());
    let n = cap.saturating_sub(tx.len()).min(data.len());
    tx.extend_from_slice(&data[..n]);
    n
}

impl ConsoleWriteHalf for FakeWriter {
    fn try_write(&mut self, data: &[u8]) -> PalResult<usize> {
        Ok(shared_write(&self.shared, data))
    }
}

struct FakeWaker {
    shared: Arc<Shared>,
}

impl IoWaker for FakeWaker {
    fn wake(&self) {
        *self.shared.woken.lock().unwrap_or_else(|e| e.into_inner()) = true;
        self.shared.cond.notify_all();
    }
}

impl ConsoleIo for FakeConsoleIo {
    fn read(&mut self, buf: &mut [u8], timeout: Option<Duration>) -> PalResult<ReadOutcome> {
        match self.wait(timeout) {
            Readiness::Woken => Ok(ReadOutcome::Woken),
            Readiness::TimedOut => Ok(ReadOutcome::TimedOut),
            Readiness::Ready => {
                let mut rx = self.shared.rx.lock().unwrap_or_else(|e| e.into_inner());
                let n = buf.len().min(rx.len());
                for slot in buf.iter_mut().take(n) {
                    *slot = rx.pop_front().unwrap_or(0);
                }
                Ok(ReadOutcome::Data(n))
            }
        }
    }
    fn try_write(&mut self, data: &[u8]) -> PalResult<usize> {
        Ok(shared_write(&self.shared, data))
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
    fn wait_readable(&mut self, timeout: Option<Duration>) -> PalResult<Readiness> {
        Ok(self.wait(timeout))
    }
}
