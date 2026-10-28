//! 背景命令 capture 與 agent log capture（Slice 2.1 Task 14；05 §1.1）。
//!
//! 對照 `session_manager.py` 的 `BackgroundCapture`（`add_chunk`／`maybe_finalize`）、
//! `get_background_result`（`itertools.islice` 語意）與 `SessionCapture`／`log_start`。

use std::collections::VecDeque;
use std::io::Write;
use std::sync::Arc;

use sw_pal::traits::Clock;
use sw_proto::pycompat::PyErr;
use sw_proto::pyjson::{PyDict, PyInt, PyValue};

use crate::Mono;

/// `BG_CAPTURE_MAX_BYTES`（4 MiB；Python 以字元數計）。
pub const BG_CAPTURE_MAX_BYTES: usize = 4 * 1024 * 1024;
/// `BG_CAPTURE_MAX_COUNT`。
pub const BG_CAPTURE_MAX_COUNT: usize = 64;

/// 遠端淘汰回呼：`(擁有者 session_id, cmd_id)`；由 manager 轉為對該 session 的 `ControlEvent::EvictCapture`。
pub type RemoteEvictor = Arc<dyn Fn(&str, &str) + Send + Sync>;

/// 全 daemon 共用的 background capture 索引（Python `SessionManager._background` 為 manager 層全域 dict，
/// `_evict_background_locked` 依全域 `created_at` 淘汰；審查 F28）。capture 本體仍由各 session 持有，
/// 索引只記擁有者、`created_at`、插入序與是否 active（與 capture 的 `status` 同步）。
pub struct BackgroundIndex {
    inner: std::sync::Mutex<BackgroundIndexInner>,
    remote: std::sync::OnceLock<RemoteEvictor>,
}

#[derive(Default)]
struct BackgroundIndexInner {
    next_seq: u64,
    /// cmd_id → (sid, created_at, seq, active)；插入序即 Python dict 序。
    entries: indexmap::IndexMap<String, (String, String, u64, bool)>,
}

impl BackgroundIndex {
    pub fn new() -> Arc<BackgroundIndex> {
        Arc::new(BackgroundIndex {
            inner: std::sync::Mutex::new(BackgroundIndexInner::default()),
            remote: std::sync::OnceLock::new(),
        })
    }

    /// 設定遠端淘汰回呼（只生效一次；未設定時跨 session 的淘汰只從索引移除）。
    pub fn set_remote_evictor(&self, f: RemoteEvictor) {
        let _ = self.remote.set(f);
    }

    fn lock(&self) -> std::sync::MutexGuard<'_, BackgroundIndexInner> {
        self.inner.lock().unwrap_or_else(|e| e.into_inner())
    }

    /// 登錄新 capture（active）並依 `_evict_background_locked` 淘汰：總數超過 `BG_CAPTURE_MAX_COUNT` 時，
    /// 依 `created_at`（同值依插入序）移除最舊的已終結者，回傳被淘汰的 `(sid, cmd_id)`（呼叫端處理本 session 者，
    /// 其餘經遠端回呼）。active 者永不淘汰。
    pub fn register(&self, cmd_id: &str, sid: &str, created_at: &str) -> Vec<(String, String)> {
        let mut g = self.lock();
        let seq = g.next_seq;
        g.next_seq = g.next_seq.saturating_add(1);
        g.entries
            .insert(cmd_id.to_string(), (sid.to_string(), created_at.to_string(), seq, true));
        if g.entries.len() <= BG_CAPTURE_MAX_COUNT {
            return Vec::new();
        }
        let mut finalized: Vec<(String, u64, String, String)> = g
            .entries
            .iter()
            .filter(|(_, (_, _, _, active))| !*active)
            .map(|(id, (owner, at, seq, _))| (at.clone(), *seq, id.clone(), owner.clone()))
            .collect();
        finalized.sort();
        let excess = g.entries.len() - BG_CAPTURE_MAX_COUNT;
        let victims: Vec<(String, String)> = finalized
            .into_iter()
            .take(excess)
            .map(|(_, _, id, owner)| (owner, id))
            .collect();
        for (_, id) in &victims {
            g.entries.shift_remove(id);
        }
        victims
    }

    /// 同步 capture 的 active 狀態（`status == "active"`）。
    pub fn set_active(&self, cmd_id: &str, active: bool) {
        if let Some(entry) = self.lock().entries.get_mut(cmd_id) {
            entry.3 = active;
        }
    }

    /// 自索引移除（capture 被擁有者丟棄時）。
    pub fn remove(&self, cmd_id: &str) {
        self.lock().entries.shift_remove(cmd_id);
    }

    /// 索引內 capture 數（測試與診斷用）。
    pub fn len(&self) -> usize {
        self.lock().entries.len()
    }

    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }

    /// 通知擁有者 session 丟棄被全域淘汰的 capture。
    pub fn evict_remote(&self, sid: &str, cmd_id: &str) {
        if let Some(f) = self.remote.get() {
            f(sid, cmd_id);
        }
    }
}

/// `background` 命令的輸出累積（`command.result_tail` 逐段讀取）。
#[derive(Clone, Debug)]
pub struct BackgroundCapture {
    pub cmd_id: String,
    pub session_id: String,
    pub from_seq: u64,
    pub quiet_window_s: f64,
    pub created_at: String,
    pub chunks: VecDeque<String>,
    pub last_seq: u64,
    pub status: &'static str,
    pub error_code: Option<String>,
    pub last_activity: Mono,
    /// chunks 在記憶體的累計大小（字元數；Python `len(str)`）。
    pub total_bytes: usize,
    /// 因環形上限丟棄的最舊 chunk 數（result cursor 對齊）。
    pub dropped_chunks: usize,
    /// 建立順序（淘汰時依 `created_at` 排序的 tie-break）。
    pub seq_no: u64,
}

impl BackgroundCapture {
    pub fn new(
        cmd_id: String,
        session_id: String,
        from_seq: u64,
        last_seq: u64,
        quiet_window_s: f64,
        created_at: String,
        now: Mono,
        seq_no: u64,
    ) -> BackgroundCapture {
        BackgroundCapture {
            cmd_id,
            session_id,
            from_seq,
            quiet_window_s,
            created_at,
            chunks: VecDeque::new(),
            last_seq,
            status: "active",
            error_code: None,
            last_activity: now,
            total_bytes: 0,
            dropped_chunks: 0,
            seq_no,
        }
    }

    /// 新增一段 RX chunk；超過 `max_chars` 時丟最舊 chunk，至少保留一段（#81）。
    pub fn add_chunk(&mut self, chunk: &str, max_chars: usize) {
        if chunk.is_empty() {
            return;
        }
        self.total_bytes = self.total_bytes.saturating_add(chunk.chars().count());
        self.chunks.push_back(chunk.to_string());
        while self.total_bytes > max_chars && self.chunks.len() > 1 {
            if let Some(oldest) = self.chunks.pop_front() {
                self.total_bytes = self.total_bytes.saturating_sub(oldest.chars().count());
                self.dropped_chunks = self.dropped_chunks.saturating_add(1);
            }
        }
    }

    /// active 且距最後活動 ≥ `quiet_window_s` → `done`。
    pub fn maybe_finalize(&mut self, now: Mono) {
        if self.status == "active"
            && now.saturating_sub(self.last_activity) as f64 / 1_000_000_000.0
                >= self.quiet_window_s
        {
            self.status = "done";
        }
    }

    /// `get_background_result`：`start = max(from_chunk - dropped_chunks, 0)`，`islice(start, start+limit)`。
    /// `start + limit < 0` 照 Python `islice` 拋 `ValueError`。
    pub fn result(&mut self, from_chunk: i128, limit: i128, now: Mono) -> Result<PyDict, PyErr> {
        self.maybe_finalize(now);
        let dropped = self.dropped_chunks as i128;
        let start = (from_chunk - dropped).max(0);
        let stop = start + limit;
        if stop < 0 {
            return Err(PyErr::value_error(
                "Stop argument for islice() must be None or an integer: 0 <= x <= sys.maxsize.",
            ));
        }
        let len = self.chunks.len() as i128;
        let begin = start.min(len);
        let end = stop.min(len).max(begin);
        let chunks: Vec<PyValue> = self
            .chunks
            .iter()
            .skip(begin as usize)
            .take((end - begin) as usize)
            .map(|chunk| PyValue::from(chunk.clone()))
            .collect();
        let returned_from = dropped + start;
        let next_chunk = returned_from + chunks.len() as i128;
        let mut out = PyDict::new();
        out.insert("ok", PyValue::Bool(true));
        out.insert("cmd_id", PyValue::from(self.cmd_id.clone()));
        out.insert("status", PyValue::from(self.status));
        out.insert(
            "error_code",
            self.error_code
                .clone()
                .map(PyValue::from)
                .unwrap_or(PyValue::Null),
        );
        out.insert("from_seq", py_int(self.from_seq as i128));
        out.insert("last_seq", py_int(self.last_seq as i128));
        out.insert("from_chunk", py_int(from_chunk));
        out.insert("next_chunk", py_int(next_chunk));
        out.insert("dropped_chunks", py_int(dropped));
        out.insert("lost", PyValue::Bool(from_chunk < dropped));
        out.insert("chunks", PyValue::List(chunks));
        Ok(out)
    }
}

/// `session.log_start` 啟動的 agent log capture（寫入 `{log_dir}/{COM}_{YYMMDD}-{HHMMSS}.log`）。
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct AgentLogCapture {
    /// `uuid4` 含連字號。
    pub capture_id: String,
    pub log_path: String,
    pub started_at: String,
    pub line_count: u64,
    pub byte_count: u64,
    pub status: &'static str,
}

// ── P7 Task 0（08-slice23-login-events.md）凍結的新增公開項；本體由標示的 Task 實作 ──

/// agent log capture 的行數／位元組計數（reader 端 observer 更新）。
pub struct CaptureStats {
    pub line_count: std::sync::atomic::AtomicU64,
    pub byte_count: std::sync::atomic::AtomicU64,
}

/// reader 端 agent log 寫入者（DF-15、EVT-D-L1；不經 actor 佇列、無丟棄）。
pub struct CaptureObserver {
    _todo: (),
}

impl CaptureObserver {
    pub fn new() -> std::sync::Arc<Self> {
        todo!("P7 Task 10")
    }
    /// actor 在前景開始／結束時發布（EVT-D-L1）。
    pub fn set_foreground_busy(&self, _busy: bool) {
        todo!("P7 Task 10")
    }
    /// executor 呼叫。
    pub fn install(
        &self,
        _capture_id: String,
        _file: Box<dyn sw_pal::traits::AppendFile>,
    ) -> std::sync::Arc<CaptureStats> {
        todo!("P7 Task 10")
    }
    /// executor 呼叫；取鎖保證先前寫入完成；回 `(line_count, byte_count)`。
    pub fn remove(&self, _capture_id: &str) -> Option<(u64, u64)> {
        todo!("P7 Task 10")
    }
}

impl crate::uart_bridge::RxObserver for CaptureObserver {
    fn on_rx(&self, _com: &str, _data: &[u8], _wal_seq: u64) {
        todo!("P7 Task 10")
    }
}

/// `"{log_dir}/{com}_{%y%m%d-%H%M%S}.log"`（本地時間）。
pub fn log_path_for(_log_dir: &str, _com: &str, _unix_s: i64, _offset_s: i32) -> String {
    todo!("P7 Task 10")
}

pub fn log_start_reply(_cap: &AgentLogCapture, _already_active: bool, _session: PyDict) -> PyDict {
    todo!("P7 Task 10")
}

/// `{"ok":false,"error_code":"LOG_OPEN_FAILED","detail":<str(OSError)>}`。
pub fn log_open_failed_reply(_err: &sw_pal::traits::PalError) -> PyDict {
    todo!("P7 Task 10")
}

/// 無 session。
pub fn log_stop_reply(_cap: &AgentLogCapture, _line_count: u64, _byte_count: u64) -> PyDict {
    todo!("P7 Task 10")
}

pub fn no_active_capture_reply(_session: PyDict) -> PyDict {
    todo!("P7 Task 10")
}

pub fn log_status_reply(_cap: Option<(&AgentLogCapture, u64, u64)>, _session: PyDict) -> PyDict {
    todo!("P7 Task 10")
}

/// agent log 檔案的 append 寫入端（每 chunk flush）。
pub trait LogFile: Send {
    fn append_flush(&mut self, data: &[u8]) -> std::io::Result<()>;
}

/// agent log 的檔案系統操作（`os.makedirs(exist_ok=True)`＋`open(path, "a")`）。
pub trait LogFs: Send + Sync {
    fn makedirs(&self, dir: &str) -> std::io::Result<()>;
    fn open_append(&self, path: &str) -> std::io::Result<Box<dyn LogFile>>;
}

/// 標準函式庫實作（跨平台；不含 platform cfg）。
pub struct StdLogFs;

struct StdLogFile(std::fs::File);

impl LogFile for StdLogFile {
    fn append_flush(&mut self, data: &[u8]) -> std::io::Result<()> {
        self.0.write_all(data)?;
        self.0.flush()
    }
}

impl LogFs for StdLogFs {
    fn makedirs(&self, dir: &str) -> std::io::Result<()> {
        std::fs::create_dir_all(dir)
    }

    fn open_append(&self, path: &str) -> std::io::Result<Box<dyn LogFile>> {
        let file = std::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(path)?;
        Ok(Box::new(StdLogFile(file)))
    }
}

/// agent log capture 的注入依賴（`SessionCore::set_capture_env`）。
#[derive(Clone)]
pub struct CaptureEnv {
    pub fs: Arc<dyn LogFs>,
    /// 檔名時間戳（`time.strftime("%y%m%d-%H%M%S")` 本地時間）。產品路徑由 manager 注入平台 Clock；
    /// `None` 只見於未經 manager 組裝的獨立 core（以 UTC 系統時間代替）。
    pub clock: Option<Arc<dyn Clock>>,
    /// `LOG_DIR`（`SERIALWRAP_LOG_DIR` 或 `~/b-log`）；`None` 時由 env 推導。
    pub default_log_dir: Option<String>,
}

impl Default for CaptureEnv {
    fn default() -> Self {
        CaptureEnv {
            fs: Arc::new(StdLogFs),
            clock: None,
            default_log_dir: None,
        }
    }
}

fn py_int(value: i128) -> PyValue {
    PyValue::Int(PyInt::Small(value))
}
