//! Windows com0com console-side 埠（`CreateFileW(OPEN_EXISTING, dwShareMode=0)`；P6 Task 8）。Task 1 骨架。
//!
//! LOCAL-WINDOWS-TODO（P6 Task 8）：實作完成前回 `Unsupported`，不得 panic（sol 補審 P6 #4）。

use crate::console_pty::ConsoleIo;
use crate::traits::{PalError, PalErrorKind, PalResult};

pub fn open(port: &str) -> PalResult<Box<dyn ConsoleIo>> {
    Err(PalError::plain(
        PalErrorKind::Unsupported,
        format!("com0com serial console 在 Windows 尚未實作（port={port}；LOCAL-WINDOWS-TODO P6 Task 8）"),
    ))
}
