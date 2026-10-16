//! Python 路徑操作相容層（posixpath / ntpath）（P3-T4 stub，行為尚未實作）。
#![allow(unused_variables)]

/// 路徑風格：POSIX 或 Windows。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum PathFlavor {
    Posix,
    Windows,
}

/// 家目錄查詢介面（供 `expanduser` 使用）。
pub trait HomeLookup {
    /// 目前使用者的家目錄（POSIX `pwd.getpwuid(os.getuid()).pw_dir`）。
    fn current_user_home(&self) -> Option<String>;

    /// 指定使用者的家目錄（POSIX `pwd.getpwnam(user).pw_dir`）。
    fn user_home(&self, user: &str) -> Option<String>;
}

/// `posixpath.join` 或 `ntpath.join`。
pub fn join(f: PathFlavor, a: &str, rest: &[&str]) -> String {
    todo!("P3-T4: join")
}

/// `posixpath.normpath` 或 `ntpath.normpath`。
pub fn normpath(f: PathFlavor, p: &str) -> String {
    todo!("P3-T4: normpath")
}

/// `posixpath.isabs` 或 `ntpath.isabs`。
pub fn isabs(f: PathFlavor, p: &str) -> bool {
    todo!("P3-T4: isabs")
}

/// `posixpath.dirname` 或 `ntpath.dirname`。
pub fn dirname(f: PathFlavor, p: &str) -> String {
    todo!("P3-T4: dirname")
}

/// `posixpath.basename` 或 `ntpath.basename`。
pub fn basename(f: PathFlavor, p: &str) -> String {
    todo!("P3-T4: basename")
}

/// `posixpath.splitext` 或 `ntpath.splitext`。
pub fn splitext(f: PathFlavor, p: &str) -> (String, String) {
    todo!("P3-T4: splitext")
}

/// `posixpath.expanduser` 或 `ntpath.expanduser`。
pub fn expanduser(
    f: PathFlavor,
    p: &str,
    env: &crate::runtime_paths::EnvSnapshot,
    home: &dyn HomeLookup,
) -> String {
    todo!("P3-T4: expanduser")
}
