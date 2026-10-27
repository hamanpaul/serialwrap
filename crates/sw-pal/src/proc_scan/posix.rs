//! POSIX `/proc` 行程掃描（P4-T4）。
#[cfg(unix)]
use std::collections::BTreeSet;
use std::fs;
use std::path::{Path, PathBuf};

#[cfg(unix)]
use crate::proc_holders::posix::{same_tty, HolderFs, RealHolderFs, TtyStat};
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
        let pids = daemons.iter().map(|daemon| daemon.pid).collect::<Vec<_>>();
        #[cfg(unix)]
        {
            tty_holders_with(&RealHolderFs, &self.proc_root, &pids, paths)
        }
        // 非 unix 無 inode 身分可比（`/proc` 掃描器在 Windows 不會走到此處）：證據不足即 Unknown。
        #[cfg(not(unix))]
        {
            let _ = (pids, paths);
            (Vec::new(), HolderScanStatus::Unknown)
        }
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

#[cfg(unix)]
/// `serialwrapd` fd 與 tty 的同一性比對（Python `multi_open.detect_multi_open` 的 holders 段；#R2／parity #64）。
///
/// 以 `stat`（跟隨 fd symlink）取得 inode 身分後經 [`same_tty`] 判定，不以 readlink 字串比對：跨 mount
/// namespace 的同名 `/dev/pts/N` 字串相同卻非同一 tty。tty 本身 stat 失敗即略過該 tty（Python `tty_stat`
/// 回 None）；fd stat 失敗略過該 fd；fd 目錄 PermissionError → `Permission`，其他錯誤 → `Unknown`。
pub fn tty_holders_with(
    fs: &dyn HolderFs,
    proc_root: &Path,
    pids: &[u32],
    paths: &[PathBuf],
) -> (Vec<u32>, HolderScanStatus) {
    let tty_stats = paths
        .iter()
        .filter_map(|path| fs.stat(path).ok())
        .collect::<Vec<TtyStat>>();
    let mut holders = BTreeSet::new();
    let mut status = HolderScanStatus::Ok;
    for pid in pids {
        let fd_dir = proc_root.join(pid.to_string()).join("fd");
        let fds = match fs.list_dir(&fd_dir) {
            Ok(fds) => fds,
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
        for fd in fds {
            let Ok(fd_stat) = fs.stat(&fd_dir.join(&fd)) else {
                continue;
            };
            if tty_stats.iter().any(|tty| same_tty(&fd_stat, tty)) {
                holders.insert(*pid);
            }
        }
    }
    (holders.into_iter().collect(), status)
}
