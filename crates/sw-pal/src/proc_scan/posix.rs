//! POSIX `/proc` 行程掃描（P4-T4）。
use std::collections::BTreeSet;
use std::fs;
use std::path::{Path, PathBuf};

use crate::proc_scan::{
    extract_socket_arg, is_serialwrapd_argv, ArgvFlavor, DaemonProc, HolderScanStatus,
    ProcDetails, ProcIdentity, ProcScan, ScanFailure,
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
        scan_fd_holders(&self.proc_root, &pids, paths)
    }

    fn proc_details(&self, pid: u32) -> Option<ProcDetails> {
        let dir = self.proc_root.join(pid.to_string());
        let ProcStatRead::Parsed(stat) = read_proc_stat(&self.proc_root, pid) else {
            return None;
        };
        if stat.state == b'Z' || stat.state == b'X' {
            return None;
        }
        let argv = fs::read(dir.join("cmdline"))
            .map(|raw| {
                raw.split(|byte| *byte == 0)
                    .filter(|part| !part.is_empty())
                    .map(|part| String::from_utf8_lossy(part).into_owned())
                    .collect::<Vec<_>>()
            })
            .unwrap_or_default();
        let exe = fs::read_link(dir.join("exe"))
            .ok()
            .map(|path| path.to_string_lossy().into_owned());
        Some(ProcDetails {
            pid,
            ppid: stat.ppid,
            uid: proc_uid(&dir),
            exe,
            argv,
            start_ticks: stat.start_ticks,
        })
    }

    fn current_uid(&self) -> Option<u32> {
        #[cfg(unix)]
        {
            // SAFETY: getuid 無參數、不會失敗。
            Some(unsafe { libc::getuid() })
        }
        #[cfg(not(unix))]
        {
            None
        }
    }

    fn proc_cgroup(&self, pid: u32) -> Option<String> {
        fs::read(self.proc_root.join(pid.to_string()).join("cgroup"))
            .ok()
            .map(|raw| String::from_utf8_lossy(&raw).into_owned())
    }

    fn uart_holders_same_uid(&self, paths: &[PathBuf]) -> (Vec<u32>, HolderScanStatus) {
        let Ok(entries) = fs::read_dir(&self.proc_root) else {
            return (Vec::new(), HolderScanStatus::Unknown);
        };
        let me = self.current_uid();
        let mut pids = Vec::new();
        for entry in entries.flatten() {
            let name = entry.file_name();
            let Some(pid) = name.to_str().and_then(|name| name.parse::<u32>().ok()) else {
                continue;
            };
            // 他 uid 的行程不在掃描範圍（D3.2／D5.2）；uid 讀不到者保守納入。
            match (me, proc_uid(&entry.path())) {
                (Some(me), Some(uid)) if uid != me => continue,
                _ => pids.push(pid),
            }
        }
        scan_fd_holders(&self.proc_root, &pids, paths)
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


/// 掃描 `pids` 的 fd 是否指向 `paths` 中任一 tty（#R2 同一 tty 判準）。
fn scan_fd_holders(proc_root: &Path, pids: &[u32], paths: &[PathBuf]) -> (Vec<u32>, HolderScanStatus) {
    // #R2：與 Python `sw_core.serial_port.same_tty` 同一判準（見 `same_tty`）。比對依 inode 身分，不以
    // readlink 字串：跨 mount namespace（docker 容器）的同名 `/dev/pts/N` 字串相同卻非同一 tty。
    // 目標身分取不到時不得推論為「無 holder」，status 回報 Permission／Unknown。
    let mut status = HolderScanStatus::Ok;
    let mut targets = Vec::with_capacity(paths.len());
    for path in paths {
        match fs::metadata(path) {
            Ok(meta) => targets.push(meta),
            Err(error) if error.kind() == std::io::ErrorKind::PermissionDenied => {
                status = HolderScanStatus::Permission;
            }
            Err(_) => {
                if status == HolderScanStatus::Ok {
                    status = HolderScanStatus::Unknown;
                }
            }
        }
    }
    let mut holders = BTreeSet::new();
    for pid in pids.iter().copied() {
        let fd_dir = proc_root.join(pid.to_string()).join("fd");
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
            // `fs::metadata` 跟隨 /proc/<pid>/fd/N 至 fd 實際指向的 inode。
            let fd_meta = match fs::metadata(entry.path()) {
                Ok(meta) => meta,
                // 行程或 fd 在掃描期間消失屬正常競態。
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => continue,
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
            if targets.iter().any(|tty| same_tty(&fd_meta, tty)) {
                holders.insert(pid);
            }
        }
    }
    (holders.into_iter().collect(), status)
}

struct ProcStat {
    state: u8,
    ppid: Option<u32>,
    start_ticks: Option<u64>,
}

/// `/proc/<pid>/status` 的 `Uid:` 第一欄（real uid）。
fn proc_uid(dir: &Path) -> Option<u32> {
    let status = fs::read_to_string(dir.join("status")).ok()?;
    status
        .lines()
        .find_map(|line| line.strip_prefix("Uid:"))
        .and_then(|rest| rest.split_whitespace().next())
        .and_then(|uid| uid.parse::<u32>().ok())
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
    let ppid = fields.get(1).and_then(|value| {
        std::str::from_utf8(value)
            .ok()
            .and_then(|value| value.parse::<u32>().ok())
    });
    ProcStatRead::Parsed(ProcStat {
        state,
        ppid,
        start_ticks,
    })
}

/// 同一 tty 判定（#R2，與 Python `serial_port.same_tty` 一致）：`(dev, ino)` 相同；或兩邊皆為字元裝置、
/// `rdev` 相等且非零、且 major 不在 devpts 範圍。devpts 各 instance 會重用 136:N，不以 rdev 判定。
#[cfg(unix)]
fn same_tty(fd: &fs::Metadata, tty: &fs::Metadata) -> bool {
    use std::os::unix::fs::{FileTypeExt, MetadataExt};

    if fd.dev() == tty.dev() && fd.ino() == tty.ino() {
        return true;
    }
    if !(fd.file_type().is_char_device() && tty.file_type().is_char_device()) {
        return false;
    }
    let rdev = tty.rdev();
    rdev != 0 && fd.rdev() == rdev && !is_devpts_major(rdev)
}

/// Linux `dev_t` 的 major（與 glibc `gnu_dev_major` 同一位元運算）落在 UNIX98 devpts 範圍 136–143。
#[cfg(unix)]
fn is_devpts_major(rdev: u64) -> bool {
    let major = ((rdev >> 8) & 0xfff) | ((rdev >> 32) & !0xfff_u64);
    (136..=143).contains(&major)
}

/// 非 unix 無 `(dev, ino)` 身分可用；`PosixProcScan` 的 holder 判定僅於 unix 實際使用（Windows 走 `WindowsProcScan`）。
#[cfg(not(unix))]
fn same_tty(_fd: &fs::Metadata, _tty: &fs::Metadata) -> bool {
    false
}
