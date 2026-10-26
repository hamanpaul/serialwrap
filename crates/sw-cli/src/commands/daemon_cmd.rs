//! `daemon` 群組與 SC-18 首次需求啟動（P4-T7）。

use std::ffi::OsString;
use std::path::{Path, PathBuf};
use std::time::Duration;

use sw_pal::proc_scan::{DaemonProc, ProcIdentity};
use sw_pal::service_ctl::{ServiceAction, SupervisionMode};
use sw_proto::endpoint::{parse_endpoint, Endpoint};
use sw_proto::error_codes::ErrorCode;
use sw_proto::pycompat::py_truthy;
use sw_proto::pyjson::{PyDict, PyInt, PyValue};
use sw_proto::pypath::{self, HomeLookup, PathFlavor};
use sw_proto::yaml11::{py_str_of, py_truthy_of};

use crate::argparse::{ArgValue, Namespace};
use crate::cli_trace::{format_trace_line, resolve_cli_trace_level, trace_enabled};
use crate::client::{self, ClientCrash, FailPhase, RpcFailure};
use crate::commands::service_cmd;
use crate::commands::supervision_cmd::{load_config_or_report, ConfigErrorReport};
use crate::ctx::CliCtx;
use crate::endpoint_resolve::{
    local_default_endpoint, print_endpoint_resolution_error, resolve_endpoint_info, EndpointArgs,
    ResolveFailure, ResolvedEndpoint,
};
use crate::output;

pub fn run_daemon_start(ns: &Namespace, ctx: &mut CliCtx<'_>) -> i32 {
    let is_win = crate::endpoint_resolve::rpc_backend_is_win(ctx);
    if let Some(endpoint) = ns.str("endpoint").filter(|value| !value.is_empty()) {
        if !is_win || !loopback_endpoint(endpoint) {
            let message = if is_win {
                "--endpoint 於 daemon start 僅接受 loopback tcp://（127.0.0.1/localhost/::1）作為本機 bind 位址"
            } else {
                "--endpoint 不支援 daemon start（daemon 只能在本機啟動）"
            };
            let resp = error(ErrorCode::RemoteNotSupported, message);
            emit(ctx, &resp, Some("daemon start"), 2);
            return 2;
        }
    }
    if let (Some(endpoint), Some(socket)) = (ns.str("endpoint"), ns.str("socket")) {
        if !endpoint.is_empty() && endpoint != socket {
            let resp = error(
                ErrorCode::InvalidArgs,
                "--endpoint 與 --socket 不一致；daemon start 請擇一指定 bind 位址",
            );
            emit(ctx, &resp, Some("daemon start"), 2);
            return 2;
        }
    }
    let cfg = match load_config_or_report(
        ctx,
        ConfigErrorReport::JsonInternalError("daemon start".to_string()),
    ) {
        Ok(config) => config,
        Err(code) => return code,
    };
    let mode_name = cfg
        .mode()
        .filter(|value| py_truthy_of(value))
        .map(py_str_of)
        .unwrap_or_else(|| "on-demand".to_string());
    let mode = SupervisionMode::parse(&mode_name);
    if mode_name != "on-demand" {
        let raw_mode = match cfg.mode().filter(|value| py_truthy_of(value)) {
            Some(value) => match service_cmd::yvalue_to_pyvalue(value) {
                Ok(raw) => raw,
                Err(message) => {
                    let _ = ctx.io.err_str(&format!("{message}\n"));
                    return 1;
                }
            },
            None => PyValue::Str("on-demand".to_string()),
        };
        return service_cmd::run_service_action_with_mode_value(
            ctx,
            &mode,
            ServiceAction::Start,
            flag(ns, "with_sudo"),
            "daemon start",
            Some("service start"),
            Some(raw_mode),
        );
    }

    let resolved = match resolve_local(ns, ctx) {
        Ok(value) => value,
        Err(error) => return report_resolve_failure(ctx, error, "daemon start"),
    };
    if let Some(outcome) = rpc(
        ctx,
        &resolved,
        "health.ping",
        0.5,
        &PyValue::Dict(PyDict::new()),
    ) {
        trace(
            ctx,
            &resolved,
            "health.ping",
            0.5,
            &outcome.resp,
            &outcome.meta,
        );
        if outcome.resp.get_truthy("ok") {
            let mut resp = success();
            resp.insert("already_running", PyValue::Bool(true));
            put_str(&mut resp, "socket", &resolved.endpoint);
            let _ = output::print_json(&mut ctx.io, &resp);
            return 0;
        }
    }

    let socket = match ns.str("socket") {
        Some(value) => value.to_string(),
        None => match ns.str("endpoint").filter(|value| !value.is_empty()) {
            Some(value) => value.to_string(),
            None => local_default_endpoint(ctx),
        },
    };
    if !flag(ns, "force_spawn") {
        if let Ok(daemons) = ctx.p.procs.serialwrapd_instances() {
            let cwd = process_cwd();
            if let Some(existing) = find_conflicting_daemon_with_home(
                &socket,
                &daemons,
                &cwd,
                path_flavor(ctx),
                context_home(ctx).as_deref(),
            ) {
                let known_socket = existing.socket.as_deref();
                let shown_socket =
                    known_socket.unwrap_or("未知（無法從 /proc 讀出 --socket，可能是舊版 daemon）");
                let hint = match known_socket {
                    Some(value) => format!(
                        "改用 --socket {value} 指向現有 daemon；確認後要強制另起才加 --force-spawn"
                    ),
                    None => "先用 `serialwrap daemon status`（看 multi_open_detail）確認現有 daemon 的 socket；確認後要強制另起才加 --force-spawn".to_string(),
                };
                let mut resp = error(
                    ErrorCode::DaemonAlreadyRunningElsewhere,
                    &format!(
                        "偵測到另一個 serialwrapd（pid={}）綁在 {shown_socket}，與本次 daemon start 目標 {socket} 不同；為避免同一批裝置被兩個 daemon 同時開啟（two-reader），預設拒絕再 spawn 一個。",
                        existing.pid
                    ),
                );
                put_str(&mut resp, "hint", &hint);
                match known_socket {
                    Some(value) => put_str(&mut resp, "existing_socket", value),
                    None => resp.insert("existing_socket", PyValue::Null),
                }
                put_int(&mut resp, "existing_pid", i64::from(existing.pid));
                put_str(&mut resp, "target_socket", &socket);
                let _ = output::print_json(&mut ctx.io, &resp);
                return 2;
            }
        }
    }

    let daemon_argv = match locate_daemon_argv(ctx) {
        Ok(value) => value,
        Err(_) => {
            let resp = error(
                ErrorCode::DaemonBinaryNotFound,
                "找不到 serialwrapd（release manifest 的 launcher、CLI 同層 serialwrapd、manifest 的 Python runtime 皆不存在）；請確認發行包完整",
            );
            emit(ctx, &resp, Some("daemon start"), 2);
            return 2;
        }
    };
    let profile_dir = ns
        .str("profile_dir")
        .unwrap_or(&ctx.paths.profile_dir)
        .to_string();
    let lock = ns.str("lock").unwrap_or(&ctx.paths.lock_path).to_string();
    let mut cmd = daemon_argv;
    cmd.extend([
        "--profile-dir".to_string(),
        profile_dir.clone(),
        "--socket".to_string(),
        socket.clone(),
        "--lock".to_string(),
        lock,
    ]);
    let env_files = resolve_daemon_start_env_files(ctx, &profile_dir);
    let (daemon_env, loaded_env_files) = match load_daemon_env(ctx, &env_files) {
        Ok(value) => value,
        Err(failure) => {
            let mut resp = error(ErrorCode::EnvFileSourceFailed, &failure.message);
            put_str(&mut resp, "env_file", &failure.path);
            if !env_files.is_empty() {
                let expanded = env_files
                    .iter()
                    .map(|path| expand_user(ctx, path))
                    .map(PyValue::Str)
                    .collect();
                resp.insert("env_files", PyValue::List(expanded));
            }
            emit(ctx, &resp, Some("daemon start"), 2);
            return 2;
        }
    };
    if flag(ns, "foreground") {
        return ctx
            .p
            .process
            .call_foreground(&cmd, &daemon_env)
            .unwrap_or(1) as i32;
    }

    let child = match ctx.p.process.spawn_daemon(&cmd, &daemon_env, true) {
        Ok(child) => child,
        Err(spawn_error) => {
            let resp = error(ErrorCode::DaemonExited, &spawn_error.py_message);
            emit(ctx, &resp, Some("daemon start"), 2);
            return 2;
        }
    };
    let source = if ns.str("socket").is_some() {
        "--socket"
    } else if ns.str("endpoint").is_some_and(|value| !value.is_empty()) {
        "--endpoint"
    } else {
        "default"
    };
    let started_endpoint = ResolvedEndpoint {
        endpoint: socket.clone(),
        source,
    };
    let attempts = if is_win { 50 } else { 15 };
    for _ in 0..attempts {
        ctx.p.clock.sleep(Duration::from_millis(200));
        if let Some(rc) = ctx.p.process.poll(&child) {
            let mut resp = output::error_resp(ErrorCode::DaemonExited, None);
            put_int(&mut resp, "pid", i64::from(child.pid));
            put_int(&mut resp, "returncode", rc);
            emit(ctx, &resp, Some("daemon start"), 2);
            return 2;
        }
        let ping_result = rpc(
            ctx,
            &started_endpoint,
            "health.ping",
            0.5,
            &PyValue::Dict(PyDict::new()),
        );
        let Some(ping_outcome) = ping_result else {
            continue;
        };
        trace(
            ctx,
            &started_endpoint,
            "health.ping",
            0.5,
            &ping_outcome.resp,
            &ping_outcome.meta,
        );
        if !ping_outcome.resp.get_truthy("ok") {
            continue;
        }
        let mut resp = success();
        put_int(&mut resp, "pid", i64::from(child.pid));
        put_str(&mut resp, "socket", &socket);
        if !loaded_env_files.is_empty() {
            put_string_list(&mut resp, "env_files", &loaded_env_files);
        }
        if let Some(status) = rpc(
            ctx,
            &started_endpoint,
            "health.status",
            1.0,
            &PyValue::Dict(PyDict::new()),
        ) {
            trace(
                ctx,
                &started_endpoint,
                "health.status",
                1.0,
                &status.resp,
                &status.meta,
            );
            if let Some(warnings) = status.resp.get("warnings").filter(|value| py_truthy(value)) {
                resp.insert("warnings", warnings.clone());
            }
        }
        let _ = output::print_json(&mut ctx.io, &resp);
        return 0;
    }
    let mut resp = output::error_resp(ErrorCode::DaemonNotReady, None);
    put_int(&mut resp, "pid", i64::from(child.pid));
    emit(ctx, &resp, Some("daemon start"), 2);
    2
}

pub fn run_daemon_stop(ns: &Namespace, ctx: &mut CliCtx<'_>) -> i32 {
    let cfg = match load_config_or_report(
        ctx,
        ConfigErrorReport::JsonInternalError("daemon stop".to_string()),
    ) {
        Ok(config) => config,
        Err(code) => return code,
    };
    let mode_name = cfg
        .mode()
        .filter(|value| py_truthy_of(value))
        .map(py_str_of)
        .unwrap_or_else(|| "on-demand".to_string());
    let mode = SupervisionMode::parse(&mode_name);
    let explicit = ns.str("endpoint").is_some_and(|value| !value.is_empty())
        || ns.str("socket").is_some()
        || ns.str("bench").is_some_and(|value| !value.is_empty());
    if !explicit && (mode.is_supervised() || mode_name.starts_with("systemd")) {
        let routed = service_cmd::run_service_action(
            ctx,
            &mode,
            ServiceAction::Stop,
            flag(ns, "with_sudo"),
            "daemon stop",
            Some("service stop"),
        );
        return routed;
    }
    let resolved = match resolve(ns, ctx) {
        Ok(value) => value,
        Err(error) => return report_resolve_failure(ctx, error, "daemon.stop"),
    };
    let Some(outcome) = rpc(
        ctx,
        &resolved,
        "daemon.stop",
        2.0,
        &PyValue::Dict(PyDict::new()),
    ) else {
        return client_crash(
            ctx,
            &resolved,
            "daemon.stop",
            2.0,
            &PyValue::Dict(PyDict::new()),
        );
    };
    trace(
        ctx,
        &resolved,
        "daemon.stop",
        2.0,
        &outcome.resp,
        &outcome.meta,
    );
    let code = if outcome.resp.get_truthy("ok") { 0 } else { 2 };
    emit(ctx, &outcome.resp, Some("daemon.stop"), code);
    code
}

/// `health.status` 查詢不經 SC-18，避免唯讀查詢啟動 daemon。
pub fn run_daemon_status(ns: &Namespace, ctx: &mut CliCtx<'_>) -> i32 {
    let resolved = match resolve(ns, ctx) {
        Ok(value) => value,
        Err(error) => return report_resolve_failure(ctx, error, "health.status"),
    };
    let timeout = timeout_arg(ns).unwrap_or(5.0);
    let retries = retries_arg(ns);
    let outcome = match client::call(
        ctx,
        &resolved.endpoint,
        "health.status",
        &PyValue::Dict(PyDict::new()),
        timeout,
        &retries,
    ) {
        Ok(outcome) => outcome,
        Err(ClientCrash::InvalidTimeout(message) | ClientCrash::EncodeError(message)) => {
            return crate::commands::rpc_cmds::internal_error(ctx, &message);
        }
    };
    trace(
        ctx,
        &resolved,
        "health.status",
        timeout,
        &outcome.resp,
        &outcome.meta,
    );
    emit_rpc_response(ctx, &outcome.resp, "health.status")
}

pub fn run_daemon_restart(ns: &Namespace, ctx: &mut CliCtx<'_>) -> i32 {
    let cfg = match load_config_or_report(
        ctx,
        ConfigErrorReport::JsonInternalError("daemon restart".to_string()),
    ) {
        Ok(config) => config,
        Err(code) => return code,
    };
    let mode_name = cfg
        .mode()
        .filter(|value| py_truthy_of(value))
        .map(py_str_of)
        .unwrap_or_else(|| "on-demand".to_string());
    let mode = SupervisionMode::parse(&mode_name);
    if mode_name.starts_with("systemd") {
        return service_cmd::run_service_action(
            ctx,
            &mode,
            ServiceAction::Restart,
            flag(ns, "with_sudo"),
            "service restart",
            None,
        );
    }
    if matches!(
        mode,
        SupervisionMode::WindowsTask | SupervisionMode::WindowsService
    ) {
        return service_cmd::run_service_action(
            ctx,
            &mode,
            ServiceAction::Restart,
            flag(ns, "with_sudo"),
            "service restart",
            None,
        );
    }
    if ns.str("bench").is_some_and(|value| !value.is_empty())
        || ns.str("endpoint").is_some_and(|value| {
            !value.is_empty()
                && (!crate::endpoint_resolve::rpc_backend_is_win(ctx) || !loopback_endpoint(value))
        })
    {
        let resp = error(
            ErrorCode::InvalidArgs,
            "daemon restart 只能重啟本機 daemon（不支援 --endpoint／--bench）",
        );
        emit(ctx, &resp, Some("daemon.restart"), 2);
        return 2;
    }
    let resolved = match resolve_local(ns, ctx) {
        Ok(value) => value,
        Err(error) => return report_resolve_failure(ctx, error, "daemon.restart"),
    };
    let stopped = ping(ctx, &resolved.endpoint, 0.5);
    let id = capture_daemon_identity(ctx, &resolved.endpoint);
    if stopped {
        if let Some(outcome) = rpc(
            ctx,
            &resolved,
            "daemon.stop",
            2.0,
            &PyValue::Dict(PyDict::new()),
        ) {
            trace(
                ctx,
                &resolved,
                "daemon.stop",
                2.0,
                &outcome.resp,
                &outcome.meta,
            );
        }
    }
    // RPC 即使已停止回應，舊行程仍可能持有 UART。確認已無法 ping 且原身分行程全數消失後才 spawn。
    if id.known && !wait_daemon_gone(ctx, &resolved.endpoint, &id, Duration::from_secs(10)) {
        let mut resp = PyDict::new();
        resp.insert(
            "error_code",
            PyValue::Str(ErrorCode::Timeout.as_str().to_string()),
        );
        resp.insert("ok", PyValue::Bool(false));
        put_str(&mut resp, "phase", "restart_wait_old_exit");
        emit(ctx, &resp, Some("daemon.restart"), 1);
        return 1;
    }
    let mut start_ns = ns.clone();
    start_ns.vals.insert(
        "socket".to_string(),
        ArgValue::Str(resolved.endpoint.clone()),
    );
    start_ns.vals.insert("endpoint".to_string(), ArgValue::None);
    start_ns.vals.insert("bench".to_string(), ArgValue::None);
    start_ns
        .vals
        .insert("foreground".to_string(), ArgValue::Bool(false));
    start_ns
        .vals
        .insert("force_spawn".to_string(), ArgValue::Bool(false));
    let (_start_rc, start_resp, start_stderr) = capture_start(ctx, &start_ns);
    if !start_stderr.is_empty() {
        let text = String::from_utf8_lossy(&start_stderr).replace("\r\n", "\n");
        let _ = ctx.io.err_str(&text);
    }
    let ok = start_resp.get_truthy("ok");
    let mut resp = PyDict::new();
    resp.insert("ok", PyValue::Bool(ok));
    resp.insert("restarted", PyValue::Bool(true));
    resp.insert("stopped", PyValue::Bool(stopped));
    resp.insert("start", PyValue::Dict(start_resp));
    let code = if ok { 0 } else { 2 };
    emit(ctx, &resp, Some("daemon.restart"), code);
    code
}

fn capture_start(ctx: &mut CliCtx<'_>, ns: &Namespace) -> (i32, PyDict, Vec<u8>) {
    let mut out = Vec::new();
    let mut err = Vec::new();
    let newline = ctx.p.caps.text_newline;
    let mut nested = CliCtx {
        p: ctx.p,
        env: ctx.env.clone(),
        paths: ctx.paths.clone(),
        io: crate::ctx::TextIo::new(&mut out, &mut err, newline),
        verbose: ctx.verbose,
        cli_version: ctx.cli_version,
        autostart_lock: None,
    };
    let rc = run_daemon_start(ns, &mut nested);
    drop(nested);
    let text = String::from_utf8_lossy(&out);
    let response = match sw_proto::pyjson::loads(text.trim()) {
        Ok(PyValue::Dict(value)) => value,
        _ => {
            let mut value = PyDict::new();
            value.insert("ok", PyValue::Bool(rc == 0));
            value
        }
    };
    (rc, response, err)
}

#[derive(Debug)]
pub enum DaemonLocateError {
    NotFound,
}

/// DF-05：依 manifest launcher、CLI 同層 daemon、manifest runtime 的順序尋找啟動入口。
pub fn locate_daemon_argv(ctx: &CliCtx<'_>) -> Result<Vec<String>, DaemonLocateError> {
    let manifest = crate::install_layout::locate_manifest(ctx).ok();
    if let Some((path, parsed)) = &manifest {
        if let Some(relative) = manifest_component_path(&parsed.raw, "launcher") {
            let candidate = resolve_component_path(path, relative);
            if ctx.p.fs.is_file(&candidate) {
                let windows_python_script = path_flavor(ctx) == PathFlavor::Windows
                    && Path::new(relative).extension().is_some_and(|extension| {
                        extension.to_string_lossy().eq_ignore_ascii_case("py")
                    });
                if windows_python_script {
                    if let Some(runtime_relative) = manifest_component_path(&parsed.raw, "runtime")
                    {
                        let runtime = resolve_component_path(path, runtime_relative);
                        if ctx.p.fs.is_file(&runtime) {
                            return Ok(vec![
                                absolute_path(ctx, &runtime),
                                "-I".to_string(),
                                "-B".to_string(),
                                absolute_path(ctx, &candidate),
                            ]);
                        }
                    }
                } else {
                    return Ok(vec![absolute_path(ctx, &candidate)]);
                }
            }
        }
    }
    if let Ok(executable) = ctx.p.env.current_exe() {
        let real = ctx.p.env.realpath(&executable).unwrap_or(executable);
        if let Some(directory) = real.parent() {
            let name = if path_flavor(ctx) == PathFlavor::Windows {
                "serialwrapd.exe"
            } else {
                "serialwrapd"
            };
            let candidate = directory.join(name);
            if ctx.p.fs.is_file(&candidate) {
                return Ok(vec![absolute_path(ctx, &candidate)]);
            }
        }
    }
    if let Some((path, parsed)) = manifest {
        if let Some(relative) = manifest_component_path(&parsed.raw, "runtime") {
            let runtime = resolve_component_path(&path, relative);
            if ctx.p.fs.is_file(&runtime) {
                return Ok(vec![
                    absolute_path(ctx, &runtime),
                    "-I".to_string(),
                    "-B".to_string(),
                    "-m".to_string(),
                    "sw_core.daemon".to_string(),
                ]);
            }
        }
    }
    Err(DaemonLocateError::NotFound)
}

fn manifest_component_path<'a>(raw: &'a PyDict, key: &str) -> Option<&'a str> {
    let components = as_dict(raw.get("components")?)?;
    let daemon = as_dict(components.get("daemon")?)?;
    let component = as_dict(daemon.get(key)?)?;
    as_str(component.get("path")?)
}

fn resolve_component_path(manifest: &Path, relative: &str) -> PathBuf {
    let path = Path::new(relative);
    if path.is_absolute() {
        path.to_path_buf()
    } else {
        manifest
            .parent()
            .unwrap_or_else(|| Path::new("."))
            .join(path)
    }
}

/// SC-10：把 unix URL 去掉，將檔案 endpoint 絕對化並正規化；TCP endpoint 保持字串原樣。
pub fn normalize_endpoint_for_compare(s: &str, cwd: &Path, flavor: PathFlavor) -> String {
    normalize_endpoint_for_compare_with_home(s, cwd, flavor, process_home(flavor).as_deref())
}

pub(crate) fn normalize_endpoint_for_compare_with_home(
    s: &str,
    cwd: &Path,
    flavor: PathFlavor,
    home: Option<&str>,
) -> String {
    if s.starts_with("tcp://") {
        return s.to_string();
    }
    let raw_path = s.strip_prefix("unix://").unwrap_or(s);
    let path = expand_home_path(raw_path, flavor, home);
    let path = if pypath::isabs(flavor, &path) {
        path
    } else {
        pypath::join(flavor, &cwd.to_string_lossy(), &[&path])
    };
    let normalized = pypath::normpath(flavor, &path);
    if flavor == PathFlavor::Windows {
        normalized.replace('/', "\\").to_lowercase()
    } else {
        normalized
    }
}

/// SC-10：回傳與本次 spawn endpoint 不同的第一個 serialwrapd。
pub fn find_conflicting_daemon<'a>(
    target: &str,
    daemons: &'a [DaemonProc],
    cwd: &Path,
    flavor: PathFlavor,
) -> Option<&'a DaemonProc> {
    find_conflicting_daemon_with_home(
        target,
        daemons,
        cwd,
        flavor,
        process_home(flavor).as_deref(),
    )
}

fn find_conflicting_daemon_with_home<'a>(
    target: &str,
    daemons: &'a [DaemonProc],
    cwd: &Path,
    flavor: PathFlavor,
    home: Option<&str>,
) -> Option<&'a DaemonProc> {
    let target = normalize_endpoint_for_compare_with_home(target, cwd, flavor, home);
    daemons.iter().find(|daemon| {
        daemon.socket.as_deref().is_none_or(|socket| {
            normalize_endpoint_for_compare_with_home(socket, cwd, flavor, home) != target
        })
    })
}

pub(crate) fn normalize_endpoint_for_context(
    ctx: &CliCtx<'_>,
    endpoint: &str,
    cwd: &Path,
) -> String {
    normalize_endpoint_for_compare_with_home(
        endpoint,
        cwd,
        path_flavor(ctx),
        context_home(ctx).as_deref(),
    )
}

/// SC-18：只處理 Windows、預設本機 endpoint、尚未送出且可確認為不存在的連線失敗。
pub fn maybe_autostart_on_demand(
    ctx: &mut CliCtx<'_>,
    method: &str,
    resolved: &ResolvedEndpoint,
    failure: Option<&RpcFailure>,
    resp: &PyDict,
) -> AutostartDecision {
    if !ctx.p.caps.on_demand_autostart
        || matches!(method, "health.ping" | "health.status" | "daemon.stop")
        || !matches!(resolved.source, "default" | "config.yaml")
        || dict_str(resp, "error_code") != Some(ErrorCode::SocketError.as_str())
        || !failure.is_some_and(|failure| {
            failure.phase == FailPhase::Connect
                && !failure.maybe_sent
                && matches!(
                    failure.pal_kind,
                    Some(sw_pal::PalErrorKind::ConnectionRefused | sw_pal::PalErrorKind::NotFound)
                )
        })
    {
        return AutostartDecision::NotApplicable;
    }
    let default_endpoint = local_default_endpoint(ctx);
    if normalize_endpoint_for_context(ctx, &resolved.endpoint, &process_cwd())
        != normalize_endpoint_for_context(ctx, &default_endpoint, &process_cwd())
    {
        return AutostartDecision::NotApplicable;
    }
    let mode = match crate::endpoint_resolve::load_runtime_config(ctx)
        .ok()
        .and_then(|config| config.mode().cloned())
    {
        None => "on-demand".to_string(),
        Some(value) => py_str_of(&value),
    };
    if mode != "on-demand" {
        return AutostartDecision::NotApplicable;
    }
    let lock_path = PathBuf::from(&ctx.paths.run_dir).join("autostart.lock");
    if let Some(parent) = lock_path.parent() {
        let _ = ctx.p.fs.mkdirs(parent);
    }
    let start = ctx.p.clock.mono_ns();
    let timeout = Duration::from_secs(15);
    let lock = loop {
        match ctx.p.fs.file_lock_exclusive(&lock_path, false) {
            Ok(guard) => break guard,
            Err(lock_error) if lock_error.kind == sw_pal::PalErrorKind::WouldBlock => {
                if ctx.p.clock.mono_ns().saturating_sub(start) >= timeout.as_nanos() as u64 {
                    return AutostartDecision::Failed(error(
                        ErrorCode::Timeout,
                        "等待 on-demand 啟動鎖逾時",
                    ));
                }
                ctx.p.clock.sleep(Duration::from_millis(200));
            }
            Err(lock_error) => {
                return AutostartDecision::Failed(error(
                    ErrorCode::InternalError,
                    &lock_error.py_message,
                ));
            }
        }
    };
    if ping(ctx, &resolved.endpoint, 0.5) {
        drop(lock);
        return AutostartDecision::AlreadyUp;
    }
    if install_journal_exists(ctx) {
        drop(lock);
        let _ = ctx.io.err_str("serialwrap: on-demand 自動啟動暫停：更新／回滾進行中（install-journal.json 存在），請待 serialwrap setup 完成\n");
        return AutostartDecision::NotApplicable;
    }
    let mut start_ns = Namespace::default();
    start_ns.vals.insert(
        "socket".to_string(),
        ArgValue::Str(resolved.endpoint.clone()),
    );
    start_ns
        .vals
        .insert("foreground".to_string(), ArgValue::Bool(false));
    let (start_rc, start_resp, _) = capture_start(ctx, &start_ns);
    drop(lock);
    if start_resp.get_truthy("already_running") {
        return AutostartDecision::AlreadyUp;
    }
    if start_resp.get_truthy("ok") {
        let pid = start_resp
            .get("pid")
            .and_then(int_as_i64)
            .unwrap_or_default()
            .max(0) as u32;
        return AutostartDecision::Started { pid };
    }
    if ping(ctx, &resolved.endpoint, 0.5) {
        return AutostartDecision::AlreadyUp;
    }
    let mut failure = start_resp;
    if dict_str(&failure, "error_code").is_none() {
        let code = if start_rc == 1 {
            ErrorCode::InternalError
        } else {
            ErrorCode::DaemonNotReady
        };
        failure.insert("error_code", PyValue::Str(code.as_str().to_string()));
        failure.insert("ok", PyValue::Bool(false));
    }
    AutostartDecision::Failed(failure)
}

#[derive(Debug)]
pub enum AutostartDecision {
    NotApplicable,
    Started { pid: u32 },
    AlreadyUp,
    Failed(PyDict),
}

#[derive(Debug)]
pub struct EnvFileSourceError {
    pub path: String,
    pub message: String,
}

/// 產生 daemon 啟動時要讀取的 env 檔清單。
pub fn resolve_daemon_start_env_files(ctx: &CliCtx<'_>, profile_dir: &str) -> Vec<String> {
    if let Some(raw) = ctx.env.get("SERIALWRAP_DAEMON_ENV_FILE") {
        let trimmed = raw.trim();
        return if trimmed.is_empty() {
            Vec::new()
        } else {
            vec![trimmed.to_string()]
        };
    }
    let mut files = vec!["~/OPI.env".to_string()];
    let profile = pypath::join(path_flavor(ctx), profile_dir, &["OPI.env"]);
    if !files.contains(&profile) {
        files.push(profile);
    }
    files
}

/// 載入 env 檔；POSIX 以 bash 保留 shell 語意及環境值原始位元組，Windows 使用簡易解析器。
pub fn load_daemon_env(
    ctx: &CliCtx<'_>,
    files: &[String],
) -> Result<(Vec<(OsString, OsString)>, Vec<String>), EnvFileSourceError> {
    let mut env: Vec<(OsString, OsString)> = ctx
        .p
        .env
        .vars()
        .into_iter()
        .map(|(key, value)| (OsString::from(key), OsString::from(value)))
        .collect();
    let mut loaded = Vec::new();
    let mut seen = Vec::new();
    for raw in files {
        let path = expand_user(ctx, raw.trim());
        if path.is_empty() || seen.contains(&path) {
            continue;
        }
        seen.push(path.clone());
        if !ctx.p.fs.is_file(Path::new(&path)) {
            continue;
        }
        if crate::endpoint_resolve::rpc_backend_is_win(ctx) {
            let bytes = ctx
                .p
                .fs
                .read(Path::new(&path))
                .map_err(|error| EnvFileSourceError {
                    path: path.clone(),
                    message: format!("無法讀取 env 檔：{}", error.py_message),
                })?;
            let entries = parse_env_file_simple(&bytes);
            for (key, value) in entries {
                set_env(&mut env, OsString::from(key), OsString::from(value));
            }
        } else {
            let argv = vec![
                "bash".to_string(),
                "-lc".to_string(),
                "set -a && source \"$1\" >/dev/null && env -0".to_string(),
                "serialwrap".to_string(),
                path.clone(),
            ];
            let output = ctx
                .p
                .env
                .run_raw(&argv, Some(&env), None)
                .map_err(|error| EnvFileSourceError {
                    path: path.clone(),
                    message: error.py_message,
                })?;
            if output.rc != 0 {
                let message = String::from_utf8_lossy(&output.stderr).trim().to_string();
                return Err(EnvFileSourceError {
                    path: path.clone(),
                    message: if message.is_empty() {
                        format!("failed to source {path}")
                    } else {
                        message
                    },
                });
            }
            env = parse_env_zero(&output.stdout);
        }
        loaded.push(path);
    }
    Ok((env, loaded))
}

fn parse_env_file_simple(bytes: &[u8]) -> Vec<(String, String)> {
    let mut result = Vec::new();
    for raw in String::from_utf8_lossy(bytes).lines() {
        let mut line = raw.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        if let Some(rest) = line.strip_prefix("export ") {
            line = rest.trim_start();
        }
        let Some((key, value)) = line.split_once('=') else {
            continue;
        };
        let key = key.trim();
        if key.is_empty() {
            continue;
        }
        let mut value = value.trim();
        if value.len() >= 2
            && ((value.starts_with('\'') && value.ends_with('\''))
                || (value.starts_with('"') && value.ends_with('"')))
        {
            value = &value[1..value.len() - 1];
        }
        result.push((key.to_string(), value.to_string()));
    }
    result
}

fn parse_env_zero(bytes: &[u8]) -> Vec<(OsString, OsString)> {
    bytes
        .split(|byte| *byte == 0)
        .filter_map(|entry| {
            let position = entry.iter().position(|byte| *byte == b'=')?;
            Some((
                sw_pal::os_string_from_bytes(&entry[..position]),
                sw_pal::os_string_from_bytes(&entry[position + 1..]),
            ))
        })
        .collect()
}

fn set_env(env: &mut Vec<(OsString, OsString)>, key: OsString, value: OsString) {
    if let Some(slot) = env.iter_mut().find(|(current, _)| *current == key) {
        slot.1 = value;
    } else {
        env.push((key, value));
    }
}

fn install_journal_exists(ctx: &CliCtx<'_>) -> bool {
    let journal = crate::install_layout::resolve_install_root(ctx)
        .root
        .join("install-journal.json");
    ctx.p.fs.exists(&journal)
}

pub fn capture_daemon_identity(
    ctx: &CliCtx<'_>,
    endpoint: &str,
) -> crate::graceful_stop::DaemonIdentity {
    capture_daemon_identity_with_status(ctx, endpoint).0
}

/// setup reconcile（SC-12）在停舊之前擷取舊 daemon 身分。
///
/// 與 Python `setup_cmd.reconcile` 一致：`health.status` 回報 pid 時，只以該 pid 判定舊 daemon 是否結束
/// （`multi_open.daemon_process_alive`），不再掃描並納入其他 socket 不明的 serialwrapd；
/// 只有 status 無 pid 時才由 [`capture_reconcile_daemon_identity`] 以行程掃描補足。
pub(crate) fn capture_setup_daemon_identity(
    ctx: &CliCtx<'_>,
    endpoint: &str,
) -> crate::graceful_stop::DaemonIdentity {
    let (identity, status_pid) = capture_daemon_identity_with_status(ctx, endpoint);
    match status_pid {
        Some(pid) if !identity.known => crate::graceful_stop::DaemonIdentity {
            pid: Some(pid),
            known: true,
            tree: vec![ProcIdentity {
                pid,
                start_ticks: None,
            }],
        },
        _ => identity,
    }
}

/// 回傳（身分, `health.status` 回報的 pid）。
fn capture_daemon_identity_with_status(
    ctx: &CliCtx<'_>,
    endpoint: &str,
) -> (crate::graceful_stop::DaemonIdentity, Option<u32>) {
    let status = client::call(
        ctx,
        endpoint,
        "health.status",
        &PyValue::Dict(PyDict::new()),
        1.0,
        &PyInt::Small(0),
    )
    .ok()
    .and_then(|outcome| outcome.resp.get("pid").and_then(int_as_i64))
    .and_then(|pid| u32::try_from(pid).ok());
    let cwd = process_cwd();
    let Ok(daemons) = ctx.p.procs.serialwrapd_instances() else {
        return (crate::graceful_stop::DaemonIdentity::default(), status);
    };
    let mut tree = Vec::new();
    let mut found_pid = None;
    for daemon in daemons {
        let endpoint_matches = daemon.socket.as_deref().is_some_and(|socket| {
            normalize_endpoint_for_context(ctx, socket, &cwd)
                == normalize_endpoint_for_context(ctx, endpoint, &cwd)
        });
        if endpoint_matches || status == Some(daemon.pid) {
            found_pid = Some(daemon.pid);
            tree.extend(daemon.tree);
            if !tree.iter().any(|item| item.pid == daemon.pid) {
                tree.push(ProcIdentity {
                    pid: daemon.pid,
                    start_ticks: None,
                });
            }
        }
    }
    tree.sort_by_key(|item| item.pid);
    tree.dedup_by_key(|item| item.pid);
    (
        crate::graceful_stop::DaemonIdentity {
            pid: found_pid,
            known: !tree.is_empty(),
            tree,
        },
        status,
    )
}

pub(crate) fn wait_daemon_gone(
    ctx: &CliCtx<'_>,
    endpoint: &str,
    id: &crate::graceful_stop::DaemonIdentity,
    timeout: Duration,
) -> bool {
    let started = ctx.p.clock.mono_ns();
    let limit = timeout.as_nanos().min(u64::MAX as u128) as u64;
    loop {
        let alive = ping(ctx, endpoint, 0.5);
        let process_alive = id
            .tree
            .iter()
            .any(|identity| ctx.p.procs.identity_alive(identity));
        if !alive && !process_alive {
            return true;
        }
        if ctx.p.clock.mono_ns().saturating_sub(started) >= limit {
            return false;
        }
        ctx.p.clock.sleep(Duration::from_millis(200));
    }
}

pub(crate) fn capture_daemon_identity_public(
    ctx: &CliCtx<'_>,
    endpoint: &str,
) -> crate::graceful_stop::DaemonIdentity {
    capture_daemon_identity(ctx, endpoint)
}

/// SC-12：daemon.status 無法提供 PID 時，以行程掃描補足可驗證的舊實例身分。
pub(crate) fn capture_reconcile_daemon_identity(
    ctx: &CliCtx<'_>,
    endpoint: &str,
) -> Result<crate::graceful_stop::DaemonIdentity, ()> {
    let mut identity = capture_daemon_identity(ctx, endpoint);
    if identity.known {
        return Ok(identity);
    }

    let cwd = process_cwd();
    let normalized_endpoint = normalize_endpoint_for_context(ctx, endpoint, &cwd);
    let daemons = ctx.p.procs.serialwrapd_instances().map_err(|_| ())?;
    for daemon in daemons {
        let matches = match daemon.socket.as_deref() {
            None => true,
            Some(socket) => match parse_endpoint(socket) {
                Ok(_) => normalize_endpoint_for_context(ctx, socket, &cwd) == normalized_endpoint,
                Err(_) => true,
            },
        };
        if !matches {
            continue;
        }
        identity.pid.get_or_insert(daemon.pid);
        identity.tree.extend(daemon.tree);
        if !identity.tree.iter().any(|item| item.pid == daemon.pid) {
            identity.tree.push(ProcIdentity {
                pid: daemon.pid,
                start_ticks: None,
            });
        }
    }
    identity.tree.sort_by_key(|item| item.pid);
    identity.tree.dedup_by_key(|item| item.pid);
    identity.known = !identity.tree.is_empty();
    Ok(identity)
}

fn resolve(ns: &Namespace, ctx: &mut CliCtx<'_>) -> Result<ResolvedEndpoint, ResolveFailure> {
    resolve_endpoint_info(
        ctx,
        &EndpointArgs {
            endpoint: ns.str("endpoint"),
            socket: ns.str("socket"),
            bench: ns.str("bench"),
        },
    )
}

fn resolve_local(ns: &Namespace, ctx: &mut CliCtx<'_>) -> Result<ResolvedEndpoint, ResolveFailure> {
    resolve_endpoint_info(
        ctx,
        &EndpointArgs {
            endpoint: ns.str("endpoint"),
            socket: ns.str("socket"),
            bench: None,
        },
    )
}

fn report_resolve_failure(ctx: &mut CliCtx<'_>, error: ResolveFailure, context: &str) -> i32 {
    match error {
        ResolveFailure::Resolution(error) => print_endpoint_resolution_error(ctx, &error, context),
        ResolveFailure::Crash(message) => crate::commands::rpc_cmds::internal_error(ctx, &message),
    }
}

fn rpc(
    ctx: &CliCtx<'_>,
    endpoint: &ResolvedEndpoint,
    method: &str,
    timeout: f64,
    params: &PyValue,
) -> Option<client::RpcOutcome> {
    client::call(
        ctx,
        &endpoint.endpoint,
        method,
        params,
        timeout,
        &PyInt::Small(0),
    )
    .ok()
}

fn ping(ctx: &CliCtx<'_>, endpoint: &str, timeout: f64) -> bool {
    client::call(
        ctx,
        endpoint,
        "health.ping",
        &PyValue::Dict(PyDict::new()),
        timeout,
        &PyInt::Small(0),
    )
    .is_ok_and(|outcome| outcome.resp.get_truthy("ok"))
}

fn trace(
    ctx: &mut CliCtx<'_>,
    endpoint: &ResolvedEndpoint,
    method: &str,
    timeout: f64,
    resp: &PyDict,
    meta: &crate::cli_trace::TraceMeta,
) {
    let level = resolve_cli_trace_level(ctx.verbose, ctx.env.get("SERIALWRAP_LOG_LEVEL"));
    if trace_enabled(&level) {
        let line = format_trace_line(
            endpoint.endpoint.as_str(),
            endpoint.source,
            method,
            timeout,
            resp,
            meta,
        );
        let _ = ctx.io.err_str(&format!("{line}\n"));
    }
}

fn emit_rpc_response(ctx: &mut CliCtx<'_>, resp: &PyDict, method: &str) -> i32 {
    let _ = output::print_json(&mut ctx.io, resp);
    let _ = output::mirror_err(&mut ctx.io, resp, Some(method));
    let _ = output::warn_version_mismatch(&mut ctx.io, resp, ctx.cli_version);
    if resp.get_truthy("ok") {
        0
    } else {
        2
    }
}

fn client_crash(
    ctx: &mut CliCtx<'_>,
    endpoint: &ResolvedEndpoint,
    method: &str,
    timeout: f64,
    params: &PyValue,
) -> i32 {
    match client::call(
        ctx,
        &endpoint.endpoint,
        method,
        params,
        timeout,
        &PyInt::Small(0),
    ) {
        Err(ClientCrash::InvalidTimeout(message) | ClientCrash::EncodeError(message)) => {
            crate::commands::rpc_cmds::internal_error(ctx, &message)
        }
        Ok(outcome) => emit_rpc_response(ctx, &outcome.resp, method),
    }
}

fn loopback_endpoint(value: &str) -> bool {
    match parse_endpoint(value) {
        Ok(Endpoint::Tcp { host, .. }) => {
            matches!(host.as_str(), "127.0.0.1" | "localhost" | "::1" | "[::1]")
                || host.starts_with("127.")
        }
        _ => false,
    }
}

fn path_flavor(ctx: &CliCtx<'_>) -> PathFlavor {
    match ctx.p.caps.path_flavor {
        sw_pal::PalPathFlavor::Windows => PathFlavor::Windows,
        sw_pal::PalPathFlavor::Posix => PathFlavor::Posix,
    }
}

fn process_cwd() -> PathBuf {
    std::env::current_dir().unwrap_or_else(|_| PathBuf::from("."))
}

fn process_home(flavor: PathFlavor) -> Option<String> {
    match flavor {
        PathFlavor::Posix => std::env::var("HOME").ok(),
        PathFlavor::Windows => std::env::var("USERPROFILE").ok().or_else(|| {
            Some(format!(
                "{}{}",
                std::env::var("HOMEDRIVE").ok()?,
                std::env::var("HOMEPATH").ok()?
            ))
        }),
    }
}

fn context_home(ctx: &CliCtx<'_>) -> Option<String> {
    match path_flavor(ctx) {
        PathFlavor::Posix => ctx
            .env
            .get("HOME")
            .map(str::to_string)
            .or_else(|| ctx.p.env.current_user_home()),
        PathFlavor::Windows => ctx
            .env
            .get("USERPROFILE")
            .map(str::to_string)
            .or_else(|| {
                Some(format!(
                    "{}{}",
                    ctx.env.get("HOMEDRIVE")?,
                    ctx.env.get("HOMEPATH")?
                ))
            })
            .or_else(|| ctx.p.env.current_user_home()),
    }
}

fn expand_home_path(path: &str, flavor: PathFlavor, home: Option<&str>) -> String {
    if !path.starts_with('~') {
        return path.to_string();
    }
    let remainder = &path[1..];
    let user_home = remainder.is_empty()
        || remainder.starts_with('/')
        || (flavor == PathFlavor::Windows && remainder.starts_with('\\'));
    if !user_home {
        return path.to_string();
    }
    let Some(home) = home else {
        return path.to_string();
    };
    let suffix = remainder.trim_start_matches(|c| c == '/' || c == '\\');
    if suffix.is_empty() {
        home.to_string()
    } else {
        pypath::join(flavor, home, &[suffix])
    }
}

fn absolute_path(ctx: &CliCtx<'_>, path: &Path) -> String {
    let flavor = path_flavor(ctx);
    let expanded = expand_user(ctx, &path.to_string_lossy());
    let cwd = process_cwd();
    // 候選路徑來自主機檔案系統（`ctx.p.fs.is_file` 已確認存在）：主機視為絕對者不得再接 cwd。
    // 真實平台的旗標即主機旗標，兩者判定一致；只在跨主機模擬（Windows 主機上的 POSIX 旗標）時有差。
    let absolute = if pypath::isabs(flavor, &expanded) || Path::new(&expanded).is_absolute() {
        expanded
    } else {
        pypath::join(flavor, &cwd.to_string_lossy(), &[&expanded])
    };
    pypath::normpath(flavor, &absolute)
}

struct EnvHome<'a>(&'a dyn sw_pal::SysEnv);
impl HomeLookup for EnvHome<'_> {
    fn current_user_home(&self) -> Option<String> {
        self.0.current_user_home()
    }
    fn user_home(&self, user: &str) -> Option<String> {
        self.0.user_home(user)
    }
}

fn expand_user(ctx: &CliCtx<'_>, value: &str) -> String {
    pypath::expanduser(
        path_flavor(ctx),
        value,
        &ctx.env,
        &EnvHome(ctx.p.env.as_ref()),
    )
}

fn timeout_arg(ns: &Namespace) -> Option<f64> {
    match ns.get("timeout_s") {
        ArgValue::Float(value) => Some(*value),
        _ => None,
    }
}

fn retries_arg(ns: &Namespace) -> PyInt {
    match ns.get("retries") {
        ArgValue::Int(value) => value.clone(),
        _ => PyInt::Small(0),
    }
}

fn flag(ns: &Namespace, key: &str) -> bool {
    matches!(ns.get(key), ArgValue::Bool(true))
}

fn error(code: ErrorCode, message: &str) -> PyDict {
    let mut resp = output::error_resp(code, Some(message));
    resp.insert("ok", PyValue::Bool(false));
    resp
}

fn success() -> PyDict {
    PyDict::from_pairs(vec![("ok".to_string(), PyValue::Bool(true))])
}

fn emit(ctx: &mut CliCtx<'_>, resp: &PyDict, context: Option<&str>, _code: i32) {
    let _ = output::print_json(&mut ctx.io, resp);
    let _ = output::mirror_err(&mut ctx.io, resp, context);
}

fn put_str(resp: &mut PyDict, key: &str, value: &str) {
    resp.insert(key, PyValue::Str(value.to_string()));
}

fn put_int(resp: &mut PyDict, key: &str, value: i64) {
    resp.insert(key, PyValue::Int(PyInt::Small(i128::from(value))));
}

fn put_string_list(resp: &mut PyDict, key: &str, values: &[String]) {
    resp.insert(
        key,
        PyValue::List(values.iter().cloned().map(PyValue::Str).collect()),
    );
}

fn as_dict(value: &PyValue) -> Option<&PyDict> {
    match value {
        PyValue::Dict(value) => Some(value),
        _ => None,
    }
}

fn as_str(value: &PyValue) -> Option<&str> {
    match value {
        PyValue::Str(value) => Some(value),
        _ => None,
    }
}

fn int_as_i64(value: &PyValue) -> Option<i64> {
    match value {
        PyValue::Int(PyInt::Small(value)) => i64::try_from(*value).ok(),
        _ => None,
    }
}

fn dict_str<'a>(dict: &'a PyDict, key: &str) -> Option<&'a str> {
    dict.get(key).and_then(as_str)
}
