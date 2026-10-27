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
