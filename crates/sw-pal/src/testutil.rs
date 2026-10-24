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
