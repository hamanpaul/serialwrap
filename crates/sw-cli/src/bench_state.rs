//! `benches.state.json`（bench endpoint 記憶）與 `benches.yaml` 存取（`cli.py:1115-1243`、`bench_registry.py`）。
//!
//! 讀取由 P3 承接；寫入與跨行程鎖由 P4-T9 承接（SC-27）：`remember`／`forget` 以 `<path>.lock`
//! 的 `FsOps::file_lock_exclusive`（POSIX flock、Windows LockFileEx）包住「讀取→修改→原子寫入」整段，
//! 寫入經 `FsOps::atomic_write`（tmp＋rename，檔案 0600，DISK_STABLE 位元組、無尾端換行）。

use std::collections::BTreeMap;
use std::path::Path;

use crate::ctx::CliCtx;
use sw_pal::traits::PalError;
use sw_pal::PalPathFlavor;
use sw_proto::bench_registry::{self, BenchConfigError, BenchEntry};
use sw_proto::endpoint::is_loopback_tcp;
use sw_proto::error_codes::ErrorCode;
use sw_proto::pycompat::{decode_utf8_strict, py_strip, PyOsError};
use sw_proto::pyjson::{dumps_dict, loads, PyDict, PyValue, DISK_STABLE};
use sw_proto::pypath::{HomeLookup, PathFlavor};
use sw_proto::runtime_paths;

#[derive(Clone, Debug, PartialEq)]
pub enum BenchStateError {
    /// 內容不合格（Python `ValueError`）→ `INVALID_BENCH_STATE`。
    Value(String),
    /// OS 層 I/O 失敗（Python `OSError`）→ `BENCH_STATE_IO_ERROR`。
    Os(String),
}

/// `HomeLookup` 轉接：以 `SysEnv` 取得使用者家目錄（與 `endpoint_resolve` 相同來源）。
struct SysEnvHome<'a>(&'a dyn sw_pal::SysEnv);

impl HomeLookup for SysEnvHome<'_> {
    fn current_user_home(&self) -> Option<String> {
        self.0.current_user_home()
    }
    fn user_home(&self, user: &str) -> Option<String> {
        self.0.user_home(user)
    }
}

fn flavor(ctx: &CliCtx) -> PathFlavor {
    match ctx.p.caps.path_flavor {
        PalPathFlavor::Posix => PathFlavor::Posix,
        PalPathFlavor::Windows => PathFlavor::Windows,
    }
}

/// `_bench_state_path()`（cli.py:1115-1122）解析後的 `benches.state.json` 路徑。
pub fn state_path(ctx: &CliCtx) -> String {
    let home = SysEnvHome(ctx.p.env.as_ref());
    runtime_paths::bench_state_path(&ctx.env, flavor(ctx), &home)
}

/// `_default_benches_path()`（bench_registry.py）：`SERIALWRAP_BENCHES_FILE` 優先，否則 `<config dir>/benches.yaml`。
pub fn benches_yaml_path(ctx: &CliCtx) -> String {
    runtime_paths::benches_yaml_path(
        &ctx.env,
        flavor(ctx),
        &SysEnvHome(ctx.p.env.as_ref()),
        &ctx.paths.config_dir,
    )
}

/// `load_configured_benches()`：檔案不存在 → 空表。ValueError 類（UTF-8、YAML 結構）保留 Python 的
/// `INVALID_BENCH_CONFIG`／`INTERNAL_ERROR` 分類；I/O 失敗與 YAML 解析例外（Python 非 ValueError）→ `INTERNAL_ERROR`。
pub fn load_configured_benches(
    ctx: &CliCtx,
) -> Result<BTreeMap<String, BenchEntry>, BenchConfigError> {
    let path = benches_yaml_path(ctx);
    let target = Path::new(&path);
    if !ctx.p.fs.exists(target) {
        return Ok(BTreeMap::new());
    }
    let bytes = ctx.p.fs.read(target).map_err(|error| BenchConfigError {
        code: ErrorCode::InternalError.as_str(),
        message: pal_os_message(&path, &error),
    })?;
    let text = decode_utf8_strict(&bytes).map_err(|error| BenchConfigError {
        code: ErrorCode::InvalidBenchConfig.as_str(),
        message: error.to_string(),
    })?;
    bench_registry::load_benches(&text)
}

/// 讀取 `benches.state.json`，回傳 benches 檢視：巢狀格式（`benches` 物件）回其內容；扁平格式（或 `benches` 為 null）回根物件。
pub fn load_bench_state_doc(ctx: &CliCtx, path: &str) -> Result<PyDict, BenchStateError> {
    let payload = read_payload(ctx, path)?;
    let nested = match payload.get("benches") {
        None | Some(PyValue::Null) => None,
        Some(PyValue::Dict(benches)) => Some(benches.clone()),
        Some(_) => {
            return Err(BenchStateError::Value(
                "benches.state.json 的 benches 欄位必須是 object".to_string(),
            ))
        }
    };
    Ok(nested.unwrap_or(payload))
}

/// 讀取 endpoint 記憶（`_remembered_bench_endpoint`）：缺項或 null → `None`；損壞 → 錯誤訊息。
pub fn remembered_bench_endpoint(benches: &PyDict, code: &str) -> Result<Option<String>, String> {
    let endpoint_val = match benches.get(code) {
        None | Some(PyValue::Null) => return Ok(None),
        Some(v) => v,
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

/// 記憶特定 bench 代號的 endpoint（`_remember_bench_endpoint(code, endpoint)`，`<path>.lock` 跨行程鎖）。
pub fn remember(ctx: &CliCtx<'_>, code: &str, endpoint: &str) -> Result<(), BenchStateError> {
    update_benches(ctx, |benches| {
        benches.insert(code, PyValue::Str(endpoint.to_string()))
    })
}

/// 清除特定 bench 代號的 endpoint 記憶（`_remember_bench_endpoint(code, None)`，`<path>.lock` 跨行程鎖）。
pub fn forget(ctx: &CliCtx<'_>, code: &str) -> Result<(), BenchStateError> {
    update_benches(ctx, |benches| {
        benches.0.shift_remove(code);
    })
}

/// `_lock_bench_state_doc` ＋ `_load_bench_state_doc` ＋ `_write_bench_state_doc` 的整段臨界區。
///
/// 扁平格式（或 `benches` 為 null）時 `edit` 直接作用於根物件；巢狀格式時作用於 `benches` 物件。
fn update_benches(ctx: &CliCtx<'_>, edit: impl FnOnce(&mut PyDict)) -> Result<(), BenchStateError> {
    let path = state_path(ctx);
    let state_dir = Path::new(&path)
        .parent()
        .filter(|dir| !dir.as_os_str().is_empty())
        .unwrap_or_else(|| Path::new("."));
    ctx.p
        .fs
        .mkdirs(state_dir)
        .map_err(|error| os_error(&state_dir.display().to_string(), &error))?;
    let lock_path = format!("{path}.lock");
    let _guard = ctx
        .p
        .fs
        .file_lock_exclusive(Path::new(&lock_path), true)
        .map_err(|error| os_error(&lock_path, &error))?;

    let mut payload = read_payload(ctx, &path)?;
    let nested = match payload.get("benches") {
        None | Some(PyValue::Null) => false,
        Some(PyValue::Dict(_)) => true,
        Some(_) => {
            return Err(BenchStateError::Value(
                "benches.state.json 的 benches 欄位必須是 object".to_string(),
            ))
        }
    };
    if nested {
        if let Some(PyValue::Dict(benches)) = payload.0.get_mut("benches") {
            edit(benches);
        }
    } else {
        edit(&mut payload);
    }
    let text = dumps_dict(&payload, &DISK_STABLE);
    ctx.p
        .fs
        .atomic_write(Path::new(&path), text.as_bytes(), None)
        .map_err(|error| os_error(&path, &error))
}

/// 讀取整個 `benches.state.json` 根物件（不存在 → 空物件）。
fn read_payload(ctx: &CliCtx, path: &str) -> Result<PyDict, BenchStateError> {
    let target = Path::new(path);
    if !ctx.p.fs.exists(target) {
        return Ok(PyDict::new());
    }
    let bytes = ctx
        .p
        .fs
        .read(target)
        .map_err(|error| os_error(path, &error))?;
    let text = decode_utf8_strict(&bytes).map_err(|e| BenchStateError::Value(e.to_string()))?;
    let py_val = loads(&text).map_err(|e| BenchStateError::Value(e.to_string()))?;
    match py_val {
        PyValue::Dict(payload) => Ok(payload),
        _ => Err(BenchStateError::Value(
            "benches.state.json 必須是 object".to_string(),
        )),
    }
}

fn os_error(path: &str, error: &PalError) -> BenchStateError {
    BenchStateError::Os(pal_os_message(path, error))
}

/// Python `str(OSError)`：`[Errno n] strerror: 'path'`（Windows 為 `[WinError w]`）。
fn pal_os_message(path: &str, error: &PalError) -> String {
    PyOsError {
        errno: error.os_code.map(i64::from),
        winerror: error.winerror.map(i64::from),
        strerror: Some(error.strerror.clone()),
        filename: Some(path.to_string()),
        plain: None,
    }
    .to_string()
}
