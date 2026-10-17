//! Windows Toolhelp32 行程掃描（P4-T4）。
use std::path::PathBuf;

use crate::proc_scan::{DaemonProc, HolderScanStatus, ProcIdentity, ProcScan, ScanFailure};

/// Windows 行程快照掃描器。
#[derive(Debug, Clone, Default)]
pub struct WindowsProcScan;

impl WindowsProcScan {
    pub fn new() -> Self {
        Self
    }

    pub fn serialwrapd_instances(&self) -> Result<Vec<DaemonProc>, ScanFailure> {
        <Self as ProcScan>::serialwrapd_instances(self)
    }

    pub fn serialwrapd_tty_holders(&self, paths: &[PathBuf]) -> (Vec<u32>, HolderScanStatus) {
        <Self as ProcScan>::serialwrapd_tty_holders(self, paths)
    }

    pub fn pid_alive(&self, pid: u32) -> bool {
        <Self as ProcScan>::pid_alive(self, pid)
    }

    pub fn identity_alive(&self, id: &ProcIdentity) -> bool {
        <Self as ProcScan>::identity_alive(self, id)
    }
}

impl ProcScan for WindowsProcScan {
    fn serialwrapd_instances(&self) -> Result<Vec<DaemonProc>, ScanFailure> {
        unimplemented!("P4-T4")
    }

    fn serialwrapd_tty_holders(&self, _paths: &[PathBuf]) -> (Vec<u32>, HolderScanStatus) {
        unimplemented!("P4-T4")
    }

    fn pid_alive(&self, _pid: u32) -> bool {
        unimplemented!("P4-T4")
    }

    fn identity_alive(&self, _id: &ProcIdentity) -> bool {
        unimplemented!("P4-T4")
    }
}
