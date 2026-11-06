//! Python 格式化與 repr 語意層（P3-T4）。
//!
//! `repr` 規則對齊 CPython 3.12.3 `Objects/unicodeobject.c::unicode_repr`；
//! float 的 repr 唯一實作位於 `pyjson::repr_float`，此處僅單向轉呼叫。

use crate::pycompat::py_isprintable;
use crate::pyjson::{PyInt, PyValue};
use std::fmt::Write as _;

/// Python float repr；單向轉呼叫 `pyjson::repr_float`。
pub fn py_repr_float(x: f64) -> String {
    crate::pyjson::repr_float(x)
}

/// argv 內無效單位的跳脫前綴（P3-T14，Q-19）。argv 不可能含 NUL，故帶此前綴的字元一定來自跳脫。
pub const ESCAPE_MARK: char = '\0';

/// 跳脫後的 lone surrogate 碼位偏移：`ESCAPE_MARK` 之後接 `char::from_u32(ESCAPE_BASE + cp)`，`cp` 為 `0xD800..=0xDFFF`。
const ESCAPE_BASE: u32 = 0xF0000;

/// 把 lone surrogate 碼位（`0xD800..=0xDFFF`）跳脫為兩個字元（`ESCAPE_MARK` + 偏移字元）；範圍外的碼位不輸出。
pub fn escape_surrogate(cp: u32, out: &mut String) {
    if !(0xD800..=0xDFFF).contains(&cp) {
        return;
    }
    out.push(ESCAPE_MARK);
    if let Some(c) = char::from_u32(ESCAPE_BASE + cp) {
        out.push(c);
    }
}

/// 若 `c` 為跳脫字元（偏移字元）則回傳其 lone surrogate 碼位。
pub fn surrogate_of_escape(c: char) -> Option<u32> {
    let cp = (c as u32).checked_sub(ESCAPE_BASE)?;
    (0xD800..=0xDFFF).contains(&cp).then_some(cp)
}

/// 單一字元的 repr 輸出（`unicode_repr` 的逐字規則）。
fn push_repr_char(out: &mut String, c: char, quote: char) {
    let cp = c as u32;
    if c == quote || c == '\\' {
        out.push('\\');
        out.push(c);
    } else if c == '\t' {
        out.push_str("\\t");
    } else if c == '\n' {
        out.push_str("\\n");
    } else if c == '\r' {
        out.push_str("\\r");
    } else if cp < 0x20 || cp == 0x7f {
        let _ = write!(out, "\\x{cp:02x}");
    } else if cp < 0x7f || py_isprintable(c) {
        out.push(c);
    } else if cp <= 0xff {
        let _ = write!(out, "\\x{cp:02x}");
    } else if cp <= 0xffff {
        let _ = write!(out, "\\u{cp:04x}");
    } else {
        let _ = write!(out, "\\U{cp:08x}");
    }
}

/// `repr(str)`：引號選擇、跳脫字元、`isprintable` 規則。
///
/// 含單引號且不含雙引號時以雙引號包覆；其餘情況以單引號包覆並跳脫單引號。
pub fn py_repr_str(s: &str) -> String {
    let has_single = s.contains('\'');
    let has_double = s.contains('"');
    let quote = if has_single && !has_double { '"' } else { '\'' };

    let mut out = String::with_capacity(s.len() + 2);
    out.push(quote);
    for c in s.chars() {
        push_repr_char(&mut out, c, quote);
    }
    out.push(quote);
    out
}

/// 含跳脫 lone surrogate 的字串之 `repr`：跳脫處以 Python 的 `\udXXX` 形式呈現（ASCII）。
///
/// 無跳脫字元時與 [`py_repr_str`] 完全相同。
pub fn py_repr_argv(s: &str) -> String {
    let has_single = s.contains('\'');
    let has_double = s.contains('"');
    let quote = if has_single && !has_double { '"' } else { '\'' };

    let mut out = String::with_capacity(s.len() + 2);
    out.push(quote);
    for segment in segments(s) {
        match segment {
            Segment::Char(c) => push_repr_char(&mut out, c, quote),
            Segment::Surrogate(cp) => {
                let _ = write!(out, "\\u{cp:04x}");
            }
        }
    }
    out.push(quote);
    out
}

/// stderr 的 backslashreplace 形式：跳脫的 lone surrogate 以 `\udXXX` 文字呈現，其餘字元原樣。
pub fn py_display_argv(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for segment in segments(s) {
        match segment {
            Segment::Char(c) => out.push(c),
            Segment::Surrogate(cp) => {
                let _ = write!(out, "\\u{cp:04x}");
            }
        }
    }
    out
}

/// 字串的一段：原字元，或跳脫還原的 lone surrogate 碼位。
enum Segment {
    Char(char),
    Surrogate(u32),
}

/// 依 [`ESCAPE_MARK`] 規則切段；前綴後不是偏移字元者，前綴本身當作一般字元。
fn segments(s: &str) -> impl Iterator<Item = Segment> + '_ {
    let mut chars = s.chars().peekable();
    std::iter::from_fn(move || {
        let c = chars.next()?;
        if c == ESCAPE_MARK {
            if let Some(cp) = chars.peek().and_then(|next| surrogate_of_escape(*next)) {
                chars.next();
                return Some(Segment::Surrogate(cp));
            }
        }
        Some(Segment::Char(c))
    })
}

/// `str(v)`：None→"None"、True→"True"、float→repr、str 原樣、list/dict→repr。
pub fn py_str(v: &PyValue) -> String {
    match v {
        PyValue::Str(s) => s.clone(),
        other => py_repr(other),
    }
}

/// `repr(v)`：dict 為 `{'k': v}`、list 為 `[a, b]`。
///
/// 以顯式堆疊迭代實作（已登記 NC-05，`spec-reconciliation.md` §6）：深層巢狀不會耗盡執行緒堆疊（stack overflow 會直接 abort），
/// 且不擲 `RecursionError`；CPython 於約 1000 層擲 `RecursionError`，此差異由 Rust 端呼叫者以既有深度上限（`JSON_MAX_DEPTH`）約束。
pub fn py_repr(v: &PyValue) -> String {
    /// 待輸出的工作項目（後進先出）。
    enum Work<'a> {
        Val(&'a PyValue),
        Key(&'a str),
        Lit(&'static str),
    }

    let mut out = String::new();
    let mut stack: Vec<Work<'_>> = vec![Work::Val(v)];
    while let Some(work) = stack.pop() {
        match work {
            Work::Lit(s) => out.push_str(s),
            Work::Key(k) => out.push_str(&py_repr_str(k)),
            Work::Val(val) => match val {
                PyValue::Null => out.push_str("None"),
                PyValue::Bool(true) => out.push_str("True"),
                PyValue::Bool(false) => out.push_str("False"),
                PyValue::Int(PyInt::Small(n)) => out.push_str(&n.to_string()),
                PyValue::Int(PyInt::Big(s)) => out.push_str(s),
                PyValue::Float(x) => out.push_str(&py_repr_float(*x)),
                PyValue::Str(s) => out.push_str(&py_repr_str(s)),
                PyValue::List(items) => {
                    out.push('[');
                    // 依輸出順序產生序列，再反向壓入堆疊，使第一個元素位於堆疊頂端。
                    let mut seq: Vec<Work<'_>> = Vec::with_capacity(items.len() * 2 + 1);
                    for (i, item) in items.iter().enumerate() {
                        if i > 0 {
                            seq.push(Work::Lit(", "));
                        }
                        seq.push(Work::Val(item));
                    }
                    seq.push(Work::Lit("]"));
                    stack.extend(seq.into_iter().rev());
                }
                PyValue::Dict(d) => {
                    out.push('{');
                    let mut seq: Vec<Work<'_>> = Vec::with_capacity(d.0.len() * 4 + 1);
                    for (i, (k, item)) in d.0.iter().enumerate() {
                        if i > 0 {
                            seq.push(Work::Lit(", "));
                        }
                        seq.push(Work::Key(k.as_str()));
                        seq.push(Work::Lit(": "));
                        seq.push(Work::Val(item));
                    }
                    seq.push(Work::Lit("}"));
                    stack.extend(seq.into_iter().rev());
                }
            },
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::pyjson::PyDict;

    #[test]
    fn repr_nested_containers_recorded_cases() {
        // CPython 3.12.3：repr({"a": [1, {"b": None}], "c": "x'y"})
        let v = PyValue::Dict(PyDict::from_pairs(vec![
            (
                "a".to_string(),
                PyValue::List(vec![
                    PyValue::Int(PyInt::Small(1)),
                    PyValue::Dict(PyDict::from_pairs(vec![("b".to_string(), PyValue::Null)])),
                ]),
            ),
            ("c".to_string(), PyValue::Str("x'y".to_string())),
        ]));
        assert_eq!(py_repr(&v), "{'a': [1, {'b': None}], 'c': \"x'y\"}");
        assert_eq!(py_repr(&PyValue::List(Vec::new())), "[]");
        assert_eq!(py_repr(&PyValue::Dict(PyDict::new())), "{}");
        assert_eq!(py_str(&PyValue::Str("it's".to_string())), "it's");
        assert_eq!(py_str(&PyValue::Null), "None");
    }

    /// NC-05：2900 層巢狀（`JSON_MAX_DEPTH` 上限）不會耗盡堆疊。
    #[test]
    fn repr_deep_nesting_does_not_overflow() {
        let depth = 2900;
        let mut v = PyValue::Int(PyInt::Small(1));
        for _ in 0..depth {
            v = PyValue::List(vec![v]);
        }
        let text = py_repr(&v);
        assert_eq!(text.len(), 2 * depth + 1);
        assert!(text.starts_with(&"[".repeat(depth)));
        assert!(text.ends_with(&format!("1{}", "]".repeat(depth))));
    }
}
