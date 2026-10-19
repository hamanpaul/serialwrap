//! RPC client：單次呼叫、唯讀白名單重試與 TIMEOUT 後探測。

use crate::cli_trace::TraceMeta;
use std::time::Duration;
use sw_pal::traits::{EndpointSpec, PalError, PalErrorKind, SockTimeout};
use sw_proto::constants::{
    CLIENT_RESPONSE_MAX_BYTES, PROBE_DEADLINE_S, PROBE_METHODS, PROBE_TIMEOUT_S, RECV_CHUNK,
    RETRYABLE_READONLY_METHODS, RETRY_BACKOFF_BASE_S, RETRY_BACKOFF_MAX_S, TRANSIENT_ERROR_CODES,
};
use sw_proto::endpoint::{parse_endpoint, Endpoint};
use sw_proto::error_codes::ErrorCode;
use sw_proto::pyjson::{dumps, loads, PyDict, PyInt, PyValue, RPC_WIRE};

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

/// RPC 參數的來源（Q-19 (b)）。
///
/// argv 衍生的值只可能含非 UTF-8 跳脫（`sw_proto::pyfmt::ESCAPE_MARK` 之後接偏移字元），送出前須攔截；
/// 由檔案內容解析而來的值（`event add --file` 的 JSON）是合法資料，其中的 NUL 等碼位屬於正常內容，不得檢查。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ParamOrigin {
    /// 參數全部來自 argv（既有所有呼叫點）。
    Argv,
    /// 參數來自檔案內容（`event add --file`）。
    Document,
}

/// NaN／<0／奈秒向上取整後達到 2**63 → InvalidTimeout；0 → NonBlocking；其他 → Blocking。
pub fn sock_timeout(timeout_s: f64) -> Result<SockTimeout, ClientCrash> {
    const PYTIME_NS_LIMIT: f64 = 9_223_372_036_854_775_808.0; // 2**63
    let timeout_ns = (timeout_s * 1_000_000_000.0).ceil();
    if timeout_s.is_nan() || timeout_s < 0.0 || !(timeout_ns < PYTIME_NS_LIMIT) {
        return Err(ClientCrash::InvalidTimeout(
            "timeout value out of range".to_string(),
        ));
    }
    if timeout_s == 0.0 {
        return Ok(SockTimeout::NonBlocking);
    }
    let timeout_ns = timeout_ns as u64;
    Ok(SockTimeout::Blocking(Duration::new(
        timeout_ns / 1_000_000_000,
        (timeout_ns % 1_000_000_000) as u32,
    )))
}

/// RPC_WIRE：`{"id":<req_id>,"method":<method>,"params":<params>}` + `"\n"`。
pub fn encode_request(req_id: i64, method: &str, params: &PyValue) -> Vec<u8> {
    let request = PyValue::Dict(PyDict::from_pairs(vec![
        (
            "id".to_string(),
            PyValue::Int(PyInt::Small(i128::from(req_id))),
        ),
        ("method".to_string(), PyValue::Str(method.to_string())),
        ("params".to_string(), params.clone()),
    ]));
    let mut encoded = dumps(&request, &RPC_WIRE).into_bytes();
    encoded.push(b'\n');
    encoded
}

pub fn rpc_call_once(
    p: &sw_pal::ClientPlatform,
    endpoint: &str,
    method: &str,
    params: &PyValue,
    req_id: i64,
    timeout_s: f64,
) -> Result<(PyDict, Option<i64> /* errno */), ClientCrash> {
    rpc_call_once_origin(
        p,
        endpoint,
        method,
        params,
        req_id,
        timeout_s,
        ParamOrigin::Argv,
    )
}

fn rpc_call_once_origin(
    p: &sw_pal::ClientPlatform,
    endpoint: &str,
    method: &str,
    params: &PyValue,
    req_id: i64,
    timeout_s: f64,
    origin: ParamOrigin,
) -> Result<(PyDict, Option<i64> /* errno */), ClientCrash> {
    rpc_call_once_capped_origin(
        p,
        endpoint,
        method,
        params,
        req_id,
        timeout_s,
        CLIENT_RESPONSE_MAX_BYTES,
        origin,
    )
}

pub fn probe_daemon_after_timeout(p: &sw_pal::ClientPlatform, endpoint: &str) -> PyDict {
    let deadline_ns = p
        .clock
        .mono_ns()
        .saturating_add((PROBE_DEADLINE_S * 1_000_000_000.0) as u64);
    let empty = PyValue::Dict(PyDict::new());
    let ping = rpc_call_once(p, endpoint, "health.ping", &empty, 0, PROBE_TIMEOUT_S)
        .map(|(response, _)| response)
        .unwrap_or_default();

    let reachable = ping.get_truthy("ok");
    let mut info = PyDict::from_pairs(vec![(
        "daemon_reachable".to_string(),
        PyValue::Bool(reachable),
    )]);
    if !reachable || p.clock.mono_ns() >= deadline_ns {
        return info;
    }

    let status = rpc_call_once(p, endpoint, "health.status", &empty, 0, PROBE_TIMEOUT_S)
        .map(|(response, _)| response)
        .unwrap_or_default();
    if status.get_truthy("ok") {
        let mut busy = PyDict::new();
        for key in ["commands", "sessions"] {
            match status.get(key) {
                // Python 的 bool 是 int 子類，故也保留布林計數。
                Some(value @ (PyValue::Int(_) | PyValue::Bool(_))) => {
                    busy.insert(key, value.clone());
                }
                _ => {}
            }
        }
        if !busy.0.is_empty() {
            info.insert("daemon_busy", PyValue::Dict(busy));
        }
    }
    info
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
    rpc_call_origin(
        p,
        endpoint,
        method,
        params,
        req_id,
        timeout_s,
        retries,
        ParamOrigin::Argv,
    )
}

/// [`rpc_call`] 的參數來源版本；`origin` 只決定送出前的非 UTF-8 攔截是否套用（見 [`ParamOrigin`]）。
#[allow(clippy::too_many_arguments)]
pub fn rpc_call_origin(
    p: &sw_pal::ClientPlatform,
    endpoint: &str,
    method: &str,
    params: &PyValue,
    req_id: i64,
    timeout_s: f64,
    retries: &PyInt,
    origin: ParamOrigin,
) -> Result<RpcOutcome, ClientCrash> {
    let started_ns = p.clock.mono_ns();
    let attempts = attempt_limit(retries, RETRYABLE_READONLY_METHODS.contains(&method));
    let mut delay_s = RETRY_BACKOFF_BASE_S;
    let mut retry_count = 0_i64;
    let mut last_errno = None;
    let mut response = PyDict::new();

    for attempt in 0..attempts {
        let (current, errno) =
            rpc_call_once_origin(p, endpoint, method, params, req_id, timeout_s, origin)?;
        response = current;
        last_errno = errno;

        let transient = matches!(response.get("error_code"), Some(PyValue::Str(code))
            if TRANSIENT_ERROR_CODES.contains(&code.as_str()));
        if response.get_truthy("ok") || !transient {
            retry_count = attempt;
            break;
        }
        if attempt + 1 < attempts {
            retry_count = attempt + 1;
            p.clock
                .sleep(Duration::from_secs_f64(delay_s.min(RETRY_BACKOFF_MAX_S)));
            delay_s *= 2.0;
        }
    }

    if !response.get_truthy("ok")
        && matches!(response.get("error_code"), Some(PyValue::Str(code)) if code == ErrorCode::Timeout.as_str())
        && !PROBE_METHODS.contains(&method)
    {
        response.update(&probe_daemon_after_timeout(p, endpoint));
    }

    let elapsed_ns = p.clock.mono_ns().saturating_sub(started_ns);
    let elapsed_ms = (elapsed_ns / 1_000_000).min(i64::MAX as u64) as i64;
    let errno_name = last_errno
        .and_then(|code| i32::try_from(code).ok())
        .and_then(|code| p.env.errno_name(code))
        .map(str::to_string);
    Ok(RpcOutcome {
        resp: response,
        meta: TraceMeta {
            elapsed_ms,
            retry_count,
            errno: last_errno,
            errno_name,
        },
    })
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
    rpc_call_once_capped_origin(
        p,
        endpoint,
        method,
        params,
        req_id,
        timeout_s,
        max_bytes,
        ParamOrigin::Argv,
    )
}

#[allow(clippy::too_many_arguments)]
fn rpc_call_once_capped_origin(
    p: &sw_pal::ClientPlatform,
    endpoint: &str,
    method: &str,
    params: &PyValue,
    req_id: i64,
    timeout_s: f64,
    max_bytes: usize,
    origin: ParamOrigin,
) -> Result<(PyDict, Option<i64>), ClientCrash> {
    let parsed = match parse_endpoint(endpoint) {
        Ok(parsed) => parsed,
        Err(error) => return Ok((failure(ErrorCode::InvalidEndpoint, Some(error.0)), None)),
    };

    let (spec, timeout) = match parsed {
        Endpoint::Unix(path) => {
            if !p.caps.af_unix || !p.rpc.af_unix_available() {
                return Ok((
                    failure(
                        ErrorCode::SocketError,
                        Some(format!(
                            "本平台無 AF_UNIX，無法連接 unix endpoint {}（請改用 tcp://127.0.0.1:<port>，#131）",
                            sw_proto::pyfmt::py_repr_argv(endpoint)
                        )),
                    ),
                    None,
                ));
            }
            (
                EndpointSpec::Unix(crate::argv::decode(&path)),
                sock_timeout(timeout_s)?,
            )
        }
        Endpoint::Tcp { host, port } => {
            // Python 於 getaddrinfo 對非 UTF-8 主機名拋未攔截的 UnicodeError（exit 1，stdout 空）。
            if crate::argv::has_escape(&host) {
                return Err(ClientCrash::EncodeError(
                    crate::argv::NOT_UTF8_MESSAGE.to_string(),
                ));
            }
            if let Err(error) = p.rpc.resolve_tcp(&host, port) {
                return Ok((socket_error(error.clone()), error.os_code.map(i64::from)));
            }
            (EndpointSpec::Tcp { host, port }, sock_timeout(timeout_s)?)
        }
    };

    let mut stream = match p.rpc.connect(&spec, timeout) {
        Ok(stream) => stream,
        Err(error) if error.kind == PalErrorKind::TimedOut => {
            return Ok((failure(ErrorCode::Timeout, None), None));
        }
        Err(error) => {
            let errno = error.os_code.map(i64::from);
            return Ok((socket_error(error), errno));
        }
    };

    // Q-19 (b)：argv 衍生參數含非 UTF-8 原文時，連線成功後、送出前以編碼錯誤結束（daemon 收到 0 筆請求行）。
    // 檔案內容來源（`ParamOrigin::Document`）不檢查：JSON 的 NUL 等碼位是合法內容。
    if origin == ParamOrigin::Argv && crate::argv::value_is_tainted(params) {
        return Err(ClientCrash::EncodeError(
            crate::argv::NOT_UTF8_MESSAGE.to_string(),
        ));
    }
    if let Err(error) = stream.send_all(&encode_request(req_id, method, params)) {
        if error.kind == PalErrorKind::TimedOut {
            return Ok((failure(ErrorCode::Timeout, None), None));
        }
        let errno = error.os_code.map(i64::from);
        return Ok((socket_error(error), errno));
    }

    let mut bytes = Vec::new();
    let mut chunk = vec![0_u8; RECV_CHUNK];
    loop {
        let read = match stream.recv(&mut chunk) {
            Ok(read) => read,
            Err(error) if error.kind == PalErrorKind::TimedOut => {
                return Ok((failure(ErrorCode::Timeout, None), None));
            }
            Err(error) => {
                let errno = error.os_code.map(i64::from);
                return Ok((socket_error(error), errno));
            }
        };
        if read == 0 {
            break;
        }
        let received = &chunk[..read];
        bytes.extend_from_slice(received);
        if received.contains(&b'\n') {
            break;
        }
        if bytes.len() > max_bytes {
            return Ok((failure(ErrorCode::InvalidResponse, None), None));
        }
    }

    let line_end = bytes
        .iter()
        .position(|byte| *byte == b'\n')
        .unwrap_or(bytes.len());
    let line = trim_ascii(&bytes[..line_end]);
    if line.is_empty() {
        return Ok((failure(ErrorCode::EmptyResponse, None), None));
    }

    let decoded = String::from_utf8_lossy(line);
    match loads(&decoded) {
        Ok(PyValue::Dict(response)) => Ok((response, None)),
        Ok(_) | Err(_) => Ok((failure(ErrorCode::InvalidResponse, None), None)),
    }
}

fn attempt_limit(retries: &PyInt, retryable: bool) -> i64 {
    if !retryable {
        return 1;
    }
    match retries {
        PyInt::Small(count) if *count > 0 => {
            if *count >= i128::from(i64::MAX) {
                i64::MAX
            } else {
                *count as i64 + 1
            }
        }
        PyInt::Big(raw) if !raw.starts_with('-') => {
            let digits = raw.trim_start_matches('0');
            if digits.is_empty() {
                1
            } else if let Ok(count) = digits.parse::<i64>() {
                count.saturating_add(1).max(1)
            } else {
                i64::MAX
            }
        }
        _ => 1,
    }
}

fn trim_ascii(mut bytes: &[u8]) -> &[u8] {
    while bytes
        .first()
        .is_some_and(|byte| is_python_bytes_space(*byte))
    {
        bytes = &bytes[1..];
    }
    while bytes
        .last()
        .is_some_and(|byte| is_python_bytes_space(*byte))
    {
        bytes = &bytes[..bytes.len() - 1];
    }
    bytes
}

fn is_python_bytes_space(byte: u8) -> bool {
    matches!(byte, b' ' | b'\t' | b'\n' | b'\r' | 0x0b | 0x0c)
}

fn failure(code: ErrorCode, message: Option<String>) -> PyDict {
    let mut fields = vec![
        ("ok".to_string(), PyValue::Bool(false)),
        (
            "error_code".to_string(),
            PyValue::Str(code.as_str().to_string()),
        ),
    ];
    if let Some(message) = message {
        fields.push(("message".to_string(), PyValue::Str(message)));
    }
    PyDict::from_pairs(fields)
}

fn socket_error(error: PalError) -> PyDict {
    failure(ErrorCode::SocketError, Some(error.py_message))
}

/// 管理面子命令的 RPC 薄包裝（P4-T1，計畫 §0.2）：`req_id` 依 P3 規則固定為 `1`，`ClientCrash` 由呼叫端轉 exit 1。
/// RPC 失敗發生的階段（P4-T7 SC-18 判定用；由 `rpc_call_once` 的內部分支直接產生，不解析訊息）。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum FailPhase {
    Connect,
    Send,
    Recv,
}

/// 型別化的 RPC 失敗 metadata（P4-T7）。`maybe_sent` 為 send_all 已開始呼叫。
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RpcFailure {
    pub phase: FailPhase,
    pub pal_kind: Option<sw_pal::PalErrorKind>,
    pub maybe_sent: bool,
}

/// 同 `call`，另回傳型別化失敗 metadata（P4-T7 紅燈骨架）。
pub fn call_with_failure(
    ctx: &crate::ctx::CliCtx,
    ep: &str,
    method: &str,
    params: &PyValue,
    timeout_s: f64,
    retries: &PyInt,
) -> (RpcOutcome, Option<RpcFailure>) {
    todo!("P4-T7: call_with_failure")
}

pub fn call(
    ctx: &crate::ctx::CliCtx,
    endpoint: &str,
    method: &str,
    params: &PyValue,
    timeout_s: f64,
    retries: &PyInt,
) -> Result<RpcOutcome, ClientCrash> {
    rpc_call(ctx.p, endpoint, method, params, 1, timeout_s, retries)
}
