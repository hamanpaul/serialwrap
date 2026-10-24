//! TCP＋Telnet console listener（P6 Task 8；arch §4.7）：只綁 `127.0.0.1:0`（OD-12）、SO_REUSEADDR、backlog 8、nonblocking；
//! accept 後設 TCP_NODELAY。Task 1 骨架：簽章凍結，本體為 `todo!("P6 Task 8")`。

use crate::console_pty::ConsoleListener;
use crate::traits::PalResult;

#[cfg(unix)]
pub mod posix;
#[cfg(windows)]
pub mod windows;

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
