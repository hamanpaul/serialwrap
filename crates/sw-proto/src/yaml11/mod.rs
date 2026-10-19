//! YAML 1.1 `SafeLoader` 子集（P3-T6）。
//!
//! 只做讀取：以 yaml-rust2 低階事件取得 scalar style 與 tag，再由自製 implicit resolver
//! 逐條照 PyYAML `resolver.py`；語料與期望值由 `scripts/rust/goldens/gen_yaml11.py` 產生。

mod resolve;

pub mod emit;

pub use resolve::resolve_plain;

use crate::pyjson::{PyDict, PyInt, PyValue};

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
    Set(Vec<YValue>),
}

/// 保留插入序的 mapping。重複 key（Python 相等：`1 == 1.0 == True`）→ 值覆寫、位置保留。
#[derive(Debug, Clone, Default, PartialEq)]
pub struct YMap(pub Vec<(YValue, YValue)>);

impl YMap {
    /// 以字串 key 取值（僅比對 `YValue::Str` key）。
    pub fn get_str(&self, k: &str) -> Option<&YValue> {
        self.0
            .iter()
            .find(|(key, _)| matches!(key, YValue::Str(s) if s == k))
            .map(|(_, v)| v)
    }

    /// 是否含字串 key。
    pub fn contains_str(&self, k: &str) -> bool {
        self.get_str(k).is_some()
    }
}

/// YAML 解析錯誤（訊息非契約，見 plan G5）。
#[derive(Debug, Clone, PartialEq)]
pub struct YamlError {
    pub message: String,
}

impl std::fmt::Display for YamlError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}", self.message)
    }
}

impl std::error::Error for YamlError {}

/// 只取第一份文件；空文件 → `Null`；多文件 → `YamlError`（PyYAML `expected a single document`）。
pub fn safe_load(text: &str) -> Result<YValue, YamlError> {
    resolve::build_document(text)
}

/// CPython `bytes.__repr__`（`str(bytes)` 在未加 `-b` 時等同 repr）。
fn py_repr_bytes(b: &[u8]) -> String {
    let quote = if b.contains(&b'\'') && !b.contains(&b'"') { '"' } else { '\'' };
    let mut out = String::from("b");
    out.push(quote);
    for &c in b {
        match c {
            b'\\' => out.push_str("\\\\"),
            b'\t' => out.push_str("\\t"),
            b'\n' => out.push_str("\\n"),
            b'\r' => out.push_str("\\r"),
            c if c as char == quote => {
                out.push('\\');
                out.push(c as char);
            }
            0x20..=0x7e => out.push(c as char),
            _ => out.push_str(&format!("\\x{:02x}", c)),
        }
    }
    out.push(quote);
    out
}

fn py_repr_of(v: &YValue) -> String {
    match v {
        YValue::Null => "None".to_string(),
        YValue::Bool(true) => "True".to_string(),
        YValue::Bool(false) => "False".to_string(),
        YValue::Int(PyInt::Small(n)) => n.to_string(),
        YValue::Int(PyInt::Big(s)) => s.clone(),
        YValue::Float(f) => crate::pyjson::repr_float(*f),
        YValue::Str(s) => crate::pyfmt::py_repr_str(s),
        YValue::Timestamp { py_str } => py_str.clone(),
        YValue::Binary(b) => py_repr_bytes(b),
        YValue::Seq(items) => {
            let inner = items.iter().map(py_repr_of).collect::<Vec<_>>().join(", ");
            format!("[{inner}]")
        }
        YValue::Map(map) => {
            let inner = map
                .0
                .iter()
                .map(|(k, v)| format!("{}: {}", py_repr_of(k), py_repr_of(v)))
                .collect::<Vec<_>>()
                .join(", ");
            format!("{{{inner}}}")
        }
        YValue::Set(items) => {
            if items.is_empty() {
                "set()".to_string()
            } else {
                let inner = items.iter().map(py_repr_of).collect::<Vec<_>>().join(", ");
                format!("{{{inner}}}")
            }
        }
    }
}

/// `str(v)` 語意（`Timestamp` 用 `py_str`）。
pub fn py_str_of(v: &YValue) -> String {
    match v {
        YValue::Null => "None".to_string(),
        YValue::Bool(true) => "True".to_string(),
        YValue::Bool(false) => "False".to_string(),
        YValue::Int(PyInt::Small(n)) => n.to_string(),
        YValue::Int(PyInt::Big(s)) => s.clone(),
        YValue::Float(f) => crate::pyjson::repr_float(*f),
        YValue::Str(s) => s.clone(),
        YValue::Timestamp { py_str } => py_str.clone(),
        YValue::Binary(b) => py_repr_bytes(b),
        YValue::Seq(_) | YValue::Map(_) | YValue::Set(_) => py_repr_of(v),
    }
}

/// Python `bool(v)` 語意。
pub fn py_truthy_of(v: &YValue) -> bool {
    match v {
        YValue::Null => false,
        YValue::Bool(b) => *b,
        YValue::Int(PyInt::Small(n)) => *n != 0,
        YValue::Int(PyInt::Big(s)) => !s.is_empty() && s != "0",
        YValue::Float(f) => *f != 0.0,
        YValue::Str(s) => !s.is_empty(),
        YValue::Timestamp { .. } => true,
        YValue::Binary(b) => !b.is_empty(),
        YValue::Seq(l) => !l.is_empty(),
        YValue::Map(m) => !m.0.is_empty(),
        YValue::Set(s) => !s.is_empty(),
    }
}

/// golden 比對用的型別標記編碼（`gen_yaml11.py` 的 `tag()` 逐一對應）。
pub fn to_tagged_json(v: &YValue) -> PyValue {
    match v {
        YValue::Null => PyValue::Dict(PyDict::from_pairs(vec![
            ("t".into(), PyValue::Str("null".into())),
        ])),
        YValue::Bool(b) => PyValue::Dict(PyDict::from_pairs(vec![
            ("t".into(), PyValue::Str("bool".into())),
            ("v".into(), PyValue::Bool(*b)),
        ])),
        YValue::Int(i) => {
            let s = match i {
                PyInt::Small(n) => n.to_string(),
                PyInt::Big(s) => s.clone(),
            };
            PyValue::Dict(PyDict::from_pairs(vec![
                ("t".into(), PyValue::Str("int".into())),
                ("v".into(), PyValue::Str(s)),
            ]))
        }
        YValue::Float(f) => PyValue::Dict(PyDict::from_pairs(vec![
            ("t".into(), PyValue::Str("float".into())),
            ("v".into(), PyValue::Str(crate::pyjson::repr_float(*f))),
        ])),
        YValue::Str(s) => PyValue::Dict(PyDict::from_pairs(vec![
            ("t".into(), PyValue::Str("str".into())),
            ("v".into(), PyValue::Str(s.clone())),
        ])),
        YValue::Timestamp { py_str } => PyValue::Dict(PyDict::from_pairs(vec![
            ("t".into(), PyValue::Str("ts".into())),
            ("v".into(), PyValue::Str(py_str.clone())),
        ])),
        YValue::Binary(b) => {
            let mut hex = String::with_capacity(b.len() * 2);
            for byte in b {
                use std::fmt::Write as _;
                let _ = write!(hex, "{:02x}", byte);
            }
            PyValue::Dict(PyDict::from_pairs(vec![
                ("t".into(), PyValue::Str("bin".into())),
                ("v".into(), PyValue::Str(hex)),
            ]))
        }
        YValue::Set(elements) => PyValue::Dict(PyDict::from_pairs(vec![
            ("t".into(), PyValue::Str("set".into())),
            ("v".into(), PyValue::List(elements.iter().map(to_tagged_json).collect())),
        ])),
        YValue::Seq(seq) => PyValue::Dict(PyDict::from_pairs(vec![
            ("t".into(), PyValue::Str("seq".into())),
            ("v".into(), PyValue::List(seq.iter().map(to_tagged_json).collect())),
        ])),
        YValue::Map(map) => {
            let pairs: Vec<PyValue> = map
                .0
                .iter()
                .map(|(k, v)| PyValue::List(vec![to_tagged_json(k), to_tagged_json(v)]))
                .collect();
            PyValue::Dict(PyDict::from_pairs(vec![
                ("t".into(), PyValue::Str("map".into())),
                ("v".into(), PyValue::List(pairs)),
            ]))
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_valid_yaml_block_scalars_and_regexes() {
        let y1 = "post_login_cmd: |\n  : \"${FOO:=bar}\"\n  echo $FOO\n";
        assert!(safe_load(y1).is_ok());

        let y2 = "prompt_regex: (?m)^root@x [#$] $\nlogin_regex: (?mi)login:\\s*$\n";
        assert!(safe_load(y2).is_ok());

        let y3 = "- ? a\n  : b\n";
        assert!(safe_load(y3).is_ok());

        let y4 = "a: |\n  x\ty\n";
        assert!(safe_load(y4).is_ok());
    }

    #[test]
    fn test_deep_nesting_and_alias_bomb_bounded() {
        let deep = "- ".repeat(300) + "x\n";
        assert!(safe_load(&deep).is_err());

        // Alias bomb
        let mut bomb = String::from("a0: &a0 [x, x]\n");
        for i in 1..25 {
            bomb.push_str(&format!("a{i}: &a{i} [*a{}, *a{}]\n", i - 1, i - 1));
        }
        assert!(safe_load(&bomb).is_err());
    }

    #[test]
    fn test_relax_flow_indent_and_lone_dash() {
        let y1 = "bootloader_prompts: [\n  \"^=> $\",\n  \"^U-Boot> $\"\n]\n";
        assert!(safe_load(y1).is_ok());

        let y2 = "e: [a, -]\n";
        let res = safe_load(y2).unwrap();
        if let YValue::Map(m) = res {
            if let Some(YValue::Seq(items)) = m.get_str("e") {
                assert_eq!(items.len(), 2);
                assert_eq!(items[1], YValue::Str("-".into()));
            } else {
                panic!("expected seq");
            }
        }
    }

    #[test]
    fn test_big_integers_radix() {
        let hex = "val: 0x100000000000000000000000000000000\n";
        let res = safe_load(hex).unwrap();
        if let YValue::Map(m) = res {
            assert_eq!(
                m.get_str("val"),
                Some(&YValue::Int(PyInt::Big(
                    "340282366920938463463374607431768211456".into()
                )))
            );
        }

        let oct = "val: 017\n";
        let res = safe_load(oct).unwrap();
        if let YValue::Map(m) = res {
            assert_eq!(m.get_str("val"), Some(&YValue::Int(PyInt::Small(15))));
        }

        let oct_tagged = "val: !!int 0o17\n";
        let res = safe_load(oct_tagged).unwrap();
        if let YValue::Map(m) = res {
            assert_eq!(m.get_str("val"), Some(&YValue::Int(PyInt::Small(15))));
        }

        let sexa = "val: 190:20:30\n";
        let res = safe_load(sexa).unwrap();
        if let YValue::Map(m) = res {
            assert_eq!(
                m.get_str("val"),
                Some(&YValue::Int(PyInt::Small(190 * 3600 + 20 * 60 + 30)))
            );
        }
    }

    #[test]
    fn test_binary_decode_stops_at_padding() {
        let y = "val: !!binary YQ==Yg==\n";
        let res = safe_load(y).unwrap();
        if let YValue::Map(m) = res {
            assert_eq!(m.get_str("val"), Some(&YValue::Binary(vec![b'a'])));
        }
    }

    #[test]
    fn test_big_int_map_key_equality() {
        let y = "0x100000000000000000000000000000000: 1\n0x100000000000000000000000000000000: 2\n";
        let res = safe_load(y).unwrap();
        if let YValue::Map(m) = res {
            assert_eq!(m.0.len(), 1);
            assert_eq!(m.0[0].1, YValue::Int(PyInt::Small(2)));
        }
    }

    #[test]
    fn test_pairs_and_non_specific_tag() {
        let y_pairs = "val: !!pairs\n  - a: 1\n  - b: 2\n";
        let res = safe_load(y_pairs).unwrap();
        if let YValue::Map(m) = res {
            if let Some(YValue::Seq(items)) = m.get_str("val") {
                assert_eq!(items.len(), 2);
            } else {
                panic!("expected seq for pairs");
            }
        }

        let y_tag = "val: ! 5\n";
        let res = safe_load(y_tag).unwrap();
        if let YValue::Map(m) = res {
            assert_eq!(m.get_str("val"), Some(&YValue::Int(PyInt::Small(5))));
        }
    }

    #[test]
    fn test_py_repr_bytes_formatting() {
        assert_eq!(py_repr_bytes(b"hi"), "b'hi'");
        assert_eq!(py_repr_bytes(b"'single'"), "b\"'single'\"");
        assert_eq!(py_repr_bytes(b"\x00\xff"), "b'\\x00\\xff'");
    }
}
