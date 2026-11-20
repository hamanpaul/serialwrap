//! TCP＋Telnet console listener（P6 Task 8；arch §4.7）：只綁 `127.0.0.1:0`（OD-12）、SO_REUSEADDR、backlog 8、nonblocking；
//! accept 後設 TCP_NODELAY。POSIX 本體在 `posix.rs`；Windows（Winsock）在 `windows.rs`。

use crate::console_pty::ConsoleListener;
use crate::traits::PalResult;

#[cfg(unix)]
pub mod posix;
#[cfg(windows)]
pub mod windows;

/// listen backlog（Python `CONSOLE_TCP_BACKLOG`；sw-pal 不依賴 sw-proto，故在此重述，arch §1.3）。
#[cfg(unix)]
pub(crate) const CONSOLE_TCP_BACKLOG: libc::c_int = 8;

/// 建立 loopback Telnet console listener（`ConsoleTransportFactory::bind_tcp_console` 的原生實作）。
pub fn bind_loopback_console() -> PalResult<Box<dyn ConsoleListener>> {
    #[cfg(unix)]
    {
        posix::bind()
    }
    #[cfg(windows)]
    {
        windows::bind()
    }
}
