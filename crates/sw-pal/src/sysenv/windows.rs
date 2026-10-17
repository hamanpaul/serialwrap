//! Windows 環境查詢。

use crate::sysenv::{
    capture_command, capture_command_merged, exit_code, universal_newlines, CapturedOutput,
};
use crate::traits::{
    windows_win32_error, CmdOutput, CmdOutputRaw, InterruptScope, PalError, PalErrorKind,
    PalResult, SysEnv,
};
use std::ffi::OsString;
use std::os::windows::ffi::{OsStrExt, OsStringExt};
use std::path::{Component, Path, PathBuf};
use std::process::Command;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Duration;
use windows_sys::Win32::Foundation::{
    CloseHandle, GetLastError, ERROR_INSUFFICIENT_BUFFER, HANDLE,
};
use windows_sys::Win32::Globalization::{
    GetACP, IsDBCSLeadByteEx, MultiByteToWideChar, MB_ERR_INVALID_CHARS,
};
use windows_sys::Win32::Security::{
    GetTokenInformation, LookupAccountSidW, TokenUser, TOKEN_QUERY, TOKEN_USER,
};
use windows_sys::Win32::Storage::FileSystem::{GetFileType, FILE_TYPE_CHAR};
use windows_sys::Win32::System::Console::{
    GetConsoleScreenBufferInfo, GetStdHandle, SetConsoleCtrlHandler, CONSOLE_SCREEN_BUFFER_INFO,
    CTRL_C_EVENT, STD_OUTPUT_HANDLE,
};
use windows_sys::Win32::System::Threading::{GetCurrentProcess, OpenProcessToken};

#[derive(Clone, Copy, Debug, Default)]
pub struct NativeSysEnv;

static INTERRUPTED: AtomicBool = AtomicBool::new(false);

struct WindowsInterruptScope;

impl InterruptScope for WindowsInterruptScope {
    fn interrupted(&self) -> bool {
        INTERRUPTED.load(Ordering::SeqCst)
    }
}

impl Drop for WindowsInterruptScope {
    fn drop(&mut self) {
        // SAFETY: 此 scope 註冊了相同 callback，FALSE 會移除該 callback。
        unsafe {
            SetConsoleCtrlHandler(Some(record_ctrl_c), 0);
        }
    }
}

unsafe extern "system" fn record_ctrl_c(control_type: u32) -> i32 {
    if control_type == CTRL_C_EVENT {
        INTERRUPTED.store(true, Ordering::SeqCst);
        1
    } else {
        0
    }
}

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

    fn run(&self, argv: &[String], timeout: Option<Duration>) -> PalResult<CmdOutput> {
        let (status, stdout, stderr) = run_bytes(argv, None, timeout)?;
        let stdout = decode_acp(&stdout)?;
        let stderr = decode_acp(&stderr)?;
        Ok(CmdOutput {
            rc: status,
            stdout: universal_newlines(stdout),
            stderr: universal_newlines(stderr),
        })
    }

    fn run_merged(&self, argv: &[String], timeout: Option<Duration>) -> PalResult<CmdOutput> {
        let (status, output) = run_bytes_merged(argv, timeout)?;
        let stdout = decode_acp(&output)?;
        let timed_out = status.is_none();
        Ok(CmdOutput {
            rc: status.map(exit_code).unwrap_or(-1),
            stdout: universal_newlines(stdout),
            stderr: if timed_out {
                "TIMEOUT".to_string()
            } else {
                String::new()
            },
        })
    }

    fn interrupt_scope(&self) -> PalResult<Box<dyn InterruptScope>> {
        INTERRUPTED.store(false, Ordering::SeqCst);
        // SAFETY: callback 僅設定 lock-free 的原子旗標，不呼叫其他 Windows API。
        if unsafe { SetConsoleCtrlHandler(Some(record_ctrl_c), 1) } == 0 {
            return Err(windows_win32_error(unsafe { GetLastError() }));
        }
        Ok(Box::new(WindowsInterruptScope))
    }

    fn run_raw(
        &self,
        argv: &[String],
        env: Option<&[(OsString, OsString)]>,
        timeout: Option<Duration>,
    ) -> PalResult<CmdOutputRaw> {
        let (status, stdout, stderr) = run_bytes(argv, env, timeout)?;
        Ok(CmdOutputRaw {
            rc: status,
            stdout,
            stderr,
        })
    }

    fn which(&self, name: &str) -> Option<PathBuf> {
        if name.contains('/') || name.contains('\\') {
            windows_candidates(name)
                .into_iter()
                .find(|candidate| candidate.is_file())
        } else {
            which_windows(name)
        }
    }

    fn which_all(&self, name: &str) -> Vec<PathBuf> {
        which_all_windows(name)
    }

    fn has_systemd(&self) -> bool {
        false
    }

    fn is_wsl(&self) -> bool {
        false
    }

    fn user_in_group(&self, _group: &str) -> bool {
        false
    }

    fn user_identity(&self) -> String {
        token_user_identity().unwrap_or_else(fallback_user_identity)
    }

    fn local_appdata(&self) -> Option<PathBuf> {
        self.var("LOCALAPPDATA")
            .filter(|path| !path.is_empty())
            .map(PathBuf::from)
            .or_else(|| {
                self.var("USERPROFILE")
                    .filter(|path| !path.is_empty())
                    .map(|path| PathBuf::from(path).join("AppData").join("Local"))
            })
    }
}

fn run_bytes(
    argv: &[String],
    env: Option<&[(OsString, OsString)]>,
    timeout: Option<Duration>,
) -> PalResult<(i32, Vec<u8>, Vec<u8>)> {
    let Some(program) = argv.first() else {
        return Err(PalError::plain(
            PalErrorKind::InvalidInput,
            "至少需要一個執行檔參數",
        ));
    };
    let mut command = Command::new(program);
    command.args(&argv[1..]);
    if let Some(env) = env {
        command
            .env_clear()
            .envs(env.iter().map(|(key, value)| (key, value)));
    }
    match capture_command(command, timeout).map_err(|error| {
        let code = error
            .raw_os_error()
            .map(|code| code as u32)
            .unwrap_or_else(|| {
                if error.kind() == std::io::ErrorKind::NotFound {
                    2
                } else {
                    5
                }
            });
        windows_win32_error(code)
    })? {
        CapturedOutput::TimedOut => Ok((-1, Vec::new(), b"TIMEOUT".to_vec())),
        CapturedOutput::Exited {
            status,
            stdout,
            stderr,
        } => Ok((exit_code(status), stdout, stderr)),
    }
}

fn run_bytes_merged(
    argv: &[String],
    timeout: Option<Duration>,
) -> PalResult<(Option<std::process::ExitStatus>, Vec<u8>)> {
    let Some(program) = argv.first() else {
        return Err(PalError::plain(
            PalErrorKind::InvalidInput,
            "至少需要一個執行檔參數",
        ));
    };
    let mut command = Command::new(program);
    command.args(&argv[1..]);
    let output = capture_command_merged(command, timeout).map_err(|error| {
        let code = error
            .raw_os_error()
            .map(|code| code as u32)
            .unwrap_or_else(|| {
                if error.kind() == std::io::ErrorKind::NotFound {
                    2
                } else {
                    5
                }
            });
        windows_win32_error(code)
    })?;
    Ok((output.status, output.output))
}

fn decode_acp(bytes: &[u8]) -> PalResult<String> {
    // SAFETY: GetACP 無參數，會回傳目前作用中的 Windows ANSI 字碼頁。
    decode_with_code_page(bytes, unsafe { GetACP() })
}

fn decode_with_code_page(bytes: &[u8], code_page: u32) -> PalResult<String> {
    if bytes.is_empty() {
        return Ok(String::new());
    }
    let Ok(length) = i32::try_from(bytes.len()) else {
        return Err(decode_error(
            bytes,
            DecodeFailure {
                start: 0,
                end: 1,
                reason: "illegal multibyte sequence",
                encoding: format!("cp{code_page}"),
            },
        ));
    };
    // SAFETY: 兩次轉換呼叫期間，位元組切片均保持有效。
    let required = unsafe {
        MultiByteToWideChar(
            code_page,
            MB_ERR_INVALID_CHARS,
            bytes.as_ptr(),
            length,
            std::ptr::null_mut(),
            0,
        )
    };
    if required <= 0 {
        return Err(decode_error(bytes, find_decode_error(code_page, bytes)));
    }
    let mut wide = vec![0u16; required as usize];
    // SAFETY: wide 容量採用前次查詢結果；bytes 在呼叫期間保持有效。
    let converted = unsafe {
        MultiByteToWideChar(
            code_page,
            MB_ERR_INVALID_CHARS,
            bytes.as_ptr(),
            length,
            wide.as_mut_ptr(),
            required,
        )
    };
    if converted <= 0 {
        return Err(decode_error(bytes, find_decode_error(code_page, bytes)));
    }
    String::from_utf16(&wide[..converted as usize])
        .map_err(|_| decode_error(bytes, find_decode_error(code_page, bytes)))
}

#[derive(Clone)]
struct DecodeFailure {
    start: usize,
    end: usize,
    reason: &'static str,
    encoding: String,
}

fn find_decode_error(code_page: u32, bytes: &[u8]) -> DecodeFailure {
    if code_page == 65001 {
        let error = std::str::from_utf8(bytes).expect_err("呼叫端已確認 UTF-8 解碼失敗");
        let start = error.valid_up_to();
        let length = error.error_len();
        let end = length.map_or(bytes.len(), |length| start.saturating_add(length));
        let first = bytes.get(start).copied().unwrap_or_default();
        let reason = if length.is_none() {
            "unexpected end of data"
        } else if matches!(first, 0x80..=0xc1 | 0xf5..=0xff) {
            "invalid start byte"
        } else {
            "invalid continuation byte"
        };
        return DecodeFailure {
            start,
            end,
            reason,
            encoding: "utf-8".to_string(),
        };
    }

    let mut position = 0;
    while position < bytes.len() {
        let byte = bytes[position];
        // CP950 的 Python codec 將末尾 0xff 視為未完成的雙位元組序列。
        // SAFETY: IsDBCSLeadByteEx 只查詢指定字碼頁中的單一位元組。
        let lead =
            unsafe { IsDBCSLeadByteEx(code_page, byte) != 0 } || code_page == 950 && byte == 0xff;
        if lead {
            if position + 1 >= bytes.len() {
                return DecodeFailure {
                    start: position,
                    end: bytes.len(),
                    reason: "incomplete multibyte sequence",
                    encoding: format!("cp{code_page}"),
                };
            }
            if !can_decode(code_page, &bytes[position..position + 2]) {
                return DecodeFailure {
                    start: position,
                    end: position + 1,
                    reason: "illegal multibyte sequence",
                    encoding: format!("cp{code_page}"),
                };
            }
            position += 2;
            continue;
        }
        if !can_decode(code_page, &bytes[position..position + 1]) {
            return DecodeFailure {
                start: position,
                end: position + 1,
                reason: "character maps to <undefined>",
                encoding: "charmap".to_string(),
            };
        }
        position += 1;
    }
    DecodeFailure {
        start: bytes.len().saturating_sub(1),
        end: bytes.len(),
        reason: "illegal multibyte sequence",
        encoding: format!("cp{code_page}"),
    }
}

fn can_decode(code_page: u32, bytes: &[u8]) -> bool {
    let Ok(length) = i32::try_from(bytes.len()) else {
        return false;
    };
    // SAFETY: bytes 是有效切片；null 輸出緩衝區只查詢轉換長度。
    unsafe {
        MultiByteToWideChar(
            code_page,
            MB_ERR_INVALID_CHARS,
            bytes.as_ptr(),
            length,
            std::ptr::null_mut(),
            0,
        ) > 0
    }
}

fn decode_error(bytes: &[u8], failure: DecodeFailure) -> PalError {
    let byte = bytes.get(failure.start).copied().unwrap_or_default();
    let message = if failure.end.saturating_sub(failure.start) == 1 {
        format!(
            "'{}' codec can't decode byte 0x{byte:02x} in position {}: {}",
            failure.encoding, failure.start, failure.reason
        )
    } else {
        format!(
            "'{}' codec can't decode bytes in position {}-{}: {}",
            failure.encoding,
            failure.start,
            failure.end.saturating_sub(1),
            failure.reason
        )
    };
    PalError::new(PalErrorKind::Io, None, None, "", message)
}

fn windows_candidates(name: &str) -> Vec<PathBuf> {
    let has_extension = name_has_extension(name);
    let extensions = if has_extension {
        vec![String::new()]
    } else {
        std::env::var_os("PATHEXT")
            .map(|value| {
                value
                    .to_string_lossy()
                    .split(';')
                    .map(str::to_owned)
                    .collect()
            })
            .unwrap_or_else(|| {
                [".COM", ".EXE", ".BAT", ".CMD"]
                    .into_iter()
                    .map(str::to_owned)
                    .collect()
            })
    };
    extensions
        .into_iter()
        .map(|extension| PathBuf::from(format!("{name}{extension}")))
        .collect()
}

fn which_all_windows(name: &str) -> Vec<PathBuf> {
    let Some(path) = std::env::var_os("PATH") else {
        return Vec::new();
    };
    let candidates = windows_candidates(name);
    let mut seen = std::collections::HashSet::<OsString>::new();
    std::env::split_paths(&path)
        .filter(|directory| !directory.as_os_str().is_empty())
        .flat_map(|directory| candidates.iter().map(move |name| directory.join(name)))
        .filter(|candidate| {
            candidate.is_file() && seen.insert(candidate.as_os_str().to_os_string())
        })
        .collect()
}

fn which_windows(name: &str) -> Option<PathBuf> {
    let path = std::env::var_os("PATH")?;
    if path.is_empty() {
        return None;
    }
    let candidates = windows_candidates(name);
    if let Some(found) = candidates.iter().find(|candidate| candidate.is_file()) {
        return Some(found.clone());
    }
    for directory in std::env::split_paths(&path) {
        for name in &candidates {
            let candidate = if directory.as_os_str().is_empty() {
                name.clone()
            } else {
                directory.join(name)
            };
            if candidate.is_file() {
                return Some(candidate);
            }
        }
    }
    None
}

fn name_has_extension(name: &str) -> bool {
    let leaf = name.rsplit(['/', '\\']).next().unwrap_or(name);
    leaf.rfind('.').is_some_and(|position| position > 0)
}

struct TokenHandle(HANDLE);

impl Drop for TokenHandle {
    fn drop(&mut self) {
        // SAFETY: 此包裝器擁有 OpenProcessToken 回傳的 token 控制代碼。
        unsafe { CloseHandle(self.0) };
    }
}

fn token_user_identity() -> Option<String> {
    let mut token = std::ptr::null_mut();
    // SAFETY: GetCurrentProcess 回傳偽控制代碼；token 是有效的可寫輸出位置。
    if unsafe { OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &mut token) } == 0 {
        return None;
    }
    let token = TokenHandle(token);
    let mut required = 0u32;
    // SAFETY: 第一次呼叫只查詢所需緩衝區大小。
    unsafe {
        GetTokenInformation(token.0, TokenUser, std::ptr::null_mut(), 0, &mut required);
    }
    if required == 0 {
        return None;
    }
    let mut storage = vec![0u64; required.div_ceil(std::mem::size_of::<u64>() as u32) as usize];
    // SAFETY: storage 已按要求對齊，大小也符合 GetTokenInformation 的回報值。
    if unsafe {
        GetTokenInformation(
            token.0,
            TokenUser,
            storage.as_mut_ptr().cast(),
            required,
            &mut required,
        )
    } == 0
    {
        return None;
    }
    // SAFETY: TokenUser 會在已對齊輸出緩衝區的起始位置填入 TOKEN_USER。
    let user = unsafe { &*(storage.as_ptr().cast::<TOKEN_USER>()) };
    lookup_account_name(user.User.Sid)
}

fn lookup_account_name(sid: *mut std::ffi::c_void) -> Option<String> {
    let mut name_len = 0u32;
    let mut domain_len = 0u32;
    let mut sid_type = 0;
    // SAFETY: null 緩衝區用於查詢所需大小；SID 來自 TokenUser。
    unsafe {
        LookupAccountSidW(
            std::ptr::null(),
            sid,
            std::ptr::null_mut(),
            &mut name_len,
            std::ptr::null_mut(),
            &mut domain_len,
            &mut sid_type,
        );
    }
    if unsafe { GetLastError() } != ERROR_INSUFFICIENT_BUFFER || name_len == 0 {
        return None;
    }
    let mut name = vec![0u16; name_len as usize];
    let mut domain = vec![0u16; domain_len as usize];
    // SAFETY: 兩個輸出向量均採用探測呼叫回報的大小。
    if unsafe {
        LookupAccountSidW(
            std::ptr::null(),
            sid,
            name.as_mut_ptr(),
            &mut name_len,
            domain.as_mut_ptr(),
            &mut domain_len,
            &mut sid_type,
        )
    } == 0
    {
        return None;
    }
    name.truncate(name_len as usize);
    domain.truncate(domain_len as usize);
    let name = String::from_utf16(&name).ok()?;
    let domain = String::from_utf16(&domain).ok()?;
    Some(if domain.is_empty() {
        name
    } else {
        format!("{domain}\\{name}")
    })
}

fn fallback_user_identity() -> String {
    let domain = std::env::var_os("USERDOMAIN")
        .unwrap_or_default()
        .to_string_lossy()
        .into_owned();
    let user = std::env::var_os("USERNAME")
        .unwrap_or_default()
        .to_string_lossy()
        .into_owned();
    format!("{domain}\\{user}")
}

#[cfg(test)]
mod tests {
    use super::decode_with_code_page;

    #[test]
    fn cp950_decode_error_skips_valid_multibyte_prefix() {
        let error = decode_with_code_page(&[0xa4, 0xa4, 0xff], 950).unwrap_err();
        let golden = include_str!("../../tests/golden/run_decode.ndjson");
        let line = golden
            .lines()
            .find(|line| line.contains("\"case\":\"windows_cp950_invalid_bytes\""))
            .expect("缺少 Python cp950 解碼 golden");
        let expected = line
            .split_once("\"decode_error\":\"")
            .and_then(|(_, rest)| rest.split_once("\",\"encoding\"").map(|(value, _)| value))
            .expect("golden 缺少 decode_error");
        assert_eq!(error.py_message, expected);
    }

    #[test]
    fn cp950_decode_error_reports_truncated_lead_byte() {
        let error = decode_with_code_page(&[0xa4], 950).unwrap_err();
        assert_eq!(
            error.py_message,
            "'cp950' codec can't decode byte 0xa4 in position 0: incomplete multibyte sequence"
        );
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
