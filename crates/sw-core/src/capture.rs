//! 背景命令 capture 與 agent log capture（Slice 2.1 Task 14；05 §1.1）。
//!
//! Task 0 骨架：型別與簽章凍結，行為由 Task 14 交付。

use sw_proto::pycompat::PyErr;
use sw_proto::pyjson::PyDict;

use crate::Mono;

/// `background` 命令的輸出累積（`command.result_tail` 逐段讀取）。
pub struct BackgroundCapture {
    _priv: (),
}

impl BackgroundCapture {
    pub fn add_chunk(&mut self, _chunk: &str, _max_chars: usize) {
        crate::s21_todo!("T14", "BackgroundCapture::add_chunk")
    }
    pub fn maybe_finalize(&mut self, _now: Mono) {
        crate::s21_todo!("T14", "BackgroundCapture::maybe_finalize")
    }
    pub fn result(&mut self, _from_chunk: i128, _limit: i128, _now: Mono) -> Result<PyDict, PyErr> {
        crate::s21_todo!("T14", "BackgroundCapture::result")
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
