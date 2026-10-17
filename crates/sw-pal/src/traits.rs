//! client 側 trait 與共通錯誤型別。

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
