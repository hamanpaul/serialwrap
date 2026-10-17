//! `ProcessCtl` 的平台實作放在 `posix`／`windows` 子模組，依目標平台擇一編譯。
//!
//! 背景啟動保留 stdin，並依平台建立獨立 session／process group；stdout 與 stderr
//! 導向 null device。同步前景呼叫則繼承標準串流並回傳子行程結束碼。
#[cfg(unix)]
pub mod posix;
#[cfg(windows)]
pub mod windows;
