//! Python `json` 相容層（P3-T3）。
//!
//! 本檔目前為紅燈階段的 stub：公開介面依 plan／arch §3.3 固定，行為本體一律 `todo!`，
//! 實作（Step 4）時移除 `allow(unused_variables)` 與所有 `todo!`。
#![allow(unused_variables)]

use indexmap::IndexMap;

/// Python 值（`json.loads` 的產出、`json.dumps` 的輸入）。
///
/// 相對 arch §3.3 的細化：加 `Clone, Debug, PartialEq`，供測試 `assert_eq!(loads(..), Err(..))` 使用。
#[derive(Clone, Debug, PartialEq)]
pub enum PyValue {
    Null,
    Bool(bool),
    Int(PyInt),
    Float(f64),
    Str(String),
    List(Vec<PyValue>),
    Dict(PyDict),
}

/// Python 整數；超出 i128 時以正規化十進位字串保存（含負號）。
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum PyInt {
    Small(i128),
    Big(String),
}

/// 保留插入序的字串鍵字典（Python dict 語意：已存在的 key 改值、位置不變）。
#[derive(Clone, Debug, Default, PartialEq)]
pub struct PyDict(pub IndexMap<String, PyValue>);

impl PyDict {
    pub fn new() -> Self {
        todo!("P3-T3: PyDict::new")
    }
    pub fn from_pairs(v: Vec<(String, PyValue)>) -> Self {
        todo!("P3-T3: PyDict::from_pairs")
    }
    pub fn insert(&mut self, k: &str, v: PyValue) {
        todo!("P3-T3: PyDict::insert")
    }
    pub fn get(&self, k: &str) -> Option<&PyValue> {
        todo!("P3-T3: PyDict::get")
    }
    pub fn setdefault(&mut self, k: &str, v: PyValue) {
        todo!("P3-T3: PyDict::setdefault")
    }
    pub fn update(&mut self, other: &PyDict) {
        todo!("P3-T3: PyDict::update")
    }
    /// `bool(d.get(k))`。
    pub fn get_truthy(&self, k: &str) -> bool {
        todo!("P3-T3: PyDict::get_truthy")
    }
}

/// 對應 Python `json.dumps` 的 `separators` 設定。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Sep {
    /// `(",", ":")`
    Compact,
    /// `(", ", ": ")`（預設）
    Default,
    /// `indent=n`
    Indent(u8),
}

/// `json.dumps` 的選項組合（四種 profile 見下方常數）。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct DumpOpts {
    pub sep: Sep,
    pub ensure_ascii: bool,
    pub sort_keys: bool,
    pub allow_nan: bool,
}

/// CLI stdout：`json.dumps(v, ensure_ascii=False, sort_keys=True, separators=(",", ":"))`。
pub const CLI_STDOUT: DumpOpts = DumpOpts { sep: Sep::Compact, ensure_ascii: false, sort_keys: true, allow_nan: true };
/// RPC 線路格式：Compact、ensure_ascii=false、sort_keys=false。
pub const RPC_WIRE: DumpOpts = DumpOpts { sep: Sep::Compact, ensure_ascii: false, sort_keys: false, allow_nan: true };
/// 早期錯誤回應：`json.dumps(v, ensure_ascii=False)`（預設分隔符）。
pub const RPC_EARLY: DumpOpts = DumpOpts { sep: Sep::Default, ensure_ascii: false, sort_keys: false, allow_nan: true };
/// 磁碟穩定格式（state.json／WAL）：Compact、ensure_ascii=false、sort_keys=true。
pub const DISK_STABLE: DumpOpts = DumpOpts { sep: Sep::Compact, ensure_ascii: false, sort_keys: true, allow_nan: true };

/// 對應 Python `json.JSONDecodeError` 的欄位。`pos` 以 code point 計。
#[derive(Debug, Clone, PartialEq)]
pub struct JsonDecodeError {
    pub msg: String,
    pub pos: usize,
    pub lineno: usize,
    pub colno: usize,
}

impl std::fmt::Display for JsonDecodeError {
    /// 格式：`"{msg}: line {lineno} column {colno} (char {pos})"`。
    fn fmt(&self, _f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        todo!("P3-T3: JsonDecodeError Display")
    }
}

/// `loads` 的錯誤；`Recursion` 對應 Python `RecursionError`（不是 `ValueError`）。
#[derive(Debug, Clone, PartialEq)]
pub enum JsonError {
    Decode(JsonDecodeError),
    Recursion,
}

impl std::fmt::Display for JsonError {
    /// `Decode` 同 `JsonDecodeError`；`Recursion` 為 `"maximum recursion depth exceeded"`。
    fn fmt(&self, _f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        todo!("P3-T3: JsonError Display")
    }
}

/// Rust 自訂的巢狀深度上限（非契約，見 plan §0.7）；解析器以顯式堆疊實作，不靠遞迴。
pub const JSON_MAX_DEPTH: usize = 2900;

/// `json.loads`（strict=True）。
pub fn loads(s: &str) -> Result<PyValue, JsonError> {
    todo!("P3-T3: loads")
}

/// `json.dumps`，不含尾端換行。
pub fn dumps(v: &PyValue, o: &DumpOpts) -> String {
    todo!("P3-T3: dumps")
}

/// Python `repr(float)`；唯一實作位置，`pyfmt::py_repr_float` 單向轉呼叫。
pub fn repr_float(x: f64) -> String {
    todo!("P3-T3: repr_float")
}

/// 依插入序建立 `PyDict`：`pydict! { "k" => v, ... }`。
#[macro_export]
macro_rules! pydict {
    ($($k:expr => $v:expr),* $(,)?) => {
        $crate::pyjson::PyDict::from_pairs(vec![$(($k.to_string(), $crate::pyjson::PyValue::from($v))),*])
    };
}

impl From<bool> for PyValue {
    fn from(b: bool) -> Self {
        todo!("P3-T3: From<bool>")
    }
}
impl From<&str> for PyValue {
    fn from(s: &str) -> Self {
        todo!("P3-T3: From<&str>")
    }
}
impl From<String> for PyValue {
    fn from(s: String) -> Self {
        todo!("P3-T3: From<String>")
    }
}
impl From<i64> for PyValue {
    fn from(n: i64) -> Self {
        todo!("P3-T3: From<i64>")
    }
}
impl From<f64> for PyValue {
    fn from(x: f64) -> Self {
        todo!("P3-T3: From<f64>")
    }
}
impl From<PyInt> for PyValue {
    fn from(n: PyInt) -> Self {
        todo!("P3-T3: From<PyInt>")
    }
}
impl From<PyDict> for PyValue {
    fn from(d: PyDict) -> Self {
        todo!("P3-T3: From<PyDict>")
    }
}
impl From<Vec<PyValue>> for PyValue {
    fn from(v: Vec<PyValue>) -> Self {
        todo!("P3-T3: From<Vec<PyValue>>")
    }
}
impl<T: Into<PyValue>> From<Option<T>> for PyValue {
    /// `None` → `Null`。
    fn from(o: Option<T>) -> Self {
        todo!("P3-T3: From<Option<T>>")
    }
}
