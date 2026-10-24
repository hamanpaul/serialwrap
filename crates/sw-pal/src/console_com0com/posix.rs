//! 非 Windows：com0com console 不支援（P6 Task 8 回 `Unsupported`）。Task 1 骨架。

use crate::console_pty::ConsoleIo;
use crate::traits::PalResult;

pub fn open(_port: &str) -> PalResult<Box<dyn ConsoleIo>> {
    todo!("P6 Task 8")
}
