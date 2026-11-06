//! Windows（Winsock）TCP console listener（P6 Task 8）。Task 1 骨架。
//!
//! LOCAL-WINDOWS-TODO（P6 Task 8）：Winsock 實作完成前回 `Unsupported`，不得 panic（sol 補審 P6 #4）。

use crate::console_pty::ConsoleListener;
use crate::traits::{PalError, PalErrorKind, PalResult};

pub fn bind() -> PalResult<Box<dyn ConsoleListener>> {
    Err(PalError::plain(
        PalErrorKind::Unsupported,
        "TCP console listener 在 Windows 尚未實作（LOCAL-WINDOWS-TODO P6 Task 8）",
    ))
}
