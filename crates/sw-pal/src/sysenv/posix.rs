//! POSIX 環境查詢。

use crate::traits::{posix_io_error, PalResult, SysEnv};
use std::ffi::{CStr, CString};
use std::io::IsTerminal;
use std::path::{Path, PathBuf};

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
}

fn passwd_buffer() -> Vec<u8> {
    // POSIX 允許 sysconf 回傳 -1；以保守緩衝區大小處理該情況。
    // SAFETY: sysconf 只讀取編譯期常數 _SC_GETPW_R_SIZE_MAX。
    let size = unsafe { libc::sysconf(libc::_SC_GETPW_R_SIZE_MAX) };
    vec![0; if size > 0 { size as usize } else { 16 * 1024 }]
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

/// P4-T2：以 root 為基準的 POSIX 環境查詢型別名（正式路徑 root=`/`）。
pub type SysEnvPosix = NativeSysEnv;

#[cfg(feature = "test-fakes")]
impl NativeSysEnv {
    /// P4-T2 紅燈骨架：測試建構子（`test-fakes` 限定）。實作者改為記錄 root，`has_systemd` 等以 root 解析。
    pub fn with_root(_root: PathBuf) -> Self {
        unimplemented!("P4-T2")
    }
}
