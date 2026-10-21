//! POSIX `/proc` 行程掃描（P4-T4）。
use std::collections::BTreeSet;
use std::fs;
use std::path::{Path, PathBuf};

use crate::proc_scan::{
    extract_socket_arg, is_serialwrapd_argv, ArgvFlavor, DaemonProc, HolderScanStatus,
    ProcIdentity, ProcScan, ScanFailure,
};

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
        let entries = fs::read_dir(&self.proc_root).map_err(|_| ScanFailure)?;
        let mut daemons = Vec::new();
        for entry in entries {
            let entry = entry.map_err(|_| ScanFailure)?;
            let name = entry.file_name();
            let name = name.to_string_lossy();
            if !name.bytes().all(|byte| byte.is_ascii_digit()) || name.is_empty() {
                continue;
            }
            let Ok(pid) = name.parse::<u32>() else {
                continue;
            };
            let Ok(raw_cmdline) = fs::read(entry.path().join("cmdline")) else {
                continue;
            };
            let parts = raw_cmdline
                .split(|byte| *byte == 0)
                .filter(|part| !part.is_empty())
                .map(|part| String::from_utf8_lossy(part).into_owned())
                .collect::<Vec<_>>();
            if !is_serialwrapd_argv(&parts, ArgvFlavor::Posix) {
                continue;
            }
            daemons.push(DaemonProc {
                pid,
                socket: extract_socket_arg(&parts),
                tree: vec![ProcIdentity {
                    pid,
                    start_ticks: proc_start_ticks(&self.proc_root, pid),
                }],
            });
        }
        daemons.sort_by_key(|daemon| daemon.pid);
        Ok(daemons)
    }

    fn serialwrapd_tty_holders(&self, paths: &[PathBuf]) -> (Vec<u32>, HolderScanStatus) {
        let Ok(daemons) = self.serialwrapd_instances() else {
            return (Vec::new(), HolderScanStatus::Unknown);
        };
        let expected = paths.iter().collect::<BTreeSet<_>>();
        let mut holders = BTreeSet::new();
        let mut status = HolderScanStatus::Ok;
        for daemon in daemons {
            let fd_dir = self.proc_root.join(daemon.pid.to_string()).join("fd");
            let entries = match fs::read_dir(fd_dir) {
                Ok(entries) => entries,
                Err(error) if error.kind() == std::io::ErrorKind::PermissionDenied => {
                    status = HolderScanStatus::Permission;
                    continue;
                }
                Err(_) => {
                    if status == HolderScanStatus::Ok {
                        status = HolderScanStatus::Unknown;
                    }
                    continue;
                }
            };
            for entry in entries {
                let Ok(entry) = entry else {
                    if status == HolderScanStatus::Ok {
                        status = HolderScanStatus::Unknown;
                    }
                    continue;
                };
                let Ok(target) = fs::read_link(entry.path()) else {
                    continue;
                };
                if expected.contains(&target) {
                    holders.insert(daemon.pid);
                }
            }
        }
        (holders.into_iter().collect(), status)
    }

    fn pid_alive(&self, pid: u32) -> bool {
        match read_proc_stat(&self.proc_root, pid) {
            ProcStatRead::Missing => false,
            ProcStatRead::Unreadable => true,
            ProcStatRead::Parsed(stat) => stat.state != b'Z' && stat.state != b'X',
        }
    }

    fn identity_alive(&self, id: &ProcIdentity) -> bool {
        match read_proc_stat(&self.proc_root, id.pid) {
            ProcStatRead::Missing => false,
            ProcStatRead::Unreadable => true,
            ProcStatRead::Parsed(stat) => {
                if stat.state == b'Z' || stat.state == b'X' {
                    return false;
                }
                match (id.start_ticks, stat.start_ticks) {
                    (Some(expected), Some(actual)) => expected == actual,
                    _ => true,
                }
            }
        }
    }
}

struct ProcStat {
    state: u8,
    start_ticks: Option<u64>,
}

enum ProcStatRead {
    Missing,
    Unreadable,
    Parsed(ProcStat),
}

fn proc_start_ticks(root: &Path, pid: u32) -> Option<u64> {
    match read_proc_stat(root, pid) {
        ProcStatRead::Parsed(stat) => stat.start_ticks,
        ProcStatRead::Missing | ProcStatRead::Unreadable => None,
    }
}

fn read_proc_stat(root: &Path, pid: u32) -> ProcStatRead {
    let contents = match fs::read(root.join(pid.to_string()).join("stat")) {
        Ok(contents) => contents,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            return ProcStatRead::Missing;
        }
        Err(_) => return ProcStatRead::Unreadable,
    };
    let Some(command_end) = contents.iter().rposition(|byte| *byte == b')') else {
        return ProcStatRead::Unreadable;
    };
    let fields = contents[command_end + 1..]
        .split(|byte| byte.is_ascii_whitespace())
        .filter(|field| !field.is_empty())
        .collect::<Vec<_>>();
    let Some(state_field) = fields
        .first()
        .filter(|field| field.len() == 1 && field[0].is_ascii_alphabetic())
    else {
        return ProcStatRead::Unreadable;
    };
    let state = state_field[0];
    let start_ticks = fields.get(19).and_then(|value| {
        std::str::from_utf8(value)
            .ok()
            .and_then(|value| value.parse::<u64>().ok())
    });
    ProcStatRead::Parsed(ProcStat { state, start_ticks })
}
