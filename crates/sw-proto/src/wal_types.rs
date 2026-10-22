//! WAL 記錄型別與序列化（P3-T7；arch §3.8）。
//!
//! 線路格式與 Python `sw_core/wal.py::append` 的 record dict 一致：磁碟穩定序列化（[`crate::pyjson::DISK_STABLE`]），
//! 鍵排序、緊湊分隔、非 ASCII 原樣輸出。golden：`contracts/golden/wal_line.tsv`（`scripts/rust/goldens/gen_wal_line.py`）。

use crate::pyjson::{self, PyDict, PyInt, PyValue, DISK_STABLE};

/// 單筆 WAL 紀錄（欄位與 Python record dict 對應；`crc32_hex` 對應鍵 `crc32`）。
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
    /// 僅在 `Some` 時輸出鍵 `rotation_failed`（Python 為條件式鍵，常態紀錄不含此鍵）。
    pub rotation_failed: Option<bool>,
}

fn opt_str(v: &Option<String>) -> PyValue {
    match v {
        Some(s) => PyValue::Str(s.clone()),
        None => PyValue::Null,
    }
}

impl WalRecord {
    /// 序列化為一行（不含換行）。`rotation_failed` 為 `None` 時不輸出該鍵。
    pub fn to_line(&self) -> String {
        let mut fields: Vec<(String, PyValue)> = vec![
            (
                "seq".into(),
                PyValue::Int(PyInt::Small(i128::from(self.seq))),
            ),
            (
                "mono_ts_ns".into(),
                PyValue::Int(PyInt::Small(i128::from(self.mono_ts_ns))),
            ),
            ("wall_ts".into(), PyValue::Str(self.wall_ts.clone())),
            ("com".into(), opt_str(&self.com)),
            ("dir".into(), PyValue::Str(self.dir.clone())),
            ("source".into(), PyValue::Str(self.source.clone())),
            ("cmd_id".into(), opt_str(&self.cmd_id)),
            (
                "len".into(),
                PyValue::Int(PyInt::Small(i128::from(self.len))),
            ),
            ("crc32".into(), PyValue::Str(self.crc32_hex.clone())),
            ("payload_b64".into(), PyValue::Str(self.payload_b64.clone())),
            ("loss_flag".into(), PyValue::Bool(self.loss_flag)),
            ("meta".into(), PyValue::Dict(self.meta.clone())),
        ];
        if let Some(rf) = self.rotation_failed {
            fields.push(("rotation_failed".into(), PyValue::Bool(rf)));
        }
        pyjson::dumps(&PyValue::Dict(PyDict::from_pairs(fields)), &DISK_STABLE)
    }
}

/// `WAL_RECREATED_AFTER_EXTERNAL_UNLINK` meta 事件（arch §3.8，BC-01）。
///
/// 恰五個鍵，鍵順序與 Python `sw_core/wal.py`（約 437～445 行）及 arch §3.8 一致。
/// 已知區間的判定：`range` 為 `Some((first, last))` 且 `1 <= first <= last` 時 `range_known=true`，
/// `potentially_lost_seq_range=[first, last]`；否則 `range_known=false` 且為 `null`（不得猜測）。
/// 此判定同時滿足 Python 的 `last_seq >= first_seq`（可達狀態下 `first >= 1`，因 `_file_first_seq = seq + 1`）
/// 與 `contracts/wal_meta_events.schema.json` 的 `x-constraint: first <= last` 及 `minimum: 1`。
/// `previous_identity` 為 `None` 時為 `null`。
pub fn wal_recreated_meta(
    reason: &str,
    previous_identity: Option<&str>,
    range: Option<(u64, u64)>,
) -> PyDict {
    let previous = match previous_identity {
        Some(s) => PyValue::Str(s.to_string()),
        None => PyValue::Null,
    };
    let known = range.filter(|&(first, last)| first >= 1 && first <= last);
    let lost_range = match known {
        Some((first, last)) => PyValue::List(vec![
            PyValue::Int(PyInt::Small(i128::from(first))),
            PyValue::Int(PyInt::Small(i128::from(last))),
        ]),
        None => PyValue::Null,
    };
    PyDict::from_pairs(vec![
        (
            "event".into(),
            PyValue::Str("WAL_RECREATED_AFTER_EXTERNAL_UNLINK".into()),
        ),
        ("reason".into(), PyValue::Str(reason.to_string())),
        ("previous_identity".into(), previous),
        ("potentially_lost_seq_range".into(), lost_range),
        ("range_known".into(), PyValue::Bool(known.is_some())),
    ])
}

/// WAL 列的 CRC32（小寫 8 位十六進位；Task 0c，arch §1.2）。
pub fn crc32_hex(_bytes: &[u8]) -> String {
    crate::s21_todo!("T0c", "crc32_hex")
}
