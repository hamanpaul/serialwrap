//! human console 承接工廠（arch §4.7）。
//!
//! 本檔只宣告型別與簽章；POSIX `create_pty`（哨兵 PTY）由 Task 2 實作，其餘方法於本 Phase 回 `Unsupported`
//! （`bind_tcp_console`、`open_com0com_console` 屬 2.2；`held_slave_paths` 於 2.2 承接，本 Phase 回 `None`）。

use std::collections::HashSet;
use std::sync::Arc;
use std::time::Duration;

use crate::traits::{IoWaker, PalError, PalErrorKind, PalResult, ReadOutcome};
use crate::ConsoleKind;

#[cfg(unix)]
pub mod posix;

/// PTY slave 的參照（`mirror_termios_from`、`tty_setraw`、`tcgetattr_speed` 的輸入；arch §4.2、§4.7）。
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PtySlaveRef {
    pub path: String,
}

/// 保持 PTY slave 開啟的 keepalive（daemon 同時持有 master 與 slave，避免閒置時 master 一直 EOF；arch §4.7）。
#[cfg(unix)]
pub struct KeepAlive {
    _fd: std::os::fd::OwnedFd,
}

#[cfg(not(unix))]
pub struct KeepAlive {
    _priv: (),
}

/// PTY master 或 TCP console 的 I/O 端（arch §4.7）。
pub trait ConsoleIo: Send {
    fn read(&mut self, buf: &mut [u8], timeout: Option<Duration>) -> PalResult<ReadOutcome>;
    /// 不阻塞；EAGAIN → `Ok(0)`。
    fn try_write(&mut self, data: &[u8]) -> PalResult<usize>;
    fn waker(&self) -> Arc<dyn IoWaker>;
    fn has_external_peer(&self, held: Option<&HashSet<String>>) -> bool;
    fn close(self: Box<Self>);
}

/// `create_pty` 的結果（arch §4.7）。
pub struct PtyPair {
    pub master: Box<dyn ConsoleIo>,
    pub slave_keepalive: KeepAlive,
    pub slave_path: String,
    pub slave_ref: PtySlaveRef,
}

/// TCP loopback console listener（arch §4.7）：`127.0.0.1:0`、SO_REUSEADDR、backlog 8、nonblocking。
pub trait ConsoleListener: Send {
    /// `"127.0.0.1:<port>"`。
    fn endpoint(&self) -> String;
    /// `peer` 為 `"ip:port"`。
    fn accept(&mut self, timeout: Duration) -> PalResult<Option<(Box<dyn ConsoleIo>, String)>>;
}

/// human console 承接工廠（arch §4.7）。
///
/// 除 `kind` 外的方法於本 Phase 提供預設實作（回 `Unsupported`／`None`），使只實作 `kind` 的既有替身仍可編譯；
/// POSIX 原生實作在 `NativeConsoleFactory::create_pty` 建立哨兵 PTY。
pub trait ConsoleTransportFactory: Send + Sync {
    fn kind(&self) -> ConsoleKind;
    /// POSIX openpty：master 設 O_NONBLOCK；slave raw（iflag=oflag=lflag=0、CREAD|CLOCAL|CS8、VMIN=1、VTIME=0）。
    fn create_pty(&self) -> PalResult<PtyPair> {
        Err(unsupported("create_pty"))
    }
    /// 2.2 承接。
    fn bind_tcp_console(&self) -> PalResult<Box<dyn ConsoleListener>> {
        Err(unsupported("bind_tcp_console"))
    }
    /// OD-01 選配；2.2 承接。
    fn open_com0com_console(&self, _port: &str) -> PalResult<Box<dyn ConsoleIo>> {
        Err(unsupported("open_com0com_console"))
    }
    /// /proc 掃描外部持有的 pts 路徑；Windows 或不可讀時回 `None`。2.2 承接，本 Phase 回 `None`。
    fn held_slave_paths(&self) -> Option<HashSet<String>> {
        None
    }
}

fn unsupported(name: &str) -> PalError {
    PalError::plain(
        PalErrorKind::Unsupported,
        format!("{name} 於 Slice 2.1 未支援"),
    )
}

/// 原生 console 工廠（P5-T2）：`kind` 依目標平台（POSIX 為 PTY，無 PTY 平台為 TCP）。
pub struct NativeConsoleFactory;

impl ConsoleTransportFactory for NativeConsoleFactory {
    fn kind(&self) -> ConsoleKind {
        #[cfg(unix)]
        {
            ConsoleKind::Pty
        }
        #[cfg(not(unix))]
        {
            ConsoleKind::TcpTelnet
        }
    }

    fn create_pty(&self) -> PalResult<PtyPair> {
        #[cfg(unix)]
        {
            posix::create_pty()
        }
        #[cfg(not(unix))]
        {
            Err(unsupported("create_pty"))
        }
    }
}
