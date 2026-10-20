//! 記憶體內唯讀檔案系統。

use crate::traits::{Clock, FsRead, PalError, PalErrorKind, PalResult};
use std::collections::{BTreeMap, BTreeSet};
use std::path::{Path, PathBuf};
use std::sync::Mutex;

#[derive(Debug, Default)]
pub struct MemFs {
    pub files: Mutex<BTreeMap<PathBuf, Result<Vec<u8>, PalError>>>,
    pub dirs: Mutex<BTreeSet<PathBuf>>,
}

impl FsRead for MemFs {
    fn read(&self, path: &Path) -> PalResult<Vec<u8>> {
        self.files
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .get(path)
            .cloned()
            .unwrap_or_else(|| {
                Err(PalError::plain(
                    PalErrorKind::NotFound,
                    "No such file or directory",
                ))
            })
    }

    fn exists(&self, path: &Path) -> bool {
        self.files
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .contains_key(path)
            || self
                .dirs
                .lock()
                .unwrap_or_else(|e| e.into_inner())
                .contains(path)
    }

    fn is_dir(&self, path: &Path) -> bool {
        self.dirs
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .contains(path)
    }

    fn is_file(&self, path: &Path) -> bool {
        matches!(
            self.files
                .lock()
                .unwrap_or_else(|e| e.into_inner())
                .get(path),
            Some(Ok(_))
        )
    }

    fn list_dir(&self, path: &Path) -> PalResult<Vec<String>> {
        let files = self.files.lock().unwrap_or_else(|e| e.into_inner());
        if let Some(Err(error)) = files.get(path) {
            return Err(error.clone());
        }
        let dirs = self.dirs.lock().unwrap_or_else(|e| e.into_inner());
        if !dirs.contains(path) {
            return Err(PalError::plain(
                PalErrorKind::NotFound,
                "No such file or directory",
            ));
        }
        let mut names = BTreeSet::new();
        for child in files.keys().chain(dirs.iter()) {
            if child.parent() == Some(path) {
                if let Some(name) = child.file_name() {
                    names.insert(name.to_string_lossy().into_owned());
                }
            }
        }
        Ok(names.into_iter().collect())
    }
}

/// 故障注入的檔案系統包裝（P4-T1 替身）：可在 `replace`／`copy_tree` 指定位置注入失敗，其餘委派給內層。
pub struct FaultFs {
    inner: std::sync::Arc<dyn crate::fs_ops::FsOps>,
    replace_fault_suffix: Mutex<Option<String>>,
    replace_fault_contains: Mutex<Option<String>>,
    write_target_fault_suffix: Mutex<Option<String>>,
    copy_tree_fault: Mutex<bool>,
    lock_block: Mutex<Option<(
        PathBuf,
        std::sync::Arc<super::clock::FakeClock>,
        Option<u64>,
        Option<(PathBuf, Vec<u8>)>,
        bool,
    )>>,
}

impl std::fmt::Debug for FaultFs {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("FaultFs").finish_non_exhaustive()
    }
}

impl FaultFs {
    pub fn wrap(inner: std::sync::Arc<dyn crate::fs_ops::FsOps>) -> std::sync::Arc<Self> {
        std::sync::Arc::new(Self {
            inner,
            replace_fault_suffix: Mutex::new(None),
            replace_fault_contains: Mutex::new(None),
            write_target_fault_suffix: Mutex::new(None),
            copy_tree_fault: Mutex::new(false),
            lock_block: Mutex::new(None),
        })
    }

    /// `replace(from, to)` 的 `from` 以此後綴結尾時回 `Io` 錯誤。
    pub fn fail_replace_when_src_ends_with(&self, suffix: &str) {
        *self.replace_fault_suffix.lock().unwrap_or_else(|e| e.into_inner()) = Some(suffix.to_string());
    }

    /// `replace(from, to)` 的來源路徑含有此字串時回 `Io` 錯誤。
    pub fn fail_replace_when_src_contains(&self, part: &str) {
        *self.replace_fault_contains.lock().unwrap_or_else(|e| e.into_inner()) = Some(part.to_string());
    }

    /// 寫入目標以此後綴結尾時回 `Io` 錯誤：涵蓋 `atomic_write(p)`、`write_file(p)` 與 `replace(_, to)`
    /// （平台中立地模擬目標檔不可寫，取代以 `cfg(unix)` chmod 的作法）。
    pub fn fail_writes_to_suffix(&self, suffix: &str) {
        *self.write_target_fault_suffix.lock().unwrap_or_else(|e| e.into_inner()) = Some(suffix.to_string());
    }

    fn write_target_faulted(&self, target: &Path) -> bool {
        let fault = self.write_target_fault_suffix.lock().unwrap_or_else(|e| e.into_inner()).clone();
        matches!(fault, Some(suffix) if target.to_string_lossy().ends_with(&suffix))
    }

    /// 之後的 `copy_tree` 一律失敗。
    pub fn fail_copy_tree(&self) {
        *self.copy_tree_fault.lock().unwrap_or_else(|e| e.into_inner()) = true;
    }

    /// 模擬另一個行程持有鎖，直到假時鐘到達指定時間；`None` 表示一直持有。
    pub fn block_lock_until(
        &self,
        path: &Path,
        clock: std::sync::Arc<super::clock::FakeClock>,
        release_at_ns: Option<u64>,
    ) {
        *self.lock_block.lock().unwrap_or_else(|e| e.into_inner()) =
            Some((path.to_path_buf(), clock, release_at_ns, None, false));
    }

    /// 鎖於假時鐘到期釋放時，先建立指定檔案，供測試鎖內重查 journal 的競態。
    pub fn block_lock_until_and_create_file(
        &self,
        path: &Path,
        clock: std::sync::Arc<super::clock::FakeClock>,
        release_at_ns: u64,
        file: &Path,
        contents: &[u8],
    ) {
        *self.lock_block.lock().unwrap_or_else(|e| e.into_inner()) = Some((
            path.to_path_buf(),
            clock,
            Some(release_at_ns),
            Some((file.to_path_buf(), contents.to_vec())),
            false,
        ));
    }
}

impl FsRead for FaultFs {
    fn read(&self, p: &Path) -> PalResult<Vec<u8>> {
        self.inner.read(p)
    }
    fn exists(&self, p: &Path) -> bool {
        self.inner.exists(p)
    }
    fn is_dir(&self, p: &Path) -> bool {
        self.inner.is_dir(p)
    }
    fn is_file(&self, p: &Path) -> bool {
        self.inner.is_file(p)
    }
    fn list_dir(&self, p: &Path) -> PalResult<Vec<String>> {
        self.inner.list_dir(p)
    }
}

impl crate::fs_ops::FsOps for FaultFs {
    fn atomic_write(&self, p: &Path, data: &[u8], mode: Option<u32>) -> PalResult<()> {
        if self.write_target_faulted(p) {
            return Err(PalError::plain(PalErrorKind::Io, "fake write fault"));
        }
        self.inner.atomic_write(p, data, mode)
    }
    fn write_file(&self, p: &Path, data: &[u8]) -> PalResult<()> {
        if self.write_target_faulted(p) {
            return Err(PalError::plain(PalErrorKind::Io, "fake write fault"));
        }
        self.inner.write_file(p, data)
    }
    fn mkdirs(&self, p: &Path) -> PalResult<()> {
        self.inner.mkdirs(p)
    }
    fn replace(&self, from: &Path, to: &Path) -> PalResult<()> {
        let fault = self.replace_fault_suffix.lock().unwrap_or_else(|e| e.into_inner()).clone();
        let contains = self.replace_fault_contains.lock().unwrap_or_else(|e| e.into_inner()).clone();
        let from_text = from.to_string_lossy();
        if matches!(fault, Some(suffix) if from_text.ends_with(&suffix))
            || matches!(contains, Some(part) if from_text.contains(&part))
        {
            return Err(PalError::plain(PalErrorKind::Io, "fake replace fault"));
        }
        if self.write_target_faulted(to) {
            return Err(PalError::plain(PalErrorKind::Io, "fake write fault"));
        }
        self.inner.replace(from, to)
    }
    fn remove_file(&self, p: &Path) -> PalResult<()> {
        self.inner.remove_file(p)
    }
    fn copy_preserving(&self, from: &Path, to: &Path) -> PalResult<()> {
        self.inner.copy_preserving(from, to)
    }
    fn copy_tree(&self, from: &Path, to: &Path) -> PalResult<()> {
        if *self.copy_tree_fault.lock().unwrap_or_else(|e| e.into_inner()) {
            return Err(PalError::plain(PalErrorKind::Io, "fake copy_tree fault"));
        }
        self.inner.copy_tree(from, to)
    }
    fn remove_tree(&self, p: &Path) -> PalResult<()> {
        self.inner.remove_tree(p)
    }
    fn read_link(&self, p: &Path) -> PalResult<Option<std::path::PathBuf>> {
        self.inner.read_link(p)
    }
    fn set_mode(&self, p: &Path, mode: u32) -> PalResult<()> {
        self.inner.set_mode(p, mode)
    }
    fn sync_dir(&self, p: &Path) -> PalResult<()> {
        self.inner.sync_dir(p)
    }
    fn symlink_file(&self, link: &Path, target: &Path) -> PalResult<()> {
        self.inner.symlink_file(link, target)
    }
    fn symlink_or_copy_dir(&self, link: &Path, target: &Path) -> PalResult<crate::fs_ops::LinkKind> {
        self.inner.symlink_or_copy_dir(link, target)
    }
    fn file_lock_exclusive(&self, p: &Path, blocking: bool) -> PalResult<crate::fs_ops::FileLockGuard> {
        let mut lock_block = self.lock_block.lock().unwrap_or_else(|e| e.into_inner());
        if let Some((path, clock, release_at_ns, file, created)) = lock_block.as_mut() {
            if path == p && release_at_ns.is_none_or(|release| clock.mono_ns() < release) {
                return Err(PalError::plain(PalErrorKind::WouldBlock, "fake external lock holder"));
            }
            if path == p && !*created {
                if let Some((target, contents)) = file {
                    self.inner.write_file(target, contents)?;
                }
                *created = true;
            }
        }
        drop(lock_block);
        self.inner.file_lock_exclusive(p, blocking)
    }
}

impl crate::fs_ops::FsOps for MemFs {}
