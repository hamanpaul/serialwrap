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
