//! POSIX `/proc` 行程掃描（P4-T4）。
use std::path::{Path, PathBuf};

use crate::proc_scan::{DaemonProc, HolderScanStatus, ProcIdentity, ProcScan, ScanFailure};

/// POSIX `/proc` 檔案系統掃描器。
#[derive(Debug, Clone)]
pub struct PosixProcScan {
    #[allow(dead_code)]
    pub proc_root: PathBuf,
}

impl Default for PosixProcScan {
    fn default() -> Self {
        Self::new()
    }
}

impl PosixProcScan {
    pub fn new() -> Self {
        Self::with_root(Path::new("/proc"))
    }

    pub fn with_root(p: impl AsRef<Path>) -> Self {
        Self {
            proc_root: p.as_ref().to_path_buf(),
        }
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

impl ProcScan for PosixProcScan {
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
