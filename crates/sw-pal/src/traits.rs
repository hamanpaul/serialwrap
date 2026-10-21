//! client 側 trait 與共通錯誤型別。

use std::ffi::OsString;
use std::path::{Path, PathBuf};
use std::time::Duration;

// 明列項目（不用 glob）：避免 proc_scan 的 `posix`／`windows` 子模組名洩漏成 `sw_pal::posix`。
pub use crate::device_source::windows_model::{
    exclude_bluetooth, read_bt_ports, RegistryView, SERIALCOMM_KEY,
};
pub use crate::fs_ops::{FileLockGuard, FsOps, LinkKind};
#[cfg(windows)]
pub use crate::proc_scan::WindowsProcScan;
pub use crate::proc_scan::{
    dedup_launcher_children, extract_socket_arg, is_serialwrapd_argv, ArgvFlavor, DaemonProc,
    HolderScanStatus, PosixProcScan, ProcIdentity, ProcScan, ScanFailure, WinProcInfo,
};
pub use crate::process_tunnel::{ProcGroupRef, ProcessTunnel, TunnelProc};
pub use crate::service_ctl::{
    encode_utf16le_bom, render_task_xml, shell_join_windows, split_windows_cmdline,
    AutostartHoldKind, AutostartHoldRecord, CmdRecord, CompositeServiceCtl, HoldState,
    ServiceAction, ServiceCtl, ServiceResult, SupervisionMode, SupervisorDefinition, SystemdCtl,
    TaskQuery, TaskXmlSpec, WindowsServiceCtl, WindowsTaskCtl,
};

// P4-T19：提權結果型別定義於 process 模組，此處再匯出供 trait 與呼叫端共用。
pub use crate::process::ElevatedOutcome;

pub type PalResult<T> = Result<T, PalError>;

/// Python `OSError` 的平台無關表示；`py_message` 為 Python `str(OSError)` 格式，不含檔名。
#[derive(Debug, Clone, PartialEq)]
pub struct PalError {
    pub kind: PalErrorKind,
    pub os_code: Option<i32>,
    pub winerror: Option<i32>,
    pub strerror: String,
    /// Python str(OSError)，不含檔名。
    pub py_message: String,
}

impl PalError {
    /// 建立已依目前平台格式化的錯誤。
    pub fn new(
        kind: PalErrorKind,
        os_code: Option<i32>,
        winerror: Option<i32>,
        strerror: impl Into<String>,
        py_message: impl Into<String>,
    ) -> Self {
        Self {
            kind,
            os_code,
            winerror,
            strerror: strerror.into(),
            py_message: py_message.into(),
        }
    }

    /// 建立沒有作業系統錯誤碼的 PAL 錯誤。
    pub fn plain(kind: PalErrorKind, message: impl Into<String>) -> Self {
        let message = message.into();
        Self::new(kind, None, None, message.clone(), message)
    }
}

/// 將 Python `errno` 常見值映射到 PAL 的平台無關種類。
pub(crate) fn kind_from_errno(code: i32) -> PalErrorKind {
    match code {
        1 => PalErrorKind::AccessDenied,
        2 | 3 => PalErrorKind::NotFound,
        5 => PalErrorKind::Io,
        9 => PalErrorKind::Io,
        4 | 10004 => PalErrorKind::Interrupted,
        11 | 35 | 10035 => PalErrorKind::WouldBlock,
        13 | 10013 => PalErrorKind::PermissionDenied,
        16 | 170 => PalErrorKind::Busy,
        17 => PalErrorKind::Io,
        22 | 10022 => PalErrorKind::InvalidInput,
        32 | 104 | 103 | 10053 | 10054 => PalErrorKind::BrokenPipe,
        98 | 10048 => PalErrorKind::AddrInUse,
        110 | 10060 => PalErrorKind::TimedOut,
        111 | 10061 => PalErrorKind::ConnectionRefused,
        95 | 10045 => PalErrorKind::Unsupported,
        125 => PalErrorKind::Cancelled,
        _ => PalErrorKind::Io,
    }
}

#[cfg(unix)]
pub(crate) fn posix_errno(code: i32) -> PalError {
    // SAFETY: `strerror` 回傳靜態緩衝區；立即複製成 owned UTF-8 字串。
    let strerror = unsafe {
        let ptr = libc::strerror(code);
        if ptr.is_null() {
            format!("Unknown error {code}")
        } else {
            std::ffi::CStr::from_ptr(ptr).to_string_lossy().into_owned()
        }
    };
    let kind = kind_from_errno(code);
    PalError::new(
        kind,
        Some(code),
        None,
        strerror.clone(),
        format!("[Errno {code}] {strerror}"),
    )
}

#[cfg(unix)]
pub(crate) fn posix_io_error(error: std::io::Error) -> PalError {
    match error.raw_os_error() {
        Some(code) if code == libc::ETIMEDOUT => timed_out(),
        Some(code) => posix_errno(code),
        None => PalError::plain(PalErrorKind::Io, error.to_string()),
    }
}

pub(crate) fn timed_out() -> PalError {
    PalError::new(PalErrorKind::TimedOut, None, None, "", "timed out")
}

#[cfg(windows)]
pub(crate) fn windows_crt_error(code: i32) -> PalError {
    let strerror = match code {
        1 => "Operation not permitted",
        2 => "No such file or directory",
        5 => "Input/output error",
        7 => "Argument list too long",
        8 => "Exec format error",
        9 => "Bad file descriptor",
        10 => "No child processes",
        11 => "Resource temporarily unavailable",
        12 => "Not enough space",
        13 => "Permission denied",
        17 => "File exists",
        18 => "Cross-device link",
        20 => "Not a directory",
        21 => "Is a directory",
        22 => "Invalid argument",
        24 => "Too many open files",
        28 => "No space left on device",
        32 => "Broken pipe",
        41 => "Directory not empty",
        42 => "Illegal byte sequence",
        107 => "Connection refused",
        138 => "Connection timed out",
        _ => {
            return PalError::new(
                kind_from_errno(code),
                Some(code),
                None,
                format!("Unknown error {code}"),
                format!("[Errno {code}] Unknown error {code}"),
            )
        }
    };
    PalError::new(
        windows_errno_kind(code),
        Some(code),
        None,
        strerror,
        format!("[Errno {code}] {strerror}"),
    )
}

#[cfg(windows)]
pub(crate) fn windows_win32_error(code: u32) -> PalError {
    let errno = windows_win32_errno(code);
    let strerror = windows_message(code);
    PalError::new(
        windows_errno_kind(errno),
        Some(errno),
        Some(code as i32),
        strerror.clone(),
        format!("[WinError {code}] {strerror}"),
    )
}

#[cfg(windows)]
fn windows_errno_kind(code: i32) -> PalErrorKind {
    match code {
        107 => PalErrorKind::ConnectionRefused,
        138 => PalErrorKind::TimedOut,
        _ => kind_from_errno(code),
    }
}

#[cfg(windows)]
pub(crate) fn windows_crt_errno(code: u32) -> i32 {
    match code {
        2 | 3 | 15 | 18 | 53 | 67 | 161 | 206 => 2,
        4 => 24,
        5 | 16 | 19..=36 | 65 | 82 | 83 | 108 | 132 | 158 | 167 => 13,
        6 | 114 | 130 => 9,
        7 | 8 | 9 | 1816 => 12,
        10 => 7,
        11 | 188..=202 => 8,
        12 | 13 | 87 | 131 => 22,
        17 => 18,
        80 | 183 => 17,
        89 | 164 | 215 => 11,
        109 => 32,
        112 => 28,
        128 | 129 => 10,
        145 => 41,
        1113 => 42,
        _ => 22,
    }
}

#[cfg(windows)]
fn windows_win32_errno(code: u32) -> i32 {
    let code = if code & 0xffff_0000 == 0x8007_0000 {
        code & 0x0000_ffff
    } else {
        code
    };
    if (10_000..12_000).contains(&code) {
        return match code {
            10_004 | 10_009 | 10_013 | 10_014 | 10_022 | 10_024 => code as i32 - 10_000,
            _ => code as i32,
        };
    }
    match code {
        2 | 3 | 15 | 18 | 53 | 67 | 161 | 206 => 2,
        10 => 7,
        11 | 188..=202 => 8,
        6 | 114 | 130 => 9,
        128 | 129 => 10,
        89 | 164 | 215 => 11,
        7 | 8 | 9 | 1816 => 12,
        5 | 16 | 19..=36 | 65 | 82 | 83 | 108 | 132 | 158 | 167 => 13,
        80 | 183 => 17,
        17 => 18,
        267 => 20,
        4 => 24,
        112 => 28,
        109 | 232 => 32,
        145 => 41,
        1113 => 42,
        258 => 138,
        _ => 22,
    }
}

#[cfg(windows)]
pub(crate) fn windows_wsa_error(code: i32) -> PalError {
    let strerror = windows_message(code as u32);
    PalError::new(
        kind_from_errno(code),
        Some(code),
        Some(code),
        strerror.clone(),
        format!("[WinError {code}] {strerror}"),
    )
}

#[cfg(windows)]
pub(crate) fn windows_gai_error(code: i32) -> PalError {
    let strerror = "getaddrinfo failed";
    PalError::new(
        PalErrorKind::Io,
        Some(code),
        None,
        strerror,
        format!("[Errno {code}] {strerror}"),
    )
}

#[cfg(windows)]
pub(crate) fn windows_message(code: u32) -> String {
    use std::ffi::c_void;

    #[link(name = "kernel32")]
    unsafe extern "system" {
        fn FormatMessageW(
            flags: u32,
            source: *const c_void,
            message_id: u32,
            language_id: u32,
            buffer: *mut u16,
            size: u32,
            arguments: *const *const i8,
        ) -> u32;
    }

    let mut buffer = [0u16; 2048];
    // SAFETY: buffer 是可寫 UTF-16 陣列；來源為系統訊息表，arguments 為 NULL。
    let length = unsafe {
        FormatMessageW(
            0x1000 | 0x0200,
            std::ptr::null(),
            code,
            0,
            buffer.as_mut_ptr(),
            buffer.len() as u32,
            std::ptr::null(),
        )
    } as usize;
    let mut message = if length == 0 {
        format!("Unknown error {code}")
    } else {
        String::from_utf16_lossy(&buffer[..length])
    };
    while message.ends_with(char::is_whitespace) || message.ends_with('.') {
        message.pop();
    }
    message
}

#[cfg(all(test, windows))]
mod windows_mapping_review_tests {
    use super::*;

    #[test]
    fn maps_win32_errors_with_python_errno_values() {
        assert_eq!(windows_win32_errno(267), 20);
        assert_eq!(windows_win32_errno(123), 22);
        assert_eq!(windows_win32_errno(53), 2);
        assert_eq!(windows_win32_errno(0x8007_0002), 2);
    }

    #[test]
    fn maps_crt_file_errors_with_ucrt_errno_values() {
        assert_eq!(windows_crt_errno(123), 22);
        assert_eq!(windows_crt_errno(53), 2);
        assert_eq!(windows_crt_errno(267), 22);
        assert_eq!(windows_crt_errno(35), 13);
        assert_eq!(windows_crt_errno(193), 8);
        assert_eq!(windows_crt_errno(9999), 22);
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PalErrorKind {
    NotFound,
    PermissionDenied,
    AccessDenied,
    Busy,
    WouldBlock,
    TimedOut,
    Cancelled,
    Interrupted,
    InvalidInput,
    Unsupported,
    BrokenPipe,
    ConnectionRefused,
    AddrInUse,
    Eof,
    Io,
}

/// [`SysEnv::run`]／`run_merged` 逾時時 `CmdOutput.stderr` 的固定標記（呼叫端據此辨識逾時，非 RPC 錯誤碼）。
pub const RUN_TIMEOUT_STDERR: &str = "TIMEOUT";

pub trait SysEnv: Send + Sync {
    fn var(&self, k: &str) -> Option<String>;
    /// 以 `std::env::vars_os()` 實作再 lossy 轉換；不得用 `std::env::vars()`（遇非 UTF-8 會 panic）。
    fn vars(&self) -> Vec<(String, String)>;
    /// SC-09：POSIX `"/etc/serialwrap"`；Windows `"%ProgramData%\serialwrap"`（ProgramData 缺時 `"C:\ProgramData\serialwrap"`）。
    /// CLI 不得自行判斷平台。
    fn system_config_dir(&self) -> String;
    fn current_user_home(&self) -> Option<String>;
    fn user_home(&self, user: &str) -> Option<String>;
    fn current_exe(&self) -> PalResult<PathBuf>;
    fn realpath(&self, p: &Path) -> PalResult<PathBuf>;
    /// 查 stdout fd；不讀 `COLUMNS`。
    fn terminal_columns(&self) -> Option<u16>;
    fn stdout_is_terminal(&self) -> bool;
    /// `= Python errno.errorcode.get(code)`。
    fn errno_name(&self, code: i32) -> Option<&'static str>;
    fn pid(&self) -> u32;

    /// 子行程執行（等同 Python `subprocess.run(capture_output=True, text=True)`）。逾時回 `rc=-1`、`stderr="TIMEOUT"`。
    fn run(&self, argv: &[String], timeout: Option<Duration>) -> PalResult<CmdOutput>;
    /// 子行程合併 stdout／stderr。原生 POSIX／Windows 及正式假環境保留寫入順序；預設替身依 stdout、stderr 串接。
    fn run_merged(&self, argv: &[String], timeout: Option<Duration>) -> PalResult<CmdOutput> {
        let mut output = self.run(argv, timeout)?;
        output.stdout.push_str(&output.stderr);
        output.stderr.clear();
        Ok(output)
    }
    /// 暫時攔截 Ctrl+C，讓呼叫端在清理資源後以 130 結束。
    fn interrupt_scope(&self) -> PalResult<Box<dyn InterruptScope>> {
        Ok(Box::new(NoInterruptScope))
    }
    /// 原始 bytes 版本：不解碼、不做換行轉換；`env=Some(..)` 時取代子行程環境。
    fn run_raw(
        &self,
        argv: &[String],
        env: Option<&[(OsString, OsString)]>,
        timeout: Option<Duration>,
    ) -> PalResult<CmdOutputRaw>;
    /// `shutil.which` 語意；回第一個可執行的完整路徑。
    fn which(&self, name: &str) -> Option<PathBuf>;
    /// 07 §4.2 演算法：依 PATH 順序列出所有候選、去重、略過空目錄字串。
    fn which_all(&self, name: &str) -> Vec<PathBuf>;
    /// `/run/systemd/system` 為目錄；Windows 恆 false。
    fn has_systemd(&self) -> bool;
    /// `uname().release` 含 `microsoft`（小寫比對）；Windows 恆 false。
    fn is_wsl(&self) -> bool;
    /// POSIX：`getgrnam(g).gr_gid ∈ getgroups() ∪ {getegid()}`；Windows 恆 false。
    fn user_in_group(&self, g: &str) -> bool;
    /// POSIX：`$USER` 非空優先，否則 `getpwuid(geteuid()).pw_name`；Windows：Token → `"DOMAIN\\name"`。
    fn user_identity(&self) -> String;
    /// Windows：`%LOCALAPPDATA%`（非空）否則 `%USERPROFILE%\AppData\Local`；POSIX 為 `None`。
    fn local_appdata(&self) -> Option<PathBuf>;
}

pub trait InterruptScope: Send + Sync {
    fn interrupted(&self) -> bool;
}

struct NoInterruptScope;

impl InterruptScope for NoInterruptScope {
    fn interrupted(&self) -> bool {
        false
    }
}

/// `SysEnv::run` 的結果；`stdout`／`stderr` 已依 Python text mode（strict、universal newlines）解碼。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CmdOutput {
    pub rc: i32,
    pub stdout: String,
    pub stderr: String,
}

/// `SysEnv::run_raw` 的結果；原始 bytes。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CmdOutputRaw {
    pub rc: i32,
    pub stdout: Vec<u8>,
    pub stderr: Vec<u8>,
}

pub trait FsRead: Send + Sync {
    /// open()+read；Windows 錯誤以 CRT errno 風格（_dosmaperr＋CRT strerror）。
    fn read(&self, p: &Path) -> PalResult<Vec<u8>>;
    /// `os.path.exists`。
    fn exists(&self, p: &Path) -> bool;
    /// `os.path.isdir`。
    fn is_dir(&self, p: &Path) -> bool;
    /// 判斷路徑本身是否為一般檔案，不跟隨 symlink。
    fn is_file(&self, p: &Path) -> bool;
    /// `os.listdir`；Windows 錯誤為 WinError 風格。
    fn list_dir(&self, p: &Path) -> PalResult<Vec<String>>;
}

pub trait Clock: Send + Sync {
    fn mono_ns(&self) -> u64;
    fn sleep(&self, d: Duration);
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum EndpointSpec {
    Unix(OsString),
    Tcp { host: String, port: u16 },
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub enum SockTimeout {
    /// 整體上限（Python `settimeout(d)`）。
    Blocking(Duration),
    /// Python `settimeout(0)` 非阻塞語意（Q-10）。
    NonBlocking,
}

pub trait RpcConnector: Send + Sync {
    fn af_unix_available(&self) -> bool;
    fn connect(&self, ep: &EndpointSpec, t: SockTimeout) -> PalResult<Box<dyn RpcStream>>;
    /// 只做 getaddrinfo（Q-10：TCP 位址解析早於 timeout 驗證）；失敗的 PalError 與 connect 內的 gaierror 相同。
    fn resolve_tcp(&self, host: &str, port: u16) -> PalResult<()>;
}

pub trait RpcStream: Send {
    /// sendall；`Blocking(d)` 為整體上限。
    fn send_all(&mut self, data: &[u8]) -> PalResult<()>;
    /// 每次呼叫各自套 timeout；0 = EOF。
    fn recv(&mut self, buf: &mut [u8]) -> PalResult<usize>;
}

/// 子行程控制句柄（P4-T4）。
#[derive(Debug, Clone)]
pub struct ChildHandle {
    pub pid: u32,
    pub(crate) exit_code: std::sync::Arc<std::sync::Mutex<Option<i64>>>,
    #[cfg(windows)]
    pub(crate) native: Option<std::sync::Arc<std::sync::Mutex<std::process::Child>>>,
}

impl ChildHandle {
    /// 建立跨平台子行程句柄；實際 spawn 由 `ProcessCtl` 負責。
    pub fn new(pid: u32) -> Self {
        Self {
            pid,
            exit_code: std::sync::Arc::new(std::sync::Mutex::new(None)),
            #[cfg(windows)]
            native: None,
        }
    }
}

pub trait ProcessCtl: Send + Sync {
    /// 成功時不返回：POSIX execve 取代行程；Windows 等子行程結束後 `std::process::exit(child_code)`。只在失敗時返回錯誤。
    fn exec_or_wait(&self, argv: &[OsString], env_add: &[(String, String)]) -> PalError;

    /// 以指定平台選項執行前景程序；POSIX 預設忽略選項並沿用 execve。
    fn exec_or_wait_opts(
        &self,
        argv: &[OsString],
        env_add: &[(String, String)],
        _opts: crate::process::ExecOpts,
    ) -> PalError {
        self.exec_or_wait(argv, env_add)
    }

    // ── P4-T4：ProcessCtl（spawn／poll／foreground）──

    /// 啟動常駐 daemon（POSIX setsid + stdout/err 重導到 /dev/null；Windows DETACHED_PROCESS）。
    fn spawn_daemon(
        &self,
        _argv: &[String],
        _env: &[(OsString, OsString)],
        _detached: bool,
    ) -> PalResult<ChildHandle> {
        Err(PalError::plain(
            PalErrorKind::Unsupported,
            "不支援啟動子行程",
        ))
    }

    /// 輪詢子行程狀態（非阻塞）。
    fn poll(&self, _c: &ChildHandle) -> Option<i64> {
        None
    }

    /// 前景同步呼叫子行程（等同 Python subprocess.call）。
    fn call_foreground(&self, _argv: &[String], _env: &[(OsString, OsString)]) -> PalResult<i64> {
        Err(PalError::plain(
            PalErrorKind::Unsupported,
            "不支援前景子行程呼叫",
        ))
    }

    // ── P4-T19：提權啟動（SC-36）──

    /// Windows：`ShellExecuteExW(lpVerb="runas", SEE_MASK_NOCLOSEPROCESS)` 啟動 `exe` 並等待結束；
    /// 使用者拒絕 UAC 回 `ElevatedOutcome::Declined`。POSIX：`Err(kind=Unsupported)`。
    fn spawn_elevated(&self, _exe: &Path, _args: &[String]) -> PalResult<ElevatedOutcome> {
        Err(PalError::plain(PalErrorKind::Unsupported, "不支援提權啟動"))
    }

}

// ---- P5-T5 裝置來源型別（arch §4.3；計畫 Task 0 介面骨架的 TDD 紅燈 stub）----

/// 單一裝置的識別資訊（arch §4.3）：`by_id` 為掃描到的路徑（Windows 為 `COMx`），`real_path` 為解析後實體路徑。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DeviceInfo {
    pub by_id: String,
    pub real_path: String,
}

/// 裝置選擇條件（arch §4.3）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DeviceSelection {
    pub by_id_dir: PathBuf,
    pub by_path_dir: PathBuf,
    pub exclude_coms: Vec<String>,
    /// SC-19（OD-11）：存在時只保留清單內的 by_id。
    pub test_allowlist: Option<Vec<String>>,
}

/// 裝置來源（arch §4.3）：保留列舉順序。
pub trait DeviceSource: Send {
    fn scan(&mut self) -> PalResult<Vec<(String, DeviceInfo)>>;
}

/// DeviceWatcher 送出的事件（arch §4.3）。
#[derive(Debug)]
pub enum WatcherEvent {
    Delta {
        added: Vec<DeviceInfo>,
        removed: Vec<DeviceInfo>,
        snapshot: Vec<(String, DeviceInfo)>,
    },
    Tick,
    ScanFailed(PalError),
}

// ---------------------------------------------------------------------------
// Slice 2.1 骨架（P5 Task 1 測試替身所需；arch §4.2、§4.5）。
// 僅宣告型別與簽章；SerialBackend／WalFs 的真實實作與 OS 語意由 Task 2／6 交付。
// ---------------------------------------------------------------------------

impl PalError {
    /// 由 POSIX errno 與 Python `str(OSError)` 建立錯誤（替身與 PAL 實作共用）。
    pub fn io(code: i32, py_message: &str) -> PalError {
        PalError::new(kind_from_errno(code), Some(code), None, py_message, py_message)
    }
}

/// 序列埠裝置路徑（POSIX：realpath；Windows：`\\.\COMn`）。
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub struct DevicePath(pub String);

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Parity {
    None,
    Even,
    Odd,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum FlowControl {
    None,
    RtsCts,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SerialConfig {
    pub baud: u32,
    pub data_bits: u8,
    pub parity: Parity,
    pub stop_bits: u8,
    pub flow: FlowControl,
    /// POSIX backend 忽略（UIO-12.1-e）。
    pub xonxoff: bool,
}

impl SerialConfig {
    /// 由 UartProfile 欄位建立。骨架對應表（parity `N`／`E`／`O`，flow `rtscts`）待 Task 2 依 config 語意確認。
    pub fn from_uart_profile(
        baud: u32,
        data_bits: u8,
        parity: &str,
        stop_bits: u8,
        flow_control: &str,
        xonxoff: bool,
    ) -> SerialConfig {
        let parity = match parity.to_ascii_uppercase().as_str() {
            "E" => Parity::Even,
            "O" => Parity::Odd,
            _ => Parity::None,
        };
        let flow = if flow_control.eq_ignore_ascii_case("rtscts") {
            FlowControl::RtsCts
        } else {
            FlowControl::None
        };
        SerialConfig {
            baud,
            data_bits,
            parity,
            stop_bits,
            flow,
            xonxoff,
        }
    }
}

/// 獨占開啟失敗時的持有者診斷（arch §4.2）。
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct HolderReport {
    pub pids: Vec<u32>,
    pub holder_unknown: bool,
    pub status: HolderScanStatus,
}

#[derive(Debug)]
pub enum OpenError {
    /// POSIX EWOULDBLOCK(flock)、open EBUSY(TIOCEXCL)，或鎖後重掃識別到外部 holder。
    Held(HolderReport),
    /// Windows ERROR_ACCESS_DENIED：不推斷 holder，holder_unknown=true。
    AccessDenied {
        os: PalError,
        holder: HolderReport,
    },
    NotFound(PalError),
    Other(PalError),
}

#[derive(Debug, PartialEq, Eq)]
pub enum ReadOutcome {
    Data(usize),
    Eof,
    Woken,
    TimedOut,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum TxOutcome {
    NotSubmitted,
    Completed(usize),
    /// `confirmed` 為結果不明前 OS 已確認的前綴；未知尾段長度為 requested - confirmed。
    Unknown {
        confirmed: usize,
    },
}

/// `Completed(n<len)` 加 `error` 表示部分完成後失敗。
#[derive(Debug, Clone)]
pub struct TxResult {
    pub outcome: TxOutcome,
    pub error: Option<PalError>,
}

pub trait IoWaker: Send + Sync {
    fn wake(&self);
}

/// 取消旗標與喚醒器的組合（arch §4.2）。`cancel` 設旗標並喚醒阻塞中的 I/O。
#[derive(Clone)]
pub struct CancelToken {
    flag: std::sync::Arc<std::sync::atomic::AtomicBool>,
    waker: std::sync::Arc<dyn IoWaker>,
}

impl CancelToken {
    pub fn new(waker: std::sync::Arc<dyn IoWaker>) -> CancelToken {
        CancelToken {
            flag: std::sync::Arc::new(std::sync::atomic::AtomicBool::new(false)),
            waker,
        }
    }

    pub fn cancel(&self) {
        self.flag.store(true, std::sync::atomic::Ordering::SeqCst);
        self.waker.wake();
    }

    pub fn is_cancelled(&self) -> bool {
        self.flag.load(std::sync::atomic::Ordering::SeqCst)
    }
}

/// `close` 的實作端（各後端與替身各自實作）。
pub trait CloseImpl: Send {
    fn close(self: Box<Self>) -> CloseReceipt;
}

/// 只能在 reader 與 writer 都交回後呼叫（型別保證不雙重關閉、不在 I/O 未收斂時關閉）。
pub struct CloseToken {
    inner: Box<dyn CloseImpl>,
}

impl CloseToken {
    pub fn new(inner: Box<dyn CloseImpl>) -> CloseToken {
        CloseToken { inner }
    }

    pub fn close(self, r: Box<dyn SerialReader>, w: Box<dyn SerialWriter>) -> CloseReceipt {
        drop(r);
        drop(w);
        self.inner.close()
    }
}

/// `excl_cleared`：POSIX 關 fd 前 TIOCNXCL 是否成功（arch §4.2，R0-F3）；Windows／fake 恆等於 `closed`。
#[derive(Debug, Clone)]
pub struct CloseReceipt {
    pub closed: bool,
    pub excl_cleared: bool,
    pub error: Option<PalError>,
}

pub trait SerialBackend: Send + Sync {
    /// 以獨占方式開啟並完成設定（arch §4.2）。
    fn open_exclusive(
        &self,
        dev: &DevicePath,
        cfg: &SerialConfig,
    ) -> Result<Box<dyn SerialHandle>, OpenError>;
}

pub trait SerialHandle: Send {
    fn split(
        self: Box<Self>,
    ) -> (
        Box<dyn SerialReader>,
        Box<dyn SerialWriter>,
        std::sync::Arc<dyn IoWaker>,
        CloseToken,
    );
    fn set_baud(&self, baud: u32) -> PalResult<()>;
    fn is_alive(&self) -> bool;
}

/// 獨占診斷（arch §4.2）：只供診斷、不是鎖。POSIX 掃 `/proc/*/fd`；Windows 回 `holder_unknown:true`、`status:Unsupported`。
pub trait UartExclusive: Send + Sync {
    fn probe_holders(&self, real_path: &str) -> HolderReport;
}

pub trait SerialReader: Send {
    /// 逾時以 `None` 表示無限等待；被 waker 喚醒回 `Woken`。
    fn read(&mut self, buf: &mut [u8], timeout: Option<Duration>) -> PalResult<ReadOutcome>;
}

/// OS 提交的序列化點（arch §4.2，R0-F1）。`submit` 被撤銷時不得呼叫 `os_submit`。
pub trait SubmitGate {
    fn submit(
        &self,
        os_submit: &mut dyn FnMut() -> SubmitStep,
    ) -> Result<SubmitStep, SubmitRevoked>;
}

#[derive(Debug, Clone)]
pub enum SubmitStep {
    /// POSIX write(2)=n>0 或 Windows 同步完成。
    Accepted(usize),
    /// Windows ERROR_IO_PENDING：已交 OS。
    Pending,
    /// EAGAIN：未交 OS。
    WouldBlock,
    /// 0 byte 交 OS。
    Failed(PalError),
}

/// 原因由 gate 實作自行保存（PAL 不依賴 sw-core 的 FenceReject）。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct SubmitRevoked;

/// 直接呼叫 `os_submit`；只供 PAL conformance 與 PAL 單元測試。
pub struct AlwaysOpen;

impl SubmitGate for AlwaysOpen {
    fn submit(
        &self,
        os_submit: &mut dyn FnMut() -> SubmitStep,
    ) -> Result<SubmitStep, SubmitRevoked> {
        Ok(os_submit())
    }
}

pub trait SerialWriter: Send {
    /// 回傳值必須能區分「從未提交 OS」「OS 確認 n bytes」「結果不明」（BC-03）。
    fn write_gated(&mut self, data: &[u8], cancel: &CancelToken, gate: &dyn SubmitGate) -> TxResult;

    /// = `write_gated(data, cancel, &AlwaysOpen)`。
    fn write(&mut self, data: &[u8], cancel: &CancelToken) -> TxResult {
        self.write_gated(data, cancel, &AlwaysOpen)
    }
}

/// WAL 檔案系統（arch §4.5）。
pub trait WalFs: Send + Sync {
    /// POSIX：`O_WRONLY|O_APPEND|O_CREAT|O_CLOEXEC`；Windows：share-delete 常駐 handle。
    fn open_append(&self, path: &Path) -> PalResult<Box<dyn WalFile>>;
    /// `None` = 路徑不存在。
    fn path_identity(&self, path: &Path) -> PalResult<Option<FileIdentity>>;
    fn size(&self, path: &Path) -> PalResult<Option<u64>>;
    fn tail_needs_termination(&self, path: &Path) -> PalResult<bool>;
    /// DF-03：優先原子 no-replace；不支援時降級；同名加 `.1` `.2` …。
    fn rename_noreplace_or_suffix(&self, from: &Path, base_to: &Path) -> PalResult<PathBuf>;
    fn sync_dir(&self, dir: &Path) -> PalResult<()>;
    fn ensure_dir(&self, dir: &Path) -> PalResult<()>;
    /// `os.access(W_OK|X_OK)` 等價。
    fn dir_writable(&self, dir: &Path) -> bool;
}

pub trait WalFile: Send {
    /// 失敗時必須回報失敗前 OS 已確認寫出的長度 `sent`（R2-F3）。
    fn write_all_flush(&mut self, bytes: &[u8]) -> Result<(), WalWriteError>;
    fn sync(&mut self) -> PalResult<()>;
    fn identity(&self) -> PalResult<FileIdentity>;
    /// POSIX `st_nlink==0`；Windows `DeletePending`。
    fn unlinked(&self) -> PalResult<bool>;
    fn close(self: Box<Self>) -> PalResult<()>;
}

#[derive(PartialEq, Eq, Clone, Debug)]
pub struct FileIdentity(pub u128, pub u128);

/// WAL／mirror 寫入錯誤：`sent` 為失敗前已交 OS 且確認的 bytes。
#[derive(Debug, Clone)]
pub struct WalWriteError {
    pub sent: usize,
    pub error: PalError,
}

// ---------------------------------------------------------------------------
// P5-T16 Step 0 最小骨架：`ConsoleTransportFactory`（arch §4.7）。
// 完整宣告（create_pty／bind_tcp_console／open_com0com_console／held_slave_paths 與 PtyPair 等型別）
// 由 P5 Task 2 補齊；本段只讓 SessionManager 的 `ManagerDeps.console` 可編譯。
// ---------------------------------------------------------------------------

/// human console 承接工廠（arch §4.7，最小形式）。
pub trait ConsoleTransportFactory: Send + Sync {
    fn kind(&self) -> crate::ConsoleKind;
}
