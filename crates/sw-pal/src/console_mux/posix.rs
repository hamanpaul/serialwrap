//! POSIX ConsoleMux（P6 Task 8）：`poll(2)`＋eventfd（非 Linux 用 self-pipe）。Task 1 骨架。

use crate::console_mux::ConsoleMux;
use crate::traits::PalResult;

/// POSIX `poll(2)` 多工器（Task 8 實作）。
pub struct PosixConsoleMux {
    _stub: (),
}

impl PosixConsoleMux {
    pub fn create() -> PalResult<Box<dyn ConsoleMux>> {
        todo!("P6 Task 8")
    }
}
