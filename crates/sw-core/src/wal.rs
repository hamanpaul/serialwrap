//! Rust WAL 寫入器（BC-01、DF-03、DF-17）。

use std::collections::{HashSet, VecDeque};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex, OnceLock, RwLock, Weak};

use sw_pal::{
    Clock, FileIdentity, PalError, PalErrorKind, PalResult, WalFile, WalFs, WalWriteError,
};
use sw_proto::pycompat::{
    a2b_base64_lenient, b2a_base64, decode_utf8_replace, iso_utc_now, py_strip, splitlines,
    strftime_utc, to_printable,
};
use sw_proto::pyjson::{loads, PyDict, PyInt, PyValue};
use sw_proto::wal_types::{crc32_hex, wal_recreated_meta, WalRecord};

const IDENTITY_CHECK_INTERVAL_NS: u64 = 50_000_000;
const RECREATE_BACKOFF_NS: [u64; 3] = [10_000_000, 50_000_000, 200_000_000];
/// `wal.py` `_is_transient_rotate_error`：共用違規或存取拒絕（POSIX EACCES／EBUSY、Windows winerror 5／32）。
fn is_transient_rotate_error(error: &PalError) -> bool {
    matches!(error.os_code, Some(13) | Some(16)) || matches!(error.winerror, Some(5) | Some(32))
}

const RECREATE_RETRY_INTERVALS_NS: [u64; 3] = [1_000_000_000, 2_000_000_000, 5_000_000_000];

pub struct WalConfig {
    pub dir: PathBuf,
    pub rotate_bytes: u64,
}

/// 全域 WAL 健康狀態的接收端。
pub trait WalHealthSink: Send + Sync {
    fn set_wal_unhealthy(&self, unhealthy: bool);
}

#[derive(Default)]
struct FanoutState {
    unhealthy: bool,
    sinks: Vec<Weak<dyn WalHealthSink>>,
}

/// 全域唯一 sink；晚註冊的 session 會立即繼承目前狀態。
pub struct WalHealthFanout {
    inner: Mutex<FanoutState>,
    /// 串行化註冊初始化與狀態通知，避免較舊快照覆蓋新狀態。
    dispatch: Mutex<()>,
}

impl WalHealthFanout {
    pub fn new() -> Arc<WalHealthFanout> {
        Arc::new(WalHealthFanout {
            inner: Mutex::new(FanoutState::default()),
            dispatch: Mutex::new(()),
        })
    }

    pub fn register(&self, sink: Arc<dyn WalHealthSink>) {
        let _dispatch = self.dispatch.lock().unwrap_or_else(|e| e.into_inner());
        let unhealthy = {
            let mut state = self.inner.lock().unwrap_or_else(|e| e.into_inner());
            state.sinks.retain(|entry| entry.strong_count() > 0);
            state.sinks.push(Arc::downgrade(&sink));
            state.unhealthy
        };
        sink.set_wal_unhealthy(unhealthy);
    }

    /// 回傳目前是否不健康。
    pub fn current(&self) -> bool {
        self.inner
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .unhealthy
    }
}

impl WalHealthSink for WalHealthFanout {
    fn set_wal_unhealthy(&self, unhealthy: bool) {
        let _dispatch = self.dispatch.lock().unwrap_or_else(|e| e.into_inner());
        let sinks = {
            let mut state = self.inner.lock().unwrap_or_else(|e| e.into_inner());
            if state.unhealthy == unhealthy {
                return;
            }
            state.unhealthy = unhealthy;
            let sinks = state
                .sinks
                .iter()
                .filter_map(Weak::upgrade)
                .collect::<Vec<_>>();
            state.sinks.retain(|entry| entry.strong_count() > 0);
            sinks
        };
        for sink in sinks {
            sink.set_wal_unhealthy(unhealthy);
        }
    }
}

pub struct WalAppendReq<'a> {
    pub com: &'a str,
    pub dir: &'a str,
    pub source: &'a str,
    pub payload: &'a [u8],
    pub cmd_id: Option<&'a str>,
    pub loss_flag: bool,
    pub meta: Option<PyDict>,
}

#[derive(Debug, Clone, PartialEq)]
pub struct WalAppend {
    pub seq: u64,
    pub ok: bool,
    pub loss_flag: bool,
}

#[derive(Clone)]
pub struct WalHealth {
    pub wal_dir: String,
    pub wal_path: String,
    pub wal_dir_exists: bool,
    pub wal_file_exists: bool,
    pub wal_dir_writable: bool,
    pub current_seq: u64,
    pub write_failures: u64,
    pub last_write_error: Option<String>,
    pub recreated_count: u64,
    /// OD-19：暫時性輪替失敗（EACCES／EBUSY、Windows winerror 5／32）累計次數（`wal.py` `_rotate_retry_count`）。
    pub rotate_retry: u64,
    /// OD-19：轉入延遲佇列的次數（Rust 尚無延遲佇列，恆 0；parity-amendments-2026-10-26 #61 只承接鍵集合）。
    pub rotate_deferred: u64,
    /// OD-19：延遲佇列中待輪替的檔案數（同上，恆 0）。
    pub deferred_delete_pending: u64,
    pub healthy: bool,
    /// `"recreate_failed"`、`"write_failed"`、`"dir_unwritable"` 或 `None`。
    pub unhealthy_reason: Option<&'static str>,
}

impl WalHealth {
    /// 依 Python 基線的欄位順序建立字典。
    pub fn to_pydict(&self) -> PyDict {
        let mut dict = PyDict::new();
        dict.insert("wal_dir", PyValue::Str(self.wal_dir.clone()));
        dict.insert("wal_path", PyValue::Str(self.wal_path.clone()));
        dict.insert("wal_dir_exists", PyValue::Bool(self.wal_dir_exists));
        dict.insert("wal_file_exists", PyValue::Bool(self.wal_file_exists));
        dict.insert("wal_dir_writable", PyValue::Bool(self.wal_dir_writable));
        dict.insert(
            "current_seq",
            PyValue::Int(PyInt::Small(i128::from(self.current_seq))),
        );
        dict.insert(
            "write_failures",
            PyValue::Int(PyInt::Small(i128::from(self.write_failures))),
        );
        dict.insert(
            "last_write_error",
            self.last_write_error
                .clone()
                .map_or(PyValue::Null, PyValue::Str),
        );
        dict.insert(
            "recreated_count",
            PyValue::Int(PyInt::Small(i128::from(self.recreated_count))),
        );
        // wal.py:861-914 的 OD-19 三鍵，位於 recreated_count 之後（parity-amendments-2026-10-26 #61）。
        for (key, value) in [
            ("rotate_retry", self.rotate_retry),
            ("rotate_deferred", self.rotate_deferred),
            ("deferred_delete_pending", self.deferred_delete_pending),
        ] {
            dict.insert(key, PyValue::Int(PyInt::Small(i128::from(value))));
        }
        dict.insert("healthy", PyValue::Bool(self.healthy));
        dict.insert(
            "unhealthy_reason",
            self.unhealthy_reason
                .map_or(PyValue::Null, |reason| PyValue::Str(reason.to_string())),
        );
        dict
    }
}

pub struct WalReset {
    pub previous_seq: u64,
    /// WAL 實際使用的歸檔後綴；碰撞時包含 `.N`。
    pub rotated_suffix: String,
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum Target {
    Wal,
    Mirror,
}

struct PendingRecreate {
    reason: &'static str,
    previous_identity: Option<String>,
    file_first_seq: Option<u64>,
    warning_cause: Option<String>,
    recreated_counted: bool,
}

struct WalInner {
    seq: u64,
    startup_read_failed: bool,
    wal_file: Option<Box<dyn WalFile>>,
    mirror_file: Option<Box<dyn WalFile>>,
    wal_size: Option<u64>,
    mirror_size: Option<u64>,
    wal_path_expected: bool,
    file_first_seq: Option<u64>,
    file_first_seq_pending: bool,
    wal_identity: Option<FileIdentity>,
    last_identity_check_ns: Option<u64>,
    dirty_wal_tail: bool,
    dirty_mirror_tail: bool,
    pending_recreate: Option<PendingRecreate>,
    recreate_failures: usize,
    next_recreate_at_ns: Option<u64>,
    write_failures: u64,
    last_write_error: Option<String>,
    recreated_count: u64,
    rotate_retry: u64,
    unhealthy_reason: Option<&'static str>,
    dir_exists: bool,
    closed: bool,
}

impl WalInner {
    fn file(&self, target: Target) -> &Option<Box<dyn WalFile>> {
        match target {
            Target::Wal => &self.wal_file,
            Target::Mirror => &self.mirror_file,
        }
    }

    fn file_mut(&mut self, target: Target) -> &mut Option<Box<dyn WalFile>> {
        match target {
            Target::Wal => &mut self.wal_file,
            Target::Mirror => &mut self.mirror_file,
        }
    }

    fn size(&self, target: Target) -> Option<u64> {
        match target {
            Target::Wal => self.wal_size,
            Target::Mirror => self.mirror_size,
        }
    }

    fn set_size(&mut self, target: Target, size: Option<u64>) {
        match target {
            Target::Wal => self.wal_size = size,
            Target::Mirror => self.mirror_size = size,
        }
    }

    fn dirty_tail(&self, target: Target) -> bool {
        match target {
            Target::Wal => self.dirty_wal_tail,
            Target::Mirror => self.dirty_mirror_tail,
        }
    }

    fn set_dirty_tail(&mut self, target: Target, dirty: bool) {
        match target {
            Target::Wal => self.dirty_wal_tail = dirty,
            Target::Mirror => self.dirty_mirror_tail = dirty,
        }
    }
}

struct SizeSnapshot {
    wal: Option<u64>,
    mirror: Option<u64>,
    wal_error: bool,
    mirror_error: bool,
}

impl SizeSnapshot {
    fn get(&self, target: Target) -> Option<u64> {
        match target {
            Target::Wal => self.wal,
            Target::Mirror => self.mirror,
        }
    }

    fn errored(&self, target: Target) -> bool {
        match target {
            Target::Wal => self.wal_error,
            Target::Mirror => self.mirror_error,
        }
    }
}

/// 以常駐檔案 handle 寫入權威 WAL 與 best-effort mirror。
pub struct WalWriter {
    dir: PathBuf,
    dir_identity_known: AtomicBool,
    wal_path: PathBuf,
    mirror_path: PathBuf,
    rotate_bytes: u64,
    fs: Arc<dyn WalFs>,
    clock: Arc<dyn Clock>,
    inner: Mutex<WalInner>,
    seq: AtomicU64,
    health_snapshot: RwLock<WalHealth>,
    health_sink: OnceLock<Arc<WalHealthFanout>>,
    warn_sink: RwLock<Arc<dyn Fn(&str) + Send + Sync>>,
}

impl WalWriter {
    /// 建立目錄並載入現行檔序號；檔案 handle 延至首次寫入才開啟。
    pub fn open(cfg: WalConfig, fs: Arc<dyn WalFs>, clock: Arc<dyn Clock>) -> WalWriter {
        let dir_result = fs.ensure_dir(&cfg.dir);
        let dir_writable = fs.dir_writable(&cfg.dir);
        let wal_path = cfg.dir.join("raw.wal.ndjson");
        let mirror_path = cfg.dir.join("raw.mirror.log");
        let initial_wal_result = fs.read_all(&wal_path);
        let startup_read_error = initial_wal_result.as_ref().err().cloned();
        let initial_wal = initial_wal_result.ok().flatten();
        let dir_identity_known = fs.path_identity(&cfg.dir).ok().flatten().is_some();
        let (seq, file_first_seq, first_nonempty_seen, sequence_overflow) = initial_wal
            .as_deref()
            .map(scan_seq_bounds)
            .unwrap_or((0, None, false, false));
        let wal_size = fs.size(&wal_path).ok().flatten();
        let mirror_size = fs.size(&mirror_path).ok().flatten();
        let wal_identity = fs.path_identity(&wal_path).ok().flatten();
        let wal_file_exists = initial_wal.is_some()
            || (startup_read_error.is_some() && (wal_size.is_some() || wal_identity.is_some()));
        let dir_exists = dir_result.is_ok() || dir_writable || wal_file_exists;
        let unhealthy_reason = if !dir_writable {
            Some("dir_unwritable")
        } else if startup_read_error.is_some() || sequence_overflow {
            Some("write_failed")
        } else {
            None
        };
        let last_write_error = startup_read_error
            .as_ref()
            .map(write_error_text)
            .or_else(|| {
                sequence_overflow.then(|| "OverflowError: WAL sequence exceeds u64".into())
            });

        let writer = WalWriter {
            dir: cfg.dir.clone(),
            dir_identity_known: AtomicBool::new(dir_identity_known),
            wal_path: wal_path.clone(),
            mirror_path,
            rotate_bytes: cfg.rotate_bytes,
            fs,
            clock,
            inner: Mutex::new(WalInner {
                seq,
                startup_read_failed: startup_read_error.is_some(),
                wal_file: None,
                mirror_file: None,
                wal_size,
                mirror_size,
                wal_path_expected: wal_file_exists,
                file_first_seq,
                file_first_seq_pending: !first_nonempty_seen,
                wal_identity,
                last_identity_check_ns: None,
                dirty_wal_tail: initial_wal
                    .as_deref()
                    .is_some_and(|bytes| !bytes.is_empty() && !bytes.ends_with(b"\n")),
                dirty_mirror_tail: false,
                pending_recreate: None,
                recreate_failures: 0,
                next_recreate_at_ns: None,
                write_failures: 0,
                last_write_error: last_write_error.clone(),
                recreated_count: 0,
                rotate_retry: 0,
                unhealthy_reason,
                dir_exists,
                closed: false,
            }),
            seq: AtomicU64::new(seq),
            health_snapshot: RwLock::new(WalHealth {
                wal_dir: cfg.dir.to_string_lossy().into_owned(),
                wal_path: wal_path.to_string_lossy().into_owned(),
                wal_dir_exists: dir_exists,
                wal_file_exists,
                wal_dir_writable: dir_writable,
                current_seq: seq,
                write_failures: 0,
                last_write_error,
                recreated_count: 0,
                rotate_retry: 0,
                rotate_deferred: 0,
                deferred_delete_pending: 0,
                healthy: dir_exists && dir_writable && unhealthy_reason.is_none(),
                unhealthy_reason,
            }),
            health_sink: OnceLock::new(),
            warn_sink: RwLock::new(Arc::new(|message| eprintln!("{message}"))),
        };
        {
            let inner = writer.inner.lock().unwrap_or_else(|e| e.into_inner());
            writer.refresh_health_locked(&inner);
        }
        if let Some(error) = startup_read_error {
            writer.warn(&format!(
                "WAL append 寫入失敗（best-effort 略過）：{}",
                error.py_message
            ));
        } else if sequence_overflow {
            writer.warn("WAL append 寫入失敗（best-effort 略過）：WAL sequence exceeds u64");
        }
        writer
    }

    /// 設定唯一的 session health fanout。
    pub fn set_health_sink(&self, sink: Arc<WalHealthFanout>) {
        if self.health_sink.set(sink).is_err() {
            return;
        }
        let inner = self.inner.lock().unwrap_or_else(|e| e.into_inner());
        self.refresh_health_locked(&inner);
    }

    /// 設定 WAL WARNING 輸出端；預設寫至 stderr。
    pub fn set_warn_sink(&self, sink: Arc<dyn Fn(&str) + Send + Sync>) {
        *self.warn_sink.write().unwrap_or_else(|e| e.into_inner()) = sink;
    }

    /// 追加一筆 WAL；失敗時仍消耗序號，但不保留待寫資料。
    pub fn append(&self, req: WalAppendReq<'_>) -> WalAppend {
        let payload_b64 = b2a_base64(req.payload);
        let crc = crc32_hex(req.payload);
        let printable = to_printable(req.payload);
        let mut inner = self.inner.lock().unwrap_or_else(|e| e.into_inner());

        if inner.closed {
            let seq = self.next_seq_locked(&mut inner);
            inner.write_failures += 1;
            inner.last_write_error = Some("WalClosed: writer closed".to_string());
            self.refresh_health_locked(&inner);
            return WalAppend {
                seq,
                ok: false,
                loss_flag: true,
            };
        }

        if inner.startup_read_failed {
            if let Err(error) = self.reload_startup_state_locked(&mut inner) {
                inner.write_failures += 1;
                inner.last_write_error = Some(write_error_text(&error));
                inner.unhealthy_reason = Some("write_failed");
                self.warn(&format!(
                    "WAL append 寫入失敗（best-effort 略過）：{}",
                    error.py_message
                ));
                self.refresh_health_locked(&inner);
                return WalAppend {
                    seq: inner.seq,
                    ok: false,
                    loss_flag: true,
                };
            }
        }

        if inner.seq == u64::MAX {
            inner.write_failures += 1;
            inner.last_write_error = Some("OverflowError: WAL sequence exceeds u64".into());
            inner.unhealthy_reason = Some("write_failed");
            self.warn("WAL append 寫入失敗（best-effort 略過）：WAL sequence exceeds u64");
            self.refresh_health_locked(&inner);
            return WalAppend {
                seq: inner.seq,
                ok: false,
                loss_flag: true,
            };
        }

        let now = self.clock.mono_ns();
        let mut rotation_failed = false;
        if inner.pending_recreate.is_none() {
            let sizes = self.observe_sizes();
            let rotation_due = [Target::Wal, Target::Mirror]
                .into_iter()
                .filter_map(|target| sizes.get(target))
                .any(|size| size >= self.rotate_bytes);
            let (recreate_reason, rotation_skip) =
                self.check_identity_locked(&mut inner, &sizes, now, rotation_due);
            if let Some(reason) = recreate_reason {
                self.begin_recreate_locked(&mut inner, reason);
            }
            rotation_failed = self.rotate_if_needed_locked(&mut inner, &sizes, &rotation_skip);
            if inner.pending_recreate.is_some() {
                self.attempt_recreate_locked(&mut inner, now);
            }
        } else {
            self.attempt_recreate_locked(&mut inner, now);
        }

        if inner.pending_recreate.is_some() {
            let seq = self.next_seq_locked(&mut inner);
            inner.write_failures += 1;
            if inner.last_write_error.is_none() {
                inner.last_write_error = Some("WAL recreation is pending".to_string());
            }
            inner.unhealthy_reason = Some("recreate_failed");
            self.refresh_health_locked(&inner);
            return WalAppend {
                seq,
                ok: false,
                loss_flag: true,
            };
        }

        let seq = self.next_seq_locked(&mut inner);
        let record = WalRecord {
            seq,
            mono_ts_ns: now,
            wall_ts: iso_utc_now(self.clock.wall_unix_us()),
            com: Some(req.com.to_string()),
            dir: req.dir.to_string(),
            source: req.source.to_string(),
            cmd_id: req.cmd_id.map(str::to_string),
            len: req.payload.len() as u64,
            crc32_hex: crc,
            payload_b64,
            loss_flag: req.loss_flag,
            meta: req.meta.unwrap_or_default(),
            rotation_failed: rotation_failed.then_some(true),
        };

        let mut line = record.to_line().into_bytes();
        line.push(b'\n');
        let wal_result = self.write_target_locked(&mut inner, Target::Wal, &line);
        let wal_ok = wal_result.is_ok();
        if wal_ok && inner.file_first_seq_pending {
            inner.file_first_seq_pending = false;
            inner.file_first_seq = Some(seq);
        }
        if !wal_ok {
            inner.write_failures += 1;
            let error = wal_result.expect_err("WAL 寫入失敗時必須帶有錯誤");
            inner.last_write_error = Some(write_error_text(&error));
            inner.unhealthy_reason = Some("write_failed");
            self.warn(&format!(
                "WAL append 寫入失敗（best-effort 略過）：{}",
                error.py_message
            ));
        }

        let mirror_result = if req.payload.is_empty() {
            let _ = self.open_handle_locked(&mut inner, Target::Mirror);
            Ok(())
        } else {
            self.write_target_locked(&mut inner, Target::Mirror, printable.as_bytes())
        };
        let mirror_ok = mirror_result.is_ok();
        if !mirror_ok {
            inner.write_failures += 1;
            let error = mirror_result.expect_err("mirror 寫入失敗時必須帶有錯誤");
            inner.last_write_error = Some(write_error_text(&error));
            self.warn(&format!(
                "WAL append 寫入失敗（best-effort 略過）：{}",
                error.py_message
            ));
        }

        if wal_ok {
            inner.unhealthy_reason = None;
        }
        self.refresh_health_locked(&inner);
        // 寫入錯誤不會改變已序列化在 WAL 行內的 loss_flag。
        WalAppend {
            seq,
            ok: wal_ok && mirror_ok,
            loss_flag: req.loss_flag || !wal_ok,
        }
    }

    pub fn current_seq(&self) -> u64 {
        self.seq.load(Ordering::Acquire)
    }

    /// 不持有 WAL 主鎖，依快照與檔案系統現況回傳健康狀態。
    pub fn health(&self) -> WalHealth {
        let mut health = self
            .health_snapshot
            .read()
            .unwrap_or_else(|e| e.into_inner())
            .clone();
        health.wal_dir_exists = self.current_dir_exists(health.wal_dir_exists);
        health.wal_dir_writable = self.fs.dir_writable(&self.dir);
        let file_exists = self.fs.size(&self.wal_path).ok().flatten().is_some()
            || self
                .fs
                .path_identity(&self.wal_path)
                .ok()
                .flatten()
                .is_some();
        health.wal_file_exists = file_exists;
        health.current_seq = self.current_seq();
        health.healthy = health.wal_dir_exists
            && health.wal_dir_writable
            && !(health.current_seq > 0 && !health.wal_file_exists)
            && health.unhealthy_reason.is_none();
        health
    }

    /// 先同步並關閉兩個 handle，再以 no-replace 語意歸檔現行檔。
    pub fn reset(&self) -> Result<WalReset, PalError> {
        let mut inner = self.inner.lock().unwrap_or_else(|e| e.into_inner());
        if inner.closed {
            return Err(PalError::plain(
                PalErrorKind::Io,
                "WalClosed: writer closed",
            ));
        }
        let previous_seq = inner.seq;
        let timestamp = strftime_utc(self.clock.wall_unix_us(), "%Y%m%d-%H%M%S");

        for target in [Target::Wal, Target::Mirror] {
            if inner.file(target).is_some() {
                if let Some(error) = self.close_handle_locked(&mut inner, target, true) {
                    for reopen in [Target::Wal, Target::Mirror] {
                        let _ = self.open_handle_locked(&mut inner, reopen);
                    }
                    return Err(error);
                }
            }
        }

        let mut wal_suffix = timestamp.clone();
        let mut renamed = false;
        for target in [Target::Wal, Target::Mirror] {
            let path = self.path_for(target);
            let size = match self.fs.size(path) {
                Ok(size) => size.unwrap_or(0),
                Err(error) => {
                    for reopen in [Target::Wal, Target::Mirror] {
                        let _ = self.open_handle_locked(&mut inner, reopen);
                    }
                    return Err(error);
                }
            };
            if size == 0 {
                continue;
            }
            let base = archive_path(path, &timestamp);
            match self.fs.rename_noreplace_or_suffix(path, &base) {
                Ok(archived) => {
                    renamed = true;
                    if target == Target::Wal {
                        wal_suffix = archive_suffix(path, &archived, &timestamp);
                        inner.wal_path_expected = false;
                        inner.wal_identity = None;
                        inner.file_first_seq = None;
                        inner.file_first_seq_pending = true;
                    }
                    inner.set_size(target, None);
                    inner.set_dirty_tail(target, false);
                }
                Err(error) => {
                    for reopen in [Target::Wal, Target::Mirror] {
                        let _ = self.open_handle_locked(&mut inner, reopen);
                    }
                    return Err(error);
                }
            }
        }
        // Python reset 即使沒有可歸檔檔案，也會同步目錄。
        if let Err(error) = self.fs.sync_dir(&self.dir) {
            return Err(error);
        }

        inner.seq = 0;
        self.seq.store(0, Ordering::Release);
        inner.file_first_seq = None;
        inner.file_first_seq_pending = true;
        inner.wal_path_expected = false;
        inner.wal_identity = None;
        inner.last_identity_check_ns = None;
        inner.wal_size = if renamed {
            None
        } else {
            self.fs.size(&self.wal_path).ok().flatten()
        };
        inner.mirror_size = if renamed {
            None
        } else {
            self.fs.size(&self.mirror_path).ok().flatten()
        };
        self.refresh_health_locked(&inner);
        Ok(WalReset {
            previous_seq,
            rotated_suffix: wal_suffix,
        })
    }

    pub fn tail_raw_with_meta(
        &self,
        from_seq: Option<i128>,
        com: Option<&str>,
        limit: i128,
    ) -> PalResult<(Vec<PyDict>, bool)> {
        let Some(bytes) = self.fs.read_all(&self.wal_path)? else {
            return Ok((Vec::new(), false));
        };
        let limit = usize::try_from(limit.max(0)).unwrap_or(usize::MAX);
        let latest = from_seq.is_none();
        let mut selected = VecDeque::new();
        let mut truncated = false;
        for_each_wal_row(&bytes, com, |row, seq| {
            if from_seq.is_some_and(|from| seq <= from) {
                return true;
            }
            if latest {
                if limit == 0 {
                    truncated = true;
                    return false;
                }
                if selected.len() == limit {
                    selected.pop_front();
                    truncated = true;
                }
                selected.push_back(row);
                true
            } else if selected.len() == limit {
                truncated = true;
                false
            } else {
                selected.push_back(row);
                true
            }
        });
        Ok((selected.into_iter().collect(), truncated))
    }

    pub fn available_from_seq(&self) -> Option<i128> {
        let bytes = self.fs.read_all(&self.wal_path).ok().flatten()?;
        let mut first = None;
        for_each_wal_row(&bytes, None, |_, seq| {
            first = Some(seq);
            false
        });
        first
    }

    pub fn wal_path(&self) -> PathBuf {
        self.wal_path.clone()
    }

    pub fn mirror_path(&self) -> PathBuf {
        self.mirror_path.clone()
    }

    /// 同步並各自關閉常駐 handle；呼叫後不再接受寫入。
    pub fn shutdown(&self) -> PalResult<()> {
        let mut inner = self.inner.lock().unwrap_or_else(|e| e.into_inner());
        inner.closed = true;
        let mut first_error = None;
        for target in [Target::Wal, Target::Mirror] {
            if let Some(error) = self.close_handle_locked(&mut inner, target, true) {
                if first_error.is_none() {
                    first_error = Some(error);
                }
            }
        }
        if let Some(error) = first_error {
            inner.last_write_error = Some(write_error_text(&error));
            self.refresh_health_locked(&inner);
            Err(error)
        } else {
            self.refresh_health_locked(&inner);
            Ok(())
        }
    }

    fn reload_startup_state_locked(&self, inner: &mut WalInner) -> PalResult<()> {
        let bytes = self.fs.read_all(&self.wal_path)?;
        let (seq, file_first_seq, first_nonempty_seen, sequence_overflow) = bytes
            .as_deref()
            .map(scan_seq_bounds)
            .unwrap_or((0, None, false, false));
        inner.seq = seq;
        self.seq.store(seq, Ordering::Release);
        inner.file_first_seq = file_first_seq;
        inner.file_first_seq_pending = !first_nonempty_seen;
        inner.wal_size = self.fs.size(&self.wal_path).ok().flatten();
        inner.wal_path_expected = bytes.is_some();
        inner.wal_identity = self.fs.path_identity(&self.wal_path).ok().flatten();
        inner.dirty_wal_tail = bytes
            .as_deref()
            .is_some_and(|data| !data.is_empty() && !data.ends_with(b"\n"));
        inner.dir_exists = self.current_dir_exists(inner.dir_exists);
        inner.startup_read_failed = false;
        if sequence_overflow {
            inner.unhealthy_reason = Some("write_failed");
            inner.last_write_error = Some("OverflowError: WAL sequence exceeds u64".into());
        } else {
            if inner.unhealthy_reason == Some("write_failed") {
                inner.unhealthy_reason = None;
            }
            inner.last_write_error = None;
        }
        self.refresh_health_locked(inner);
        if sequence_overflow {
            return Err(PalError::plain(
                PalErrorKind::Io,
                "WAL sequence exceeds u64",
            ));
        }
        Ok(())
    }

    fn warn(&self, message: &str) {
        let sink = self
            .warn_sink
            .read()
            .unwrap_or_else(|e| e.into_inner())
            .clone();
        sink(message);
    }

    fn warn_dir_recreated(&self, cause: &str) {
        self.warn(&format!(
            "WAL 目錄 {} 消失，已重建並續寫（原因：{}）；消失期間的紀錄無法復原",
            self.dir.display(),
            cause
        ));
    }

    fn next_seq_locked(&self, inner: &mut WalInner) -> u64 {
        inner.seq = inner.seq.saturating_add(1);
        self.seq.store(inner.seq, Ordering::Release);
        inner.seq
    }

    fn path_for(&self, target: Target) -> &Path {
        match target {
            Target::Wal => &self.wal_path,
            Target::Mirror => &self.mirror_path,
        }
    }

    fn observe_sizes(&self) -> SizeSnapshot {
        let (wal, wal_error) = match self.fs.size(&self.wal_path) {
            Ok(size) => (size, false),
            Err(_) => (None, true),
        };
        let (mirror, mirror_error) = match self.fs.size(&self.mirror_path) {
            Ok(size) => (size, false),
            Err(_) => (None, true),
        };
        SizeSnapshot {
            wal,
            mirror,
            wal_error,
            mirror_error,
        }
    }

    fn check_identity_locked(
        &self,
        inner: &mut WalInner,
        sizes: &SizeSnapshot,
        now_ns: u64,
        force: bool,
    ) -> (Option<&'static str>, HashSet<TargetKey>) {
        let mut skip = HashSet::new();
        if sizes.wal_error {
            skip.insert(TargetKey::Wal);
        }
        if sizes.mirror_error {
            skip.insert(TargetKey::Mirror);
        }

        let wal_expected = inner.wal_path_expected || inner.wal_file.is_some();
        let wal_history_exists = wal_expected || inner.mirror_file.is_some() || inner.seq > 0;
        let dir_exists = self.current_dir_exists(inner.dir_exists);
        inner.dir_exists = dir_exists;
        let directory_missing = wal_history_exists && !dir_exists;
        let mut wal_reason = if directory_missing && wal_expected {
            Some("dir_missing")
        } else {
            None
        };
        if wal_reason.is_none()
            && wal_expected
            && sizes.wal.is_none()
            && !sizes.wal_error
            && !directory_missing
        {
            wal_reason = Some("external_unlink");
        }

        for target in [Target::Wal, Target::Mirror] {
            if inner.file(target).is_none() || sizes.errored(target) {
                continue;
            }
            if directory_missing || sizes.get(target).is_none() {
                if target == Target::Wal {
                    wal_reason = Some(if directory_missing {
                        "dir_missing"
                    } else {
                        "external_unlink"
                    });
                } else {
                    let _ = self.close_handle_locked(inner, target, false);
                    inner.set_dirty_tail(target, false);
                }
            }
        }

        let due = force
            || inner
                .last_identity_check_ns
                .is_none_or(|last| now_ns.saturating_sub(last) >= IDENTITY_CHECK_INTERVAL_NS);
        if !due {
            return (wal_reason, skip);
        }
        inner.last_identity_check_ns = Some(now_ns);

        for target in [Target::Wal, Target::Mirror] {
            if inner.file(target).is_none() || skip.contains(&TargetKey::from(target)) {
                continue;
            }
            if target == Target::Wal && wal_reason.is_some() {
                continue;
            }
            if sizes.get(target).is_none() {
                if target == Target::Wal {
                    wal_reason = Some("external_unlink");
                } else {
                    let _ = self.close_handle_locked(inner, target, false);
                    inner.set_dirty_tail(target, false);
                }
                continue;
            }
            let path_identity = match self.fs.path_identity(self.path_for(target)) {
                Ok(identity) => identity,
                Err(_) => {
                    skip.insert(TargetKey::from(target));
                    continue;
                }
            };
            let Some(path_identity) = path_identity else {
                if target == Target::Wal {
                    wal_reason = Some("external_unlink");
                } else {
                    let _ = self.close_handle_locked(inner, target, false);
                    inner.set_dirty_tail(target, false);
                }
                continue;
            };
            if path_identity == FileIdentity(0, 0) {
                if target == Target::Wal {
                    wal_reason = Some("external_unlink");
                } else {
                    let _ = self.close_handle_locked(inner, target, false);
                    inner.set_dirty_tail(target, false);
                }
                continue;
            }
            let handle_identity = match inner.file(target).as_ref().unwrap().identity() {
                Ok(identity) => identity,
                Err(_) => {
                    skip.insert(TargetKey::from(target));
                    continue;
                }
            };
            if target == Target::Wal {
                inner.wal_identity = Some(handle_identity.clone());
            }
            let reason = if path_identity != handle_identity {
                Some("replaced")
            } else {
                match inner.file(target).as_ref().unwrap().unlinked() {
                    Ok(true) => Some("external_unlink"),
                    Ok(false) => None,
                    Err(_) => {
                        skip.insert(TargetKey::from(target));
                        None
                    }
                }
            };
            if let Some(reason) = reason {
                if target == Target::Wal {
                    wal_reason = Some(reason);
                } else {
                    let _ = self.close_handle_locked(inner, target, false);
                    inner.set_dirty_tail(target, false);
                    inner.mirror_size = sizes.mirror;
                }
            }
        }
        (wal_reason, skip)
    }

    fn begin_recreate_locked(&self, inner: &mut WalInner, reason: &'static str) {
        if inner.pending_recreate.is_some() {
            return;
        }
        let previous_identity = inner
            .wal_file
            .as_ref()
            .and_then(|file| file.identity().ok())
            .or_else(|| inner.wal_identity.clone())
            .map(identity_text);
        let file_first_seq = inner.file_first_seq;
        let _ = self.close_handle_locked(inner, Target::Wal, false);
        inner.dirty_wal_tail = false;
        inner.pending_recreate = Some(PendingRecreate {
            reason,
            previous_identity,
            file_first_seq,
            warning_cause: Some(format!(
                "[Errno 2] WAL 檔案路徑消失: '{}'",
                self.dir.display()
            )),
            recreated_counted: false,
        });
        inner.wal_size = None;
    }

    fn attempt_recreate_locked(&self, inner: &mut WalInner, now_ns: u64) -> bool {
        let Some(pending) = inner.pending_recreate.as_ref() else {
            return true;
        };
        if inner
            .next_recreate_at_ns
            .is_some_and(|deadline| now_ns < deadline)
        {
            return false;
        }
        let reason = pending.reason;
        let previous_identity = pending.previous_identity.clone();
        let file_first_seq = pending.file_first_seq;
        let warning_cause = pending.warning_cause.clone();
        let recreated_counted = pending.recreated_counted;

        match self
            .fs
            .ensure_dir(&self.dir)
            .and_then(|()| self.open_handle_locked(inner, Target::Wal))
        {
            Ok(()) => {}
            Err(error) => {
                self.schedule_recreate_failure(inner, &error, now_ns);
                return false;
            }
        }
        let size = self.fs.size(&self.wal_path).ok().flatten().unwrap_or(0);
        let identity = inner
            .wal_file
            .as_ref()
            .and_then(|handle| handle.identity().ok());
        inner.wal_size = Some(size);
        inner.wal_path_expected = true;
        inner.wal_identity = identity;
        inner.last_identity_check_ns = Some(now_ns);

        let needs_termination = match self.fs.tail_needs_termination(&self.wal_path) {
            Ok(needs) => needs,
            Err(error) => {
                let _ = self.close_handle_locked(inner, Target::Wal, false);
                self.schedule_recreate_failure(inner, &error, now_ns);
                return false;
            }
        };
        if needs_termination {
            if let Err(error) = self.write_attempt_locked(inner, Target::Wal, b"\n") {
                let _ = self.close_handle_locked(inner, Target::Wal, false);
                self.schedule_recreate_failure(inner, &error.error, now_ns);
                return false;
            }
            inner.dirty_wal_tail = false;
        }

        let last_seq = inner.seq;
        let range = file_first_seq
            .filter(|first| *first >= 1 && last_seq >= *first)
            .map(|first| (first, last_seq));
        let meta = wal_recreated_meta(reason, previous_identity.as_deref(), range);
        let record = self.new_meta_record_locked(inner, now_ns, meta);
        let mut line = record.to_line().into_bytes();
        line.push(b'\n');
        if let Err(error) = self.write_target_locked(inner, Target::Wal, &line) {
            let _ = self.close_handle_locked(inner, Target::Wal, false);
            self.schedule_recreate_failure(inner, &error, now_ns);
            return false;
        }

        let _ = self.open_handle_locked(inner, Target::Mirror);
        inner.file_first_seq = Some(record.seq);
        inner.file_first_seq_pending = false;
        if !recreated_counted {
            inner.recreated_count += 1;
        }
        if !recreated_counted {
            self.warn_dir_recreated(warning_cause.as_deref().unwrap_or(reason));
        }
        inner.pending_recreate = None;
        inner.recreate_failures = 0;
        inner.next_recreate_at_ns = None;
        inner.unhealthy_reason = None;
        self.refresh_health_locked(inner);
        true
    }

    fn schedule_recreate_failure(&self, inner: &mut WalInner, error: &PalError, now_ns: u64) {
        inner.recreate_failures += 1;
        let delay = if inner.recreate_failures <= RECREATE_BACKOFF_NS.len() {
            RECREATE_BACKOFF_NS[inner.recreate_failures - 1]
        } else {
            let index = (inner.recreate_failures - RECREATE_BACKOFF_NS.len() - 1)
                .min(RECREATE_RETRY_INTERVALS_NS.len() - 1);
            RECREATE_RETRY_INTERVALS_NS[index]
        };
        inner.next_recreate_at_ns = Some(now_ns.saturating_add(delay));
        inner.unhealthy_reason = Some("recreate_failed");
        inner.last_write_error = Some(write_error_text(error));
        self.warn(&format!(
            "WAL 重建失敗（best-effort 延後重試）：{}",
            error.py_message
        ));
        self.refresh_health_locked(inner);
    }

    fn new_meta_record_locked(
        &self,
        inner: &mut WalInner,
        mono_ns: u64,
        meta: PyDict,
    ) -> WalRecord {
        let seq = self.next_seq_locked(inner);
        WalRecord {
            seq,
            mono_ts_ns: mono_ns,
            wall_ts: iso_utc_now(self.clock.wall_unix_us()),
            com: Some("WAL".to_string()),
            dir: "META".to_string(),
            source: "system".to_string(),
            cmd_id: None,
            len: 0,
            crc32_hex: "00000000".to_string(),
            payload_b64: String::new(),
            loss_flag: true,
            meta,
            rotation_failed: None,
        }
    }

    fn rotate_if_needed_locked(
        &self,
        inner: &mut WalInner,
        sizes: &SizeSnapshot,
        skip: &HashSet<TargetKey>,
    ) -> bool {
        let mut failed = false;
        let timestamp = strftime_utc(self.clock.wall_unix_us(), "%Y%m%d-%H%M%S");
        for target in [Target::Wal, Target::Mirror] {
            if skip.contains(&TargetKey::from(target)) || sizes.errored(target) {
                continue;
            }
            let Some(size) = sizes.get(target) else {
                continue;
            };
            if size < self.rotate_bytes {
                inner.set_size(target, Some(size));
                continue;
            }
            let path = self.path_for(target);
            if inner.file(target).is_none() {
                if let Err(error) = self.open_handle_locked(inner, target) {
                    self.warn_rotate_failed(path, &error);
                    failed = true;
                    continue;
                }
            }
            if let Some(error) = self.close_handle_locked(inner, target, true) {
                self.warn_rotate_failed(path, &error);
                failed = true;
                let _ = self.open_handle_locked(inner, target);
                continue;
            }
            let base = archive_path(path, &timestamp);
            match self.fs.rename_noreplace_or_suffix(path, &base) {
                Ok(_) => {
                    inner.set_size(target, None);
                    inner.set_dirty_tail(target, false);
                    if target == Target::Wal {
                        inner.wal_path_expected = false;
                        inner.wal_identity = None;
                        inner.file_first_seq = Some(inner.seq.saturating_add(1));
                        inner.file_first_seq_pending = false;
                    }
                    if let Err(error) = self.fs.sync_dir(&self.dir) {
                        self.warn_rotate_failed(path, &error);
                        failed = true;
                    }
                }
                Err(error) => {
                    if is_transient_rotate_error(&error) {
                        inner.rotate_retry += 1;
                    }
                    self.warn_rotate_failed(path, &error);
                    failed = true;
                    let _ = self.open_handle_locked(inner, target);
                }
            }
        }
        failed
    }

    fn warn_rotate_failed(&self, path: &Path, error: &PalError) {
        self.warn(&format!(
            "WAL 輪替失敗（best-effort 續用既有檔案，不殺 reader）：{}：{}",
            path.display(),
            error.py_message
        ));
    }

    fn open_handle_locked(&self, inner: &mut WalInner, target: Target) -> PalResult<()> {
        if inner.file(target).is_some() {
            return Ok(());
        }
        let handle = self.fs.open_append(self.path_for(target))?;
        let size = self
            .fs
            .size(self.path_for(target))
            .ok()
            .flatten()
            .unwrap_or(0);
        let identity = handle.identity().ok();
        *inner.file_mut(target) = Some(handle);
        inner.set_size(target, Some(size));
        if target == Target::Wal {
            inner.wal_path_expected = true;
            inner.wal_identity = identity;
            inner.last_identity_check_ns = Some(self.clock.mono_ns());
        }
        Ok(())
    }

    fn close_handle_locked(
        &self,
        inner: &mut WalInner,
        target: Target,
        sync: bool,
    ) -> Option<PalError> {
        let Some(mut handle) = inner.file_mut(target).take() else {
            return None;
        };
        let mut first_error = if sync { handle.sync().err() } else { None };
        if let Err(error) = handle.close() {
            if first_error.is_none() {
                first_error = Some(error);
            }
        }
        first_error
    }

    fn write_attempt_locked(
        &self,
        inner: &mut WalInner,
        target: Target,
        bytes: &[u8],
    ) -> Result<(), WalWriteError> {
        if let Err(error) = self.open_handle_locked(inner, target) {
            return Err(WalWriteError { sent: 0, error });
        }
        let result = inner
            .file_mut(target)
            .as_mut()
            .expect("open_handle_locked 已建立 handle")
            .write_all_flush(bytes);
        match result {
            Ok(()) => {
                self.add_size(inner, target, bytes.len());
                Ok(())
            }
            Err(error) => {
                self.add_size(inner, target, error.sent.min(bytes.len()));
                Err(error)
            }
        }
    }

    fn add_size(&self, inner: &mut WalInner, target: Target, count: usize) {
        let size = inner.size(target).unwrap_or(0).saturating_add(count as u64);
        inner.set_size(target, Some(size));
    }

    fn write_target_locked(
        &self,
        inner: &mut WalInner,
        target: Target,
        data: &[u8],
    ) -> Result<(), PalError> {
        let mut buffer = Vec::new();
        let term = usize::from(inner.dirty_tail(target));
        if term != 0 {
            buffer.push(b'\n');
        }
        buffer.extend_from_slice(data);

        match self.write_attempt_locked(inner, target, &buffer) {
            Ok(()) => {
                inner.set_dirty_tail(target, false);
                return Ok(());
            }
            Err(first) => {
                let sent = first.sent.min(buffer.len());
                if sent >= buffer.len() {
                    inner.set_dirty_tail(target, false);
                    inner.last_write_error = Some(write_error_text(&first.error));
                    return Ok(());
                }
                if sent > 0 {
                    return match self.write_attempt_locked(inner, target, &buffer[sent..]) {
                        Ok(()) => {
                            inner.set_dirty_tail(target, false);
                            inner.last_write_error = Some(write_error_text(&first.error));
                            Ok(())
                        }
                        Err(retry) => {
                            let confirmed =
                                sent.saturating_add(retry.sent.min(buffer.len() - sent));
                            inner.set_dirty_tail(
                                target,
                                dirty_tail_after(term, confirmed, buffer.len()),
                            );
                            Err(retry.error)
                        }
                    };
                }

                let directory_missing = first.error.kind == PalErrorKind::NotFound;
                let previous_identity = inner
                    .wal_identity
                    .clone()
                    .map(|identity| identity_text(identity));
                let file_first_seq = inner.file_first_seq;
                if let Some(error) = self.close_handle_locked(inner, target, false) {
                    inner.set_dirty_tail(target, term > 0);
                    return Err(error);
                }
                if let Err(error) = self.fs.ensure_dir(&self.dir) {
                    inner.set_dirty_tail(target, term > 0);
                    return Err(error);
                }
                match self.write_attempt_locked(inner, target, &buffer) {
                    Ok(()) => {
                        inner.set_dirty_tail(target, false);
                        inner.last_write_error = Some(write_error_text(&first.error));
                        if directory_missing {
                            inner.recreated_count += 1;
                            if target == Target::Wal {
                                inner.pending_recreate = Some(PendingRecreate {
                                    reason: "dir_missing",
                                    previous_identity,
                                    file_first_seq,
                                    warning_cause: Some(first.error.py_message.clone()),
                                    recreated_counted: true,
                                });
                            }
                            self.warn_dir_recreated(&first.error.py_message);
                        }
                        Ok(())
                    }
                    Err(retry) => {
                        let confirmed = retry.sent.min(buffer.len());
                        inner.set_dirty_tail(
                            target,
                            dirty_tail_after(term, confirmed, buffer.len()),
                        );
                        Err(retry.error)
                    }
                }
            }
        }
    }

    fn refresh_health_locked(&self, inner: &WalInner) {
        let dir_writable = self.fs.dir_writable(&self.dir);
        let wal_file_exists = self.fs.size(&self.wal_path).ok().flatten().is_some()
            || self
                .fs
                .path_identity(&self.wal_path)
                .ok()
                .flatten()
                .is_some();
        let wal_dir_exists = self.current_dir_exists(inner.dir_exists) || wal_file_exists;
        let healthy = wal_dir_exists
            && dir_writable
            && !(inner.seq > 0 && !wal_file_exists)
            && inner.unhealthy_reason.is_none();
        let snapshot = WalHealth {
            wal_dir: self.dir.to_string_lossy().into_owned(),
            wal_path: self.wal_path.to_string_lossy().into_owned(),
            wal_dir_exists,
            wal_file_exists,
            wal_dir_writable: dir_writable,
            current_seq: inner.seq,
            write_failures: inner.write_failures,
            last_write_error: inner.last_write_error.clone(),
            recreated_count: inner.recreated_count,
            rotate_retry: inner.rotate_retry,
            rotate_deferred: 0,
            deferred_delete_pending: 0,
            healthy,
            unhealthy_reason: inner.unhealthy_reason,
        };
        *self
            .health_snapshot
            .write()
            .unwrap_or_else(|e| e.into_inner()) = snapshot;
        self.notify_health(!healthy);
    }

    fn notify_health(&self, unhealthy: bool) {
        if let Some(sink) = self.health_sink.get() {
            sink.set_wal_unhealthy(unhealthy);
        }
    }

    fn current_dir_exists(&self, fallback: bool) -> bool {
        match self.fs.path_identity(&self.dir) {
            Ok(Some(_)) => {
                self.dir_identity_known.store(true, Ordering::Relaxed);
                true
            }
            Ok(None) if self.dir_identity_known.load(Ordering::Relaxed) => false,
            Ok(None) | Err(_) => self.fs.dir_writable(&self.dir) || fallback,
        }
    }
}

#[derive(Hash, PartialEq, Eq)]
enum TargetKey {
    Wal,
    Mirror,
}

impl From<Target> for TargetKey {
    fn from(target: Target) -> Self {
        match target {
            Target::Wal => TargetKey::Wal,
            Target::Mirror => TargetKey::Mirror,
        }
    }
}

fn for_each_wal_row(bytes: &[u8], com: Option<&str>, mut visit: impl FnMut(PyDict, i128) -> bool) {
    for raw_line in bytes.split(|byte| *byte == b'\n') {
        let decoded = decode_utf8_replace(raw_line);
        let line = py_strip(&decoded);
        if line.is_empty() {
            continue;
        }
        let Ok(PyValue::Dict(row)) = loads(&line) else {
            continue;
        };
        let Some(seq) = row.get("seq").and_then(py_int_as_i128) else {
            continue;
        };
        if com.is_some_and(|wanted| {
            !wanted.is_empty() && row.get("com").and_then(PyValue::as_str) != Some(wanted)
        }) {
            continue;
        }
        if !visit(row, seq) {
            break;
        }
    }
}

fn py_int_as_i128(value: &PyValue) -> Option<i128> {
    match value {
        PyValue::Int(PyInt::Small(value)) => Some(*value),
        PyValue::Int(PyInt::Big(value)) => value.parse().ok(),
        _ => None,
    }
}

fn py_int_exceeds_u64(value: &PyValue) -> bool {
    match value {
        PyValue::Int(PyInt::Small(value)) => *value > i128::from(u64::MAX),
        PyValue::Int(PyInt::Big(value)) => !value.starts_with('-') && value.parse::<u64>().is_err(),
        _ => false,
    }
}

fn scan_seq_bounds(bytes: &[u8]) -> (u64, Option<u64>, bool, bool) {
    let mut maximum = 0_u64;
    let mut first = None;
    let mut first_nonempty_seen = false;
    let mut sequence_overflow = false;
    for raw_line in bytes.split(|byte| *byte == b'\n') {
        let decoded = decode_utf8_replace(raw_line);
        let line = py_strip(&decoded);
        if line.is_empty() {
            continue;
        }
        let first_nonempty = !first_nonempty_seen;
        first_nonempty_seen = true;
        let Ok(PyValue::Dict(row)) = loads(line) else {
            continue;
        };
        let Some(value) = row.get("seq") else {
            continue;
        };
        if py_int_exceeds_u64(value) {
            sequence_overflow = true;
            maximum = u64::MAX;
            continue;
        }
        let Some(seq) = py_int_as_i128(value) else {
            continue;
        };
        if seq < 0 {
            continue;
        }
        let Ok(seq) = u64::try_from(seq) else {
            sequence_overflow = true;
            maximum = u64::MAX;
            continue;
        };
        maximum = maximum.max(seq);
        if first_nonempty {
            first = Some(seq);
        }
    }
    (maximum, first, first_nonempty_seen, sequence_overflow)
}

fn identity_text(identity: FileIdentity) -> String {
    format!("{}:{}", identity.0, identity.1)
}

fn write_error_text(error: &PalError) -> String {
    format!("{}: {}", error.py_exc_name(), error.py_message)
}

fn dirty_tail_after(term: usize, confirmed: usize, buffer_len: usize) -> bool {
    if term < confirmed && confirmed < buffer_len {
        true
    } else if confirmed <= term {
        confirmed < term
    } else {
        false
    }
}

fn archive_path(path: &Path, timestamp: &str) -> PathBuf {
    let mut name = path.as_os_str().to_os_string();
    name.push(format!(".{timestamp}"));
    PathBuf::from(name)
}

fn archive_suffix(path: &Path, archived: &Path, timestamp: &str) -> String {
    let base = path.file_name().unwrap_or_default().to_string_lossy();
    let archived_name = archived.file_name().unwrap_or_default().to_string_lossy();
    archived_name
        .strip_prefix(&format!("{base}."))
        .unwrap_or(timestamp)
        .to_string()
}

/// 把 WAL 列解碼成 mirror 文字行，保留 Python 的 partial 尾段切片行為。
pub fn rows_to_text_lines(rows: &[PyDict]) -> Vec<String> {
    let mut text = String::new();
    for row in rows {
        let encoded = row
            .get("payload_b64")
            .and_then(PyValue::as_str)
            .unwrap_or("");
        if let Ok(decoded) = a2b_base64_lenient(encoded.as_bytes()) {
            text.push_str(&to_printable(&decoded));
        }
    }
    let mut lines = splitlines(&text)
        .into_iter()
        .map(str::to_string)
        .collect::<Vec<_>>();
    if text.ends_with('\n') {
        return lines;
    }
    let consumed = lines.iter().map(|line| line.chars().count()).sum::<usize>()
        + lines.len().saturating_sub(1);
    let text_len = text.chars().count();
    if consumed < text_len {
        lines.push(text.chars().skip(consumed).collect());
    }
    lines
}
