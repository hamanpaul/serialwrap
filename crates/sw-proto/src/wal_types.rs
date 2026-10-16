//! WAL 記錄型別與序列化（P3-T7 stub）。
#![allow(unused_variables, dead_code)]

use crate::pyjson::PyDict;

pub struct WalRecord {
    pub seq: u64,
    pub mono_ts_ns: u64,
    pub wall_ts: String,
    pub com: Option<String>,
    pub dir: String,
    pub source: String,
    pub cmd_id: Option<String>,
    pub len: u64,
    pub crc32_hex: String,
    pub payload_b64: String,
    pub loss_flag: bool,
    pub meta: PyDict,
    pub rotation_failed: Option<bool>,
}

impl WalRecord {
    pub fn to_line(&self) -> String {
        todo!("P3-T7: WalRecord::to_line")
    }
}

pub fn wal_recreated_meta(
    reason: &str,
    previous_identity: Option<&str>,
    range: Option<(u64, u64)>,
) -> PyDict {
    todo!("P3-T7: wal_recreated_meta")
}
