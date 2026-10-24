//! ConsoleMux（P6 Task 8；07 D-04，arch §4.7 P6 細化）：一條 `ConsoleHub` thread 同時等待多個 PTY master、socket、
//! listener 與 waker。POSIX 用 `poll(2)`＋eventfd（非 Linux 用 self-pipe）；Windows 用 `WSAPoll`＋loopback socket pair；
//! com0com handle 由 PAL 內部 reader thread（有界 channel 64）轉成 mux 事件。
//!
//! Task 1 骨架：型別與簽章凍結，函式本體為 `todo!("P6 Task 8")`。

use std::sync::Arc;
use std::time::Duration;

use crate::console_pty::{ConsoleIo, ConsoleListener};
use crate::traits::{IoWaker, PalResult, ReadOutcome};
use crate::ConsoleKind;

#[cfg(unix)]
pub mod posix;
#[cfg(windows)]
pub mod windows;

/// PAL 私有建構的不透明 handle；只有同一個 PAL 的 mux 能解讀。
#[derive(Debug)]
pub struct MuxHandle(#[allow(dead_code)] pub(crate) MuxHandleInner);

/// `MuxHandle` 的內容（PAL 私有）。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[allow(dead_code)]
pub(crate) enum MuxHandleInner {
    /// POSIX fd。
    Posix(i32),
    /// Windows SOCKET。
    WinSocket(usize),
    /// com0com reader channel id。
    Com0com(u64),
    /// 記憶體替身 id。
    Fake(u64),
}

/// mux 內的登記 id（hub 指派）。
pub type MuxId = u64;

/// `wait` 的事件。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum MuxEvent {
    Readable(MuxId),
    Writable(MuxId),
    Hangup(MuxId),
    Acceptable(MuxId),
    Woken,
}

/// `remove` 交回的登記物。
pub enum MuxEntry {
    Io(Box<dyn ConsoleIo>),
    Listener(Box<dyn ConsoleListener>),
}

/// console I/O 多工器。
pub trait ConsoleMux: Send {
    /// 以 `io.mux_handle()` 取得 fd／SOCKET／channel；不同 PAL 的 handle → `Err(Unsupported)`。
    fn add_io(&mut self, id: MuxId, io: Box<dyn ConsoleIo>) -> PalResult<()>;
    fn add_listener(&mut self, id: MuxId, l: Box<dyn ConsoleListener>) -> PalResult<()>;
    fn remove(&mut self, id: MuxId) -> Option<MuxEntry>;
    fn set_write_interest(&mut self, id: MuxId, on: bool);
    /// POSIX `poll(2)`；Windows `WSAPoll`。
    fn wait(&mut self, timeout: Duration) -> PalResult<Vec<MuxEvent>>;
    /// 非阻塞；EAGAIN → `TimedOut`。
    fn read(&mut self, id: MuxId, buf: &mut [u8]) -> PalResult<ReadOutcome>;
    /// EAGAIN → `Ok(0)`。
    fn try_write(&mut self, id: MuxId, data: &[u8]) -> PalResult<usize>;
    fn accept(&mut self, id: MuxId) -> PalResult<Option<(Box<dyn ConsoleIo>, String)>>;
    fn waker(&self) -> Arc<dyn IoWaker>;
}

/// SC-28（07 D-11）：`SERIALWRAP_CONSOLE_BACKEND` ∈ {auto, pty, tcp}（不分大小寫、strip）。
/// 回傳選定的 kind 與（`pty` 但平台無 PTY 時的）WARNING 文字。
pub fn select_console_kind(_env: Option<&str>, _has_pty: bool) -> (ConsoleKind, Option<&'static str>) {
    todo!("P6 Task 8")
}
