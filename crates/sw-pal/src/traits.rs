//! client 側 trait 與共通錯誤型別（P3-T9 紅燈 stub）。
#![allow(unused_variables, dead_code)]

use std::ffi::OsString;
use std::path::{Path, PathBuf};
use std::time::Duration;

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
}

pub trait FsRead: Send + Sync {
    /// open()+read；Windows 錯誤以 CRT errno 風格（_dosmaperr＋CRT strerror）。
    fn read(&self, p: &Path) -> PalResult<Vec<u8>>;
    /// `os.path.exists`。
    fn exists(&self, p: &Path) -> bool;
    /// `os.path.isdir`。
    fn is_dir(&self, p: &Path) -> bool;
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

pub trait ProcessCtl: Send + Sync {
    /// 成功時不返回：POSIX execve 取代行程；Windows 等子行程結束後 `std::process::exit(child_code)`。只在失敗時返回錯誤。
    fn exec_or_wait(&self, argv: &[OsString], env_add: &[(String, String)]) -> PalError;
}
