//! `ProcessCtl` 的平台實作放在 `posix`／`windows` 子模組，依目標平台擇一編譯。
//!
//! 背景啟動保留 stdin，並依平台建立獨立 session／process group；stdout 與 stderr
//! 導向 null device。同步前景呼叫則繼承標準串流並回傳子行程結束碼。
#[cfg(unix)]
pub mod posix;
#[cfg(windows)]
pub mod windows;

/// 前景執行程序的啟動選項。
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct ExecOpts {
    /// Windows 下不建立 console 視窗。
    pub no_window: bool,
    /// Windows 下以 Job Object 確保 launcher 結束時清除子行程。
    pub kill_on_close: bool,
}

/// `ProcessCtl::spawn_elevated` 的結果（P4-T19，SC-36，OD-15）。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ElevatedOutcome {
    /// 提權行程已結束，附結束碼。
    Exited(i32),
    /// 使用者拒絕 UAC（`ERROR_CANCELLED`）。
    Declined,
}
