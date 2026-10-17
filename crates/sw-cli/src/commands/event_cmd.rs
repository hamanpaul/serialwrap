//! `event` 群組（`cli.py:965-1078`）：10 個子指令的 RPC 組包與分派（P3-T15）。
//!
//! - 方法對映與 Python `_EVENT_CMD_METHOD` 逐項一致；未知子指令回 `UNKNOWN_EVENT_CMD`（exit 2）。
//! - `event.*` 不在重試白名單、也不是長操作：`--retries` 不轉送（Q-31），timeout 只取顯式 `--timeout`，否則 5.0（Q-30）。
//! - 與 `_run_rpc` 不同，Python `_dispatch_event` 以 `except Exception` 包住整段：`add` 讀檔／解析失敗與 RPC 階段的
//!   例外皆輸出 stdout `INTERNAL_ERROR` JSON（exit 1）；寫入順序為 trace → stdout → 版本警告 → 錯誤行（Q-03）。

use std::path::PathBuf;

use crate::argparse::{ArgValue, Namespace};
use crate::argv;
use crate::cli_trace::{format_trace_line, resolve_cli_trace_level, trace_enabled};
use crate::client::{self, ClientCrash, ParamOrigin};
use crate::commands::rpc_cmds::{effective_timeout_s, internal_error};
use crate::ctx::CliCtx;
use crate::endpoint_resolve::{
    print_endpoint_resolution_error, resolve_endpoint_info, EndpointArgs, ResolveFailure,
};
use crate::output::{mirror_err, print_internal_error, print_json, warn_version_mismatch};
use sw_proto::error_codes::ErrorCode;
use sw_proto::pycompat::{decode_utf8_strict, universal_newlines};
use sw_proto::pyfmt::{py_repr_argv, surrogate_of_escape};
use sw_proto::pyjson::{loads, PyDict, PyInt, PyValue};

/// 子指令名 → RPC method（與 Python `_EVENT_CMD_METHOD` 同序、同值）。
pub const EVENT_CMD_METHOD: [(&str, &str); 10] = [
    ("add", "event.rule_set"),
    ("rm", "event.rule_delete"),
    ("list", "event.rule_list"),
    ("show", "event.rule_get"),
    ("enable", "event.com_enable"),
    ("disable", "event.com_disable"),
    ("status", "event.com_status"),
    ("reset", "event.reset"),
    ("reload", "event.reload"),
    ("tail", "event.tail"),
];

/// 組裝 RPC params（`_dispatch_event` 各分支）。
///
/// `Err(message)` 為 Python `except Exception` 的 `str(exc)`，呼叫端以 `INTERNAL_ERROR` 輸出。
/// `add` 依序：`FsRead::read`（錯誤訊息照 `open()`，含檔名 repr）→ UTF-8 strict 解碼 → universal newlines → `loads`。
/// 未知子指令回 `Ok(PyValue::Null)`；分派層已先行攔截，此分支不會被 CLI 路徑觸發。
pub fn event_params(ctx: &CliCtx, ns: &Namespace) -> Result<PyValue, String> {
    let params = match ns.str("event_cmd").unwrap_or("") {
        "add" => return read_rule_file(ctx, ns.str("file").unwrap_or("")),
        "rm" | "show" => obj(vec![("rule_id", text(ns, "rule_id"))]),
        "list" => obj(vec![
            ("selector", text(ns, "selector")),
            ("owner", text(ns, "owner")),
        ]),
        "enable" | "disable" | "status" => obj(vec![("selector", text(ns, "selector"))]),
        "reset" => obj(vec![
            ("rule_id", text(ns, "rule_id")),
            ("selector", text(ns, "selector")),
        ]),
        "reload" => obj(vec![]),
        "tail" => obj(vec![
            ("rule_id", text(ns, "rule_id")),
            ("selector", text(ns, "selector")),
            ("n", int(ns, "n")),
            ("since_ts", int(ns, "since")),
        ]),
        _ => PyValue::Null,
    };
    Ok(params)
}

/// `cli.py:_dispatch_event` 逐步對照：endpoint 解析 → 子指令分支 → RPC → trace → stdout → 版本警告 → 錯誤行。
pub fn dispatch_event(ctx: &mut CliCtx, ns: &Namespace) -> i32 {
    let subcommand = ns.str("event_cmd");
    let method = subcommand.and_then(|name| {
        EVENT_CMD_METHOD
            .iter()
            .find(|(cmd, _)| *cmd == name)
            .map(|(_, method)| *method)
    });
    let context = match (method, subcommand) {
        (Some(method), _) => method.to_string(),
        (None, Some(name)) => format!("event.{name}"),
        (None, None) => "event.None".to_string(),
    };

    let args = EndpointArgs {
        endpoint: ns.str("endpoint"),
        socket: ns.str("socket"),
        bench: ns.str("bench"),
    };
    let resolved = match resolve_endpoint_info(ctx, &args) {
        Ok(resolved) => resolved,
        Err(ResolveFailure::Resolution(error)) => {
            // Q-19 (c)：`--bench` 原文出現於錯誤訊息時，Python 的 strict stdout 於輸出時失敗（traceback、exit 1、stdout 空）。
            if let Some(bench) = ns.str("bench") {
                if argv::has_escape(bench) && error.message.contains(bench) {
                    return internal_error(ctx, argv::NOT_UTF8_MESSAGE);
                }
            }
            return print_endpoint_resolution_error(ctx, &error, &context);
        }
        Err(ResolveFailure::Crash(message)) => {
            return print_internal_error(&mut ctx.io, &context, python_exc_text(&message));
        }
    };

    let Some(method) = method else {
        return print_unknown_command(ctx, subcommand);
    };

    let timeout_s = effective_timeout_s(ns, method);
    let params = match event_params(ctx, ns) {
        Ok(params) => params,
        Err(message) => return print_internal_error(&mut ctx.io, &context, &message),
    };
    // `add` 的參數來自檔案內容（JSON），其 NUL 等碼位是合法資料；其餘子指令的參數來自 argv（見 `ParamOrigin`）。
    let origin = match subcommand {
        Some("add") => ParamOrigin::Document,
        _ => ParamOrigin::Argv,
    };

    let outcome = match client::rpc_call_origin(
        ctx.p,
        &resolved.endpoint,
        method,
        &params,
        1,
        timeout_s,
        &PyInt::Small(0),
        origin,
    ) {
        Ok(outcome) => outcome,
        // Python 的 `settimeout` 例外文字依數值類別而異；`client` 僅回報類別，文字在此依 CPython 3.12.3 重現。
        Err(ClientCrash::InvalidTimeout(_)) => {
            return print_internal_error(&mut ctx.io, &context, python_timeout_message(timeout_s));
        }
        // 參數含非 UTF-8 原文：Python 於 `json.dumps(...).encode("utf-8")` 拋 UnicodeEncodeError，文字含位置（Q-19 b）。
        Err(ClientCrash::EncodeError(message)) => {
            let text = if origin == ParamOrigin::Argv && argv::value_is_tainted(&params) {
                encode_error_text(method, &params).unwrap_or(message)
            } else {
                message
            };
            return print_internal_error(&mut ctx.io, &context, &text);
        }
    };

    let level = resolve_cli_trace_level(ctx.verbose, ctx.env.get("SERIALWRAP_LOG_LEVEL"));
    if trace_enabled(&level) {
        let line = format_trace_line(
            &resolved.endpoint,
            resolved.source,
            method,
            timeout_s,
            &outcome.resp,
            &outcome.meta,
        );
        let _ = ctx.io.err_str(&format!("{line}\n"));
    }

    let resp = outcome.resp;
    // Python：`_print` 拋例外時由外層 `except Exception` 接手，改印 INTERNAL_ERROR 並略過版本警告與錯誤行，回 1。
    // 最終 flush 是否失敗才決定 exit 120（見 `lib.rs::run_process_with`）。
    if let Err(error) = print_json(&mut ctx.io, &resp) {
        return print_internal_error(&mut ctx.io, &context, &py_io_error_text(&error));
    }
    let _ = warn_version_mismatch(&mut ctx.io, &resp, sw_proto::version::version());
    let _ = mirror_err(&mut ctx.io, &resp, Some(&context));
    if resp.get_truthy("ok") {
        0
    } else {
        2
    }
}

/// `add --file` 的讀取與解析（`cli.py:_dispatch_event` 的 `open` + `json.load`）。
fn read_rule_file(ctx: &CliCtx, file: &str) -> Result<PyValue, String> {
    let path = PathBuf::from(argv::decode(file));
    let bytes = ctx
        .p
        .fs
        .read(&path)
        .map_err(|error| format!("{}: {}", error.py_message, py_repr_argv(file)))?;
    let text = decode_utf8_strict(&bytes).map_err(|error| error.to_string())?;
    let value = loads(&universal_newlines(&text)).map_err(|error| error.to_string())?;
    check_int_digits(&value)?;
    Ok(value)
}

/// CPython 3.12 預設的整數字串轉換位數上限（CVE-2020-10735）。
const INT_MAX_STR_DIGITS: usize = 4300;

/// `json.load` 遇到超過 [`INT_MAX_STR_DIGITS`] 位數的整數字面量時拋 `ValueError`。
///
/// 此處於解析成功後檢查同一棵值樹（與 Python 逐字元解析的差異僅在「同一文件同時含超長整數與較後方的語法錯誤」時，
/// 報出的錯誤類別不同；兩者皆為 INTERNAL_ERROR、皆零請求送出）。
fn check_int_digits(value: &PyValue) -> Result<(), String> {
    match value {
        PyValue::Int(PyInt::Big(text)) => {
            let digits = text.trim_start_matches('-').len();
            if digits > INT_MAX_STR_DIGITS {
                return Err(format!(
                    "Exceeds the limit ({INT_MAX_STR_DIGITS} digits) for integer string conversion: value has {digits} digits; use sys.set_int_max_str_digits() to increase the limit"
                ));
            }
            Ok(())
        }
        PyValue::List(items) => items.iter().try_for_each(check_int_digits),
        PyValue::Dict(dict) => dict.0.values().try_for_each(check_int_digits),
        _ => Ok(()),
    }
}

/// Python `str(OSError)`（`[Errno N] strerror`）。Rust 的 `Display` 會追加 ` (os error N)`，於此去除。
fn py_io_error_text(error: &std::io::Error) -> String {
    match error.raw_os_error() {
        Some(code) => {
            let display = error.to_string();
            let strerror = display
                .strip_suffix(&format!(" (os error {code})"))
                .unwrap_or(&display);
            format!("[Errno {code}] {strerror}")
        }
        None => error.to_string(),
    }
}

/// 未知子指令（Python `else` 分支）：stdout `UNKNOWN_EVENT_CMD` 回應、stderr 鏡射、exit 2。
fn print_unknown_command(ctx: &mut CliCtx, name: Option<&str>) -> i32 {
    let mut resp = PyDict::new();
    resp.insert("ok", PyValue::Bool(false));
    resp.insert(
        "error_code",
        PyValue::Str(ErrorCode::UnknownEventCmd.as_str().to_string()),
    );
    resp.insert(
        "cmd",
        name.map_or(PyValue::Null, |value| PyValue::Str(value.to_string())),
    );
    let _ = print_json(&mut ctx.io, &resp);
    let _ = mirror_err(&mut ctx.io, &resp, Some("event"));
    2
}

/// CPython 3.12.3 `socket.settimeout` 對非法值的 `str(exc)`（已於 3.12.3 實測）：
/// NaN → `ValueError: Invalid value NaN (not a number)`；有限負值 → `ValueError: Timeout value out of range`；
/// ±inf 與超出 `_PyTime_t` 範圍 → `OverflowError: timestamp out of range for platform time_t`。
fn python_timeout_message(timeout_s: f64) -> &'static str {
    const PYTIME_NS_LIMIT: f64 = 9_223_372_036_854_775_808.0; // 2**63
    if timeout_s.is_nan() {
        "Invalid value NaN (not a number)"
    } else if timeout_s < 0.0 && (timeout_s * 1_000_000_000.0).ceil() > -PYTIME_NS_LIMIT {
        "Timeout value out of range"
    } else {
        "timestamp out of range for platform time_t"
    }
}

/// Python `json.dumps(req, ensure_ascii=False).encode("utf-8")` 遇到 lone surrogate 的 `UnicodeEncodeError` 文字。
///
/// 位置為 Python 字元索引：線路格式中，跳脫的 surrogate 為 `\u0000`（6 字元）加偏移字元，在此合併計為 1。
/// 連續的 surrogate 與 CPython 相同合併為區間（`characters in position a-b`）。
/// 請求的 `id` 與 [`dispatch_event`] 送出者相同（1）。
fn encode_error_text(method: &str, params: &PyValue) -> Option<String> {
    let encoded = client::encode_request(1, method, params);
    let text = String::from_utf8(encoded).ok()?;
    let mut position = 0usize;
    let mut rest = text.as_str();
    while let Some(ch) = rest.chars().next() {
        if let Some((first, _)) = escaped_surrogate_at(rest) {
            let mut count = 0usize;
            let mut cursor = rest;
            while let Some((_, len)) = escaped_surrogate_at(cursor) {
                count += 1;
                cursor = &cursor[len..];
            }
            return Some(if count == 1 {
                format!(
                    "'utf-8' codec can't encode character '\\u{first:04x}' in position {position}: surrogates not allowed"
                )
            } else {
                format!(
                    "'utf-8' codec can't encode characters in position {position}-{}: surrogates not allowed",
                    position + count - 1
                )
            });
        }
        position += 1;
        rest = &rest[ch.len_utf8()..];
    }
    None
}

/// `text` 開頭為一個跳脫的 surrogate（`\u0000` ＋ 偏移字元）時，回傳其碼位與在 `text` 內佔用的位元組數。
fn escaped_surrogate_at(text: &str) -> Option<(u32, usize)> {
    let after = text.strip_prefix("\\u0000")?;
    let offset = after.chars().next()?;
    let cp = surrogate_of_escape(offset)?;
    Some((cp, "\\u0000".len() + offset.len_utf8()))
}

/// `ResolveFailure::Crash` 的訊息帶 Python 例外類別前綴（`TypeError: `）；`str(exc)` 不含該前綴。
fn python_exc_text(message: &str) -> &str {
    message.strip_prefix("TypeError: ").unwrap_or(message)
}

fn obj(pairs: Vec<(&str, PyValue)>) -> PyValue {
    PyValue::Dict(PyDict::from_pairs(
        pairs
            .into_iter()
            .map(|(key, value)| (key.to_string(), value))
            .collect(),
    ))
}

fn text(ns: &Namespace, dest: &str) -> PyValue {
    match ns.get(dest) {
        ArgValue::Str(value) => PyValue::Str(value.clone()),
        _ => PyValue::Null,
    }
}

fn int(ns: &Namespace, dest: &str) -> PyValue {
    match ns.get(dest) {
        ArgValue::Int(value) => PyValue::Int(value.clone()),
        _ => PyValue::Null,
    }
}
