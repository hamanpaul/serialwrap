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

impl DaemonProc {
    pub fn win(pid: u32, ep: &str) -> Self {
        Self {
            pid,
            socket: Some(ep.to_string()),
            tree: vec![ProcIdentity {
                pid,
                start_ticks: None,
            }],
        }
    }
}

/// 行程識別資訊（PID 與建立時間戳 / start_ticks）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ProcIdentity {
    pub pid: u32,
    pub start_ticks: Option<u64>,
}

/// 單一行程的辨識資訊（OD-48 D1／D3：正向辨識 0.3.x Python daemon、同 uid 判定與父行程檢查）。
/// 取不到的欄位為 `None`／空，呼叫端一律視為「無法正向辨識」而 fail-closed。
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct ProcDetails {
    pub pid: u32,
    pub ppid: Option<u32>,
    /// POSIX real uid；Windows 為 `None`。
    pub uid: Option<u32>,
    /// POSIX `/proc/<pid>/exe` 解析結果；Windows 為完整映像路徑（取不到時退回映像名稱）。
    pub exe: Option<String>,
    pub argv: Vec<String>,
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
    PartialOtherUid,
    Permission,
    Unknown,
    Unsupported,
    /// 只由 [`ProcScan::uart_holders_same_uid`] 回傳：目標 tty 身分都取得（或節點已不存在）、讀得到的行程都檢查完畢，
    /// 只剩掃描範圍內的行程整個無法檢查（見 [`UninspectableReason`]；例如 systemd user session 的 `(sd-pam)` 與
    /// `systemd --user`，或同使用者執行的 setuid 工具 `fusermount3`），清單見 [`HolderScanOutcome::Partial`]。
    /// 每個行程附四 uid 分類（[`UidMatch`]），只供分類與回報，不決定放行。
    /// 不代表這些行程沒有持有 tty；呼叫端要接受時必須自行限定範圍（OD-48 D5.2：停止前快照，見 [`UninspectableSnapshot`]）
    /// 並把清單回報出來。
    Partial,
}

impl HolderScanStatus {
    /// 報告欄位 `holders_status` 的字串（OD-48 legacy 停止報告；與 [`HolderScanStatus::py_name`] 同一組字串）。
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Ok => "ok",
            Self::PartialOtherUid => "partial_other_uid",
            Self::Permission => "permission",
            Self::Unknown => "unknown",
            Self::Unsupported => "unsupported",
            Self::Partial => "partial",
        }
    }
}

/// 行程整個無法檢查的原因。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum UninspectableReason {
    /// `/proc/<pid>/fd` 目錄本身回 PermissionDenied（non-dumpable 行程，例如 `(sd-pam)`）。
    FdDirDenied,
    /// fd 目錄列得出，但條目（至少一個）全部回 PermissionDenied、沒有任何條目檢查得了
    /// （核心以整個行程為單位檢查 fd 連結的存取權，例如持有呼叫端沒有之 capability 的 `systemd --user`）。
    FdEntriesDenied,
}

impl UninspectableReason {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::FdDirDenied => "fd_dir_denied",
            Self::FdEntriesDenied => "fd_entries_denied",
        }
    }
}

/// 行程的四個 uid（real、effective、saved、fs）與自身 uid 的關係（四 uid 判定）。
/// OD-48 D5.2（編排者裁決 2026-11-07）：只用來分類與回報「是否為同 uid 行程」，不決定擋或放。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum UidMatch {
    /// 四個 uid 都等於自身（同 uid 行程）。
    All,
    /// 只有部分等於自身（例如同使用者執行的 setuid 工具：`fusermount3` 為 `Uid: 1000 0 0 0`）。
    Partial,
    /// uid 讀不到（或自身 uid 不明），無法分類。
    Unknown,
}

impl UidMatch {
    /// 報告欄位 `uid_match` 的字串。
    pub fn as_str(self) -> &'static str {
        match self {
            Self::All => "all",
            Self::Partial => "partial",
            Self::Unknown => "unknown",
        }
    }
}

/// 整個無法檢查 fd 的行程。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UninspectableProc {
    pub pid: u32,
    /// `/proc/<pid>/stat` 的 starttime（與 `pid` 一起辨識行程，避免 pid 重用誤判）；讀不到為 `None`。
    pub start_ticks: Option<u64>,
    /// `/proc/<pid>/comm`（去掉行尾換行）；讀不到為 `None`。
    pub comm: Option<String>,
    pub reason: UninspectableReason,
    /// 四 uid 分類（只供回報）。
    pub uid_match: UidMatch,
    /// `/proc/<pid>/status` 的 `Uid:` 四欄（real、effective、saved、fs）；讀不到為 `None`。
    pub uids: Option<[u32; 4]>,
}

/// 非空、依 pid 排序的無法檢查行程清單（[`HolderScanOutcome::Partial`] 必定帶一份，組不出空清單的 `Partial`）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UninspectableSet(Vec<UninspectableProc>);

impl UninspectableSet {
    /// 空清單回 `None`。
    pub fn new(mut procs: Vec<UninspectableProc>) -> Option<Self> {
        if procs.is_empty() {
            return None;
        }
        procs.sort_by_key(|item| item.pid);
        Some(Self(procs))
    }

    pub fn procs(&self) -> &[UninspectableProc] {
        &self.0
    }
}

/// [`HolderScan`] 的判定；無法檢查的行程清單放在 variant 內，狀態由 variant 推導（[`HolderScan::status`]）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum HolderScanOutcome {
    Ok,
    Unsupported,
    /// 有不得推論為「無 holder」的 PermissionDenied 類讀取失敗；附帶整個無法檢查的行程（可能為空）。
    Permission(Vec<UninspectableProc>),
    /// 有其他讀取失敗；附帶整個無法檢查的行程（可能為空）。
    Unknown(Vec<UninspectableProc>),
    /// 見 [`HolderScanStatus::Partial`]。
    Partial(UninspectableSet),
}

/// [`ProcScan::uart_holders_same_uid`] 的結果。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HolderScan {
    pub holders: Vec<u32>,
    pub outcome: HolderScanOutcome,
}

impl HolderScan {
    /// 由舊式 `(holders, status)` 組成（無「無法檢查」清單）。`Partial` 沒有清單即不成立，轉為 `Unknown`（fail-closed）；
    /// `PartialOtherUid` 只屬 holder probe／開埠路徑（rulings/s2.md WP5-9），出現在此處同樣以 `Unknown` 處理（fail-closed）。
    pub fn from_pair((holders, status): (Vec<u32>, HolderScanStatus)) -> Self {
        let outcome = match status {
            HolderScanStatus::Ok => HolderScanOutcome::Ok,
            HolderScanStatus::Unsupported => HolderScanOutcome::Unsupported,
            HolderScanStatus::Permission => HolderScanOutcome::Permission(Vec::new()),
            HolderScanStatus::Unknown | HolderScanStatus::Partial | HolderScanStatus::PartialOtherUid => {
                HolderScanOutcome::Unknown(Vec::new())
            }
        };
        Self { holders, outcome }
    }

    pub fn status(&self) -> HolderScanStatus {
        match self.outcome {
            HolderScanOutcome::Ok => HolderScanStatus::Ok,
            HolderScanOutcome::Unsupported => HolderScanStatus::Unsupported,
            HolderScanOutcome::Permission(_) => HolderScanStatus::Permission,
            HolderScanOutcome::Unknown(_) => HolderScanStatus::Unknown,
            HolderScanOutcome::Partial(_) => HolderScanStatus::Partial,
        }
    }

    /// 整個無法檢查的行程（依 pid 排序）。`Partial` 時必定非空；`Permission`／`Unknown` 時也可能非空
    /// （表示除此之外另有其他讀取失敗）。
    pub fn uninspectable(&self) -> &[UninspectableProc] {
        match &self.outcome {
            HolderScanOutcome::Ok | HolderScanOutcome::Unsupported => &[],
            HolderScanOutcome::Permission(procs) | HolderScanOutcome::Unknown(procs) => procs,
            HolderScanOutcome::Partial(set) => set.procs(),
        }
    }
}

/// OD-48 D5.2（編排者裁決 2026-11-07）：停止前快照——送出 stop 前整個無法檢查 fd 的行程（pid＋start_ticks）。
///
/// 停止後的掃描只放行快照內的同一行程，不論四 uid 分類；停止後才出現（含 pid 相同、start_ticks 不同）者一律不放行。
/// 快照掃描本身另有其他讀取失敗（[`HolderScanOutcome::Permission`]／[`HolderScanOutcome::Unknown`]，例如列舉
/// `/proc` 中途失敗）時，清單不完整、不得採信：整份記為 [`UninspectableSnapshot::Failed`]，任何行程都不放行（fail-closed）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum UninspectableSnapshot {
    Taken(Vec<ProcIdentity>),
    Failed,
}

impl UninspectableSnapshot {
    /// 由送出 stop 前的 holder 掃描建立。
    pub fn from_scan(scan: &HolderScan) -> Self {
        match &scan.outcome {
            HolderScanOutcome::Ok | HolderScanOutcome::Unsupported => Self::Taken(Vec::new()),
            HolderScanOutcome::Partial(set) => Self::Taken(
                set.procs()
                    .iter()
                    .map(|item| ProcIdentity { pid: item.pid, start_ticks: item.start_ticks })
                    .collect(),
            ),
            HolderScanOutcome::Permission(_) | HolderScanOutcome::Unknown(_) => Self::Failed,
        }
    }

    /// `item` 是否為快照中的同一行程：pid 相同且 start_ticks 兩邊都取得、相等；快照失敗時一律 `false`。
    pub fn admits(&self, item: &UninspectableProc) -> bool {
        match self {
            Self::Failed => false,
            Self::Taken(ids) => {
                item.start_ticks.is_some()
                    && ids.iter().any(|id| id.pid == item.pid && id.start_ticks == item.start_ticks)
            }
        }
    }

    /// 報告欄位 `before_stop_snapshot` 的字串。
    pub fn as_str(&self) -> &'static str {
        match self {
            Self::Taken(_) => "ok",
            Self::Failed => "failed",
        }
    }
}

impl HolderScanStatus {
    /// Python holder probe 的狀態字串；health 掃描另將 `PartialOtherUid` 正規化為 `permission`。
    /// `Partial` 只由 OD-48 的 `uart_holders_same_uid` 產生，不會出現在 holder probe。
    pub fn py_name(self) -> &'static str {
        self.as_str()
    }
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
    /// fail-closed 版（CLI 更新／回滾確認釋放用；Python `serial_port.probe_holders` 語意）：目標 tty 取不到
    /// 身分、或 fd 因權限／其他錯誤讀不到時，不得推論「無 holder」，status 回 `Permission`／`Unknown`。
    fn serialwrapd_tty_holders(&self, paths: &[PathBuf]) -> (Vec<u32>, HolderScanStatus);
    /// daemon status／doctor 的多開偵測版（Python `multi_open.detect_multi_open` 語意）：tty 本身 stat 失敗即
    /// 略過該 tty、fd stat 失敗略過該 fd，只有 fd 目錄讀不到才降級 status。預設沿用 fail-closed 版（替身）。
    fn multi_open_tty_holders(&self, paths: &[PathBuf]) -> (Vec<u32>, HolderScanStatus) {
        self.serialwrapd_tty_holders(paths)
    }
    fn pid_alive(&self, pid: u32) -> bool;
    fn identity_alive(&self, id: &ProcIdentity) -> bool;
    /// OD-48 D1／D3：指定 pid 的辨識資訊；行程不存在或平台不支援 → `None`。
    fn proc_details(&self, _pid: u32) -> Option<ProcDetails> {
        None
    }
    /// 目前行程的 real uid（POSIX）；Windows 與替身預設為 `None`（呼叫端視為同 uid）。
    fn current_uid(&self) -> Option<u32> {
        None
    }
    /// OD-48 D5：與目前行程同 uid 的**任何**行程是否仍持有 `paths`（不限 serialwrapd）。
    /// 預設沿用 serialwrapd holder 掃描；POSIX 實作掃所有至少一個 uid（real、effective、saved、fs）等於自身的行程
    /// （四個 uid 都不同者不在範圍內），只剩整個無法檢查的行程時回 `Partial` 並附四 uid 分類；Windows 回 `Unsupported`。
    fn uart_holders_same_uid(&self, paths: &[PathBuf]) -> HolderScan {
        HolderScan::from_pair(self.serialwrapd_tty_holders(paths))
    }
    /// OD-48 OWNER-BATCH #3：`/proc/<pid>/cgroup` 原文（Linux）；行程不存在或平台不支援 → `None`。
    /// 由 [`classify_cgroup`] 判定該行程所屬的監管者。
    fn proc_cgroup(&self, _pid: u32) -> Option<String> {
        None
    }
}

/// OD-48 OWNER-BATCH #3：依 cgroup 判定的行程監管歸屬。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CgroupOwner {
    /// serialwrap 自己的 systemd unit（`serialwrap.service`，system 或 user manager 底下）。
    SerialwrapUnit,
    /// 非 serialwrap 的 systemd service unit，或 system.slice 底下其他監管者的 transient scope（容器執行環境等）。
    ForeignUnit(String),
    /// 使用者登入 session、終端機或 user manager 的 app scope（一般 shell 啟動）。
    Session,
    /// 無法判定（根 cgroup、格式不明或讀不到）；呼叫端退回父行程判定。
    Unknown,
}

/// OD-48 OWNER-BATCH #3：解析 `/proc/<pid>/cgroup`。cgroup v2 取 `0::<path>`，v1 取 `name=systemd` 階層；
/// 以路徑最後一段判定：`*.service` 為 systemd service unit（`serialwrap.service` 以外即外部監管者），
/// `*.scope` 一律視為一般 session，不因位於 `system.slice` 或 `user.slice` 而轉為外部監管者。
pub fn classify_cgroup(content: &str) -> CgroupOwner {
    let mut path = None;
    for line in content.lines() {
        let mut fields = line.splitn(3, ':');
        let (Some(id), Some(controllers), Some(value)) = (fields.next(), fields.next(), fields.next()) else {
            continue;
        };
        if id == "0" && controllers.is_empty() {
            path = Some(value);
            break;
        }
        if controllers == "name=systemd" {
            path = Some(value);
        }
    }
    let Some(path) = path.map(str::trim) else {
        return CgroupOwner::Unknown;
    };
    let parts = path.split('/').filter(|part| !part.is_empty()).collect::<Vec<_>>();
    let Some(leaf) = parts.last() else {
        return CgroupOwner::Unknown;
    };
    if leaf.ends_with(".service") {
        if *leaf == "serialwrap.service" {
            return CgroupOwner::SerialwrapUnit;
        }
        return CgroupOwner::ForeignUnit((*leaf).to_string());
    }
    if leaf.ends_with(".scope") {
        return CgroupOwner::Session;
    }
    CgroupOwner::Unknown
}

/// OD-48 D1.3：執行檔是否為 Python 直譯器（basename `python`、`python3`、`python3.12`…；Windows 另含 `.exe`）。
pub fn is_python_interpreter(path: &str) -> bool {
    let base = path.rsplit(|ch| ch == '/' || ch == '\\').next().unwrap_or(path);
    let base = base
        .get(base.len().saturating_sub(4)..)
        .filter(|suffix| suffix.eq_ignore_ascii_case(".exe"))
        .and_then(|_| base.get(..base.len() - 4))
        .unwrap_or(base);
    let lower = base.to_ascii_lowercase();
    let Some(rest) = lower.strip_prefix("python") else {
        return false;
    };
    rest.chars().all(|ch| ch.is_ascii_digit() || ch == '.')
}

/// OD-48 D1.3：argv 是否為 0.3.x Python daemon（`…/serialwrapd` script 或 `-m sw_core.daemon`）；
/// 回傳 script 路徑（`-m` 形式為 `None`）。
pub fn python_daemon_script(argv: &[String], flavor: ArgvFlavor) -> Option<Option<String>> {
    if argv
        .windows(2)
        .any(|pair| pair[0] == "-m" && pair[1] == "sw_core.daemon")
    {
        return Some(None);
    }
    argv.iter().skip(1).find_map(|part| {
        let script = match flavor {
            ArgvFlavor::Posix => part.contains('/') && posix_basename(part) == "serialwrapd",
            ArgvFlavor::Windows => {
                (part.contains('/') || part.contains('\\'))
                    && (windows_name_eq(part, "serialwrapd")
                        || windows_basename(part).eq_ignore_ascii_case("serialwrapd-script.py"))
            }
        } || part.ends_with("serialwrapd.py");
        script.then(|| Some(part.clone()))
    })
}
