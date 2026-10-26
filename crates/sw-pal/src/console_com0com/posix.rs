//! 非 Windows：com0com console 不支援（P6 Task 8 回 `Unsupported`）。
use crate::console_pty::ConsoleIo;
use crate::traits::{PalError, PalErrorKind, PalResult};

pub fn open(port: &str) -> PalResult<Box<dyn ConsoleIo>> {
    Err(PalError::plain(
        PalErrorKind::Unsupported,
        format!("com0com serial console 在此平台不支援（port={port}）"),
    ))
}
