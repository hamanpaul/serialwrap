//! Python 路徑操作相容層（posixpath / ntpath）（P3-T4）。
//!
//! 演算法逐條對齊 CPython 3.12.3 `Lib/posixpath.py` 與 `Lib/ntpath.py` 的純 Python 版本。
//! `normpath` 的 Windows 版取純 Python 語意（與 `contracts/golden/pypath.windows.tsv` 的產生方式一致）；
//! Windows 原生 CPython 3.12.10 改用 C 實作 `nt._path_normpath`，兩者對少數病態輸入不同，已登記為 NC-04（`spec-reconciliation.md` §6），
//! 本檔的 `normpath_windows_c_impl_difference_recorded_nc04` 測試記錄該差異。
//!
//! 字元與位元組：Windows 的磁碟判定以 Python 字元索引（`normp[1:2] == ':'`）為準，首字元可能是多位元組 UTF-8，
//! 因此 `splitroot_win` 先以字元迭代判定，再換算為位元組位移切片。其餘分隔字元皆為 ASCII。

use crate::runtime_paths::EnvSnapshot;

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
    match f {
        PathFlavor::Posix => join_posix(a, rest),
        PathFlavor::Windows => join_win(a, rest),
    }
}

/// `posixpath.normpath` 或 `ntpath.normpath`。
pub fn normpath(f: PathFlavor, p: &str) -> String {
    match f {
        PathFlavor::Posix => normpath_posix(p),
        PathFlavor::Windows => normpath_win(p),
    }
}

/// `posixpath.isabs` 或 `ntpath.isabs`。
pub fn isabs(f: PathFlavor, p: &str) -> bool {
    match f {
        PathFlavor::Posix => p.starts_with('/'),
        PathFlavor::Windows => {
            let s: Vec<char> = p
                .chars()
                .take(3)
                .map(|c| if c == '/' { '\\' } else { c })
                .collect();
            if s.first() == Some(&'\\') {
                return true;
            }
            s.len() >= 3 && s[1] == ':' && s[2] == '\\'
        }
    }
}

/// `posixpath.dirname` 或 `ntpath.dirname`。
pub fn dirname(f: PathFlavor, p: &str) -> String {
    split(f, p).0
}

/// `posixpath.basename` 或 `ntpath.basename`。
pub fn basename(f: PathFlavor, p: &str) -> String {
    split(f, p).1
}

/// `posixpath.splitext` 或 `ntpath.splitext`。
pub fn splitext(f: PathFlavor, p: &str) -> (String, String) {
    match f {
        PathFlavor::Posix => splitext_generic(p, b'/', None),
        PathFlavor::Windows => splitext_generic(p, b'\\', Some(b'/')),
    }
}

/// `posixpath.expanduser` 或 `ntpath.expanduser`。
pub fn expanduser(f: PathFlavor, p: &str, env: &EnvSnapshot, home: &dyn HomeLookup) -> String {
    match f {
        PathFlavor::Posix => expanduser_posix(p, env, home),
        PathFlavor::Windows => expanduser_win(p, env, home),
    }
}

// ---------------------------------------------------------------------------
// POSIX
// ---------------------------------------------------------------------------

fn join_posix(a: &str, rest: &[&str]) -> String {
    let mut path = a.to_string();
    for b in rest {
        if b.starts_with('/') {
            path = (*b).to_string();
        } else if path.is_empty() || path.ends_with('/') {
            path.push_str(b);
        } else {
            path.push('/');
            path.push_str(b);
        }
    }
    path
}

fn normpath_posix(p: &str) -> String {
    if p.is_empty() {
        return ".".to_string();
    }
    // POSIX 允許一或兩個前導斜線，三個以上視為單一斜線。
    let mut initial: usize = if p.starts_with('/') { 1 } else { 0 };
    if initial == 1 && p.starts_with("//") && !p.starts_with("///") {
        initial = 2;
    }
    let mut new: Vec<&str> = Vec::new();
    for comp in p.split('/') {
        if comp.is_empty() || comp == "." {
            continue;
        }
        if comp != ".." || (initial == 0 && new.is_empty()) || new.last() == Some(&"..") {
            new.push(comp);
        } else if !new.is_empty() {
            new.pop();
        }
    }
    let mut path = new.join("/");
    if initial > 0 {
        path = format!("{}{}", "/".repeat(initial), path);
    }
    if path.is_empty() {
        ".".to_string()
    } else {
        path
    }
}

fn split_posix(p: &str) -> (String, String) {
    let i = p.rfind('/').map_or(0, |k| k + 1);
    let head = &p[..i];
    let tail = &p[i..];
    let head = if !head.is_empty() && !head.bytes().all(|b| b == b'/') {
        head.trim_end_matches('/')
    } else {
        head
    };
    (head.to_string(), tail.to_string())
}

fn expanduser_posix(p: &str, env: &EnvSnapshot, home: &dyn HomeLookup) -> String {
    if !p.starts_with('~') {
        return p.to_string();
    }
    let i = p[1..].find('/').map_or(p.len(), |k| k + 1);
    let userhome = if i == 1 {
        match env.get("HOME") {
            Some(h) => h.to_string(),
            None => match home.current_user_home() {
                Some(h) => h,
                None => return p.to_string(),
            },
        }
    } else {
        match home.user_home(&p[1..i]) {
            Some(h) => h,
            None => return p.to_string(),
        }
    };
    let joined = format!("{}{}", userhome.trim_end_matches('/'), &p[i..]);
    if joined.is_empty() {
        "/".to_string()
    } else {
        joined
    }
}

// ---------------------------------------------------------------------------
// Windows
// ---------------------------------------------------------------------------

/// `ntpath.splitroot`：回傳 `(drive, root, tail)`，三者皆為 `p` 的切片。
fn splitroot_win(p: &str) -> (&str, &str, &str) {
    let np = p.replace('/', "\\");
    let nb = np.as_bytes();
    if nb.first() == Some(&b'\\') {
        if nb.get(1) == Some(&b'\\') {
            // UNC（`\\server\share`）或裝置路徑（`\\?\UNC\server\share`）。
            let unc = np
                .get(..8)
                .is_some_and(|h| h.eq_ignore_ascii_case("\\\\?\\UNC\\"));
            let start = if unc { 8 } else { 2 };
            let Some(i1) = find_backslash_from(nb, start) else {
                return (p, "", "");
            };
            let Some(i2) = find_backslash_from(nb, i1 + 1) else {
                return (p, "", "");
            };
            return (&p[..i2], &p[i2..i2 + 1], &p[i2 + 1..]);
        }
        // 相對於磁碟根的路徑，例如 `\Windows`。
        return ("", &p[..1], &p[1..]);
    }
    // 對應 Python `normp[1:2] == ':'`（字元索引）：取前兩個字元，第二個為 ':' 才是磁碟。
    let mut chars = np.chars();
    if let (Some(c0), Some(':')) = (chars.next(), chars.next()) {
        // `end` 為冒號之後的位元組位移（`c0` 可能是多位元組字元）。
        let end = c0.len_utf8() + 1;
        if nb.get(end) == Some(&b'\\') {
            // 絕對磁碟路徑，例如 `C:\Windows`、`é:\x`。
            return (&p[..end], &p[end..end + 1], &p[end + 1..]);
        }
        // 磁碟相對路徑，例如 `C:Windows`。
        return (&p[..end], "", &p[end..]);
    }
    ("", "", p)
}

fn find_backslash_from(nb: &[u8], from: usize) -> Option<usize> {
    nb.iter()
        .enumerate()
        .skip(from)
        .find(|(_, &c)| c == b'\\')
        .map(|(i, _)| i)
}

/// `ntpath.split`：`(head, tail)`，head 已去除尾端分隔字元。
fn split_win(p: &str) -> (String, String) {
    let (d, r, tail) = splitroot_win(p);
    let i = tail.rfind(['\\', '/']).map_or(0, |k| k + 1);
    let head = &tail[..i];
    let rest = &tail[i..];
    (
        format!("{d}{r}{}", head.trim_end_matches(['\\', '/'])),
        rest.to_string(),
    )
}

fn dirname_win(p: &str) -> String {
    split_win(p).0
}

fn basename_win(p: &str) -> String {
    split_win(p).1
}

fn split(f: PathFlavor, p: &str) -> (String, String) {
    match f {
        PathFlavor::Posix => split_posix(p),
        PathFlavor::Windows => split_win(p),
    }
}

/// `ntpath.join`（含跨磁碟與 UNC 規則）。
fn join_win(a: &str, rest: &[&str]) -> String {
    let (d0, r0, p0) = splitroot_win(a);
    let mut rdrive = d0.to_string();
    let mut rroot = r0.to_string();
    let mut rpath = p0.to_string();
    for b in rest {
        let (pdrive, proot, ppath) = splitroot_win(b);
        if !proot.is_empty() {
            if !pdrive.is_empty() || rdrive.is_empty() {
                rdrive = pdrive.to_string();
            }
            rroot = proot.to_string();
            rpath = ppath.to_string();
            continue;
        } else if !pdrive.is_empty() && pdrive != rdrive {
            if pdrive.to_lowercase() != rdrive.to_lowercase() {
                rdrive = pdrive.to_string();
                rroot = String::new();
                rpath = ppath.to_string();
                continue;
            }
            rdrive = pdrive.to_string();
        }
        if let Some(last) = rpath.chars().last() {
            if last != '\\' && last != '/' {
                rpath.push('\\');
            }
        }
        rpath.push_str(ppath);
    }
    // 對應 CPython：UNC 或磁碟與相對路徑之間補分隔字元（磁碟字元結尾者不補）。
    if !rpath.is_empty()
        && rroot.is_empty()
        && !rdrive.is_empty()
        && !rdrive.ends_with([':', '\\', '/'])
    {
        return format!("{rdrive}\\{rpath}");
    }
    format!("{rdrive}{rroot}{rpath}")
}

/// `ntpath.normpath` 的純 Python 版本（對應 Linux golden 產生環境）。
///
/// Windows CPython 3.12.10 改用 C 實作 `nt._path_normpath`，對 `C:.\foo`、`C:./`、`\\?\UNC\…\..\x` 等輸入結果不同，
/// 已登記為 NC-04（見 `spec-reconciliation.md` §6）；本函式保留純 Python 語意。
fn normpath_win(p: &str) -> String {
    let path = p.replace('/', "\\");
    let (drive, root, rest) = splitroot_win(&path);
    let prefix = format!("{drive}{root}");
    let mut comps: Vec<&str> = rest.split('\\').collect();
    let mut i = 0usize;
    while i < comps.len() {
        if comps[i].is_empty() || comps[i] == "." {
            comps.remove(i);
        } else if comps[i] == ".." {
            if i > 0 && comps[i - 1] != ".." {
                comps.drain(i - 1..=i);
                i -= 1;
            } else if i == 0 && !root.is_empty() {
                comps.remove(i);
            } else {
                i += 1;
            }
        } else {
            i += 1;
        }
    }
    if prefix.is_empty() && comps.is_empty() {
        comps.push(".");
    }
    format!("{prefix}{}", comps.join("\\"))
}

/// `genericpath._splitext`。
fn splitext_generic(p: &str, sep: u8, altsep: Option<u8>) -> (String, String) {
    let b = p.as_bytes();
    let mut sep_index: isize = b.iter().rposition(|&c| c == sep).map_or(-1, |i| i as isize);
    if let Some(alt) = altsep {
        let alt_index = b.iter().rposition(|&c| c == alt).map_or(-1, |i| i as isize);
        sep_index = sep_index.max(alt_index);
    }
    let dot_index: isize = b
        .iter()
        .rposition(|&c| c == b'.')
        .map_or(-1, |i| i as isize);
    if dot_index > sep_index {
        let mut fi = sep_index + 1;
        while fi < dot_index {
            if b[fi as usize] != b'.' {
                let d = dot_index as usize;
                return (p[..d].to_string(), p[d..].to_string());
            }
            fi += 1;
        }
    }
    (p.to_string(), String::new())
}

/// `ntpath.expanduser`（忠實移植 CPython 3.12.3 的 Windows 分支，無已登記差異）。
///
/// `~user` 比對 `USERNAME`：`USERNAME` 未設定（`None`）時不等於任何 basename，因此原樣回傳；
/// `USERNAME` 與目標不同時，僅在家目錄 basename 等於 `USERNAME` 的情況下，以其上層目錄代換為目標使用者。
fn expanduser_win(p: &str, env: &EnvSnapshot, _home: &dyn HomeLookup) -> String {
    if !p.starts_with('~') {
        return p.to_string();
    }
    let b = p.as_bytes();
    let mut i = 1usize;
    while i < b.len() && b[i] != b'\\' && b[i] != b'/' {
        i += 1;
    }
    let mut userhome: String = if let Some(up) = env.get("USERPROFILE") {
        up.to_string()
    } else if let Some(hp) = env.get("HOMEPATH") {
        let drive = env.get("HOMEDRIVE").unwrap_or("");
        join_win(drive, &[hp])
    } else {
        return p.to_string();
    };
    if i != 1 {
        let target = &p[1..i];
        let current = env.get("USERNAME");
        if Some(target) != current {
            let base = basename_win(&userhome);
            if current != Some(base.as_str()) {
                return p.to_string();
            }
            userhome = join_win(&dirname_win(&userhome), &[target]);
        }
    }
    format!("{userhome}{}", &p[i..])
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Windows `expanduser` 不查詢 pwd；此假實作只為滿足介面。
    struct NoPwd;
    impl HomeLookup for NoPwd {
        fn current_user_home(&self) -> Option<String> {
            None
        }
        fn user_home(&self, _user: &str) -> Option<String> {
            None
        }
    }

    fn win_env(pairs: &[(&str, &str)]) -> EnvSnapshot {
        EnvSnapshot::from_pairs(pairs.iter().map(|(k, v)| (k.to_string(), v.to_string())))
    }

    /// 以下期望值皆由 CPython 3.12.3 `ntpath.expanduser`（patch `os.environ`）實測；Windows 3.12.10 結果相同。
    #[test]
    fn expanduser_win_missing_username_matches_cpython() {
        let env = win_env(&[("USERPROFILE", r"C:\Users\golden")]);
        // USERNAME 未設定：~user 不等於 None，且 None 不等於 basename，原樣回傳。
        assert_eq!(expanduser_win("~bob/x", &env, &NoPwd), "~bob/x");
        assert_eq!(expanduser_win("~golden/x", &env, &NoPwd), "~golden/x");
        assert_eq!(expanduser_win("~", &env, &NoPwd), r"C:\Users\golden");
        assert_eq!(expanduser_win("~/x", &env, &NoPwd), r"C:\Users\golden/x");
    }

    #[test]
    fn expanduser_win_with_username_matches_cpython() {
        let env = win_env(&[("USERPROFILE", r"C:\Users\golden"), ("USERNAME", "golden")]);
        assert_eq!(expanduser_win("~bob/x", &env, &NoPwd), r"C:\Users\bob/x");
        assert_eq!(
            expanduser_win("~golden/x", &env, &NoPwd),
            r"C:\Users\golden/x"
        );
    }

    #[test]
    fn expanduser_win_homepath_branch_and_no_env() {
        let env = win_env(&[("HOMEDRIVE", "D:"), ("HOMEPATH", r"\u\me")]);
        assert_eq!(expanduser_win("~/x", &env, &NoPwd), r"D:\u\me/x");
        assert_eq!(expanduser_win("~bob/x", &env, &NoPwd), "~bob/x");
        let empty = win_env(&[]);
        assert_eq!(expanduser_win("~/x", &empty, &NoPwd), "~/x");
    }

    #[test]
    fn windows_drive_is_char_indexed_for_multibyte_first_char() {
        // CPython：`normp[1:2] == ':'` 為字元索引，首字元為多位元組時仍判定為磁碟。
        assert_eq!(dirname(PathFlavor::Windows, "é:/"), "é:/");
        assert_eq!(basename(PathFlavor::Windows, "\u{3000}:"), "");
        assert_eq!(dirname(PathFlavor::Windows, "😀:foo"), "😀:");
        assert_eq!(basename(PathFlavor::Windows, "😀:foo"), "foo");
        assert_eq!(
            join(PathFlavor::Windows, "\u{3000}:", &["a.\\"]),
            "\u{3000}:a.\\"
        );
        assert_eq!(normpath(PathFlavor::Windows, "é:/"), "é:\\");
        assert!(isabs(PathFlavor::Windows, "é:\\x"));
    }

    /// NC-04：Windows CPython 3.12.10（`nt._path_normpath`，C 實作）與本函式（純 Python 語意）的已登記差異。
    /// 左側為 Rust 結果（Linux 3.12.3 純 Python `ntpath.normpath` 同值）；註解為 Windows 3.12.10 實測值。
    /// 此測試記錄差異，不代表 Windows 原生行為。
    #[test]
    fn normpath_windows_c_impl_difference_recorded_nc04() {
        // Windows: `C:.\foo`
        assert_eq!(normpath(PathFlavor::Windows, r"C:.\foo"), "C:foo");
        // Windows: `C:.`
        assert_eq!(normpath(PathFlavor::Windows, "C:."), "C:");
        // Windows: `C:.`
        assert_eq!(normpath(PathFlavor::Windows, "C:./"), "C:");
        // Windows: `D:.`
        assert_eq!(normpath(PathFlavor::Windows, r"D:.\"), "D:");
        // Windows: `\\?\UNC\x`
        assert_eq!(
            normpath(PathFlavor::Windows, r"\\?\UNC\server\share\..\..\x"),
            r"\\?\UNC\server\share\x"
        );
        // Windows: `\\?\UNC\server\share`
        assert_eq!(
            normpath(PathFlavor::Windows, r"\\?\UNC\server\share\\"),
            r"\\?\UNC\server\share\"
        );
    }
}
