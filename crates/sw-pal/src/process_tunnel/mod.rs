//! `ProcessTunnel`（BC-05）：無 pipe 的分離行程、身分核對與整棵樹終止（arch §4.9）。
//!
//! 平台實作在 `posix`／`windows` 子模組。本檔為 P4-T5 紅燈骨架：所有主體為 `unimplemented!("P4-T5")`。

use std::path::Path;
use std::time::Duration;

use crate::traits::PalResult;

#[cfg(unix)]
pub mod posix;
#[cfg(windows)]
pub mod windows;

/// 分離行程的控制代碼；`start_ticks` 用於 PID 重用時的身分核對。
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct TunnelProc {
    pub pid: u32,
    pub group: ProcGroupRef,
    /// POSIX：`/proc/<pid>/stat` 第 22 欄；Windows：CreationTime FILETIME。
    pub start_ticks: Option<u64>,
}

/// 終止範圍：POSIX 為行程群組；Windows 以 Toolhelp32 父子鏈收集整棵樹。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ProcGroupRef {
    Pgid(i32),
    WinTree,
}

pub trait ProcessTunnel: Send + Sync {
    /// stdin 為 NUL，stdout 與 stderr 都導到 `log`（append）；全程不使用 pipe。
    fn spawn_detached(&self, argv: &[String], log: &Path) -> PalResult<TunnelProc>;
    /// POSIX `kill(pid,0)` 加 `start_ticks` 比對；Windows 使用 `OpenProcess`、`GetExitCodeProcess` 與 CreationTime。
    fn alive(&self, p: &TunnelProc) -> bool;
    /// 先送 SIGTERM（或 Windows 整棵樹 `TerminateProcess`），每 0.1 s 檢查，逾時才強制終止。
    fn terminate(&self, p: &TunnelProc, grace: Duration) -> PalResult<()>;
    /// 讀取 `log` 末尾至多 `max` bytes，以 UTF-8 replace 解碼。
    fn stderr_tail(&self, log: &Path, max: usize) -> String;
    /// 無 ControlMaster 平台（Win32-OpenSSH）為 false。
    fn supports_control_master(&self) -> bool;
}
