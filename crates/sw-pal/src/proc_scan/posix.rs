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

    /// 外部行程持有、且與本 daemon 所見為同一 tty 的路徑字串集合（排除自身 pid）；`proc_root` 不可列舉時回
    /// `None`（P6 Task 8；uart_io.py `_enumerate_all_held_paths` #R2 版）。見 [`held_tty_paths_with`]。
    pub fn held_fd_targets(&self) -> Option<std::collections::HashSet<String>> {
        held_tty_paths_with(
            &self.proc_root,
            &|path: &Path| fs::read_link(path),
            &|path: &Path| crate::tty_identity::stat_identity(path),
        )
    }
}

/// console reaper 的 held 集合（#40／#64，Python `_enumerate_all_held_paths` #R2 版逐步對照）。
///
/// fd 的 readlink 字串只是「該行程 mount namespace 內」的路徑：一條 fd 只有在「fd 實際指向的 inode」
/// （`stat(/proc/<pid>/fd/N)`）與「該字串於本 daemon 命名空間 stat 到的 inode」為同一 tty
/// （`tty_identity::same_tty`）時才計入。非 `/dev/` 開頭的 link（pipe:／socket:／一般檔）不做 stat；
/// 個別 pid／fd 讀取或任一 stat 失敗即略過該 fd（身分取不到不得計為 peer）。`proc_root` 不可列舉回 `None`。
/// `readlink`／`stat` 以參數注入，使「字串相同、inode 不同」的他 namespace 情境可測。
pub fn held_tty_paths_with(
    proc_root: &Path,
    readlink: &dyn Fn(&Path) -> std::io::Result<PathBuf>,
    stat: &dyn Fn(&Path) -> std::io::Result<crate::tty_identity::TtyIdentity>,
) -> Option<std::collections::HashSet<String>> {
    let self_pid = std::process::id().to_string();
    let mut held = std::collections::HashSet::new();
    for entry in fs::read_dir(proc_root).ok()?.flatten() {
        let name = entry.file_name();
        let name = name.to_string_lossy();
        if name.is_empty() || !name.bytes().all(|b| b.is_ascii_digit()) || name == self_pid {
            continue;
        }
        let Ok(fds) = fs::read_dir(entry.path().join("fd")) else {
            continue;
        };
        for fd in fds.flatten() {
            let fd_path = fd.path();
            let Ok(target) = readlink(&fd_path) else {
                continue;
            };
            let target = target.to_string_lossy().into_owned();
            if !target.starts_with("/dev/") {
                continue;
            }
            let Ok(fd_id) = stat(&fd_path) else {
                continue;
            };
            let Ok(target_id) = stat(Path::new(&target)) else {
                continue;
            };
            if crate::tty_identity::same_tty(&fd_id, &target_id) {
                held.insert(target);
            }
        }
    }
    Some(held)
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
