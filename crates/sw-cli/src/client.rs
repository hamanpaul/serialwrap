//! RPC client（白名單重試、退避、TIMEOUT 探測）：P3-T13 紅燈 stub。
#![allow(unused_variables)]

use crate::cli_trace::TraceMeta;
use sw_proto::pyjson::{PyDict, PyInt, PyValue};

#[derive(Clone, Debug, PartialEq)]
pub enum ClientCrash {
    /// Python ValueError／OverflowError。
    InvalidTimeout(String),
    /// Python UnicodeEncodeError。
    EncodeError(String),
}

pub struct RpcOutcome {
    pub resp: PyDict,
    pub meta: TraceMeta,
}

/// NaN／<0 → InvalidTimeout；>9_223_372_036.854775807 或 inf → InvalidTimeout；0 → NonBlocking；其他 → Blocking。
pub fn sock_timeout(timeout_s: f64) -> Result<sw_pal::traits::SockTimeout, ClientCrash> {
    todo!("P3-T13: sock_timeout")
}

/// RPC_WIRE：`{"id":<req_id>,"method":<method>,"params":<params>}` + `"\n"`。
pub fn encode_request(req_id: i64, method: &str, params: &PyValue) -> Vec<u8> {
    todo!("P3-T13: encode_request")
}

pub fn rpc_call_once(
    p: &sw_pal::ClientPlatform,
    endpoint: &str,
    method: &str,
    params: &PyValue,
    req_id: i64,
    timeout_s: f64,
) -> Result<(PyDict, Option<i64> /* errno */), ClientCrash> {
    todo!("P3-T13: rpc_call_once")
}

pub fn probe_daemon_after_timeout(p: &sw_pal::ClientPlatform, endpoint: &str) -> PyDict {
    todo!("P3-T13: probe_daemon_after_timeout")
}

pub fn rpc_call(
    p: &sw_pal::ClientPlatform,
    endpoint: &str,
    method: &str,
    params: &PyValue,
    req_id: i64,
    timeout_s: f64,
    retries: &PyInt,
) -> Result<RpcOutcome, ClientCrash> {
    todo!("P3-T13: rpc_call")
}

/// 只供測試以小上限驗 response cap；產品路徑一律用 `CLIENT_RESPONSE_MAX_BYTES`。
#[doc(hidden)]
pub fn rpc_call_once_capped(
    p: &sw_pal::ClientPlatform,
    endpoint: &str,
    method: &str,
    params: &PyValue,
    req_id: i64,
    timeout_s: f64,
    max_bytes: usize,
) -> Result<(PyDict, Option<i64>), ClientCrash> {
    todo!("P3-T13: rpc_call_once_capped")
}
