//! POSIX `/proc` 行程掃描（P4-T4）。
use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::path::{Path, PathBuf};

use crate::proc_scan::{
    extract_socket_arg, is_serialwrapd_argv, ArgvFlavor, DaemonProc, HolderScan, HolderScanOutcome,
    HolderScanStatus, ProcDetails, ProcIdentity, ProcScan, ScanFailure, UidMatch, UninspectableProc,
    UninspectableReason, UninspectableSet,
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

    fn uart_holders_same_uid(&self, paths: &[PathBuf]) -> HolderScan {
        let Ok(entries) = fs::read_dir(&self.proc_root) else {
            return HolderScan::from_pair((Vec::new(), HolderScanStatus::Unknown));
        };
        let me = self.current_uid();
        let mut pids = Vec::new();
        // 掃描範圍內每個行程的四 uid 分類（只供回報，不決定放行）。
        let mut uid_info = BTreeMap::new();
        let mut list_failed = false;
        for entry in entries {
            let Ok(entry) = entry else {
                // 列舉 `/proc` 中途失敗：其餘行程沒有檢查，不得推論為沒有 holder。
                list_failed = true;
                continue;
            };
            let name = entry.file_name();
            let Some(pid) = name.to_str().and_then(|name| name.parse::<u32>().ok()) else {
                continue;
            };
            // 四個 uid 都不是自身的行程不在掃描範圍（D3.2／D5.2）；uid 讀不到或只有部分相同（setuid 工具）者照常納入。
            let uids = proc_uids(&entry.path());
            let Some(uid_match) = uid_match(me, uids) else {
                continue;
            };
            uid_info.insert(pid, (uid_match, uids));
            pids.push(pid);
        }
        let mut scan = scan_fd(&self.proc_root, &pids, paths);
        if list_failed {
            scan.findings.push(Finding::ProcListFailed);
        }
        let outcome = scan.holder_outcome(|pid, reason| {
            let (uid_match, uids) = uid_info.get(&pid).copied().unwrap_or((UidMatch::Unknown, None));
            UninspectableProc {
                pid,
                start_ticks: proc_start_ticks(&self.proc_root, pid),
                comm: proc_comm(&self.proc_root, pid),
                reason,
                uid_match,
                uids,
            }
        });
        HolderScan {
            holders: scan.holders,
            outcome,
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


/// 掃描 `pids` 的 fd 是否指向 `paths` 中任一 tty（#R2 同一 tty 判準）。
fn scan_fd_holders(proc_root: &Path, pids: &[u32], paths: &[PathBuf]) -> (Vec<u32>, HolderScanStatus) {
    let scan = scan_fd(proc_root, pids, paths);
    let status = scan.strict_status();
    (scan.holders, status)
}

/// 讀取失敗的嚴重度（`Permission` 優先於 `Unknown`）。
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
enum ReadFailure {
    Unknown,
    Permission,
}

/// [`scan_fd`] 的每一項發現。這是掃描結果唯一的失敗來源：兩種判定（[`FdScan::strict_status`]、
/// [`FdScan::holder_outcome`]）都以窮舉 `match` 解讀，新增種類時編譯器會要求兩邊都處理，不會因漏設旗標而 fail-open。
#[derive(Debug, Clone, Copy)]
enum Finding {
    /// 目標 tty 的 metadata 讀不到（節點不存在以外的錯誤）。
    TargetUnreadable(ReadFailure),
    /// 目標 tty 節點不存在（例如等待中拔掉 USB）。
    TargetVanished,
    /// 列舉 `/proc` 中途失敗，其餘行程沒有檢查。
    ProcListFailed,
    /// `/proc/<pid>/fd` 在掃描期間消失（NotFound：行程已結束）。
    ProcVanished,
    /// 整個行程無法檢查。
    ProcUninspectable(u32, UninspectableReason),
    /// 行程部分 fd 條目回 PermissionDenied（`Permission`），或有其他讀取失敗（`Unknown`）。
    ProcFailed(ReadFailure),
}

/// [`scan_fd`] 的結果。
struct FdScan {
    holders: Vec<u32>,
    findings: Vec<Finding>,
}

fn worst_status(worst: Option<ReadFailure>) -> HolderScanStatus {
    match worst {
        None => HolderScanStatus::Ok,
        Some(ReadFailure::Unknown) => HolderScanStatus::Unknown,
        Some(ReadFailure::Permission) => HolderScanStatus::Permission,
    }
}

impl FdScan {
    /// 原有判定（`serialwrapd_tty_holders`，行為不變）：任一 PermissionDenied（含整個無法檢查的行程）→ `Permission`；
    /// 否則任一讀取失敗、目標節點不存在或行程在掃描中消失 → `Unknown`；否則 `Ok`。
    fn strict_status(&self) -> HolderScanStatus {
        let worst = self
            .findings
            .iter()
            .map(|finding| match *finding {
                Finding::TargetUnreadable(kind) | Finding::ProcFailed(kind) => kind,
                Finding::ProcUninspectable(..) => ReadFailure::Permission,
                Finding::TargetVanished | Finding::ProcListFailed | Finding::ProcVanished => {
                    ReadFailure::Unknown
                }
            })
            .max();
        worst_status(worst)
    }

    /// OD-48 D5.2（`uart_holders_same_uid`）：目標節點不存在與行程在掃描中消失不可能衝突，不算失敗。其餘失敗中，只有
    /// 「行程整個無法檢查」不在這一層擋下（不論四 uid 分類；放不放行由呼叫端以停止前快照決定，編排者裁決 2026-11-07）；
    /// 全部失敗都屬此類時回 `Partial`。有任何其他失敗時沿用原本的嚴重度判定（整個無法檢查的行程也計入，
    /// 任一 PermissionDenied 即 `Permission`）。
    fn holder_outcome(
        &self,
        describe: impl Fn(u32, UninspectableReason) -> UninspectableProc,
    ) -> HolderScanOutcome {
        let mut worst = None;
        let mut blocked = false;
        let mut uninspectable = Vec::new();
        for finding in &self.findings {
            let (severity, blocks) = match *finding {
                Finding::TargetUnreadable(kind) | Finding::ProcFailed(kind) => (Some(kind), true),
                Finding::ProcListFailed => (Some(ReadFailure::Unknown), true),
                Finding::TargetVanished | Finding::ProcVanished => (None, false),
                Finding::ProcUninspectable(pid, reason) => {
                    uninspectable.push(describe(pid, reason));
                    (Some(ReadFailure::Permission), false)
                }
            };
            worst = worst.max(severity);
            blocked |= blocks;
        }
        uninspectable.sort_by_key(|item| item.pid);
        if blocked {
            return match worst_status(worst) {
                HolderScanStatus::Permission => HolderScanOutcome::Permission(uninspectable),
                _ => HolderScanOutcome::Unknown(uninspectable),
            };
        }
        match UninspectableSet::new(uninspectable) {
            Some(set) => HolderScanOutcome::Partial(set),
            None => HolderScanOutcome::Ok,
        }
    }
}

fn scan_fd(proc_root: &Path, pids: &[u32], paths: &[PathBuf]) -> FdScan {
    // #R2：與 Python `sw_core.serial_port.same_tty` 同一判準（見 `same_tty`）。比對依 inode 身分，不以
    // readlink 字串：跨 mount namespace（docker 容器）的同名 `/dev/pts/N` 字串相同卻非同一 tty。
    // 目標身分取不到時不得推論為「無 holder」：記成 `Finding`，由呼叫端的判定決定狀態。
    let mut findings = Vec::new();
    let mut targets = Vec::with_capacity(paths.len());
    for path in paths {
        match fs::metadata(path) {
            Ok(meta) => targets.push(meta),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                findings.push(Finding::TargetVanished);
            }
            Err(error) if error.kind() == std::io::ErrorKind::PermissionDenied => {
                findings.push(Finding::TargetUnreadable(ReadFailure::Permission));
            }
            Err(_) => findings.push(Finding::TargetUnreadable(ReadFailure::Unknown)),
        }
    }
    let mut holders = BTreeSet::new();
    for pid in pids.iter().copied() {
        let fd_dir = proc_root.join(pid.to_string()).join("fd");
        let entries = match fs::read_dir(fd_dir) {
            Ok(entries) => entries,
            // 行程在掃描期間結束（與條目層的 NotFound 同屬正常競態）。
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                findings.push(Finding::ProcVanished);
                continue;
            }
            Err(error) if error.kind() == std::io::ErrorKind::PermissionDenied => {
                findings.push(Finding::ProcUninspectable(pid, UninspectableReason::FdDirDenied));
                continue;
            }
            Err(_) => {
                findings.push(Finding::ProcFailed(ReadFailure::Unknown));
                continue;
            }
        };
        // 本行程的 fd 條目：檢查得了的數量、PermissionDenied 的數量、是否有其他讀取失敗、是否在列舉中途結束。
        let (mut inspected, mut denied, mut failed, mut vanished) = (0usize, 0usize, false, false);
        for entry in entries {
            let entry = match entry {
                Ok(entry) => entry,
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                    vanished = true;
                    break;
                }
                Err(_) => {
                    failed = true;
                    continue;
                }
            };
            // `fs::metadata` 跟隨 /proc/<pid>/fd/N 至 fd 實際指向的 inode。
            let fd_meta = match fs::metadata(entry.path()) {
                Ok(meta) => meta,
                // 行程或 fd 在掃描期間消失屬正常競態。
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => continue,
                Err(error) if error.kind() == std::io::ErrorKind::PermissionDenied => {
                    denied += 1;
                    continue;
                }
                Err(_) => {
                    failed = true;
                    continue;
                }
            };
            inspected += 1;
            if targets.iter().any(|tty| same_tty(&fd_meta, tty)) {
                holders.insert(pid);
            }
        }
        // 核心以整個行程為單位檢查 fd 連結的存取權（例如 `systemd --user` 持有呼叫端沒有的 capability）：
        // 目錄列得出、條目卻一個都檢查不到，與 fd 目錄整個讀不到同屬「整個行程無法檢查」。
        // 部分條目可讀、部分 PermissionDenied 的混合情況，或另有其他讀取失敗，仍算掃描失敗。
        let finding = if vanished {
            Some(Finding::ProcVanished)
        } else if denied > 0 && inspected == 0 && !failed {
            Some(Finding::ProcUninspectable(pid, UninspectableReason::FdEntriesDenied))
        } else if denied > 0 {
            Some(Finding::ProcFailed(ReadFailure::Permission))
        } else if failed {
            Some(Finding::ProcFailed(ReadFailure::Unknown))
        } else {
            None
        };
        findings.extend(finding);
    }
    FdScan {
        holders: holders.into_iter().collect(),
        findings,
    }
}

/// `/proc/<pid>/comm`（去掉行尾換行）；讀不到為 `None`。
fn proc_comm(root: &Path, pid: u32) -> Option<String> {
    let comm = fs::read_to_string(root.join(pid.to_string()).join("comm")).ok()?;
    Some(comm.trim_end_matches('\n').to_string())
}

struct ProcStat {
    state: u8,
    ppid: Option<u32>,
    start_ticks: Option<u64>,
}

/// 行程 uid 與自身 uid 的關係（OD-48 D5.2 的掃描範圍與四 uid 分類）：四個 uid（real、effective、saved、fs）
/// 都不是自身 → `None`（他 uid，不在掃描範圍）；都等於自身 → `All`；只有部分等於自身（例如同使用者執行的 setuid
/// 工具 `Uid: 1000 0 0 0`）→ `Partial`；uid 讀不到或自身 uid 不明 → `Unknown`（照常納入掃描）。
fn uid_match(me: Option<u32>, uids: Option<[u32; 4]>) -> Option<UidMatch> {
    match (me, uids) {
        (Some(me), Some(uids)) if uids.iter().all(|uid| *uid == me) => Some(UidMatch::All),
        (Some(me), Some(uids)) if uids.iter().all(|uid| *uid != me) => None,
        (Some(_), Some(_)) => Some(UidMatch::Partial),
        _ => Some(UidMatch::Unknown),
    }
}

/// `/proc/<pid>/status` 的 `Uid:` 四欄（real、effective、saved、fs）；欄位不足或無法解析為 `None`。
fn proc_uids(dir: &Path) -> Option<[u32; 4]> {
    let status = fs::read_to_string(dir.join("status")).ok()?;
    let rest = status.lines().find_map(|line| line.strip_prefix("Uid:"))?;
    let mut fields = rest.split_whitespace().map(|uid| uid.parse::<u32>().ok());
    let uids = [fields.next()??, fields.next()??, fields.next()??, fields.next()??];
    Some(uids)
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
