//! endpoint 解析（#108、`cli.py:658-835`）。

use std::ffi::OsString;
use std::path::Path;
use std::time::Duration;

use crate::bench_state::{load_bench_state_doc, remembered_bench_endpoint, BenchStateError};
use crate::ctx::CliCtx;
use sw_proto::config_types::{ConfigError, RuntimeConfig};
use sw_proto::endpoint::{parse_endpoint, Endpoint};
use sw_proto::error_codes::ErrorCode;
use sw_proto::pycompat::{decode_utf8_strict, PyOsError};
use sw_proto::pypath::{HomeLookup, PathFlavor};
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
            let spec = sw_pal::EndpointSpec::Unix(OsString::from(path));
            let timeout = sw_pal::SockTimeout::Blocking(Duration::from_millis(200));
            ctx.p.rpc.connect(&spec, timeout).is_ok()
        }
    }
}

/// SC-09：`resolve_config_path(...)` 後讀取；不存在 → 空 `RuntimeConfig`。
pub fn load_runtime_config(ctx: &CliCtx) -> Result<RuntimeConfig, ConfigError> {
    let flavor = flavor_of(ctx);
    let home = EnvHome(ctx.p.env.as_ref());
    let sys_cfg_dir = ctx.p.env.system_config_dir();
    let config_path = resolve_config_path(
        &ctx.env,
        &ctx.paths,
        &sys_cfg_dir,
        flavor,
        &home,
        &|p| ctx.p.fs.exists(Path::new(p)),
    );
    let p = Path::new(&config_path);
    if !ctx.p.fs.exists(p) {
        return RuntimeConfig::from_text(None);
    }
    let bytes = ctx.p.fs.read(p).map_err(|pal_err| {
        ConfigError::Os(PyOsError {
            errno: pal_err.os_code.map(|c| c as i64),
            winerror: pal_err.winerror.map(|c| c as i64),
            strerror: Some(pal_err.strerror),
            filename: Some(config_path.clone()),
            plain: None,
        })
    })?;
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

