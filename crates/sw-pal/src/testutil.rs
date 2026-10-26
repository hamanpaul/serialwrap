//! console 測試工具（P6 Task 1 骨架；feature `test-fakes`；本體由 Task 8 交付）。
//! 平台 cfg 只在 sw-pal 內，讓 sw-core／sw-daemon 測試不需自行分支。

use std::path::Path;
use std::time::Duration;

use crate::console_pty::{ConsoleIo, ConsoleListener, PtyPair};
use crate::traits::PalResult;

/// `tcgetattr` 的可比較摘要（raw 模式斷言用）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TermiosView {
    pub iflag: u64,
    pub oflag: u64,
    pub cflag: u64,
    pub lflag: u64,
    pub vmin: u8,
    pub vtime: u8,
    /// 平台 `CREAD`／`CLOCAL`／`CS8` 位元值（讓斷言不必直接碰 `libc`）。
    #[allow(non_snake_case)]
    pub CREAD: u64,
    #[allow(non_snake_case)]
    pub CLOCAL: u64,
    #[allow(non_snake_case)]
    pub CS8: u64,
}

pub fn tcgetattr_path(_p: &str) -> PalResult<TermiosView> {
    todo!("P6 Task 8")
}

pub fn master_is_nonblocking(_p: &PtyPair) -> bool {
    todo!("P6 Task 8")
}

pub fn no_symlink_points_to(_target: &str, _dirs: &[&Path]) -> bool {
    todo!("P6 Task 8")
}

/// `^/dev/pts/\d+$` 手寫判斷（不引入 regex）。
pub fn is_dev_pts_path(_p: &str) -> bool {
    todo!("P6 Task 8")
}

pub fn wait_accept(_l: &mut dyn ConsoleListener, _timeout: Duration) -> (Box<dyn ConsoleIo>, String) {
    todo!("P6 Task 8")
}

pub fn nodelay(_io: &dyn ConsoleIo) -> bool {
    todo!("P6 Task 8")
}

pub fn reuseaddr(_l: &dyn ConsoleListener) -> bool {
    todo!("P6 Task 8")
}

/// 連線到已關閉 loopback port 的錯誤（`winsock_error_message_format`）。
pub fn tcp_connect_closed_port_error() -> crate::traits::PalError {
    todo!("P6 Task 8")
}

/// com0com console handle 上的設定 API 呼叫計數（SC-29 控制線隔離證據）。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct CommApiCalls {
    pub set_comm_state: u32,
    pub escape_comm_function: u32,
}

pub fn comm_api_calls(_io: &dyn ConsoleIo) -> CommApiCalls {
    todo!("P6 Task 8")
}

/// 對 peer 端（COMb）改 baud 並切換 DTR／RTS。
pub fn toggle_peer_modem_lines(_port: &str) {
    todo!("P6 Task 8")
}
