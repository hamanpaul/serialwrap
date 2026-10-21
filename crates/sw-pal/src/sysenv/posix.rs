//! POSIX 環境查詢。

use crate::sysenv::{
    capture_command, capture_command_merged, exit_code, universal_newlines, CapturedOutput,
};
use crate::traits::{
    posix_errno, posix_io_error, CmdOutput, CmdOutputRaw, InterruptScope, PalError, PalErrorKind,
    PalResult, SysEnv,
};
use std::ffi::{CStr, CString, OsString};
use std::io::IsTerminal;
use std::os::unix::ffi::{OsStrExt, OsStringExt};
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Duration;

#[derive(Clone, Copy, Debug, Default)]
pub struct NativeSysEnv;

static INTERRUPTED: AtomicBool = AtomicBool::new(false);

struct PosixInterruptScope {
    previous: libc::sigaction,
}

impl InterruptScope for PosixInterruptScope {
    fn interrupted(&self) -> bool {
        INTERRUPTED.load(Ordering::SeqCst)
    }
}

impl Drop for PosixInterruptScope {
    fn drop(&mut self) {
        // SAFETY: `previous` 是建立此 scope 時由 sigaction 原樣取得的 handler 狀態。
        unsafe {
            libc::sigaction(libc::SIGINT, &self.previous, std::ptr::null_mut());
        }
    }
}

extern "C" fn record_sigint(_signal: libc::c_int) {
    INTERRUPTED.store(true, Ordering::SeqCst);
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
        vars.sort_by(|left, right| left.0.cmp(&right.0));
        vars
    }

    fn system_config_dir(&self) -> String {
        "/etc/serialwrap".into()
    }

    fn current_user_home(&self) -> Option<String> {
        // SAFETY: getuid 不接收指標，也不會失敗。
        let uid = unsafe { libc::getuid() };
        passwd_by_uid(uid)
    }

    fn user_home(&self, user: &str) -> Option<String> {
        let name = CString::new(user).ok()?;
        let mut entry = std::mem::MaybeUninit::<libc::passwd>::uninit();
        let mut result = std::ptr::null_mut();
        let mut buffer = passwd_buffer();
        // SAFETY: `name`, passwd buffer 與 result 指標在呼叫期間均有效；成功結果只讀取 pw_dir。
        let status = unsafe {
            libc::getpwnam_r(
                name.as_ptr(),
                entry.as_mut_ptr(),
                buffer.as_mut_ptr().cast(),
                buffer.len(),
                &mut result,
            )
        };
        if status != 0 || result.is_null() {
            return None;
        }
        // SAFETY: getpwnam_r 回傳非空 result，且 entry 在上方初始化。
        let home = unsafe { CStr::from_ptr((*result).pw_dir) };
        Some(home.to_string_lossy().into_owned())
    }

    fn current_exe(&self) -> PalResult<PathBuf> {
        std::env::current_exe().map_err(posix_io_error)
    }

    fn realpath(&self, path: &Path) -> PalResult<PathBuf> {
        std::fs::canonicalize(path).map_err(posix_io_error)
    }

    fn terminal_columns(&self) -> Option<u16> {
        let mut size = std::mem::MaybeUninit::<libc::winsize>::zeroed();
        // SAFETY: `size` 指向足夠大的可寫 winsize；ioctl 讀取 stdout 的終端視窗大小。
        let result =
            unsafe { libc::ioctl(libc::STDOUT_FILENO, libc::TIOCGWINSZ, size.as_mut_ptr()) };
        if result != 0 {
            return None;
        }
        // SAFETY: ioctl 成功後已初始化 winsize 欄位。
        let columns = unsafe { size.assume_init().ws_col };
        (columns > 0).then_some(columns)
    }

    fn stdout_is_terminal(&self) -> bool {
        std::io::stdout().is_terminal()
    }

    fn errno_name(&self, code: i32) -> Option<&'static str> {
        errno_name(code)
    }

    fn pid(&self) -> u32 {
        std::process::id()
    }

    fn run(&self, argv: &[String], timeout: Option<Duration>) -> PalResult<CmdOutput> {
        let (status, stdout, stderr) = run_bytes(argv, None, timeout)?;
        let stdout = decode_locale_text(&stdout)?;
        let stderr = decode_locale_text(&stderr)?;
        Ok(CmdOutput {
            rc: status,
            stdout: universal_newlines(stdout),
            stderr: universal_newlines(stderr),
        })
    }

    fn run_merged(&self, argv: &[String], timeout: Option<Duration>) -> PalResult<CmdOutput> {
        let (status, output) = run_bytes_merged(argv, timeout)?;
        let stdout = decode_locale_text(&output)?;
        let timed_out = status.is_none();
        Ok(CmdOutput {
            rc: status.map(exit_code).unwrap_or(-1),
            stdout: universal_newlines(stdout),
            stderr: if timed_out {
                crate::traits::RUN_TIMEOUT_STDERR.to_string()
            } else {
                String::new()
            },
        })
    }

    fn interrupt_scope(&self) -> PalResult<Box<dyn InterruptScope>> {
        INTERRUPTED.store(false, Ordering::SeqCst);
        let mut action = unsafe { std::mem::zeroed::<libc::sigaction>() };
        let mut previous = unsafe { std::mem::zeroed::<libc::sigaction>() };
        action.sa_sigaction = record_sigint as *const () as libc::sighandler_t;
        action.sa_flags = 0;
        // SAFETY: action 是全零初始化後設定了 handler，signal mask 在呼叫前初始化。
        unsafe {
            libc::sigemptyset(&mut action.sa_mask);
            if libc::sigaction(libc::SIGINT, &action, &mut previous) != 0 {
                return Err(posix_io_error(std::io::Error::last_os_error()));
            }
        }
        Ok(Box::new(PosixInterruptScope { previous }))
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
        which_posix(name)
    }

    fn which_all(&self, name: &str) -> Vec<PathBuf> {
        which_all_posix(name)
    }

    fn has_systemd(&self) -> bool {
        has_systemd_at(Path::new("/"))
    }

    fn is_wsl(&self) -> bool {
        is_wsl()
    }

    fn user_in_group(&self, group: &str) -> bool {
        user_in_group(group)
    }

    fn user_identity(&self) -> String {
        std::env::var_os("USER")
            .filter(|user| !user.is_empty())
            .map(|user| user.to_string_lossy().into_owned())
            .or_else(|| passwd_name_by_uid(unsafe { libc::geteuid() }))
            .unwrap_or_default()
    }

    fn local_appdata(&self) -> Option<PathBuf> {
        None
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
    match capture_command(command, timeout).map_err(|error| posix_spawn_error(error, program))? {
        CapturedOutput::TimedOut => Ok((-1, Vec::new(), crate::traits::RUN_TIMEOUT_STDERR.as_bytes().to_vec())),
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
    let output = capture_command_merged(command, timeout)
        .map_err(|error| posix_spawn_error(error, program))?;
    Ok((output.status, output.output))
}

fn posix_spawn_error(error: std::io::Error, program: &str) -> PalError {
    if let Some(code) = error.raw_os_error() {
        let base = posix_errno(code);
        let message = format!("{}: '{program}'", base.strerror);
        PalError::new(
            base.kind,
            base.os_code,
            base.winerror,
            base.strerror,
            format!("[Errno {code}] {message}"),
        )
    } else {
        PalError::plain(PalErrorKind::Io, error.to_string())
    }
}

fn decode_locale_text(bytes: &[u8]) -> PalResult<String> {
    match String::from_utf8(bytes.to_vec()) {
        Ok(text) => Ok(text),
        Err(error) => Err(PalError::new(
            PalErrorKind::Io,
            None,
            None,
            "",
            python_utf8_decode_error(
                bytes,
                error.utf8_error().valid_up_to(),
                error.utf8_error().error_len(),
            ),
        )),
    }
}

fn python_utf8_decode_error(bytes: &[u8], start: usize, length: Option<usize>) -> String {
    let encoding = "utf-8";
    let end = length.map_or(bytes.len(), |length| start.saturating_add(length));
    let first = bytes.get(start).copied().unwrap_or_default();
    let reason = if length.is_none() {
        "unexpected end of data"
    } else if matches!(first, 0x80..=0xc1 | 0xf5..=0xff) {
        "invalid start byte"
    } else {
        "invalid continuation byte"
    };
    if length.is_some() && reason == "invalid start byte" || end.saturating_sub(start) == 1 {
        format!("'{encoding}' codec can't decode byte 0x{first:02x} in position {start}: {reason}")
    } else {
        let last = end.saturating_sub(1).max(start);
        format!("'{encoding}' codec can't decode bytes in position {start}-{last}: {reason}")
    }
}

fn which_posix(name: &str) -> Option<PathBuf> {
    if name.contains('/') {
        let candidate = PathBuf::from(name);
        return is_executable_file(&candidate).then_some(candidate);
    }
    let path = match std::env::var_os("PATH") {
        Some(path) if path.is_empty() => return None,
        Some(path) => path,
        None => default_search_path()?,
    };
    std::env::split_paths(&path).find_map(|directory| {
        let candidate = if directory.as_os_str().is_empty() {
            PathBuf::from(name)
        } else {
            directory.join(name)
        };
        is_executable_file(&candidate).then_some(candidate)
    })
}

fn which_all_posix(name: &str) -> Vec<PathBuf> {
    let Some(path) = std::env::var_os("PATH") else {
        return Vec::new();
    };
    let mut seen = std::collections::HashSet::<OsString>::new();
    std::env::split_paths(&path)
        .filter(|directory| !directory.as_os_str().is_empty())
        .map(|directory| directory.join(name))
        .filter(|candidate| {
            is_executable_file(candidate) && seen.insert(candidate.as_os_str().to_os_string())
        })
        .collect()
}

fn default_search_path() -> Option<OsString> {
    // SAFETY: confstr 只讀取系統提供的 CS_PATH 字串。
    let size = unsafe { libc::confstr(libc::_CS_PATH, std::ptr::null_mut(), 0) };
    if size == 0 {
        return None;
    }
    let mut bytes = vec![0u8; size];
    // SAFETY: bytes 的容量符合前次查詢結果。
    let written = unsafe { libc::confstr(libc::_CS_PATH, bytes.as_mut_ptr().cast(), bytes.len()) };
    if written == 0 {
        return None;
    }
    bytes.truncate(written.saturating_sub(1));
    Some(OsString::from_vec(bytes))
}

fn is_executable_file(path: &Path) -> bool {
    if !path.is_file() {
        return false;
    }
    let Ok(path) = CString::new(path.as_os_str().as_bytes()) else {
        return false;
    };
    // SAFETY: `path` 是 NUL 結尾路徑；access 只查詢執行權限。
    unsafe { libc::access(path.as_ptr(), libc::X_OK) == 0 }
}

fn has_systemd_at(root: &Path) -> bool {
    root.join("run/systemd/system").is_dir()
}

fn is_wsl() -> bool {
    let mut info = std::mem::MaybeUninit::<libc::utsname>::zeroed();
    // SAFETY: `info` 指向可寫入的 utsname 儲存空間。
    if unsafe { libc::uname(info.as_mut_ptr()) } != 0 {
        return false;
    }
    // SAFETY: uname 成功後已初始化固定長度的 release 欄位。
    let info = unsafe { info.assume_init() };
    let release = unsafe { CStr::from_ptr(info.release.as_ptr()) };
    release
        .to_string_lossy()
        .to_ascii_lowercase()
        .contains("microsoft")
}

fn user_in_group(group: &str) -> bool {
    let Ok(name) = CString::new(group) else {
        return false;
    };
    let (status, gid) = with_retry_buffer(group_buffer(), |buffer| {
        let mut entry = std::mem::MaybeUninit::<libc::group>::uninit();
        let mut result = std::ptr::null_mut();
        // SAFETY: 呼叫期間，各參數均指向有效儲存空間。
        let status = unsafe {
            libc::getgrnam_r(
                name.as_ptr(),
                entry.as_mut_ptr(),
                buffer.as_mut_ptr().cast(),
                buffer.len(),
                &mut result,
            )
        };
        let gid = if status == 0 && !result.is_null() {
            // SAFETY: getgrnam_r 已在 entry 中回傳有效結果。
            Some(unsafe { (*result).gr_gid })
        } else {
            None
        };
        (status, gid)
    });
    if status != 0 {
        return false;
    }
    let Some(gid) = gid else {
        return false;
    };
    // SAFETY: getgroups(0, null) 只查詢目前補充群組數量。
    let count = unsafe { libc::getgroups(0, std::ptr::null_mut()) };
    if count < 0 {
        return false;
    }
    let mut groups = vec![0 as libc::gid_t; count as usize];
    if count > 0 {
        // SAFETY: groups 的容量足以容納前一次 getgroups 回報的群組。
        let actual = unsafe { libc::getgroups(count, groups.as_mut_ptr()) };
        if actual < 0 {
            return false;
        }
        groups.truncate(actual as usize);
    }
    groups.contains(&gid) || unsafe { libc::getegid() == gid }
}

fn group_buffer() -> Vec<u8> {
    // SAFETY: sysconf 只讀取編譯期定義的群組緩衝區大小常數。
    let size = unsafe { libc::sysconf(libc::_SC_GETGR_R_SIZE_MAX) };
    lookup_buffer(size)
}

fn passwd_name_by_uid(uid: libc::uid_t) -> Option<String> {
    let (status, name) = with_retry_buffer(passwd_buffer(), |buffer| {
        let mut entry = std::mem::MaybeUninit::<libc::passwd>::uninit();
        let mut result = std::ptr::null_mut();
        // SAFETY: passwd 緩衝區與 result 指標在呼叫期間均有效。
        let status = unsafe {
            libc::getpwuid_r(
                uid,
                entry.as_mut_ptr(),
                buffer.as_mut_ptr().cast(),
                buffer.len(),
                &mut result,
            )
        };
        let name = if status == 0 && !result.is_null() {
            // SAFETY: getpwuid_r 已回傳有效結果，pw_name 為 NUL 結尾字串。
            let name = unsafe { CStr::from_ptr((*result).pw_name) };
            Some(name.to_string_lossy().into_owned())
        } else {
            None
        };
        (status, name)
    });
    if status != 0 {
        return None;
    }
    name
}

fn passwd_buffer() -> Vec<u8> {
    // POSIX 允許 sysconf 回傳 -1；以保守緩衝區大小處理該情況。
    // SAFETY: sysconf 只讀取編譯期常數 _SC_GETPW_R_SIZE_MAX。
    let size = unsafe { libc::sysconf(libc::_SC_GETPW_R_SIZE_MAX) };
    lookup_buffer(size)
}

const MAX_LOOKUP_BUFFER: usize = 1024 * 1024;

fn lookup_buffer(size: libc::c_long) -> Vec<u8> {
    let size = if size > 0 { size as usize } else { 16 * 1024 };
    vec![0; size.min(MAX_LOOKUP_BUFFER)]
}

fn with_retry_buffer<T>(
    mut buffer: Vec<u8>,
    mut lookup: impl FnMut(&mut [u8]) -> (i32, T),
) -> (i32, T) {
    loop {
        let (status, value) = lookup(&mut buffer);
        if status != libc::ERANGE || buffer.len() >= MAX_LOOKUP_BUFFER {
            return (status, value);
        }
        let next_size = buffer.len().saturating_mul(2).min(MAX_LOOKUP_BUFFER);
        buffer.resize(next_size.max(buffer.len() + 1), 0);
    }
}

fn passwd_by_uid(uid: libc::uid_t) -> Option<String> {
    let mut entry = std::mem::MaybeUninit::<libc::passwd>::uninit();
    let mut result = std::ptr::null_mut();
    let mut buffer = passwd_buffer();
    // SAFETY: passwd buffer 與 result 指標在呼叫期間均有效；成功結果只讀取 pw_dir。
    let status = unsafe {
        libc::getpwuid_r(
            uid,
            entry.as_mut_ptr(),
            buffer.as_mut_ptr().cast(),
            buffer.len(),
            &mut result,
        )
    };
    if status != 0 || result.is_null() {
        return None;
    }
    // SAFETY: getpwuid_r 回傳非空 result，且 entry 在上方初始化。
    let home = unsafe { CStr::from_ptr((*result).pw_dir) };
    Some(home.to_string_lossy().into_owned())
}

/// 依 Python `errno.errorcode` 的 Linux 基準回傳名稱（同值別名採 Python 勝出值）。
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
        15 => Some("ENOTBLK"),
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
        26 => Some("ETXTBSY"),
        27 => Some("EFBIG"),
        28 => Some("ENOSPC"),
        29 => Some("ESPIPE"),
        30 => Some("EROFS"),
        31 => Some("EMLINK"),
        32 => Some("EPIPE"),
        33 => Some("EDOM"),
        34 => Some("ERANGE"),
        35 => Some("EDEADLOCK"),
        36 => Some("ENAMETOOLONG"),
        37 => Some("ENOLCK"),
        38 => Some("ENOSYS"),
        39 => Some("ENOTEMPTY"),
        40 => Some("ELOOP"),
        42 => Some("ENOMSG"),
        43 => Some("EIDRM"),
        44 => Some("ECHRNG"),
        45 => Some("EL2NSYNC"),
        46 => Some("EL3HLT"),
        47 => Some("EL3RST"),
        48 => Some("ELNRNG"),
        49 => Some("EUNATCH"),
        50 => Some("ENOCSI"),
        51 => Some("EL2HLT"),
        52 => Some("EBADE"),
        53 => Some("EBADR"),
        54 => Some("EXFULL"),
        55 => Some("ENOANO"),
        56 => Some("EBADRQC"),
        57 => Some("EBADSLT"),
        59 => Some("EBFONT"),
        60 => Some("ENOSTR"),
        61 => Some("ENODATA"),
        62 => Some("ETIME"),
        63 => Some("ENOSR"),
        64 => Some("ENONET"),
        65 => Some("ENOPKG"),
        66 => Some("EREMOTE"),
        67 => Some("ENOLINK"),
        68 => Some("EADV"),
        69 => Some("ESRMNT"),
        70 => Some("ECOMM"),
        71 => Some("EPROTO"),
        72 => Some("EMULTIHOP"),
        73 => Some("EDOTDOT"),
        74 => Some("EBADMSG"),
        75 => Some("EOVERFLOW"),
        76 => Some("ENOTUNIQ"),
        77 => Some("EBADFD"),
        78 => Some("EREMCHG"),
        79 => Some("ELIBACC"),
        80 => Some("ELIBBAD"),
        81 => Some("ELIBSCN"),
        82 => Some("ELIBMAX"),
        83 => Some("ELIBEXEC"),
        84 => Some("EILSEQ"),
        85 => Some("ERESTART"),
        86 => Some("ESTRPIPE"),
        87 => Some("EUSERS"),
        88 => Some("ENOTSOCK"),
        89 => Some("EDESTADDRREQ"),
        90 => Some("EMSGSIZE"),
        91 => Some("EPROTOTYPE"),
        92 => Some("ENOPROTOOPT"),
        93 => Some("EPROTONOSUPPORT"),
        94 => Some("ESOCKTNOSUPPORT"),
        95 => Some("ENOTSUP"),
        96 => Some("EPFNOSUPPORT"),
        97 => Some("EAFNOSUPPORT"),
        98 => Some("EADDRINUSE"),
        99 => Some("EADDRNOTAVAIL"),
        100 => Some("ENETDOWN"),
        101 => Some("ENETUNREACH"),
        102 => Some("ENETRESET"),
        103 => Some("ECONNABORTED"),
        104 => Some("ECONNRESET"),
        105 => Some("ENOBUFS"),
        106 => Some("EISCONN"),
        107 => Some("ENOTCONN"),
        108 => Some("ESHUTDOWN"),
        109 => Some("ETOOMANYREFS"),
        110 => Some("ETIMEDOUT"),
        111 => Some("ECONNREFUSED"),
        112 => Some("EHOSTDOWN"),
        113 => Some("EHOSTUNREACH"),
        114 => Some("EALREADY"),
        115 => Some("EINPROGRESS"),
        116 => Some("ESTALE"),
        117 => Some("EUCLEAN"),
        118 => Some("ENOTNAM"),
        119 => Some("ENAVAIL"),
        120 => Some("EISNAM"),
        121 => Some("EREMOTEIO"),
        122 => Some("EDQUOT"),
        123 => Some("ENOMEDIUM"),
        124 => Some("EMEDIUMTYPE"),
        125 => Some("ECANCELED"),
        126 => Some("ENOKEY"),
        127 => Some("EKEYEXPIRED"),
        128 => Some("EKEYREVOKED"),
        129 => Some("EKEYREJECTED"),
        130 => Some("EOWNERDEAD"),
        131 => Some("ENOTRECOVERABLE"),
        132 => Some("ERFKILL"),
        _ => None,
    }
}

/// POSIX 環境查詢的隔離建構子；正式平台仍使用 `NativeSysEnv` 的系統根目錄。
#[cfg(feature = "test-fakes")]
#[derive(Clone, Debug)]
pub struct SysEnvPosix {
    root: PathBuf,
}

#[cfg(feature = "test-fakes")]
impl SysEnvPosix {
    /// 建立以暫存根目錄為基準的測試環境。
    pub fn with_root(root: PathBuf) -> Self {
        Self { root }
    }

    fn has_systemd_at_root(&self) -> bool {
        has_systemd_at(&self.root)
    }
}

#[cfg(feature = "test-fakes")]
impl SysEnv for SysEnvPosix {
    fn var(&self, key: &str) -> Option<String> {
        NativeSysEnv.var(key)
    }
    fn vars(&self) -> Vec<(String, String)> {
        NativeSysEnv.vars()
    }
    fn system_config_dir(&self) -> String {
        NativeSysEnv.system_config_dir()
    }
    fn current_user_home(&self) -> Option<String> {
        NativeSysEnv.current_user_home()
    }
    fn user_home(&self, user: &str) -> Option<String> {
        NativeSysEnv.user_home(user)
    }
    fn current_exe(&self) -> PalResult<PathBuf> {
        NativeSysEnv.current_exe()
    }
    fn realpath(&self, path: &Path) -> PalResult<PathBuf> {
        NativeSysEnv.realpath(path)
    }
    fn terminal_columns(&self) -> Option<u16> {
        NativeSysEnv.terminal_columns()
    }
    fn stdout_is_terminal(&self) -> bool {
        NativeSysEnv.stdout_is_terminal()
    }
    fn errno_name(&self, code: i32) -> Option<&'static str> {
        NativeSysEnv.errno_name(code)
    }
    fn pid(&self) -> u32 {
        NativeSysEnv.pid()
    }
    fn run(&self, argv: &[String], timeout: Option<Duration>) -> PalResult<CmdOutput> {
        NativeSysEnv.run(argv, timeout)
    }
    fn run_merged(&self, argv: &[String], timeout: Option<Duration>) -> PalResult<CmdOutput> {
        NativeSysEnv.run_merged(argv, timeout)
    }
    fn interrupt_scope(&self) -> PalResult<Box<dyn InterruptScope>> {
        NativeSysEnv.interrupt_scope()
    }
    fn run_raw(
        &self,
        argv: &[String],
        env: Option<&[(OsString, OsString)]>,
        timeout: Option<Duration>,
    ) -> PalResult<CmdOutputRaw> {
        NativeSysEnv.run_raw(argv, env, timeout)
    }
    fn which(&self, name: &str) -> Option<PathBuf> {
        NativeSysEnv.which(name)
    }
    fn which_all(&self, name: &str) -> Vec<PathBuf> {
        NativeSysEnv.which_all(name)
    }
    fn has_systemd(&self) -> bool {
        self.has_systemd_at_root()
    }
    fn is_wsl(&self) -> bool {
        NativeSysEnv.is_wsl()
    }
    fn user_in_group(&self, group: &str) -> bool {
        NativeSysEnv.user_in_group(group)
    }
    fn user_identity(&self) -> String {
        NativeSysEnv.user_identity()
    }
    fn local_appdata(&self) -> Option<PathBuf> {
        NativeSysEnv.local_appdata()
    }
}

#[cfg(not(feature = "test-fakes"))]
pub type SysEnvPosix = NativeSysEnv;

#[cfg(test)]
mod tests {
    use super::{lookup_buffer, with_retry_buffer, NativeSysEnv, MAX_LOOKUP_BUFFER};
    use crate::traits::SysEnv;
    use std::time::Duration;

    #[test]
    fn retry_buffer_grows_after_erange() {
        let mut lengths = Vec::new();
        let (status, found) = with_retry_buffer(lookup_buffer(1), |buffer| {
            lengths.push(buffer.len());
            if buffer.len() < 16 {
                (libc::ERANGE, None)
            } else {
                (0, Some(buffer.len()))
            }
        });

        assert_eq!(status, 0);
        assert_eq!(found, Some(16));
        assert_eq!(lengths, vec![1, 2, 4, 8, 16]);
    }

    #[test]
    fn retry_buffer_stops_at_one_megabyte() {
        let (status, size) =
            with_retry_buffer(lookup_buffer(1), |buffer| (libc::ERANGE, buffer.len()));

        assert_eq!(status, libc::ERANGE);
        assert_eq!(size, MAX_LOOKUP_BUFFER);
    }

    #[test]
    fn merged_runner_preserves_stdout_and_stderr_write_order() {
        let output = NativeSysEnv
            .run_merged(
                &[
                    "/bin/sh".into(),
                    "-c".into(),
                    "printf first; printf second >&2; printf third".into(),
                ],
                Some(Duration::from_secs(3)),
            )
            .expect("合併 runner 執行成功");

        assert_eq!(output.rc, 0);
        assert_eq!(output.stdout, "firstsecondthird");
        assert!(output.stderr.is_empty());
    }
}
