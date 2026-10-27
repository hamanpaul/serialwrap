//! RPC 分派器（arch §6.3，P5-T18）：39 個 served method 的平面 `match`；15 個延後 method 落入 `METHOD_NOT_FOUND`。
//!
//! 對照 Python `sw_core/service.py` 的 `SerialwrapService.rpc()`／`_dispatch()`：
//! - 分支順序與 `_dispatch` 相同；每個分支的參數轉型逐行照 Python（`str(x or d)`、`int(x or d)`、`float(x or d)`、
//!   `bool(x)`、`_coerce_rpc_bool(x)`），轉型例外以 `Err(PyErr)` 回傳，由 framing 轉為 `EXCEPTION`（例外不越過 RPC 邊界）。
//! - Slice 2.1 延後的 15 個 method（console／event／file）不寫分支，落入 `METHOD_NOT_FOUND`，無任何副作用。
//! - `rpc()`＝`dispatch()` 後 `setdefault("version", version)`（#154）。
//!
//! 與 Python 的分層差異（外部回應不變）：Python 的 `_resolve_session_id`（READY／RECOVERING／FLASHING／
//! PROFILE_NOT_COMMAND_CAPABLE／READY_UNCONFIRMED／AUTOBOOT_QUIET gate）在 Rust 由 `SessionCore::submit` 承接，
//! 本層只做 selector 解析（查無 → `SESSION_NOT_FOUND`＋原 selector）與參數轉型。

use std::path::PathBuf;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Duration;

use sw_core::arbiter::{CommandRegistry, SubmitReq};
use sw_core::mcu_patterns::McuPatternRegistry;
use sw_core::session_core::SessionRequest;
use sw_core::session_manager::SessionManager;
use sw_core::wal::{WalHealth, WalWriter, rows_to_text_lines};
use sw_pal::ProcScan;
use sw_pal::traits::PalError;
use sw_proto::pycompat::{PyErr, coerce_rpc_bool, py_truthy};
use sw_proto::pydict;
use sw_proto::pyfmt::py_str;
use sw_proto::pyjson::{PyDict, PyInt, PyValue};
use sw_proto::pyre::valid_completion_regex;
use sw_proto::rpc_types::Params;

use crate::framing::RpcHandler;

/// 一般 session 請求的等待上限。
const ROUTE_TIMEOUT: Duration = Duration::from_secs(60);
/// `session.recover`／`session.attach`／`session.self_test` 的等待上限（含登入／probe）。
const LONG_ROUTE_TIMEOUT: Duration = Duration::from_secs(600);

/// OD-11 測試 allowlist（Task 19 Produces；`resolve_test_allowlist` 的輸出，`enabled` 為 false 時不注入 service）。
/// `source`：`env`｜`config`｜`none`；`rejected_reason`：env／config 值非法時的拒絕原因（P1 T13 規格）。
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct TestAllowlist {
    pub enabled: bool,
    pub coms: Vec<String>,
    pub source: &'static str,
    pub rejected_reason: Option<String>,
}

/// RPC 服務本體（分派與 health 的共用狀態）。
pub struct Service {
    pub(crate) manager: Arc<SessionManager>,
    pub(crate) wal: Arc<WalWriter>,
    pub(crate) registry: Arc<CommandRegistry>,
    pub(crate) procs: Arc<dyn ProcScan>,
    pub(crate) version: String,
    pub(crate) started_at: String,
    /// Python `_profile_count`：只計 explicit YAML target（RPC-H-17）。
    pub(crate) profile_count: usize,
    pub(crate) allowlist: Option<TestAllowlist>,
    pub(crate) pid: u32,
    mcu: McuPatternRegistry,
    running: AtomicBool,
}

/// `Service::new` 的依賴（組裝點由 `daemon_main` 提供）。
pub struct ServiceDeps {
    pub manager: Arc<SessionManager>,
    pub wal: Arc<WalWriter>,
    pub registry: Arc<CommandRegistry>,
    pub procs: Arc<dyn ProcScan>,
    pub version: String,
    pub started_at: String,
    pub explicit_target_count: usize,
    /// Task 19：enabled 時才有值。
    pub test_device_allowlist: Option<TestAllowlist>,
    pub pid: u32,
}

pub(crate) fn int(v: i128) -> PyValue {
    PyValue::Int(PyInt::Small(v))
}

fn error(code: &str) -> PyDict {
    pydict! { "ok" => false, "error_code" => code }
}

fn invalid_args() -> PyDict {
    error("INVALID_ARGS")
}

fn session_not_found(selector: &str) -> PyDict {
    pydict! { "ok" => false, "error_code" => "SESSION_NOT_FOUND", "selector" => selector }
}

fn cmd_not_found(cmd_id: &str) -> PyDict {
    pydict! { "ok" => false, "error_code" => "CMD_NOT_FOUND", "cmd_id" => cmd_id }
}

/// OSError 穿越 → Python `str(exc)`（framing 轉 `EXCEPTION`）。
fn pal_err(e: PalError) -> PyErr {
    PyErr::value_error(e.py_message)
}

/// `str(params.get(k1) or params.get(k2) or ... or "")`。
fn first_str(p: &Params<'_>, keys: &[&str]) -> String {
    keys.iter()
        .find_map(|k| p.raw(k).filter(|v| py_truthy(v)).map(py_str))
        .unwrap_or_default()
}

/// `str(params.get("selector") or params.get("session_id") or params.get("com") or params.get("alias") or "")`。
fn selector_chain(p: &Params<'_>) -> String {
    p.selector_chain().into_iter().next().unwrap_or_default()
}

fn py_type_name(v: &PyValue) -> &'static str {
    match v {
        PyValue::Null => "NoneType",
        PyValue::Bool(_) => "bool",
        PyValue::Int(_) => "int",
        PyValue::Float(_) => "float",
        PyValue::Str(_) => "str",
        PyValue::List(_) => "list",
        PyValue::Dict(_) => "dict",
    }
}

/// Python `int(v)`（無 `or` 預設值；falsy 值亦須轉型）。
fn py_int_of(v: &PyValue) -> Result<i128, PyErr> {
    if py_truthy(v) {
        let d = pydict! { "v" => v.clone() };
        return Params::new(&d).int_or("v", 0);
    }
    match v {
        PyValue::Bool(_) | PyValue::Int(_) | PyValue::Float(_) => Ok(0),
        PyValue::Str(_) => Err(PyErr::value_error(
            "invalid literal for int() with base 10: ''",
        )),
        other => Err(PyErr::value_error(format!(
            "int() argument must be a string, a bytes-like object or a real number, not '{}'",
            py_type_name(other)
        ))),
    }
}

/// Python `float(v)`（無 `or` 預設值；falsy 值亦須轉型）。
fn py_float_of(v: &PyValue) -> Result<f64, PyErr> {
    if py_truthy(v) {
        let d = pydict! { "v" => v.clone() };
        return Params::new(&d).float_or("v", 0.0);
    }
    match v {
        PyValue::Bool(_) | PyValue::Int(_) => Ok(0.0),
        PyValue::Float(f) => Ok(*f),
        PyValue::Str(_) => Err(PyErr::value_error(
            "could not convert string to float: ''",
        )),
        other => Err(PyErr::value_error(format!(
            "float() argument must be a string or a real number, not '{}'",
            py_type_name(other)
        ))),
    }
}

/// `_wal_missing_response`：WAL 已寫過紀錄但現行檔不存在 → `WAL_MISSING`（#189）。
fn wal_missing_response(h: &WalHealth) -> Option<PyDict> {
    if h.wal_file_exists || h.current_seq == 0 {
        return None;
    }
    let hint = format!(
        "WAL 已寫過 {} 筆紀錄，但現行 WAL 檔不存在——稽核紀錄在 daemon 運行期間被移除（常見於外部工具 rmtree WAL 目錄）。消失期間的紀錄無法復原；daemon 會在下一次寫入時自動重建目錄續寫。以 `serialwrap doctor` 的 wal_writable 檢查確認目錄狀態。",
        h.current_seq
    );
    Some(pydict! {
        "ok" => false,
        "error_code" => "WAL_MISSING",
        "wal_path" => h.wal_path.clone(),
        "wal_dir" => h.wal_dir.clone(),
        "wal_dir_exists" => h.wal_dir_exists,
        "wal_file_exists" => false,
        "current_seq" => int(i128::from(h.current_seq)),
        "hint" => hint,
    })
}

fn is_ok(d: &PyDict) -> bool {
    d.get("ok") == Some(&PyValue::Bool(true))
}

fn dict_list(rows: Vec<PyDict>) -> PyValue {
    PyValue::List(rows.into_iter().map(PyValue::Dict).collect())
}

impl Service {
    pub fn new(deps: ServiceDeps) -> Service {
        Service {
            manager: deps.manager,
            wal: deps.wal,
            registry: deps.registry,
            procs: deps.procs,
            version: deps.version,
            started_at: deps.started_at,
            profile_count: deps.explicit_target_count,
            allowlist: deps.test_device_allowlist.filter(|a| a.enabled),
            pid: deps.pid,
            mcu: McuPatternRegistry::default(),
            running: AtomicBool::new(true),
        }
    }

    /// `health.status` 的 `running`（Python `_running`；daemon 停機時由 `daemon_main` 設為 false）。
    pub fn set_running(&self, running: bool) {
        self.running.store(running, Ordering::SeqCst);
    }

    pub(crate) fn is_running(&self) -> bool {
        self.running.load(Ordering::SeqCst)
    }

    /// dispatch＋`setdefault("version")`。
    pub fn rpc(&self, method: &str, params: &PyDict) -> Result<PyDict, PyErr> {
        let mut result = self.dispatch(method, params)?;
        result.setdefault("version", PyValue::from(self.version.clone()));
        Ok(result)
    }

    /// selector → session 請求；查無 → `SESSION_NOT_FOUND`＋原 selector。
    fn route(&self, selector: &str, req: SessionRequest, timeout: Duration) -> Result<PyDict, PyErr> {
        match self.manager.resolve(selector) {
            Some(sid) => self.manager.route(&sid, req, timeout),
            None => Ok(session_not_found(selector)),
        }
    }

    /// Python `SessionManager.get_session_state(selector)`。
    fn get_state(&self, selector: &str) -> Result<PyDict, PyErr> {
        self.route(selector, SessionRequest::GetState, ROUTE_TIMEOUT)
    }

    /// `log.tail_*`／`result.tail` 的 selector 過濾：空 selector → 不過濾；查無 session → `Err(回應)`。
    fn target_com(&self, selector: &str) -> Result<Result<Option<String>, PyDict>, PyErr> {
        if selector.is_empty() {
            return Ok(Ok(None));
        }
        let state = self.get_state(selector)?;
        if !is_ok(&state) {
            return Ok(Err(state));
        }
        let com = match state.get("session") {
            Some(PyValue::Dict(s)) => s.get("com").map(py_str),
            _ => None,
        };
        Ok(Ok(com))
    }

    /// 命令紀錄所屬的 session_id。
    fn cmd_owner(&self, cmd_id: &str) -> Option<String> {
        self.registry
            .get(cmd_id)
            .and_then(|r| r.get("session_id").and_then(PyValue::as_str).map(str::to_string))
    }

    /// `get_background_result`＋`_bg_fallback_from_arbiter`（兩者皆在 SessionCore 內；此處只決定路由的 session）。
    fn result_tail(&self, cmd_id: &str, from_chunk: i128, limit: i128) -> Result<PyDict, PyErr> {
        let req = || SessionRequest::ResultTail {
            cmd_id: cmd_id.to_string(),
            from_chunk,
            limit,
        };
        if let Some(sid) = self.cmd_owner(cmd_id) {
            if self.manager.resolve(&sid).as_deref() == Some(sid.as_str()) {
                return self.manager.route(&sid, req(), ROUTE_TIMEOUT);
            }
        }
        // 命令紀錄已被 history 淘汰（或 session_id 已重算）：逐一詢問可能持有 capture 的 session。
        for s in self.manager.list_sessions() {
            let Some(sid) = s.get("session_id").and_then(PyValue::as_str).map(str::to_string) else {
                continue;
            };
            let r = self.manager.route(&sid, req(), ROUTE_TIMEOUT)?;
            if is_ok(&r) {
                return Ok(r);
            }
        }
        Ok(cmd_not_found(cmd_id))
    }

    fn dispatch(&self, method: &str, params: &PyDict) -> Result<PyDict, PyErr> {
        let p = Params::new(params);
        match method {
            "health.ping" => Ok(pydict! { "ok" => true, "pong" => true }),
            "health.status" => Ok(crate::health::health(self)),

            "mcu.patterns" => Ok(pydict! { "ok" => true, "patterns" => dict_list(self.mcu.to_rpc_rows()) }),
            "mcu.status" => {
                // Slice 2.1 不承接 flash（2.4）：恆不在 FLASHING、無偵測紀錄。
                Ok(pydict! {
                    "ok" => true,
                    "candidates" => dict_list(self.manager.flash_candidates()),
                    "flashing" => false,
                    "flashing_com" => PyValue::Null,
                    "last_detect" => PyValue::Null,
                })
            }

            "device.list" => Ok(pydict! { "ok" => true, "devices" => dict_list(self.manager.list_devices()) }),
            "device.release" => {
                let selector = p.str_or("selector", "")?;
                if selector.is_empty() {
                    return Ok(invalid_args());
                }
                let source = p.str_or("source", "cli")?;
                // reason 原樣（任意 JSON）進 released_reason。
                let reason = p.raw("reason").cloned().unwrap_or(PyValue::Null);
                self.route(&selector, SessionRequest::Release { source, reason }, ROUTE_TIMEOUT)
            }
            "device.attach" => {
                let selector = p.str_or("selector", "")?;
                if selector.is_empty() {
                    return Ok(invalid_args());
                }
                // RPC-H-06 KEEP：`bool(params.get("force"))` truthiness（"false" 亦為 true）。
                let force = p.truthy("force");
                self.route(&selector, SessionRequest::DeviceAttach { force }, ROUTE_TIMEOUT)
            }

            "session.list" => Ok(pydict! { "ok" => true, "sessions" => dict_list(self.manager.list_sessions()) }),
            "session.get_state" => {
                let selector = p.str_or("selector", "")?;
                self.get_state(&selector)
            }
            "session.activity" => {
                let selector = selector_chain(&p);
                if selector.is_empty() {
                    return Ok(invalid_args());
                }
                // Python 與 session.get_state 相同（`get_session_state`）；activity_classification 已在 session 公開欄位內。
                self.get_state(&selector)
            }
            "session.self_test" => {
                let selector = selector_chain(&p);
                let timeout_s = p.float_or("timeout_s", 2.0)?;
                let strict_human_lock = coerce_rpc_bool(p.raw("strict_human_lock").unwrap_or(&PyValue::Null));
                if selector.is_empty() {
                    return Ok(invalid_args());
                }
                match self.manager.resolve(&selector) {
                    Some(sid) => self.manager.route(
                        &sid,
                        SessionRequest::SelfTest {
                            timeout_s,
                            strict_human_lock,
                        },
                        LONG_ROUTE_TIMEOUT,
                    ),
                    // `_self_test_impl` 查無分支：附 `_lease_context(None)`，外層再補 `command_capable: False`。
                    None => Ok(pydict! {
                        "ok" => false,
                        "error_code" => "SESSION_NOT_FOUND",
                        "selector" => selector,
                        "interactive_owner" => PyValue::Null,
                        "human_attached" => false,
                        "human_active" => false,
                        "recovery_mode" => false,
                        "command_capable" => false,
                    }),
                }
            }
            "session.recover" => {
                let selector = selector_chain(&p);
                let timeout_s = p.float_or("timeout_s", 2.0)?;
                let force = p.truthy("force");
                if selector.is_empty() {
                    return Ok(invalid_args());
                }
                self.route(&selector, SessionRequest::Recover { timeout_s, force }, LONG_ROUTE_TIMEOUT)
            }
            "session.clear" => {
                let selector = selector_chain(&p);
                if selector.is_empty() {
                    return Ok(invalid_args());
                }
                self.route(&selector, SessionRequest::Clear, ROUTE_TIMEOUT)
            }
            "session.bind" => {
                let selector = selector_chain(&p);
                let device_by_id = first_str(&p, &["device_by_id", "by_id"]);
                if selector.is_empty() || device_by_id.is_empty() {
                    return Ok(invalid_args());
                }
                self.manager.bind(&selector, &device_by_id)
            }
            "session.pin" => {
                let selector = selector_chain(&p);
                let profile = first_str(&p, &["profile", "profile_name"]);
                if selector.is_empty() || profile.is_empty() {
                    return Ok(invalid_args());
                }
                self.manager.pin(&selector, &profile)
            }
            "session.unpin" => {
                let selector = selector_chain(&p);
                if selector.is_empty() {
                    return Ok(invalid_args());
                }
                self.manager.unpin(&selector)
            }
            "session.attach" => {
                let selector = selector_chain(&p);
                if selector.is_empty() {
                    return Ok(invalid_args());
                }
                self.route(&selector, SessionRequest::SessionAttach, LONG_ROUTE_TIMEOUT)
            }
            "session.interactive_open" => {
                let selector = selector_chain(&p);
                let owner = p.str_or("owner", "agent")?;
                let timeout_s = p.float_or("timeout_s", 60.0)?;
                let command = p.str_or("command", "")?;
                let allow_attached = p.truthy("allow_attached");
                if selector.is_empty() {
                    return Ok(invalid_args());
                }
                self.route(
                    &selector,
                    SessionRequest::InteractiveOpen {
                        owner,
                        timeout_s,
                        command,
                        allow_attached,
                    },
                    ROUTE_TIMEOUT,
                )
            }
            "session.interactive_send" => {
                let interactive_id = p.str_or("interactive_id", "")?;
                let data = p.str_or("data", "")?;
                let encoding = p.str_or("encoding", "plain")?;
                if interactive_id.is_empty() {
                    return Ok(invalid_args());
                }
                self.manager.route_interactive(
                    &interactive_id,
                    SessionRequest::InteractiveSend {
                        interactive_id: interactive_id.clone(),
                        data,
                        encoding,
                    },
                    ROUTE_TIMEOUT,
                )
            }
            "session.interactive_status" => {
                let interactive_id = p.str_or("interactive_id", "")?;
                let screen_chars = p.int_or("screen_chars", 2048)?;
                if interactive_id.is_empty() {
                    return Ok(invalid_args());
                }
                self.manager.route_interactive(
                    &interactive_id,
                    SessionRequest::InteractiveStatus {
                        interactive_id: interactive_id.clone(),
                        screen_chars,
                    },
                    ROUTE_TIMEOUT,
                )
            }
            "session.interactive_close" => {
                let interactive_id = p.str_or("interactive_id", "")?;
                if interactive_id.is_empty() {
                    return Ok(invalid_args());
                }
                self.manager.route_interactive(
                    &interactive_id,
                    SessionRequest::InteractiveClose {
                        interactive_id: interactive_id.clone(),
                    },
                    ROUTE_TIMEOUT,
                )
            }

            "alias.list" => Ok(self.manager.alias_list()),
            "alias.set" => {
                // Python 不檢查空值（空 alias 亦寫入）。
                let session_id = p.str_or("session_id", "")?;
                let alias = p.str_or("alias", "")?;
                self.manager.alias_set(&session_id, &alias)
            }
            "alias.assign" => {
                let by_id = p.str_or("by_id", "")?;
                let alias = p.str_or("alias", "")?;
                let profile = p.raw("profile").filter(|v| py_truthy(v)).map(py_str);
                if by_id.is_empty() || alias.is_empty() {
                    return Ok(invalid_args());
                }
                self.manager.alias_assign(&by_id, &alias, profile.as_deref())
            }
            "alias.unassign" => {
                let alias = p.str_or("alias", "")?;
                self.manager.alias_unassign(&alias)
            }

            "command.submit" => {
                // completion_regex 驗證在 selector 解析之前（Python 順序）。
                let completion = p.raw("completion_regex");
                if !valid_completion_regex(completion) {
                    return Ok(error("INVALID_COMPLETION_REGEX"));
                }
                let completion_regex = completion.and_then(PyValue::as_str).map(str::to_string);
                let selector = first_str(&p, &["selector", "com", "alias"]);
                let command = first_str(&p, &["cmd", "command"]);
                let source = p.str_or("source", "agent")?;
                let mode = p.str_or("mode", "line")?;
                let timeout_s = p.float_or("timeout_s", 10.0)?;
                let priority = p.int_or("priority", 10)?;
                // `float(raw_ed) if raw_ed is not None else None`：0 → 0.0（非 None）。
                let expected_duration_s = match p.raw("expected_duration_s") {
                    None | Some(PyValue::Null) => None,
                    Some(v) => Some(py_float_of(v)?),
                };
                if selector.is_empty() {
                    return Ok(invalid_args());
                }
                let Some(sid) = self.manager.resolve(&selector) else {
                    return Ok(session_not_found(&selector));
                };
                let req = SubmitReq {
                    session_id: sid.clone(),
                    command,
                    source,
                    mode,
                    timeout_s,
                    priority,
                    expected_duration_s,
                    completion_regex,
                };
                self.manager.route(&sid, SessionRequest::Submit(req), ROUTE_TIMEOUT)
            }
            "command.get" => {
                let cmd_id = p.str_or("cmd_id", "")?;
                Ok(match self.registry.get(&cmd_id) {
                    Some(rec) => pydict! { "ok" => true, "command" => rec },
                    None => cmd_not_found(&cmd_id),
                })
            }
            "command.result_tail" => {
                let cmd_id = p.str_or("cmd_id", "")?;
                let from_chunk = p.int_or("from_chunk", 0)?;
                let limit = p.int_or("limit", 200)?;
                if cmd_id.is_empty() {
                    return Ok(invalid_args());
                }
                self.result_tail(&cmd_id, from_chunk, limit)
            }
            "command.cancel" => {
                let cmd_id = p.str_or("cmd_id", "")?;
                let result = self.registry.cancel(&cmd_id, &self.manager.now_iso());
                if is_ok(&result) {
                    // Python `_on_cancel` callback 等價：best-effort 自 session 佇列移除（結果不影響回應）。
                    if let Some(sid) = self.cmd_owner(&cmd_id) {
                        let _ = self.manager.route(
                            &sid,
                            SessionRequest::CancelQueued {
                                cmd_id: cmd_id.clone(),
                            },
                            ROUTE_TIMEOUT,
                        );
                    }
                }
                Ok(result)
            }
            "result.tail" => {
                let cmd_id = p.str_or("cmd_id", "")?;
                if !cmd_id.is_empty() {
                    let from_chunk = p.int_or("from_chunk", 0)?;
                    let limit = p.int_or("limit", 200)?;
                    return self.result_tail(&cmd_id, from_chunk, limit);
                }
                // Deprecated legacy 路徑：無 cmd_id 時 fallback 到 raw WAL tail by selector
                // （from_seq 增量語意、未帶視同 0、不加 metadata、無 WAL_MISSING 檢查）。
                let selector = first_str(&p, &["com", "selector"]);
                let from_seq = p.int_or("from_seq", 0)?;
                let limit = p.int_or("limit", 200)?;
                let com = match self.target_com(&selector)? {
                    Ok(com) => com,
                    Err(resp) => return Ok(resp),
                };
                let (rows, _) = self
                    .wal
                    .tail_raw_with_meta(Some(from_seq), com.as_deref(), limit)
                    .map_err(pal_err)?;
                Ok(pydict! { "ok" => true, "records" => dict_list(rows) })
            }
            "log.tail_raw" | "log.tail_text" => {
                let selector = first_str(&p, &["com", "selector"]);
                // 未帶 key 或 JSON null → latest 模式；顯式帶值（含 0）→ range 模式；非法值 → INVALID_ARGS（#124）。
                let from_seq = match p.raw("from_seq") {
                    None | Some(PyValue::Null) => None,
                    Some(v) => match py_int_of(v) {
                        Ok(n) => Some(n),
                        Err(_) => return Ok(invalid_args()),
                    },
                };
                let limit = p.int_or("limit", 200)?;
                let com = match self.target_com(&selector)? {
                    Ok(com) => com,
                    Err(resp) => return Ok(resp),
                };
                let wal_health = self.wal.health();
                if let Some(missing) = wal_missing_response(&wal_health) {
                    return Ok(missing);
                }
                let (rows, truncated) = self
                    .wal
                    .tail_raw_with_meta(from_seq, com.as_deref(), limit)
                    .map_err(pal_err)?;
                let last_seq = rows
                    .last()
                    .and_then(|r| r.get("seq").cloned())
                    .unwrap_or(PyValue::Null);
                let mut out = PyDict::new();
                out.insert("ok", PyValue::Bool(true));
                if method == "log.tail_raw" {
                    let n = rows.len();
                    out.insert("records", dict_list(rows));
                    out.insert("returned", int(n as i128));
                } else {
                    let lines = rows_to_text_lines(&rows);
                    let n = lines.len();
                    out.insert("lines", PyValue::List(lines.into_iter().map(PyValue::Str).collect()));
                    out.insert("returned", int(n as i128));
                }
                out.insert("from_seq", from_seq.map(int).unwrap_or(PyValue::Null));
                out.insert("last_seq", last_seq);
                out.insert("current_seq", int(i128::from(wal_health.current_seq)));
                out.insert("truncated", PyValue::Bool(truncated));
                out.insert("wal_path", PyValue::Str(wal_health.wal_path.clone()));
                out.insert("wal_file_exists", PyValue::Bool(wal_health.wal_file_exists));
                Ok(out)
            }
            "wal.range" => {
                // WAL-D13：`int(... or 0)` 轉型例外穿越（EXCEPTION），不是 INVALID_ARGS。
                let from_seq = p.int_or("from_seq", 0)?;
                let to_seq = p.int_or("to_seq", 0)?;
                let limit = p.int_or("limit", 1000)?;
                let wal_health = self.wal.health();
                if let Some(missing) = wal_missing_response(&wal_health) {
                    return Ok(missing);
                }
                let (mut rows, _) = self
                    .wal
                    .tail_raw_with_meta(Some(from_seq), None, limit)
                    .map_err(pal_err)?;
                if to_seq > 0 {
                    rows.retain(|r| {
                        r.get("seq")
                            .and_then(PyValue::as_i64)
                            .is_some_and(|s| i128::from(s) <= to_seq)
                    });
                }
                let available = self.wal.available_from_seq();
                let rotated_out = available.is_some_and(|a| from_seq + 1 < a);
                Ok(pydict! {
                    "ok" => true,
                    "records" => dict_list(rows),
                    "wal_path" => wal_health.wal_path.clone(),
                    "wal_file_exists" => wal_health.wal_file_exists,
                    "available_from_seq" => available.map(int).unwrap_or(PyValue::Null),
                    "rotated_out" => rotated_out,
                })
            }
            "wal.reset" => {
                let r = self.wal.reset().map_err(pal_err)?;
                Ok(pydict! {
                    "ok" => true,
                    "previous_seq" => int(i128::from(r.previous_seq)),
                    "rotated_suffix" => r.rotated_suffix,
                })
            }
            "wal.current_seq" => Ok(pydict! { "ok" => true, "seq" => int(i128::from(self.wal.current_seq())) }),

            "session.log_start" | "session.log_stop" | "session.log_status" => {
                let selector = selector_chain(&p);
                if selector.is_empty() {
                    return Ok(invalid_args());
                }
                let req = match method {
                    "session.log_start" => SessionRequest::LogStart,
                    "session.log_stop" => SessionRequest::LogStop,
                    _ => SessionRequest::LogStatus,
                };
                self.route(&selector, req, ROUTE_TIMEOUT)
            }

            // Slice 2.1 延後的 15 個 method（session.console_*、event.*、file.*）與未知 method：無副作用。
            _ => Ok(pydict! { "ok" => false, "error_code" => "METHOD_NOT_FOUND", "method" => method }),
        }
    }

    /// `health()` 用：已 attach session 的 `attached_real_path`（非空者）。
    pub(crate) fn attached_paths(sessions: &[PyDict]) -> Vec<PathBuf> {
        sessions
            .iter()
            .filter_map(|s| s.get("attached_real_path").and_then(PyValue::as_str))
            .filter(|p| !p.is_empty())
            .map(PathBuf::from)
            .collect()
    }
}

/// `RpcHandler` 實作：分派到 `Service::rpc`，`daemon.stop` 經 `stop` channel 通知 daemon main。
pub struct ServiceHandler {
    svc: Arc<Service>,
    stop: crossbeam_channel::Sender<()>,
}

impl ServiceHandler {
    pub fn new(svc: Arc<Service>, stop: crossbeam_channel::Sender<()>) -> ServiceHandler {
        ServiceHandler { svc, stop }
    }
}

impl RpcHandler for ServiceHandler {
    fn call(&self, method: &str, params: &PyDict) -> Result<PyDict, PyErr> {
        self.svc.rpc(method, params)
    }
    fn request_stop(&self) {
        let _ = self.stop.try_send(());
    }
}
