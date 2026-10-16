//! CLI 輸出函式（`_print`、錯誤行、版本警告）：P3-T11 紅燈 stub。
#![allow(unused_variables)]

use crate::ctx::TextIo;
use sw_proto::error_codes::ErrorCode;
use sw_proto::pyjson::PyDict;

/// 組裝錯誤回應（P3-T8 stub；P3-T11 完整實作）。
pub fn error_resp(code: ErrorCode, message: Option<&str>) -> PyDict {
    todo!("P3-T8: error_resp")
}


/// Q-01：`json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",",":"))` 後接 `\n`。
pub fn print_json(io: &mut TextIo, v: &PyDict) -> std::io::Result<()> {
    todo!("P3-T11: print_json")
}

/// Q-02：錯誤行（不含換行）。
pub fn format_err_line(resp: &PyDict, context: Option<&str>) -> String {
    todo!("P3-T11: format_err_line")
}

pub fn mirror_err(io: &mut TextIo, resp: &PyDict, context: Option<&str>) -> std::io::Result<()> {
    todo!("P3-T11: mirror_err")
}

/// Q-28：版本警告。
pub fn warn_version_mismatch(io: &mut TextIo, resp: &PyDict, cli_version: &str) -> std::io::Result<()> {
    todo!("P3-T11: warn_version_mismatch")
}

/// stdout＋mirror，回傳 1。
pub fn print_internal_error(io: &mut TextIo, context: &str, message: &str) -> i32 {
    todo!("P3-T11: print_internal_error")
}
