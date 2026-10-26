//! human console 承接工廠（arch §4.7）。
//!
//! trait 預設實作回 `Unsupported`／`None`（只實作 `kind` 的替身仍可編譯）；`NativeConsoleFactory` 組裝 POSIX PTY、
//! TCP console、ConsoleMux、`/proc` 持有者掃描與（Windows）com0com console（P6 Task 8）。

use std::collections::HashSet;
use std::sync::Arc;
use std::time::Duration;

use crate::console_mux::{ConsoleMux, MuxHandle};
use crate::traits::{IoWaker, PalError, PalErrorKind, PalResult, ReadOutcome};
use crate::ConsoleKind;

#[cfg(unix)]
pub mod posix;
#[cfg(windows)]
pub mod windows;

/// PTY slave 的參照（`mirror_termios_from`、`tty_setraw`、`tcgetattr_speed` 的輸入；arch §4.2、§4.7）。
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PtySlaveRef {
    pub path: String,
}

/// 保持 PTY slave 開啟的 keepalive（daemon 同時持有 master 與 slave，避免閒置時 master 一直 EOF；arch §4.7）。
#[cfg(unix)]
pub struct KeepAlive {
    pub(crate) _fd: Option<std::os::fd::OwnedFd>,
}

#[cfg(not(unix))]
pub struct KeepAlive {
    _priv: (),
}

impl KeepAlive {
    /// 不持有任何 fd 的 keepalive（記憶體替身的 `PtyPair`）。
    #[cfg(feature = "test-fakes")]
    pub(crate) fn detached() -> KeepAlive {
        #[cfg(unix)]
        {
            KeepAlive { _fd: None }
        }
        #[cfg(not(unix))]
        {
            KeepAlive { _priv: () }
        }
    }
}

/// PTY master 或 TCP console 的 I/O 端（arch §4.7）。
pub trait ConsoleIo: Send {
    fn read(&mut self, buf: &mut [u8], timeout: Option<Duration>) -> PalResult<ReadOutcome>;
    /// 不阻塞；EAGAIN → `Ok(0)`。
    fn try_write(&mut self, data: &[u8]) -> PalResult<usize>;
    fn waker(&self) -> Arc<dyn IoWaker>;
    fn has_external_peer(&self, held: Option<&HashSet<String>>) -> bool;
    fn close(self: Box<Self>);
    /// P6（07 D-04）：`ConsoleMux` 用的底層 handle（PAL 私有建構）。
    fn mux_handle(&self) -> MuxHandle;
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
    /// P6（07 D-04）：`ConsoleMux` 用的底層 handle（PAL 私有建構）。
    fn mux_handle(&self) -> MuxHandle;
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
    /// P6（07 D-04，Task 8）：建立此 PAL 的 `ConsoleMux`；預設 `Unsupported`（既有只實作 `kind` 的替身仍可編譯）。
    fn new_mux(&self) -> PalResult<Box<dyn ConsoleMux>> {
        Err(unsupported("new_mux"))
    }
}

fn unsupported(name: &str) -> PalError {
    PalError::plain(
        PalErrorKind::Unsupported,
        format!("{name} 於 Slice 2.1 未支援"),
    )
}

/// 原生 console 工廠：`kind` 由 `native()` 依 SC-28（`select_console_kind`）決定。
pub struct NativeConsoleFactory {
    kind: ConsoleKind,
}

impl NativeConsoleFactory {
    pub fn new(kind: ConsoleKind) -> NativeConsoleFactory {
        NativeConsoleFactory { kind }
    }
}

/// 此平台是否能建立 PTY（SC-28 的 `has_pty`）。
pub fn native_has_pty() -> bool {
    cfg!(unix)
}

impl ConsoleTransportFactory for NativeConsoleFactory {
    fn kind(&self) -> ConsoleKind {
        self.kind
    }

    fn create_pty(&self) -> PalResult<PtyPair> {
        #[cfg(unix)]
        {
            posix::create_pty()
        }
        #[cfg(windows)]
        {
            windows::create_pty()
        }
    }

    fn bind_tcp_console(&self) -> PalResult<Box<dyn ConsoleListener>> {
        crate::console_tcp::bind_loopback_console()
    }

    fn open_com0com_console(&self, port: &str) -> PalResult<Box<dyn ConsoleIo>> {
        crate::console_com0com::open_console_port(port)
    }

    /// `/proc/<pid>/fd/*` 的 readlink 目標（排除自身 pid）；`/proc` 不可讀或 Windows 回 `None`（uart_io.py `_enumerate_all_held_paths`）。
    fn held_slave_paths(&self) -> Option<HashSet<String>> {
        #[cfg(unix)]
        {
            crate::proc_scan::PosixProcScan::new().held_fd_targets()
        }
        #[cfg(windows)]
        {
            None
        }
    }

    fn new_mux(&self) -> PalResult<Box<dyn ConsoleMux>> {
        #[cfg(unix)]
        {
            crate::console_mux::posix::PosixConsoleMux::create()
        }
        #[cfg(windows)]
        {
            crate::console_mux::windows::WinConsoleMux::create()
        }
    }
}
