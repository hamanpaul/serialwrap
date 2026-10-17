//! Python `json` 相容層（P3-T3）。
//!
//! 提供與 CPython 3.12 `json` 模組逐字對齊（已登記的非契約差異除外）的值表示、解析器與序列化器。
//!
//! 已登記非契約差異（arch §3.3、spec §11.3）：
//! - `RPC-H-03`：孤立 surrogate（lone surrogate，U+D800..=U+DFFF）轉譯為 U+FFFD 替換字元。
//! - `JSON_MAX_DEPTH`：2900（顯式堆疊巢狀深度上限，超過時回傳 `JsonError::Recursion`）。
//! - 整數字面量長度：CPython 3.12 預設限制 4300 位（CVE-2020-10735 `ValueError`），此處以 `PyInt::Big` 接受大於 i128 的整數。

use indexmap::IndexMap;
use std::fmt::Write as _;

/// Python 值（`json.loads` 的產出、`json.dumps` 的輸入）。
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

impl PyValue {
    /// 依 Python 語意判定真值（`bool(val)`）。
    pub(crate) fn is_truthy(&self) -> bool {
        match self {
            PyValue::Null => false,
            PyValue::Bool(b) => *b,
            PyValue::Int(PyInt::Small(n)) => *n != 0,
            PyValue::Int(PyInt::Big(s)) => !s.is_empty() && s != "0",
            PyValue::Float(f) => *f != 0.0,
            PyValue::Str(s) => !s.is_empty(),
            PyValue::List(l) => !l.is_empty(),
            PyValue::Dict(d) => !d.0.is_empty(),
        }
    }
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
    /// 建立空的 `PyDict`。
    pub fn new() -> Self {
        PyDict(IndexMap::new())
    }

    /// 從鍵值對向量建立字典；重複 key 保留首次位置、值取後者。
    pub fn from_pairs(v: Vec<(String, PyValue)>) -> Self {
        let mut map = IndexMap::with_capacity(v.len());
        for (k, val) in v {
            if let Some(slot) = map.get_mut(&k) {
                *slot = val;
            } else {
                map.insert(k, val);
            }
        }
        PyDict(map)
    }

    /// 插入鍵值；已存在的 key 改值、位置不變。
    pub fn insert(&mut self, k: &str, v: PyValue) {
        if let Some(slot) = self.0.get_mut(k) {
            *slot = v;
        } else {
            self.0.insert(k.to_string(), v);
        }
    }

    /// 取得鍵對應的值。
    pub fn get(&self, k: &str) -> Option<&PyValue> {
        self.0.get(k)
    }

    /// 若鍵不存在則寫入預設值；已存在則不更動。
    pub fn setdefault(&mut self, k: &str, v: PyValue) {
        if !self.0.contains_key(k) {
            self.0.insert(k.to_string(), v);
        }
    }

    /// 以另一個字典更新當前字典；重複 key 保留既有位置。
    pub fn update(&mut self, other: &PyDict) {
        for (k, v) in &other.0 {
            if let Some(slot) = self.0.get_mut(k) {
                *slot = v.clone();
            } else {
                self.0.insert(k.clone(), v.clone());
            }
        }
    }

    /// `bool(d.get(k))`。
    pub fn get_truthy(&self, k: &str) -> bool {
        match self.0.get(k) {
            None => false,
            Some(v) => v.is_truthy(),
        }
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
    /// 是否允許非有限浮點數（P3 僅支援 true；false 於 Python 會拋 ValueError，此處斷言阻擋）。
    pub allow_nan: bool,
}

/// CLI stdout：`json.dumps(v, ensure_ascii=False, sort_keys=True, separators=(",", ":"))`。
pub const CLI_STDOUT: DumpOpts = DumpOpts {
    sep: Sep::Compact,
    ensure_ascii: false,
    sort_keys: true,
    allow_nan: true,
};
/// RPC 線路格式：Compact、ensure_ascii=false、sort_keys=false。
pub const RPC_WIRE: DumpOpts = DumpOpts {
    sep: Sep::Compact,
    ensure_ascii: false,
    sort_keys: false,
    allow_nan: true,
};
/// 早期錯誤回應：`json.dumps(v, ensure_ascii=False)`（預設分隔符）。
pub const RPC_EARLY: DumpOpts = DumpOpts {
    sep: Sep::Default,
    ensure_ascii: false,
    sort_keys: false,
    allow_nan: true,
};
/// 磁碟穩定格式（state.json／WAL）：Compact、ensure_ascii=false、sort_keys=true。
pub const DISK_STABLE: DumpOpts = DumpOpts {
    sep: Sep::Compact,
    ensure_ascii: false,
    sort_keys: true,
    allow_nan: true,
};

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
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(
            f,
            "{}: line {} column {} (char {})",
            self.msg, self.lineno, self.colno, self.pos
        )
    }
}

impl std::error::Error for JsonDecodeError {}

/// `loads` 的錯誤；`Recursion` 對應 Python `RecursionError`（不是 `ValueError`）。
#[derive(Debug, Clone, PartialEq)]
pub enum JsonError {
    Decode(JsonDecodeError),
    Recursion,
}

impl std::fmt::Display for JsonError {
    /// `Decode` 同 `JsonDecodeError`；`Recursion` 為 `"maximum recursion depth exceeded"`。
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            JsonError::Decode(e) => write!(f, "{}", e),
            JsonError::Recursion => write!(f, "maximum recursion depth exceeded"),
        }
    }
}

impl std::error::Error for JsonError {}

/// Rust 自訂的巢狀深度上限（非契約，見 plan §0.7）；解析器以顯式堆疊實作，不靠遞迴。
pub const JSON_MAX_DEPTH: usize = 2900;

/// Python `repr(float)`；唯一實作位置，`pyfmt::py_repr_float` 單向轉呼叫。
pub fn repr_float(x: f64) -> String {
    if x.is_nan() {
        return "nan".to_string();
    }
    if x.is_infinite() {
        return if x.is_sign_positive() {
            "inf".to_string()
        } else {
            "-inf".to_string()
        };
    }
    if x == 0.0 {
        return if x.is_sign_negative() {
            "-0.0".to_string()
        } else {
            "0.0".to_string()
        };
    }

    let is_negative = x.is_sign_negative();
    let abs_x = x.abs();

    // 先以 Rust shortest 表示（half-up 平手處理）
    let s_shortest = format!("{:e}", abs_x);
    // 檢查同位數的 exact 格式化（ties-to-even）能否 round-trip；若能則採用，對齊 CPython dtoa mode 0
    let s = if let Some((m, _)) = s_shortest.split_once('e') {
        let prec = m.split_once('.').map(|(_, r)| r.len()).unwrap_or(0);
        let s_exact = format!("{:.prec$e}", abs_x, prec = prec);
        if let Ok(parsed) = s_exact.parse::<f64>() {
            if parsed.to_bits() == abs_x.to_bits() {
                s_exact
            } else {
                s_shortest
            }
        } else {
            s_shortest
        }
    } else {
        s_shortest
    };

    let (mantissa, exp_str) = s.split_once('e').expect("float format should contain 'e'");
    let exp: i32 = exp_str.parse().expect("exponent should parse as integer");

    let mut out = String::new();
    if is_negative {
        out.push('-');
    }

    if (-4..16).contains(&exp) {
        let (d0, rest) = if let Some((first, remainder)) = mantissa.split_once('.') {
            (first, remainder)
        } else {
            (mantissa, "")
        };
        let mut all_digits = String::with_capacity(d0.len() + rest.len());
        all_digits.push_str(d0);
        all_digits.push_str(rest);
        let total_digits = all_digits.len();

        if exp >= 0 {
            let int_digits = (exp + 1) as usize;
            if total_digits <= int_digits {
                out.push_str(&all_digits);
                for _ in 0..(int_digits - total_digits) {
                    out.push('0');
                }
                out.push_str(".0");
            } else {
                out.push_str(&all_digits[..int_digits]);
                out.push('.');
                out.push_str(&all_digits[int_digits..]);
            }
        } else {
            out.push_str("0.");
            let leading_zeros = (-exp - 1) as usize;
            for _ in 0..leading_zeros {
                out.push('0');
            }
            out.push_str(&all_digits);
        }
    } else {
        out.push_str(mantissa);
        let sign = if exp >= 0 { '+' } else { '-' };
        let abs_exp = exp.unsigned_abs();
        write!(out, "e{}{:02}", sign, abs_exp).unwrap();
    }

    out
}

/// 將字串以 JSON 跳脫格式寫入緩衝區。
fn dump_str(s: &str, ensure_ascii: bool, out: &mut String) {
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\x08' => out.push_str("\\b"),
            '\x0C' => out.push_str("\\f"),
            c if (c as u32) < 0x20 => {
                write!(out, "\\u{:04x}", c as u32).unwrap();
            }
            c if ensure_ascii => {
                if c.is_ascii() && c != '\x7f' {
                    out.push(c);
                } else {
                    let cp = c as u32;
                    if cp <= 0xFFFF {
                        write!(out, "\\u{:04x}", cp).unwrap();
                    } else {
                        let u = cp - 0x10000;
                        let high = 0xD800 + (u >> 10);
                        let low = 0xDC00 + (u & 0x3FF);
                        write!(out, "\\u{:04x}\\u{:04x}", high, low).unwrap();
                    }
                }
            }
            c => out.push(c),
        }
    }
    out.push('"');
}

enum DumpFrame<'a> {
    Array {
        items: &'a [PyValue],
        index: usize,
        level: usize,
    },
    Object {
        keys: Vec<&'a String>,
        dict: &'a PyDict,
        index: usize,
        level: usize,
    },
}

/// 迭代序列化的節點：一般值，或尚未展開的 dict。
///
/// `Dict` 變體讓根節點能以借用的 [`PyDict`] 開始序列化（見 [`dumps_dict`]），不必先包成 `PyValue::Dict` 再 clone。
#[derive(Clone, Copy)]
enum Node<'a> {
    Val(&'a PyValue),
    Dict(&'a PyDict),
}

/// `json.dumps`，不含尾端換行（顯式堆疊實作，不靠遞迴）。
pub fn dumps(v: &PyValue, o: &DumpOpts) -> String {
    dumps_node(Node::Val(v), o)
}

/// 同 [`dumps`]，但根節點為借用的 dict：輸出與 `dumps(&PyValue::Dict(d.clone()), o)` 逐位元組相同，
/// 且不複製整棵樹（大型 CLI 回應的 `print_json` 路徑使用）。
pub fn dumps_dict(d: &PyDict, o: &DumpOpts) -> String {
    dumps_node(Node::Dict(d), o)
}

fn dumps_node(root: Node<'_>, o: &DumpOpts) -> String {
    assert!(o.allow_nan, "P3 dumps only supports allow_nan=true");
    let mut out = String::new();
    let mut stack: Vec<DumpFrame<'_>> = Vec::new();
    let mut current: Option<(Node<'_>, usize)> = Some((root, 0));

    let colon_sep = match o.sep {
        Sep::Compact => ":",
        Sep::Default | Sep::Indent(_) => ": ",
    };

    loop {
        if let Some((node, level)) = current {
            match node {
                Node::Val(val) => match val {
                    PyValue::Null => {
                        out.push_str("null");
                        current = None;
                    }
                    PyValue::Bool(true) => {
                        out.push_str("true");
                        current = None;
                    }
                    PyValue::Bool(false) => {
                        out.push_str("false");
                        current = None;
                    }
                    PyValue::Int(PyInt::Small(n)) => {
                        write!(out, "{}", n).unwrap();
                        current = None;
                    }
                    PyValue::Int(PyInt::Big(s)) => {
                        out.push_str(s);
                        current = None;
                    }
                    PyValue::Float(f) => {
                        if f.is_nan() {
                            out.push_str("NaN");
                        } else if *f == f64::INFINITY {
                            out.push_str("Infinity");
                        } else if *f == f64::NEG_INFINITY {
                            out.push_str("-Infinity");
                        } else {
                            out.push_str(&repr_float(*f));
                        }
                        current = None;
                    }
                    PyValue::Str(s) => {
                        dump_str(s, o.ensure_ascii, &mut out);
                        current = None;
                    }
                    PyValue::List(items) => {
                        if items.is_empty() {
                            out.push_str("[]");
                            current = None;
                        } else {
                            out.push('[');
                            if let Sep::Indent(n) = o.sep {
                                out.push('\n');
                                out.push_str(&" ".repeat((level + 1) * n as usize));
                            }
                            stack.push(DumpFrame::Array {
                                items: items.as_slice(),
                                index: 0,
                                level,
                            });
                            current = Some((Node::Val(&items[0]), level + 1));
                        }
                    }
                    // 一般 dict 交給下一輪以 `Node::Dict` 處理，與根 dict 共用同一段邏輯。
                    PyValue::Dict(d) => current = Some((Node::Dict(d), level)),
                },
                Node::Dict(d) => {
                    if d.0.is_empty() {
                        out.push_str("{}");
                        current = None;
                    } else {
                        out.push('{');
                        let keys: Vec<&String> = if o.sort_keys {
                            let mut k: Vec<&String> = d.0.keys().collect();
                            k.sort();
                            k
                        } else {
                            d.0.keys().collect()
                        };

                        if let Sep::Indent(n) = o.sep {
                            out.push('\n');
                            out.push_str(&" ".repeat((level + 1) * n as usize));
                        }
                        dump_str(keys[0], o.ensure_ascii, &mut out);
                        out.push_str(colon_sep);
                        let first_val = &d.0[keys[0]];
                        stack.push(DumpFrame::Object {
                            keys,
                            dict: d,
                            index: 0,
                            level,
                        });
                        current = Some((Node::Val(first_val), level + 1));
                    }
                }
            }
        } else if let Some(top) = stack.last_mut() {
            match top {
                DumpFrame::Array {
                    items,
                    index,
                    level,
                } => {
                    *index += 1;
                    if *index < items.len() {
                        match o.sep {
                            Sep::Compact => out.push(','),
                            Sep::Default => out.push_str(", "),
                            Sep::Indent(n) => {
                                out.push(',');
                                out.push('\n');
                                out.push_str(&" ".repeat((*level + 1) * n as usize));
                            }
                        }
                        current = Some((Node::Val(&items[*index]), *level + 1));
                    } else {
                        if let Sep::Indent(n) = o.sep {
                            out.push('\n');
                            out.push_str(&" ".repeat(*level * n as usize));
                        }
                        out.push(']');
                        let _ = stack.pop();
                        current = None;
                    }
                }
                DumpFrame::Object {
                    keys,
                    dict,
                    index,
                    level,
                } => {
                    *index += 1;
                    if *index < keys.len() {
                        match o.sep {
                            Sep::Compact => out.push(','),
                            Sep::Default => out.push_str(", "),
                            Sep::Indent(n) => {
                                out.push(',');
                                out.push('\n');
                                out.push_str(&" ".repeat((*level + 1) * n as usize));
                            }
                        }
                        dump_str(keys[*index], o.ensure_ascii, &mut out);
                        out.push_str(colon_sep);
                        let val = &dict.0[keys[*index]];
                        current = Some((Node::Val(val), *level + 1));
                    } else {
                        if let Sep::Indent(n) = o.sep {
                            out.push('\n');
                            out.push_str(&" ".repeat(*level * n as usize));
                        }
                        out.push('}');
                        let _ = stack.pop();
                        current = None;
                    }
                }
            }
        } else {
            break;
        }
    }

    out
}

/// 依字元偏移量計算行列號（1-indexed）。
fn compute_line_col(s: &str, pos: usize) -> (usize, usize) {
    let mut lineno = 1;
    let mut colno = 1;
    let mut char_count = 0;
    for c in s.chars() {
        if char_count >= pos {
            break;
        }
        if c == '\n' {
            lineno += 1;
            colno = 1;
        } else {
            colno += 1;
        }
        char_count += 1;
    }
    (lineno, colno)
}

/// 產生與 CPython 解碼錯誤完全相同的 `JsonDecodeError`。
fn decode_error(s: &str, byte_pos: usize, msg: &str) -> JsonError {
    let bounded_pos = byte_pos.min(s.len());
    let pos = s[..bounded_pos].chars().count();
    let (lineno, colno) = compute_line_col(s, pos);
    JsonError::Decode(JsonDecodeError {
        msg: msg.to_string(),
        pos,
        lineno,
        colno,
    })
}

/// 跳過 ASCII 空白（` `、`\t`、`\n`、`\r`）。
fn skip_ws(bytes: &[u8], mut idx: usize) -> usize {
    while idx < bytes.len() {
        match bytes[idx] {
            b' ' | b'\t' | b'\n' | b'\r' => idx += 1,
            _ => break,
        }
    }
    idx
}

/// 解析 4 個 ASCII 16 進位字元；必須正好 4 個字元且全部為 16 進位數值（不接受 '+'、'-' 或前綴）。
fn parse_hex4(bytes: &[u8]) -> Option<u16> {
    if bytes.len() == 4 && bytes.iter().all(|b| b.is_ascii_hexdigit()) {
        u16::from_str_radix(std::str::from_utf8(bytes).ok()?, 16).ok()
    } else {
        None
    }
}

/// 解析 JSON 字串（含跳脫字元與 surrogate pair 轉譯）。
fn parse_string(s: &str, bytes: &[u8], start_quote: usize) -> Result<(String, usize), JsonError> {
    let mut out = String::new();
    let mut idx = start_quote + 1;

    while idx < bytes.len() {
        let b = bytes[idx];
        if b == b'"' {
            return Ok((out, idx + 1));
        }
        if b == b'\\' {
            let slash_idx = idx;
            idx += 1;
            if idx >= bytes.len() {
                return Err(decode_error(s, start_quote, "Unterminated string starting at"));
            }
            match bytes[idx] {
                b'"' => {
                    out.push('"');
                    idx += 1;
                }
                b'\\' => {
                    out.push('\\');
                    idx += 1;
                }
                b'/' => {
                    out.push('/');
                    idx += 1;
                }
                b'b' => {
                    out.push('\x08');
                    idx += 1;
                }
                b'f' => {
                    out.push('\x0c');
                    idx += 1;
                }
                b'n' => {
                    out.push('\n');
                    idx += 1;
                }
                b'r' => {
                    out.push('\r');
                    idx += 1;
                }
                b't' => {
                    out.push('\t');
                    idx += 1;
                }
                b'u' => {
                    let u_idx = idx;
                    idx += 1;
                    if idx + 4 >= bytes.len() {
                        return Err(decode_error(s, u_idx, "Invalid \\uXXXX escape"));
                    }
                    let Some(uni) = parse_hex4(&bytes[idx..idx + 4]) else {
                        return Err(decode_error(s, u_idx, "Invalid \\uXXXX escape"));
                    };
                    idx += 4;

                    if (0xD800..=0xDBFF).contains(&uni) {
                        // High surrogate: 只有在接續 \u 且長度足以包含 4 hex + 至少 1 個後續 byte 時才嘗試配對
                        if idx + 2 <= bytes.len() && &bytes[idx..idx + 2] == b"\\u" {
                            let u2_idx = idx + 1;
                            if idx + 6 >= bytes.len() {
                                return Err(decode_error(s, u2_idx, "Invalid \\uXXXX escape"));
                            }
                            let Some(uni2) = parse_hex4(&bytes[idx + 2..idx + 6]) else {
                                return Err(decode_error(s, u2_idx, "Invalid \\uXXXX escape"));
                            };

                            if (0xDC00..=0xDFFF).contains(&uni2) {
                                let cp = 0x10000 + (((uni as u32 - 0xD800) << 10) | (uni2 as u32 - 0xDC00));
                                out.push(char::from_u32(cp).unwrap());
                                idx += 6;
                            } else {
                                // 接續不是 low surrogate，依 RPC-H-03 當前 high surrogate 為孤立 surrogate
                                // 不消耗接續的 \uXXXX，留待下一輪作為一般 escape 解析
                                out.push('\u{FFFD}');
                            }
                        } else {
                            // 無接續的 \u，當前 high surrogate 為孤立 surrogate
                            out.push('\u{FFFD}');
                        }
                    } else if (0xDC00..=0xDFFF).contains(&uni) {
                        // 孤立 low surrogate（RPC-H-03）
                        out.push('\u{FFFD}');
                    } else {
                        out.push(char::from_u32(uni as u32).unwrap());
                    }
                }
                _ => {
                    return Err(decode_error(s, slash_idx, "Invalid \\escape"));
                }
            }
        } else if b < 0x20 {
            return Err(decode_error(s, idx, "Invalid control character at"));
        } else {
            // 一般 UTF-8 字元
            let remainder = &s[idx..];
            let c = remainder.chars().next().unwrap();
            out.push(c);
            idx += c.len_utf8();
        }
    }

    Err(decode_error(s, start_quote, "Unterminated string starting at"))
}

/// 解析數字（int 或 float）。
fn parse_number(s: &str, bytes: &[u8], start: usize) -> Result<(PyValue, usize), JsonError> {
    let mut idx = start;
    if bytes[idx] == b'-' {
        idx += 1;
    }
    if idx >= bytes.len() {
        return Err(decode_error(s, start, "Expecting value"));
    }

    if bytes[idx] == b'0' {
        idx += 1;
    } else if bytes[idx].is_ascii_digit() {
        while idx < bytes.len() && bytes[idx].is_ascii_digit() {
            idx += 1;
        }
    } else {
        return Err(decode_error(s, start, "Expecting value"));
    }

    let mut is_float = false;

    // 小數部分：必須有小數點且其後至少有一位數字
    if idx + 1 < bytes.len() && bytes[idx] == b'.' && bytes[idx + 1].is_ascii_digit() {
        is_float = true;
        idx += 2;
        while idx < bytes.len() && bytes[idx].is_ascii_digit() {
            idx += 1;
        }
    }

    // 指數部分：必須有 e/E 且可選正負號後至少有一位數字
    if idx < bytes.len() && (bytes[idx] == b'e' || bytes[idx] == b'E') {
        let mut exp_idx = idx + 1;
        if exp_idx < bytes.len() && (bytes[exp_idx] == b'+' || bytes[exp_idx] == b'-') {
            exp_idx += 1;
        }
        if exp_idx < bytes.len() && bytes[exp_idx].is_ascii_digit() {
            is_float = true;
            idx = exp_idx + 1;
            while idx < bytes.len() && bytes[idx].is_ascii_digit() {
                idx += 1;
            }
        }
    }

    let num_str = std::str::from_utf8(&bytes[start..idx]).unwrap();
    if is_float {
        let val = num_str.parse::<f64>().unwrap_or_else(|_| {
            if num_str.starts_with('-') {
                f64::NEG_INFINITY
            } else {
                f64::INFINITY
            }
        });
        Ok((PyValue::Float(val), idx))
    } else {
        match num_str.parse::<i128>() {
            Ok(n) => Ok((PyValue::Int(PyInt::Small(n)), idx)),
            Err(_) => Ok((PyValue::Int(PyInt::Big(num_str.to_string())), idx)),
        }
    }
}

/// 容器堆疊框架（顯式堆疊實作，防止遞迴堆疊溢位）。
enum ContainerFrame {
    Array(Vec<PyValue>),
    Object {
        dict: PyDict,
        pending_key: Option<String>,
    },
}

/// `json.loads`（strict=True）。
pub fn loads(s: &str) -> Result<PyValue, JsonError> {
    if s.starts_with('\u{FEFF}') {
        return Err(JsonError::Decode(JsonDecodeError {
            msg: "Unexpected UTF-8 BOM (decode using utf-8-sig)".to_string(),
            pos: 0,
            lineno: 1,
            colno: 1,
        }));
    }

    let bytes = s.as_bytes();
    let mut idx = skip_ws(bytes, 0);
    if idx >= bytes.len() {
        return Err(decode_error(s, idx, "Expecting value"));
    }

    let mut stack: Vec<ContainerFrame> = Vec::new();
    let mut depth: usize = 0;

    // 狀態機：要麼解析下一個值，要麼將產出的值送回父容器
    enum Action {
        ScanValue,
        PassUp(PyValue),
    }

    let mut action = Action::ScanValue;

    loop {
        match action {
            Action::ScanValue => {
                idx = skip_ws(bytes, idx);
                if idx >= bytes.len() {
                    return Err(decode_error(s, idx, "Expecting value"));
                }

                let b = bytes[idx];
                match b {
                    b'"' => {
                        let (val_str, new_idx) = parse_string(s, bytes, idx)?;
                        idx = new_idx;
                        action = Action::PassUp(PyValue::Str(val_str));
                    }
                    b'[' => {
                        depth += 1;
                        if depth > JSON_MAX_DEPTH {
                            return Err(JsonError::Recursion);
                        }
                        idx += 1;
                        idx = skip_ws(bytes, idx);
                        if idx < bytes.len() && bytes[idx] == b']' {
                            idx += 1;
                            depth -= 1;
                            action = Action::PassUp(PyValue::List(Vec::new()));
                        } else {
                            stack.push(ContainerFrame::Array(Vec::new()));
                            action = Action::ScanValue;
                        }
                    }
                    b'{' => {
                        depth += 1;
                        if depth > JSON_MAX_DEPTH {
                            return Err(JsonError::Recursion);
                        }
                        idx += 1;
                        idx = skip_ws(bytes, idx);
                        if idx < bytes.len() && bytes[idx] == b'}' {
                            idx += 1;
                            depth -= 1;
                            action = Action::PassUp(PyValue::Dict(PyDict::new()));
                        } else {
                            if idx >= bytes.len() || bytes[idx] != b'"' {
                                return Err(decode_error(
                                    s,
                                    idx,
                                    "Expecting property name enclosed in double quotes",
                                ));
                            }
                            let (key, new_idx) = parse_string(s, bytes, idx)?;
                            idx = skip_ws(bytes, new_idx);
                            if idx >= bytes.len() || bytes[idx] != b':' {
                                return Err(decode_error(s, idx, "Expecting ':' delimiter"));
                            }
                            idx += 1;
                            stack.push(ContainerFrame::Object {
                                dict: PyDict::new(),
                                pending_key: Some(key),
                            });
                            action = Action::ScanValue;
                        }
                    }
                    b'n' if bytes[idx..].starts_with(b"null") => {
                        idx += 4;
                        action = Action::PassUp(PyValue::Null);
                    }
                    b't' if bytes[idx..].starts_with(b"true") => {
                        idx += 4;
                        action = Action::PassUp(PyValue::Bool(true));
                    }
                    b'f' if bytes[idx..].starts_with(b"false") => {
                        idx += 5;
                        action = Action::PassUp(PyValue::Bool(false));
                    }
                    b'N' if bytes[idx..].starts_with(b"NaN") => {
                        idx += 3;
                        action = Action::PassUp(PyValue::Float(f64::NAN));
                    }
                    b'I' if bytes[idx..].starts_with(b"Infinity") => {
                        idx += 8;
                        action = Action::PassUp(PyValue::Float(f64::INFINITY));
                    }
                    b'-' if bytes[idx..].starts_with(b"-Infinity") => {
                        idx += 9;
                        action = Action::PassUp(PyValue::Float(f64::NEG_INFINITY));
                    }
                    b'-' | b'0'..=b'9' => {
                        let (num_val, new_idx) = parse_number(s, bytes, idx)?;
                        idx = new_idx;
                        action = Action::PassUp(num_val);
                    }
                    _ => {
                        return Err(decode_error(s, idx, "Expecting value"));
                    }
                }
            }
            Action::PassUp(val) => {
                if stack.is_empty() {
                    idx = skip_ws(bytes, idx);
                    if idx < bytes.len() {
                        return Err(decode_error(s, idx, "Extra data"));
                    }
                    return Ok(val);
                }

                let top = stack.last_mut().unwrap();
                match top {
                    ContainerFrame::Array(items) => {
                        items.push(val);
                        idx = skip_ws(bytes, idx);
                        if idx >= bytes.len() {
                            return Err(decode_error(s, idx, "Expecting ',' delimiter"));
                        }
                        if bytes[idx] == b']' {
                            idx += 1;
                            let ContainerFrame::Array(final_items) = stack.pop().unwrap() else {
                                unreachable!()
                            };
                            depth -= 1;
                            action = Action::PassUp(PyValue::List(final_items));
                        } else if bytes[idx] == b',' {
                            idx += 1;
                            action = Action::ScanValue;
                        } else {
                            return Err(decode_error(s, idx, "Expecting ',' delimiter"));
                        }
                    }
                    ContainerFrame::Object { dict, pending_key } => {
                        let key = pending_key.take().expect("pending_key should be set");
                        dict.insert(&key, val);
                        idx = skip_ws(bytes, idx);
                        if idx >= bytes.len() {
                            return Err(decode_error(s, idx, "Expecting ',' delimiter"));
                        }
                        if bytes[idx] == b'}' {
                            idx += 1;
                            let ContainerFrame::Object { dict: final_dict, .. } =
                                stack.pop().unwrap()
                            else {
                                unreachable!()
                            };
                            depth -= 1;
                            action = Action::PassUp(PyValue::Dict(final_dict));
                        } else if bytes[idx] == b',' {
                            idx += 1;
                            idx = skip_ws(bytes, idx);
                            if idx >= bytes.len() || bytes[idx] != b'"' {
                                return Err(decode_error(
                                    s,
                                    idx,
                                    "Expecting property name enclosed in double quotes",
                                ));
                            }
                            let (next_key, new_idx) = parse_string(s, bytes, idx)?;
                            idx = skip_ws(bytes, new_idx);
                            if idx >= bytes.len() || bytes[idx] != b':' {
                                return Err(decode_error(s, idx, "Expecting ':' delimiter"));
                            }
                            idx += 1;
                            let top_again = stack.last_mut().unwrap();
                            let ContainerFrame::Object { pending_key, .. } = top_again else {
                                unreachable!()
                            };
                            *pending_key = Some(next_key);
                            action = Action::ScanValue;
                        } else {
                            return Err(decode_error(s, idx, "Expecting ',' delimiter"));
                        }
                    }
                }
            }
        }
    }
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
        PyValue::Bool(b)
    }
}

impl From<&str> for PyValue {
    fn from(s: &str) -> Self {
        PyValue::Str(s.to_string())
    }
}

impl From<String> for PyValue {
    fn from(s: String) -> Self {
        PyValue::Str(s)
    }
}

impl From<i64> for PyValue {
    fn from(n: i64) -> Self {
        PyValue::Int(PyInt::Small(n as i128))
    }
}

impl From<f64> for PyValue {
    fn from(x: f64) -> Self {
        PyValue::Float(x)
    }
}

impl From<PyInt> for PyValue {
    fn from(n: PyInt) -> Self {
        PyValue::Int(n)
    }
}

impl From<PyDict> for PyValue {
    fn from(d: PyDict) -> Self {
        PyValue::Dict(d)
    }
}

impl From<Vec<PyValue>> for PyValue {
    fn from(v: Vec<PyValue>) -> Self {
        PyValue::List(v)
    }
}

impl<T: Into<PyValue>> From<Option<T>> for PyValue {
    /// `None` → `Null`。
    fn from(o: Option<T>) -> Self {
        match o {
            Some(v) => v.into(),
            None => PyValue::Null,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn illegal_unicode_escapes_rejected() {
        // '+' 前綴應被拒絕
        assert!(matches!(loads(r#""\u+123""#), Err(JsonError::Decode(_))));
        assert!(matches!(loads(r#"{"\u+123": 1}"#), Err(JsonError::Decode(_))));
        // surrogate 接續 '\u+123'
        assert!(matches!(loads(r#""\ud800\u+123""#), Err(JsonError::Decode(_))));

        // 截斷字串（無結尾引號）
        assert_eq!(
            loads(r#""\u0000"#),
            Err(JsonError::Decode(JsonDecodeError {
                msg: "Invalid \\uXXXX escape".to_string(),
                pos: 2,
                lineno: 1,
                colno: 3,
            }))
        );
        assert_eq!(
            loads(r#""\ud800\udc00"#),
            Err(JsonError::Decode(JsonDecodeError {
                msg: "Invalid \\uXXXX escape".to_string(),
                pos: 8,
                lineno: 1,
                colno: 9,
            }))
        );
    }

    #[test]
    fn del_escape_with_ensure_ascii() {
        let opts_ascii = DumpOpts {
            sep: Sep::Compact,
            ensure_ascii: true,
            sort_keys: true,
            allow_nan: true,
        };
        let opts_raw = DumpOpts {
            sep: Sep::Compact,
            ensure_ascii: false,
            sort_keys: true,
            allow_nan: true,
        };

        // 原始字串
        let val = PyValue::Str("\x7f".to_string());
        assert_eq!(dumps(&val, &opts_ascii), r#""\u007f""#);
        assert_eq!(dumps(&val, &opts_raw), "\"\x7f\"");

        // Dict key
        let mut dict = PyDict::new();
        dict.insert("\x7f", PyValue::Int(PyInt::Small(1)));
        assert_eq!(dumps(&PyValue::Dict(dict.clone()), &opts_ascii), r#"{"\u007f":1}"#);
        assert_eq!(dumps(&PyValue::Dict(dict), &opts_raw), "{\"\x7f\":1}");
    }

    #[test]
    fn repr_float_ties_to_even() {
        // bits 0x3e60000000000000 (2**-25): CPython produces 2.9802322387695312e-08
        let x1 = f64::from_bits(0x3e60000000000000);
        assert_eq!(repr_float(x1), "2.9802322387695312e-08");

        // bits 0x3e70000000000000: CPython produces 5.960464477539063e-08
        let x2 = f64::from_bits(0x3e70000000000000);
        assert_eq!(repr_float(x2), "5.960464477539063e-08");
    }

    #[test]
    fn big_int_representation() {
        let text = "12345678901234567890123456789012345678901234567890";
        let val = loads(text).unwrap();
        assert_eq!(val, PyValue::Int(PyInt::Big(text.to_string())));
        assert_eq!(dumps(&val, &CLI_STDOUT), text);
    }

    #[test]
    #[should_panic(expected = "P3 dumps only supports allow_nan=true")]
    fn allow_nan_false_panics() {
        let opts = DumpOpts {
            sep: Sep::Compact,
            ensure_ascii: false,
            sort_keys: true,
            allow_nan: false,
        };
        dumps(&PyValue::Null, &opts);
    }
}
