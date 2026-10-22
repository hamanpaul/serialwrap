//! RPC 方法表與型別（P3-T7）。
//!
//! 方法名稱以 `contracts/rpc_schema.json` 為權威來源（`x-methods` 54 項＋ `x-pending` 的 `daemon.handoff_stop`，SC-11）。
//! 重試白名單與長逾時集合分別取自 [`crate::constants`]，測試 `tests/rpc_types.rs` 以集合比對。

use crate::constants::{LONG_RPC_METHODS, RETRYABLE_READONLY_METHODS};

/// RPC 方法；未知名稱落入 [`Method::Unknown`]（保留原字串，不拋錯）。
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Method {
    HealthPing,
    HealthStatus,
    DaemonStop,
    DaemonHandoffStop,
    DeviceList,
    DeviceRelease,
    DeviceAttach,
    SessionList,
    SessionGetState,
    SessionClear,
    SessionBind,
    SessionPin,
    SessionUnpin,
    SessionAttach,
    SessionSelfTest,
    SessionActivity,
    SessionRecover,
    SessionConsoleAttach,
    SessionConsoleDetach,
    SessionConsoleList,
    SessionInteractiveOpen,
    SessionInteractiveSend,
    SessionInteractiveStatus,
    SessionInteractiveClose,
    SessionLogStart,
    SessionLogStop,
    SessionLogStatus,
    AliasList,
    AliasSet,
    AliasAssign,
    AliasUnassign,
    CommandSubmit,
    CommandGet,
    CommandResultTail,
    CommandCancel,
    ResultTail,
    LogTailRaw,
    LogTailText,
    FilePush,
    FilePull,
    WalRange,
    WalReset,
    WalCurrentSeq,
    McuPatterns,
    McuStatus,
    EventRuleSet,
    EventRuleDelete,
    EventRuleList,
    EventRuleGet,
    EventComEnable,
    EventComDisable,
    EventComStatus,
    EventReset,
    EventReload,
    EventTail,
    Unknown(String),
}

/// 方法名稱表（55 項），與 [`Method`] 的已知變體一一對應。
static KNOWN: [&str; 55] = [
    "health.ping",
    "health.status",
    "daemon.stop",
    "daemon.handoff_stop",
    "device.list",
    "device.release",
    "device.attach",
    "session.list",
    "session.get_state",
    "session.clear",
    "session.bind",
    "session.pin",
    "session.unpin",
    "session.attach",
    "session.self_test",
    "session.activity",
    "session.recover",
    "session.console_attach",
    "session.console_detach",
    "session.console_list",
    "session.interactive_open",
    "session.interactive_send",
    "session.interactive_status",
    "session.interactive_close",
    "session.log_start",
    "session.log_stop",
    "session.log_status",
    "alias.list",
    "alias.set",
    "alias.assign",
    "alias.unassign",
    "command.submit",
    "command.get",
    "command.result_tail",
    "command.cancel",
    "result.tail",
    "log.tail_raw",
    "log.tail_text",
    "file.push",
    "file.pull",
    "wal.range",
    "wal.reset",
    "wal.current_seq",
    "mcu.patterns",
    "mcu.status",
    "event.rule_set",
    "event.rule_delete",
    "event.rule_list",
    "event.rule_get",
    "event.com_enable",
    "event.com_disable",
    "event.com_status",
    "event.reset",
    "event.reload",
    "event.tail",
];

impl Method {
    /// 線路上的方法字串。
    pub fn as_str(&self) -> &str {
        match self {
            Method::HealthPing => "health.ping",
            Method::HealthStatus => "health.status",
            Method::DaemonStop => "daemon.stop",
            Method::DaemonHandoffStop => "daemon.handoff_stop",
            Method::DeviceList => "device.list",
            Method::DeviceRelease => "device.release",
            Method::DeviceAttach => "device.attach",
            Method::SessionList => "session.list",
            Method::SessionGetState => "session.get_state",
            Method::SessionClear => "session.clear",
            Method::SessionBind => "session.bind",
            Method::SessionPin => "session.pin",
            Method::SessionUnpin => "session.unpin",
            Method::SessionAttach => "session.attach",
            Method::SessionSelfTest => "session.self_test",
            Method::SessionActivity => "session.activity",
            Method::SessionRecover => "session.recover",
            Method::SessionConsoleAttach => "session.console_attach",
            Method::SessionConsoleDetach => "session.console_detach",
            Method::SessionConsoleList => "session.console_list",
            Method::SessionInteractiveOpen => "session.interactive_open",
            Method::SessionInteractiveSend => "session.interactive_send",
            Method::SessionInteractiveStatus => "session.interactive_status",
            Method::SessionInteractiveClose => "session.interactive_close",
            Method::SessionLogStart => "session.log_start",
            Method::SessionLogStop => "session.log_stop",
            Method::SessionLogStatus => "session.log_status",
            Method::AliasList => "alias.list",
            Method::AliasSet => "alias.set",
            Method::AliasAssign => "alias.assign",
            Method::AliasUnassign => "alias.unassign",
            Method::CommandSubmit => "command.submit",
            Method::CommandGet => "command.get",
            Method::CommandResultTail => "command.result_tail",
            Method::CommandCancel => "command.cancel",
            Method::ResultTail => "result.tail",
            Method::LogTailRaw => "log.tail_raw",
            Method::LogTailText => "log.tail_text",
            Method::FilePush => "file.push",
            Method::FilePull => "file.pull",
            Method::WalRange => "wal.range",
            Method::WalReset => "wal.reset",
            Method::WalCurrentSeq => "wal.current_seq",
            Method::McuPatterns => "mcu.patterns",
            Method::McuStatus => "mcu.status",
            Method::EventRuleSet => "event.rule_set",
            Method::EventRuleDelete => "event.rule_delete",
            Method::EventRuleList => "event.rule_list",
            Method::EventRuleGet => "event.rule_get",
            Method::EventComEnable => "event.com_enable",
            Method::EventComDisable => "event.com_disable",
            Method::EventComStatus => "event.com_status",
            Method::EventReset => "event.reset",
            Method::EventReload => "event.reload",
            Method::EventTail => "event.tail",
            Method::Unknown(s) => s.as_str(),
        }
    }

    /// 由線路字串解析；已知名稱精確比對，其餘為 `Unknown`。
    pub fn parse(s: &str) -> Method {
        match s {
            "health.ping" => Method::HealthPing,
            "health.status" => Method::HealthStatus,
            "daemon.stop" => Method::DaemonStop,
            "daemon.handoff_stop" => Method::DaemonHandoffStop,
            "device.list" => Method::DeviceList,
            "device.release" => Method::DeviceRelease,
            "device.attach" => Method::DeviceAttach,
            "session.list" => Method::SessionList,
            "session.get_state" => Method::SessionGetState,
            "session.clear" => Method::SessionClear,
            "session.bind" => Method::SessionBind,
            "session.pin" => Method::SessionPin,
            "session.unpin" => Method::SessionUnpin,
            "session.attach" => Method::SessionAttach,
            "session.self_test" => Method::SessionSelfTest,
            "session.activity" => Method::SessionActivity,
            "session.recover" => Method::SessionRecover,
            "session.console_attach" => Method::SessionConsoleAttach,
            "session.console_detach" => Method::SessionConsoleDetach,
            "session.console_list" => Method::SessionConsoleList,
            "session.interactive_open" => Method::SessionInteractiveOpen,
            "session.interactive_send" => Method::SessionInteractiveSend,
            "session.interactive_status" => Method::SessionInteractiveStatus,
            "session.interactive_close" => Method::SessionInteractiveClose,
            "session.log_start" => Method::SessionLogStart,
            "session.log_stop" => Method::SessionLogStop,
            "session.log_status" => Method::SessionLogStatus,
            "alias.list" => Method::AliasList,
            "alias.set" => Method::AliasSet,
            "alias.assign" => Method::AliasAssign,
            "alias.unassign" => Method::AliasUnassign,
            "command.submit" => Method::CommandSubmit,
            "command.get" => Method::CommandGet,
            "command.result_tail" => Method::CommandResultTail,
            "command.cancel" => Method::CommandCancel,
            "result.tail" => Method::ResultTail,
            "log.tail_raw" => Method::LogTailRaw,
            "log.tail_text" => Method::LogTailText,
            "file.push" => Method::FilePush,
            "file.pull" => Method::FilePull,
            "wal.range" => Method::WalRange,
            "wal.reset" => Method::WalReset,
            "wal.current_seq" => Method::WalCurrentSeq,
            "mcu.patterns" => Method::McuPatterns,
            "mcu.status" => Method::McuStatus,
            "event.rule_set" => Method::EventRuleSet,
            "event.rule_delete" => Method::EventRuleDelete,
            "event.rule_list" => Method::EventRuleList,
            "event.rule_get" => Method::EventRuleGet,
            "event.com_enable" => Method::EventComEnable,
            "event.com_disable" => Method::EventComDisable,
            "event.com_status" => Method::EventComStatus,
            "event.reset" => Method::EventReset,
            "event.reload" => Method::EventReload,
            "event.tail" => Method::EventTail,
            _ => Method::Unknown(s.to_string()),
        }
    }

    /// 已知方法名稱（55 項，無重複）。
    pub fn known() -> &'static [&'static str] {
        &KNOWN
    }

    /// 是否在 client 端可安全重送的唯讀白名單內（`RETRYABLE_READONLY_METHODS`）。
    pub fn is_retryable_readonly(&self) -> bool {
        RETRYABLE_READONLY_METHODS.contains(&self.as_str())
    }

    /// 是否使用長 RPC 逾時（`LONG_RPC_METHODS`）。
    pub fn uses_long_timeout(&self) -> bool {
        LONG_RPC_METHODS.contains(&self.as_str())
    }
}

/// RPC 參數讀取器（Task 0c；對應 Python `params.get(...)` 的轉型規則，02 §5.2）。
#[allow(dead_code)] // Task 0c 實作前 `raw` 尚未被讀取。
pub struct Params<'a> {
    raw: &'a crate::pyjson::PyDict,
}

impl<'a> Params<'a> {
    pub fn new(raw: &'a crate::pyjson::PyDict) -> Params<'a> {
        Params { raw }
    }
    /// `str(params.get(k) or default)` 等價。
    pub fn str_or(&self, k: &str, default: &str) -> Result<String, crate::pycompat::PyErr> {
        Ok(self
            .raw(k)
            .filter(|value| value.is_truthy())
            .map(crate::pyfmt::py_str)
            .unwrap_or_else(|| default.to_string()))
    }
    /// `int(params.get(k) or default)` 等價（Python `int()` 規則）。
    pub fn int_or(&self, k: &str, default: i128) -> Result<i128, crate::pycompat::PyErr> {
        use crate::pyjson::{PyInt, PyValue};

        let Some(value) = self.raw(k).filter(|value| value.is_truthy()) else {
            return Ok(default);
        };
        match value {
            PyValue::Bool(value) => Ok(if *value { 1 } else { 0 }),
            PyValue::Int(PyInt::Small(value)) => Ok(*value),
            PyValue::Int(PyInt::Big(value)) => value
                .parse::<i128>()
                .map_err(|_| crate::pycompat::PyErr::value_error("int too large to convert")),
            PyValue::Float(value) if value.is_nan() => Err(crate::pycompat::PyErr::value_error(
                "cannot convert float NaN to integer",
            )),
            PyValue::Float(value) if value.is_infinite() => Err(
                crate::pycompat::PyErr::value_error("cannot convert float infinity to integer"),
            ),
            PyValue::Float(value) => {
                let truncated = value.trunc();
                if truncated < i128::MIN as f64 || truncated >= -(i128::MIN as f64) {
                    Err(crate::pycompat::PyErr::value_error(
                        "int too large to convert",
                    ))
                } else {
                    Ok(truncated as i128)
                }
            }
            PyValue::Str(value) => crate::pycompat::py_int_parse(value)
                .and_then(|value| match value {
                    PyInt::Small(value) => Some(value),
                    PyInt::Big(value) => value.parse::<i128>().ok(),
                })
                .ok_or_else(|| {
                    crate::pycompat::PyErr::value_error(format!(
                        "invalid literal for int() with base 10: {}",
                        crate::pyfmt::py_repr_str(value)
                    ))
                }),
            other => Err(crate::pycompat::PyErr::value_error(format!(
                "int() argument must be a string, a bytes-like object or a real number, not '{}'",
                py_type_name(other)
            ))),
        }
    }
    /// `float(params.get(k) or default)` 等價。
    pub fn float_or(&self, k: &str, default: f64) -> Result<f64, crate::pycompat::PyErr> {
        use crate::pyjson::{PyInt, PyValue};

        let Some(value) = self.raw(k).filter(|value| value.is_truthy()) else {
            return Ok(default);
        };
        match value {
            PyValue::Bool(value) => Ok(if *value { 1.0 } else { 0.0 }),
            PyValue::Int(PyInt::Small(value)) => Ok(*value as f64),
            PyValue::Int(PyInt::Big(value)) => value
                .parse::<f64>()
                .ok()
                .filter(|value| value.is_finite())
                .ok_or_else(|| {
                    crate::pycompat::PyErr::value_error("int too large to convert to float")
                }),
            PyValue::Float(value) => Ok(*value),
            PyValue::Str(value) => crate::pycompat::py_float_parse(value).ok_or_else(|| {
                crate::pycompat::PyErr::value_error(format!(
                    "could not convert string to float: {}",
                    crate::pyfmt::py_repr_str(value)
                ))
            }),
            other => Err(crate::pycompat::PyErr::value_error(format!(
                "float() argument must be a string or a real number, not '{}'",
                py_type_name(other)
            ))),
        }
    }
    /// Python `bool(params.get(k))` 真值語意。
    pub fn truthy(&self, k: &str) -> bool {
        self.raw(k).is_some_and(|value| value.is_truthy())
    }
    /// 原始值（未轉型）。
    pub fn raw(&self, k: &str) -> Option<&crate::pyjson::PyValue> {
        self.raw.get(k)
    }
    /// `selector` 鏈（`session`／`selector` 等欄位的 fallback 順序）。
    pub fn selector_chain(&self) -> Vec<String> {
        ["selector", "session_id", "com", "alias"]
            .into_iter()
            .filter_map(|key| {
                self.raw(key)
                    .filter(|value| value.is_truthy())
                    .map(crate::pyfmt::py_str)
            })
            .collect()
    }
}

fn py_type_name(value: &crate::pyjson::PyValue) -> &'static str {
    use crate::pyjson::PyValue;
    match value {
        PyValue::Null => "NoneType",
        PyValue::Bool(_) => "bool",
        PyValue::Int(_) => "int",
        PyValue::Float(_) => "float",
        PyValue::Str(_) => "str",
        PyValue::List(_) => "list",
        PyValue::Dict(_) => "dict",
    }
}
