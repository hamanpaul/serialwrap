//! `WalFs` 記憶體替身（feature `test-fakes`）。

use std::collections::{HashMap, HashSet, VecDeque};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};

use crate::traits::{FileIdentity, PalError, PalErrorKind, PalResult, WalFile, WalFs, WalWriteError};

#[derive(Clone)]
enum WriteFailure {
    Full(PalError),
    Partial(usize, PalError),
}

#[derive(Clone)]
struct FileNode {
    identity: FileIdentity,
    bytes: Arc<Mutex<Vec<u8>>>,
    unlinked: Arc<AtomicBool>,
    delete_pending: Arc<AtomicBool>,
    external_readers: Arc<AtomicUsize>,
}

#[derive(Default)]
struct FsInner {
    files: HashMap<PathBuf, FileNode>,
    next_inode: u128,
    write_failures: HashMap<PathBuf, VecDeque<WriteFailure>>,
    write_calls: HashMap<PathBuf, Vec<Vec<u8>>>,
    open_calls: HashMap<PathBuf, usize>,
    open_attempts: HashMap<PathBuf, usize>,
    sync_calls: HashMap<PathBuf, usize>,
    fail_opens: HashMap<PathBuf, VecDeque<PalError>>,
    fail_renames: VecDeque<PalError>,
    fail_sync_dirs: VecDeque<PalError>,
    fail_handle_syncs: HashMap<PathBuf, VecDeque<PalError>>,
    fail_identity: HashMap<PathBuf, PalError>,
    force_identity: HashMap<PathBuf, Option<FileIdentity>>,
    dir_writable: HashMap<PathBuf, bool>,
    dirs: HashSet<PathBuf>,
}

/// 外部 reader 的 handle；Drop 時釋放 pending-delete（Windows 語意）。
pub struct ExternalHandle {
    node: FileNode,
    path: PathBuf,
    fs: Arc<Mutex<FsInner>>,
}

impl Drop for ExternalHandle {
    fn drop(&mut self) {
        let prev = self.node.external_readers.fetch_sub(1, Ordering::SeqCst);
        if prev == 1 && self.node.delete_pending.load(Ordering::SeqCst) {
            let mut inner = self.fs.lock().unwrap_or_else(|e| e.into_inner());
            if let Some(entry) = inner.files.get(&self.path) {
                if entry.identity == self.node.identity {
                    inner.files.remove(&self.path);
                }
            }
        }
    }
}

pub struct FakeWalFs {
    inner: Arc<Mutex<FsInner>>,
}

impl FakeWalFs {
    pub fn new() -> Arc<FakeWalFs> {
        Arc::new(FakeWalFs {
            inner: Arc::new(Mutex::new(FsInner::default())),
        })
    }

    pub fn contents(&self, path: &Path) -> Option<Vec<u8>> {
        let inner = self.inner.lock().unwrap_or_else(|e| e.into_inner());
        inner.files.get(path).map(|node| {
            node.bytes.lock().unwrap_or_else(|e| e.into_inner()).clone()
        })
    }

    /// API 補充：預置檔案內容（啟動掃描與既有歸檔測試用）。
    pub fn seed(&self, path: &Path, bytes: &[u8]) {
        self.external_replace(path, bytes);
    }

    pub fn tail_needs_termination(&self, path: &Path) -> bool {
        let inner = self.inner.lock().unwrap_or_else(|e| e.into_inner());
        match inner.files.get(path) {
            None => false,
            Some(node) => {
                let data = node.bytes.lock().unwrap_or_else(|e| e.into_inner());
                !data.is_empty() && !data.ends_with(b"\n")
            }
        }
    }

    /// 排序後回傳目錄下所有路徑。
    pub fn list_dir(&self, dir: &Path) -> Vec<PathBuf> {
        let inner = self.inner.lock().unwrap_or_else(|e| e.into_inner());
        let mut entries = Vec::new();
        for p in inner.files.keys() {
            if let Some(parent) = p.parent() {
                if parent == dir {
                    entries.push(p.clone());
                }
            }
        }
        entries.sort();
        entries
    }

    /// POSIX 語意：路徑消失、既有 handle 存活、`unlinked()` 為 true。
    pub fn external_unlink(&self, path: &Path) {
        let mut inner = self.inner.lock().unwrap_or_else(|e| e.into_inner());
        if let Some(node) = inner.files.get(path) {
            node.unlinked.store(true, Ordering::SeqCst);
            if node.external_readers.load(Ordering::SeqCst) > 0 {
                node.delete_pending.store(true, Ordering::SeqCst);
            } else {
                inner.files.remove(path);
            }
        }
    }

    /// 就地截短（identity 不變；外部截短現行檔，OD-19 #88 的「檔案已低於門檻」情境）。
    pub fn external_truncate(&self, path: &Path, len: usize) {
        let inner = self.inner.lock().unwrap_or_else(|e| e.into_inner());
        if let Some(node) = inner.files.get(path) {
            node.bytes.lock().unwrap_or_else(|e| e.into_inner()).truncate(len);
        }
    }

    /// 新 identity 佔用路徑。
    pub fn external_replace(&self, path: &Path, bytes: &[u8]) {
        let mut inner = self.inner.lock().unwrap_or_else(|e| e.into_inner());
        if let Some(old) = inner.files.get(path) {
            old.unlinked.store(true, Ordering::SeqCst);
        }
        inner.next_inode += 1;
        let node = FileNode {
            identity: FileIdentity(1, inner.next_inode),
            bytes: Arc::new(Mutex::new(bytes.to_vec())),
            unlinked: Arc::new(AtomicBool::new(false)),
            delete_pending: Arc::new(AtomicBool::new(false)),
            external_readers: Arc::new(AtomicUsize::new(0)),
        };
        inner.files.insert(path.to_path_buf(), node);
    }

    /// Windows pending-delete 語意：unlink 後路徑保留到 handle drop。
    pub fn hold_external_reader(&self, path: &Path) -> ExternalHandle {
        let mut inner = self.inner.lock().unwrap_or_else(|e| e.into_inner());
        let node = if let Some(existing) = inner.files.get(path) {
            existing.clone()
        } else {
            inner.next_inode += 1;
            let node = FileNode {
                identity: FileIdentity(1, inner.next_inode),
                bytes: Arc::new(Mutex::new(Vec::new())),
                unlinked: Arc::new(AtomicBool::new(false)),
                delete_pending: Arc::new(AtomicBool::new(false)),
                external_readers: Arc::new(AtomicUsize::new(0)),
            };
            inner.files.insert(path.to_path_buf(), node.clone());
            node
        };
        node.external_readers.fetch_add(1, Ordering::SeqCst);
        ExternalHandle {
            node,
            path: path.to_path_buf(),
            fs: Arc::clone(&self.inner),
        }
    }

    /// 接下來 `n` 次 `write_all_flush` 不寫任何 byte，回 `sent:0`。
    pub fn fail_next_writes(&self, path: &Path, n: usize, err: PalError) {
        let mut inner = self.inner.lock().unwrap_or_else(|e| e.into_inner());
        let q = inner.write_failures.entry(path.to_path_buf()).or_default();
        for _ in 0..n {
            q.push_back(WriteFailure::Full(err.clone()));
        }
    }

    /// 下一次 `write_all_flush` 只附加前 `min(accept, len-1)` bytes，回 `sent` 為實際附加數。
    pub fn fail_next_write_partial(&self, path: &Path, accept: usize, err: PalError) {
        let mut inner = self.inner.lock().unwrap_or_else(|e| e.into_inner());
        inner
            .write_failures
            .entry(path.to_path_buf())
            .or_default()
            .push_back(WriteFailure::Partial(accept, err));
    }

    /// 依序記錄每次 `write_all_flush` 收到的 bytes。
    pub fn write_calls(&self, path: &Path) -> Vec<Vec<u8>> {
        let inner = self.inner.lock().unwrap_or_else(|e| e.into_inner());
        inner
            .write_calls
            .get(path)
            .cloned()
            .unwrap_or_default()
    }

    /// `open_append` 成功次數。
    pub fn open_calls(&self, path: &Path) -> usize {
        let inner = self.inner.lock().unwrap_or_else(|e| e.into_inner());
        inner.open_calls.get(path).copied().unwrap_or(0)
    }

    /// API 補充：`open_append` 嘗試次數（含失敗），供重試排程斷言。
    pub fn open_attempts(&self, path: &Path) -> usize {
        let inner = self.inner.lock().unwrap_or_else(|e| e.into_inner());
        inner.open_attempts.get(path).copied().unwrap_or(0)
    }

    pub fn fail_next_renames(&self, n: usize, err: PalError) {
        let mut inner = self.inner.lock().unwrap_or_else(|e| e.into_inner());
        for _ in 0..n {
            inner.fail_renames.push_back(err.clone());
        }
    }

    /// 下 `n` 次 `sync_dir` 失敗（OD-19：輪替後的目錄同步錯誤亦計入 `rotate_retry`）。
    pub fn fail_next_sync_dirs(&self, n: usize, err: PalError) {
        let mut inner = self.inner.lock().unwrap_or_else(|e| e.into_inner());
        for _ in 0..n {
            inner.fail_sync_dirs.push_back(err.clone());
        }
    }

    /// `path` 的 handle 下 `n` 次 `sync` 失敗（輪替前 close(sync=True) 的錯誤路徑）。
    pub fn fail_next_handle_syncs(&self, path: &Path, n: usize, err: PalError) {
        let mut inner = self.inner.lock().unwrap_or_else(|e| e.into_inner());
        let queue = inner.fail_handle_syncs.entry(path.to_path_buf()).or_default();
        for _ in 0..n {
            queue.push_back(err.clone());
        }
    }

    pub fn fail_next_opens(&self, path: &Path, n: usize, err: PalError) {
        let mut inner = self.inner.lock().unwrap_or_else(|e| e.into_inner());
        let q = inner.fail_opens.entry(path.to_path_buf()).or_default();
        for _ in 0..n {
            q.push_back(err.clone());
        }
    }

    /// API 補充（持續性）：之後所有 `path_identity` 查詢回 `err`，直到替身重置。
    pub fn fail_identity_queries(&self, path: &Path, err: PalError) {
        let mut inner = self.inner.lock().unwrap_or_else(|e| e.into_inner());
        inner.fail_identity.insert(path.to_path_buf(), err);
    }

    /// API 補充：強制 `path_identity` 回傳指定值（`None` 表示路徑不存在），用於 `(0,0)` sentinel 測試。
    pub fn force_path_identity(&self, path: &Path, id: Option<FileIdentity>) {
        let mut inner = self.inner.lock().unwrap_or_else(|e| e.into_inner());
        inner.force_identity.insert(path.to_path_buf(), id);
    }

    pub fn sync_calls(&self, path: &Path) -> usize {
        let inner = self.inner.lock().unwrap_or_else(|e| e.into_inner());
        inner.sync_calls.get(path).copied().unwrap_or(0)
    }

    pub fn set_dir_writable(&self, dir: &Path, writable: bool) {
        let mut inner = self.inner.lock().unwrap_or_else(|e| e.into_inner());
        inner.dir_writable.insert(dir.to_path_buf(), writable);
    }
}

struct FakeWalFile {
    path: PathBuf,
    node: FileNode,
    fs: Arc<Mutex<FsInner>>,
}

impl WalFile for FakeWalFile {
    fn write_all_flush(&mut self, bytes: &[u8]) -> Result<(), WalWriteError> {
        let mut inner = self.fs.lock().unwrap_or_else(|e| e.into_inner());
        inner
            .write_calls
            .entry(self.path.clone())
            .or_default()
            .push(bytes.to_vec());
        let failure = inner
            .write_failures
            .get_mut(&self.path)
            .and_then(|q| q.pop_front());
        drop(inner);

        match failure {
            Some(WriteFailure::Full(err)) => Err(WalWriteError {
                sent: 0,
                error: err,
            }),
            Some(WriteFailure::Partial(accept, err)) => {
                let sent = accept.min(bytes.len().saturating_sub(1));
                let mut data = self.node.bytes.lock().unwrap_or_else(|e| e.into_inner());
                data.extend_from_slice(&bytes[..sent]);
                Err(WalWriteError { sent, error: err })
            }
            None => {
                let mut data = self.node.bytes.lock().unwrap_or_else(|e| e.into_inner());
                data.extend_from_slice(bytes);
                Ok(())
            }
        }
    }

    fn sync(&mut self) -> PalResult<()> {
        let mut inner = self.fs.lock().unwrap_or_else(|e| e.into_inner());
        *inner.sync_calls.entry(self.path.clone()).or_default() += 1;
        if let Some(err) = inner
            .fail_handle_syncs
            .get_mut(&self.path)
            .and_then(VecDeque::pop_front)
        {
            return Err(err);
        }
        Ok(())
    }

    fn identity(&self) -> PalResult<FileIdentity> {
        Ok(self.node.identity.clone())
    }

    fn unlinked(&self) -> PalResult<bool> {
        Ok(self.node.unlinked.load(Ordering::SeqCst))
    }

    fn close(self: Box<Self>) -> PalResult<()> {
        Ok(())
    }
}

impl WalFs for FakeWalFs {
    fn read_all(&self, path: &Path) -> PalResult<Option<Vec<u8>>> {
        Ok(self.contents(path))
    }

    fn open_append(&self, path: &Path) -> PalResult<Box<dyn WalFile>> {
        let mut inner = self.inner.lock().unwrap_or_else(|e| e.into_inner());
        *inner.open_attempts.entry(path.to_path_buf()).or_default() += 1;
        if let Some(err) = inner
            .fail_opens
            .get_mut(path)
            .and_then(|q| q.pop_front())
        {
            return Err(err);
        }
        if let Some(parent) = path.parent() {
            if inner.dir_writable.get(parent) == Some(&false) && !inner.files.contains_key(path) {
                return Err(PalError::io(13, "[Errno 13] Permission denied"));
            }
        }
        if let Some(existing) = inner.files.get(path) {
            if existing.delete_pending.load(Ordering::SeqCst) {
                return Err(PalError::new(
                    PalErrorKind::AccessDenied,
                    Some(13),
                    Some(5),
                    "Access is denied",
                    "[WinError 5] Access is denied",
                ));
            }
            let node = existing.clone();
            *inner.open_calls.entry(path.to_path_buf()).or_default() += 1;
            return Ok(Box::new(FakeWalFile {
                path: path.to_path_buf(),
                node,
                fs: Arc::clone(&self.inner),
            }));
        }
        inner.next_inode += 1;
        let node = FileNode {
            identity: FileIdentity(1, inner.next_inode),
            bytes: Arc::new(Mutex::new(Vec::new())),
            unlinked: Arc::new(AtomicBool::new(false)),
            delete_pending: Arc::new(AtomicBool::new(false)),
            external_readers: Arc::new(AtomicUsize::new(0)),
        };
        inner.files.insert(path.to_path_buf(), node.clone());
        *inner.open_calls.entry(path.to_path_buf()).or_default() += 1;
        Ok(Box::new(FakeWalFile {
            path: path.to_path_buf(),
            node,
            fs: Arc::clone(&self.inner),
        }))
    }

    fn path_identity(&self, path: &Path) -> PalResult<Option<FileIdentity>> {
        let inner = self.inner.lock().unwrap_or_else(|e| e.into_inner());
        if let Some(err) = inner.fail_identity.get(path) {
            return Err(err.clone());
        }
        if let Some(forced) = inner.force_identity.get(path) {
            return Ok(forced.clone());
        }
        match inner.files.get(path) {
            Some(node) => Ok(Some(node.identity.clone())),
            None => Ok(None),
        }
    }

    fn size(&self, path: &Path) -> PalResult<Option<u64>> {
        let inner = self.inner.lock().unwrap_or_else(|e| e.into_inner());
        Ok(inner.files.get(path).map(|node| {
            let data = node.bytes.lock().unwrap_or_else(|e| e.into_inner());
            data.len() as u64
        }))
    }

    fn tail_needs_termination(&self, path: &Path) -> PalResult<bool> {
        Ok(FakeWalFs::tail_needs_termination(self, path))
    }

    fn rename_noreplace_or_suffix(&self, from: &Path, base_to: &Path) -> PalResult<PathBuf> {
        let mut inner = self.inner.lock().unwrap_or_else(|e| e.into_inner());
        if let Some(err) = inner.fail_renames.pop_front() {
            return Err(err);
        }
        if let Some(parent) = from.parent() {
            if inner.dir_writable.get(parent) == Some(&false) {
                return Err(PalError::io(13, "[Errno 13] Permission denied"));
            }
        }
        if let Some(parent) = base_to.parent() {
            if inner.dir_writable.get(parent) == Some(&false) {
                return Err(PalError::io(13, "[Errno 13] Permission denied"));
            }
        }
        let node = inner.files.remove(from).ok_or_else(|| {
            PalError::not_found("[Errno 2] No such file or directory")
        })?;

        let dest = if !inner.files.contains_key(base_to) {
            base_to.to_path_buf()
        } else {
            let base_str = base_to.to_string_lossy();
            let mut idx = 1;
            loop {
                let candidate = PathBuf::from(format!("{base_str}.{idx}"));
                if !inner.files.contains_key(&candidate) {
                    break candidate;
                }
                idx += 1;
            }
        };

        inner.files.insert(dest.clone(), node);
        Ok(dest)
    }

    fn sync_dir(&self, dir: &Path) -> PalResult<()> {
        let mut inner = self.inner.lock().unwrap_or_else(|e| e.into_inner());
        *inner.sync_calls.entry(dir.to_path_buf()).or_default() += 1;
        if let Some(err) = inner.fail_sync_dirs.pop_front() {
            return Err(err);
        }
        Ok(())
    }

    fn ensure_dir(&self, dir: &Path) -> PalResult<()> {
        let mut inner = self.inner.lock().unwrap_or_else(|e| e.into_inner());
        inner.dirs.insert(dir.to_path_buf());
        Ok(())
    }

    fn dir_writable(&self, dir: &Path) -> bool {
        let inner = self.inner.lock().unwrap_or_else(|e| e.into_inner());
        if inner.files.contains_key(dir) {
            return false;
        }
        inner
            .dir_writable
            .get(dir)
            .copied()
            .unwrap_or_else(|| inner.dirs.contains(dir))
    }
}
