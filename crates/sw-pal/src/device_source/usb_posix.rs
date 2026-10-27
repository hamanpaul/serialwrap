//! POSIX `UsbTopology`（P7 Task 6；`transport_stall.resolve_usb_busid`）：sysfs tty `device` 連結的 realpath，
//! 由深至淺找第一個 fullmatch `\d+-\d+(\.\d+)*` 的路徑段（不含 interface 後綴 `1-1:1.0`）。任何錯誤回 `None`。
use std::path::{Path, PathBuf};

use crate::traits::UsbTopology;

/// 以 `sysfs_root`（預設 `/sys`）解析 USB busid；測試可注入 tempdir。
pub struct SysfsUsbTopology {
    root: PathBuf,
}

impl SysfsUsbTopology {
    pub fn with_root(root: &Path) -> Self {
        Self {
            root: root.to_path_buf(),
        }
    }
}

impl Default for SysfsUsbTopology {
    fn default() -> Self {
        Self::with_root(Path::new("/sys"))
    }
}

/// `re.fullmatch(r"\d+-\d+(\.\d+)*", seg)`。
fn is_busid(seg: &str) -> bool {
    let Some((head, rest)) = seg.split_once('-') else {
        return false;
    };
    let digits = |s: &str| !s.is_empty() && s.bytes().all(|b| b.is_ascii_digit());
    digits(head) && rest.split('.').all(digits)
}

/// `posixpath.join(a, b)`（b 為絕對路徑時取 b）。
fn pjoin(a: &[u8], b: &[u8]) -> Vec<u8> {
    if b.first() == Some(&b'/') {
        return b.to_vec();
    }
    let mut out = a.to_vec();
    if !out.is_empty() && out.last() != Some(&b'/') {
        out.push(b'/');
    }
    out.extend_from_slice(b);
    out
}

/// `posixpath.split(p)`。
fn psplit(p: &[u8]) -> (Vec<u8>, Vec<u8>) {
    let i = p.iter().rposition(|&c| c == b'/').map_or(0, |i| i + 1);
    let (mut head, tail) = (p[..i].to_vec(), p[i..].to_vec());
    if !head.is_empty() && head.iter().any(|&c| c != b'/') {
        while head.last() == Some(&b'/') {
            head.pop();
        }
    }
    (head, tail)
}

/// CPython 3.12 `posixpath._joinrealpath(path, rest, strict=False, seen)`。
fn join_realpath(
    mut path: Vec<u8>,
    rest: &[u8],
    seen: &mut std::collections::HashMap<Vec<u8>, Option<Vec<u8>>>,
) -> (Vec<u8>, bool) {
    use std::ffi::OsStr;
    use std::os::unix::ffi::OsStrExt;
    let mut rest: &[u8] = rest;
    if rest.first() == Some(&b'/') {
        rest = &rest[1..];
        path = b"/".to_vec();
    }
    while !rest.is_empty() {
        let (name, tail) = match rest.iter().position(|&c| c == b'/') {
            Some(i) => (&rest[..i], &rest[i + 1..]),
            None => (rest, &b""[..]),
        };
        rest = tail;
        if name.is_empty() || name == b"." {
            continue;
        }
        if name == b".." {
            if path.is_empty() {
                path = b"..".to_vec();
            } else {
                let (head, last) = psplit(&path);
                path = head;
                if last == b".." {
                    path = pjoin(&pjoin(&path, b".."), b"..");
                }
            }
            continue;
        }
        let newpath = pjoin(&path, name);
        let is_link = std::fs::symlink_metadata(OsStr::from_bytes(&newpath))
            .is_ok_and(|m| m.file_type().is_symlink());
        if !is_link {
            path = newpath;
            continue;
        }
        if let Some(cached) = seen.get(&newpath) {
            match cached {
                Some(p) => {
                    path = p.clone();
                    continue;
                }
                // symlink 迴圈：已解析部分＋其餘原樣。
                None => return (pjoin(&newpath, rest), false),
            }
        }
        seen.insert(newpath.clone(), None);
        let Ok(target) = std::fs::read_link(OsStr::from_bytes(&newpath)) else {
            return (pjoin(&newpath, rest), false);
        };
        let (resolved, ok) = join_realpath(path, target.as_os_str().as_bytes(), seen);
        if !ok {
            return (pjoin(&resolved, rest), false);
        }
        seen.insert(newpath, Some(resolved.clone()));
        path = resolved;
    }
    (path, true)
}

/// `os.path.realpath(strict=False)`：逐段解析 symlink（懸空連結亦依其內容展開），不存在的段原樣接上。
/// 呼叫端只傳絕對路徑（sysfs root 下），`abspath` 的 cwd 基底不適用。
fn realpath_lenient(path: &Path) -> PathBuf {
    use std::os::unix::ffi::{OsStrExt, OsStringExt};
    let (resolved, _) = join_realpath(
        Vec::new(),
        path.as_os_str().as_bytes(),
        &mut std::collections::HashMap::new(),
    );
    PathBuf::from(std::ffi::OsString::from_vec(resolved))
}

impl UsbTopology for SysfsUsbTopology {
    fn usb_busid(&self, real_path: &str) -> Option<String> {
        if real_path.is_empty() {
            return None;
        }
        // `os.path.basename`：最後一個 `/` 之後。
        let name = real_path.rsplit('/').next().unwrap_or("");
        if name.is_empty() {
            return None;
        }
        let link = self.root.join("class/tty").join(name).join("device");
        let resolved = realpath_lenient(&link);
        let text = resolved.to_string_lossy().into_owned();
        text.rsplit('/')
            .find(|seg| is_busid(seg))
            .map(str::to_string)
    }
}
