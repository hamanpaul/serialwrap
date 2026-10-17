//! Windows 環境查詢。

use crate::traits::{windows_win32_error, PalResult, SysEnv};
use std::ffi::OsString;
use std::os::windows::ffi::{OsStrExt, OsStringExt};
use std::path::{Component, Path, PathBuf};
use windows_sys::Win32::Storage::FileSystem::{GetFileType, FILE_TYPE_CHAR};
use windows_sys::Win32::System::Console::{
    GetConsoleScreenBufferInfo, GetStdHandle, CONSOLE_SCREEN_BUFFER_INFO, STD_OUTPUT_HANDLE,
};

#[derive(Clone, Copy, Debug, Default)]
pub struct NativeSysEnv;

impl SysEnv for NativeSysEnv {
    fn var(&self, key: &str) -> Option<String> {
        std::env::var_os(key).map(|value| value.to_string_lossy().into_owned())
    }

    fn vars(&self) -> Vec<(String, String)> {
        let mut vars: Vec<_> = std::env::vars_os()
            .map(|(key, value)| {
                (
                    key.to_string_lossy().into_owned(),
                    value.to_string_lossy().into_owned(),
                )
            })
            .collect();
        vars.sort_by(|left, right| {
            left.0
                .to_ascii_lowercase()
                .cmp(&right.0.to_ascii_lowercase())
        });
        vars
    }

    fn system_config_dir(&self) -> String {
        let base = std::env::var_os("ProgramData")
            .map(PathBuf::from)
            .unwrap_or_else(|| PathBuf::from(r"C:\ProgramData"));
        base.join("serialwrap").to_string_lossy().into_owned()
    }

    fn current_user_home(&self) -> Option<String> {
        self.var("USERPROFILE").or_else(|| {
            let drive = self.var("HOMEDRIVE")?;
            let path = self.var("HOMEPATH")?;
            Some(format!("{drive}{path}"))
        })
    }

    fn user_home(&self, user: &str) -> Option<String> {
        if user.is_empty()
            || self
                .var("USERNAME")
                .is_some_and(|current| current.eq_ignore_ascii_case(user))
        {
            self.current_user_home()
        } else {
            None
        }
    }

    fn current_exe(&self) -> PalResult<PathBuf> {
        std::env::current_exe()
            .map_err(|error| windows_win32_error(error.raw_os_error().unwrap_or(5) as u32))
    }

    fn realpath(&self, path: &Path) -> PalResult<PathBuf> {
        match std::fs::canonicalize(path) {
            Ok(path) => Ok(strip_verbatim_prefix(path)),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                let absolute = std::path::absolute(path).map_err(|error| {
                    windows_win32_error(error.raw_os_error().unwrap_or(5) as u32)
                })?;
                Ok(strip_verbatim_prefix(normalize_lexically(&absolute)))
            }
            Err(error) => Err(windows_win32_error(error.raw_os_error().unwrap_or(5) as u32)),
        }
    }

    fn terminal_columns(&self) -> Option<u16> {
        // SAFETY: GetStdHandle 只讀取標準輸出控制代碼。
        let handle = unsafe { GetStdHandle(STD_OUTPUT_HANDLE) };
        if handle.is_null() || handle as isize == -1 {
            return None;
        }
        let mut info = std::mem::MaybeUninit::<CONSOLE_SCREEN_BUFFER_INFO>::zeroed();
        // SAFETY: info 指向有效可寫結構，handle 由 GetStdHandle 取得。
        let ok = unsafe { GetConsoleScreenBufferInfo(handle, info.as_mut_ptr()) };
        if ok == 0 {
            return None;
        }
        // SAFETY: API 成功後已初始化輸出結構。
        let info = unsafe { info.assume_init() };
        let width = i32::from(info.srWindow.Right) - i32::from(info.srWindow.Left) + 1;
        (width > 0 && width <= i32::from(u16::MAX)).then_some(width as u16)
    }

    fn stdout_is_terminal(&self) -> bool {
        // SAFETY: GetStdHandle 僅查詢目前行程的標準輸出控制代碼。
        let handle = unsafe { GetStdHandle(STD_OUTPUT_HANDLE) };
        if handle.is_null() || handle as isize == -1 {
            return false;
        }
        // CPython 的 Windows isatty 以 FILE_TYPE_CHAR 判斷，不採 Rust 額外的 MSYS/Cygwin 探測。
        // SAFETY: handle 由 GetStdHandle 取得，GetFileType 僅查詢控制代碼種類。
        unsafe { is_terminal_file_type(GetFileType(handle)) }
    }

    fn errno_name(&self, code: i32) -> Option<&'static str> {
        errno_name(code)
    }

    fn pid(&self) -> u32 {
        std::process::id()
    }
}

fn strip_verbatim_prefix(path: PathBuf) -> PathBuf {
    let wide: Vec<u16> = path.as_os_str().encode_wide().collect();
    const VERBATIM: [u16; 4] = [b'\\' as u16, b'\\' as u16, b'?' as u16, b'\\' as u16];
    const VERBATIM_UNC: [u16; 8] = [
        b'\\' as u16,
        b'\\' as u16,
        b'?' as u16,
        b'\\' as u16,
        b'U' as u16,
        b'N' as u16,
        b'C' as u16,
        b'\\' as u16,
    ];
    if starts_with_ascii_case_insensitive(&wide, &VERBATIM_UNC) {
        let mut normalized = vec![b'\\' as u16, b'\\' as u16];
        normalized.extend_from_slice(&wide[VERBATIM_UNC.len()..]);
        PathBuf::from(OsString::from_wide(&normalized))
    } else if wide.starts_with(&VERBATIM) {
        PathBuf::from(OsString::from_wide(&wide[VERBATIM.len()..]))
    } else {
        path
    }
}

fn starts_with_ascii_case_insensitive(value: &[u16], prefix: &[u16]) -> bool {
    value.len() >= prefix.len()
        && value
            .iter()
            .zip(prefix)
            .all(|(actual, expected)| ascii_lower(*actual) == ascii_lower(*expected))
}

fn ascii_lower(value: u16) -> u16 {
    if (b'A' as u16..=b'Z' as u16).contains(&value) {
        value + (b'a' - b'A') as u16
    } else {
        value
    }
}

fn normalize_lexically(path: &Path) -> PathBuf {
    let mut normalized = PathBuf::new();
    for component in path.components() {
        match component {
            Component::Prefix(prefix) => normalized.push(prefix.as_os_str()),
            Component::RootDir => normalized.push(component.as_os_str()),
            Component::CurDir => {}
            Component::ParentDir => {
                if normalized.file_name().is_some() {
                    normalized.pop();
                } else if !normalized.has_root() {
                    normalized.push(component.as_os_str());
                }
            }
            Component::Normal(part) => normalized.push(part),
        }
    }
    normalized
}

fn is_terminal_file_type(file_type: u32) -> bool {
    file_type == FILE_TYPE_CHAR
}

#[cfg(test)]
mod review_regression_tests {
    use super::*;

    #[test]
    fn strips_verbatim_drive_and_unc_prefixes() {
        assert_eq!(
            strip_verbatim_prefix(PathBuf::from(r"\\?\C:\Windows\notepad.exe")),
            PathBuf::from(r"C:\Windows\notepad.exe")
        );
        assert_eq!(
            strip_verbatim_prefix(PathBuf::from(r"\\?\UNC\server\share\tool.exe")),
            PathBuf::from(r"\\server\share\tool.exe")
        );
    }

    #[test]
    fn missing_paths_are_lexically_normalized() {
        assert_eq!(
            normalize_lexically(Path::new(r"C:\missing\child\..\tool.exe")),
            PathBuf::from(r"C:\missing\tool.exe")
        );
    }

    #[test]
    fn only_character_handles_are_terminals() {
        assert!(is_terminal_file_type(FILE_TYPE_CHAR));
        assert!(!is_terminal_file_type(0));
        assert!(!is_terminal_file_type(1));
        assert!(!is_terminal_file_type(2));
    }
}

/// 依 Python Windows `errno.errorcode` 的基準回傳名稱。
pub fn errno_name(code: i32) -> Option<&'static str> {
    match code {
        1 => Some("EPERM"),
        2 => Some("ENOENT"),
        3 => Some("ESRCH"),
        4 => Some("EINTR"),
        5 => Some("EIO"),
        6 => Some("ENXIO"),
        7 => Some("E2BIG"),
        8 => Some("ENOEXEC"),
        9 => Some("EBADF"),
        10 => Some("ECHILD"),
        11 => Some("EAGAIN"),
        12 => Some("ENOMEM"),
        13 => Some("EACCES"),
        14 => Some("EFAULT"),
        16 => Some("EBUSY"),
        17 => Some("EEXIST"),
        18 => Some("EXDEV"),
        19 => Some("ENODEV"),
        20 => Some("ENOTDIR"),
        21 => Some("EISDIR"),
        22 => Some("EINVAL"),
        23 => Some("ENFILE"),
        24 => Some("EMFILE"),
        25 => Some("ENOTTY"),
        27 => Some("EFBIG"),
        28 => Some("ENOSPC"),
        29 => Some("ESPIPE"),
        30 => Some("EROFS"),
        31 => Some("EMLINK"),
        32 => Some("EPIPE"),
        33 => Some("EDOM"),
        34 => Some("ERANGE"),
        36 => Some("EDEADLOCK"),
        38 => Some("ENAMETOOLONG"),
        39 => Some("ENOLCK"),
        40 => Some("ENOSYS"),
        41 => Some("ENOTEMPTY"),
        42 => Some("EILSEQ"),
        104 => Some("EBADMSG"),
        105 => Some("ECANCELED"),
        111 => Some("EIDRM"),
        120 => Some("ENODATA"),
        121 => Some("ENOLINK"),
        122 => Some("ENOMSG"),
        124 => Some("ENOSR"),
        125 => Some("ENOSTR"),
        127 => Some("ENOTRECOVERABLE"),
        129 => Some("ENOTSUP"),
        132 => Some("EOVERFLOW"),
        133 => Some("EOWNERDEAD"),
        134 => Some("EPROTO"),
        137 => Some("ETIME"),
        139 => Some("ETXTBSY"),
        10000 => Some("WSABASEERR"),
        10004 => Some("WSAEINTR"),
        10009 => Some("WSAEBADF"),
        10013 => Some("WSAEACCES"),
        10014 => Some("WSAEFAULT"),
        10022 => Some("WSAEINVAL"),
        10024 => Some("WSAEMFILE"),
        10035 => Some("WSAEWOULDBLOCK"),
        10036 => Some("WSAEINPROGRESS"),
        10037 => Some("WSAEALREADY"),
        10038 => Some("WSAENOTSOCK"),
        10039 => Some("WSAEDESTADDRREQ"),
        10040 => Some("WSAEMSGSIZE"),
        10041 => Some("WSAEPROTOTYPE"),
        10042 => Some("WSAENOPROTOOPT"),
        10043 => Some("WSAEPROTONOSUPPORT"),
        10044 => Some("WSAESOCKTNOSUPPORT"),
        10045 => Some("WSAEOPNOTSUPP"),
        10046 => Some("WSAEPFNOSUPPORT"),
        10047 => Some("WSAEAFNOSUPPORT"),
        10048 => Some("WSAEADDRINUSE"),
        10049 => Some("WSAEADDRNOTAVAIL"),
        10050 => Some("WSAENETDOWN"),
        10051 => Some("WSAENETUNREACH"),
        10052 => Some("WSAENETRESET"),
        10053 => Some("WSAECONNABORTED"),
        10054 => Some("WSAECONNRESET"),
        10055 => Some("WSAENOBUFS"),
        10056 => Some("WSAEISCONN"),
        10057 => Some("WSAENOTCONN"),
        10058 => Some("WSAESHUTDOWN"),
        10059 => Some("WSAETOOMANYREFS"),
        10060 => Some("WSAETIMEDOUT"),
        10061 => Some("WSAECONNREFUSED"),
        10062 => Some("WSAELOOP"),
        10063 => Some("WSAENAMETOOLONG"),
        10064 => Some("WSAEHOSTDOWN"),
        10065 => Some("WSAEHOSTUNREACH"),
        10066 => Some("WSAENOTEMPTY"),
        10067 => Some("WSAEPROCLIM"),
        10068 => Some("WSAEUSERS"),
        10069 => Some("WSAEDQUOT"),
        10070 => Some("WSAESTALE"),
        10071 => Some("WSAEREMOTE"),
        10091 => Some("WSASYSNOTREADY"),
        10092 => Some("WSAVERNOTSUPPORTED"),
        10093 => Some("WSANOTINITIALISED"),
        10101 => Some("WSAEDISCON"),
        _ => None,
    }
}
