//! POSIX `/proc` 序列埠 holder 診斷；此快照不取代 `flock`／`TIOCEXCL`。
//!
//! 同一 tty 判定逐字照 Python `serial_port.same_tty`／`probe_holders`（#R2；parity-amendments-2026-10-26 #64）：
//! 以 `stat(/proc/<pid>/fd/N)`（跟隨至 fd 實際指向的 inode）與 `stat(real_path)` 的 `(st_dev, st_ino)` 比對；
//! 不以 readlink 字串或 devpts `st_rdev` 判定——跨 mount namespace（docker 容器）的同名 `/dev/pts/N`
//! 字串相同、rdev 亦同，卻非同一 tty。

use std::collections::{BTreeMap, BTreeSet, VecDeque};
use std::fs;
use std::io;
use std::os::unix::fs::{FileTypeExt, MetadataExt};
use std::path::{Path, PathBuf};

use crate::traits::{HolderReport, HolderScanStatus, UartExclusive};

/// POSIX `/proc` holder 掃描器。
pub struct PosixUartExclusive;

impl UartExclusive for PosixUartExclusive {
    fn probe_holders(&self, real_path: &str) -> HolderReport {
        probe_holders_in(Path::new("/proc"), real_path)
    }
}

/// `stat` 結果中判定同一 tty 所需的欄位（對應 Python `os.stat_result` 的 `st_dev／st_ino／st_mode／st_rdev`）。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct TtyStat {
    pub dev: u64,
    pub ino: u64,
    pub is_char_device: bool,
    pub rdev: u64,
}

impl TtyStat {
    fn from_metadata(meta: &fs::Metadata) -> TtyStat {
        TtyStat {
            dev: meta.dev(),
            ino: meta.ino(),
            is_char_device: meta.file_type().is_char_device(),
            rdev: meta.rdev(),
        }
    }
}

/// holder 掃描所需的檔案系統操作（正式實作為真實 `/proc`；測試可注入假 inode 身分）。
pub trait HolderFs {
    /// 列出目錄項目名稱（對應 `os.listdir`）。
    fn list_dir(&self, path: &Path) -> io::Result<Vec<String>>;
    /// `stat`（跟隨 symlink；對應 `os.stat`）。
    fn stat(&self, path: &Path) -> io::Result<TtyStat>;
    /// 呼叫者的 real uid；預設無法確認，既有替身維持保守分類。
    fn current_uid(&self) -> Option<u32> {
        None
    }
    /// `proc_root/<pid>` 目錄 owner 的 uid；不得由 fd 或目標 tty 推測。
    fn process_uid(&self, _proc_root: &Path, _pid: u32) -> Option<u32> {
        None
    }
}

/// 真實檔案系統。
pub struct RealHolderFs;

impl HolderFs for RealHolderFs {
    fn list_dir(&self, path: &Path) -> io::Result<Vec<String>> {
        let mut names = Vec::new();
        for entry in fs::read_dir(path)? {
            names.push(entry?.file_name().to_string_lossy().into_owned());
        }
        Ok(names)
    }

    fn stat(&self, path: &Path) -> io::Result<TtyStat> {
        fs::metadata(path).map(|meta| TtyStat::from_metadata(&meta))
    }

    fn current_uid(&self) -> Option<u32> {
        Some(unsafe { libc::getuid() } as u32)
    }

    fn process_uid(&self, proc_root: &Path, pid: u32) -> Option<u32> {
        fs::metadata(proc_root.join(pid.to_string()))
            .ok()
            .map(|meta| meta.uid())
    }
}

/// UNIX98 devpts slave 的 major 範圍（136–143）；各 devpts instance 皆可能重用 136:N。
fn is_devpts_major(rdev: u64) -> bool {
    // 與 glibc `gnu_dev_major` 同一位元運算。
    let major = ((rdev >> 8) & 0xfff) | ((rdev >> 32) & !0xfff_u64);
    (136..=143).contains(&major)
}

/// 同一 tty 判定（Python `serial_port.same_tty` 逐字）：`(dev, ino)` 相同；或兩邊皆為字元裝置、
/// `rdev` 相等且非零、且 major 不在 devpts 範圍（例如容器以 mknod 掛入同一實體 UART 節點）。
pub fn same_tty(fd: &TtyStat, tty: &TtyStat) -> bool {
    if fd.dev == tty.dev && fd.ino == tty.ino {
        return true;
    }
    if !(fd.is_char_device && tty.is_char_device) {
        return false;
    }
    tty.rdev != 0 && fd.rdev == tty.rdev && !is_devpts_major(tty.rdev)
}

/// 掃描 `proc_root/<pid>/fd/*`，排除自身行程樹，並以 inode 身分判斷 holder（真實檔案系統）。
pub fn probe_holders_in(proc_root: &Path, path: &str) -> HolderReport {
    probe_holders_excluding(
        &RealHolderFs,
        proc_root,
        path,
        &process_tree_pids(proc_root, std::process::id()),
    )
}

/// `probe_holders` 的可注入版本（Python `serial_port.probe_holders(real_path, proc_root=, exclude_pid=)`）。
pub fn probe_holders_with(
    fs: &dyn HolderFs,
    proc_root: &Path,
    path: &str,
    exclude_pid: Option<u32>,
) -> HolderReport {
    let excluded = exclude_pid.into_iter().collect::<BTreeSet<_>>();
    probe_holders_excluding(fs, proc_root, path, &excluded)
}

fn probe_holders_excluding(
    fs: &dyn HolderFs,
    proc_root: &Path,
    path: &str,
    excluded_pids: &BTreeSet<u32>,
) -> HolderReport {
    // 無法取得目標 tty 身分時不得推論為「無 holder」（serial_port.py：target stat 失敗即回 unknown）。
    let target = match fs.stat(Path::new(path)) {
        Ok(target) => target,
        Err(error) if error.kind() == io::ErrorKind::PermissionDenied => {
            return report(BTreeSet::new(), HolderScanStatus::Permission);
        }
        Err(_) => return report(BTreeSet::new(), HolderScanStatus::Unknown),
    };
    let mut pids = BTreeSet::new();
    let mut status = HolderScanStatus::Ok;
    let current_uid = fs.current_uid();
    let mut other_uid_unreadable = false;

    // Python：os.listdir(proc_root) 的 PermissionError 與其他 OSError 皆回 permission。
    let entries = match fs.list_dir(proc_root) {
        Ok(entries) => entries,
        Err(_) => return report(pids, HolderScanStatus::Permission),
    };

    for name in entries {
        if name.is_empty() || !name.bytes().all(|byte| byte.is_ascii_digit()) {
            continue;
        }
        let Ok(pid) = name.parse::<u32>() else {
            continue;
        };
        if excluded_pids.contains(&pid) {
            continue;
        }
        let fd_dir: PathBuf = proc_root.join(&name).join("fd");
        let fds = match fs.list_dir(&fd_dir) {
            Ok(fds) => fds,
            Err(error) if error.kind() == io::ErrorKind::PermissionDenied => {
                // 只有已確認他 uid 的權限限制可降為局部不可讀；同 uid 或 owner 未知仍維持保守分類。
                let owner_uid = fs.process_uid(proc_root, pid);
                if current_uid
                    .zip(owner_uid)
                    .is_some_and(|(current, owner)| current != owner)
                {
                    other_uid_unreadable = true;
                } else {
                    status = HolderScanStatus::Permission;
                }
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
            let fd_stat = match fs.stat(&fd_dir.join(&fd)) {
                Ok(fd_stat) => fd_stat,
                // 行程或 fd 在掃描期間消失屬正常競態。
                Err(error) if error.kind() == io::ErrorKind::NotFound => continue,
                Err(error) if error.kind() == io::ErrorKind::PermissionDenied => {
                    let owner_uid = fs.process_uid(proc_root, pid);
                    if current_uid
                        .zip(owner_uid)
                        .is_some_and(|(current, owner)| current != owner)
                    {
                        other_uid_unreadable = true;
                    } else {
                        status = HolderScanStatus::Permission;
                    }
                    continue;
                }
                Err(_) => {
                    if status == HolderScanStatus::Ok {
                        status = HolderScanStatus::Unknown;
                    }
                    continue;
                }
            };
            if same_tty(&fd_stat, &target) {
                pids.insert(pid);
                break;
            }
        }
    }

    if status == HolderScanStatus::Ok && other_uid_unreadable {
        status = HolderScanStatus::PartialOtherUid;
    }
    report(pids, status)
}

/// 取得 root PID 與其 `/proc` 可見的所有後代。正式 holder 掃描排除自身行程樹，
/// 避免 daemon 在 fork→exec 窗口 spawn 的 sysenv／process_tunnel 子行程被誤認為外部 holder。
fn process_tree_pids(proc_root: &Path, root_pid: u32) -> BTreeSet<u32> {
    let mut children_by_parent = BTreeMap::<u32, Vec<u32>>::new();
    if let Ok(entries) = fs::read_dir(proc_root) {
        for entry in entries.flatten() {
            let name = entry.file_name();
            let Some(name) = name.to_str() else {
                continue;
            };
            if name.is_empty() || !name.bytes().all(|byte| byte.is_ascii_digit()) {
                continue;
            }
            let Ok(pid) = name.parse::<u32>() else {
                continue;
            };
            let Ok(stat) = fs::read_to_string(entry.path().join("stat")) else {
                continue;
            };
            if let Some(ppid) = parse_parent_pid(&stat) {
                children_by_parent.entry(ppid).or_default().push(pid);
            }
        }
    }

    let mut tree = BTreeSet::from([root_pid]);
    let mut frontier = VecDeque::from([root_pid]);
    while let Some(parent) = frontier.pop_front() {
        if let Some(children) = children_by_parent.get(&parent) {
            for &child in children {
                if tree.insert(child) {
                    frontier.push_back(child);
                }
            }
        }
    }
    tree
}

/// `/proc/<pid>/stat` 的 comm 可包含空白與右括號；以最後一個 `)` 後的欄位解析 state、PPID。
fn parse_parent_pid(stat: &str) -> Option<u32> {
    let fields = stat.get(stat.rfind(')')? + 1..)?.split_whitespace();
    fields.skip(1).next()?.parse().ok()
}

/// `HolderReport(pids, not pids and status != "ok", status)`（serial_port.py `probe_holders` 尾段）。
fn report(pids: BTreeSet<u32>, status: HolderScanStatus) -> HolderReport {
    let pids = pids.into_iter().collect::<Vec<_>>();
    HolderReport {
        holder_unknown: pids.is_empty()
            && !matches!(
                status,
                HolderScanStatus::Ok | HolderScanStatus::PartialOtherUid
            ),
        pids,
        status,
    }
}
