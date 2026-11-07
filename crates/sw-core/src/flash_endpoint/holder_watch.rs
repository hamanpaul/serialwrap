//! P8 Task 9：flasher 斷線偵測（Python `service.py` `_watch_flasher_disconnect`；CLAUDE.md MCU 坑 3）。
//!
//! daemon 同持端點 PTY 的 master 與 slave（keepalive），flasher 關閉端點時 master 收不到 EOF；改以 holder-probe
//! 輪詢端點 slave 的外部持有者：曾偵測到持有（armed）之後歸零即視為 flasher 結束。每 `poll` 一次、最長 `max`，
//! 等待一律經 [`Clock`]（可被本次 flash 的取消喚醒）。結束（armed 後歸零、逾時、已取消）一律
//! [`FlashRunCancel::cancel`]——**只**取消本次 flash，簽章不接受 `EndpointShutdown`，結構上不可能停止端點（R2-F5）。
//!
//! 以 `EndpointShutdown` 呼叫不能編譯（holder watcher 不可能停止常駐端點）：
//!
//! ```compile_fail
//! use std::time::Duration;
//! use sw_core::flash_endpoint::{watch_flasher_disconnect, EndpointShutdown};
//! fn f(excl: &dyn sw_pal::traits::UartExclusive, clock: &dyn sw_pal::traits::Clock, s: &EndpointShutdown) {
//!     watch_flasher_disconnect(excl, "/dev/pts/9", s, clock, Duration::from_millis(500), Duration::from_secs(1800));
//! }
//! ```
//!
//! 對照組（同一呼叫改傳 `FlashRunCancel` 即可編譯，證明上例失敗只因型別不符）：
//!
//! ```no_run
//! use std::time::Duration;
//! use sw_core::flash_endpoint::{watch_flasher_disconnect, FlashRunCancel};
//! fn f(excl: &dyn sw_pal::traits::UartExclusive, clock: &dyn sw_pal::traits::Clock, run: &FlashRunCancel) {
//!     watch_flasher_disconnect(excl, "/dev/pts/9", run, clock, Duration::from_millis(500), Duration::from_secs(1800));
//! }
//! ```
//!
//! pump 同理：
//!
//! ```compile_fail
//! use std::time::Duration;
//! use sw_core::flash_endpoint::{pump_endpoint_to_sink, EndpointShutdown};
//! fn f(m: &mut dyn sw_pal::console_pty::ConsoleIo, s: &EndpointShutdown) {
//!     let mut sink = |_: &[u8]| true;
//!     pump_endpoint_to_sink(m, &mut sink, s, 4096, Duration::from_millis(200));
//! }
//! ```
//!
//! ```no_run
//! use std::time::Duration;
//! use sw_core::flash_endpoint::{pump_endpoint_to_sink, FlashRunCancel};
//! fn f(m: &mut dyn sw_pal::console_pty::ConsoleIo, run: &FlashRunCancel) {
//!     let mut sink = |_: &[u8]| true;
//!     pump_endpoint_to_sink(m, &mut sink, run, 4096, Duration::from_millis(200));
//! }
//! ```

use std::time::Duration;

use sw_pal::traits::{Clock, UartExclusive};

use super::coordinator::FlashRunCancel;

/// Python `_watch_flasher_disconnect(slave_path, stop, poll_s=0.5, max_s=1800.0)`。
///
/// `/proc` 不可讀（`HolderScanStatus` 非 Ok）時 Python 得 `pids=[]`，與「無 holder」不可區分：依 `pids` 判定，
/// 永不 armed，直到 `max` 逾時才結束（不 panic、不誤判 armed）。
pub fn watch_flasher_disconnect(
    excl: &dyn UartExclusive,
    slave_path: &str,
    run: &FlashRunCancel,
    clock: &dyn Clock,
    poll: Duration,
    max: Duration,
) {
    let _ = (excl, slave_path, run, clock, poll, max);
    todo!("P8-T9")
}
