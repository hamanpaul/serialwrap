//! RPC 指令組包與執行（P3-T14）：`cli.py:2491-2673` 的 `request_for` 對照與 `cli.py:932-956` 的 `_run_rpc`。
//!
//! - 組包為平面 `match ns.path()`；params 的 key 順序即 wire 順序（`RPC_WIRE` 不排序）。
//! - None 處理（Q-24）：`text()` 對 `None` 送 `null`；`truthy_text()` 對假值省略 key；`present()` 只判斷是否為 `None`。
//! - `cmd submit` 的本地 completion regex 檢查（Q-25）由 [`check_completion_regex`] 於分派時執行，早於組包。

use crate::argparse::{ArgValue, Namespace};
use crate::cli_trace::{format_trace_line, resolve_cli_trace_level, trace_enabled};
use crate::client::{self, ClientCrash};
use crate::commands::daemon_cmd::{self, AutostartDecision};
use crate::ctx::CliCtx;
use crate::endpoint_resolve::{
    print_endpoint_resolution_error, resolve_endpoint_info, EndpointArgs, ResolveFailure,
};
use crate::output::{error_resp, format_err_line, print_json, warn_version_mismatch};
use sw_proto::constants::{DEFAULT_RPC_TIMEOUT_S, LONG_RPC_METHODS, LONG_RPC_TIMEOUT_FLOOR_S};
use sw_proto::error_codes::ErrorCode;
use sw_proto::pyjson::{PyDict, PyInt, PyValue};
use sw_proto::pyre::{completion_regex_check, CompletionCheck};

/// 回傳 `(method, params)`；非 RPC 指令回傳 `None`。
pub fn request_for(ns: &Namespace) -> Option<(&'static str, PyValue)> {
    let path = ns.path();
    let request = match path.as_slice() {
        ["device", "list"] => ("device.list", obj(vec![])),
        ["device", "release"] => (
            "device.release",
            obj(vec![
                ("selector", text(ns, "selector")),
                ("source", text(ns, "source")),
                ("reason", text(ns, "reason")),
            ]),
        ),
        ["device", "attach"] => (
            "device.attach",
            obj(vec![
                ("selector", text(ns, "selector")),
                ("force", flag(ns, "force")),
            ]),
        ),
        ["session", "list"] => ("session.list", obj(vec![])),
        ["session", "clear"] => ("session.clear", selector_only(ns)),
        ["session", "bind"] => (
            "session.bind",
            obj(vec![
                ("selector", text(ns, "selector")),
                ("device_by_id", text(ns, "device_by_id")),
            ]),
        ),
        ["session", "pin"] => (
            "session.pin",
            obj(vec![
                ("selector", text(ns, "selector")),
                ("profile", text(ns, "profile")),
            ]),
        ),
        ["session", "unpin"] => ("session.unpin", selector_only(ns)),
        ["session", "attach"] => ("session.attach", selector_only(ns)),
        ["session", "self-test"] => (
            "session.self_test",
            obj(vec![
                ("selector", text(ns, "selector")),
                ("timeout_s", float(ns, "probe_timeout_s")),
                ("strict_human_lock", flag(ns, "strict_human_lock")),
            ]),
        ),
        ["session", "activity"] => ("session.activity", selector_only(ns)),
        ["session", "recover"] => (
            "session.recover",
            obj(vec![
                ("selector", text(ns, "selector")),
                ("timeout_s", float(ns, "recover_timeout_s")),
                ("force", flag(ns, "force")),
            ]),
        ),
        ["session", "console-attach"] => {
            let mut pairs = vec![("selector", text(ns, "selector"))];
            if let Some(label) = truthy_text(ns, "label") {
                pairs.push(("label", PyValue::Str(label)));
            }
            ("session.console_attach", obj(pairs))
        }
        ["session", "console-detach"] => (
            "session.console_detach",
            obj(vec![
                ("selector", text(ns, "selector")),
                ("client_id", text(ns, "client_id")),
            ]),
        ),
        ["session", "console-list"] => ("session.console_list", selector_only(ns)),
        ["session", "interactive-open"] => (
            "session.interactive_open",
            obj(vec![
                ("selector", text(ns, "selector")),
                ("owner", text(ns, "owner")),
                ("timeout_s", float(ns, "interactive_timeout_s")),
                ("command", text(ns, "command")),
                ("allow_attached", flag(ns, "allow_attached")),
            ]),
        ),
        ["session", "interactive-send"] => (
            "session.interactive_send",
            obj(vec![
                ("interactive_id", text(ns, "interactive_id")),
                ("data", text(ns, "data")),
                ("encoding", text(ns, "encoding")),
            ]),
        ),
        ["session", "interactive-status"] => (
            "session.interactive_status",
            obj(vec![
                ("interactive_id", text(ns, "interactive_id")),
                ("screen_chars", int(ns, "screen_chars")),
            ]),
        ),
        ["session", "interactive-close"] => (
            "session.interactive_close",
            obj(vec![("interactive_id", text(ns, "interactive_id"))]),
        ),
        ["session", "log-start"] => ("session.log_start", selector_only(ns)),
        ["session", "log-stop"] => ("session.log_stop", selector_only(ns)),
        ["session", "log-status"] => ("session.log_status", selector_only(ns)),
        ["alias", "list"] => ("alias.list", obj(vec![])),
        ["alias", "set"] => (
            "alias.set",
            obj(vec![
                ("session_id", text(ns, "session_id")),
                ("alias", text(ns, "alias")),
            ]),
        ),
        ["alias", "assign"] => {
            let mut pairs = vec![("by_id", text(ns, "by_id")), ("alias", text(ns, "alias"))];
            if let Some(profile) = truthy_text(ns, "profile") {
                pairs.push(("profile", PyValue::Str(profile)));
            }
            ("alias.assign", obj(pairs))
        }
        ["alias", "unassign"] => ("alias.unassign", obj(vec![("alias", text(ns, "alias"))])),
        ["cmd", "submit"] => {
            let mut pairs = vec![
                ("selector", text(ns, "selector")),
                ("cmd", text(ns, "command_text")),
                ("source", text(ns, "source")),
                ("mode", text(ns, "mode")),
                ("priority", int(ns, "priority")),
                ("timeout_s", float(ns, "cmd_timeout_s")),
            ];
            if present(ns, "expected_duration_s") {
                pairs.push(("expected_duration_s", float(ns, "expected_duration_s")));
            }
            if present(ns, "completion_regex") {
                pairs.push(("completion_regex", text(ns, "completion_regex")));
            }
            ("command.submit", obj(pairs))
        }
        ["cmd", "status"] => ("command.get", obj(vec![("cmd_id", text(ns, "cmd_id"))])),
        ["cmd", "result-tail"] => (
            "command.result_tail",
            obj(vec![
                ("cmd_id", text(ns, "cmd_id")),
                ("from_chunk", int(ns, "from_chunk")),
                ("limit", int(ns, "limit")),
            ]),
        ),
        ["cmd", "cancel"] => ("command.cancel", obj(vec![("cmd_id", text(ns, "cmd_id"))])),
        ["stream", "tail"] => {
            let mut pairs = vec![
                ("from_seq", int(ns, "from_seq")),
                ("limit", int(ns, "limit")),
            ];
            if let Some(selector) = selector_or_com(ns) {
                pairs.push(("selector", PyValue::Str(selector)));
            }
            ("result.tail", obj(pairs))
        }
        ["log", "tail-raw"] => ("log.tail_raw", log_params(ns)),
        ["log", "tail-text"] => ("log.tail_text", log_params(ns)),
        ["file", "push"] => {
            let mut pairs = vec![
                ("selector", text(ns, "selector")),
                ("local_path", text(ns, "local")),
                ("remote_path", text(ns, "remote")),
                ("chunk_size", int(ns, "chunk_size")),
                ("ack_mode", text(ns, "ack_mode")),
                ("source", text(ns, "source")),
            ];
            if present(ns, "chunk_timeout_s") {
                pairs.push(("chunk_timeout_s", float(ns, "chunk_timeout_s")));
            }
            ("file.push", obj(pairs))
        }
        ["file", "pull"] => {
            let mut pairs = vec![
                ("selector", text(ns, "selector")),
                ("remote_path", text(ns, "remote")),
                ("ack_mode", text(ns, "ack_mode")),
                ("source", text(ns, "source")),
            ];
            if present(ns, "local") {
                pairs.push(("local_path", text(ns, "local")));
            }
            if present(ns, "chunk_timeout_s") {
                pairs.push(("chunk_timeout_s", float(ns, "chunk_timeout_s")));
            }
            ("file.pull", obj(pairs))
        }
        ["wal", "export"] => (
            "wal.range",
            obj(vec![
                ("from_seq", int(ns, "from_seq")),
                ("to_seq", int(ns, "to_seq")),
                ("limit", int(ns, "limit")),
            ]),
        ),
        ["wal", "reset"] => ("wal.reset", obj(vec![])),
        ["wal", "current-seq"] => ("wal.current_seq", obj(vec![])),
        ["mcu", "patterns"] => ("mcu.patterns", obj(vec![])),
        ["mcu", "status"] => ("mcu.status", obj(vec![])),
        _ => return None,
    };
    Some(request)
}

/// Q-30：`--timeout`（全域）有給就用；否則長操作 floor 45.0、其餘 5.0。
pub fn effective_timeout_s(ns: &Namespace, method: &str) -> f64 {
    match ns.get("timeout_s") {
        ArgValue::Float(value) => *value,
        _ => {
            if LONG_RPC_METHODS.contains(&method) {
                LONG_RPC_TIMEOUT_FLOOR_S
            } else {
                DEFAULT_RPC_TIMEOUT_S
            }
        }
    }
}

/// `cli.py:932-956` `_run_rpc`：解析 endpoint → RPC → trace → stdout JSON → 錯誤行 → 版本警告。
///
/// 回傳 0（ok）／2（業務失敗）；endpoint 解析失敗回傳 1；崩潰（Python 未攔截例外）回傳 1 並只寫 stderr。
pub fn run_rpc(ctx: &mut CliCtx, ns: &Namespace, method: &str, params: PyValue) -> i32 {
    let args = EndpointArgs {
        endpoint: ns.str("endpoint"),
        socket: ns.str("socket"),
        bench: ns.str("bench"),
    };
    let resolved = match resolve_endpoint_info(ctx, &args) {
        Ok(resolved) => resolved,
        Err(ResolveFailure::Resolution(error)) => {
            // Q-19 (c)：`--bench` 的非 UTF-8 原文會回顯於錯誤訊息；Python 的 strict stdout 於輸出時失敗
            // （stdout 空、exit 1）。daemon 或檔案內容中的 NUL 不在此檢查範圍。
            if let Some(bench) = ns.str("bench") {
                if crate::argv::has_escape(bench) && error.message.contains(bench) {
                    return internal_error(ctx, crate::argv::NOT_UTF8_MESSAGE);
                }
            }
            return print_endpoint_resolution_error(ctx, &error, method);
        }
        Err(ResolveFailure::Crash(message)) => return internal_error(ctx, &message),
    };

    let timeout_s = effective_timeout_s(ns, method);
    let retries = match ns.get("retries") {
        ArgValue::Int(count) => count.clone(),
        _ => PyInt::Small(0),
    };
    let level = resolve_cli_trace_level(ctx.verbose, ctx.env.get("SERIALWRAP_LOG_LEVEL"));
    let (mut outcome, failure) = match client::call_with_failure(
        ctx,
        &resolved.endpoint,
        method,
        &params,
        timeout_s,
        &retries,
    ) {
        Ok(result) => result,
        Err(ClientCrash::InvalidTimeout(message)) | Err(ClientCrash::EncodeError(message)) => {
            return internal_error(ctx, &message);
        }
    };

    match daemon_cmd::maybe_autostart_on_demand(
        ctx,
        method,
        &resolved,
        failure.as_ref(),
        &outcome.resp,
    ) {
        AutostartDecision::Started { pid } => {
            let _ = ctx.io.err_str(&format!(
                "serialwrap: on-demand：預設 endpoint {} 沒有 daemon，已自動啟動（pid={pid}）\n",
                resolved.endpoint
            ));
            match client::call(
                ctx,
                &resolved.endpoint,
                method,
                &params,
                timeout_s,
                &PyInt::Small(0),
            ) {
                Ok(retry) => outcome = retry,
                Err(ClientCrash::InvalidTimeout(message) | ClientCrash::EncodeError(message)) => {
                    return internal_error(ctx, &message);
                }
            }
        }
        AutostartDecision::AlreadyUp => {
            match client::call(
                ctx,
                &resolved.endpoint,
                method,
                &params,
                timeout_s,
                &PyInt::Small(0),
            ) {
                Ok(retry) => outcome = retry,
                Err(ClientCrash::InvalidTimeout(message) | ClientCrash::EncodeError(message)) => {
                    return internal_error(ctx, &message);
                }
            }
        }
        AutostartDecision::Failed(failure) => {
            let code = match failure.get("error_code") {
                Some(PyValue::Str(value)) => value.as_str(),
                _ => ErrorCode::InternalError.as_str(),
            };
            let _ = ctx
                .io
                .err_str(&format!("serialwrap: on-demand 自動啟動失敗: {code}\n"));
        }
        AutostartDecision::NotApplicable => {}
    }

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
    let _ = print_json(&mut ctx.io, &resp);
    let ok = resp.get_truthy("ok");
    if !ok {
        let _ = ctx
            .io
            .err_str(&format!("{}\n", format_err_line(&resp, Some(method))));
    }
    let _ = warn_version_mismatch(&mut ctx.io, &resp, ctx.cli_version);
    if ok {
        0
    } else {
        2
    }
}

/// `cmd submit` 的本地 completion regex 檢查（Q-25）。
///
/// 回傳 `None` 表示通過；`Some(code)` 表示已輸出並以該 exit code 結束（不連 daemon）。
/// 無效 regex → stdout `{"error_code":"INVALID_COMPLETION_REGEX","ok":false}`、exit 1、stderr 空；
/// 含非 UTF-8 原文者（Python `valid_completion_regex` 的 `UnicodeEncodeError`）同樣視為無效；
/// 未攔截的 OverflowError（Task 5 `Crash`）→ stderr 一行、stdout 空、exit 1。
pub fn check_completion_regex(ctx: &mut CliCtx, ns: &Namespace) -> Option<i32> {
    let check = match ns.get("completion_regex") {
        ArgValue::Str(text) if crate::argv::has_escape(text) => CompletionCheck::Invalid,
        ArgValue::Str(text) => completion_regex_check(Some(&PyValue::Str(text.clone()))),
        _ => completion_regex_check(None),
    };
    match check {
        CompletionCheck::Valid => None,
        CompletionCheck::Invalid => {
            let resp = error_resp(ErrorCode::InvalidCompletionRegex, None);
            let _ = print_json(&mut ctx.io, &resp);
            Some(1)
        }
        CompletionCheck::Crash(message) => Some(internal_error(ctx, &message)),
    }
}

/// Python 未攔截例外的 stderr 形式（exit 1，stdout 不寫）。
pub fn internal_error(ctx: &mut CliCtx, message: &str) -> i32 {
    let _ = ctx
        .io
        .err_str(&format!("serialwrap: internal error: {message}\n"));
    1
}

fn obj(pairs: Vec<(&str, PyValue)>) -> PyValue {
    PyValue::Dict(PyDict::from_pairs(
        pairs
            .into_iter()
            .map(|(key, value)| (key.to_string(), value))
            .collect(),
    ))
}

fn selector_only(ns: &Namespace) -> PyValue {
    obj(vec![("selector", text(ns, "selector"))])
}

fn log_params(ns: &Namespace) -> PyValue {
    let mut pairs = vec![("limit", int(ns, "limit"))];
    if present(ns, "from_seq") {
        pairs.push(("from_seq", int(ns, "from_seq")));
    }
    if let Some(selector) = selector_or_com(ns) {
        pairs.push(("selector", PyValue::Str(selector)));
    }
    obj(pairs)
}

/// `args.selector or args.com`（Python 假值語意）。
fn selector_or_com(ns: &Namespace) -> Option<String> {
    truthy_text(ns, "selector").or_else(|| truthy_text(ns, "com"))
}

fn text(ns: &Namespace, dest: &str) -> PyValue {
    match ns.get(dest) {
        ArgValue::Str(value) => PyValue::Str(value.clone()),
        _ => PyValue::Null,
    }
}

fn truthy_text(ns: &Namespace, dest: &str) -> Option<String> {
    match ns.get(dest) {
        ArgValue::Str(value) if !value.is_empty() => Some(value.clone()),
        _ => None,
    }
}

fn flag(ns: &Namespace, dest: &str) -> PyValue {
    PyValue::Bool(matches!(ns.get(dest), ArgValue::Bool(true)))
}

fn int(ns: &Namespace, dest: &str) -> PyValue {
    match ns.get(dest) {
        ArgValue::Int(value) => PyValue::Int(value.clone()),
        _ => PyValue::Null,
    }
}

fn float(ns: &Namespace, dest: &str) -> PyValue {
    match ns.get(dest) {
        ArgValue::Float(value) => PyValue::Float(*value),
        _ => PyValue::Null,
    }
}

fn present(ns: &Namespace, dest: &str) -> bool {
    !matches!(ns.get(dest), ArgValue::None)
}
