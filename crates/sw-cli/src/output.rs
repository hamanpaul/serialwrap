//! CLI 輸出函式（對應 `sw_core/cli.py` 的 `_print`、`_format_err_line`、`_mirror_err`、
//! `_warn_version_mismatch`、`_print_internal_error`）。`error_resp` 於 P3-T8 實作，其餘於 P3-T11 承接。

use crate::ctx::TextIo;
use sw_proto::error_codes::ErrorCode;
use sw_proto::pycompat::{py_strip, py_truthy};
use sw_proto::pyfmt::py_str;
use sw_proto::pyjson::{dumps_dict, PyDict, PyValue, CLI_STDOUT};

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
///
/// JSON 字串內的換行已被跳脫，因此 `\n` 轉換只會作用於行尾。
///
/// 根 dict 以借用方式序列化（不複製整棵回應）；JSON 與換行由 [`TextIo::out_str`] 一次寫出，
/// 寫入時機與 Python 在 CRLF 模式下的 flush 邊界一致（見 `ctx.rs`）。
pub fn print_json(io: &mut TextIo, v: &PyDict) -> std::io::Result<()> {
    let mut line = dumps_dict(v, &CLI_STDOUT);
    line.push('\n');
    io.out_str(&line)
}

/// Q-02：錯誤行（不含換行）。形狀照 `_format_err_line`：`serialwrap: {context} failed: {code}`，
/// `message` 有值時追加 `: {message}`，`hint` 有值時追加 `（hint: {hint}）`。
///
/// `error_code` 為假值時改用 `message` 頂替（#94 fallback），兩者皆假值時為 `UNKNOWN_ERROR`。
pub fn format_err_line(resp: &PyDict, context: Option<&str>) -> String {
    let error_code = resp.get("error_code").filter(|v| py_truthy(v));
    let message = resp.get("message").filter(|v| py_truthy(v));
    let hint = resp.get("hint").filter(|v| py_truthy(v));

    let (code_str, message) = match (error_code, message) {
        (Some(code), msg) => (py_str(code), msg),
        (None, Some(msg)) => (py_str(msg), None),
        (None, None) => ("UNKNOWN_ERROR".to_string(), None),
    };

    let prefix = match context {
        Some(c) if !c.is_empty() => format!("{c} "),
        _ => String::new(),
    };
    let mut line = format!("serialwrap: {prefix}failed: {code_str}");
    if let Some(msg) = message {
        line.push_str(": ");
        line.push_str(&py_str(msg));
    }
    if let Some(h) = hint {
        line.push_str(&format!("（hint: {}）", py_str(h)));
    }
    line
}

/// `_mirror_err`：`ok` 為真時不輸出；否則在 stderr 寫一行 [`format_err_line`]。
pub fn mirror_err(io: &mut TextIo, resp: &PyDict, context: Option<&str>) -> std::io::Result<()> {
    if resp.get_truthy("ok") {
        return Ok(());
    }
    io.err_str(&format!("{}\n", format_err_line(resp, context)))
}

/// Q-28：版本警告（`_warn_version_mismatch`）。
///
/// 回應 `version` 非字串或 strip 後為空時靜默略過；CLI 版本為 `unknown` 或與 daemon 版本相同時亦不輸出。
pub fn warn_version_mismatch(io: &mut TextIo, resp: &PyDict, cli_version: &str) -> std::io::Result<()> {
    let daemon_version = match resp.get("version") {
        Some(PyValue::Str(s)) if !py_strip(s).is_empty() => s.as_str(),
        _ => return Ok(()),
    };
    if cli_version == "unknown" || cli_version == daemon_version {
        return Ok(());
    }
    io.err_str(&format!(
        "serialwrap: client 版本 {cli_version} 與 daemon 版本 {daemon_version} 不一致\
（建議統一安裝來源或重啟 daemon 對齊版本；serialwrap doctor 可查其他安裝）\n"
    ))
}

/// `_print_internal_error`：stdout 寫入 `INTERNAL_ERROR` 回應，stderr 鏡射一行，回傳 1。
///
/// 回傳型別為 `i32`，寫入失敗於此處忽略（與 Python 的 `_print` 不同，無法向上拋出）；
/// 真正的 stdout 寫入失敗由組裝點（P3-T14 `run_process`）的 exit code 規則處理。
pub fn print_internal_error(io: &mut TextIo, context: &str, message: &str) -> i32 {
    let resp = error_resp(ErrorCode::InternalError, Some(message));
    let _ = print_json(io, &resp);
    let _ = mirror_err(io, &resp, Some(context));
    1
}
