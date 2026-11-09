//! P8 Task 6：`file.push`／`file.pull` 的 RPC 參數轉型與 session 請求型別。
//!
//! 對照 origin/main `sw_core/service.py` 的 `file.push`／`file.pull` 分支（轉型、`ack_mode` 白名單、
//! `check_source`、必填檢查的順序與回應形狀）。session 閘門與傳輸本體在 `SessionCore`（actor）與
//! `OpRunner` 內執行；本模組只做 RPC 層的純函式轉型，不碰 session。

use sw_proto::pycompat::{py_float_parse, py_int_parse, py_truthy, PyErr};
use sw_proto::pydict;
use sw_proto::pyjson::{PyDict, PyInt, PyValue};
use sw_proto::rpc_types::Params;
use sw_proto::source_policy::{check_source, SourceCheck};

use super::templates::{ACK_MODES, DEFAULT_CHUNK_SIZE};

/// 傳輸方向。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FileTransferKind {
    Push,
    Pull,
}

/// RPC 層轉型後的 file.* 請求（`SessionRequest::FileTransfer` 的內容）。
///
/// - `source`：`check_source(params.get("source"), default="agent")` 正規化後的來源（WAL 歸因用）；
///   RPC 路徑一律 `trusted_human=False`，閘門與 lease 判定都比照 agent（OD-74、DF-02）。
/// - `local_path`：push 為非空字串；pull 只有原始值 truthy 時才是 `Some(str(x))`。
/// - `chunk_size`：push 為 `max(1, int(x or 512))`；pull 不使用（固定 512）。
/// - `ack_mode`：push 為驗證過的白名單值；pull 只驗不轉傳（固定 `"auto"`）。
#[derive(Debug, Clone, PartialEq)]
pub struct FileTransferRequest {
    pub kind: FileTransferKind,
    pub selector: String,
    pub source: String,
    pub local_path: Option<String>,
    pub remote_path: String,
    pub chunk_size: i128,
    pub chunk_timeout_s: Option<f64>,
    pub ack_mode: String,
}

/// `from_rpc` 的結果：外層 `Err` 為穿越 RPC 邊界的 Python 例外（framing `EXCEPTION`）；內層 `Err` 為直接回給
/// 呼叫端的錯誤回應（`INVALID_ARGS`、`SOURCE_RESERVED`）。
pub type ParsedFileRpc = Result<Result<(FileTransferRequest, SourceCheck), PyDict>, PyErr>;

/// 數值轉型失敗的種類：`Invalid`＝Python `TypeError`／`ValueError`（`service.py` 捕捉後回 `INVALID_ARGS`）；
/// `Overflow`＝Python `OverflowError`（不在 `except (TypeError, ValueError)` 內，穿越為 framing `EXCEPTION`）。
enum Conv {
    Invalid,
    Overflow(&'static str),
}

/// 2^127（`i128` 表示不了的 Python 大整數以正負號飽和；之後 `max(1, …)`／`min(…)` 的結果不變）。
const I128_SPAN: f64 = 170_141_183_460_469_231_731_687_303_715_884_105_728.0;

fn saturate_sign(negative: bool) -> i128 {
    if negative { i128::MIN } else { i128::MAX }
}

/// Python `int(params.get("chunk_size") or 512)`。
fn int_or_default(raw: Option<&PyValue>, default: i128) -> Result<i128, Conv> {
    let Some(value) = raw.filter(|v| py_truthy(v)) else {
        return Ok(default);
    };
    match value {
        PyValue::Float(v) if v.is_infinite() => Err(Conv::Overflow("cannot convert float infinity to integer")),
        PyValue::Float(v) if v.is_nan() => Err(Conv::Invalid),
        PyValue::Float(v) if v.trunc().abs() >= I128_SPAN => Ok(saturate_sign(*v < 0.0)),
        PyValue::Int(PyInt::Big(text)) => Ok(text.parse::<i128>().unwrap_or_else(|_| saturate_sign(text.starts_with('-')))),
        PyValue::Str(text) => match py_int_parse(text) {
            Some(PyInt::Small(v)) => Ok(v),
            Some(PyInt::Big(digits)) => Ok(digits.parse::<i128>().unwrap_or_else(|_| saturate_sign(digits.starts_with('-')))),
            None => Err(Conv::Invalid),
        },
        _ => {
            let d = pydict! { "v" => value.clone() };
            Params::new(&d).int_or("v", default).map_err(|_| Conv::Invalid)
        }
    }
}

/// Python `float(raw) if raw is not None else None`（falsy 值同樣轉型：`0`→`0.0`、`""`→`ValueError`）。
fn float_or_none(raw: Option<&PyValue>) -> Result<Option<f64>, Conv> {
    let value = match raw {
        None | Some(PyValue::Null) => return Ok(None),
        Some(v) => v,
    };
    let f = match value {
        PyValue::Bool(b) => f64::from(u8::from(*b)),
        PyValue::Int(PyInt::Small(v)) => *v as f64,
        PyValue::Int(PyInt::Big(text)) => match text.parse::<f64>() {
            Ok(v) if v.is_finite() => v,
            _ => return Err(Conv::Overflow("int too large to convert to float")),
        },
        PyValue::Float(v) => *v,
        PyValue::Str(text) => py_float_parse(text).ok_or(Conv::Invalid)?,
        PyValue::List(_) | PyValue::Dict(_) | PyValue::Null => return Err(Conv::Invalid),
    };
    Ok(Some(f))
}

fn invalid_args() -> PyDict {
    pydict! { "ok" => false, "error_code" => "INVALID_ARGS" }
}

impl FileTransferRequest {
    /// 逐行照 `service.py` 的 `file.push`／`file.pull` 分支轉型（順序：數值轉型 → `ack_mode` → `source` → 必填）。
    pub fn from_rpc(kind: FileTransferKind, params: &PyDict) -> ParsedFileRpc {
        let p = Params::new(params);
        let selector = p.str_or("selector", "")?;
        let remote_path = p.str_or("remote_path", "")?;
        // push：`str(params.get("local_path") or "")`；pull：原始值 truthy 才 `str()`。
        let local_raw = p.str_or("local_path", "")?;
        // 轉型全包（Copilot review）：TypeError／ValueError 不得穿越 RPC 邊界；OverflowError 照 Python 穿越。
        let chunk_size = match kind {
            FileTransferKind::Push => match int_or_default(p.raw("chunk_size"), DEFAULT_CHUNK_SIZE) {
                Ok(v) => v.max(1),
                Err(Conv::Invalid) => return Ok(Err(invalid_args())),
                Err(Conv::Overflow(msg)) => return Err(PyErr::value_error(msg)),
            },
            FileTransferKind::Pull => DEFAULT_CHUNK_SIZE,
        };
        let chunk_timeout_s = match float_or_none(p.raw("chunk_timeout_s")) {
            Ok(v) => v,
            Err(Conv::Invalid) => return Ok(Err(invalid_args())),
            Err(Conv::Overflow(msg)) => return Err(PyErr::value_error(msg)),
        };
        // ack_mode 白名單（#161）；pull 只驗不轉傳。
        let ack_raw = p.str_or("ack_mode", "auto")?;
        if !ACK_MODES.contains(&ack_raw.as_str()) {
            return Ok(Err(invalid_args()));
        }
        let raw_source = p.str_or("source", "")?;
        let check = check_source(Some(&raw_source), "agent", "source", true);
        if let Some(error) = check.error.clone() {
            return Ok(Err(error));
        }
        let missing = selector.is_empty()
            || remote_path.is_empty()
            || (kind == FileTransferKind::Push && local_raw.is_empty());
        if missing {
            return Ok(Err(invalid_args()));
        }
        let (local_path, ack_mode) = match kind {
            FileTransferKind::Push => (Some(local_raw), ack_raw),
            FileTransferKind::Pull => ((!local_raw.is_empty()).then_some(local_raw), "auto".to_string()),
        };
        let req = FileTransferRequest {
            kind,
            selector,
            source: check.source.clone(),
            local_path,
            remote_path,
            chunk_size,
            chunk_timeout_s,
            ack_mode,
        };
        Ok(Ok((req, check)))
    }
}
