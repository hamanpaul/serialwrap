//! `benches.state.json` 唯讀存取（`cli.py:1115-1188`）。

use std::path::Path;

use crate::ctx::CliCtx;
use sw_proto::endpoint::is_loopback_tcp;
use sw_proto::pycompat::{decode_utf8_strict, py_strip, PyOsError};
use sw_proto::pyjson::{loads, PyDict, PyValue};

#[derive(Clone, Debug, PartialEq)]
pub enum BenchStateError {
    Value(String),
    Os(String),
}

/// 1A 只讀，不加鎖。
pub fn load_bench_state_doc(ctx: &CliCtx, path: &str) -> Result<PyDict, BenchStateError> {
    let p = Path::new(path);
    if !ctx.p.fs.exists(p) {
        return Ok(PyDict::new());
    }
    let bytes = ctx.p.fs.read(p).map_err(|pal_err| {
        let os_err = PyOsError {
            errno: pal_err.os_code.map(|c| c as i64),
            winerror: pal_err.winerror.map(|c| c as i64),
            strerror: Some(pal_err.strerror),
            filename: Some(path.to_string()),
            plain: None,
        };
        BenchStateError::Os(os_err.to_string())
    })?;
    let text = decode_utf8_strict(&bytes).map_err(|e| BenchStateError::Value(e.to_string()))?;
    let py_val = loads(&text).map_err(|e| BenchStateError::Value(e.to_string()))?;
    let payload = match py_val {
        PyValue::Dict(d) => d,
        _ => return Err(BenchStateError::Value("benches.state.json 必須是 object".to_string())),
    };
    match payload.get("benches") {
        None => Ok(payload),
        Some(PyValue::Dict(benches)) => Ok(benches.clone()),
        Some(_) => Err(BenchStateError::Value(
            "benches.state.json 的 benches 欄位必須是 object".to_string(),
        )),
    }
}

pub fn remembered_bench_endpoint(benches: &PyDict, code: &str) -> Result<Option<String>, String> {
    let endpoint_val = match benches.get(code) {
        Some(v) => v,
        None => return Ok(None),
    };
    let endpoint_str = match endpoint_val {
        PyValue::Str(s) => s.as_str(),
        _ => {
            return Err(format!(
                "bench \"{code}\" 的 endpoint 記憶損壞；請重新執行 serialwrap connect {code}"
            ));
        }
    };
    let stripped = py_strip(endpoint_str);
    if stripped.is_empty() {
        return Err(format!(
            "bench \"{code}\" 的 endpoint 記憶損壞；請重新執行 serialwrap connect {code}"
        ));
    }
    if !is_loopback_tcp(stripped) {
        return Err(format!(
            "bench \"{code}\" 的 endpoint 記憶損壞；請重新執行 serialwrap connect {code}"
        ));
    }
    Ok(Some(stripped.to_string()))
}

/// 記憶特定 bench 代號的 endpoint（加跨行程鎖；紅燈 stub）。
pub fn remember(
    _ctx: &CliCtx<'_>,
    _code: &str,
    _endpoint: &str,
) -> Result<(), BenchStateError> {
    unimplemented!("not implemented: P4-T9: bench_state::remember")
}

/// 清除特定 bench 代號的 endpoint 記憶（加跨行程鎖；紅燈 stub）。
pub fn forget(
    _ctx: &CliCtx<'_>,
    _code: &str,
) -> Result<(), BenchStateError> {
    unimplemented!("not implemented: P4-T9: bench_state::forget")
}

