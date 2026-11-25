//! endpoint 解析（#108、`cli.py:658-835`）。

use std::net::{Ipv4Addr, Ipv6Addr};
use std::path::{Path, PathBuf};
use std::time::Duration;

use crate::bench_state::{load_bench_state_doc, remembered_bench_endpoint, BenchStateError};
use crate::ctx::CliCtx;
use sw_proto::config_types::{ConfigError, RuntimeConfig};
use sw_proto::endpoint::{parse_endpoint, Endpoint};
use sw_proto::error_codes::ErrorCode;
use sw_proto::pycompat::{decode_utf8_strict, py_strip, PyOsError};
use sw_proto::pypath::{self, HomeLookup, PathFlavor};
use sw_proto::runtime_paths::{bench_state_path, resolve_config_path};
use sw_proto::yaml11::{py_truthy_of, YValue};

#[derive(Clone, Debug, PartialEq)]
pub struct ResolvedEndpoint {
    pub endpoint: String,
    /// `"--endpoint"`、`"--socket"`、`"--bench"`、`"config.yaml"`、`"default"`、`"config.yaml -> canonical fallback"`。
    pub source: &'static str,
}

#[derive(Clone, Debug, PartialEq)]
pub struct EndpointResolutionError {
    pub code: ErrorCode,
    pub message: String,
}

/// `Crash`：Python traceback 路徑（Q-18），exit 1。
#[derive(Clone, Debug, PartialEq)]
pub enum ResolveFailure {
    Resolution(EndpointResolutionError),
    Crash(String),
}

pub struct EndpointArgs<'a> {
    pub endpoint: Option<&'a str>,
    pub socket: Option<&'a str>,
    pub bench: Option<&'a str>,
}

struct EnvHome<'a>(&'a dyn sw_pal::SysEnv);

impl<'a> HomeLookup for EnvHome<'a> {
    fn current_user_home(&self) -> Option<String> {
        self.0.current_user_home()
    }
    fn user_home(&self, user: &str) -> Option<String> {
        self.0.user_home(user)
    }
}

fn flavor_of(ctx: &CliCtx) -> PathFlavor {
    match ctx.p.caps.path_flavor {
        sw_pal::PalPathFlavor::Posix => PathFlavor::Posix,
        sw_pal::PalPathFlavor::Windows => PathFlavor::Windows,
    }
}

/// Q-17。
pub fn rpc_backend_is_win(ctx: &CliCtx) -> bool {
    let raw = ctx.env.get("SERIALWRAP_RPC_BACKEND");
    match sw_pal::select::select_backend(raw, ctx.p.caps.native_backend) {
        Ok(sw_pal::BackendKind::Win) => true,
        Ok(sw_pal::BackendKind::Posix) => false,
        Err(_) => ctx.p.caps.native_backend == sw_pal::BackendKind::Win,
    }
}

/// Q-16。
pub fn local_default_endpoint(ctx: &CliCtx) -> String {
    if rpc_backend_is_win(ctx) {
        if ctx.paths.default_endpoint.starts_with("tcp://") {
            ctx.paths.default_endpoint.clone()
        } else {
            let port_str = match &ctx.paths.default_tcp_port {
                sw_proto::pyjson::PyInt::Small(n) => n.to_string(),
                sw_proto::pyjson::PyInt::Big(s) => s.clone(),
            };
            format!("tcp://127.0.0.1:{port_str}")
        }
    } else {
        ctx.paths.socket_path.clone()
    }
}

/// 判斷 endpoint 是否可作為本機管理目標；TCP 僅接受 loopback 位址。
pub fn is_local_management_endpoint(endpoint: &str) -> bool {
    match parse_endpoint(endpoint) {
        Ok(Endpoint::Unix(_)) => true,
        Ok(Endpoint::Tcp { host, .. }) => {
            host == "localhost"
                || host
                    .parse::<Ipv4Addr>()
                    .is_ok_and(|address| address.is_loopback())
                || host
                    .parse::<Ipv6Addr>()
                    .is_ok_and(|address| address.is_loopback())
        }
        Err(_) => false,
    }
}

fn local_endpoint_error(endpoint: &str) -> Option<EndpointResolutionError> {
    match parse_endpoint(endpoint) {
        Err(error) => Some(EndpointResolutionError {
            code: ErrorCode::InvalidArgs,
            message: format!("本機管理 endpoint 無效：{}", error.0),
        }),
        Ok(Endpoint::Tcp { .. }) if !is_local_management_endpoint(endpoint) => {
            Some(EndpointResolutionError {
                code: ErrorCode::RemoteNotSupported,
                message: format!("本機管理不支援非 loopback TCP endpoint：{endpoint}"),
            })
        }
        _ => None,
    }
}

/// 本機管理動作共用解析：`--endpoint`／`--socket` > 環境變數 > config.yaml > 內建預設。
/// `default_endpoint` 供 systemd-system 等固定本機模式指定最後一層預設值。
pub fn resolve_local_management_endpoint(
    ctx: &CliCtx<'_>,
    endpoint: Option<&str>,
    socket: Option<&str>,
    default_endpoint: Option<&str>,
) -> Result<ResolvedEndpoint, EndpointResolutionError> {
    let explicit = endpoint
        .filter(|value| !value.is_empty())
        .map(|value| (value, "--endpoint"))
        .or_else(|| socket.filter(|value| !value.is_empty()).map(|value| (value, "--socket")));
    let configured = if let Some((value, source)) = explicit {
        Some((value.to_string(), source))
    } else if let Some(value) = ctx.env.get("SERIALWRAP_ENDPOINT").filter(|v| !v.is_empty()) {
        Some((value.to_string(), "SERIALWRAP_ENDPOINT"))
    } else if rpc_backend_is_win(ctx) && ctx.env.get("SERIALWRAP_TCP_PORT").is_some() {
        Some((local_default_endpoint(ctx), "SERIALWRAP_TCP_PORT"))
    } else {
        let config = load_runtime_config(ctx).map_err(|error| EndpointResolutionError {
            code: ErrorCode::InternalError,
            message: format!("config.yaml 讀取失敗：{error}"),
        })?;
        match config.socket_path() {
            Some(value) if py_truthy_of(value) => match value {
                YValue::Str(value) => Some((value.clone(), "config.yaml")),
                _ => {
                    return Err(EndpointResolutionError {
                        code: ErrorCode::InvalidArgs,
                        message: "config.yaml socket_path 必須是字串".to_string(),
                    });
                }
            },
            _ => None,
        }
    };
    let (endpoint, source) = configured.unwrap_or_else(|| {
        (
            default_endpoint
                .map(str::to_string)
                .unwrap_or_else(|| local_default_endpoint(ctx)),
            "default",
        )
    });
    if let Some(error) = local_endpoint_error(&endpoint) {
        return Err(error);
    }
    Ok(ResolvedEndpoint { endpoint, source })
}

/// Windows ping 逾時後以 daemon 共用 singleton lock 確認實例狀態。
pub fn probe_instance_lock_held(ctx: &CliCtx<'_>, endpoint: &str) -> Result<bool, String> {
    let Endpoint::Tcp { host, port } = parse_endpoint(endpoint).map_err(|error| error.0)? else {
        return Err("Windows singleton lock probe 需要 TCP endpoint".to_string());
    };
    if !is_local_management_endpoint(endpoint) {
        return Err("singleton lock probe 拒絕非 loopback endpoint".to_string());
    }
    if ctx.p.caps.native_backend != sw_pal::BackendKind::Win {
        return Err("原生 Windows singleton lock probe 不適用於此平台".to_string());
    }
    let spec = sw_pal::EndpointSpec::Tcp { host, port };
    match ctx
        .p
        .instance_lock
        .acquire(Path::new(&ctx.paths.lock_path), &spec)
    {
        Ok(guard) => {
            guard.release();
            Ok(false)
        }
        Err(sw_pal::lock::InstanceLockError::AlreadyRunning) => Ok(true),
        Err(sw_pal::lock::InstanceLockError::SocketUnavailable) => {
            Err("Windows singleton lock 無法確認 endpoint 狀態".to_string())
        }
        Err(sw_pal::lock::InstanceLockError::Io(error)) => Err(error.py_message),
    }
}

/// systemd-system unit 固定的 daemon socket（0.3.2 與 0.4.0 相同）。
pub const SYSTEMD_SYSTEM_SOCKET: &str = "/run/serialwrap/serialwrapd.sock";

/// 安裝／更新／監管切換定位本機 daemon 的 endpoint；非 loopback TCP 不視為本機 instance。
/// 解析優先序與一般本機管理命令相同，systemd-system 只以固定 socket 作為最終預設。
pub fn instance_endpoint(
    ctx: &CliCtx<'_>,
    mode: &sw_pal::service_ctl::SupervisionMode,
) -> Result<String, EndpointResolutionError> {
    use sw_pal::service_ctl::SupervisionMode;
    let fallback = (mode == &SupervisionMode::SystemdSystem).then_some(SYSTEMD_SYSTEM_SOCKET);
    resolve_local_management_endpoint(ctx, None, None, fallback)
        .map(|resolved| resolved.endpoint)
}

/// 目標模式的 instance endpoint（啟新／驗證用）：與舊模式相同時沿用舊端點；否則依模式取固定值，
/// 不探測 config（新 daemon 尚未啟動，探測沒有意義）。
pub fn target_instance_endpoint(
    ctx: &CliCtx<'_>,
    target: &sw_pal::service_ctl::SupervisionMode,
    old: &sw_pal::service_ctl::SupervisionMode,
    old_endpoint: &str,
) -> Result<String, EndpointResolutionError> {
    use sw_pal::service_ctl::SupervisionMode;
    if target == old {
        if let Some(error) = local_endpoint_error(old_endpoint) {
            return Err(error);
        }
        return Ok(old_endpoint.to_string());
    }
    match target {
        SupervisionMode::SystemdSystem => Ok(SYSTEMD_SYSTEM_SOCKET.to_string()),
        SupervisionMode::WindowsTask | SupervisionMode::WindowsService => {
            resolve_local_management_endpoint(ctx, None, None, None)
                .map(|resolved| resolved.endpoint)
        }
        _ if rpc_backend_is_win(ctx) => {
            resolve_local_management_endpoint(ctx, None, None, None)
                .map(|resolved| resolved.endpoint)
        }
        _ => Ok(local_default_endpoint(ctx)),
    }
}

/// 01 §4.4，探測 0.2 s。
pub fn endpoint_alive(ctx: &CliCtx, ep: &str) -> bool {
    if ep.is_empty() {
        return true;
    }
    let parsed = match parse_endpoint(ep) {
        Ok(p) => p,
        Err(_) => return true,
    };
    match parsed {
        Endpoint::Tcp { host, port } => {
            if rpc_backend_is_win(ctx) {
                let spec = sw_pal::EndpointSpec::Tcp { host, port };
                let timeout = sw_pal::SockTimeout::Blocking(Duration::from_millis(200));
                ctx.p.rpc.connect(&spec, timeout).is_ok()
            } else {
                true
            }
        }
        Endpoint::Unix(path) => {
            if !ctx.p.caps.af_unix || !ctx.p.rpc.af_unix_available() {
                return false;
            }
            let spec = sw_pal::EndpointSpec::Unix(crate::argv::decode(&path));
            let timeout = sw_pal::SockTimeout::Blocking(Duration::from_millis(200));
            ctx.p.rpc.connect(&spec, timeout).is_ok()
        }
    }
}

/// SC-09 讀取目標：`resolve_config_path(...)`（`SERIALWRAP_CONFIG` > `CONFIG_DIR/config.yaml`（存在時）
/// > 系統唯讀 fallback（存在時）> `CONFIG_DIR/config.yaml`）。
pub fn config_read_path(ctx: &CliCtx) -> String {
    let flavor = flavor_of(ctx);
    let home = EnvHome(ctx.p.env.as_ref());
    resolve_config_path(
        &ctx.env,
        &ctx.paths,
        &ctx.paths.system_config_path,
        flavor,
        &home,
        &|p| ctx.p.fs.exists(Path::new(p)),
    )
}

/// PAL I/O 錯誤 → `ConfigError::Os`（`OSError` 形狀，檔名為 `path`）。
fn config_os_error(path: &str, pal_err: sw_pal::PalError) -> ConfigError {
    ConfigError::Os(PyOsError {
        errno: pal_err.os_code.map(|c| c as i64),
        winerror: pal_err.winerror.map(|c| c as i64),
        strerror: Some(pal_err.strerror),
        filename: Some(path.to_string()),
        plain: None,
    })
}

/// SC-09：`resolve_config_path(...)` 後讀取；不存在 → 空 `RuntimeConfig`。
pub fn load_runtime_config(ctx: &CliCtx) -> Result<RuntimeConfig, ConfigError> {
    let config_path = config_read_path(ctx);
    let p = Path::new(&config_path);
    if !ctx.p.fs.exists(p) {
        return RuntimeConfig::from_text(None);
    }
    let bytes = ctx
        .p
        .fs
        .read(p)
        .map_err(|pal_err| config_os_error(&config_path, pal_err))?;
    let text = decode_utf8_strict(&bytes)?;
    RuntimeConfig::from_text(Some(&text))
}

pub fn safe_runtime_config(ctx: &CliCtx) -> Option<RuntimeConfig> {
    load_runtime_config(ctx).ok()
}

pub fn resolve_endpoint_info(
    ctx: &mut CliCtx,
    a: &EndpointArgs,
) -> Result<ResolvedEndpoint, ResolveFailure> {
    if let Some(ep) = a.endpoint {
        if !ep.is_empty() {
            return Ok(ResolvedEndpoint {
                endpoint: ep.to_string(),
                source: "--endpoint",
            });
        }
    }
    if let Some(sock) = a.socket {
        return Ok(ResolvedEndpoint {
            endpoint: sock.to_string(),
            source: "--socket",
        });
    }
    if let Some(bench_code) = a.bench {
        if !bench_code.is_empty() {
            let flavor = flavor_of(ctx);
            let home = EnvHome(ctx.p.env.as_ref());
            let path = bench_state_path(&ctx.env, flavor, &home);
            let benches = match load_bench_state_doc(ctx, &path) {
                Ok(b) => b,
                Err(BenchStateError::Value(msg)) => {
                    return Err(ResolveFailure::Resolution(EndpointResolutionError {
                        code: ErrorCode::InvalidBenchState,
                        message: msg,
                    }));
                }
                Err(BenchStateError::Os(msg)) => {
                    return Err(ResolveFailure::Resolution(EndpointResolutionError {
                        code: ErrorCode::BenchStateIoError,
                        message: msg,
                    }));
                }
            };
            let ep_opt = match remembered_bench_endpoint(&benches, bench_code) {
                Ok(opt) => opt,
                Err(msg) => {
                    return Err(ResolveFailure::Resolution(EndpointResolutionError {
                        code: ErrorCode::InvalidBenchState,
                        message: msg,
                    }));
                }
            };
            let endpoint = match ep_opt {
                Some(ep) => ep,
                None => {
                    return Err(ResolveFailure::Resolution(EndpointResolutionError {
                        code: ErrorCode::BenchEndpointNotRemembered,
                        message: format!(
                            "bench \"{bench_code}\" 尚未記住 endpoint；請先執行 serialwrap connect {bench_code}"
                        ),
                    }));
                }
            };
            return Ok(ResolvedEndpoint {
                endpoint,
                source: "--bench",
            });
        }
    }
    let rc = safe_runtime_config(ctx);
    let cfg_sock_val = rc.as_ref().and_then(|c| c.socket_path());
    let cfg_sock_str: Option<String> = match cfg_sock_val {
        Some(val) => {
            if !py_truthy_of(val) {
                None
            } else if let YValue::Str(s) = val {
                Some(s.clone())
            } else {
                return Err(ResolveFailure::Crash(format!(
                    "TypeError: argument of type '{}' is not iterable",
                    match val {
                        YValue::Int(_) => "int",
                        YValue::Float(_) => "float",
                        YValue::Bool(_) => "bool",
                        _ => "object",
                    }
                )));
            }
        }
        None => None,
    };
    let local_default = local_default_endpoint(ctx);
    let (chosen, source) = match cfg_sock_str {
        Some(ref s) => (s.clone(), "config.yaml"),
        None => (local_default.clone(), "default"),
    };
    if let Some(ref cfg_sock) = cfg_sock_str {
        if chosen == *cfg_sock && !endpoint_alive(ctx, cfg_sock) {
            let mode = match rc.as_ref().and_then(|c| c.mode()) {
                Some(YValue::Str(s)) if !s.is_empty() => s.as_str(),
                _ => "on-demand",
            };
            let canonical = if mode == "systemd-system" {
                sw_proto::constants::SYSTEM_SOCKET.to_string()
            } else {
                local_default_endpoint(ctx)
            };
            if canonical != *cfg_sock && endpoint_alive(ctx, &canonical) {
                let msg = format!(
                    "serialwrap: config.yaml socket_path '{cfg_sock}' 不可連，依 supervision_mode={mode} 改用 '{canonical}'；如需修正請更新 config.yaml 或重跑 serialwrap setup。\n"
                );
                let _ = ctx.io.err_str(&msg);
                return Ok(ResolvedEndpoint {
                    endpoint: canonical,
                    source: "config.yaml -> canonical fallback",
                });
            }
        }
    }
    Ok(ResolvedEndpoint {
        endpoint: chosen,
        source,
    })
}

/// 回傳 1。
pub fn print_endpoint_resolution_error(
    ctx: &mut CliCtx,
    e: &EndpointResolutionError,
    context: &str,
) -> i32 {
    let resp = crate::output::error_resp(e.code, Some(&e.message));
    let _ = crate::output::print_json(&mut ctx.io, &resp);
    let _ = crate::output::mirror_err(&mut ctx.io, &resp, Some(context));
    1
}

/// SC-09 寫入目標：SERIALWRAP_CONFIG（strip 非空）> CONFIG_DIR/config.yaml；永不為系統唯讀 fallback。
///
/// 對應 `sw_core/constants.py` `resolve_config_path(for_write=True)`：寫入路徑不看檔案是否存在。
pub fn config_write_path(ctx: &CliCtx) -> PathBuf {
    if let Some(explicit) = ctx.env.get("SERIALWRAP_CONFIG") {
        if !py_strip(explicit).is_empty() {
            let home = EnvHome(ctx.p.env.as_ref());
            return PathBuf::from(pypath::expanduser(
                flavor_of(ctx),
                explicit,
                &ctx.env,
                &home,
            ));
        }
    }
    PathBuf::from(&ctx.paths.config_yaml)
}

/// `RuntimeConfig.set_*` 的寫入：mkdir parents → `to_yaml` → 直接覆寫（非原子，KEEP）。
///
/// 寫入一律走 [`config_write_path`]，不寫系統唯讀 fallback（SC-09）。換行依 `ctx.p.caps.text_newline`
/// 轉換，對應 Python `Path.write_text` 的文字模式（Windows 寫成 CRLF）。
pub fn save_runtime_config(ctx: &CliCtx, cfg: &RuntimeConfig) -> Result<(), ConfigError> {
    let path = config_write_path(ctx);
    let path_text = path.display().to_string();
    if let Some(parent) = path
        .parent()
        .filter(|parent| !parent.as_os_str().is_empty())
    {
        ctx.p
            .fs
            .mkdirs(parent)
            .map_err(|pal_err| config_os_error(&path_text, pal_err))?;
    }
    let text = cfg.to_yaml().replace('\n', ctx.p.caps.text_newline);
    ctx.p
        .fs
        .write_file(&path, text.as_bytes())
        .map_err(|pal_err| config_os_error(&path_text, pal_err))
}

fn yaml_comment_start(value: &str) -> Option<usize> {
    let bytes = value.as_bytes();
    let mut quote = None;
    let mut escaped = false;
    let mut index = 0;
    while index < bytes.len() {
        let byte = bytes[index];
        match quote {
            Some(b'"') => {
                if escaped {
                    escaped = false;
                } else if byte == b'\\' {
                    escaped = true;
                } else if byte == b'"' {
                    quote = None;
                }
            }
            Some(b'\'') => {
                if byte == b'\'' {
                    if bytes.get(index + 1) == Some(&b'\'') {
                        index += 1;
                    } else {
                        quote = None;
                    }
                }
            }
            _ => match byte {
                b'"' | b'\'' => quote = Some(byte),
                b'#' if index == 0 || bytes[index - 1].is_ascii_whitespace() => {
                    return Some(index);
                }
                _ => {}
            },
        }
        index += 1;
    }
    None
}

fn replace_scalar_style(old: &str, value: &str) -> String {
    match old.trim_start().as_bytes().first() {
        Some(b'\'') => format!("'{}'", value.replace('\'', "''")),
        Some(b'"') => format!(
            "\"{}\"",
            value.replace('\\', "\\\\").replace('"', "\\\"")
        ),
        _ => value.to_string(),
    }
}

fn root_yaml_key_matches(raw_key: &str, key: &str) -> bool {
    let raw_key = raw_key.trim();
    raw_key == key
        || (raw_key.len() >= 2
            && ((raw_key.starts_with('\'') && raw_key.ends_with('\''))
                || (raw_key.starts_with('"') && raw_key.ends_with('"')))
            && &raw_key[1..raw_key.len() - 1] == key)
}

/// 只更新根層純量鍵的值，保留其他行、註解、空白與換行位元組。
pub fn replace_root_yaml_scalar_value(text: &str, key: &str, value: &str) -> String {
    let mut output = String::with_capacity(text.len() + key.len() + value.len());
    let mut found = false;
    for line in text.split_inclusive('\n') {
        let (body, ending) = if let Some(body) = line.strip_suffix('\n') {
            (body, "\n")
        } else {
            (line, "")
        };
        let (body, carriage_return) = body
            .strip_suffix('\r')
            .map_or((body, ""), |body| (body, "\r"));
        let replacement = if !body.chars().next().is_some_and(char::is_whitespace)
            && !body.starts_with('#')
        {
            body.find(':').and_then(|colon| {
                root_yaml_key_matches(&body[..colon], key).then(|| {
                    found = true;
                    let value_start = colon + 1;
                    let rest = &body[value_start..];
                    let leading = rest.len()
                        - rest
                        .trim_start_matches([' ', '\t'])
                            .len();
                    let comment = yaml_comment_start(rest).unwrap_or(rest.len());
                    let without_comment = &rest[..comment];
                    let value_end = without_comment
                        .trim_end_matches([' ', '\t'])
                        .len();
                    let old_value = &rest[leading..value_end];
                    let scalar = replace_scalar_style(old_value, value);
                    format!(
                        "{}{}{}{}{}{}{}",
                        &body[..value_start],
                        &rest[..leading],
                        scalar,
                        &rest[value_end..comment],
                        &rest[comment..],
                        carriage_return,
                        ending
                    )
                })
            })
        } else {
            None
        };
        match replacement {
            Some(replacement) => output.push_str(&replacement),
            None => output.push_str(line),
        }
    }
    if !found {
        let newline = if text.contains("\r\n") { "\r\n" } else { "\n" };
        if !output.is_empty() && !output.ends_with('\n') {
            output.push_str(newline);
        }
        output.push_str(key);
        output.push_str(": '");
        output.push_str(&value.replace('\'', "''"));
        output.push('\'');
        output.push_str(newline);
    }
    output
}

/// 更新 config 指定鍵時保留原始 YAML 文字；`None` 欄位完全不動。
pub fn save_runtime_config_preserving(
    ctx: &CliCtx<'_>,
    mode: Option<&str>,
    socket_path: Option<&str>,
) -> Result<(), ConfigError> {
    let path = config_write_path(ctx);
    let path_text = path.display().to_string();
    let bytes = if ctx.p.fs.exists(&path) {
        ctx.p
            .fs
            .read(&path)
            .map_err(|pal_err| config_os_error(&path_text, pal_err))?
    } else {
        Vec::new()
    };
    let mut text = decode_utf8_strict(&bytes)?;
    if let Some(mode) = mode {
        text = replace_root_yaml_scalar_value(&text, "supervision_mode", mode);
    }
    if let Some(socket_path) = socket_path {
        text = replace_root_yaml_scalar_value(&text, "socket_path", socket_path);
    }
    if let Some(parent) = path
        .parent()
        .filter(|parent| !parent.as_os_str().is_empty())
    {
        ctx.p
            .fs
            .mkdirs(parent)
            .map_err(|pal_err| config_os_error(&path_text, pal_err))?;
    }
    ctx.p
        .fs
        .write_file(&path, text.as_bytes())
        .map_err(|pal_err| config_os_error(&path_text, pal_err))
}
