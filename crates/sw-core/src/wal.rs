//! P5-T7：WalWriter（BC-01、DF-03、DF-17）。
//!
//! 紅燈階段骨架：公開型別與簽章依 `06-slice21-daemon-core.md` Task 7 Produces 全部宣告（含 T9 已使用的
//! `WalConfig`、`WalHealthSink`、`WalAppendReq`、`WalAppend`、`WalWriter::{open, append}`，簽章保持不變），
//! 本體皆為 `s21_todo!`（即 `unimplemented!`），由 Task 7 實作者補上。

use std::path::PathBuf;
use std::sync::Arc;

use sw_pal::{Clock, PalError, PalResult, WalFs};
use sw_proto::pyjson::PyDict;

pub struct WalConfig {
    pub dir: PathBuf,
    pub rotate_bytes: u64,
}

/// 全域健康狀態的接收端（Fence 實作）。
pub trait WalHealthSink: Send + Sync {
    fn set_wal_unhealthy(&self, unhealthy: bool);
}

/// 全域唯一 sink：持有所有 session fence 的 Weak 參照；狀態變化時逐一更新，
/// `register` 時以目前狀態初始化新 fence（新建 session 繼承目前值）。
pub struct WalHealthFanout {
    _stub: (),
}

impl WalHealthFanout {
    pub fn new() -> Arc<WalHealthFanout> {
        s21_todo!("T7", "WalHealthFanout::new")
    }

    pub fn register(&self, _sink: Arc<dyn WalHealthSink>) {
        s21_todo!("T7", "WalHealthFanout::register")
    }

    pub fn current(&self) -> bool {
        s21_todo!("T7", "WalHealthFanout::current")
    }
}

impl WalHealthSink for WalHealthFanout {
    fn set_wal_unhealthy(&self, _unhealthy: bool) {
        s21_todo!("T7", "WalHealthFanout::set_wal_unhealthy")
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
    pub healthy: bool,
    /// `"recreate_failed"` | `"write_failed"` | `"dir_unwritable"` | `None`。
    pub unhealthy_reason: Option<&'static str>,
}

impl WalHealth {
    /// key 順序照 04 §1.10 ＋ P1 T05 第 11 鍵 `unhealthy_reason`。
    pub fn to_pydict(&self) -> PyDict {
        s21_todo!("T7", "WalHealth::to_pydict")
    }
}

pub struct WalReset {
    pub previous_seq: u64,
    /// WAL 實際歸檔後綴（含 `.N`）；mirror 後綴各自計算。
    pub rotated_suffix: String,
}

pub struct WalWriter {
    _stub: (),
}

impl WalWriter {
    /// 不回錯；失敗反映在 health（WAL 檔案 lazy open）。
    pub fn open(_cfg: WalConfig, _fs: Arc<dyn WalFs>, _clock: Arc<dyn Clock>) -> WalWriter {
        s21_todo!("T7", "WalWriter::open")
    }

    /// 只呼叫一次（SessionManager 建立時）。
    pub fn set_health_sink(&self, _sink: Arc<WalHealthFanout>) {
        s21_todo!("T7", "WalWriter::set_health_sink")
    }

    pub fn append(&self, _req: WalAppendReq<'_>) -> WalAppend {
        s21_todo!("T7", "WalWriter::append")
    }

    pub fn current_seq(&self) -> u64 {
        s21_todo!("T7", "WalWriter::current_seq")
    }

    pub fn health(&self) -> WalHealth {
        s21_todo!("T7", "WalWriter::health")
    }

    pub fn reset(&self) -> Result<WalReset, PalError> {
        s21_todo!("T7", "WalWriter::reset")
    }

    pub fn tail_raw_with_meta(
        &self,
        _from_seq: Option<i128>,
        _com: Option<&str>,
        _limit: i128,
    ) -> PalResult<(Vec<PyDict>, bool)> {
        s21_todo!("T7", "WalWriter::tail_raw_with_meta")
    }

    pub fn available_from_seq(&self) -> Option<i128> {
        s21_todo!("T7", "WalWriter::available_from_seq")
    }

    pub fn wal_path(&self) -> PathBuf {
        s21_todo!("T7", "WalWriter::wal_path")
    }

    pub fn mirror_path(&self) -> PathBuf {
        s21_todo!("T7", "WalWriter::mirror_path")
    }

    pub fn shutdown(&self) -> PalResult<()> {
        s21_todo!("T7", "WalWriter::shutdown")
    }
}

/// 把 WAL 紀錄列解碼為人類可讀文字行（WAL-1.11 KEEP：partial 尾段保留為最後一行）。
pub fn rows_to_text_lines(_rows: &[PyDict]) -> Vec<String> {
    s21_todo!("T7", "rows_to_text_lines")
}
