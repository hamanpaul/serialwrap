//! Windows ConsoleMux（P6 Task 8）：`WSAPoll`＋loopback socket pair waker；com0com 由 reader thread 轉事件。Task 1 骨架。
//!
//! LOCAL-WINDOWS-TODO（P6 Task 8）：實作完成前 `create` 回 `Unsupported`，不得 panic（sol 補審 P6 #4）。

use crate::console_mux::ConsoleMux;
use crate::traits::{PalError, PalErrorKind, PalResult};

/// Windows `WSAPoll` 多工器（Task 8 實作）。
pub struct WinConsoleMux {
    _stub: (),
}

impl WinConsoleMux {
    pub fn create() -> PalResult<Box<dyn ConsoleMux>> {
        Err(PalError::plain(
            PalErrorKind::Unsupported,
            "ConsoleMux（WSAPoll）在 Windows 尚未實作（LOCAL-WINDOWS-TODO P6 Task 8）",
        ))
    }
}
