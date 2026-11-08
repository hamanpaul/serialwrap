//! RPC 分派器（arch §6.3，P5-T18）：平面 `match`；Slice 2.2 承接 console 三個 method（P6 Task 10），P8 Task 6 承接 `file.push`／`file.pull`。
//!
//! 對照 Python `sw_core/service.py` 的 `SerialwrapService.rpc()`／`_dispatch()`：
//! - 分支順序與 `_dispatch` 相同；每個分支的參數轉型逐行照 Python（`str(x or d)`、`int(x or d)`、`float(x or d)`、
//!   `bool(x)`、`_coerce_rpc_bool(x)`），轉型例外以 `Err(PyErr)` 回傳，由 framing 轉為 `EXCEPTION`（例外不越過 RPC 邊界）。
//! - `file.push`／`file.pull`（P8 Task 6）：轉型與來源檢查在本層，session 閘門、前景槽與傳輸在 actor／OpRunner。
//! - `rpc()`＝`dispatch()` 後 `setdefault("version", version)`（#154）。
//!
//! 與 Python 的分層差異（外部回應不變）：Python 的 `_resolve_session_id`（READY／RECOVERING／FLASHING／
//! PROFILE_NOT_COMMAND_CAPABLE／READY_UNCONFIRMED／AUTOBOOT_QUIET gate）在 Rust 由 `SessionCore::submit` 承接，
//! 本層只做 selector 解析（查無 → `SESSION_NOT_FOUND`＋原 selector）與參數轉型。

use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};
use std::path::PathBuf;
use std::sync::{Arc, Mutex};
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Duration;

use sw_core::admission::{AdmissionFlag, AdmissionGate, AdmissionTicket, Busy, BusyCom, FreezeOutcome, Ticket};
use sw_core::arbiter::{CommandRegistry, SubmitOrigin, SubmitReq, expect_reboot_rejection};
use sw_core::event_engine::registry::UpsertError;
use sw_core::event_engine::schema::RuleError;
use sw_core::event_engine::EventEngine;
use sw_core::file_transfer::{FileTransferKind, FileTransferRequest};
use sw_core::flash_endpoint::{FlashCoordinator, FlashEndpoint};
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
use sw_proto::source_policy::{classify_source, check_source, SourceCheck};

use crate::framing::RpcHandler;

/// 一般 session 請求的等待上限。
const ROUTE_TIMEOUT: Duration = Duration::from_secs(60);
/// `session.recover`／`session.attach`／`session.self_test` 的等待上限（含登入／probe）。
const LONG_ROUTE_TIMEOUT: Duration = Duration::from_secs(600);

/// P8 Task 14：分派前須先取得 admission ticket 的 RPC 方法（Python `_ADMISSION_METHODS`：`contracts/rpc_schema.json` 中
/// `side_effects` 非空且 `retryable_readonly==false` 者，排除 framing 攔截的 `daemon.stop`／`daemon.handoff_stop`，以及
/// 由 `CommandRegistry::admit` 於命令驗證之後才取 ticket 的 `command.submit`）。凍結後回 `FLUSHED_BY_SHUTDOWN`＋
/// `reason:"handoff_frozen"`，零副作用。
const ADMISSION_METHODS: &[&str] = &[
    "alias.assign",
    "alias.set",
    "alias.unassign",
    "command.cancel",
    "command.result_tail",
    "device.attach",
    "device.release",
    "event.com_disable",
    "event.com_enable",
    "event.reload",
    "event.reset",
    "event.rule_delete",
    "event.rule_set",
    "file.pull",
    "file.push",
    "result.tail",
    "session.attach",
    "session.bind",
    "session.clear",
    "session.console_attach",
    "session.console_detach",
    "session.interactive_close",
    "session.interactive_open",
    "session.interactive_send",
    "session.interactive_status",
    "session.log_start",
    "session.log_stop",
    "session.pin",
    "session.recover",
    "session.self_test",
    "session.unpin",
    "wal.reset",
];

/// P8 Task 14：交接凍結時每個 TX fence 的收斂期限（Python `freeze_tx` 的 `DRAIN_FENCE_ACQUIRE_S`＝0.05 s）。
const HANDOFF_TX_FENCE_TIMEOUT: Duration = Duration::from_millis(50);

/// admission ticket 的 RAII 守衛：分派結束（含 `Err`／panic unwind）即歸還，除非以 `take` 移交給持有到終態的表。
struct TicketGuard {
    gate: Arc<AdmissionGate>,
    ticket: Option<Ticket>,
}

impl TicketGuard {
    fn take(mut self) -> Option<Ticket> {
        self.ticket.take()
    }

    fn into_async_ticket(&mut self) -> Option<Arc<AdmissionTicket>> {
        self.ticket
            .take()
            .map(|ticket| Arc::new(AdmissionTicket::new(Arc::clone(&self.gate), ticket)))
    }
}

impl Drop for TicketGuard {
    fn drop(&mut self) {
        if let Some(ticket) = self.ticket.take() {
            self.gate.done(ticket);
        }
    }
}

/// P8 Task 14：`CommandRegistry::admit` 的凍結拒絕經 actor 回覆時會被補上 `session`（actor 對入列錯誤的慣例）；Python
/// `CommandArbiter.submit` 的凍結回應只有 `ok`／`error_code`／`reason`，與其他有副作用方法的凍結回應同形，故移除。
fn strip_frozen_session(mut result: PyDict) -> PyDict {
    let frozen = result.get("error_code").and_then(PyValue::as_str) == Some("FLUSHED_BY_SHUTDOWN")
        && result.get("reason").and_then(PyValue::as_str) == Some("handoff_frozen");
    if frozen {
        result.0.shift_remove("session");
    }
    result
}

fn handoff_frozen_response() -> PyDict {
    pydict! { "ok" => false, "error_code" => "FLUSHED_BY_SHUTDOWN", "reason" => "handoff_frozen" }
}

fn lock<T>(m: &Mutex<T>) -> std::sync::MutexGuard<'_, T> {
    m.lock().unwrap_or_else(|e| e.into_inner())
}

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
    /// P7 Task 16：Event Engine（組裝點以 `attach_engine` 掛上；未掛上時 `event.*` 維持 METHOD_NOT_FOUND）。
    engine: std::sync::OnceLock<Arc<EventEngine>>,
    /// P8 Task 10：flash 端點狀態（組裝點以 `attach_flash` 掛上；未掛上時 `mcu.status` 視同無端點）。
    flash: std::sync::OnceLock<Arc<FlashState>>,
    /// P8 Task 14：所有新操作的共同 admission 邊界（arch §6.2 第 7 步）。
    admission: Arc<AdmissionGate>,
    /// P8 Task 14：`session.interactive_open` 的 ticket 持有到 lease 關閉／到期（interactive_id → ticket；Python
    /// `_interactive_admission_tickets`）。每次 open／close 與交接前回收已不存在的 lease。
    lease_tickets: Mutex<HashMap<String, Ticket>>,
    /// P8 Task 14：交接判定串行化（Python 在 manager lock 內完成整個判定）；內容為上次發布過旗標的 COM（下次先清除）。
    handoff: Mutex<BTreeSet<String>>,
    /// 測試用（test-fakes）：拉長 TX fence 等待，穩定安排重探結果與失敗交接的交錯。
    #[cfg(feature = "test-fakes")]
    handoff_tx_fence_timeout_override: Mutex<Option<Duration>>,
}

/// P8 Task 10：`mcu.status` 的 flash 真值來源（Python `_flash_endpoint.is_flashing()`、`_flash_active_com`、
/// `_flash_last_detect`）。
///
/// 端點由 daemon_main 以 RAII 持有（`take_endpoint` 後 drop 即 unlink；Task 12 的停止序改呼叫
/// `FlashEndpoint::stop`）；service 只讀。`caps.flash_endpoint=false`（Windows）時端點為 `None`，
/// `flashing` 恆 false、`last_detect` 恆 null。
pub struct FlashState {
    coord: Arc<FlashCoordinator>,
    endpoint: std::sync::Mutex<Option<FlashEndpoint>>,
}

impl FlashState {
    pub fn new(coord: Arc<FlashCoordinator>, endpoint: Option<FlashEndpoint>) -> Arc<FlashState> {
        Arc::new(FlashState {
            coord,
            endpoint: std::sync::Mutex::new(endpoint),
        })
    }

    fn slot(&self) -> std::sync::MutexGuard<'_, Option<FlashEndpoint>> {
        self.endpoint.lock().unwrap_or_else(|e| e.into_inner())
    }

    /// Python `FlashEndpoint.is_flashing()`：`on_flash_open` 進行中（含 detection，MCU-STATUS-01）；無端點恆 false。
    pub fn is_flashing(&self) -> bool {
        self.slot().as_ref().is_some_and(FlashEndpoint::is_flashing)
    }

    /// 取出端點（呼叫端 drop 即 unlink）；之後 `is_flashing` 恆 false。
    pub fn take_endpoint(&self) -> Option<FlashEndpoint> {
        self.slot().take()
    }

    /// 認線協調者（`mcu.status` 的 `flashing_com`／`last_detect` 來源）。
    pub fn coordinator(&self) -> &Arc<FlashCoordinator> {
        &self.coord
    }
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

/// 對齊 Python `SerialwrapService._annotate_source`：只在成功回應末端追加來源標註。
fn annotate_source(mut result: PyDict, check: &SourceCheck, with_class: bool) -> PyDict {
    if !result.get("ok").is_some_and(py_truthy) {
        return result;
    }
    if let Some(normalized_from) = check.normalized_from.as_deref() {
        result.insert("source", PyValue::from(check.source.clone()));
        result.insert("source_normalized_from", PyValue::from(normalized_from));
    }
    if with_class {
        result.insert(
            "source_class",
            PyValue::from(classify_source(&check.source)),
        );
    }
    result
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
        // P8 Task 14：`command.submit` 的 ticket 由命令登錄表於驗證之後取得（Python `CommandArbiter.submit`），與 RPC
        // 分派、flash sync-probe 共用同一個 gate；共用同一登錄表的 Service 亦共用其 gate。
        let admission = deps.registry.admission_gate();
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
            mcu: McuPatternRegistry::default_registry(),
            running: AtomicBool::new(true),
            engine: std::sync::OnceLock::new(),
            flash: std::sync::OnceLock::new(),
            admission,
            lease_tickets: Mutex::new(HashMap::new()),
            handoff: Mutex::new(BTreeSet::new()),
            #[cfg(feature = "test-fakes")]
            handoff_tx_fence_timeout_override: Mutex::new(None),
        }
    }

    /// 只供測試（test-fakes）：覆寫交接 TX fence 等待上限。
    #[cfg(feature = "test-fakes")]
    pub fn set_handoff_tx_fence_timeout_for_test(&self, timeout: Duration) {
        *lock(&self.handoff_tx_fence_timeout_override) = Some(timeout);
    }

    /// P8 Task 14：共同 admission 邊界（flash 端點的 sync-probe 與 RPC 分派共用同一個 gate）。
    pub fn admission(&self) -> &Arc<AdmissionGate> {
        &self.admission
    }

    /// P8 Task 14：SC-11 交接停止（Python `SerialwrapService.handoff_stop`；arch §6.2 第 7 步）。
    ///
    /// 1. 回收已終結的命令／lease ticket，依當下 session 發布 `recovering`／`flashing`／`lease` 旗標；
    /// 2. `freeze_if_idle`：有 ticket 或旗標 → `FLASHING_BUSY`（flash 優先）／`SESSION_BUSY`＋`busy[]`，不凍結、不停止；
    /// 3. 新凍結成立後重查 session：manager 沒有 Python 那把同時涵蓋狀態轉移與判定的全域鎖，快照到凍結之間發生的
    ///    RECOVERING／FLASHING／lease 轉移於此擋下（撤銷本次凍結、回忙碌）；
    /// 4. 凍結背景來源與既有 bridge 的新 TX（`SessionManager::freeze_for_handoff`）；TX fence 逾時 → `SESSION_BUSY`
    ///    （`ops:["tx.in_flight"]`），只撤銷本次新建的凍結；
    /// 5. 成立 → `{"ok":true,"stopping":true,"handoff_token":<32 hex>}`（無 version；排空窗口與停止由 framing 排程）。
    pub fn handoff_stop(&self, params: &PyDict) -> PyDict {
        let mut published = lock(&self.handoff);
        let p = Params::new(params);
        let reason = match first_str(&p, &["reason"]) {
            r if r.is_empty() => "update".to_string(),
            r => r,
        };
        self.registry.reconcile_admission();
        let sessions = match self.manager.list_sessions_checked() {
            Ok(sessions) => sessions,
            Err(com) => return self.handoff_query_incomplete(&com),
        };
        self.reconcile_lease_tickets(&sessions);
        self.publish_flags(&sessions, &mut published);
        let (token, created) = match self.admission.freeze_if_idle(&reason) {
            FreezeOutcome::Busy(busy) => return handoff_busy(&busy, &sessions),
            FreezeOutcome::Frozen { token, created } => (token, created),
        };
        if created {
            let fresh = match self.manager.list_sessions_checked() {
                Ok(sessions) => sessions,
                Err(com) => {
                    self.admission.thaw(&token);
                    return self.handoff_query_incomplete(&com);
                }
            };
            let busy = self.flag_busy(&fresh);
            if !busy.by_com.is_empty() {
                self.admission.thaw(&token);
                self.publish_flags(&fresh, &mut published);
                return handoff_busy(&busy, &fresh);
            }
        }
        #[cfg(feature = "test-fakes")]
        let tx_fence_timeout = lock(&self.handoff_tx_fence_timeout_override)
            .as_ref()
            .copied()
            .unwrap_or(HANDOFF_TX_FENCE_TIMEOUT);
        #[cfg(not(feature = "test-fakes"))]
        let tx_fence_timeout = HANDOFF_TX_FENCE_TIMEOUT;
        let unconverged = self.manager.freeze_for_handoff(tx_fence_timeout);
        if !unconverged.is_empty() {
            // fail-closed：仍有已通過提交檢查的寫入段在進行中，交接不得成立。只撤銷本次新建的 admission 凍結；
            // shutdown 或先前交接建立的凍結（created=false）不在此撤銷。
            if created {
                self.admission.thaw(&token);
            }
            return tx_in_flight_busy(&unconverged, &sessions);
        }
        pydict! { "ok" => true, "stopping" => true, "handoff_token" => token }
    }

    fn handoff_query_incomplete(&self, com: &str) -> PyDict {
        let interactive_id = lock(&self.lease_tickets)
            .iter()
            .find(|(_, ticket)| ticket.com() == Some(com))
            .map(|(interactive_id, _)| interactive_id.clone());
        handoff_query_incomplete(com, interactive_id)
    }

    /// P8 Task 14：daemon 一般關機的 admission 凍結（Python `service.stop()` 的 `admission.freeze()`）。
    pub fn shutdown_freeze(&self) {
        self.admission.freeze();
    }

    /// session 公開欄位 → 交接旗標（Python `handoff_stop` 的 `set_flag`：RECOVERING、FLASHING 或 flash-critical、lease）。
    fn session_flags(&self, session: &PyDict) -> Vec<AdmissionFlag> {
        let com = session.get("com").and_then(PyValue::as_str).unwrap_or_default();
        let state = session.get("state").and_then(PyValue::as_str).unwrap_or_default();
        let mut flags = Vec::new();
        if state == "FLASHING" || (!com.is_empty() && self.manager.flash_critical(com)) {
            flags.push(AdmissionFlag::Flashing);
        }
        if session.get("interactive_session_id").is_some_and(|v| !v.is_null()) {
            flags.push(AdmissionFlag::Lease);
        }
        if state == "RECOVERING" {
            flags.push(AdmissionFlag::Recovering);
        }
        flags
    }

    /// 依快照重新發布旗標：先清除上次發布、本次已不存在的 COM。
    fn publish_flags(&self, sessions: &[PyDict], published: &mut BTreeSet<String>) {
        const ALL: [AdmissionFlag; 3] = [AdmissionFlag::Flashing, AdmissionFlag::Lease, AdmissionFlag::Recovering];
        let mut now: BTreeSet<String> = BTreeSet::new();
        for session in sessions {
            let Some(com) = session.get("com").and_then(PyValue::as_str) else { continue };
            let flags = self.session_flags(session);
            for flag in ALL {
                self.admission.set_flag(com, flag, flags.contains(&flag));
            }
            now.insert(com.to_string());
        }
        for stale in published.difference(&now) {
            for flag in ALL {
                self.admission.set_flag(stale, flag, false);
            }
        }
        *published = now;
    }

    /// 凍結後重查：只看旗標（凍結後 admit 一律被拒，不會再有新 ticket）。
    fn flag_busy(&self, sessions: &[PyDict]) -> Busy {
        let mut busy = Busy::default();
        for session in sessions {
            let Some(com) = session.get("com").and_then(PyValue::as_str) else { continue };
            let flags = self.session_flags(session);
            if flags.is_empty() {
                continue;
            }
            busy.flashing |= flags.contains(&AdmissionFlag::Flashing);
            busy.by_com.insert(com.to_string(), BusyCom { ops: Vec::new(), flags });
        }
        busy
    }

    /// ticket 的 COM 歸屬（Python `_admission_com`）：selector 鏈 → interactive_id → device_by_id／by_id；查無為 `None`。
    fn admission_com(&self, params: &PyDict) -> Option<String> {
        let p = Params::new(params);
        let selector = first_str(&p, &["selector", "session_id", "com", "alias"]);
        if !selector.is_empty() {
            if let Some(com) = self.manager.com_of(&selector) {
                return Some(com);
            }
        }
        let interactive_id = first_str(&p, &["interactive_id"]);
        if !interactive_id.is_empty() {
            if let Some(com) = self.manager.interactive_com(&interactive_id) {
                return Some(com);
            }
        }
        let by_id = first_str(&p, &["device_by_id", "by_id"]);
        if !by_id.is_empty() {
            return self.manager.com_for_by_id(&by_id);
        }
        None
    }

    /// 回收已不存在之 lease 的 ticket（Python `_reconcile_interactive_admission_tickets`）。
    fn reconcile_lease_tickets(&self, sessions: &[PyDict]) {
        let active: HashSet<&str> = sessions
            .iter()
            .filter_map(|s| s.get("interactive_session_id").and_then(PyValue::as_str))
            .collect();
        let done: Vec<Ticket> = {
            let mut map = lock(&self.lease_tickets);
            let gone: Vec<String> = map.keys().filter(|iid| !active.contains(iid.as_str())).cloned().collect();
            gone.into_iter().filter_map(|iid| map.remove(&iid)).collect()
        };
        for ticket in done {
            self.admission.done(ticket);
        }
    }

    /// 分派成功後把需持有到終態的 ticket 移交（interactive_open → lease）；其餘於守衛 drop 歸還。
    fn retain_ticket(&self, method: &str, params: &PyDict, result: &PyDict, guard: TicketGuard) {
        if !is_ok(result) {
            return;
        }
        match method {
            "session.interactive_open" => {
                let iid = result
                    .get("interactive_id")
                    .or_else(|| result.get("interactive_session_id"))
                    .and_then(PyValue::as_str)
                    .filter(|i| !i.is_empty());
                if let Some(iid) = iid {
                    if let Some(ticket) = guard.take() {
                        if let Some(old) = lock(&self.lease_tickets).insert(iid.to_string(), ticket) {
                            self.admission.done(old);
                        }
                    }
                }
            }
            "session.interactive_close" => {
                let iid = Params::new(params).str_or("interactive_id", "").unwrap_or_default();
                if let Some(old) = lock(&self.lease_tickets).remove(&iid) {
                    self.admission.done(old);
                }
            }
            _ => {}
        }
    }

    /// 掛上 flash 端點狀態（只可一次；回 `false` 表示已掛上）。
    pub fn attach_flash(&self, state: Arc<FlashState>) -> bool {
        self.flash.set(state).is_ok()
    }

    /// `service.py:1034-1038`：預設 registry 的 rows（T2 `patterns_rpc`：`family`／`probe`／`expect`／`baud`）。
    fn mcu_patterns(&self) -> Result<PyDict, PyErr> {
        Ok(pydict! { "ok" => true, "patterns" => self.mcu.patterns_rpc() })
    }

    /// `service.py:1039-1047`：`last_detect`／`flashing_com` 取 coordinator（Python `_flash_lock` 內讀
    /// `_flash_last_detect`／`_flash_active_com`），`candidates` 為唯讀快照（`flash_candidates`，不標 flash-critical），
    /// `flashing`＝`FlashEndpoint::is_flashing()`（probe／偵測期間即為 true，MCU-STATUS-01）。未掛 flash 狀態或無端點
    /// （`caps.flash_endpoint=false`）時 `flashing:false`、`flashing_com:null`、`last_detect:null`。
    fn mcu_status(&self) -> Result<PyDict, PyErr> {
        let flash = self.flash.get();
        let last_detect = flash
            .and_then(|f| f.coordinator().last_detect())
            .map_or(PyValue::Null, |d| PyValue::Dict(d.to_py()));
        let active_com = flash
            .and_then(|f| f.coordinator().active_com())
            .map_or(PyValue::Null, PyValue::from);
        let candidates = dict_list(self.manager.flash_candidates());
        let flashing = flash.is_some_and(|f| f.is_flashing());
        Ok(pydict! {
            "ok" => true,
            "candidates" => candidates,
            "flashing" => flashing,
            "flashing_com" => active_com,
            "last_detect" => last_detect,
        })
    }

    /// 掛上 Event Engine（只可一次；回 `false` 表示已掛上）。
    pub fn attach_engine(&self, engine: Arc<EventEngine>) -> bool {
        self.engine.set(engine).is_ok()
    }

    /// 10 個 `event.*`（`service.py:1393-1432`，順序同 `_dispatch`）。RPC 連線 thread 上直接呼叫 engine（不經 session actor）。
    fn event_rpc(
        &self,
        engine: &EventEngine,
        method: &str,
        params: &PyDict,
    ) -> Result<PyDict, PyErr> {
        let p = Params::new(params);
        let raw = |k: &str| p.raw(k).filter(|v| !v.is_null());
        let spread = |extra: PyDict| {
            let mut d = pydict! { "ok" => true };
            d.update(&extra);
            d
        };
        match method {
            "event.rule_set" => match engine.rule_set(&PyValue::Dict(params.clone())) {
                // `{"ok": True, **dict(rule.raw)}`：raw 的 `ok` 覆寫值、位置仍第一（RPC-H-14 KEEP）。
                Ok(rule) => Ok(spread(rule.raw.clone())),
                Err(UpsertError::Rule(RuleError::Schema(m) | RuleError::Value(m))) => Ok(pydict! {
                    "ok" => false, "error_code" => "INVALID_RULE_SCHEMA", "error" => m
                }),
                Err(UpsertError::Rule(RuleError::Type(m))) => Err(PyErr::value_error(m)),
                Err(UpsertError::Io(e)) => Err(pal_err(e)),
            },
            "event.rule_delete" => {
                let deleted = engine.rule_delete(&first_str(&p, &["rule_id"]))?;
                Ok(pydict! { "ok" => true, "deleted" => deleted })
            }
            "event.rule_list" => {
                let rules = engine.rule_list(raw("selector"), raw("owner"))?;
                Ok(pydict! { "ok" => true, "rules" => dict_list(rules) })
            }
            "event.rule_get" => match engine.rule_get(&first_str(&p, &["rule_id"]))? {
                Some(d) => Ok(spread(d)),
                None => Ok(error("RULE_NOT_FOUND")),
            },
            "event.com_enable" => Ok(spread(engine.com_enable(&first_str(&p, &["selector"]))?)),
            "event.com_disable" => Ok(spread(engine.com_disable(&first_str(&p, &["selector"]))?)),
            "event.com_status" => Ok(spread(engine.com_status(raw("selector"))?)),
            "event.reset" => {
                let cleared = engine.reset(raw("rule_id"), raw("selector"))?;
                Ok(pydict! { "ok" => true, "cleared" => int(cleared) })
            }
            "event.reload" => Ok(spread(engine.reload()?)),
            _ => {
                let entries =
                    engine.tail(raw("rule_id"), raw("selector"), raw("since_ts"), raw("n"))?;
                Ok(pydict! { "ok" => true, "entries" => PyValue::List(entries) })
            }
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
    ///
    /// P8 Task 14：`daemon.handoff_stop` 直接回 [`Service::handoff_stop`]（Python `service.rpc`，不補 version）；
    /// [`ADMISSION_METHODS`] 在任何副作用前取得 admission ticket，已凍結回 `FLUSHED_BY_SHUTDOWN`＋`reason:"handoff_frozen"`。
    pub fn rpc(&self, method: &str, params: &PyDict) -> Result<PyDict, PyErr> {
        if method == "daemon.handoff_stop" {
            return Ok(self.handoff_stop(params));
        }
        let mut guard = if ADMISSION_METHODS.contains(&method) {
            if matches!(method, "session.interactive_open" | "session.interactive_close") {
                if let Ok(sessions) = self.manager.list_sessions_checked() {
                    self.reconcile_lease_tickets(&sessions);
                }
            }
            match self.admission.admit(method, self.admission_com(params).as_deref()) {
                Some(ticket) => Some(TicketGuard { gate: Arc::clone(&self.admission), ticket: Some(ticket) }),
                None => {
                    let mut frozen = handoff_frozen_response();
                    frozen.setdefault("version", PyValue::from(self.version.clone()));
                    return Ok(frozen);
                }
            }
        } else {
            None
        };
        let mut result = match method {
            "file.push" => self.file_transfer_rpc(
                FileTransferKind::Push,
                params,
                guard.as_mut().and_then(TicketGuard::into_async_ticket),
            )?,
            "file.pull" => self.file_transfer_rpc(
                FileTransferKind::Pull,
                params,
                guard.as_mut().and_then(TicketGuard::into_async_ticket),
            )?,
            _ => self.dispatch(method, params)?,
        };
        if let Some(guard) = guard {
            self.retain_ticket(method, params, &result, guard);
        }
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

    /// Python `get_background_result`（以 cmd_id 全域查 capture：先問命令紀錄的 session，再問其餘）＋
    /// 全部查無時一次 `_bg_fallback_from_arbiter`（審查 F76：fallback 不得先於其他 session 的 capture）。
    fn result_tail(&self, cmd_id: &str, from_chunk: i128, limit: i128) -> Result<PyDict, PyErr> {
        let req = || SessionRequest::BackgroundResult {
            cmd_id: cmd_id.to_string(),
            from_chunk,
            limit,
        };
        let owner = self
            .cmd_owner(cmd_id)
            .and_then(|sid| self.manager.resolve(&sid).filter(|resolved| *resolved == sid));
        let others = self
            .manager
            .list_sessions()
            .into_iter()
            .filter_map(|s| s.get("session_id").and_then(PyValue::as_str).map(str::to_string))
            .filter(|sid| Some(sid) != owner.as_ref());
        for sid in owner.clone().into_iter().chain(others) {
            // 個別 session 查詢失敗（actor 已結束／逾時）不使整個 RPC 失敗：Python 只查 manager 的全域 dict（審查 F98）。
            let Ok(r) = self.manager.route(&sid, req(), ROUTE_TIMEOUT) else {
                continue;
            };
            if is_ok(&r) {
                return Ok(r);
            }
        }
        Ok(sw_core::session_core::bg_fallback_from_registry(
            &self.registry,
            cmd_id,
            from_chunk,
        ))
    }

    /// P8 Task 6：`file.push`／`file.pull`（`service.py` 1426～1500）：轉型失敗與 `SOURCE_RESERVED` 直接回；
    /// 通過者交 `SessionManager::file_transfer`，成功回應依 `_annotate_source` 標註正規化來源。
    fn file_transfer_rpc(
        &self,
        kind: FileTransferKind,
        params: &PyDict,
        admission_ticket: Option<Arc<AdmissionTicket>>,
    ) -> Result<PyDict, PyErr> {
        let (req, source_check) = match FileTransferRequest::from_rpc(kind, params)? {
            Ok(parsed) => parsed,
            Err(response) => return Ok(response),
        };
        self.manager
            .file_transfer_with_admission(req, admission_ticket)
            .map(|result| annotate_source(result, &source_check, false))
    }

    fn dispatch(&self, method: &str, params: &PyDict) -> Result<PyDict, PyErr> {
        let p = Params::new(params);
        match method {
            "health.ping" => Ok(pydict! { "ok" => true, "pong" => true }),
            "health.status" => Ok(crate::health::health(self)),

            "mcu.patterns" => self.mcu_patterns(),
            "mcu.status" => self.mcu_status(),

            "device.list" => Ok(pydict! { "ok" => true, "devices" => dict_list(self.manager.list_devices()) }),
            "device.release" => {
                let selector = p.str_or("selector", "")?;
                if selector.is_empty() {
                    return Ok(invalid_args());
                }
                let raw_source = p.str_or("source", "cli")?;
                let source_check = check_source(Some(&raw_source), "cli", "source", true);
                if let Some(error) = source_check.error.as_ref() {
                    return Ok(error.clone());
                }
                let source = source_check.source.clone();
                // reason 原樣（任意 JSON）進 released_reason。
                let reason = p.raw("reason").cloned().unwrap_or(PyValue::Null);
                self.route(
                    &selector,
                    SessionRequest::Release { source, reason },
                    ROUTE_TIMEOUT,
                )
                .map(|result| annotate_source(result, &source_check, false))
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
            // P6 Task 10：console 方法（service.py:1071-1089）；查無 selector 回 SESSION_NOT_READY{selector}。
            "session.console_attach" => {
                let selector = selector_chain(&p);
                // `str(label) if label else None`
                let label = p
                    .truthy("label")
                    .then(|| p.raw("label").map(sw_proto::pyfmt::py_str))
                    .flatten();
                if selector.is_empty() {
                    return Ok(invalid_args());
                }
                self.manager.console_attach(&selector, label, ROUTE_TIMEOUT)
            }
            "session.console_detach" => {
                let selector = selector_chain(&p);
                let client_id = p.str_or("client_id", "")?;
                if selector.is_empty() || client_id.is_empty() {
                    return Ok(invalid_args());
                }
                self.manager
                    .console_request(&selector, SessionRequest::ConsoleDetach { client_id }, ROUTE_TIMEOUT)
            }
            "session.console_list" => {
                let selector = selector_chain(&p);
                if selector.is_empty() {
                    return Ok(invalid_args());
                }
                self.manager.console_request(&selector, SessionRequest::ConsoleList, ROUTE_TIMEOUT)
            }
            "session.interactive_open" => {
                let selector = selector_chain(&p);
                let owner = p.str_or("owner", "agent")?;
                let owner_check = check_source(Some(&owner), "agent", "owner", false);
                if let Some(error) = owner_check.error.as_ref() {
                    return Ok(error.clone());
                }
                let owner = owner_check.source;
                let timeout_s = p.float_or("timeout_s", 60.0)?;
                let command = p.str_or("command", "")?;
                let allow_attached = p.truthy("allow_attached");
                if selector.is_empty() {
                    return Ok(invalid_args());
                }
                if self.manager.resolve(&selector).is_none() {
                    // `interactive_open`：`get_session(selector)` 為 None → SESSION_NOT_READY（session_manager.py:6820）。
                    return Ok(pydict! { "ok" => false, "error_code" => "SESSION_NOT_READY", "selector" => selector });
                }
                let raw = selector.clone();
                let mut result = self.route(
                    &selector,
                    SessionRequest::InteractiveOpen {
                        owner,
                        timeout_s,
                        command,
                        allow_attached,
                    },
                    ROUTE_TIMEOUT,
                )?;
                // Python 各 SESSION_NOT_READY 出口皆回原樣 selector；SESSION_BUSY／RECOVERING 維持 COM。
                if result.get("error_code").and_then(PyValue::as_str) == Some("SESSION_NOT_READY") && result.get("selector").is_some() {
                    result.insert("selector", PyValue::from(raw));
                }
                Ok(result)
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

            // P8 Task 6：file.* 的參數轉型逐行照 `service.py`（`FileTransferRequest::from_rpc`），session 閘門在 actor 內。
            "file.push" => self.file_transfer_rpc(FileTransferKind::Push, params, None),
            "file.pull" => self.file_transfer_rpc(FileTransferKind::Pull, params, None),

            "command.submit" => {
                // completion_regex 驗證在 selector 解析之前（Python 順序）。
                let completion = p.raw("completion_regex");
                if !valid_completion_regex(completion) {
                    return Ok(error("INVALID_COMPLETION_REGEX"));
                }
                let completion_regex = completion.and_then(PyValue::as_str).map(str::to_string);
                // 外部來源先於 selector、timeout 與命令 admission 驗證；保留政策結果供後續回應標註。
                let raw_source = p.str_or("source", "")?;
                let source_check = check_source(Some(&raw_source), "agent", "source", true);
                if let Some(source_error) = source_check.error.clone() {
                    return Ok(source_error);
                }
                let source = source_check.source.clone();
                let mode = p.str_or("mode", "line")?;
                let raw_expect_reboot = p.raw("expect_reboot");
                if let Some(rejection) = expect_reboot_rejection(raw_expect_reboot, &mode) {
                    return Ok(rejection);
                }
                // Python `expect_reboot=expect_reboot is True`：只有原始值為 bool true 才啟用（null／缺省／false 皆 false）。
                let expect_reboot = matches!(raw_expect_reboot, Some(PyValue::Bool(true)));
                // Python `trusted_human = source_check.is_bare_human`：只認外部原始輸入的空名 human。
                let trusted_human = source_check.is_bare_human();
                let selector = first_str(&p, &["selector", "com", "alias"]);
                let command = first_str(&p, &["cmd", "command"]);
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
                    expect_reboot,
                    trusted_human,
                    // D-10（DF-02）：RPC 來源一律以 agent 對待，即使 source 以 `human:` 開頭；
                    // 空名 human 的可信判定只經 `trusted_human` 傳遞（D5：與 origin 取 OR）。
                    origin: SubmitOrigin::Rpc,
                };
                self.manager
                    .route(&sid, SessionRequest::Submit(req), ROUTE_TIMEOUT)
                    .map(|result| annotate_source(strip_frozen_session(result), &source_check, true))
            }
            "command.get" => {
                let cmd_id = p.str_or("cmd_id", "")?;
                Ok(match self.registry.get(&cmd_id) {
                    Some(mut rec) => {
                        let source = rec.get("source").and_then(PyValue::as_str).unwrap_or("");
                        rec.insert(
                            "source_class",
                            PyValue::from(classify_source(source)),
                        );
                        pydict! { "ok" => true, "command" => rec }
                    }
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
            // test-fakes 建置專用（P5-T19 `handler_panic_does_not_kill_daemon`）：handler 內 panic，驗證 framing 的
            // `catch_unwind` 轉 `EXCEPTION` 且 daemon 存活。產品 build 不編譯此分支（落入 METHOD_NOT_FOUND）。
            "event.rule_set" | "event.rule_delete" | "event.rule_list" | "event.rule_get"
            | "event.com_enable" | "event.com_disable" | "event.com_status" | "event.reset"
            | "event.reload" | "event.tail"
                if self.engine.get().is_some() =>
            {
                let engine = Arc::clone(self.engine.get().expect("guarded"));
                self.event_rpc(&engine, method, params)
            }
            #[cfg(feature = "test-fakes")]
            "test.panic" => panic!("slice21 T19 test.panic handler"),
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

/// 交接忙碌列（Python `handoff_stop` 的 `rows`：鍵序 com／state／foreground_busy／outstanding_commands／
/// interactive_session_id／recovering／ops）。查無 session 的 COM（`"*"`：ticket 未帶 COM）以預設值呈現。
fn busy_row(com: &str, public: Option<&PyDict>, recovering_flag: bool, ops: Vec<String>) -> PyValue {
    let field = |k: &str| public.and_then(|p| p.get(k));
    let state = field("state").and_then(PyValue::as_str).unwrap_or("UNKNOWN").to_string();
    let foreground_busy = field("foreground_busy").is_some_and(py_truthy);
    let outstanding = field("outstanding_commands").and_then(PyValue::as_i64).unwrap_or(0);
    let interactive = field("interactive_session_id").cloned().unwrap_or(PyValue::Null);
    let recovering = field("recovering").is_some_and(py_truthy) || recovering_flag;
    PyValue::Dict(pydict! {
        "com" => com,
        "state" => state,
        "foreground_busy" => foreground_busy,
        "outstanding_commands" => int(i128::from(outstanding)),
        "interactive_session_id" => interactive,
        "recovering" => recovering,
        "ops" => PyValue::List(ops.into_iter().map(PyValue::Str).collect()),
    })
}

fn public_by_com(sessions: &[PyDict]) -> BTreeMap<&str, &PyDict> {
    sessions
        .iter()
        .filter_map(|s| s.get("com").and_then(PyValue::as_str).map(|com| (com, s)))
        .collect()
}

/// `freeze_if_idle` 忙碌 → `{"ok":false,"error_code":FLASHING_BUSY|SESSION_BUSY,"busy":[…]}`（flash 優先）。
fn handoff_busy(busy: &Busy, sessions: &[PyDict]) -> PyDict {
    let by_com = public_by_com(sessions);
    let rows: Vec<PyValue> = busy
        .by_com
        .iter()
        .map(|(com, row)| {
            let mut ops = row.ops.clone();
            if row.flags.contains(&AdmissionFlag::Lease) && !ops.iter().any(|o| o == "session.interactive_open") {
                ops.push("session.interactive_open".to_string());
            }
            ops.sort();
            busy_row(com, by_com.get(com.as_str()).copied(), row.flags.contains(&AdmissionFlag::Recovering), ops)
        })
        .collect();
    let code = if busy.flashing { "FLASHING_BUSY" } else { "SESSION_BUSY" };
    pydict! { "ok" => false, "error_code" => code, "busy" => PyValue::List(rows) }
}

/// TX fence 未收斂 → `SESSION_BUSY`，每筆 `ops:["tx.in_flight"]`（Python `tx_rows`）。
fn tx_in_flight_busy(unconverged: &[String], sessions: &[PyDict]) -> PyDict {
    let by_com = public_by_com(sessions);
    let rows: Vec<PyValue> = unconverged
        .iter()
        .map(|com| busy_row(com, by_com.get(com.as_str()).copied(), false, vec!["tx.in_flight".to_string()]))
        .collect();
    pydict! { "ok" => false, "error_code" => "SESSION_BUSY", "busy" => PyValue::List(rows) }
}

/// session 公開查詢不完整時 fail closed；未知狀態不能作為 lease ticket 回收依據。
fn handoff_query_incomplete(com: &str, interactive_id: Option<String>) -> PyDict {
    let mut row = match busy_row(com, None, false, vec!["session.get_state".to_string()]) {
        PyValue::Dict(row) => row,
        _ => unreachable!("busy_row 恆為 dict"),
    };
    row.insert(
        "interactive_session_id",
        interactive_id.map(PyValue::from).unwrap_or(PyValue::Null),
    );
    pydict! {
        "ok" => false,
        "error_code" => "SESSION_BUSY",
        "busy" => PyValue::List(vec![PyValue::Dict(row)]),
    }
}

/// `RpcHandler` 實作：分派到 `Service::rpc`，`daemon.stop` 經 `stop` channel 通知 daemon main。
pub struct ServiceHandler {
    svc: Arc<Service>,
    stop: crossbeam_channel::Sender<()>,
    /// P8 Task 14：交接排空窗口的停止計時已排程（只排一次）。
    drain_armed: AtomicBool,
}

impl ServiceHandler {
    pub fn new(svc: Arc<Service>, stop: crossbeam_channel::Sender<()>) -> ServiceHandler {
        ServiceHandler {
            svc,
            stop,
            drain_armed: AtomicBool::new(false),
        }
    }
}

impl RpcHandler for ServiceHandler {
    fn call(&self, method: &str, params: &PyDict) -> Result<PyDict, PyErr> {
        self.svc.rpc(method, params)
    }
    fn request_stop(&self) {
        let _ = self.stop.try_send(());
    }
    fn handoff_stop(&self, params: &PyDict) -> Result<PyDict, PyErr> {
        Ok(self.svc.handoff_stop(params))
    }
    /// 交接成立後的有界排空窗口（Python `call_later(HANDOFF_DRAIN_GRACE_S, stop_event.set)`）：窗口內監聽保留，
    /// 有副作用請求由凍結的 admission 回 `handoff_frozen`；窗口結束才送停止。只排程一次；計時 thread 無法建立時立即停止。
    fn request_stop_after(&self, delay: Duration) {
        if self.drain_armed.swap(true, Ordering::SeqCst) {
            return;
        }
        let stop = self.stop.clone();
        let spawned = std::thread::Builder::new()
            .name("serialwrap-handoff-drain".into())
            .spawn(move || {
                std::thread::sleep(delay);
                let _ = stop.try_send(());
            });
        if spawned.is_err() {
            let _ = self.stop.try_send(());
        }
    }
}
