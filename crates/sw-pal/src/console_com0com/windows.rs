//! Windows com0com console-side 埠（`CreateFileW(OPEN_EXISTING, dwShareMode=0)`；P6 Task 8）。Task 1 骨架。

use crate::console_pty::ConsoleIo;
use crate::traits::PalResult;

pub fn open(_port: &str) -> PalResult<Box<dyn ConsoleIo>> {
    todo!("P6 Task 8")
}
