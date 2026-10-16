//! 常數定義（P3-T7 stub）。
#![allow(dead_code)]

pub const DEFAULT_TCP_PORT_RAW_DEFAULT: &str = "";
pub const LOOPBACK_TCP_HOSTS: [&str; 3] = ["", "", ""];
pub const RETRYABLE_READONLY_METHODS: [&str; 11] = [
    "", "", "", "", "", "", "", "", "", "", "",
];
pub const RETRY_BACKOFF_BASE_S: f64 = 0.0;
pub const RETRY_BACKOFF_MAX_S: f64 = 0.0;
pub const TRANSIENT_ERROR_CODES: [&str; 3] = ["", "", ""];
pub const PROBE_TIMEOUT_S: f64 = 0.0;
pub const PROBE_DEADLINE_S: f64 = 0.0;
pub const PROBE_METHODS: [&str; 2] = ["", ""];
pub const RECV_CHUNK: usize = 0;
pub const CLIENT_RESPONSE_MAX_BYTES: usize = 0;
pub const DEFAULT_RPC_TIMEOUT_S: f64 = 0.0;
pub const LONG_RPC_TIMEOUT_FLOOR_S: f64 = 0.0;
pub const LONG_RPC_METHODS: [&str; 4] = ["", "", "", ""];
pub const ENDPOINT_PROBE_TIMEOUT_S: f64 = 0.0;
pub const SYSTEM_SOCKET: &str = "";
pub const COMPLETION_REGEX_MAX_BYTES: usize = 0;
pub const CMD_WARN_BYTES: i64 = 0;
pub const CMD_REJECT_BYTES: i64 = 0;
pub const FILE_DEFAULT_CHUNK_SIZE: i64 = 0;
pub const FILE_ACK_MODES: [&str; 3] = ["", "", ""];
pub const DELEGATE_DEPTH_ENV: &str = "";
pub const DELEGATE_ARGV_SHA_ENV: &str = "";
pub const MAX_NUMERIC_LOG_LEVEL_DIGITS: usize = 0;
