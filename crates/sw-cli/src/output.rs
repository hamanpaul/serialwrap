//! CLI 輸出函式（`_print`、錯誤行、版本警告）：`error_resp` 已於 P3-T8 實作，其餘為 P3-T11 紅燈 stub。
#![allow(unused_variables)]

use crate::ctx::TextIo;
use sw_proto::error_codes::ErrorCode;
use sw_proto::pyjson::{PyDict, PyValue};

/// 組裝錯誤回應（P3-T8）：唯一入口，錯誤碼只收 `ErrorCode`，不接受任意字串。
///
/// 形狀照 `sw_core/cli.py` 失敗回應的 `ok`、`error_code`、`message` 三鍵：`error_code` 取自
/// `ErrorCode::as_str`；`message` 只在傳入 `Some` 時寫入（Python 的 `_print_internal_error` 等路徑恆帶訊息）。
pub fn error_resp(code: ErrorCode, message: Option<&str>) -> PyDict {
    let mut resp = PyDict::new();
    resp.insert("ok", PyValue::Bool(false));
    resp.insert("error_code", PyValue::Str(code.as_str().to_string()));
    if let Some(msg) = message {
        resp.insert("message", PyValue::Str(msg.to_string()));
    }
    resp
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
