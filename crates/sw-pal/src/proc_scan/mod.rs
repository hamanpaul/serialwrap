//! `ProcScan`：行程掃描抽象與 serialwrapd 實例偵測（P4-T4）。
use std::path::PathBuf;

pub mod posix;
#[cfg(windows)]
pub mod windows;

pub use posix::PosixProcScan;
#[cfg(windows)]
pub use windows::WindowsProcScan;

#[cfg(unix)]
pub type NativeProcScan = PosixProcScan;
#[cfg(windows)]
pub type NativeProcScan = WindowsProcScan;

/// 單一 serialwrapd 實例資訊。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DaemonProc {
    pub pid: u32,
    pub socket: Option<String>,
    pub tree: Vec<ProcIdentity>,
}

/// 行程識別資訊（PID 與建立時間戳 / start_ticks）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ProcIdentity {
    pub pid: u32,
    pub start_ticks: Option<u64>,
}

/// Windows 行程快照資訊。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WinProcInfo {
    pub pid: u32,
    pub parent_pid: u32,
    pub image: String,
    pub argv: Vec<String>,
    pub creation_time: u64,
}

/// 命令列引數字系（POSIX 與 Windows 分隔符及比對規則）。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ArgvFlavor {
    Posix,
    Windows,
}

/// TTY 持有者掃描狀態。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum HolderScanStatus {
    Ok,
    Permission,
    Unknown,
    Unsupported,
}

/// 行程掃描失敗錯誤。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ScanFailure;

impl std::fmt::Display for ScanFailure {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "process scan failure")
    }
}

impl std::error::Error for ScanFailure {}

/// Windows launcher 與 Python 子行程去重（SC-17）。
pub fn dedup_launcher_children(_procs: &[WinProcInfo]) -> Vec<DaemonProc> {
    unimplemented!("P4-T4")
}

/// 判斷命令列是否代表 serialwrapd 實例（規則 1～4）。
pub fn is_serialwrapd_argv(_parts: &[String], _flavor: ArgvFlavor) -> bool {
    unimplemented!("P4-T4")
}

/// 從命令列中擷取 `--socket` 參數值。
pub fn extract_socket_arg(_parts: &[String]) -> Option<String> {
    unimplemented!("P4-T4")
}

/// 行程掃描抽象 trait。
pub trait ProcScan: Send + Sync {
    fn serialwrapd_instances(&self) -> Result<Vec<DaemonProc>, ScanFailure>;
    fn serialwrapd_tty_holders(&self, paths: &[PathBuf]) -> (Vec<u32>, HolderScanStatus);
    fn pid_alive(&self, pid: u32) -> bool;
    fn identity_alive(&self, id: &ProcIdentity) -> bool;
}
