//! `connect` 群組（bench 代號建立與拆除；P4-T9 原生，SC-27；07 §9.3）。
//!
//! 以 `benches.yaml` 的代號解析 SSH 目標，重用 P4-T8 的 remote -L 隧道核心（`remote_tunnel::open_tunnel`）建立或拆除隧道，
//! 並把 endpoint 記憶寫入 `benches.state.json`（跨行程鎖）。Windows 與 POSIX 同一路徑，不再回 `REMOTE_NOT_SUPPORTED`。

use std::panic::{catch_unwind, AssertUnwindSafe};
use std::path::{Path, PathBuf};

use crate::argparse::{ArgValue, Namespace};
use crate::bench_state::{self, BenchStateError};
use crate::commands::benches_cmd::{
    emit_internal_error, state_error, status_tunnels, tunnel_error_response, tunnel_listen_port,
};
use crate::ctx::CliCtx;
use crate::output;
use crate::remote_tunnel::{self, panic_message, Role, TunnelError, TunnelSpec, Via};
use sw_proto::bench_registry::{self, BenchEntry};
use sw_proto::endpoint::{parse_endpoint, Endpoint};
use sw_proto::error_codes::ErrorCode;
use sw_proto::pyjson::{dumps_dict, PyDict, PyValue, CLI_STDOUT};

/// 隧道準備就緒逾時（秒；`cli.py::_run_connect` 的 `TunnelSpec` 預設）。
const READY_TIMEOUT_S: f64 = 10.0;

/// 執行原生 `connect` 指令（`cli.py::_run_connect`）。
///
/// 與 Python `except Exception` 對等：執行中的 panic 轉為 stdout `INTERNAL_ERROR` JSON、stderr 鏡像、exit 1（同 P4-T8 `run_remote`）。
pub fn run_connect(ns: &Namespace, ctx: &mut CliCtx<'_>) -> i32 {
    match catch_unwind(AssertUnwindSafe(|| run_connect_body(ns, ctx))) {
        Ok(code) => code,
        Err(payload) => emit_internal_error(ctx, "connect", &panic_message(payload.as_ref())),
    }
}

fn run_connect_body(ns: &Namespace, ctx: &mut CliCtx<'_>) -> i32 {
    let code = ns.str("code").unwrap_or("").to_string();
    let close = matches!(ns.get("close"), ArgValue::Bool(true));
    match run_connect_inner(ctx, &code, close) {
        Ok(resp) => {
            let _ = output::print_json(&mut ctx.io, &resp);
            if resp.get_truthy("ok") {
                0
            } else {
                let _ = output::mirror_err(&mut ctx.io, &resp, Some("connect"));
                1
            }
        }
        Err(error) if error.interrupted => 130,
        Err(error) => {
            let resp = tunnel_error_response(&error);
            let _ = output::print_json(&mut ctx.io, &resp);
            let _ = output::mirror_err(&mut ctx.io, &resp, Some("connect"));
            1
        }
    }
}

/// 回傳要印出的回應；`Err` 由呼叫端轉為 `TunnelError` JSON（exit 1）。
fn run_connect_inner(ctx: &mut CliCtx<'_>, code: &str, close: bool) -> Result<PyDict, TunnelError> {
    let benches = bench_state::load_configured_benches(ctx)
        .map_err(|error| TunnelError::coded(error.code, Some(error.message)))?;
    let entry = bench_registry::resolve(&benches, code)
        .map_err(|error| TunnelError::coded(error.code, Some(error.message)))?
        .clone();

    let run_dir = PathBuf::from(ctx.paths.run_dir.clone());
    let via = if entry.autossh {
        Via::Autossh
    } else {
        Via::Ssh
    };
    if close {
        return close_bench(ctx, code, &entry, via, &run_dir);
    }

    let spec = bench_spec(&entry, entry.local_port, via);
    remote_tunnel::resolve_ssh_bin(ctx, via)?;
    let response = remote_tunnel::open_tunnel(ctx, &spec, &run_dir)?;
    if !response.get_truthy("ok") {
        return Ok(response);
    }

    // 已在執行中的隧道（already_running）不由本次呼叫擁有，記憶失敗時不 rollback。
    let should_rollback = !response.get_truthy("already_running");
    let endpoint = format!("tcp://127.0.0.1:{}", entry.local_port);
    if let Err(error) = bench_state::remember(ctx, code, &endpoint) {
        let rollback_detail = if should_rollback {
            rollback_connect_open(ctx, &run_dir, entry.local_port)
        } else {
            None
        };
        let (error_code, message) = match error {
            BenchStateError::Value(message) => (ErrorCode::InvalidBenchState.as_str(), message),
            BenchStateError::Os(message) => (ErrorCode::BenchStateIoError.as_str(), message),
        };
        let mut resp = PyDict::from_pairs(vec![
            ("ok".to_string(), PyValue::Bool(false)),
            (
                "error_code".to_string(),
                PyValue::Str(error_code.to_string()),
            ),
            ("message".to_string(), PyValue::Str(message)),
        ]);
        if let Some(detail) = rollback_detail {
            resp.insert("rollback_warning", PyValue::Str(detail));
        }
        return Ok(resp);
    }
    Ok(response)
}

/// `TunnelSpec(role="connect", …)`：local 為 None，故 listen port 即 `port`。
fn bench_spec(entry: &BenchEntry, port: u16, via: Via) -> TunnelSpec {
    TunnelSpec {
        role: Role::Connect,
        ssh_target: entry.target.clone(),
        port,
        local: None,
        forward_src: None,
        remote_socket: entry.remote_socket.clone(),
        via,
        ssh_opts: entry.ssh_opts.clone(),
        ready_timeout: READY_TIMEOUT_S,
    }
}

/// `connect CODE --close`：以記憶的 port（若有）拆除 tunnel，全部成功後才清除記憶。
fn close_bench(
    ctx: &mut CliCtx<'_>,
    code: &str,
    entry: &BenchEntry,
    via: Via,
    run_dir: &Path,
) -> Result<PyDict, TunnelError> {
    let path = bench_state::state_path(ctx);
    let remembered = bench_state::load_bench_state_doc(ctx, &path).map_err(state_error)?;
    let mut close_port = entry.local_port;
    let remembered_endpoint =
        bench_state::remembered_bench_endpoint(&remembered, code).map_err(|message| {
            TunnelError::coded(ErrorCode::InvalidBenchState.as_str(), Some(message))
        })?;
    if let Some(endpoint) = remembered_endpoint {
        if let Ok(Endpoint::Tcp { port, .. }) = parse_endpoint(&endpoint) {
            close_port = port;
        }
    }

    let close_spec = bench_spec(entry, close_port, via);
    ensure_close_target_matches(ctx, run_dir, &close_spec)?;
    let response = remote_tunnel::close(ctx, run_dir, &close_port.to_string());
    if response.get_truthy("ok") {
        if connect_tunnel_present(ctx, run_dir, i128::from(close_port))? {
            let message = format!("local_port={close_port} 的 tunnel 仍存在，未清除 endpoint 記憶");
            return Ok(output::error_resp(
                ErrorCode::TunnelStillActive,
                Some(message.as_str()),
            ));
        }
        bench_state::forget(ctx, code).map_err(state_error)?;
    }
    Ok(response)
}

/// `_ensure_connect_close_target_matches`：同 port 的 stateful tunnel（略過 orphan）必須是相同 identity 的 connect tunnel。
fn ensure_close_target_matches(
    ctx: &CliCtx<'_>,
    run_dir: &Path,
    spec: &TunnelSpec,
) -> Result<(), TunnelError> {
    let listen_port = i128::from(spec.port);
    let Some(existing) = find_stateful_tunnel(ctx, run_dir, listen_port)? else {
        return Ok(());
    };
    let expected_identity = remote_tunnel::compute_identity(spec);
    let role_matches =
        matches!(existing.get("role"), Some(PyValue::Str(role)) if role == "connect");
    let identity_matches = matches!(
        existing.get("identity"),
        Some(PyValue::Str(identity)) if *identity == expected_identity
    );
    if role_matches && identity_matches {
        return Ok(());
    }
    Err(TunnelError::coded(
        ErrorCode::TunnelConflict.as_str(),
        Some(format!(
            "local_port={listen_port} 已有不相符的 stateful tunnel；僅可關閉相符的 connect tunnel"
        )),
    ))
}

fn find_stateful_tunnel(
    ctx: &CliCtx<'_>,
    run_dir: &Path,
    listen_port: i128,
) -> Result<Option<PyDict>, TunnelError> {
    for tunnel in status_tunnels(ctx, run_dir)? {
        if matches!(tunnel.get("status"), Some(PyValue::Str(status)) if status == "orphan") {
            continue;
        }
        if tunnel_listen_port(&tunnel) == Some(listen_port) {
            return Ok(Some(tunnel));
        }
    }
    Ok(None)
}

/// `_connect_tunnel_present`：任何 tunnel（含 orphan）的 listen port 等於 `listen_port`。
fn connect_tunnel_present(
    ctx: &CliCtx<'_>,
    run_dir: &Path,
    listen_port: i128,
) -> Result<bool, TunnelError> {
    Ok(status_tunnels(ctx, run_dir)?
        .iter()
        .any(|tunnel| tunnel_listen_port(tunnel) == Some(listen_port)))
}

/// `_rollback_connect_open`：記憶寫入失敗後拆除剛開的隧道；回傳 `None` 表示成功，否則為 `rollback_warning` 文字。
fn rollback_connect_open(ctx: &CliCtx<'_>, run_dir: &Path, listen_port: u16) -> Option<String> {
    let close_resp = remote_tunnel::close(ctx, run_dir, &listen_port.to_string());
    if !close_resp.get_truthy("ok") {
        let detail = match (close_resp.get("message"), close_resp.get("error_code")) {
            (Some(PyValue::Str(message)), _) if !message.is_empty() => message.clone(),
            (_, Some(PyValue::Str(code))) if !code.is_empty() => code.clone(),
            _ => dumps_dict(&close_resp, &CLI_STDOUT),
        };
        return Some(format!("rollback close 失敗：{detail}"));
    }
    match connect_tunnel_present(ctx, run_dir, i128::from(listen_port)) {
        Ok(true) => Some(format!(
            "rollback 後 local_port={listen_port} tunnel 仍存在"
        )),
        Ok(false) => None,
        Err(error) => Some(format!(
            "rollback 後無法確認 tunnel 是否已關閉：{}",
            error.message.unwrap_or(error.code)
        )),
    }
}
