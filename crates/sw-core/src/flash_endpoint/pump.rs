//! P8 Task 9：flasher → MCU 的 pump（Python `flash_endpoint.py` `pump_endpoint_to_sink`）。
//!
//! 只等 master 可讀（`wait_readable`，等價 `select(…, 0.2)`），可讀才讀一個 chunk 交給 sink；sink 回 false
//! （`NotSubmitted`／TX 失敗，等價 Python `flash_tx` 拋 `RuntimeError`／`OSError`）、master `Eof`、讀取或等待失敗、
//! 或本次 flash 已取消（[`FlashRunCancel::is_cancelled`]）即結束。`first` 已在認線 probe 送出，**不**重送。

use std::time::Duration;

use sw_pal::console_pty::{ConsoleIo, Readiness};
use sw_pal::traits::ReadOutcome;

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
    let mut buf = vec![0u8; chunk.max(1)];
    while !run.is_cancelled() {
        match master.wait_readable(Some(select)) {
            // 等價 `select` 的 OSError。
            Err(_) => return,
            Ok(Readiness::Ready) => {}
            // 逾時或被本次 run 的取消喚醒：回到迴圈頂端檢查取消。
            Ok(Readiness::TimedOut | Readiness::Woken) => continue,
        }
        match master.read(&mut buf, Some(Duration::ZERO)) {
            Ok(ReadOutcome::Data(n)) => {
                if !sink(&buf[..n]) {
                    return; // NotSubmitted／TX 失敗：bridge 中途掉線，讓上層 exit flashing 收尾
                }
            }
            // EOF：flasher 已關閉端點（daemon 持 slave keepalive 時通常不會發生）。
            Ok(ReadOutcome::Eof) => return,
            // 讀與等待之間的喚醒：回到迴圈頂端檢查取消。
            Ok(ReadOutcome::Woken) => continue,
            // 可讀卻讀不到（EAGAIN，等價 `BlockingIOError`）或讀取錯誤：Python 一律結束 pump。
            Ok(ReadOutcome::TimedOut) | Err(_) => return,
        }
    }
}
