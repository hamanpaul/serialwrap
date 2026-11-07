//! P8 Task 9：flasher → MCU 的 pump（Python `flash_endpoint.py` `pump_endpoint_to_sink`）。
//!
//! 只等 master 可讀（`wait_readable`，等價 `select(…, 0.2)`），可讀才讀一個 chunk 交給 sink；sink 回 false
//! （`NotSubmitted`／TX 失敗，等價 Python `flash_tx` 拋 `RuntimeError`／`OSError`）、master `Eof`、讀取或等待失敗、
//! 或本次 flash 已取消（[`FlashRunCancel::is_cancelled`]）即結束。`first` 已在認線 probe 送出，**不**重送。

use std::time::Duration;

use sw_pal::console_pty::ConsoleIo;

use super::coordinator::FlashRunCancel;

/// Python `pump_endpoint_to_sink(master_fd, sink, stop_event, first_bytes=b"", chunk=4096)`（`first_bytes` 一律為空）。
///
/// 簽章只接受 [`FlashRunCancel`]：pump 結構上不可能設定端點停止旗標（R2-F5）。
pub fn pump_endpoint_to_sink(
    master: &mut dyn ConsoleIo,
    sink: &mut dyn FnMut(&[u8]) -> bool,
    run: &FlashRunCancel,
    chunk: usize,
    select: Duration,
) {
    let _ = (master, sink, run, chunk, select);
    todo!("P8-T9")
}
