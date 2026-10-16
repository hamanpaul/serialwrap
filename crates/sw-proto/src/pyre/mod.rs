//! Python `re` 相容層（P3-T5 紅燈 stub，行為尚未實作）。
//!
//! 語法接受集合、`re.error` 訊息與 code point 位置照 CPython 3.12.3；執行引擎為 fancy-regex（ADR-01）。
//! 本檔目前為紅燈階段的 stub：公開介面依 plan Task 5 固定，行為本體一律 `todo!`，
//! 實作（Step 4）時移除 `allow(unused_variables)` 與所有 `todo!`。
#![allow(unused_variables)]

mod parse;
mod translate;

use crate::pyjson::PyValue;

/// Python `re` 旗標位元（與 CPython `re.I` 等常數同值）。
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct PyFlags(pub u32);

impl PyFlags {
    pub const I: u32 = 2;
    pub const L: u32 = 4;
    pub const M: u32 = 8;
    pub const S: u32 = 16;
    pub const U: u32 = 32;
    pub const X: u32 = 64;
    pub const A: u32 = 256;
}

/// 對應 Python `re.error`。
///
/// `Display`：pattern 含 `\n` 時為 `msg + " (line L, column C)"`；否則 `msg + " at position N"`；`pos` 為 `None` 時僅 `msg`。
#[derive(Debug, Clone, PartialEq)]
pub struct PyReError {
    pub msg: String,
    pub pos: Option<usize>,
    pub line_col: Option<(usize, usize)>,
}

impl std::fmt::Display for PyReError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        todo!("P3-T5: PyReError Display")
    }
}

/// 編譯錯誤分類。
///
/// `Re`：Python `re.error`；`Recursion`：Python `RecursionError`；
/// `Overflow`：Python `OverflowError`（例如 `a{99999999999}`）；`Untranslatable`：自製 parse 接受、但 fancy-regex 轉譯不出的構造（殘餘差異，EVT-D-S15）。
#[derive(Debug, Clone, PartialEq)]
pub enum CompileError {
    Re(PyReError),
    Recursion,
    Overflow(String),
    Untranslatable(String),
}

/// 已編譯的 pattern（實作時內含 `fancy_regex::Regex` 與原 pattern 字串）。
#[allow(dead_code)]
pub struct PyRegex {
    pattern: String,
}

/// 匹配區間，索引為 code point（非 byte）。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct PyMatch {
    pub start: usize,
    pub end: usize,
}

/// 執行期回溯上限；超過時視為未匹配（arch §3.6）。
pub const BACKTRACK_LIMIT: usize = 1_000_000;

/// 巢狀深度上限（Python `RecursionError` 的臨界深度 T）。
///
/// 紅燈 stub 佔位值 `0`；實作時改為 `contracts/golden/pyre.limits.json` 的 `parse_depth_limit`（`gen_pyre.py` 量得之 T）。
pub const PARSE_DEPTH_LIMIT: usize = 0;

/// `completion_regex_check` 的結果（對應 `command_completion.valid_completion_regex`）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CompletionCheck {
    Valid,
    Invalid,
    /// Python 未攔的例外（`OverflowError`）→ 呼叫端 exit 1、stdout 空。
    Crash(String),
}

impl PyRegex {
    /// 自字串起點搜尋；回傳 code point 索引。
    pub fn search(&self, hay: &str) -> Option<PyMatch> {
        todo!("P3-T5: PyRegex::search")
    }

    /// 自 `pos`（code point 索引）開始搜尋。
    pub fn search_from(&self, hay: &str, pos: usize) -> Option<PyMatch> {
        todo!("P3-T5: PyRegex::search_from")
    }
}

/// 以 Python `re` 語法接受集合編譯 `pat`。
pub fn compile(pat: &str, flags: PyFlags) -> Result<PyRegex, CompileError> {
    todo!("P3-T5: compile")
}

/// completion regex 檢查（對應 `command_completion.py`）。
///
/// `None` → `Valid`；非 Str 或空字串 → `Invalid`；UTF-8 長度 > 1024 → `Invalid`；
/// compile 為 `Re`／`Recursion` → `Invalid`；`Overflow` → `Crash`；`Untranslatable` → `Valid`（不在 CLI 本地拒絕）；
/// 可轉譯且 `search("")` 有結果 → `Invalid`。
pub fn completion_regex_check(v: Option<&PyValue>) -> CompletionCheck {
    todo!("P3-T5: completion_regex_check")
}

/// `completion_regex_check(v) == Valid`。
pub fn valid_completion_regex(v: Option<&PyValue>) -> bool {
    todo!("P3-T5: valid_completion_regex")
}
