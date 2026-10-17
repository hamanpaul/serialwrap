//! `ProcScan`：行程掃描抽象與 serialwrapd 實例偵測（P4-T4）。
use std::collections::HashMap;
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
pub fn dedup_launcher_children(procs: &[WinProcInfo]) -> Vec<DaemonProc> {
    let mut daemon_procs = Vec::new();
    let mut launcher_indices = HashMap::new();

    for proc in procs {
        let launcher = is_launcher_image(&proc.image);
        let python_daemon =
            is_python_image(&proc.image) && is_serialwrapd_argv(&proc.argv, ArgvFlavor::Windows);
        if !launcher && !python_daemon {
            continue;
        }

        let daemon = DaemonProc {
            pid: proc.pid,
            socket: extract_socket_arg(&proc.argv),
            tree: vec![ProcIdentity {
                pid: proc.pid,
                start_ticks: (proc.creation_time != 0).then_some(proc.creation_time),
            }],
        };
        let index = daemon_procs.len();
        if launcher {
            launcher_indices.insert(proc.pid, index);
        }
        daemon_procs.push((proc, daemon));
    }

    let mut parent_links = vec![None; daemon_procs.len()];
    for (child_index, (child, _)) in daemon_procs.iter().enumerate() {
        if child.creation_time == 0 {
            continue;
        }
        let Some(&parent_index) = launcher_indices.get(&child.parent_pid) else {
            continue;
        };
        let parent_creation = daemon_procs[parent_index].0.creation_time;
        if parent_index == child_index
            || parent_creation == 0
            || child.creation_time < parent_creation
        {
            continue;
        }
        parent_links[child_index] = Some(parent_index);
    }

    let roots = (0..daemon_procs.len())
        .map(|index| launcher_root(index, &parent_links))
        .collect::<Vec<_>>();
    let mut removed = vec![false; daemon_procs.len()];
    for (child_index, root_index) in roots.iter().copied().enumerate() {
        let Some(root_index) = root_index else {
            continue;
        };
        if root_index == child_index {
            continue;
        }
        let child = daemon_procs[child_index].0;
        let child_creation = child.creation_time;
        let child_identity = ProcIdentity {
            pid: child.pid,
            start_ticks: (child_creation != 0).then_some(child_creation),
        };
        let child_socket = daemon_procs[child_index].1.socket.clone();
        let root_daemon = &mut daemon_procs[root_index].1;
        if root_daemon.socket.is_none() {
            root_daemon.socket = child_socket;
        }
        root_daemon.tree.push(child_identity);
        removed[child_index] = true;
    }

    let mut result = daemon_procs
        .into_iter()
        .enumerate()
        .filter_map(|(index, (_, mut daemon))| {
            if removed[index] {
                return None;
            }
            daemon.tree.sort_by_key(|identity| identity.pid);
            Some(daemon)
        })
        .collect::<Vec<_>>();
    result.sort_by_key(|daemon| daemon.pid);
    result
}

fn launcher_root(index: usize, parents: &[Option<usize>]) -> Option<usize> {
    let mut current = index;
    for _ in 0..=parents.len() {
        match parents[current] {
            Some(parent) if parent != current => current = parent,
            Some(_) => return None,
            None => return Some(current),
        }
    }
    None
}

/// 判斷命令列是否代表 serialwrapd 實例（規則 1～4）。
pub fn is_serialwrapd_argv(parts: &[String], flavor: ArgvFlavor) -> bool {
    let Some(argv0) = parts.first() else {
        return false;
    };
    if match flavor {
        ArgvFlavor::Posix => posix_basename(argv0) == "serialwrapd",
        ArgvFlavor::Windows => windows_name_eq(argv0, "serialwrapd"),
    } {
        return true;
    }

    let has_python_shim = parts.iter().any(|part| match flavor {
        ArgvFlavor::Posix => part.ends_with("serialwrapd.py"),
        ArgvFlavor::Windows => part
            .get(part.len().saturating_sub("serialwrapd.py".len())..)
            .is_some_and(|suffix| suffix.eq_ignore_ascii_case("serialwrapd.py")),
    });
    if has_python_shim {
        return true;
    }

    let path_script = parts.iter().skip(1).any(|part| match flavor {
        ArgvFlavor::Posix => part.contains('/') && posix_basename(part) == "serialwrapd",
        ArgvFlavor::Windows => {
            (part.contains('/') || part.contains('\\')) && windows_name_eq(part, "serialwrapd")
        }
    });
    if path_script {
        return true;
    }

    parts
        .windows(2)
        .any(|pair| pair[0] == "-m" && pair[1] == "sw_core.daemon")
}

/// 從命令列中擷取 `--socket` 參數值。
pub fn extract_socket_arg(parts: &[String]) -> Option<String> {
    for (index, part) in parts.iter().enumerate() {
        if part == "--socket" {
            return parts.get(index + 1).cloned();
        }
        if let Some(value) = part.strip_prefix("--socket=") {
            return Some(value.to_string());
        }
    }
    None
}

fn posix_basename(path: &str) -> &str {
    path.rsplit('/').next().unwrap_or(path)
}

fn windows_basename(path: &str) -> &str {
    path.rsplit(|ch| ch == '/' || ch == '\\')
        .next()
        .unwrap_or(path)
}

fn windows_name_eq(path: &str, expected: &str) -> bool {
    let basename = windows_basename(path);
    let stem = basename
        .get(basename.len().saturating_sub(4)..)
        .filter(|suffix| suffix.eq_ignore_ascii_case(".exe"))
        .and_then(|_| basename.get(..basename.len() - 4))
        .unwrap_or(basename);
    stem.eq_ignore_ascii_case(expected)
}

fn is_launcher_image(image: &str) -> bool {
    image.eq_ignore_ascii_case("serialwrapd.exe")
}

fn is_python_image(image: &str) -> bool {
    image.len() >= "python.exe".len()
        && image
            .get(..6)
            .is_some_and(|prefix| prefix.eq_ignore_ascii_case("python"))
        && image
            .get(image.len() - 4..)
            .is_some_and(|suffix| suffix.eq_ignore_ascii_case(".exe"))
}

/// 行程掃描抽象 trait。
pub trait ProcScan: Send + Sync {
    fn serialwrapd_instances(&self) -> Result<Vec<DaemonProc>, ScanFailure>;
    fn serialwrapd_tty_holders(&self, paths: &[PathBuf]) -> (Vec<u32>, HolderScanStatus);
    fn pid_alive(&self, pid: u32) -> bool;
    fn identity_alive(&self, id: &ProcIdentity) -> bool;
}
