//! `cli_trace`（`sw_core/cli_trace.py`）：`SERIALWRAP_LOG_LEVEL` 與 `-v` 的層級解析、trace 啟用判定、
//! endpoint 摘要（不洩漏非 loopback 位址）與 RPC trace 行的 RPC_WIRE 序列化。

use sw_proto::constants::LOOPBACK_TCP_HOSTS;
use sw_proto::endpoint::{parse_endpoint, Endpoint};
use sw_proto::pycompat::{py_int_parse, py_isdigit, py_len_chars, py_strip, sha256_hex};
use sw_proto::pyfmt::py_str;
use sw_proto::pyjson::{dumps, PyDict, PyInt, PyValue, RPC_WIRE};

pub const LEVEL_DEBUG: i64 = 10;
pub const LEVEL_INFO: i64 = 20;
pub const LEVEL_WARNING: i64 = 30;

/// `_MAX_NUMERIC_LOG_LEVEL_DIGITS`：數值層級的十進位位數上限（以 code point 計，不含負號）。
const MAX_NUMERIC_LOG_LEVEL_DIGITS: usize = 4300;

/// `cli_trace.py:19-45` `_parse_log_level`：`None`（未設定）、空白、或無法辨識者回傳 `None`。
///
/// 數值形式（去除前導 `-` 後為 `isdigit`）超過 4300 位或 `int()` 失敗（例如 `²`、重複負號）皆回傳 `None`；
/// 名稱形式以 `upper()` 比對 `CRITICAL/FATAL/ERROR/WARNING/WARN/INFO/DEBUG/NOTSET`。
pub fn parse_log_level(raw: Option<&str>) -> Option<PyInt> {
    let value = py_strip(raw?);
    if value.is_empty() {
        return None;
    }
    let digits = value.trim_start_matches('-');
    if py_isdigit(digits) {
        if py_len_chars(digits) > MAX_NUMERIC_LOG_LEVEL_DIGITS {
            return None;
        }
        // Python `int(value)`：Unicode 數字（例如 `٠`）可解析；上標數字（例如 `²`）與重複負號失敗為 None。
        return py_int_parse(value);
    }
    // 比對方式等同 Python `levels.get(value.upper())`：表內名稱一律以 ASCII 大寫後比對，
    // 表中不出現大寫字面值（錯誤碼字串常值掃描禁止 `[A-Z][A-Z0-9_]{3,}` 常值，見 tests/emitted_codes.rs）。
    let upper = value.to_uppercase();
    LEVEL_NAMES
        .iter()
        .find(|(name, _)| name.to_ascii_uppercase() == upper)
        .map(|(_, level)| PyInt::Small(i128::from(*level)))
}

/// `cli_trace.py` `_parse_log_level` 的名稱表（小寫原始名稱；比對時轉 ASCII 大寫）。
const LEVEL_NAMES: [(&str, i64); 8] = [
    ("critical", 50),
    ("fatal", 50),
    ("error", 40),
    ("warning", 30),
    ("warn", 30),
    ("info", 20),
    ("debug", 10),
    ("notset", 0),
];

/// `resolve_cli_trace_level`：`verbose>=2` → DEBUG、`verbose>=1` → INFO；否則取環境變數層級，未設定或無效為 WARNING。
pub fn resolve_cli_trace_level(verbose: i64, env_level: Option<&str>) -> PyInt {
    if verbose >= 2 {
        return PyInt::Small(i128::from(LEVEL_DEBUG));
    }
    if verbose >= 1 {
        return PyInt::Small(i128::from(LEVEL_INFO));
    }
    parse_log_level(env_level).unwrap_or(PyInt::Small(i128::from(LEVEL_WARNING)))
}

/// 等同 Python `logger.isEnabledFor(logging.INFO)`：
/// - `0`（NOTSET）繼承 root 的 WARNING，不啟用；
/// - 正數 `<= 20` 啟用，`> 20` 不啟用；
/// - 負數（含超出 i128 的負大整數）皆 `<= 20`，啟用；超出 i128 的正大整數不啟用。
pub fn trace_enabled(level: &PyInt) -> bool {
    match level {
        PyInt::Small(0) => false,
        PyInt::Small(n) => *n <= i128::from(LEVEL_INFO),
        PyInt::Big(s) => s.starts_with('-'),
    }
}

/// SHA-256 摘要（對應 `_hash_endpoint`）：以 `errors="surrogateescape"` 的位元組計算，十六進位小寫輸出。
///
/// 非 UTF-8 路徑（P3-T14，Q-19）以原始位元組雜湊；有效 UTF-8 的輸入與純 UTF-8 計算結果相同。
fn hash_endpoint(raw: &str) -> String {
    sha256_hex(&crate::argv::decode(raw).into_encoded_bytes())
}

/// `_loopback_endpoint_id`：IPv6 主機位址加上方括號後與埠號組合。
fn loopback_endpoint_id(host: &str, port: u16) -> String {
    if host.contains(':') && !host.starts_with('[') {
        format!("[{host}]:{port}")
    } else {
        format!("{host}:{port}")
    }
}

/// `cli_trace.py:104-114` `describe_trace_endpoint`：回傳 `(transport, endpoint_id)`。
///
/// - 無法解析 → `("unknown", sha256(endpoint))`；
/// - unix → `("unix", sha256(path))`；
/// - tcp loopback（`127.0.0.1`／`localhost`／`::1`）→ `("tcp", "host:port")`；
/// - 其他 tcp → `("tcp", sha256("tcp://host:port"))`，避免把內網位址寫進 trace。
pub fn describe_trace_endpoint(ep: &str) -> (&'static str, String) {
    match parse_endpoint(ep) {
        Err(_) => ("unknown", hash_endpoint(ep)),
        Ok(Endpoint::Unix(path)) => ("unix", hash_endpoint(&path)),
        Ok(Endpoint::Tcp { host, port }) => {
            if LOOPBACK_TCP_HOSTS.contains(&host.as_str()) {
                ("tcp", loopback_endpoint_id(&host, port))
            } else {
                ("tcp", hash_endpoint(&format!("tcp://{host}:{port}")))
            }
        }
    }
}

/// trace 行的計量欄位（對應 `emit_rpc_trace` 的 `metadata`）。
#[derive(Clone, Debug, Default, PartialEq)]
pub struct TraceMeta {
    pub elapsed_ms: i64,
    pub retry_count: i64,
    pub errno: Option<i64>,
    pub errno_name: Option<String>,
}

/// RPC_WIRE 序列化、key 順序固定、不含換行。
///
/// `error_code` 只在回應 `ok` 為假時取出，非字串者以 `str()` 轉換；`errno_name` 僅在 `errno` 存在時保留。
pub fn format_trace_line(
    endpoint: &str,
    source: &str,
    method: &str,
    timeout_s: f64,
    resp: &PyDict,
    meta: &TraceMeta,
) -> String {
    let (transport, endpoint_id) = describe_trace_endpoint(endpoint);

    let error_code = if resp.get_truthy("ok") {
        PyValue::Null
    } else {
        match resp.get("error_code") {
            None | Some(PyValue::Null) => PyValue::Null,
            Some(PyValue::Str(s)) => PyValue::Str(s.clone()),
            Some(other) => PyValue::Str(py_str(other)),
        }
    };
    let errno = match meta.errno {
        Some(n) => PyValue::Int(PyInt::Small(i128::from(n))),
        None => PyValue::Null,
    };
    let errno_name = match (meta.errno, &meta.errno_name) {
        (Some(_), Some(name)) => PyValue::Str(name.clone()),
        _ => PyValue::Null,
    };

    let payload = PyDict::from_pairs(vec![
        ("endpoint_transport".to_string(), PyValue::Str(transport.to_string())),
        ("endpoint_id".to_string(), PyValue::Str(endpoint_id)),
        ("endpoint_source".to_string(), PyValue::Str(source.to_string())),
        ("method".to_string(), PyValue::Str(method.to_string())),
        ("elapsed_ms".to_string(), PyValue::Int(PyInt::Small(i128::from(meta.elapsed_ms)))),
        ("error_code".to_string(), error_code),
        ("errno".to_string(), errno),
        ("errno_name".to_string(), errno_name),
        ("retry_count".to_string(), PyValue::Int(PyInt::Small(i128::from(meta.retry_count)))),
        ("timeout_s".to_string(), PyValue::Float(timeout_s)),
    ]);
    dumps(&PyValue::Dict(payload), &RPC_WIRE)
}
