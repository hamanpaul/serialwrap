//! endpoint 解析（#108、`cli.py:658-835`）：P3-T12 紅燈 stub。
#![allow(unused_variables)]

use crate::ctx::CliCtx;
use sw_proto::config_types::{ConfigError, RuntimeConfig};
use sw_proto::error_codes::ErrorCode;

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

/// Q-17。
pub fn rpc_backend_is_win(ctx: &CliCtx) -> bool {
    todo!("P3-T12: rpc_backend_is_win")
}

/// Q-16。
pub fn local_default_endpoint(ctx: &CliCtx) -> String {
    todo!("P3-T12: local_default_endpoint")
}

/// 01 §4.4，探測 0.2 s。
pub fn endpoint_alive(ctx: &CliCtx, ep: &str) -> bool {
    todo!("P3-T12: endpoint_alive")
}

/// SC-09：`resolve_config_path(...)` 後讀取；不存在 → 空 `RuntimeConfig`。
pub fn load_runtime_config(ctx: &CliCtx) -> Result<RuntimeConfig, ConfigError> {
    todo!("P3-T12: load_runtime_config")
}

pub fn safe_runtime_config(ctx: &CliCtx) -> Option<RuntimeConfig> {
    todo!("P3-T12: safe_runtime_config")
}

pub fn resolve_endpoint_info(ctx: &mut CliCtx, a: &EndpointArgs) -> Result<ResolvedEndpoint, ResolveFailure> {
    todo!("P3-T12: resolve_endpoint_info")
}

/// 回傳 1。
pub fn print_endpoint_resolution_error(ctx: &mut CliCtx, e: &EndpointResolutionError, context: &str) -> i32 {
    todo!("P3-T12: print_endpoint_resolution_error")
}
