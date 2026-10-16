//! Python 相容語意層（P3-T4 stub，行為尚未實作）。
#![allow(unused_variables)]

use crate::pyjson::{PyInt, PyValue};

/// `int(str)`：strip、正負號、底線、Unicode 十進位數字；拒絕 `"3.5"`、`"0x10"` 等。
pub fn py_int_parse(s: &str) -> Option<PyInt> {
    todo!("P3-T4: py_int_parse")
}

/// `float(str)`：nan/inf/infinity 不分大小寫、空白、底線。
pub fn py_float_parse(s: &str) -> Option<f64> {
    todo!("P3-T4: py_float_parse")
}

/// `str.isspace`。
pub fn py_is_space(c: char) -> bool {
    todo!("P3-T4: py_is_space")
}

/// `str.strip()`。
pub fn py_strip(s: &str) -> &str {
    todo!("P3-T4: py_strip")
}

/// `bytes.strip()`：預設剝除 `b" \t\n\r\x0b\x0c"`。
pub fn py_bytes_strip(b: &[u8]) -> &[u8] {
    todo!("P3-T4: py_bytes_strip")
}

/// `str.isdigit`（含上標數字）。
pub fn py_isdigit(s: &str) -> bool {
    todo!("P3-T4: py_isdigit")
}

/// `str.splitlines()`。
pub fn splitlines(s: &str) -> Vec<&str> {
    todo!("P3-T4: splitlines")
}

/// universal newlines：`"\r\n"`、`"\r"` 統一轉為 `"\n"`。
pub fn universal_newlines(s: &str) -> String {
    todo!("P3-T4: universal_newlines")
}

/// `errors="replace"` UTF-8 解碼（W3C / Unicode maximal subpart 規則）。
pub fn decode_utf8_replace(b: &[u8]) -> String {
    todo!("P3-T4: decode_utf8_replace")
}

/// UTF-8 strict 解碼錯誤（對齊 CPython UnicodeDecodeError 格式）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UnicodeDecodeError {
    pub start: usize,
    pub end: usize,
    pub byte: u8,
    pub reason: &'static str,
}

impl std::fmt::Display for UnicodeDecodeError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        todo!("P3-T4: UnicodeDecodeError::fmt")
    }
}

impl std::error::Error for UnicodeDecodeError {}

/// `errors="strict"` UTF-8 解碼。
pub fn decode_utf8_strict(b: &[u8]) -> Result<String, UnicodeDecodeError> {
    todo!("P3-T4: decode_utf8_strict")
}

/// Python 字串字元長度（Unicode scalar count）。
pub fn py_len_chars(s: &str) -> usize {
    todo!("P3-T4: py_len_chars")
}

/// Python 字元索引轉 UTF-8 位元組位移。
pub fn char_index_to_byte(s: &str, ci: usize) -> usize {
    todo!("P3-T4: char_index_to_byte")
}

/// UTF-8 位元組位移轉 Python 字元索引。
pub fn byte_index_to_char(s: &str, bi: usize) -> usize {
    todo!("P3-T4: byte_index_to_char")
}

/// Python truthy 判斷（`bool(v)`）。
pub fn py_truthy(v: &PyValue) -> bool {
    todo!("P3-T4: py_truthy")
}

/// CPython OSError 錯誤字串格式化結構。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PyOsError {
    pub errno: Option<i64>,
    pub winerror: Option<i64>,
    pub strerror: Option<String>,
    pub filename: Option<String>,
    pub plain: Option<String>,
}

impl std::fmt::Display for PyOsError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        todo!("P3-T4: PyOsError::fmt")
    }
}

impl std::error::Error for PyOsError {}

/// SHA-256 小寫十六進位字串。
pub fn sha256_hex(data: &[u8]) -> String {
    todo!("P3-T4: sha256_hex")
}
