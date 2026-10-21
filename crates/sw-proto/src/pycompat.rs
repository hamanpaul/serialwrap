//! Python 相容語意層：`int`／`float`／`str` 方法、UTF-8 解碼、`OSError` 字串（P3-T4）。
//!
//! 行為以 CPython 3.12.3 為基準（見 `scripts/rust/goldens/gen_pycompat.py`，golden 位於 `contracts/golden/`）。
//! Unicode 分類表（空白、十進位零點、`digit` 屬性、非可印字元區段）由 CPython 3.12.3 的 `unicodedata`
//! （Unicode 15.0.0）產生並內嵌於本檔末尾，不依賴 Rust 標準函式庫的 Unicode 版本，以確保與 Python 位元組一致。
//! 內嵌表的來源為 `scripts/rust/goldens/gen_unicode_tables.py` 產生的 `contracts/golden/unicode_tables.txt`；
//! 本檔單元測試以全部碼點逐一比對，表與 golden 任一漂移即失敗。

use crate::pyfmt::py_repr_str;
use crate::pyjson::{PyInt, PyValue};
use sha2::{Digest, Sha256};
use std::cmp::Ordering;

/// Python `str.isspace` 的 ASCII 與 Unicode 空白集合（與 `Py_UNICODE_ISSPACE` 一致，含 U+001C..U+001F）。
pub fn py_is_space(c: char) -> bool {
    matches!(
        c as u32,
        0x09..=0x0d
            | 0x1c..=0x20
            | 0x85
            | 0xa0
            | 0x1680
            | 0x2000..=0x200a
            | 0x2028
            | 0x2029
            | 0x202f
            | 0x205f
            | 0x3000
    )
}

/// `str.strip()`：剝除兩端 `py_is_space` 字元。
pub fn py_strip(s: &str) -> &str {
    s.trim_matches(py_is_space)
}

/// `bytes.strip()`：預設剝除 `b" \t\n\r\x0b\x0c"`（不含 `\x1c..\x1f`）。
pub fn py_bytes_strip(b: &[u8]) -> &[u8] {
    let is_ws = |c: &u8| matches!(c, b' ' | b'\t' | b'\n' | b'\r' | 0x0b | 0x0c);
    let start = b.iter().position(|c| !is_ws(c)).unwrap_or(b.len());
    let end = b.iter().rposition(|c| !is_ws(c)).map_or(start, |i| i + 1);
    &b[start..end]
}

/// `str.isdigit`：非空且每個字元皆具 `digit` 屬性（含上標數字）。
pub fn py_isdigit(s: &str) -> bool {
    !s.is_empty() && s.chars().all(|c| in_ranges(DIGIT_RANGES, c as u32))
}

/// `str.splitlines()`：換行集合為 `\n`、`\r`、`\r\n`、`\x0b`、`\x0c`、`\x1c`、`\x1d`、`\x1e`、`\x85`、`\u{2028}`、`\u{2029}`。
/// 結尾的換行不產生空的尾段。
pub fn splitlines(s: &str) -> Vec<&str> {
    let mut out = Vec::new();
    let mut start = 0usize;
    let mut iter = s.char_indices().peekable();
    while let Some((i, c)) = iter.next() {
        match c {
            '\r' => {
                out.push(&s[start..i]);
                if let Some(&(_, '\n')) = iter.peek() {
                    iter.next();
                    start = i + 2;
                } else {
                    start = i + 1;
                }
            }
            '\n' | '\x0b' | '\x0c' | '\x1c' | '\x1d' | '\x1e' | '\u{85}' | '\u{2028}'
            | '\u{2029}' => {
                out.push(&s[start..i]);
                start = i + c.len_utf8();
            }
            _ => {}
        }
    }
    if start < s.len() {
        out.push(&s[start..]);
    }
    out
}

/// universal newlines：`"\r\n"` 與單獨的 `"\r"` 統一轉為 `"\n"`。
pub fn universal_newlines(s: &str) -> String {
    s.replace("\r\n", "\n").replace('\r', "\n")
}

/// `errors="replace"` UTF-8 解碼：每個 maximal subpart 錯誤範圍以一個 U+FFFD 取代。
pub fn decode_utf8_replace(b: &[u8]) -> String {
    let mut out = String::with_capacity(b.len());
    let mut rest = b;
    loop {
        match std::str::from_utf8(rest) {
            Ok(s) => {
                out.push_str(s);
                break;
            }
            Err(e) => {
                let good = e.valid_up_to();
                out.push_str(String::from_utf8_lossy(&rest[..good]).as_ref());
                out.push('\u{FFFD}');
                let skip = e.error_len().unwrap_or(rest.len() - good);
                rest = &rest[good + skip..];
            }
        }
    }
    out
}

/// UTF-8 strict 解碼錯誤（對齊 CPython `UnicodeDecodeError` 的欄位與字串格式）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UnicodeDecodeError {
    /// 錯誤範圍起點（位元組位移）。
    pub start: usize,
    /// 錯誤範圍終點（開區間，位元組位移）。
    pub end: usize,
    /// 起點位元組的值（僅單位元組錯誤的格式使用）。
    pub byte: u8,
    /// 原因：`invalid start byte`、`invalid continuation byte` 或 `unexpected end of data`。
    pub reason: &'static str,
}

impl std::fmt::Display for UnicodeDecodeError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        if self.end - self.start == 1 {
            write!(
                f,
                "'utf-8' codec can't decode byte 0x{:02x} in position {}: {}",
                self.byte, self.start, self.reason
            )
        } else {
            write!(
                f,
                "'utf-8' codec can't decode bytes in position {}-{}: {}",
                self.start,
                self.end - 1,
                self.reason
            )
        }
    }
}

impl std::error::Error for UnicodeDecodeError {}

/// `errors="strict"` UTF-8 解碼。
pub fn decode_utf8_strict(b: &[u8]) -> Result<String, UnicodeDecodeError> {
    match std::str::from_utf8(b) {
        Ok(s) => Ok(s.to_string()),
        Err(e) => {
            let start = e.valid_up_to();
            let (end, reason) = match e.error_len() {
                Some(n) => {
                    let lead = b[start];
                    let reason = if (0x80..=0xc1).contains(&lead) || lead >= 0xf5 {
                        "invalid start byte"
                    } else {
                        "invalid continuation byte"
                    };
                    (start + n, reason)
                }
                None => (b.len(), "unexpected end of data"),
            };
            Err(UnicodeDecodeError {
                start,
                end,
                byte: b[start],
                reason,
            })
        }
    }
}

/// Python 字串字元長度（Unicode scalar count）。
pub fn py_len_chars(s: &str) -> usize {
    s.chars().count()
}

/// Python 字元索引轉 UTF-8 位元組位移；超出範圍時回傳 `s.len()`。
pub fn char_index_to_byte(s: &str, ci: usize) -> usize {
    s.char_indices().nth(ci).map_or(s.len(), |(b, _)| b)
}

/// UTF-8 位元組位移轉 Python 字元索引（計數起點嚴格小於 `bi` 的字元）。
pub fn byte_index_to_char(s: &str, bi: usize) -> usize {
    s.char_indices().take_while(|(b, _)| *b < bi).count()
}

/// Python truthy 判斷（`bool(v)`）。
pub fn py_truthy(v: &PyValue) -> bool {
    v.is_truthy()
}

/// CPython `OSError` 錯誤字串格式化結構。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PyOsError {
    pub errno: Option<i64>,
    pub winerror: Option<i64>,
    pub strerror: Option<String>,
    pub filename: Option<String>,
    pub plain: Option<String>,
}

impl std::fmt::Display for PyOsError {
    /// 規則：`plain` 有值 → `plain`；`winerror` 有值 → `[WinError w] strerror`；否則 `errno` 有值 → `[Errno e] strerror`；
    /// 之後若有 `filename`，再接 `: ` 與 `repr(filename)`。
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        let strerror = self.strerror.as_deref().unwrap_or("");
        let mut out = if let Some(plain) = &self.plain {
            plain.clone()
        } else if let Some(w) = self.winerror {
            format!("[WinError {w}] {strerror}")
        } else if let Some(e) = self.errno {
            format!("[Errno {e}] {strerror}")
        } else {
            strerror.to_string()
        };
        if let Some(name) = &self.filename {
            out.push_str(": ");
            out.push_str(&py_repr_str(name));
        }
        f.write_str(&out)
    }
}

impl std::error::Error for PyOsError {}

/// SHA-256 小寫十六進位字串。
pub fn sha256_hex(data: &[u8]) -> String {
    Sha256::digest(data)
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect()
}

// ---------------------------------------------------------------------------
// 數值解析（int／float）
// ---------------------------------------------------------------------------

/// 對應 CPython `_PyUnicode_TransformDecimalAndSpaceToASCII`：ASCII 原樣保留（含 U+001C..U+001F），
/// 非 ASCII 空白轉為 `' '`，非 ASCII 十進位數字轉為對應 ASCII 數字；其餘非 ASCII 字元無法解析（回傳 `None`）。
fn normalize_numeric(s: &str) -> Option<String> {
    let mut out = String::with_capacity(s.len());
    for c in s.chars() {
        if (c as u32) < 127 {
            out.push(c);
        } else if py_is_space(c) {
            out.push(' ');
        } else if let Some(d) = decimal_value(c) {
            out.push((b'0' + d as u8) as char);
        } else {
            return None;
        }
    }
    Some(out)
}

/// CPython `sys.get_int_max_str_digits()` 預設值（十進位字串位數上限）。
pub const INT_MAX_STR_DIGITS: usize = 4300;

/// `int(str)`：strip、正負號、底線、Unicode 十進位數字；拒絕 `"3.5"`、`"0x10"` 等；位數超過 [`INT_MAX_STR_DIGITS`] 回傳 `None`。
pub fn py_int_parse(s: &str) -> Option<PyInt> {
    let norm = normalize_numeric(s)?;
    let t = norm.trim_matches(is_ascii_numeric_space);
    let (neg, body) = match t.as_bytes().first() {
        Some(b'-') => (true, &t[1..]),
        Some(b'+') => (false, &t[1..]),
        _ => (false, t),
    };
    let bytes = body.as_bytes();
    let mut digits = String::with_capacity(bytes.len());
    for (i, &b) in bytes.iter().enumerate() {
        match b {
            b'0'..=b'9' => digits.push(b as char),
            b'_' => {
                let prev_ok = i > 0 && bytes[i - 1].is_ascii_digit();
                let next_ok = i + 1 < bytes.len() && bytes[i + 1].is_ascii_digit();
                if !(prev_ok && next_ok) {
                    return None;
                }
            }
            _ => return None,
        }
    }
    // CPython 3.12 int 位數上限：十進位位數（含前導零、不含底線與符號）超過 4300 即 ValueError。
    if digits.is_empty() || digits.len() > INT_MAX_STR_DIGITS {
        return None;
    }
    let trimmed = digits.trim_start_matches('0');
    let mag = if trimmed.is_empty() { "0" } else { trimmed };
    let signed = if neg && mag != "0" {
        format!("-{mag}")
    } else {
        mag.to_string()
    };
    Some(match signed.parse::<i128>() {
        Ok(v) => PyInt::Small(v),
        Err(_) => PyInt::Big(signed),
    })
}

/// `float(str)`：nan／inf／infinity 不分大小寫、空白、底線（僅限兩數字之間）。
pub fn py_float_parse(s: &str) -> Option<f64> {
    let norm = normalize_numeric(s)?;
    let t = norm.trim_matches(is_ascii_numeric_space);
    let bytes = t.as_bytes();
    let mut clean = String::with_capacity(bytes.len());
    for (i, &b) in bytes.iter().enumerate() {
        if b == b'_' {
            let prev_ok = i > 0 && bytes[i - 1].is_ascii_digit();
            let next_ok = i + 1 < bytes.len() && bytes[i + 1].is_ascii_digit();
            if !(prev_ok && next_ok) {
                return None;
            }
        } else {
            clean.push(b as char);
        }
    }
    let (neg, body) = match clean.as_bytes().first() {
        Some(b'-') => (true, &clean[1..]),
        Some(b'+') => (false, &clean[1..]),
        _ => (false, clean.as_str()),
    };
    let lower = body.to_ascii_lowercase();
    if lower == "inf" || lower == "infinity" {
        return Some(if neg {
            f64::NEG_INFINITY
        } else {
            f64::INFINITY
        });
    }
    if lower == "nan" {
        return Some(f64::NAN);
    }
    if !is_decimal_float_literal(body) {
        return None;
    }
    clean.parse::<f64>().ok()
}

fn is_ascii_numeric_space(c: char) -> bool {
    matches!(c, ' ' | '\t' | '\n' | '\r' | '\x0b' | '\x0c')
}

/// 十進位浮點文字語法（已去除底線與符號）：`digits [. digits?] | . digits`，可選 `[eE][+-]?digits`。
fn is_decimal_float_literal(body: &str) -> bool {
    let b = body.as_bytes();
    let mut i = 0;
    let mut mant_digits = 0;
    while i < b.len() && b[i].is_ascii_digit() {
        i += 1;
        mant_digits += 1;
    }
    if i < b.len() && b[i] == b'.' {
        i += 1;
        while i < b.len() && b[i].is_ascii_digit() {
            i += 1;
            mant_digits += 1;
        }
    }
    if mant_digits == 0 {
        return false;
    }
    if i < b.len() && (b[i] == b'e' || b[i] == b'E') {
        i += 1;
        if i < b.len() && (b[i] == b'+' || b[i] == b'-') {
            i += 1;
        }
        let exp_start = i;
        while i < b.len() && b[i].is_ascii_digit() {
            i += 1;
        }
        if i == exp_start {
            return false;
        }
    }
    i == b.len()
}

// ---------------------------------------------------------------------------
// Unicode 分類查詢（表格見檔案末尾）
// ---------------------------------------------------------------------------

fn in_ranges(table: &[(u32, u32)], cp: u32) -> bool {
    table
        .binary_search_by(|&(lo, hi)| {
            if hi < cp {
                Ordering::Less
            } else if lo > cp {
                Ordering::Greater
            } else {
                Ordering::Equal
            }
        })
        .is_ok()
}

/// 十進位數值（Unicode `decimal` 屬性）；非十進位數字回傳 `None`。
fn decimal_value(c: char) -> Option<u32> {
    let cp = c as u32;
    let idx = DECIMAL_ZEROS.partition_point(|&z| z <= cp);
    if idx == 0 {
        return None;
    }
    let zero = DECIMAL_ZEROS[idx - 1];
    (cp - zero <= 9).then_some(cp - zero)
}

/// Python `str.isprintable` 對單一字元的判定（`repr` 跳脫規則用）。
pub(crate) fn py_isprintable(c: char) -> bool {
    let cp = c as u32;
    cp == 0x20 || !in_ranges(NONPRINT_RANGES, cp)
}

// ---------------------------------------------------------------------------
// Unicode 15.0.0 表格（由 CPython 3.12.3 unicodedata 產生；勿手改）
// ---------------------------------------------------------------------------

/// Unicode `Nd` 十進位數字的零點（每組 10 個連續碼點，數值 0..=9）。
#[rustfmt::skip]
const DECIMAL_ZEROS: &[u32] = &[
    0x00030, 0x00660, 0x006F0, 0x007C0, 0x00966, 0x009E6, 0x00A66, 0x00AE6,
    0x00B66, 0x00BE6, 0x00C66, 0x00CE6, 0x00D66, 0x00DE6, 0x00E50, 0x00ED0,
    0x00F20, 0x01040, 0x01090, 0x017E0, 0x01810, 0x01946, 0x019D0, 0x01A80,
    0x01A90, 0x01B50, 0x01BB0, 0x01C40, 0x01C50, 0x0A620, 0x0A8D0, 0x0A900,
    0x0A9D0, 0x0A9F0, 0x0AA50, 0x0ABF0, 0x0FF10, 0x104A0, 0x10D30, 0x11066,
    0x110F0, 0x11136, 0x111D0, 0x112F0, 0x11450, 0x114D0, 0x11650, 0x116C0,
    0x11730, 0x118E0, 0x11950, 0x11C50, 0x11D50, 0x11DA0, 0x11F50, 0x16A60,
    0x16AC0, 0x16B50, 0x1D7CE, 0x1D7D8, 0x1D7E2, 0x1D7EC, 0x1D7F6, 0x1E140,
    0x1E2F0, 0x1E4F0, 0x1E950, 0x1FBF0,
];

/// `str.isdigit` 的 `digit` 屬性區段（閉區間）。
#[rustfmt::skip]
const DIGIT_RANGES: &[(u32, u32)] = &[
    (0x00030, 0x00039), (0x000B2, 0x000B3), (0x000B9, 0x000B9), (0x00660, 0x00669),
    (0x006F0, 0x006F9), (0x007C0, 0x007C9), (0x00966, 0x0096F), (0x009E6, 0x009EF),
    (0x00A66, 0x00A6F), (0x00AE6, 0x00AEF), (0x00B66, 0x00B6F), (0x00BE6, 0x00BEF),
    (0x00C66, 0x00C6F), (0x00CE6, 0x00CEF), (0x00D66, 0x00D6F), (0x00DE6, 0x00DEF),
    (0x00E50, 0x00E59), (0x00ED0, 0x00ED9), (0x00F20, 0x00F29), (0x01040, 0x01049),
    (0x01090, 0x01099), (0x01369, 0x01371), (0x017E0, 0x017E9), (0x01810, 0x01819),
    (0x01946, 0x0194F), (0x019D0, 0x019DA), (0x01A80, 0x01A89), (0x01A90, 0x01A99),
    (0x01B50, 0x01B59), (0x01BB0, 0x01BB9), (0x01C40, 0x01C49), (0x01C50, 0x01C59),
    (0x02070, 0x02070), (0x02074, 0x02079), (0x02080, 0x02089), (0x02460, 0x02468),
    (0x02474, 0x0247C), (0x02488, 0x02490), (0x024EA, 0x024EA), (0x024F5, 0x024FD),
    (0x024FF, 0x024FF), (0x02776, 0x0277E), (0x02780, 0x02788), (0x0278A, 0x02792),
    (0x0A620, 0x0A629), (0x0A8D0, 0x0A8D9), (0x0A900, 0x0A909), (0x0A9D0, 0x0A9D9),
    (0x0A9F0, 0x0A9F9), (0x0AA50, 0x0AA59), (0x0ABF0, 0x0ABF9), (0x0FF10, 0x0FF19),
    (0x104A0, 0x104A9), (0x10A40, 0x10A43), (0x10D30, 0x10D39), (0x10E60, 0x10E68),
    (0x11052, 0x1105A), (0x11066, 0x1106F), (0x110F0, 0x110F9), (0x11136, 0x1113F),
    (0x111D0, 0x111D9), (0x112F0, 0x112F9), (0x11450, 0x11459), (0x114D0, 0x114D9),
    (0x11650, 0x11659), (0x116C0, 0x116C9), (0x11730, 0x11739), (0x118E0, 0x118E9),
    (0x11950, 0x11959), (0x11C50, 0x11C59), (0x11D50, 0x11D59), (0x11DA0, 0x11DA9),
    (0x11F50, 0x11F59), (0x16A60, 0x16A69), (0x16AC0, 0x16AC9), (0x16B50, 0x16B59),
    (0x1D7CE, 0x1D7FF), (0x1E140, 0x1E149), (0x1E2F0, 0x1E2F9), (0x1E4F0, 0x1E4F9),
    (0x1E950, 0x1E959), (0x1F100, 0x1F10A), (0x1FBF0, 0x1FBF9),
];

/// 非可印字元區段（Cc／Cf／Cs／Co／Cn／Zl／Zp／Zs，不含 U+0020；閉區間）。
#[rustfmt::skip]
const NONPRINT_RANGES: &[(u32, u32)] = &[
    (0x00000, 0x0001F), (0x0007F, 0x000A0), (0x000AD, 0x000AD), (0x00378, 0x00379),
    (0x00380, 0x00383), (0x0038B, 0x0038B), (0x0038D, 0x0038D), (0x003A2, 0x003A2),
    (0x00530, 0x00530), (0x00557, 0x00558), (0x0058B, 0x0058C), (0x00590, 0x00590),
    (0x005C8, 0x005CF), (0x005EB, 0x005EE), (0x005F5, 0x00605), (0x0061C, 0x0061C),
    (0x006DD, 0x006DD), (0x0070E, 0x0070F), (0x0074B, 0x0074C), (0x007B2, 0x007BF),
    (0x007FB, 0x007FC), (0x0082E, 0x0082F), (0x0083F, 0x0083F), (0x0085C, 0x0085D),
    (0x0085F, 0x0085F), (0x0086B, 0x0086F), (0x0088F, 0x00897), (0x008E2, 0x008E2),
    (0x00984, 0x00984), (0x0098D, 0x0098E), (0x00991, 0x00992), (0x009A9, 0x009A9),
    (0x009B1, 0x009B1), (0x009B3, 0x009B5), (0x009BA, 0x009BB), (0x009C5, 0x009C6),
    (0x009C9, 0x009CA), (0x009CF, 0x009D6), (0x009D8, 0x009DB), (0x009DE, 0x009DE),
    (0x009E4, 0x009E5), (0x009FF, 0x00A00), (0x00A04, 0x00A04), (0x00A0B, 0x00A0E),
    (0x00A11, 0x00A12), (0x00A29, 0x00A29), (0x00A31, 0x00A31), (0x00A34, 0x00A34),
    (0x00A37, 0x00A37), (0x00A3A, 0x00A3B), (0x00A3D, 0x00A3D), (0x00A43, 0x00A46),
    (0x00A49, 0x00A4A), (0x00A4E, 0x00A50), (0x00A52, 0x00A58), (0x00A5D, 0x00A5D),
    (0x00A5F, 0x00A65), (0x00A77, 0x00A80), (0x00A84, 0x00A84), (0x00A8E, 0x00A8E),
    (0x00A92, 0x00A92), (0x00AA9, 0x00AA9), (0x00AB1, 0x00AB1), (0x00AB4, 0x00AB4),
    (0x00ABA, 0x00ABB), (0x00AC6, 0x00AC6), (0x00ACA, 0x00ACA), (0x00ACE, 0x00ACF),
    (0x00AD1, 0x00ADF), (0x00AE4, 0x00AE5), (0x00AF2, 0x00AF8), (0x00B00, 0x00B00),
    (0x00B04, 0x00B04), (0x00B0D, 0x00B0E), (0x00B11, 0x00B12), (0x00B29, 0x00B29),
    (0x00B31, 0x00B31), (0x00B34, 0x00B34), (0x00B3A, 0x00B3B), (0x00B45, 0x00B46),
    (0x00B49, 0x00B4A), (0x00B4E, 0x00B54), (0x00B58, 0x00B5B), (0x00B5E, 0x00B5E),
    (0x00B64, 0x00B65), (0x00B78, 0x00B81), (0x00B84, 0x00B84), (0x00B8B, 0x00B8D),
    (0x00B91, 0x00B91), (0x00B96, 0x00B98), (0x00B9B, 0x00B9B), (0x00B9D, 0x00B9D),
    (0x00BA0, 0x00BA2), (0x00BA5, 0x00BA7), (0x00BAB, 0x00BAD), (0x00BBA, 0x00BBD),
    (0x00BC3, 0x00BC5), (0x00BC9, 0x00BC9), (0x00BCE, 0x00BCF), (0x00BD1, 0x00BD6),
    (0x00BD8, 0x00BE5), (0x00BFB, 0x00BFF), (0x00C0D, 0x00C0D), (0x00C11, 0x00C11),
    (0x00C29, 0x00C29), (0x00C3A, 0x00C3B), (0x00C45, 0x00C45), (0x00C49, 0x00C49),
    (0x00C4E, 0x00C54), (0x00C57, 0x00C57), (0x00C5B, 0x00C5C), (0x00C5E, 0x00C5F),
    (0x00C64, 0x00C65), (0x00C70, 0x00C76), (0x00C8D, 0x00C8D), (0x00C91, 0x00C91),
    (0x00CA9, 0x00CA9), (0x00CB4, 0x00CB4), (0x00CBA, 0x00CBB), (0x00CC5, 0x00CC5),
    (0x00CC9, 0x00CC9), (0x00CCE, 0x00CD4), (0x00CD7, 0x00CDC), (0x00CDF, 0x00CDF),
    (0x00CE4, 0x00CE5), (0x00CF0, 0x00CF0), (0x00CF4, 0x00CFF), (0x00D0D, 0x00D0D),
    (0x00D11, 0x00D11), (0x00D45, 0x00D45), (0x00D49, 0x00D49), (0x00D50, 0x00D53),
    (0x00D64, 0x00D65), (0x00D80, 0x00D80), (0x00D84, 0x00D84), (0x00D97, 0x00D99),
    (0x00DB2, 0x00DB2), (0x00DBC, 0x00DBC), (0x00DBE, 0x00DBF), (0x00DC7, 0x00DC9),
    (0x00DCB, 0x00DCE), (0x00DD5, 0x00DD5), (0x00DD7, 0x00DD7), (0x00DE0, 0x00DE5),
    (0x00DF0, 0x00DF1), (0x00DF5, 0x00E00), (0x00E3B, 0x00E3E), (0x00E5C, 0x00E80),
    (0x00E83, 0x00E83), (0x00E85, 0x00E85), (0x00E8B, 0x00E8B), (0x00EA4, 0x00EA4),
    (0x00EA6, 0x00EA6), (0x00EBE, 0x00EBF), (0x00EC5, 0x00EC5), (0x00EC7, 0x00EC7),
    (0x00ECF, 0x00ECF), (0x00EDA, 0x00EDB), (0x00EE0, 0x00EFF), (0x00F48, 0x00F48),
    (0x00F6D, 0x00F70), (0x00F98, 0x00F98), (0x00FBD, 0x00FBD), (0x00FCD, 0x00FCD),
    (0x00FDB, 0x00FFF), (0x010C6, 0x010C6), (0x010C8, 0x010CC), (0x010CE, 0x010CF),
    (0x01249, 0x01249), (0x0124E, 0x0124F), (0x01257, 0x01257), (0x01259, 0x01259),
    (0x0125E, 0x0125F), (0x01289, 0x01289), (0x0128E, 0x0128F), (0x012B1, 0x012B1),
    (0x012B6, 0x012B7), (0x012BF, 0x012BF), (0x012C1, 0x012C1), (0x012C6, 0x012C7),
    (0x012D7, 0x012D7), (0x01311, 0x01311), (0x01316, 0x01317), (0x0135B, 0x0135C),
    (0x0137D, 0x0137F), (0x0139A, 0x0139F), (0x013F6, 0x013F7), (0x013FE, 0x013FF),
    (0x01680, 0x01680), (0x0169D, 0x0169F), (0x016F9, 0x016FF), (0x01716, 0x0171E),
    (0x01737, 0x0173F), (0x01754, 0x0175F), (0x0176D, 0x0176D), (0x01771, 0x01771),
    (0x01774, 0x0177F), (0x017DE, 0x017DF), (0x017EA, 0x017EF), (0x017FA, 0x017FF),
    (0x0180E, 0x0180E), (0x0181A, 0x0181F), (0x01879, 0x0187F), (0x018AB, 0x018AF),
    (0x018F6, 0x018FF), (0x0191F, 0x0191F), (0x0192C, 0x0192F), (0x0193C, 0x0193F),
    (0x01941, 0x01943), (0x0196E, 0x0196F), (0x01975, 0x0197F), (0x019AC, 0x019AF),
    (0x019CA, 0x019CF), (0x019DB, 0x019DD), (0x01A1C, 0x01A1D), (0x01A5F, 0x01A5F),
    (0x01A7D, 0x01A7E), (0x01A8A, 0x01A8F), (0x01A9A, 0x01A9F), (0x01AAE, 0x01AAF),
    (0x01ACF, 0x01AFF), (0x01B4D, 0x01B4F), (0x01B7F, 0x01B7F), (0x01BF4, 0x01BFB),
    (0x01C38, 0x01C3A), (0x01C4A, 0x01C4C), (0x01C89, 0x01C8F), (0x01CBB, 0x01CBC),
    (0x01CC8, 0x01CCF), (0x01CFB, 0x01CFF), (0x01F16, 0x01F17), (0x01F1E, 0x01F1F),
    (0x01F46, 0x01F47), (0x01F4E, 0x01F4F), (0x01F58, 0x01F58), (0x01F5A, 0x01F5A),
    (0x01F5C, 0x01F5C), (0x01F5E, 0x01F5E), (0x01F7E, 0x01F7F), (0x01FB5, 0x01FB5),
    (0x01FC5, 0x01FC5), (0x01FD4, 0x01FD5), (0x01FDC, 0x01FDC), (0x01FF0, 0x01FF1),
    (0x01FF5, 0x01FF5), (0x01FFF, 0x0200F), (0x02028, 0x0202F), (0x0205F, 0x0206F),
    (0x02072, 0x02073), (0x0208F, 0x0208F), (0x0209D, 0x0209F), (0x020C1, 0x020CF),
    (0x020F1, 0x020FF), (0x0218C, 0x0218F), (0x02427, 0x0243F), (0x0244B, 0x0245F),
    (0x02B74, 0x02B75), (0x02B96, 0x02B96), (0x02CF4, 0x02CF8), (0x02D26, 0x02D26),
    (0x02D28, 0x02D2C), (0x02D2E, 0x02D2F), (0x02D68, 0x02D6E), (0x02D71, 0x02D7E),
    (0x02D97, 0x02D9F), (0x02DA7, 0x02DA7), (0x02DAF, 0x02DAF), (0x02DB7, 0x02DB7),
    (0x02DBF, 0x02DBF), (0x02DC7, 0x02DC7), (0x02DCF, 0x02DCF), (0x02DD7, 0x02DD7),
    (0x02DDF, 0x02DDF), (0x02E5E, 0x02E7F), (0x02E9A, 0x02E9A), (0x02EF4, 0x02EFF),
    (0x02FD6, 0x02FEF), (0x02FFC, 0x03000), (0x03040, 0x03040), (0x03097, 0x03098),
    (0x03100, 0x03104), (0x03130, 0x03130), (0x0318F, 0x0318F), (0x031E4, 0x031EF),
    (0x0321F, 0x0321F), (0x0A48D, 0x0A48F), (0x0A4C7, 0x0A4CF), (0x0A62C, 0x0A63F),
    (0x0A6F8, 0x0A6FF), (0x0A7CB, 0x0A7CF), (0x0A7D2, 0x0A7D2), (0x0A7D4, 0x0A7D4),
    (0x0A7DA, 0x0A7F1), (0x0A82D, 0x0A82F), (0x0A83A, 0x0A83F), (0x0A878, 0x0A87F),
    (0x0A8C6, 0x0A8CD), (0x0A8DA, 0x0A8DF), (0x0A954, 0x0A95E), (0x0A97D, 0x0A97F),
    (0x0A9CE, 0x0A9CE), (0x0A9DA, 0x0A9DD), (0x0A9FF, 0x0A9FF), (0x0AA37, 0x0AA3F),
    (0x0AA4E, 0x0AA4F), (0x0AA5A, 0x0AA5B), (0x0AAC3, 0x0AADA), (0x0AAF7, 0x0AB00),
    (0x0AB07, 0x0AB08), (0x0AB0F, 0x0AB10), (0x0AB17, 0x0AB1F), (0x0AB27, 0x0AB27),
    (0x0AB2F, 0x0AB2F), (0x0AB6C, 0x0AB6F), (0x0ABEE, 0x0ABEF), (0x0ABFA, 0x0ABFF),
    (0x0D7A4, 0x0D7AF), (0x0D7C7, 0x0D7CA), (0x0D7FC, 0x0F8FF), (0x0FA6E, 0x0FA6F),
    (0x0FADA, 0x0FAFF), (0x0FB07, 0x0FB12), (0x0FB18, 0x0FB1C), (0x0FB37, 0x0FB37),
    (0x0FB3D, 0x0FB3D), (0x0FB3F, 0x0FB3F), (0x0FB42, 0x0FB42), (0x0FB45, 0x0FB45),
    (0x0FBC3, 0x0FBD2), (0x0FD90, 0x0FD91), (0x0FDC8, 0x0FDCE), (0x0FDD0, 0x0FDEF),
    (0x0FE1A, 0x0FE1F), (0x0FE53, 0x0FE53), (0x0FE67, 0x0FE67), (0x0FE6C, 0x0FE6F),
    (0x0FE75, 0x0FE75), (0x0FEFD, 0x0FF00), (0x0FFBF, 0x0FFC1), (0x0FFC8, 0x0FFC9),
    (0x0FFD0, 0x0FFD1), (0x0FFD8, 0x0FFD9), (0x0FFDD, 0x0FFDF), (0x0FFE7, 0x0FFE7),
    (0x0FFEF, 0x0FFFB), (0x0FFFE, 0x0FFFF), (0x1000C, 0x1000C), (0x10027, 0x10027),
    (0x1003B, 0x1003B), (0x1003E, 0x1003E), (0x1004E, 0x1004F), (0x1005E, 0x1007F),
    (0x100FB, 0x100FF), (0x10103, 0x10106), (0x10134, 0x10136), (0x1018F, 0x1018F),
    (0x1019D, 0x1019F), (0x101A1, 0x101CF), (0x101FE, 0x1027F), (0x1029D, 0x1029F),
    (0x102D1, 0x102DF), (0x102FC, 0x102FF), (0x10324, 0x1032C), (0x1034B, 0x1034F),
    (0x1037B, 0x1037F), (0x1039E, 0x1039E), (0x103C4, 0x103C7), (0x103D6, 0x103FF),
    (0x1049E, 0x1049F), (0x104AA, 0x104AF), (0x104D4, 0x104D7), (0x104FC, 0x104FF),
    (0x10528, 0x1052F), (0x10564, 0x1056E), (0x1057B, 0x1057B), (0x1058B, 0x1058B),
    (0x10593, 0x10593), (0x10596, 0x10596), (0x105A2, 0x105A2), (0x105B2, 0x105B2),
    (0x105BA, 0x105BA), (0x105BD, 0x105FF), (0x10737, 0x1073F), (0x10756, 0x1075F),
    (0x10768, 0x1077F), (0x10786, 0x10786), (0x107B1, 0x107B1), (0x107BB, 0x107FF),
    (0x10806, 0x10807), (0x10809, 0x10809), (0x10836, 0x10836), (0x10839, 0x1083B),
    (0x1083D, 0x1083E), (0x10856, 0x10856), (0x1089F, 0x108A6), (0x108B0, 0x108DF),
    (0x108F3, 0x108F3), (0x108F6, 0x108FA), (0x1091C, 0x1091E), (0x1093A, 0x1093E),
    (0x10940, 0x1097F), (0x109B8, 0x109BB), (0x109D0, 0x109D1), (0x10A04, 0x10A04),
    (0x10A07, 0x10A0B), (0x10A14, 0x10A14), (0x10A18, 0x10A18), (0x10A36, 0x10A37),
    (0x10A3B, 0x10A3E), (0x10A49, 0x10A4F), (0x10A59, 0x10A5F), (0x10AA0, 0x10ABF),
    (0x10AE7, 0x10AEA), (0x10AF7, 0x10AFF), (0x10B36, 0x10B38), (0x10B56, 0x10B57),
    (0x10B73, 0x10B77), (0x10B92, 0x10B98), (0x10B9D, 0x10BA8), (0x10BB0, 0x10BFF),
    (0x10C49, 0x10C7F), (0x10CB3, 0x10CBF), (0x10CF3, 0x10CF9), (0x10D28, 0x10D2F),
    (0x10D3A, 0x10E5F), (0x10E7F, 0x10E7F), (0x10EAA, 0x10EAA), (0x10EAE, 0x10EAF),
    (0x10EB2, 0x10EFC), (0x10F28, 0x10F2F), (0x10F5A, 0x10F6F), (0x10F8A, 0x10FAF),
    (0x10FCC, 0x10FDF), (0x10FF7, 0x10FFF), (0x1104E, 0x11051), (0x11076, 0x1107E),
    (0x110BD, 0x110BD), (0x110C3, 0x110CF), (0x110E9, 0x110EF), (0x110FA, 0x110FF),
    (0x11135, 0x11135), (0x11148, 0x1114F), (0x11177, 0x1117F), (0x111E0, 0x111E0),
    (0x111F5, 0x111FF), (0x11212, 0x11212), (0x11242, 0x1127F), (0x11287, 0x11287),
    (0x11289, 0x11289), (0x1128E, 0x1128E), (0x1129E, 0x1129E), (0x112AA, 0x112AF),
    (0x112EB, 0x112EF), (0x112FA, 0x112FF), (0x11304, 0x11304), (0x1130D, 0x1130E),
    (0x11311, 0x11312), (0x11329, 0x11329), (0x11331, 0x11331), (0x11334, 0x11334),
    (0x1133A, 0x1133A), (0x11345, 0x11346), (0x11349, 0x1134A), (0x1134E, 0x1134F),
    (0x11351, 0x11356), (0x11358, 0x1135C), (0x11364, 0x11365), (0x1136D, 0x1136F),
    (0x11375, 0x113FF), (0x1145C, 0x1145C), (0x11462, 0x1147F), (0x114C8, 0x114CF),
    (0x114DA, 0x1157F), (0x115B6, 0x115B7), (0x115DE, 0x115FF), (0x11645, 0x1164F),
    (0x1165A, 0x1165F), (0x1166D, 0x1167F), (0x116BA, 0x116BF), (0x116CA, 0x116FF),
    (0x1171B, 0x1171C), (0x1172C, 0x1172F), (0x11747, 0x117FF), (0x1183C, 0x1189F),
    (0x118F3, 0x118FE), (0x11907, 0x11908), (0x1190A, 0x1190B), (0x11914, 0x11914),
    (0x11917, 0x11917), (0x11936, 0x11936), (0x11939, 0x1193A), (0x11947, 0x1194F),
    (0x1195A, 0x1199F), (0x119A8, 0x119A9), (0x119D8, 0x119D9), (0x119E5, 0x119FF),
    (0x11A48, 0x11A4F), (0x11AA3, 0x11AAF), (0x11AF9, 0x11AFF), (0x11B0A, 0x11BFF),
    (0x11C09, 0x11C09), (0x11C37, 0x11C37), (0x11C46, 0x11C4F), (0x11C6D, 0x11C6F),
    (0x11C90, 0x11C91), (0x11CA8, 0x11CA8), (0x11CB7, 0x11CFF), (0x11D07, 0x11D07),
    (0x11D0A, 0x11D0A), (0x11D37, 0x11D39), (0x11D3B, 0x11D3B), (0x11D3E, 0x11D3E),
    (0x11D48, 0x11D4F), (0x11D5A, 0x11D5F), (0x11D66, 0x11D66), (0x11D69, 0x11D69),
    (0x11D8F, 0x11D8F), (0x11D92, 0x11D92), (0x11D99, 0x11D9F), (0x11DAA, 0x11EDF),
    (0x11EF9, 0x11EFF), (0x11F11, 0x11F11), (0x11F3B, 0x11F3D), (0x11F5A, 0x11FAF),
    (0x11FB1, 0x11FBF), (0x11FF2, 0x11FFE), (0x1239A, 0x123FF), (0x1246F, 0x1246F),
    (0x12475, 0x1247F), (0x12544, 0x12F8F), (0x12FF3, 0x12FFF), (0x13430, 0x1343F),
    (0x13456, 0x143FF), (0x14647, 0x167FF), (0x16A39, 0x16A3F), (0x16A5F, 0x16A5F),
    (0x16A6A, 0x16A6D), (0x16ABF, 0x16ABF), (0x16ACA, 0x16ACF), (0x16AEE, 0x16AEF),
    (0x16AF6, 0x16AFF), (0x16B46, 0x16B4F), (0x16B5A, 0x16B5A), (0x16B62, 0x16B62),
    (0x16B78, 0x16B7C), (0x16B90, 0x16E3F), (0x16E9B, 0x16EFF), (0x16F4B, 0x16F4E),
    (0x16F88, 0x16F8E), (0x16FA0, 0x16FDF), (0x16FE5, 0x16FEF), (0x16FF2, 0x16FFF),
    (0x187F8, 0x187FF), (0x18CD6, 0x18CFF), (0x18D09, 0x1AFEF), (0x1AFF4, 0x1AFF4),
    (0x1AFFC, 0x1AFFC), (0x1AFFF, 0x1AFFF), (0x1B123, 0x1B131), (0x1B133, 0x1B14F),
    (0x1B153, 0x1B154), (0x1B156, 0x1B163), (0x1B168, 0x1B16F), (0x1B2FC, 0x1BBFF),
    (0x1BC6B, 0x1BC6F), (0x1BC7D, 0x1BC7F), (0x1BC89, 0x1BC8F), (0x1BC9A, 0x1BC9B),
    (0x1BCA0, 0x1CEFF), (0x1CF2E, 0x1CF2F), (0x1CF47, 0x1CF4F), (0x1CFC4, 0x1CFFF),
    (0x1D0F6, 0x1D0FF), (0x1D127, 0x1D128), (0x1D173, 0x1D17A), (0x1D1EB, 0x1D1FF),
    (0x1D246, 0x1D2BF), (0x1D2D4, 0x1D2DF), (0x1D2F4, 0x1D2FF), (0x1D357, 0x1D35F),
    (0x1D379, 0x1D3FF), (0x1D455, 0x1D455), (0x1D49D, 0x1D49D), (0x1D4A0, 0x1D4A1),
    (0x1D4A3, 0x1D4A4), (0x1D4A7, 0x1D4A8), (0x1D4AD, 0x1D4AD), (0x1D4BA, 0x1D4BA),
    (0x1D4BC, 0x1D4BC), (0x1D4C4, 0x1D4C4), (0x1D506, 0x1D506), (0x1D50B, 0x1D50C),
    (0x1D515, 0x1D515), (0x1D51D, 0x1D51D), (0x1D53A, 0x1D53A), (0x1D53F, 0x1D53F),
    (0x1D545, 0x1D545), (0x1D547, 0x1D549), (0x1D551, 0x1D551), (0x1D6A6, 0x1D6A7),
    (0x1D7CC, 0x1D7CD), (0x1DA8C, 0x1DA9A), (0x1DAA0, 0x1DAA0), (0x1DAB0, 0x1DEFF),
    (0x1DF1F, 0x1DF24), (0x1DF2B, 0x1DFFF), (0x1E007, 0x1E007), (0x1E019, 0x1E01A),
    (0x1E022, 0x1E022), (0x1E025, 0x1E025), (0x1E02B, 0x1E02F), (0x1E06E, 0x1E08E),
    (0x1E090, 0x1E0FF), (0x1E12D, 0x1E12F), (0x1E13E, 0x1E13F), (0x1E14A, 0x1E14D),
    (0x1E150, 0x1E28F), (0x1E2AF, 0x1E2BF), (0x1E2FA, 0x1E2FE), (0x1E300, 0x1E4CF),
    (0x1E4FA, 0x1E7DF), (0x1E7E7, 0x1E7E7), (0x1E7EC, 0x1E7EC), (0x1E7EF, 0x1E7EF),
    (0x1E7FF, 0x1E7FF), (0x1E8C5, 0x1E8C6), (0x1E8D7, 0x1E8FF), (0x1E94C, 0x1E94F),
    (0x1E95A, 0x1E95D), (0x1E960, 0x1EC70), (0x1ECB5, 0x1ED00), (0x1ED3E, 0x1EDFF),
    (0x1EE04, 0x1EE04), (0x1EE20, 0x1EE20), (0x1EE23, 0x1EE23), (0x1EE25, 0x1EE26),
    (0x1EE28, 0x1EE28), (0x1EE33, 0x1EE33), (0x1EE38, 0x1EE38), (0x1EE3A, 0x1EE3A),
    (0x1EE3C, 0x1EE41), (0x1EE43, 0x1EE46), (0x1EE48, 0x1EE48), (0x1EE4A, 0x1EE4A),
    (0x1EE4C, 0x1EE4C), (0x1EE50, 0x1EE50), (0x1EE53, 0x1EE53), (0x1EE55, 0x1EE56),
    (0x1EE58, 0x1EE58), (0x1EE5A, 0x1EE5A), (0x1EE5C, 0x1EE5C), (0x1EE5E, 0x1EE5E),
    (0x1EE60, 0x1EE60), (0x1EE63, 0x1EE63), (0x1EE65, 0x1EE66), (0x1EE6B, 0x1EE6B),
    (0x1EE73, 0x1EE73), (0x1EE78, 0x1EE78), (0x1EE7D, 0x1EE7D), (0x1EE7F, 0x1EE7F),
    (0x1EE8A, 0x1EE8A), (0x1EE9C, 0x1EEA0), (0x1EEA4, 0x1EEA4), (0x1EEAA, 0x1EEAA),
    (0x1EEBC, 0x1EEEF), (0x1EEF2, 0x1EFFF), (0x1F02C, 0x1F02F), (0x1F094, 0x1F09F),
    (0x1F0AF, 0x1F0B0), (0x1F0C0, 0x1F0C0), (0x1F0D0, 0x1F0D0), (0x1F0F6, 0x1F0FF),
    (0x1F1AE, 0x1F1E5), (0x1F203, 0x1F20F), (0x1F23C, 0x1F23F), (0x1F249, 0x1F24F),
    (0x1F252, 0x1F25F), (0x1F266, 0x1F2FF), (0x1F6D8, 0x1F6DB), (0x1F6ED, 0x1F6EF),
    (0x1F6FD, 0x1F6FF), (0x1F777, 0x1F77A), (0x1F7DA, 0x1F7DF), (0x1F7EC, 0x1F7EF),
    (0x1F7F1, 0x1F7FF), (0x1F80C, 0x1F80F), (0x1F848, 0x1F84F), (0x1F85A, 0x1F85F),
    (0x1F888, 0x1F88F), (0x1F8AE, 0x1F8AF), (0x1F8B2, 0x1F8FF), (0x1FA54, 0x1FA5F),
    (0x1FA6E, 0x1FA6F), (0x1FA7D, 0x1FA7F), (0x1FA89, 0x1FA8F), (0x1FABE, 0x1FABE),
    (0x1FAC6, 0x1FACD), (0x1FADC, 0x1FADF), (0x1FAE9, 0x1FAEF), (0x1FAF9, 0x1FAFF),
    (0x1FB93, 0x1FB93), (0x1FBCB, 0x1FBEF), (0x1FBFA, 0x1FFFF), (0x2A6E0, 0x2A6FF),
    (0x2B73A, 0x2B73F), (0x2B81E, 0x2B81F), (0x2CEA2, 0x2CEAF), (0x2EBE1, 0x2F7FF),
    (0x2FA1E, 0x2FFFF), (0x3134B, 0x3134F), (0x323B0, 0xE00FF), (0xE01F0, 0x10FFFF),
];

#[cfg(test)]
mod tests {
    use super::*;
    use crate::pyjson::{PyDict, PyInt, PyValue};

    /// `contracts/golden/unicode_tables.txt` 解析結果（由 `gen_unicode_tables.py` 以 CPython 3.12.3 產生）。
    struct GoldenTables {
        space: Vec<(u32, u32)>,
        decimal_zero: Vec<u32>,
        digit: Vec<(u32, u32)>,
        nonprint: Vec<(u32, u32)>,
    }

    fn parse_token(tok: &str) -> (u32, u32) {
        let hex = |s: &str| {
            u32::from_str_radix(s.trim_start_matches("0x"), 16).expect("golden 碼點必須為十六進位")
        };
        match tok.split_once('-') {
            Some((lo, hi)) => (hex(lo), hex(hi)),
            None => (hex(tok), hex(tok)),
        }
    }

    fn golden_tables() -> GoldenTables {
        const TEXT: &str = include_str!("../../../contracts/golden/unicode_tables.txt");
        let mut out = GoldenTables {
            space: Vec::new(),
            decimal_zero: Vec::new(),
            digit: Vec::new(),
            nonprint: Vec::new(),
        };
        for line in TEXT
            .lines()
            .filter(|l| !l.starts_with('#') && !l.is_empty())
        {
            let mut parts = line.split(' ');
            let key = parts.next().expect("golden 行必須有鍵");
            let tokens: Vec<(u32, u32)> = parts.map(parse_token).collect();
            match key {
                "space" => out.space = tokens,
                "decimal_zero" => out.decimal_zero = tokens.iter().map(|&(z, _)| z).collect(),
                "digit" => out.digit = tokens,
                "nonprint" => out.nonprint = tokens,
                other => panic!("未知 golden 表鍵：{other}"),
            }
        }
        assert!(
            !out.space.is_empty() && !out.decimal_zero.is_empty(),
            "golden 表缺漏"
        );
        assert!(
            !out.digit.is_empty() && !out.nonprint.is_empty(),
            "golden 表缺漏"
        );
        out
    }

    /// 內嵌表與 CPython 產生的 golden 逐項相等（表漂移時此測試即失敗）。
    #[test]
    fn embedded_tables_equal_cpython_golden() {
        let t = golden_tables();
        assert_eq!(DECIMAL_ZEROS, t.decimal_zero.as_slice());
        assert_eq!(DIGIT_RANGES, t.digit.as_slice());
        assert_eq!(NONPRINT_RANGES, t.nonprint.as_slice());
    }

    /// 全部 Unicode 純量值（排除代理區，Rust `char` 不含）逐一比對 `isspace`、`isdigit`、`isprintable`、`decimal`。
    #[test]
    fn predicates_match_cpython_for_every_codepoint() {
        let t = golden_tables();
        for cp in 0..=0x10_ffffu32 {
            let Some(c) = char::from_u32(cp) else {
                continue;
            };
            assert_eq!(
                py_is_space(c),
                in_ranges(&t.space, cp),
                "isspace U+{cp:04X}"
            );
            assert_eq!(
                py_isdigit(&c.to_string()),
                in_ranges(&t.digit, cp),
                "isdigit U+{cp:04X}"
            );
            assert_eq!(
                py_isprintable(c),
                !in_ranges(&t.nonprint, cp),
                "isprintable U+{cp:04X}"
            );
            let idx = t.decimal_zero.partition_point(|&z| z <= cp);
            let expected = (idx > 0 && cp - t.decimal_zero[idx - 1] <= 9)
                .then(|| cp - t.decimal_zero[idx - 1]);
            assert_eq!(decimal_value(c), expected, "decimal U+{cp:04X}");
        }
    }

    #[test]
    fn strip_and_bytes_strip_recorded_cases() {
        assert_eq!(py_strip("\u{3000}x\u{2028}"), "x");
        assert_eq!(py_strip("\x1c\x1dtest\x1f"), "test");
        assert_eq!(py_bytes_strip(b" \t\x1cx\x0b\x0c\n\r"), b"\x1cx");
        assert_eq!(py_bytes_strip(b"\x1c\x1d"), b"\x1c\x1d");
        assert_eq!(py_bytes_strip(b"   "), b"");
        assert_eq!(py_bytes_strip(b""), b"");
    }

    #[test]
    fn isdigit_recorded_cases() {
        assert!(py_isdigit("123"));
        assert!(py_isdigit("\u{b2}"));
        assert!(py_isdigit("٤٢"));
        assert!(!py_isdigit("1\u{bd}"));
        assert!(!py_isdigit("1a"));
        assert!(!py_isdigit(""));
    }

    #[test]
    fn splitlines_and_universal_newlines_recorded_cases() {
        assert_eq!(universal_newlines("a\r\nb\rc\nd"), "a\nb\nc\nd");
        assert_eq!(splitlines("a\u{2028}b\u{2029}c\r\n"), vec!["a", "b", "c"]);
        assert_eq!(py_len_chars("a\u{1f600}\u{e9}"), 3);
    }

    #[test]
    fn char_and_byte_index_conversion() {
        // a（1 B）、😀（4 B）、é（2 B）：共 7 B、3 個字元。
        let s = "a\u{1f600}\u{e9}";
        assert_eq!(char_index_to_byte(s, 0), 0);
        assert_eq!(char_index_to_byte(s, 2), 5);
        assert_eq!(char_index_to_byte(s, 3), 7);
        assert_eq!(char_index_to_byte(s, 99), 7);
        assert_eq!(byte_index_to_char(s, 0), 0);
        assert_eq!(byte_index_to_char(s, 5), 2);
        assert_eq!(byte_index_to_char(s, 7), 3);
        // 位移落在字元中間（呼叫端不應如此使用；字元邊界才有定義）：計入該字元，即向後取整至字元數。
        assert_eq!(byte_index_to_char(s, 3), 2);
    }

    #[test]
    fn py_truthy_matches_python_bool() {
        assert!(!py_truthy(&PyValue::Null));
        assert!(py_truthy(&PyValue::Bool(true)));
        assert!(!py_truthy(&PyValue::Int(PyInt::Small(0))));
        assert!(py_truthy(&PyValue::Int(PyInt::Small(-1))));
        assert!(!py_truthy(&PyValue::Float(0.0)));
        assert!(!py_truthy(&PyValue::Float(-0.0)));
        assert!(py_truthy(&PyValue::Float(f64::NAN)));
        assert!(!py_truthy(&PyValue::Str(String::new())));
        assert!(py_truthy(&PyValue::Str("0".into())));
        assert!(!py_truthy(&PyValue::List(Vec::new())));
        assert!(py_truthy(&PyValue::List(vec![PyValue::Null])));
        assert!(!py_truthy(&PyValue::Dict(PyDict::new())));
    }

    #[test]
    fn sha256_hex_known_vectors() {
        assert_eq!(
            sha256_hex(b""),
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        );
        assert_eq!(
            sha256_hex(b"abc"),
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        );
    }

    /// `int()` 位數上限 4300（CPython 3.12.3 實測）：含前導零、Unicode 數字計入；底線與正負號不計入。
    #[test]
    fn int_digit_limit_matches_cpython() {
        assert!(matches!(
            py_int_parse(&"9".repeat(4300)),
            Some(PyInt::Big(_))
        ));
        assert_eq!(py_int_parse(&"9".repeat(4301)), None);
        assert_eq!(py_int_parse(&format!("+{}", "9".repeat(4301))), None);
        assert_eq!(py_int_parse(&"0".repeat(4301)), None);
        assert_eq!(py_int_parse(&"0".repeat(4300)), Some(PyInt::Small(0)));
        assert_eq!(py_int_parse(&"\u{664}".repeat(4301)), None);
        assert!(py_int_parse(&"\u{664}".repeat(4300)).is_some());
        assert!(py_int_parse(&format!("{}1", "1_".repeat(4299))).is_some());
        assert!(py_int_parse(&format!("-{}", "9".repeat(4300))).is_some());
        assert!(py_int_parse(&format!(" {} ", "9".repeat(4300))).is_some());
    }

    /// CPython `repr(str)` 對 Zs／Zl／Zp 與 Cf 的跳脫形式（實測）。
    #[test]
    fn repr_str_separator_escapes_recorded_cases() {
        assert_eq!(py_repr_str("\u{2028}"), "'\\u2028'");
        assert_eq!(py_repr_str("\u{2029}"), "'\\u2029'");
        assert_eq!(py_repr_str("\u{3000}"), "'\\u3000'");
        assert_eq!(py_repr_str("\u{a0}"), "'\\xa0'");
        assert_eq!(py_repr_str("\u{85}"), "'\\x85'");
        assert_eq!(py_repr_str("\u{ad}"), "'\\xad'");
        assert_eq!(py_repr_str("é"), "'é'");
    }
}
