//! YAML 1.1 `SafeLoader` 子集（P3-T6 紅燈 stub，行為尚未實作）。
//!
//! 只做讀取：以 yaml-rust2 低階事件取得 scalar style 與 tag，再由自製 implicit resolver
//! 逐條照 PyYAML `resolver.py`；語料與期望值由 `scripts/rust/goldens/gen_yaml11.py` 產生。
//! 本檔目前為紅燈階段的 stub：公開介面依 plan Task 6 固定，行為本體一律 `todo!`，
//! 實作（Step 4）時移除 `allow(unused_variables)` 與所有 `todo!`。
#![allow(unused_variables)]

mod resolve;

use crate::pyjson::{PyInt, PyValue};

/// YAML 解析結果（對應 PyYAML `safe_load` 的 Python 值）。
#[derive(Debug, Clone, PartialEq)]
pub enum YValue {
    Null,
    Bool(bool),
    Int(PyInt),
    Float(f64),
    Str(String),
    /// `!!timestamp`／implicit timestamp；`py_str` 為 Python `str(date)`／`str(datetime)`。
    Timestamp { py_str: String },
    Binary(Vec<u8>),
    Seq(Vec<YValue>),
    Map(YMap),
}

/// 保留插入序的 mapping。重複 key（Python 相等：`1 == 1.0 == True`）→ 值覆寫、位置保留。
#[derive(Debug, Clone, Default, PartialEq)]
pub struct YMap(pub Vec<(YValue, YValue)>);

impl YMap {
    /// 以字串 key 取值（僅比對 `YValue::Str` key）。
    pub fn get_str(&self, k: &str) -> Option<&YValue> {
        todo!("P3-T6: YMap::get_str")
    }

    /// 是否含字串 key。
    pub fn contains_str(&self, k: &str) -> bool {
        todo!("P3-T6: YMap::contains_str")
    }
}

/// YAML 解析錯誤（訊息非契約，見 plan G5）。
#[derive(Debug, Clone, PartialEq)]
pub struct YamlError {
    pub message: String,
}

/// 只取第一份文件；空文件 → `Null`；多文件 → `YamlError`（PyYAML `expected a single document`）。
pub fn safe_load(text: &str) -> Result<YValue, YamlError> {
    todo!("P3-T6: safe_load")
}

/// `str(v)` 語意（`Timestamp` 用 `py_str`）。
pub fn py_str_of(v: &YValue) -> String {
    todo!("P3-T6: py_str_of")
}

/// Python `bool(v)` 語意。
pub fn py_truthy_of(v: &YValue) -> bool {
    todo!("P3-T6: py_truthy_of")
}

/// golden 比對用的型別標記編碼（`gen_yaml11.py` 的 `tag()` 逐一對應）。
pub fn to_tagged_json(v: &YValue) -> PyValue {
    todo!("P3-T6: to_tagged_json")
}
