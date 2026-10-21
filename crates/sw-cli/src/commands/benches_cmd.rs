//! `benches` 群組（P4-T9 原生，SC-27；07 §9.4）。
//!
//! 非 RPC、不連 daemon：讀 `benches.yaml`、讀 endpoint 記憶，並以 `remote status` 的 connect tunnel 判定 alive。

use std::collections::BTreeSet;
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::path::PathBuf;

use crate::argparse::Namespace;
use crate::bench_state;
use crate::ctx::CliCtx;
use crate::output;
use crate::remote_tunnel::{self, panic_message, TunnelError};
use sw_proto::endpoint::{is_loopback_tcp, parse_endpoint, Endpoint};
use sw_proto::error_codes::ErrorCode;
use sw_proto::pyjson::{PyDict, PyInt, PyValue};

/// 執行原生 `benches` 指令（`cli.py::_run_benches`）。
///
/// 與 Python `except Exception` 對等：執行中的 panic 轉為 stdout `INTERNAL_ERROR` JSON、stderr 鏡像、exit 1（同 P4-T8 `run_remote`）。
pub fn run_benches(ns: &Namespace, ctx: &mut CliCtx<'_>) -> i32 {
    match catch_unwind(AssertUnwindSafe(|| run_benches_body(ns, ctx))) {
        Ok(code) => code,
        Err(payload) => emit_internal_error(ctx, "benches", &panic_message(payload.as_ref())),
    }
}

fn run_benches_body(_ns: &Namespace, ctx: &mut CliCtx<'_>) -> i32 {
    match benches_response(ctx) {
        Ok(resp) => {
            let _ = output::print_json(&mut ctx.io, &resp);
            0
        }
        Err(error) => {
            let resp = tunnel_error_response(&error);
            let _ = output::print_json(&mut ctx.io, &resp);
            let _ = output::mirror_err(&mut ctx.io, &resp, Some("benches"));
            1
        }
    }
}

/// 例外（panic）→ `INTERNAL_ERROR`：stdout JSON、stderr 鏡像 `serialwrap: <context> failed: …`、exit 1。
pub(crate) fn emit_internal_error(ctx: &mut CliCtx<'_>, context: &str, message: &str) -> i32 {
    let resp = output::error_resp(ErrorCode::InternalError, Some(message));
    let _ = output::print_json(&mut ctx.io, &resp);
    let _ = output::mirror_err(&mut ctx.io, &resp, Some(context));
    1
}

/// `TunnelError` → `{"ok":false,"error_code":…,"message":…}`（message 缺省時回填 code，同 `exc.message or exc.code`）。
pub(crate) fn tunnel_error_response(error: &TunnelError) -> PyDict {
    let message = error
        .message
        .clone()
        .filter(|message| !message.is_empty())
        .unwrap_or_else(|| error.code.clone());
    PyDict::from_pairs(vec![
        ("ok".to_string(), PyValue::Bool(false)),
        ("error_code".to_string(), PyValue::Str(error.code.clone())),
        ("message".to_string(), PyValue::Str(message)),
    ])
}

fn benches_response(ctx: &mut CliCtx<'_>) -> Result<PyDict, TunnelError> {
    let configured = bench_state::load_configured_benches(ctx)
        .map_err(|error| TunnelError::coded(error.code, Some(error.message)))?;

    let path = bench_state::state_path(ctx);
    let remembered = bench_state::load_bench_state_doc(ctx, &path).map_err(state_error)?;

    let run_dir = PathBuf::from(ctx.paths.run_dir.clone());
    let mut alive_ports: BTreeSet<i128> = BTreeSet::new();
    for tunnel in status_tunnels(ctx, &run_dir)? {
        if !matches!(tunnel.get("role"), Some(PyValue::Str(role)) if role == "connect") {
            continue;
        }
        if let Some(port) = tunnel_listen_port(&tunnel) {
            alive_ports.insert(port);
        }
    }

    // `configured` 為 BTreeMap，迭代順序即 code 排序（與 Python `sorted(configured_benches)` 相同）。
    let mut rows = Vec::with_capacity(configured.len());
    for code in configured.keys() {
        let endpoint =
            bench_state::remembered_bench_endpoint(&remembered, code).map_err(|message| {
                TunnelError::coded(ErrorCode::InvalidBenchState.as_str(), Some(message))
            })?;
        let alive = remembered_alive(endpoint.as_deref(), &alive_ports);
        rows.push(PyValue::Dict(PyDict::from_pairs(vec![
            ("alive".to_string(), PyValue::Bool(alive)),
            ("code".to_string(), PyValue::Str(code.clone())),
            (
                "endpoint".to_string(),
                endpoint.map_or(PyValue::Null, PyValue::Str),
            ),
        ])));
    }

    Ok(PyDict::from_pairs(vec![
        ("ok".to_string(), PyValue::Bool(true)),
        ("benches".to_string(), PyValue::List(rows)),
    ]))
}

/// `_status_tunnels`：呼叫 `remote status` 並驗證回應（`cli.py:1212-1224`）。
pub(crate) fn status_tunnels(
    ctx: &CliCtx<'_>,
    run_dir: &std::path::Path,
) -> Result<Vec<PyDict>, TunnelError> {
    status_tunnels_from(&remote_tunnel::status(ctx, run_dir))
}

/// 由 `remote status` 回應抽取 `tunnels` 清單並驗證格式（`cli.py:1262-1276` 的 ok／tunnels 檢查，純函式）。
///
/// `ok` 為假 → 回應的 `error_code`（非空字串）或 `TUNNEL_STATUS_ERROR`，訊息取 `message`（非空字串）或「無法確認 tunnel 狀態」；
/// 缺 `tunnels` 清單 → `TUNNEL_STATUS_ERROR`。非 dict 項目略過（Python `isinstance(tunnel, dict)`）。
/// Python「status 回應非 dict」分支在 Rust 型別上不可達（`remote_tunnel::status` 回 `PyDict`），不另建分支。
pub fn status_tunnels_from(resp: &PyDict) -> Result<Vec<PyDict>, TunnelError> {
    if !resp.get_truthy("ok") {
        let code = match resp.get("error_code") {
            Some(PyValue::Str(code)) if !code.is_empty() => code.clone(),
            _ => ErrorCode::TunnelStatusError.as_str().to_string(),
        };
        let message = match resp.get("message") {
            Some(PyValue::Str(message)) if !message.is_empty() => message.clone(),
            _ => "無法確認 tunnel 狀態".to_string(),
        };
        return Err(TunnelError::coded(code, Some(message)));
    }
    let Some(PyValue::List(tunnels)) = resp.get("tunnels") else {
        return Err(TunnelError::coded(
            ErrorCode::TunnelStatusError.as_str(),
            Some("remote status 回應缺少 tunnels 清單".to_string()),
        ));
    };
    Ok(tunnels
        .iter()
        .filter_map(|tunnel| match tunnel {
            PyValue::Dict(dict) => Some(dict.clone()),
            _ => None,
        })
        .collect())
}

/// `_tunnel_listen_port`（cli.py:1196-1209）：`listen_port` 可轉為整數時採用，否則由 `control_path` 的 `cm-<n>` 推得。
pub(crate) fn tunnel_listen_port(tunnel: &PyDict) -> Option<i128> {
    if let Some(port) = int_value(tunnel.get("listen_port")) {
        return Some(port);
    }
    let Some(PyValue::Str(control_path)) = tunnel.get("control_path") else {
        return None;
    };
    let name = std::path::Path::new(control_path)
        .file_name()
        .and_then(|name| name.to_str())?;
    match sw_proto::pycompat::py_int_parse(name.strip_prefix("cm-")?) {
        Some(PyInt::Small(port)) => Some(port),
        _ => None,
    }
}

/// `int(value)` 的子集：整數、布林、可解析字串與有限浮點數；其餘（含缺項）→ None。
fn int_value(value: Option<&PyValue>) -> Option<i128> {
    match value? {
        PyValue::Int(PyInt::Small(port)) => Some(*port),
        PyValue::Bool(flag) => Some(i128::from(*flag)),
        PyValue::Str(text) => match sw_proto::pycompat::py_int_parse(text) {
            Some(PyInt::Small(port)) => Some(port),
            _ => None,
        },
        PyValue::Float(value) if value.is_finite() => Some(value.trunc() as i128),
        _ => None,
    }
}

/// `_remembered_bench_alive`（cli.py:1191-1199）：記憶 endpoint 為 loopback tcp 且 port 在 alive 集合內。
fn remembered_alive(endpoint: Option<&str>, alive_ports: &BTreeSet<i128>) -> bool {
    let Some(endpoint) = endpoint else {
        return false;
    };
    if !is_loopback_tcp(endpoint) {
        return false;
    }
    match parse_endpoint(endpoint) {
        Ok(Endpoint::Tcp { port, .. }) => alive_ports.contains(&i128::from(port)),
        _ => false,
    }
}

pub(crate) fn state_error(error: bench_state::BenchStateError) -> TunnelError {
    match error {
        bench_state::BenchStateError::Value(message) => {
            TunnelError::coded(ErrorCode::InvalidBenchState.as_str(), Some(message))
        }
        bench_state::BenchStateError::Os(message) => {
            TunnelError::coded(ErrorCode::BenchStateIoError.as_str(), Some(message))
        }
    }
}
