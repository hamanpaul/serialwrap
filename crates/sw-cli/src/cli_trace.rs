//! `cli_trace`（`cli_trace.py`）：P3-T11 紅燈 stub。
#![allow(unused_variables)]

use sw_proto::pyjson::{PyDict, PyInt};

pub const LEVEL_DEBUG: i64 = 10;
pub const LEVEL_INFO: i64 = 20;
pub const LEVEL_WARNING: i64 = 30;

/// `cli_trace.py:19-45`。
pub fn parse_log_level(raw: Option<&str>) -> Option<PyInt> {
    todo!("P3-T11: parse_log_level")
}

pub fn resolve_cli_trace_level(verbose: i64, env_level: Option<&str>) -> PyInt {
    todo!("P3-T11: resolve_cli_trace_level")
}

/// level==0 → 繼承 WARNING；否則 level <= 20。
pub fn trace_enabled(level: &PyInt) -> bool {
    todo!("P3-T11: trace_enabled")
}

/// `cli_trace.py:104-114`。
pub fn describe_trace_endpoint(ep: &str) -> (&'static str, String) {
    todo!("P3-T11: describe_trace_endpoint")
}

#[derive(Clone, Debug, Default, PartialEq)]
pub struct TraceMeta {
    pub elapsed_ms: i64,
    pub retry_count: i64,
    pub errno: Option<i64>,
    pub errno_name: Option<String>,
}

/// RPC_WIRE 序列化、key 順序固定、不含換行。
pub fn format_trace_line(
    endpoint: &str,
    source: &str,
    method: &str,
    timeout_s: f64,
    resp: &PyDict,
    meta: &TraceMeta,
) -> String {
    todo!("P3-T11: format_trace_line")
}
