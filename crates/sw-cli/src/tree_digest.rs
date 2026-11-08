//! 共用 Python 樹狀摘要遍歷與彙總，相容 P1 `tree_sha256` golden。

use std::fs;
use std::path::{Path, PathBuf};

use sw_pal::fs_ops::FsOps;
use sw_proto::digest::sha256_hex;

/// 樹遍歷所需的最小檔案系統介面。
pub(crate) trait TreeSource {
    fn list_dir(&self, path: &Path) -> Result<Vec<String>, String>;
    fn is_dir(&self, path: &Path) -> bool;
    fn is_symlink(&self, path: &Path) -> Result<bool, String>;
    fn read(&self, path: &Path) -> Result<Vec<u8>, String>;
}

/// 使用 `FsOps` 的產品與測試路徑。
pub(crate) struct PalTreeSource<'a>(pub &'a dyn FsOps);

impl TreeSource for PalTreeSource<'_> {
    fn list_dir(&self, path: &Path) -> Result<Vec<String>, String> {
        self.0
            .list_dir(path)
            .map_err(|err| format!("{}: {}", path.display(), pal_message(&err)))
    }

    fn is_dir(&self, path: &Path) -> bool {
        self.0.is_dir(path)
    }

    fn is_symlink(&self, path: &Path) -> Result<bool, String> {
        self.0
            .read_link(path)
            .map(|target| target.is_some())
            .map_err(|err| format!("{}: {}", path.display(), pal_message(&err)))
    }

    fn read(&self, path: &Path) -> Result<Vec<u8>, String> {
        self.0
            .read(path)
            .map_err(|err| format!("{}: {}", path.display(), pal_message(&err)))
    }
}

/// `pairing_item` 使用原生讀取，但共用與 bundle 驗證相同的遍歷規則。
pub(crate) struct NativeTreeSource;

impl TreeSource for NativeTreeSource {
    fn list_dir(&self, path: &Path) -> Result<Vec<String>, String> {
        fs::read_dir(path)
            .map_err(|err| format!("{}: {err}", path.display()))?
            .map(|entry| {
                entry
                    .map(|entry| entry.file_name().to_string_lossy().into_owned())
                    .map_err(|err| format!("{}: {err}", path.display()))
            })
            .collect()
    }

    fn is_dir(&self, path: &Path) -> bool {
        path.is_dir()
    }

    fn is_symlink(&self, path: &Path) -> Result<bool, String> {
        fs::symlink_metadata(path)
            .map(|metadata| metadata.file_type().is_symlink())
            .map_err(|err| format!("{}: {err}", path.display()))
    }

    fn read(&self, path: &Path) -> Result<Vec<u8>, String> {
        fs::read(path).map_err(|err| format!("{}: {err}", path.display()))
    }
}

/// 依相對路徑位元組序彙總 relpath、NUL、檔案 SHA-256 與換行。
pub(crate) fn digest_file_pairs(mut files: Vec<(String, String)>) -> String {
    files.sort_by(|a, b| a.0.as_bytes().cmp(b.0.as_bytes()));
    let mut material = String::new();
    for (relative, digest) in files {
        material.push_str(&format!("{relative}\0{digest}\n"));
    }
    sha256_hex(material.as_bytes())
}

/// 共用列舉器；package hash 排除 Python bytecode，SHA256SUMS 呼叫端則保留全部檔案。
pub(crate) fn collect_file_paths<S: TreeSource>(
    source: &S,
    root: &Path,
    rel_root: &str,
    exclude_python_cache: bool,
) -> Result<Vec<(String, PathBuf)>, String> {
    let mut files = Vec::new();
    collect_into(source, root, rel_root, exclude_python_cache, &mut files)?;
    Ok(files)
}

fn collect_into<S: TreeSource>(
    source: &S,
    root: &Path,
    rel_root: &str,
    exclude_python_cache: bool,
    out: &mut Vec<(String, PathBuf)>,
) -> Result<(), String> {
    let mut names = source.list_dir(root)?;
    names.sort_by(|a, b| a.as_bytes().cmp(b.as_bytes()));
    for name in names {
        let path = root.join(&name);
        let is_symlink = source.is_symlink(&path)?;
        let is_dir = source.is_dir(&path);
        if is_symlink && is_dir {
            if exclude_python_cache {
                continue;
            }
            return Err(format!("{}: bundle 不得包含 symlink 目錄", path.display()));
        }
        if is_dir {
            if !exclude_python_cache || name != "__pycache__" {
                collect_into(
                    source,
                    &path,
                    &format!("{rel_root}{name}/"),
                    exclude_python_cache,
                    out,
                )?;
            }
        } else if !exclude_python_cache || !name.ends_with(".pyc") {
            out.push((format!("{rel_root}{name}"), path));
        }
    }
    Ok(())
}

/// 依共用遍歷規則計算樹摘要。
pub(crate) fn digest_tree<S: TreeSource>(
    source: &S,
    root: &Path,
    rel_root: &str,
    exclude_python_cache: bool,
) -> Result<String, String> {
    let files = collect_file_paths(source, root, rel_root, exclude_python_cache)?;
    let mut digests = Vec::with_capacity(files.len());
    for (relative, path) in files {
        digests.push((relative, sha256_hex(&source.read(&path)?)));
    }
    Ok(digest_file_pairs(digests))
}

fn pal_message(err: &sw_pal::PalError) -> String {
    if err.py_message.is_empty() {
        err.strerror.clone()
    } else {
        err.py_message.clone()
    }
}
