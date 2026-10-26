//! 規則 schema 驗證（P7 Task 11；`event_engine/schema.py`，09 §3.1）。P7 Task 0 骨架。

use sw_proto::pyjson::{PyDict, PyInt, PyValue};
use sw_proto::pyre::PyFlags;

#[derive(Clone, Debug, PartialEq)]
pub struct Pattern {
    pub kind: String,
    pub value: String,
    pub flags: String,
}

#[derive(Clone, Debug, PartialEq)]
pub struct Handler {
    pub exec: Option<Vec<String>>,
    pub shell: Option<String>,
}

#[derive(Clone, Debug, PartialEq)]
pub struct Rule {
    pub schema_version: i64,
    pub owner: String,
    pub name: String,
    pub rule_id: String,
    pub kind: String,
    pub selectors: Vec<String>,
    pub profile: String,
    pub level: String,
    pub pattern: Pattern,
    pub scope: String,
    /// int 或 bool，保留原值。
    pub max_fires: Option<PyValue>,
    /// `PyInt` 含 Big，保留 Python 任意精度。
    pub cooldown_ms: PyInt,
    pub timeout_ms: PyInt,
    pub handler: Handler,
    pub auto_enable_com_on_load: bool,
    pub debug: bool,
    pub raw: PyDict,
}

impl Rule {
    /// 超出 i128 飽和（比較語意與 Python 大整數一致：永遠冷卻）。
    pub fn cooldown_ms_sat(&self) -> i128 {
        todo!("P7 Task 11")
    }
    /// 夾到 `Duration::MAX`（永不到期）。
    pub fn timeout(&self) -> std::time::Duration {
        todo!("P7 Task 11")
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum RuleError {
    /// RuleSchemaError。
    Schema(String),
    /// 其他 ValueError。
    Value(String),
    /// TypeError。
    Type(String),
}

pub fn validate_rule_dict(_obj: &PyValue) -> Result<Rule, RuleError> {
    todo!("P7 Task 11")
}

/// i/s/m；其他 → `Schema("unsupported regex flag: {ch}")`（DF-08）。
pub fn flags_from_string(_s: &str) -> Result<PyFlags, RuleError> {
    todo!("P7 Task 11")
}

pub const NAME_RE: &str = r"^[a-z0-9][a-z0-9-]{0,63}$";
