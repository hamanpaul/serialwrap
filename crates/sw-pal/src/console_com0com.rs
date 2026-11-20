//! OD-01 選配 com0com Serial console（P6 Task 8；07 D-12、SC-29）：console-side 埠以 share mode 0 開啟，
//! 虛擬端 DTR／RTS／baud 變更不轉送 DUT。非 Windows 平台回 `Unsupported`。Task 1 骨架。
//! （與 P4 的 `com0com.rs`（setupc 安裝／移除 helper）不同。）

use crate::console_pty::ConsoleIo;
use crate::traits::PalResult;

#[cfg(unix)]
pub mod posix;
#[cfg(windows)]
pub mod windows;

/// 開啟 com0com console-side 埠（`ConsoleTransportFactory::open_com0com_console` 的原生實作）。
pub fn open_console_port(port: &str) -> PalResult<Box<dyn ConsoleIo>> {
    #[cfg(unix)]
    {
        posix::open(port)
    }
    #[cfg(windows)]
    {
        windows::open(port)
    }
}
