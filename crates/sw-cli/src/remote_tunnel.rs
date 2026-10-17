//! remote 子命令的隧道核心（P4-T8；07 §8.1～§8.5、BC-05）。

use std::any::Any;
use std::fs;
use std::io;
use std::panic::{catch_unwind, resume_unwind, AssertUnwindSafe};
use std::path::{Path, PathBuf};
use std::time::Duration;

use sw_pal::fs_ops::{FileLockGuard, FsOps};
use sw_pal::process_tunnel::{ProcGroupRef, TunnelProc};
use sw_pal::traits::{InterruptScope, PalError, PalResult};
use sw_pal::PalPathFlavor;
use sw_proto::endpoint::{parse_endpoint, Endpoint};
use sw_proto::error_codes::ErrorCode;
use sw_proto::pycompat::{py_int_parse, py_isdigit, sha256_hex, PyOsError};
use sw_proto::pyfmt::{py_repr_str, py_str};
use sw_proto::pyjson::{dumps_dict, loads, PyDict, PyInt, PyValue, DISK_STABLE};

use crate::argparse::{ArgValue, Namespace};
use crate::ctx::CliCtx;

/// 隧道方向：-R（expose）或 -L（connect）。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Role {
    Expose,
    Connect,
}

/// 隧道程式：ssh 或 autossh。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Via {
    Ssh,
    Autossh,
}

/// 一條隧道的完整規格（identity 由此計算）。
#[derive(Clone, Debug, PartialEq)]
pub struct TunnelSpec {
    pub role: Role,
    pub ssh_target: String,
    pub port: u16,
    pub local: Option<u16>,
    pub forward_src: Option<String>,
    pub remote_socket: Option<String>,
    pub via: Via,
    pub ssh_opts: Vec<String>,
    pub ready_timeout: f64,
}

/// 隧道錯誤：code 為穩定錯誤碼，message 逐字照 07 §8.5。
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct TunnelError {
    pub code: &'static str,
    pub message: Option<String>,
    interrupted: bool,
}

impl TunnelError {
    fn new(code: ErrorCode, message: impl Into<String>) -> Self {
        Self {
            code: code.as_str(),
            message: Some(message.into()),
            interrupted: false,
        }
    }

    fn interrupted() -> Self {
        Self {
            code: ErrorCode::InternalError.as_str(),
            message: None,
            interrupted: true,
        }
    }
}

/// 解析 [user@]host:port。
pub fn parse_target(target: &str) -> Result<(String, u16), TunnelError> {
    let invalid = || {
        TunnelError::new(
            ErrorCode::InvalidTarget,
            format!("target 需為 [user@]host:port，取得 {}", py_repr_str(target)),
        )
    };
    // Python 的 `$` 錨點允許在單一結尾換行前匹配。
    let target = target.strip_suffix('\n').unwrap_or(target);
    let Some((authority, port_raw)) = target.rsplit_once(':') else {
        return Err(invalid());
    };
    if authority.is_empty() || port_raw.is_empty() || !py_isdigit(port_raw) {
        return Err(invalid());
    }
    if authority.contains(':') {
        return Err(invalid());
    }

    let (user, host) = match authority.split_once('@') {
        Some((user, host)) if !user.is_empty() && !host.is_empty() && !host.contains('@') => {
            (Some(user), host)
        }
        Some(_) => return Err(invalid()),
        None if !authority.contains('@') => (None, authority),
        None => return Err(invalid()),
    };
    if host.is_empty() || host.contains('@') {
        return Err(invalid());
    }

    let Some(port_value) = py_int_parse(port_raw) else {
        return Err(invalid());
    };
    let port = match port_value {
        PyInt::Small(value) if (1..=65535).contains(&value) => value as u16,
        PyInt::Big(value) => {
            return Err(TunnelError::new(
                ErrorCode::InvalidTarget,
                format!("port 超出範圍：{value}"),
            ))
        }
        PyInt::Small(value) => {
            return Err(TunnelError::new(
                ErrorCode::InvalidTarget,
                format!("port 超出範圍：{value}"),
            ))
        }
    };
    Ok((
        user.map_or_else(|| host.to_string(), |user| format!("{user}@{host}")),
        port,
    ))
}

/// identity：各欄位以 | 串接後取 SHA-256 小寫十六進位（含 None 字面）。
pub fn compute_identity(spec: &TunnelSpec) -> String {
    let role = match spec.role {
        Role::Expose => "expose",
        Role::Connect => "connect",
    };
    let via = match spec.via {
        Via::Ssh => "ssh",
        Via::Autossh => "autossh",
    };
    let canonical = [
        role.to_string(),
        spec.ssh_target.clone(),
        spec.port.to_string(),
        option_string(spec.local.map(|value| value.to_string())),
        option_string(spec.forward_src.clone()),
        option_string(spec.remote_socket.clone()),
        via.to_string(),
        spec.ssh_opts.join(";"),
    ]
    .join("|");
    sha256_hex(canonical.as_bytes())
}

fn option_string(value: Option<String>) -> String {
    value.unwrap_or_else(|| "None".to_string())
}

/// 預設 -o 選項序列（POSIX 含 ControlMaster 三組）。
pub fn default_ssh_opts(control_path: &str) -> Vec<String> {
    [
        "-o",
        "BatchMode=yes",
        "-o",
        "ExitOnForwardFailure=yes",
        "-o",
        "ControlMaster=auto",
        "-o",
    ]
    .into_iter()
    .map(str::to_string)
    .chain(std::iter::once(format!("ControlPath={control_path}")))
    .chain(
        [
            "-o",
            "ControlPersist=no",
            "-o",
            "ServerAliveInterval=30",
            "-o",
            "ServerAliveCountMax=3",
        ]
        .into_iter()
        .map(str::to_string),
    )
    .collect()
}

/// 組出 ssh／autossh argv；control_master=false（Windows）省略 Control* 三組 -o。
pub fn build_argv(spec: &TunnelSpec, control_path: &str, control_master: bool) -> Vec<String> {
    let mut argv = match spec.via {
        Via::Ssh => vec!["ssh".to_string()],
        Via::Autossh => vec!["autossh".to_string(), "-M".to_string(), "0".to_string()],
    };
    argv.extend(["-N", "-T"].into_iter().map(str::to_string));

    let mut defaults = default_ssh_opts(control_path);
    if !control_master {
        let mut filtered = Vec::with_capacity(defaults.len() - 6);
        let mut index = 0;
        while index < defaults.len() {
            if defaults[index] == "-o"
                && defaults
                    .get(index + 1)
                    .is_some_and(|option| option.starts_with("Control"))
            {
                index += 2;
            } else {
                filtered.push(defaults[index].clone());
                index += 1;
            }
        }
        defaults = filtered;
    }
    argv.extend(defaults);
    argv.extend(spec.ssh_opts.iter().cloned());

    let remote_socket = spec
        .remote_socket
        .as_deref()
        .filter(|value| !value.is_empty());
    match spec.role {
        Role::Expose => {
            let bind = remote_socket
                .map(str::to_string)
                .unwrap_or_else(|| format!("127.0.0.1:{}", spec.port));
            let forward_src = spec.forward_src.as_deref().unwrap_or("");
            argv.push("-R".to_string());
            argv.push(format!("{bind}:{forward_src}"));
        }
        Role::Connect => {
            let local = spec.local.unwrap_or(spec.port);
            let dest = remote_socket
                .map(str::to_string)
                .unwrap_or_else(|| format!("localhost:{}", spec.port));
            argv.push("-L".to_string());
            argv.push(format!("127.0.0.1:{local}:{dest}"));
        }
    }
    argv.push(spec.ssh_target.clone());
    argv
}

/// ss -ltnH 輸出中該 port 的監聽列是否全為 loopback。
pub fn bind_is_loopback_only(ss_out: &str, port: u16) -> bool {
    let suffix = format!(":{port}");
    let mut found = false;
    for line in ss_out.lines() {
        if !line.contains(&suffix) {
            continue;
        }
        for token in line
            .split_whitespace()
            .filter(|token| token.ends_with(&suffix))
        {
            let address = token
                .rsplit_once(':')
                .map(|(address, _)| address)
                .unwrap_or("");
            let address = address.trim_start_matches('[').trim_end_matches(']');
            found = true;
            if !(address == "127.0.0.1" || address.starts_with("127.") || address == "::1") {
                return false;
            }
        }
    }
    found
}

fn has_listener(ss_out: &str, port: u16) -> bool {
    let suffix = format!(":{port}");
    ss_out.lines().any(|line| {
        line.split_whitespace()
            .any(|token| token.ends_with(&suffix))
    })
}

/// <run_dir>/remote/<port>.json registry。
pub struct Registry {
    pub remote_dir: PathBuf,
}

impl Registry {
    pub fn new(run_dir: &Path) -> Self {
        Self {
            remote_dir: run_dir.join("remote"),
        }
    }

    pub fn lock(&self, fs: &dyn FsOps) -> PalResult<FileLockGuard> {
        fs.mkdirs(&self.remote_dir)?;
        fs.file_lock_exclusive(&self.lock_path(), true)
    }

    pub fn read(&self, port: i64) -> Option<PyDict> {
        let text = fs::read_to_string(self.state_path(port)).ok()?;
        match loads(&text).ok()? {
            PyValue::Dict(value) => Some(value),
            _ => None,
        }
    }

    pub fn read_all(&self) -> Vec<PyDict> {
        let Ok(entries) = fs::read_dir(&self.remote_dir) else {
            return Vec::new();
        };
        let mut paths: Vec<PathBuf> = entries
            .filter_map(Result::ok)
            .map(|entry| entry.path())
            .filter(|path| {
                path.file_name()
                    .and_then(|name| name.to_str())
                    .is_some_and(|name| name.ends_with(".json"))
            })
            .collect();
        paths.sort();
        paths
            .into_iter()
            .filter_map(|path| {
                let text = fs::read_to_string(path).ok()?;
                match loads(&text).ok()? {
                    PyValue::Dict(value) => Some(value),
                    _ => None,
                }
            })
            .collect()
    }

    pub fn write(&self, state: &PyDict, pid: u32) -> io::Result<()> {
        let Some(port) =
            state_int(state, "listen_port").and_then(|value| i64::try_from(value).ok())
        else {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "remote state 缺少有效 listen_port",
            ));
        };
        fs::create_dir_all(&self.remote_dir)?;
        let path = self.state_path(port);
        let name = path
            .file_name()
            .and_then(|name| name.to_str())
            .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "remote state 路徑無效"))?;
        let temporary = self.remote_dir.join(format!("{name}.tmp.{pid}"));
        fs::write(&temporary, dumps_dict(state, &DISK_STABLE).as_bytes())?;
        fs::rename(temporary, path)
    }

    pub fn remove(&self, port: i64) {
        for path in [
            self.state_path(port),
            self.control_path(port),
            self.log_path(port),
        ] {
            let _ = fs::remove_file(path);
        }
    }

    fn state_path(&self, port: i64) -> PathBuf {
        self.remote_dir.join(format!("{port}.json"))
    }

    fn lock_path(&self) -> PathBuf {
        self.remote_dir.join(".registry.lock")
    }

    fn control_path(&self, port: i64) -> PathBuf {
        self.remote_dir.join(format!("cm-{port}"))
    }

    fn log_path(&self, port: i64) -> PathBuf {
        self.remote_dir.join(format!("{port}.log"))
    }
}

/// 建立或重用隧道；持鎖完成衝突判定、spawn 與 spawning 登記，再於鎖外探測 readiness。
/// 在解析 target 後、解析 endpoint 前確認 ssh／autossh 可執行檔存在。
pub fn resolve_ssh_bin(ctx: &CliCtx<'_>, via: Via) -> Result<(), TunnelError> {
    let executable = match via {
        Via::Ssh => "ssh",
        Via::Autossh => "autossh",
    };
    if ctx.p.env.which(executable).is_none() {
        return Err(TunnelError::new(
            ErrorCode::SshNotFound,
            format!("PATH 找不到 {executable}"),
        ));
    }
    Ok(())
}

pub fn open_tunnel(
    ctx: &CliCtx<'_>,
    spec: &TunnelSpec,
    run_dir: &Path,
) -> Result<PyDict, TunnelError> {
    let interrupt_scope = ctx
        .p
        .env
        .interrupt_scope()
        .map_err(|error| internal_tunnel_error(error.py_message))?;
    let registry = Registry::new(run_dir);
    let listen_port = if spec.role == Role::Connect {
        spec.local.unwrap_or(spec.port)
    } else {
        spec.port
    };
    let identity = compute_identity(spec);
    let control_path = registry.control_path(i64::from(listen_port));
    let control_path_text = control_path.display().to_string();
    let supports_control_master = ctx.p.tunnel.supports_control_master();
    let mut spawned: Option<(TunnelProc, PyDict)> = None;

    let setup = catch_unwind(AssertUnwindSafe(
        || -> Result<Option<PyDict>, TunnelError> {
            let _guard = registry_lock(ctx, &registry)?;

            if let Some(existing) = registry.read(i64::from(listen_port)) {
                let pid = state_u32(&existing, "pid").unwrap_or(0);
                let alive = pid > 0
                    && ctx
                        .p
                        .tunnel
                        .alive(&proc_from_state(&existing, supports_control_master));
                if alive && state_string(&existing, "identity") == Some(identity.as_str()) {
                    let mut response = public_state(&existing);
                    response.insert("ok", PyValue::Bool(true));
                    response.insert("already_running", PyValue::Bool(true));
                    return Ok(Some(response));
                }
                if alive {
                    return Err(TunnelError::new(
                        ErrorCode::TunnelConflict,
                    format!(
                        "port {listen_port} 已有不同 identity 的隧道，請先 remote close {listen_port}"
                    ),
                ));
                }
                registry.remove(i64::from(listen_port));
            }

            let log_path = registry.log_path(i64::from(listen_port));
            let argv = build_argv(spec, &control_path_text, supports_control_master);
            let tunnel = ctx
                .p
                .tunnel
                .spawn_detached(&argv, &log_path)
                .map_err(|error| internal_tunnel_error(error.py_message))?;
            let state = new_state(
                spec,
                listen_port,
                &identity,
                &control_path_text,
                &tunnel,
                supports_control_master,
            );
            spawned = Some((tunnel, state.clone()));
            registry.write(&state, ctx.p.env.pid()).map_err(|error| {
                internal_tunnel_error(io_path_message(
                    &error,
                    &registry.state_path(i64::from(listen_port)),
                    ctx.p.caps.path_flavor,
                ))
            })?;
            Ok(None)
        },
    ));

    match setup {
        Ok(Ok(Some(response))) => return Ok(response),
        Ok(Ok(None)) => {}
        Ok(Err(error)) => {
            if let Some((tunnel, state)) = &spawned {
                if let Err(cleanup_error) =
                    cleanup_spawn(ctx, &registry, i64::from(listen_port), tunnel, state)
                {
                    return Err(cleanup_error);
                }
            }
            return Err(error);
        }
        Err(payload) => {
            if let Some((tunnel, state)) = &spawned {
                let _ = cleanup_spawn(ctx, &registry, i64::from(listen_port), tunnel, state);
            }
            resume_unwind(payload);
        }
    }

    let (tunnel, mut state) = spawned.expect("spawn success 必須保留 tunnel state");
    let ready = catch_unwind(AssertUnwindSafe(|| -> Result<(), TunnelError> {
        let log_path = registry.log_path(i64::from(listen_port));
        let status = wait_ready(
            ctx,
            spec,
            &tunnel,
            &state,
            supports_control_master,
            &log_path,
            interrupt_scope.as_ref(),
        )?;
        let _guard = registry_lock(ctx, &registry)?;
        state.insert("status", PyValue::Str(status.to_string()));
        registry.write(&state, ctx.p.env.pid()).map_err(|error| {
            internal_tunnel_error(io_path_message(
                &error,
                &registry.state_path(i64::from(listen_port)),
                ctx.p.caps.path_flavor,
            ))
        })?;
        Ok(())
    }));
    match ready {
        Ok(Ok(())) => {
            let mut response = public_state(&state);
            response.insert("ok", PyValue::Bool(true));
            Ok(response)
        }
        Ok(Err(error)) => {
            match cleanup_spawn(ctx, &registry, i64::from(listen_port), &tunnel, &state) {
                Ok(()) => Err(error),
                Err(cleanup_error) => Err(cleanup_error),
            }
        }
        Err(payload) => {
            let _ = cleanup_spawn(ctx, &registry, i64::from(listen_port), &tunnel, &state);
            resume_unwind(payload);
        }
    }
}

/// 列出隧道、prune 已死亡 state，並回報沒有 state 的 ControlPath 檔案。
pub fn status(ctx: &CliCtx<'_>, run_dir: &Path) -> PyDict {
    let registry = Registry::new(run_dir);
    let supports_control_master = ctx.p.tunnel.supports_control_master();
    let _guard = match registry.lock(ctx.p.fs.as_ref()) {
        Ok(guard) => guard,
        Err(error) => {
            return internal_error_response(pal_path_message(
                &error,
                &registry.lock_path(),
                ctx.p.caps.path_flavor,
            ))
        }
    };

    let mut tunnels = Vec::new();
    for state in registry.read_all() {
        let port = state_int(&state, "listen_port")
            .and_then(|value| i64::try_from(value).ok())
            .unwrap_or(-1);
        let pid = state_u32(&state, "pid").unwrap_or(0);
        if pid > 0
            && ctx
                .p
                .tunnel
                .alive(&proc_from_state(&state, supports_control_master))
        {
            let mut public = public_state(&state);
            public.insert("alive", PyValue::Bool(true));
            tunnels.push(PyValue::Dict(public));
        } else if port >= 0 {
            registry.remove(port);
        }
    }

    if let Ok(entries) = fs::read_dir(&registry.remote_dir) {
        let mut names: Vec<String> = entries
            .filter_map(Result::ok)
            .filter_map(|entry| entry.file_name().into_string().ok())
            .filter(|name| name.starts_with("cm-"))
            .collect();
        names.sort();
        for name in names {
            let port = name
                .strip_prefix("cm-")
                .and_then(|value| value.parse::<i64>().ok())
                .unwrap_or(-1);
            if registry.read(port).is_none() {
                tunnels.push(PyValue::Dict(PyDict::from_pairs(vec![
                    ("status".to_string(), PyValue::Str("orphan".to_string())),
                    (
                        "control_path".to_string(),
                        PyValue::Str(registry.remote_dir.join(name).display().to_string()),
                    ),
                    ("alive".to_string(), PyValue::Bool(true)),
                ])));
            }
        }
    }

    PyDict::from_pairs(vec![
        ("ok".to_string(), PyValue::Bool(true)),
        ("tunnels".to_string(), PyValue::List(tunnels)),
    ])
}

/// 依 selector 關閉隧道；找不到項目時為冪等成功。
pub fn close(ctx: &CliCtx<'_>, run_dir: &Path, selector: &str) -> PyDict {
    let registry = Registry::new(run_dir);
    let supports_control_master = ctx.p.tunnel.supports_control_master();
    let _guard = match registry.lock(ctx.p.fs.as_ref()) {
        Ok(guard) => guard,
        Err(error) => {
            return internal_error_response(pal_path_message(
                &error,
                &registry.lock_path(),
                ctx.p.caps.path_flavor,
            ))
        }
    };

    let mut targets = registry.read_all();
    if selector != "all" {
        targets.retain(|state| {
            state
                .get("listen_port")
                .is_some_and(|value| py_str(value) == selector)
        });
    }
    let mut closed = Vec::new();
    for mut state in targets {
        let Some(port) =
            state_int(&state, "listen_port").and_then(|value| i64::try_from(value).ok())
        else {
            continue;
        };
        let pid = state_u32(&state, "pid").unwrap_or(0);
        let proc = proc_from_state(&state, supports_control_master);
        if pid > 0 && ctx.p.tunnel.alive(&proc) {
            if ctx
                .p
                .tunnel
                .terminate(&proc, Duration::from_secs(5))
                .is_err()
            {
                state.insert("status", PyValue::Str("error".to_string()));
                if let Err(error) = registry.write(&state, ctx.p.env.pid()) {
                    return internal_error_response(io_path_message(
                        &error,
                        &registry.state_path(port),
                        ctx.p.caps.path_flavor,
                    ));
                }
                continue;
            }
        }
        registry.remove(port);
        closed.push(PyValue::Int(PyInt::Small(i128::from(port))));
    }
    PyDict::from_pairs(vec![
        ("ok".to_string(), PyValue::Bool(true)),
        ("closed".to_string(), PyValue::List(closed)),
    ])
}

/// CLI remote 分派：words 空或 status 列表、close／stop 關閉，其餘開通隧道。
pub fn run_remote(ns: &Namespace, ctx: &mut CliCtx<'_>) -> i32 {
    match catch_unwind(AssertUnwindSafe(|| run_remote_inner(ns, ctx))) {
        Ok(code) => code,
        Err(payload) => emit_error(
            ctx,
            ErrorCode::InternalError,
            &panic_message(payload.as_ref()),
        ),
    }
}

fn run_remote_inner(ns: &Namespace, ctx: &mut CliCtx<'_>) -> i32 {
    let words = match ns.get("words") {
        ArgValue::List(words) => words.as_slice(),
        _ => &[],
    };
    let run_dir = PathBuf::from(ctx.paths.run_dir.clone());

    if words.is_empty() || words[0] == "status" {
        let response = status(ctx, &run_dir);
        return emit_response(ctx, &response, 0, 1);
    }
    if words[0] == "close" || words[0] == "stop" {
        let selector = words.get(1).map(String::as_str).unwrap_or("all");
        let response = close(ctx, &run_dir, selector);
        return emit_response(ctx, &response, 0, 1);
    }

    let reverse = matches!(ns.get("reverse"), ArgValue::Bool(true));
    let forward = matches!(ns.get("forward"), ArgValue::Bool(true));
    if reverse && forward {
        return emit_tunnel_error(
            ctx,
            &TunnelError::new(ErrorCode::InvalidArgs, "-R 與 -L 不可同時指定"),
        );
    }
    let role = if forward { Role::Connect } else { Role::Expose };
    let local_arg = match ns.get("local") {
        ArgValue::Int(value) => Some(value),
        _ => None,
    };
    if local_arg.is_some() && role == Role::Expose {
        return emit_tunnel_error(
            ctx,
            &TunnelError::new(ErrorCode::InvalidArgs, "--local 僅用於 -L（connect）"),
        );
    }
    let local = match local_arg {
        Some(value) => {
            let Some(value) = pyint_to_i128(value) else {
                let shown = pyint_decimal(value);
                return emit_tunnel_error(
                    ctx,
                    &TunnelError::new(ErrorCode::InvalidArgs, format!("--local 超出範圍：{shown}")),
                );
            };
            if !(1..=65535).contains(&value) {
                return emit_tunnel_error(
                    ctx,
                    &TunnelError::new(ErrorCode::InvalidArgs, format!("--local 超出範圍：{value}")),
                );
            }
            Some(value as u16)
        }
        None => None,
    };

    let (ssh_target, port) = match parse_target(&words[0]) {
        Ok(parsed) => parsed,
        Err(error) => return emit_tunnel_error(ctx, &error),
    };
    let via = if matches!(ns.get("autossh"), ArgValue::Bool(true)) {
        Via::Autossh
    } else {
        Via::Ssh
    };
    if let Err(error) = resolve_ssh_bin(ctx, via) {
        return emit_tunnel_error(ctx, &error);
    }
    let ready_timeout = match ns.get("ready_timeout") {
        ArgValue::Float(value) => *value,
        _ => 10.0,
    };
    let ssh_opts = match ns.get("ssh_opt") {
        ArgValue::List(values) => values.clone(),
        _ => Vec::new(),
    };
    let remote_socket = ns.str("remote_socket").map(str::to_string);

    let forward_src = if role == Role::Expose {
        let endpoint_args = crate::endpoint_resolve::EndpointArgs {
            endpoint: ns.str("endpoint"),
            socket: ns.str("socket"),
            bench: ns.str("bench"),
        };
        let resolved = match crate::endpoint_resolve::resolve_endpoint_info(ctx, &endpoint_args) {
            Ok(resolved) => resolved,
            Err(crate::endpoint_resolve::ResolveFailure::Resolution(error)) => {
                return crate::endpoint_resolve::print_endpoint_resolution_error(
                    ctx, &error, "remote",
                )
            }
            Err(crate::endpoint_resolve::ResolveFailure::Crash(message)) => {
                return emit_error(ctx, ErrorCode::InternalError, &message)
            }
        };
        match parse_endpoint(&resolved.endpoint) {
            Ok(Endpoint::Unix(path)) => Some(path),
            Ok(Endpoint::Tcp { port, .. }) => Some(format!("127.0.0.1:{port}")),
            Err(error) => {
                return emit_tunnel_error(
                    ctx,
                    &TunnelError::new(ErrorCode::InvalidEndpoint, error.0),
                )
            }
        }
    } else {
        None
    };

    let spec = TunnelSpec {
        role,
        ssh_target,
        port,
        local,
        forward_src,
        remote_socket,
        via,
        ssh_opts,
        ready_timeout,
    };
    match open_tunnel(ctx, &spec, &run_dir) {
        Ok(response) => emit_response(ctx, &response, 0, 1),
        Err(error) if error.interrupted => 130,
        Err(error) => emit_tunnel_error(ctx, &error),
    }
}

fn wait_ready(
    ctx: &CliCtx<'_>,
    spec: &TunnelSpec,
    tunnel: &TunnelProc,
    state: &PyDict,
    control_master: bool,
    log_path: &Path,
    interrupt_scope: &dyn InterruptScope,
) -> Result<&'static str, TunnelError> {
    let endpoint = endpoint_for(spec);
    let control_path = state_string(state, "control_path").unwrap_or("");
    let start_ns = ctx.p.clock.mono_ns();
    let poll = if control_master {
        Duration::from_millis(200)
    } else {
        Duration::from_millis(500)
    };

    loop {
        if interrupt_scope.interrupted() {
            return Err(TunnelError::interrupted());
        }
        if !ctx.p.tunnel.alive(tunnel) {
            let tail = ctx.p.tunnel.stderr_tail(log_path, 500).trim().to_string();
            return Err(TunnelError::new(
                ErrorCode::TunnelSpawnFailed,
                tail.chars()
                    .rev()
                    .take(500)
                    .collect::<String>()
                    .chars()
                    .rev()
                    .collect::<String>(),
            ));
        }

        let ssh_ready = if control_master {
            let (rc, _) = run_probe(
                ctx,
                &[
                    "ssh".to_string(),
                    "-O".to_string(),
                    "check".to_string(),
                    "-o".to_string(),
                    format!("ControlPath={control_path}"),
                    spec.ssh_target.clone(),
                ],
            )?;
            rc == 0
        } else {
            true
        };
        if ssh_ready && role_probe(ctx, spec, control_path, &endpoint, control_master)? {
            return Ok("active");
        }

        let elapsed = ctx.p.clock.mono_ns().saturating_sub(start_ns) as f64 / 1_000_000_000.0;
        if elapsed >= spec.ready_timeout {
            return Ok("starting");
        }
        ctx.p.clock.sleep(poll);
        if interrupt_scope.interrupted() {
            return Err(TunnelError::interrupted());
        }
    }
}

fn role_probe(
    ctx: &CliCtx<'_>,
    spec: &TunnelSpec,
    control_path: &str,
    endpoint: &str,
    control_master: bool,
) -> Result<bool, TunnelError> {
    if spec.role == Role::Connect {
        return Ok(real_ping(ctx, endpoint));
    }
    if spec
        .remote_socket
        .as_deref()
        .is_some_and(|socket| !socket.is_empty())
    {
        return Ok(true);
    }

    let argv = if control_master {
        vec![
            "ssh".to_string(),
            "-o".to_string(),
            format!("ControlPath={control_path}"),
            spec.ssh_target.clone(),
            "ss".to_string(),
            "-ltnH".to_string(),
            format!("sport = :{}", spec.port),
        ]
    } else {
        let mut argv = vec!["ssh".to_string()];
        argv.extend(spec.ssh_opts.iter().cloned());
        argv.extend([
            "-o".to_string(),
            "BatchMode=yes".to_string(),
            spec.ssh_target.clone(),
            "ss".to_string(),
            "-ltnH".to_string(),
            format!("sport = :{}", spec.port),
        ]);
        argv
    };
    let (rc, out) = run_probe(ctx, &argv)?;

    if !control_master {
        if rc != 0 || !has_listener(&out, spec.port) {
            return Ok(false);
        }
        if !bind_is_loopback_only(&out, spec.port) {
            return Err(TunnelError::new(
                ErrorCode::RemoteBindUnverified,
                truncate_chars(out.trim(), 200),
            ));
        }
        return Ok(true);
    }

    if rc != 0 || out.trim().is_empty() {
        return Err(TunnelError::new(
            ErrorCode::RemoteBindUnverified,
            "無法在 relay 驗證遠端 bind",
        ));
    }
    if !bind_is_loopback_only(&out, spec.port) {
        return Err(TunnelError::new(
            ErrorCode::RemoteBindUnverified,
            truncate_chars(out.trim(), 200),
        ));
    }
    Ok(true)
}

fn run_probe(ctx: &CliCtx<'_>, argv: &[String]) -> Result<(i32, String), TunnelError> {
    match ctx.p.env.run_merged(argv, Some(Duration::from_secs(10))) {
        Ok(output) if output.rc == -1 => Ok((
            124,
            truncate_chars(&format!("probe timeout: {}", output.stdout), 500),
        )),
        Ok(output) => Ok((output.rc, output.stdout)),
        Err(error) => Err(internal_tunnel_error(error.py_message)),
    }
}

fn real_ping(ctx: &CliCtx<'_>, endpoint: &str) -> bool {
    let params = PyValue::Dict(PyDict::new());
    crate::client::rpc_call_once(ctx.p, endpoint, "health.ping", &params, 1, 2.0)
        .map(|(response, _)| response.get_truthy("ok"))
        .unwrap_or(false)
}

fn endpoint_for(spec: &TunnelSpec) -> String {
    let local = spec.local.unwrap_or(spec.port);
    format!("tcp://127.0.0.1:{local}")
}

fn new_state(
    spec: &TunnelSpec,
    listen_port: u16,
    identity: &str,
    control_path: &str,
    tunnel: &TunnelProc,
    control_master: bool,
) -> PyDict {
    let role = match spec.role {
        Role::Expose => "expose",
        Role::Connect => "connect",
    };
    let via = match spec.via {
        Via::Ssh => "ssh",
        Via::Autossh => "autossh",
    };
    let pgid = if control_master {
        match tunnel.group {
            ProcGroupRef::Pgid(value) => PyValue::Int(PyInt::Small(i128::from(value))),
            ProcGroupRef::WinTree => PyValue::Null,
        }
    } else {
        PyValue::Null
    };
    let start_ticks = tunnel
        .start_ticks
        .map(|value| PyValue::Int(PyInt::Small(i128::from(value))))
        .unwrap_or(PyValue::Null);
    let remote_bind = spec
        .remote_socket
        .as_deref()
        .filter(|socket| !socket.is_empty())
        .map(str::to_string)
        .unwrap_or_else(|| format!("127.0.0.1:{}", spec.port));
    PyDict::from_pairs(vec![
        ("identity".to_string(), PyValue::Str(identity.to_string())),
        ("status".to_string(), PyValue::Str("spawning".to_string())),
        ("role".to_string(), PyValue::Str(role.to_string())),
        (
            "pid".to_string(),
            PyValue::Int(PyInt::Small(i128::from(tunnel.pid))),
        ),
        ("pgid".to_string(), pgid),
        ("pid_start_ticks".to_string(), start_ticks),
        ("target".to_string(), PyValue::Str(spec.ssh_target.clone())),
        (
            "listen_port".to_string(),
            PyValue::Int(PyInt::Small(i128::from(listen_port))),
        ),
        ("remote_bind".to_string(), PyValue::Str(remote_bind)),
        (
            "forward_target".to_string(),
            spec.forward_src
                .clone()
                .map(PyValue::Str)
                .unwrap_or(PyValue::Null),
        ),
        ("via".to_string(), PyValue::Str(via.to_string())),
        (
            "control_path".to_string(),
            PyValue::Str(control_path.to_string()),
        ),
        ("endpoint".to_string(), PyValue::Str(endpoint_for(spec))),
    ])
}

fn public_state(state: &PyDict) -> PyDict {
    let mut public = PyDict::new();
    for key in [
        "status",
        "role",
        "pid",
        "listen_port",
        "target",
        "remote_bind",
        "forward_target",
        "via",
        "endpoint",
        "identity",
    ] {
        if let Some(value) = state.get(key) {
            public.insert(key, value.clone());
        }
    }
    if state_string(state, "role") == Some("expose") {
        let endpoint = state
            .get("endpoint")
            .map(py_str)
            .or_else(|| {
                state_int(state, "listen_port").map(|port| format!("tcp://127.0.0.1:{port}"))
            })
            .unwrap_or_else(|| "tcp://127.0.0.1:0".to_string());
        public.insert(
            "remote_hint",
            PyValue::Str(format!("agent 端用 serialwrap --endpoint {endpoint}")),
        );
    }
    public
}

fn proc_from_state(state: &PyDict, control_master: bool) -> TunnelProc {
    let pid = state_u32(state, "pid").unwrap_or(0);
    let group = if !control_master {
        ProcGroupRef::WinTree
    } else {
        state_int(state, "pgid")
            .and_then(|value| i32::try_from(value).ok())
            .map(ProcGroupRef::Pgid)
            .unwrap_or(ProcGroupRef::Pgid(pid as i32))
    };
    let start_ticks = state
        .get("pid_start_ticks")
        .and_then(pyvalue_to_i128)
        .and_then(|value| u64::try_from(value).ok());
    TunnelProc {
        pid,
        group,
        start_ticks,
    }
}

fn registry_lock(ctx: &CliCtx<'_>, registry: &Registry) -> Result<FileLockGuard, TunnelError> {
    registry.lock(ctx.p.fs.as_ref()).map_err(|error| {
        internal_tunnel_error(pal_path_message(
            &error,
            &registry.lock_path(),
            ctx.p.caps.path_flavor,
        ))
    })
}

fn cleanup_spawn(
    ctx: &CliCtx<'_>,
    registry: &Registry,
    port: i64,
    tunnel: &TunnelProc,
    state: &PyDict,
) -> Result<(), TunnelError> {
    let termination = catch_unwind(AssertUnwindSafe(|| {
        ctx.p.tunnel.terminate(tunnel, Duration::from_secs(5))
    }))
    .map_err(|payload| panic_message(payload.as_ref()))
    .and_then(|result| result.map_err(|error| error.py_message));
    let alive = catch_unwind(AssertUnwindSafe(|| ctx.p.tunnel.alive(tunnel)))
        .map_err(|payload| panic_message(payload.as_ref()));

    let detail = match alive {
        Ok(false) => {
            let _guard = registry_lock(ctx, registry)?;
            registry.remove(port);
            return Ok(());
        }
        Ok(true) => termination
            .err()
            .unwrap_or_else(|| "terminate 後行程仍存活".to_string()),
        Err(error) => termination.err().unwrap_or(error),
    };
    let mut retained = state.clone();
    retained.insert("status", PyValue::Str("error".to_string()));
    let _guard = registry_lock(ctx, registry)?;
    registry
        .write(&retained, ctx.p.env.pid())
        .map_err(|error| {
            internal_tunnel_error(format!(
                "隧道清理失敗：{detail}；無法保留 registry：{}",
                io_path_message(&error, &registry.state_path(port), ctx.p.caps.path_flavor,)
            ))
        })?;
    Err(internal_tunnel_error(format!("隧道清理失敗：{detail}")))
}

fn pal_path_message(error: &PalError, path: &Path, path_flavor: PalPathFlavor) -> String {
    if error.os_code.is_none() && error.winerror.is_none() {
        return error.py_message.clone();
    }
    PyOsError {
        errno: (path_flavor == PalPathFlavor::Posix)
            .then(|| error.os_code.map(i64::from))
            .flatten(),
        winerror: error.winerror.map(i64::from),
        strerror: Some(error.strerror.clone()),
        filename: Some(path.to_string_lossy().into_owned()),
        plain: None,
    }
    .to_string()
}

fn io_path_message(error: &io::Error, path: &Path, path_flavor: PalPathFlavor) -> String {
    let Some(code) = error.raw_os_error() else {
        return error.to_string();
    };
    let display = error.to_string();
    let rust_suffix = format!(" (os error {code})");
    let strerror = display.strip_suffix(&rust_suffix).unwrap_or(&display);
    PyOsError {
        errno: (path_flavor == PalPathFlavor::Posix).then_some(i64::from(code)),
        winerror: (path_flavor == PalPathFlavor::Windows).then_some(i64::from(code)),
        strerror: Some(strerror.to_string()),
        filename: Some(path.to_string_lossy().into_owned()),
        plain: None,
    }
    .to_string()
}

fn state_string<'a>(state: &'a PyDict, key: &str) -> Option<&'a str> {
    match state.get(key) {
        Some(PyValue::Str(value)) => Some(value),
        _ => None,
    }
}

fn state_int(state: &PyDict, key: &str) -> Option<i128> {
    state.get(key).and_then(pyvalue_to_i128)
}

fn state_u32(state: &PyDict, key: &str) -> Option<u32> {
    state_int(state, key).and_then(|value| u32::try_from(value).ok())
}

fn pyvalue_to_i128(value: &PyValue) -> Option<i128> {
    match value {
        PyValue::Int(PyInt::Small(value)) => Some(*value),
        PyValue::Int(PyInt::Big(value)) => value.parse().ok(),
        _ => None,
    }
}

fn pyint_to_i128(value: &PyInt) -> Option<i128> {
    match value {
        PyInt::Small(value) => Some(*value),
        PyInt::Big(value) => value.parse().ok(),
    }
}

fn pyint_decimal(value: &PyInt) -> String {
    match value {
        PyInt::Small(value) => value.to_string(),
        PyInt::Big(value) => value.clone(),
    }
}

fn internal_tunnel_error(message: impl Into<String>) -> TunnelError {
    TunnelError::new(ErrorCode::InternalError, message)
}

fn internal_error_response(message: impl Into<String>) -> PyDict {
    error_response(ErrorCode::InternalError.as_str(), &message.into())
}

fn error_response(code: &str, message: &str) -> PyDict {
    PyDict::from_pairs(vec![
        ("ok".to_string(), PyValue::Bool(false)),
        ("error_code".to_string(), PyValue::Str(code.to_string())),
        ("message".to_string(), PyValue::Str(message.to_string())),
    ])
}

fn emit_tunnel_error(ctx: &mut CliCtx<'_>, error: &TunnelError) -> i32 {
    let message = error
        .message
        .as_deref()
        .filter(|message| !message.is_empty())
        .unwrap_or(error.code);
    let response = error_response(error.code, message);
    emit_response(ctx, &response, 0, 1)
}

fn emit_error(ctx: &mut CliCtx<'_>, code: ErrorCode, message: &str) -> i32 {
    let response = error_response(code.as_str(), message);
    emit_response(ctx, &response, 0, 1)
}

fn emit_response(ctx: &mut CliCtx<'_>, response: &PyDict, success: i32, failure: i32) -> i32 {
    let _ = crate::output::print_json(&mut ctx.io, response);
    let _ = crate::output::mirror_err(&mut ctx.io, response, Some("remote"));
    if response.get_truthy("ok") {
        success
    } else {
        failure
    }
}

fn panic_message(payload: &(dyn Any + Send)) -> String {
    payload
        .downcast_ref::<String>()
        .cloned()
        .or_else(|| {
            payload
                .downcast_ref::<&str>()
                .map(|value| value.to_string())
        })
        .unwrap_or_else(|| "remote tunnel panic".to_string())
}

fn truncate_chars(value: &str, limit: usize) -> String {
    value.chars().take(limit).collect()
}

#[cfg(test)]
mod tests {
    use super::pal_path_message;
    use std::path::Path;
    use sw_pal::traits::{PalError, PalErrorKind};
    use sw_pal::PalPathFlavor;

    #[test]
    fn posix_os_error_includes_python_filename() {
        let error = PalError::new(
            PalErrorKind::Io,
            Some(21),
            None,
            "Is a directory",
            "[Errno 21] Is a directory",
        );

        assert_eq!(
            pal_path_message(
                &error,
                Path::new("/tmp/serialwrap/remote/.registry.lock"),
                PalPathFlavor::Posix,
            ),
            "[Errno 21] Is a directory: '/tmp/serialwrap/remote/.registry.lock'"
        );
    }
}
