//! Windows 無 PTY（P6 Task 8）：`create_pty` 回 `Unsupported`，human console 走 TCP＋Telnet（arch §4.7）。
use crate::console_pty::PtyPair;
use crate::traits::{PalError, PalErrorKind, PalResult};

pub(crate) fn create_pty() -> PalResult<PtyPair> {
    Err(PalError::plain(
        PalErrorKind::Unsupported,
        "create_pty 在此平台不支援（無 PTY，改用 TCP console）",
    ))
}
