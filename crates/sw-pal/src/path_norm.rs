//! 端點與路徑的比對正規化（arch §4.4，P5-T4）。
//!
//! 比較前正規化端點路徑，對齊 `sw_core.util.normalize_endpoint_path`。

use std::{
    io,
    path::{Component, Path, PathBuf},
};

/// 去 `unix://` 前綴 → `expanduser`（對齊 P1 `normalize_endpoint_path`）→ abspath → normpath；
/// Windows 另轉小寫並統一 `\`。用於實例鎖的 `(lock_path, socket)` 比對。
pub fn normalize_for_compare(p: &str, cwd: &Path, home: Option<&Path>) -> String {
    if p.starts_with("tcp://") {
        return p.to_string();
    }

    let path = p.strip_prefix("unix://").unwrap_or(p);
    let expanded = expanduser(path, home);
    #[cfg(not(windows))]
    let preserve_double_leading_slash = expanded.starts_with("//") && !expanded.starts_with("///");
    let mut path = PathBuf::from(expanded);

    #[cfg(windows)]
    if is_drive_relative(&path) {
        path = resolve_windows_drive_relative(&path, cwd);
    }

    #[cfg(windows)]
    if has_root(&path) && !has_prefix(&path) {
        if let Some(prefix) = path_prefix(cwd) {
            path = PathBuf::from(format!(
                "{}{}",
                prefix.to_string_lossy(),
                path.to_string_lossy()
            ));
        }
    }

    if !path.is_absolute() && !has_root(&path) {
        let base = if cwd.is_absolute() || has_root(cwd) {
            cwd.to_path_buf()
        } else {
            std::env::current_dir()
                .unwrap_or_else(|_| PathBuf::from("."))
                .join(cwd)
        };
        path = base.join(path);
    }

    let normalized = normalize_components(&path);
    #[cfg(windows)]
    {
        normalized
            .to_string_lossy()
            .replace('/', "\\")
            .to_lowercase()
    }
    #[cfg(not(windows))]
    {
        let normalized = normalized.to_string_lossy();
        if preserve_double_leading_slash && normalized.starts_with('/') {
            format!("/{normalized}")
        } else {
            normalized.into_owned()
        }
    }
}

/// 套用 Python `endpoint_path_for_io` 的解包、家目錄展開與絕對路徑語意；保留 `..` 與 symlink 的 OS 解析順序。
pub(crate) fn endpoint_path_for_io(path: &Path) -> io::Result<PathBuf> {
    #[cfg(unix)]
    {
        use std::os::unix::ffi::{OsStrExt, OsStringExt};

        let bytes = path.as_os_str().as_bytes();
        let bytes = bytes.strip_prefix(b"unix://").unwrap_or(bytes);
        let path = if let Ok(value) = std::str::from_utf8(bytes) {
            PathBuf::from(expanduser(value, None))
        } else {
            PathBuf::from(std::ffi::OsString::from_vec(bytes.to_vec()))
        };
        if path.is_absolute() {
            Ok(path)
        } else {
            Ok(std::env::current_dir()?.join(path))
        }
    }

    #[cfg(windows)]
    {
        let value = path.to_string_lossy();
        let value = value.strip_prefix("unix://").unwrap_or(&value);
        let path = PathBuf::from(expanduser(value, None));
        if path.is_absolute() || has_root(&path) {
            return Ok(path);
        }

        let cwd = std::env::current_dir()?;
        if is_drive_relative(&path) {
            return Ok(resolve_windows_drive_relative(&path, &cwd));
        }
        Ok(cwd.join(path))
    }

    #[cfg(not(any(unix, windows)))]
    {
        let value = path.to_string_lossy();
        let value = value.strip_prefix("unix://").unwrap_or(&value);
        let path = PathBuf::from(expanduser(value, None));
        if path.is_absolute() {
            Ok(path)
        } else {
            Ok(std::env::current_dir()?.join(path))
        }
    }
}

fn has_root(path: &Path) -> bool {
    path.components()
        .any(|component| matches!(component, Component::RootDir))
}

#[cfg(windows)]
fn has_prefix(path: &Path) -> bool {
    path.components()
        .any(|component| matches!(component, Component::Prefix(_)))
}

#[cfg(windows)]
fn path_prefix(path: &Path) -> Option<&std::ffi::OsStr> {
    path.components().find_map(|component| match component {
        Component::Prefix(prefix) => Some(prefix.as_os_str()),
        _ => None,
    })
}

#[cfg(windows)]
fn is_drive_relative(path: &Path) -> bool {
    matches!(path.components().next(), Some(Component::Prefix(prefix))
        if matches!(prefix.kind(), std::path::Prefix::Disk(_)))
        && !has_root(path)
}

#[cfg(windows)]
fn resolve_windows_drive_relative(path: &Path, cwd: &Path) -> PathBuf {
    let drive = match path.components().next() {
        Some(Component::Prefix(prefix)) => match prefix.kind() {
            std::path::Prefix::Disk(drive) => drive,
            _ => return windows_full_path(path).unwrap_or_else(|| path.to_path_buf()),
        },
        _ => return path.to_path_buf(),
    };
    let mut suffix = PathBuf::new();
    for component in path.components().skip(1) {
        suffix.push(component.as_os_str());
    }

    if path_prefix(cwd).is_some_and(|prefix| {
        prefix
            .to_string_lossy()
            .as_bytes()
            .first()
            .is_some_and(|cwd_drive| cwd_drive.eq_ignore_ascii_case(&drive))
    }) {
        return cwd.join(suffix);
    }

    windows_full_path(path).unwrap_or_else(|| path.to_path_buf())
}

#[cfg(windows)]
fn windows_full_path(path: &Path) -> Option<PathBuf> {
    use std::os::windows::{ffi::OsStrExt, ffi::OsStringExt};

    use windows_sys::Win32::Storage::FileSystem::GetFullPathNameW;

    let wide_path: Vec<u16> = path.as_os_str().encode_wide().chain(Some(0)).collect();
    let mut buffer = vec![0_u16; 512];
    loop {
        // SAFETY: 輸入為 NUL 結尾；輸出緩衝區可寫且大小以 u32 傳入。
        let length = unsafe {
            GetFullPathNameW(
                wide_path.as_ptr(),
                buffer.len() as u32,
                buffer.as_mut_ptr(),
                std::ptr::null_mut(),
            )
        } as usize;
        if length == 0 {
            return None;
        }
        if length >= buffer.len() {
            buffer.resize(length + 1, 0);
            continue;
        }
        return Some(PathBuf::from(std::ffi::OsString::from_wide(
            &buffer[..length],
        )));
    }
}

pub(crate) fn expanduser(path: &str, home: Option<&Path>) -> String {
    if !path.starts_with('~') {
        return path.to_string();
    }

    #[cfg(windows)]
    let separator = path.find(['/', '\\']).unwrap_or(path.len());
    #[cfg(not(windows))]
    let separator = path.find('/').unwrap_or(path.len());

    let username = &path[1..separator];
    let home = if username.is_empty() {
        home.map(Path::to_path_buf).or_else(default_home)
    } else {
        user_home(username)
    };
    let Some(home) = home else {
        return path.to_string();
    };
    let suffix = &path[separator..];
    format!("{}{}", home.to_string_lossy(), suffix)
}

#[cfg(windows)]
fn default_home() -> Option<PathBuf> {
    if let Some(home) = std::env::var_os("USERPROFILE") {
        return Some(PathBuf::from(home));
    }
    let home_path = std::env::var_os("HOMEPATH")?;
    let drive = std::env::var_os("HOMEDRIVE").unwrap_or_default();
    Some(PathBuf::from(drive).join(home_path))
}

#[cfg(windows)]
fn user_home(username: &str) -> Option<PathBuf> {
    let home = default_home()?;
    let current_user = std::env::var("USERNAME").ok()?;
    if username == current_user {
        return Some(home);
    }
    if home.file_name()?.to_str()? != current_user {
        return None;
    }
    Some(home.parent()?.join(username))
}

#[cfg(unix)]
fn default_home() -> Option<PathBuf> {
    std::env::var_os("HOME")
        .map(PathBuf::from)
        .or_else(|| posix_user_home(None))
}

#[cfg(unix)]
fn user_home(username: &str) -> Option<PathBuf> {
    posix_user_home(Some(username))
}

#[cfg(unix)]
fn posix_user_home(username: Option<&str>) -> Option<PathBuf> {
    use std::{
        ffi::{CStr, CString},
        mem::MaybeUninit,
        os::unix::ffi::OsStringExt,
    };

    let username = match username {
        Some(username) => Some(CString::new(username.as_bytes()).ok()?),
        None => None,
    };
    let mut buffer = vec![0_u8; 1024];
    loop {
        let mut entry = MaybeUninit::<libc::passwd>::uninit();
        let mut result = std::ptr::null_mut();
        let rc = unsafe {
            match username.as_ref() {
                Some(name) => libc::getpwnam_r(
                    name.as_ptr(),
                    entry.as_mut_ptr(),
                    buffer.as_mut_ptr().cast(),
                    buffer.len(),
                    &mut result,
                ),
                None => libc::getpwuid_r(
                    libc::getuid(),
                    entry.as_mut_ptr(),
                    buffer.as_mut_ptr().cast(),
                    buffer.len(),
                    &mut result,
                ),
            }
        };
        if rc == libc::ERANGE && buffer.len() < 1024 * 1024 {
            buffer.resize(buffer.len() * 2, 0);
            continue;
        }
        if rc != 0 || result.is_null() {
            return None;
        }

        let entry = unsafe { entry.assume_init() };
        if entry.pw_dir.is_null() {
            return None;
        }
        let home = unsafe { CStr::from_ptr(entry.pw_dir) }.to_bytes().to_vec();
        return Some(PathBuf::from(std::ffi::OsString::from_vec(home)));
    }
}

#[cfg(not(any(unix, windows)))]
fn default_home() -> Option<PathBuf> {
    std::env::var_os("HOME").map(PathBuf::from)
}

#[cfg(not(any(unix, windows)))]
fn user_home(_username: &str) -> Option<PathBuf> {
    None
}

fn normalize_components(path: &Path) -> PathBuf {
    let rooted = path
        .components()
        .any(|component| matches!(component, Component::RootDir));
    let mut normalized = PathBuf::new();
    for component in path.components() {
        match component {
            Component::CurDir => {}
            Component::ParentDir => {
                if !normalized.pop() && !rooted {
                    normalized.push(component.as_os_str());
                }
            }
            other => normalized.push(other.as_os_str()),
        }
    }
    if normalized.as_os_str().is_empty() {
        normalized.push(".");
    }
    normalized
}
