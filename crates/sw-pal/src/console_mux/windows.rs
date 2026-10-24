//! Windows ConsoleMux（P6 Task 8）：`WSAPoll`＋loopback socket pair waker；com0com 由 reader thread 轉事件。Task 1 骨架。

use crate::console_mux::ConsoleMux;
use crate::traits::PalResult;

/// Windows `WSAPoll` 多工器（Task 8 實作）。
pub struct WinConsoleMux {
    _stub: (),
}

impl WinConsoleMux {
    pub fn create() -> PalResult<Box<dyn ConsoleMux>> {
        todo!("P6 Task 8")
    }
}
