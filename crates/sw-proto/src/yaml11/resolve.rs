//! 事件流建樹與 PyYAML implicit resolver（P3-T6）。
//!
//! 實作以 `yaml_rust2::parser::Parser` 事件（`Scalar(value, TScalarStyle, anchor, tag)`）建樹；
//! plain scalar 依 PyYAML `resolver.py` 的 regex 與首字元表判型；quoted scalar 一律 `str`。

use std::collections::HashMap;
use std::sync::OnceLock;

use yaml_rust2::parser::{Event, MarkedEventReceiver, Parser, Tag};
use yaml_rust2::scanner::{Marker, Scanner, TScalarStyle, Token, TokenType};

use super::{YMap, YValue, YamlError};
use crate::pyjson::PyInt;

static BOOL_RE: OnceLock<fancy_regex::Regex> = OnceLock::new();
static FLOAT_RE: OnceLock<fancy_regex::Regex> = OnceLock::new();
static INT_RE: OnceLock<fancy_regex::Regex> = OnceLock::new();
static TIMESTAMP_RE: OnceLock<fancy_regex::Regex> = OnceLock::new();

fn bool_re() -> &'static fancy_regex::Regex {
    BOOL_RE.get_or_init(|| {
        fancy_regex::Regex::new(
            r"^(?:yes|Yes|YES|no|No|NO|true|True|TRUE|false|False|FALSE|on|On|ON|off|Off|OFF)$",
        )
        .unwrap()
    })
}

fn float_re() -> &'static fancy_regex::Regex {
    FLOAT_RE.get_or_init(|| {
        fancy_regex::Regex::new(
            r"^(?:[-+]?(?:[0-9][0-9_]*)\.[0-9_]*(?:[eE][-+][0-9]+)?|\.[0-9][0-9_]*(?:[eE][-+][0-9]+)?|[-+]?[0-9][0-9_]*(?::[0-5]?[0-9])+\.[0-9_]*|[-+]?\.(?:inf|Inf|INF)|\.(?:nan|NaN|NAN))$",
        )
        .unwrap()
    })
}

fn int_re() -> &'static fancy_regex::Regex {
    INT_RE.get_or_init(|| {
        fancy_regex::Regex::new(
            r"^(?:[-+]?0b[0-1_]+|[-+]?0[0-7_]+|[-+]?(?:0|[1-9][0-9_]*)|[-+]?0x[0-9a-fA-F_]+|[-+]?[1-9][0-9_]*(?::[0-5]?[0-9])+)$",
        )
        .unwrap()
    })
}

fn timestamp_re() -> &'static fancy_regex::Regex {
    TIMESTAMP_RE.get_or_init(|| {
        fancy_regex::Regex::new(
            r"^(?:[0-9]{4}-[0-9]{2}-[0-9]{2}|[0-9]{4}-[0-9]{1,2}-[0-9]{1,2}(?:[Tt]|[ \t]+)[0-9]{1,2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]*)?(?:[ \t]*(?:Z|[-+][0-9]{1,2}(?::[0-9]{2})?))?)$",
        )
        .unwrap()
    })
}

/// PyYAML implicit resolver：回傳 plain scalar 的 tag 類別（`bool`／`int`／`float`／`null`／`timestamp`／`merge`／`value`）。
pub fn resolve_plain(s: &str) -> Option<&'static str> {
    if s.is_empty() {
        return Some("null");
    }
    let c = s.chars().next().unwrap();
    match c {
        '~' => {
            if s == "~" {
                Some("null")
            } else {
                None
            }
        }
        '<' => {
            if s == "<<" {
                Some("merge")
            } else {
                None
            }
        }
        '=' => {
            if s == "=" {
                Some("value")
            } else {
                None
            }
        }
        'n' | 'N' => {
            if s == "null" || s == "Null" || s == "NULL" {
                Some("null")
            } else if bool_re().is_match(s).unwrap_or(false) {
                Some("bool")
            } else {
                None
            }
        }
        'y' | 'Y' | 't' | 'T' | 'f' | 'F' | 'o' | 'O' => {
            if bool_re().is_match(s).unwrap_or(false) {
                Some("bool")
            } else {
                None
            }
        }
        '.' => {
            if float_re().is_match(s).unwrap_or(false) {
                Some("float")
            } else {
                None
            }
        }
        '+' | '-' => {
            if float_re().is_match(s).unwrap_or(false) {
                Some("float")
            } else if int_re().is_match(s).unwrap_or(false) {
                Some("int")
            } else {
                None
            }
        }
        '0'..='9' => {
            if float_re().is_match(s).unwrap_or(false) {
                Some("float")
            } else if int_re().is_match(s).unwrap_or(false) {
                Some("int")
            } else if timestamp_re().is_match(s).unwrap_or(false) {
                Some("timestamp")
            } else {
                None
            }
        }
        _ => None,
    }
}

fn is_leap_year(year: i32) -> bool {
    (year % 4 == 0 && year % 100 != 0) || (year % 400 == 0)
}

fn days_in_month(year: i32, month: u32) -> u32 {
    match month {
        1 | 3 | 5 | 7 | 8 | 10 | 12 => 31,
        4 | 6 | 9 | 11 => 30,
        2 => {
            if is_leap_year(year) {
                29
            } else {
                28
            }
        }
        _ => 0,
    }
}

fn radix_to_decimal_string(digits: &[u32], radix: u32) -> String {
    if digits.is_empty() || digits.iter().all(|&d| d == 0) {
        return "0".to_string();
    }
    let mut dec_digits: Vec<u8> = vec![0];
    for &d in digits {
        let mut carry = d as u64;
        for byte in dec_digits.iter_mut() {
            let cur = (*byte as u64) * (radix as u64) + carry;
            *byte = (cur % 10) as u8;
            carry = cur / 10;
        }
        while carry > 0 {
            dec_digits.push((carry % 10) as u8);
            carry /= 10;
        }
    }
    dec_digits.iter().rev().map(|&d| (b'0' + d) as char).collect()
}

fn parse_radix_int(sign: i128, raw: &str, radix: u32, err_msg: &str) -> Result<PyInt, YamlError> {
    if raw.is_empty() {
        return Err(YamlError {
            message: err_msg.into(),
        });
    }
    let mut digits = Vec::with_capacity(raw.len());
    for c in raw.chars() {
        let d = c.to_digit(radix).ok_or_else(|| YamlError {
            message: err_msg.into(),
        })?;
        digits.push(d);
    }
    if let Ok(val) = i128::from_str_radix(raw, radix) {
        Ok(PyInt::Small(sign * val))
    } else {
        let dec = radix_to_decimal_string(&digits, radix);
        let mut big = String::new();
        if sign < 0 && dec != "0" {
            big.push('-');
        }
        big.push_str(&dec);
        Ok(PyInt::Big(big))
    }
}

fn parse_sexagesimal(sign: i128, rest: &str) -> Result<PyInt, YamlError> {
    let parts: Vec<&str> = rest.split(':').collect();
    let mut digits = Vec::with_capacity(parts.len());
    for part in &parts {
        if part.is_empty() {
            return Err(YamlError {
                message: "invalid sexagesimal int".into(),
            });
        }
        let d = part.parse::<u32>().map_err(|_| YamlError {
            message: "invalid sexagesimal int".into(),
        })?;
        digits.push(d);
    }
    let mut val: i128 = 0;
    let mut base: i128 = 1;
    let mut overflow = false;
    for &d in digits.iter().rev() {
        if let Some(term) = (d as i128).checked_mul(base) {
            if let Some(next_val) = val.checked_add(term) {
                val = next_val;
                if let Some(next_base) = base.checked_mul(60) {
                    base = next_base;
                    continue;
                }
            }
        }
        overflow = true;
        break;
    }
    if !overflow {
        return Ok(PyInt::Small(sign * val));
    }
    let dec = radix_to_decimal_string(&digits, 60);
    let mut big = String::new();
    if sign < 0 && dec != "0" {
        big.push('-');
    }
    big.push_str(&dec);
    Ok(PyInt::Big(big))
}

fn construct_int(s: &str) -> Result<PyInt, YamlError> {
    let clean = s.replace('_', "");
    let (sign, rest) = if let Some(stripped) = clean.strip_prefix('-') {
        (-1i128, stripped)
    } else if let Some(stripped) = clean.strip_prefix('+') {
        (1i128, stripped)
    } else {
        (1i128, clean.as_str())
    };

    if rest == "0" {
        return Ok(PyInt::Small(0));
    }
    if let Some(hex) = rest.strip_prefix("0x").or_else(|| rest.strip_prefix("0X")) {
        return parse_radix_int(sign, hex, 16, "invalid hex int");
    }
    if let Some(bin) = rest.strip_prefix("0b").or_else(|| rest.strip_prefix("0B")) {
        return parse_radix_int(sign, bin, 2, "invalid binary int");
    }
    if let Some(oct) = rest.strip_prefix("0o").or_else(|| rest.strip_prefix("0O")) {
        return parse_radix_int(sign, oct, 8, "invalid octal int");
    }
    if rest.starts_with('0') && rest.len() > 1 && !rest.contains(':') {
        return parse_radix_int(sign, rest, 8, "invalid octal int");
    }
    if rest.contains(':') {
        return parse_sexagesimal(sign, rest);
    }

    if rest.is_empty() || rest.chars().any(|c| !c.is_ascii_digit()) {
        return Err(YamlError {
            message: "invalid decimal int".into(),
        });
    }

    if let Ok(n) = rest.parse::<i128>() {
        Ok(PyInt::Small(sign * n))
    } else {
        let mut big = String::new();
        if sign < 0 {
            big.push('-');
        }
        big.push_str(rest);
        Ok(PyInt::Big(big))
    }
}

fn construct_float(s: &str) -> Result<f64, YamlError> {
    let clean = s.replace('_', "").to_lowercase();
    let (sign, rest) = if let Some(stripped) = clean.strip_prefix('-') {
        (-1.0f64, stripped)
    } else if let Some(stripped) = clean.strip_prefix('+') {
        (1.0f64, stripped)
    } else {
        (1.0f64, clean.as_str())
    };

    if rest == ".inf" {
        return Ok(sign * f64::INFINITY);
    }
    if rest == ".nan" {
        return Ok(f64::NAN);
    }
    if rest.contains(':') {
        let mut val: f64 = 0.0;
        let mut base: f64 = 1.0;
        let parts: Vec<&str> = rest.split(':').collect();
        for part in parts.iter().rev() {
            let digit = part.parse::<f64>().map_err(|_| YamlError {
                message: "invalid float".into(),
            })?;
            val += digit * base;
            base *= 60.0;
        }
        return Ok(sign * val);
    }
    let val = rest.parse::<f64>().map_err(|_| YamlError {
        message: "invalid float".into(),
    })?;
    if val == 0.0 && sign < 0.0 {
        return Ok(-0.0);
    }
    Ok(sign * val)
}

fn construct_timestamp(s: &str) -> Result<YValue, YamlError> {
    static TS_PARSE_RE: OnceLock<fancy_regex::Regex> = OnceLock::new();
    let re = TS_PARSE_RE.get_or_init(|| {
        fancy_regex::Regex::new(
            r"^(?P<year>\d{4})-(?P<month>\d{1,2})-(?P<day>\d{1,2})(?:(?:[Tt]|[ \t]+)(?P<hour>\d{1,2}):(?P<minute>\d{2}):(?P<second>\d{2})(?:\.(?P<fraction>\d*))?(?:[ \t]*(?P<tz>Z|(?P<tz_sign>[-+])(?P<tz_hour>\d{1,2})(?::(?P<tz_minute>\d{2}))?))?)?$",
        )
        .unwrap()
    });

    let caps = match re.captures(s) {
        Ok(Some(c)) => c,
        _ => return Err(YamlError {
            message: "invalid timestamp format".into(),
        }),
    };

    let year = caps
        .name("year")
        .unwrap()
        .as_str()
        .parse::<i32>()
        .map_err(|_| YamlError {
            message: "invalid year".into(),
        })?;
    if year < 1 {
        return Err(YamlError {
            message: "year out of range".into(),
        });
    }
    let month = caps
        .name("month")
        .unwrap()
        .as_str()
        .parse::<u32>()
        .map_err(|_| YamlError {
            message: "invalid month".into(),
        })?;
    let day = caps
        .name("day")
        .unwrap()
        .as_str()
        .parse::<u32>()
        .map_err(|_| YamlError {
            message: "invalid day".into(),
        })?;

    if !(1..=12).contains(&month) {
        return Err(YamlError {
            message: "month out of range".into(),
        });
    }
    let max_days = days_in_month(year, month);
    if !(1..=max_days).contains(&day) {
        return Err(YamlError {
            message: "day out of range".into(),
        });
    }

    if caps.name("hour").is_none() {
        return Ok(YValue::Timestamp {
            py_str: format!("{year:04}-{month:02}-{day:02}"),
        });
    }

    let hour = caps
        .name("hour")
        .unwrap()
        .as_str()
        .parse::<u32>()
        .map_err(|_| YamlError {
            message: "invalid hour".into(),
        })?;
    let minute = caps
        .name("minute")
        .unwrap()
        .as_str()
        .parse::<u32>()
        .map_err(|_| YamlError {
            message: "invalid minute".into(),
        })?;
    let second = caps
        .name("second")
        .unwrap()
        .as_str()
        .parse::<u32>()
        .map_err(|_| YamlError {
            message: "invalid second".into(),
        })?;

    if hour > 23 || minute > 59 || second > 59 {
        return Err(YamlError {
            message: "time out of range".into(),
        });
    }

    let mut micro = 0u32;
    if let Some(f_match) = caps.name("fraction") {
        let f_str = f_match.as_str();
        if !f_str.is_empty() {
            let mut frac_6 = f_str.chars().take(6).collect::<String>();
            while frac_6.len() < 6 {
                frac_6.push('0');
            }
            micro = frac_6.parse::<u32>().unwrap_or(0);
        }
    }

    let mut out = format!("{year:04}-{month:02}-{day:02} {hour:02}:{minute:02}:{second:02}");
    if micro > 0 {
        use std::fmt::Write as _;
        let _ = write!(out, ".{micro:06}");
    }

    if let Some(tz_sign_match) = caps.name("tz_sign") {
        let tz_sign = tz_sign_match.as_str();
        let tz_hour = caps
            .name("tz_hour")
            .unwrap()
            .as_str()
            .parse::<u32>()
            .map_err(|_| YamlError {
                message: "invalid tz hour".into(),
            })?;
        let tz_minute = if let Some(m) = caps.name("tz_minute") {
            m.as_str().parse::<u32>().map_err(|_| YamlError {
                message: "invalid tz minute".into(),
            })?
        } else {
            0
        };
        // datetime.timezone(timedelta(hours=h, minutes=m)) 要求 |offset| < 24h；分鐘可大於 59（會進位）。
        let total = tz_hour * 60 + tz_minute;
        if total >= 24 * 60 {
            return Err(YamlError {
                message: "tz out of range".into(),
            });
        }
        // str(datetime) 以正規化後的 offset 輸出；零 offset 一律為 +00:00（不保留 "-00:00"）。
        let sign = if tz_sign == "-" && total != 0 { '-' } else { '+' };
        use std::fmt::Write as _;
        let _ = write!(out, "{}{:02}:{:02}", sign, total / 60, total % 60);
    } else if caps.name("tz").map(|m| m.as_str()) == Some("Z") {
        out.push_str("+00:00");
    }

    Ok(YValue::Timestamp { py_str: out })
}

fn b64_val(b: u8) -> Result<u8, YamlError> {
    match b {
        b'A'..=b'Z' => Ok(b - b'A'),
        b'a'..=b'z' => Ok(b - b'a' + 26),
        b'0'..=b'9' => Ok(b - b'0' + 52),
        b'+' => Ok(62),
        b'/' => Ok(63),
        _ => Err(YamlError {
            message: "invalid base64 char".into(),
        }),
    }
}

fn py_base64_decode(s: &str) -> Result<Vec<u8>, YamlError> {
    let mut clean = Vec::new();
    for b in s.bytes() {
        if matches!(b, b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'+' | b'/' | b'=') {
            clean.push(b);
        } else if matches!(b, b' ' | b'\t' | b'\n' | b'\r' | 0x0b | 0x0c) {
            // Whitespace ignored
        } else if b <= 0x7f {
            // Non-base64 ascii ignored in Python binascii.a2b_base64
        } else {
            return Err(YamlError {
                message: "non-ascii byte in base64".into(),
            });
        }
    }

    if clean.len() % 4 != 0 {
        return Err(YamlError {
            message: "failed to decode base64 data: Incorrect padding".into(),
        });
    }

    let mut out = Vec::with_capacity((clean.len() / 4) * 3);
    for chunk in clean.chunks_exact(4) {
        let b0 = b64_val(chunk[0])?;
        let b1 = b64_val(chunk[1])?;
        let (b2, pad2) = if chunk[2] == b'=' {
            (0, true)
        } else {
            (b64_val(chunk[2])?, false)
        };
        let (b3, pad3) = if chunk[3] == b'=' {
            (0, true)
        } else {
            (b64_val(chunk[3])?, false)
        };

        if pad2 && !pad3 {
            return Err(YamlError {
                message: "invalid padding".into(),
            });
        }

        out.push((b0 << 2) | (b1 >> 4));
        if !pad2 {
            out.push(((b1 & 0x0f) << 4) | (b2 >> 2));
        }
        if !pad3 {
            out.push(((b2 & 0x03) << 6) | b3);
        }
        if pad2 || pad3 {
            break;
        }
    }
    Ok(out)
}

enum Numeric {
    Int(i128),
    Float(f64),
    Nan,
}

fn as_numeric(v: &YValue) -> Option<Numeric> {
    match v {
        YValue::Bool(true) => Some(Numeric::Int(1)),
        YValue::Bool(false) => Some(Numeric::Int(0)),
        YValue::Int(PyInt::Small(n)) => Some(Numeric::Int(*n)),
        YValue::Int(PyInt::Big(_)) => None,
        YValue::Float(f) => {
            if f.is_nan() {
                Some(Numeric::Nan)
            } else {
                Some(Numeric::Float(*f))
            }
        }
        _ => None,
    }
}

fn python_key_eq(a: &YValue, b: &YValue) -> bool {
    match (a, b) {
        (YValue::Null, YValue::Null) => true,
        (YValue::Int(i1), YValue::Int(i2)) => i1 == i2,
        (YValue::Bool(b1), YValue::Bool(b2)) => b1 == b2,
        (YValue::Str(s1), YValue::Str(s2)) => s1 == s2,
        (YValue::Timestamp { py_str: s1 }, YValue::Timestamp { py_str: s2 }) => s1 == s2,
        (YValue::Binary(b1), YValue::Binary(b2)) => b1 == b2,
        _ => {
            if let (Some(n1), Some(n2)) = (as_numeric(a), as_numeric(b)) {
                match (n1, n2) {
                    (Numeric::Nan, Numeric::Nan) => true,
                    (Numeric::Nan, _) | (_, Numeric::Nan) => false,
                    (Numeric::Float(f1), Numeric::Float(f2)) => f1 == f2,
                    (Numeric::Int(i1), Numeric::Int(i2)) => i1 == i2,
                    (Numeric::Float(f), Numeric::Int(i))
                    | (Numeric::Int(i), Numeric::Float(f)) => {
                        if f.is_finite() && f.fract() == 0.0 {
                            (f as i128) == i && (i as f64) == f
                        } else {
                            false
                        }
                    }
                }
            } else {
                false
            }
        }
    }
}

fn tag_name(tag: &Option<Tag>) -> Option<String> {
    tag.as_ref().and_then(|t| {
        let full = format!("{}{}", t.handle, t.suffix);
        if full == "!" {
            return None;
        }
        if let Some(stripped) = full.strip_prefix("tag:yaml.org,2002:") {
            Some(stripped.to_string())
        } else if let Some(stripped) = full.strip_prefix("!!") {
            Some(stripped.to_string())
        } else if t.handle == "!" && t.suffix.starts_with("tag:yaml.org,2002:") {
            Some(t.suffix["tag:yaml.org,2002:".len()..].to_string())
        } else {
            Some(full)
        }
    })
}

#[derive(Clone, Debug)]
enum AstNode {
    Scalar {
        value: String,
        style: TScalarStyle,
        tag: Option<Tag>,
    },
    Sequence {
        tag: Option<Tag>,
        items: Vec<AstNode>,
    },
    Mapping {
        tag: Option<Tag>,
        pairs: Vec<(AstNode, AstNode)>,
    },
}

/// alias 展開後節點總數上限（PyYAML 以共享參照載入，不受影響；Rust 為樹狀複製，須設上限防指數膨脹）。
const MAX_EXPANDED_NODES: usize = 2_000_000;
/// 事件樹遞迴深度上限（與 eval_node 一致）。
const MAX_DEPTH: usize = 200;

struct AstParser<'a> {
    events: &'a [(Event, usize)],
    chars: &'a [char],
    /// 文件最後一個區塊純量事件的索引，且原文無結尾換行（此時 PyYAML 不補 "\n"，yaml-rust2 會補）。
    eof_block_scalar: Option<usize>,
    pos: usize,
    anchor_map: HashMap<usize, (AstNode, usize)>,
    nodes: usize,
}

impl<'a> AstParser<'a> {
    fn peek(&self) -> Option<&'a Event> {
        self.events.get(self.pos).map(|(e, _)| e)
    }

    fn next(&mut self) -> Result<(&'a Event, usize), YamlError> {
        if self.pos < self.events.len() {
            let (ev, idx) = &self.events[self.pos];
            self.pos += 1;
            Ok((ev, *idx))
        } else {
            Err(YamlError {
                message: "unexpected end of events".to_string(),
            })
        }
    }

    /// 區塊純量 header（`|`／`>` 與 chomping／indent 指示符）是否含 keep（`+`）。
    fn header_has_keep(&self, idx: usize) -> bool {
        let mut i = idx + 1;
        while let Some(&c) = self.chars.get(i) {
            match c {
                '+' => return true,
                '-' | '0'..='9' => i += 1,
                _ => return false,
            }
        }
        false
    }

    /// 回傳 (節點, 展開後節點數)。
    fn parse_node(&mut self, depth: usize) -> Result<(AstNode, usize), YamlError> {
        if depth > MAX_DEPTH {
            return Err(YamlError {
                message: "recursion limit exceeded".into(),
            });
        }
        let (ev, idx) = self.next()?;
        let ev_pos = self.pos - 1;
        let ev = ev.clone();
        match ev {
            Event::Scalar(mut value, style, anchor_id, tag) => {
                if self.eof_block_scalar == Some(ev_pos)
                    && matches!(style, TScalarStyle::Literal | TScalarStyle::Folded)
                    && value.ends_with('\n')
                {
                    value.pop();
                }
                // yaml-rust2 對「無內容、clip／strip chomping」的區塊純量在串流尾端回傳 "\n"；PyYAML 為 ""。
                if matches!(style, TScalarStyle::Literal | TScalarStyle::Folded)
                    && value == "\n"
                    && !self.header_has_keep(idx)
                {
                    value.clear();
                }
                self.nodes += 1;
                let node = AstNode::Scalar { value, style, tag };
                if anchor_id > 0 {
                    self.anchor_map.insert(anchor_id, (node.clone(), 1));
                }
                Ok((node, 1))
            }
            Event::Alias(anchor_id) => {
                if let Some((target, size)) = self.anchor_map.get(&anchor_id) {
                    self.nodes = self.nodes.saturating_add(*size);
                    if self.nodes > MAX_EXPANDED_NODES {
                        return Err(YamlError {
                            message: "alias expansion limit exceeded".into(),
                        });
                    }
                    Ok((target.clone(), *size))
                } else {
                    Err(YamlError {
                        message: format!("found undefined alias {anchor_id}"),
                    })
                }
            }
            Event::SequenceStart(anchor_id, tag) => {
                let mut items = Vec::new();
                let mut size = 1usize;
                self.nodes += 1;
                while self.peek() != Some(&Event::SequenceEnd) {
                    let (n, sz) = self.parse_node(depth + 1)?;
                    size = size.saturating_add(sz);
                    items.push(n);
                }
                self.next()?; // consume SequenceEnd
                let node = AstNode::Sequence { tag, items };
                if anchor_id > 0 {
                    self.anchor_map.insert(anchor_id, (node.clone(), size));
                }
                Ok((node, size))
            }
            Event::MappingStart(anchor_id, tag) => {
                let mut pairs = Vec::new();
                let mut size = 1usize;
                self.nodes += 1;
                while self.peek() != Some(&Event::MappingEnd) {
                    let (k, ksz) = self.parse_node(depth + 1)?;
                    let (v, vsz) = self.parse_node(depth + 1)?;
                    size = size.saturating_add(ksz).saturating_add(vsz);
                    pairs.push((k, v));
                }
                self.next()?; // consume MappingEnd
                let node = AstNode::Mapping { tag, pairs };
                if anchor_id > 0 {
                    self.anchor_map.insert(anchor_id, (node.clone(), size));
                }
                Ok((node, size))
            }
            _ => Err(YamlError {
                message: format!("unexpected event: {:?}", ev),
            }),
        }
    }
}

fn is_merge_key(node: &AstNode) -> bool {
    match node {
        AstNode::Scalar { value, style, tag } => {
            if *style == TScalarStyle::Plain && value == "<<" {
                match tag {
                    None => true,
                    Some(_) => tag_name(tag).as_deref() == Some("merge"),
                }
            } else if tag.is_some() {
                tag_name(tag).as_deref() == Some("merge")
            } else {
                false
            }
        }
        _ => false,
    }
}

fn insert_into_ymap(map: &mut YMap, key: YValue, val: YValue) {
    if let Some((_, slot)) = map.0.iter_mut().find(|(k, _)| python_key_eq(k, &key)) {
        *slot = val;
    } else {
        map.0.push((key, val));
    }
}

fn collect_merge_pairs(
    v_node: &AstNode,
    merge_pairs: &mut Vec<(YValue, YValue)>,
    depth: usize,
) -> Result<(), YamlError> {
    if depth > 200 {
        return Err(YamlError {
            message: "recursion limit exceeded".into(),
        });
    }
    match v_node {
        AstNode::Mapping { .. } => {
            let val = eval_node(v_node, depth + 1)?;
            if let YValue::Map(m) = val {
                merge_pairs.extend(m.0);
                Ok(())
            } else {
                Err(YamlError {
                    message: "expected a mapping or list of mappings for merging".into(),
                })
            }
        }
        AstNode::Sequence { items, .. } => {
            for item in items.iter().rev() {
                if !matches!(item, AstNode::Mapping { .. }) {
                    return Err(YamlError {
                        message: "expected a mapping or list of mappings for merging".into(),
                    });
                }
                let val = eval_node(item, depth + 1)?;
                if let YValue::Map(m) = val {
                    merge_pairs.extend(m.0);
                } else {
                    return Err(YamlError {
                        message: "expected a mapping or list of mappings for merging".into(),
                    });
                }
            }
            Ok(())
        }
        _ => Err(YamlError {
            message: "expected a mapping or list of mappings for merging".into(),
        }),
    }
}

fn eval_scalar(
    value: &str,
    style: TScalarStyle,
    tag: &Option<Tag>,
) -> Result<YValue, YamlError> {
    let tag_n = tag_name(tag);
    if let Some(ref t) = tag_n {
        return match t.as_str() {
            "str" => Ok(YValue::Str(value.to_string())),
            "int" => construct_int(value).map(YValue::Int),
            "float" => construct_float(value).map(YValue::Float),
            "bool" => match value.to_lowercase().as_str() {
                "yes" | "true" | "on" => Ok(YValue::Bool(true)),
                "no" | "false" | "off" => Ok(YValue::Bool(false)),
                _ => Err(YamlError {
                    message: format!("invalid bool value: {value}"),
                }),
            },
            "null" => Ok(YValue::Null),
            "binary" => py_base64_decode(value).map(YValue::Binary),
            "timestamp" => construct_timestamp(value),
            _ => Err(YamlError {
                message: format!("could not determine a constructor for the tag '{t}'"),
            }),
        };
    }

    if style == TScalarStyle::Plain {
        match resolve_plain(value) {
            Some("bool") => match value {
                "yes" | "Yes" | "YES" | "true" | "True" | "TRUE" | "on" | "On" | "ON" => {
                    Ok(YValue::Bool(true))
                }
                "no" | "No" | "NO" | "false" | "False" | "FALSE" | "off" | "Off" | "OFF" => {
                    Ok(YValue::Bool(false))
                }
                _ => unreachable!(),
            },
            Some("null") => Ok(YValue::Null),
            Some("int") => construct_int(value).map(YValue::Int),
            Some("float") => construct_float(value).map(YValue::Float),
            Some("timestamp") => construct_timestamp(value),
            // SafeConstructor 對 tag:yaml.org,2002:merge／value 沒有 constructor（鍵位置另由 eval_key／is_merge_key 處理）。
            Some("merge") | Some("value") => Err(YamlError {
                message: format!("could not determine a constructor for the tag '{}'", resolve_plain(value).unwrap_or("")),
            }),
            _ => Ok(YValue::Str(value.to_string())),
        }
    } else {
        Ok(YValue::Str(value.to_string()))
    }
}

/// mapping key 位置：`=`（tag:yaml.org,2002:value）依 `flatten_mapping` 轉為 str。
fn eval_key(node: &AstNode, depth: usize) -> Result<YValue, YamlError> {
    if let AstNode::Scalar { value, style: TScalarStyle::Plain, tag: None } = node {
        if value == "=" {
            return Ok(YValue::Str("=".to_string()));
        }
    }
    eval_node(node, depth)
}

fn eval_sequence(
    tag: &Option<Tag>,
    items: &[AstNode],
    depth: usize,
) -> Result<YValue, YamlError> {
    if depth > 200 {
        return Err(YamlError {
            message: "recursion limit exceeded".into(),
        });
    }
    let tag_n = tag_name(tag);
    if let Some(ref t) = tag_n {
        if t == "omap" || t == "pairs" {
            let mut seq_items = Vec::new();
            for item in items {
                if let AstNode::Mapping { pairs, .. } = item {
                    if pairs.len() != 1 {
                        return Err(YamlError {
                            message: "expected a single mapping item".into(),
                        });
                    }
                    let k = eval_key(&pairs[0].0, depth + 1)?;
                    let v = eval_node(&pairs[0].1, depth + 1)?;
                    seq_items.push(YValue::Seq(vec![k, v]));
                } else {
                    return Err(YamlError {
                        message: "expected a mapping of length 1".into(),
                    });
                }
            }
            return Ok(YValue::Seq(seq_items));
        } else if t != "seq" {
            return Err(YamlError {
                message: format!("could not determine a constructor for the tag '{t}'"),
            });
        }
    }

    let mut out = Vec::new();
    for item in items {
        out.push(eval_node(item, depth + 1)?);
    }
    Ok(YValue::Seq(out))
}

fn eval_mapping(
    tag: &Option<Tag>,
    pairs: &[(AstNode, AstNode)],
    depth: usize,
) -> Result<YValue, YamlError> {
    if depth > 200 {
        return Err(YamlError {
            message: "recursion limit exceeded".into(),
        });
    }
    let tag_n = tag_name(tag);
    if let Some(ref t) = tag_n {
        if t == "set" {
            let mut set_elements: Vec<YValue> = Vec::new();
            for (k_node, _) in pairs {
                let k_val = eval_key(k_node, depth + 1)?;
                if matches!(k_val, YValue::Seq(_) | YValue::Map(_) | YValue::Set(_)) {
                    return Err(YamlError {
                        message: "found unhashable key".into(),
                    });
                }
                if !set_elements.iter().any(|existing| python_key_eq(existing, &k_val)) {
                    set_elements.push(k_val);
                }
            }
            return Ok(YValue::Set(set_elements));
        } else if t != "map" {
            return Err(YamlError {
                message: format!("could not determine a constructor for the tag '{t}'"),
            });
        }
    }

    let mut merge_pairs: Vec<(YValue, YValue)> = Vec::new();
    let mut own_pairs: Vec<(YValue, YValue)> = Vec::new();

    for (k_node, v_node) in pairs {
        if is_merge_key(k_node) {
            collect_merge_pairs(v_node, &mut merge_pairs, depth + 1)?;
        } else {
            let k_val = eval_key(k_node, depth + 1)?;
            if matches!(k_val, YValue::Seq(_) | YValue::Map(_) | YValue::Set(_)) {
                return Err(YamlError {
                    message: "found unhashable key".into(),
                });
            }
            let v_val = eval_node(v_node, depth + 1)?;
            own_pairs.push((k_val, v_val));
        }
    }

    let mut ymap = YMap::default();
    for (k, v) in merge_pairs {
        insert_into_ymap(&mut ymap, k, v);
    }
    for (k, v) in own_pairs {
        insert_into_ymap(&mut ymap, k, v);
    }
    Ok(YValue::Map(ymap))
}

fn eval_node(node: &AstNode, depth: usize) -> Result<YValue, YamlError> {
    if depth > 200 {
        return Err(YamlError {
            message: "recursion limit exceeded".into(),
        });
    }
    match node {
        AstNode::Scalar { value, style, tag } => eval_scalar(value, *style, tag),
        AstNode::Sequence { tag, items } => eval_sequence(tag, items, depth),
        AstNode::Mapping { tag, pairs } => eval_mapping(tag, pairs, depth),
    }
}

struct EventSink {
    events: Vec<(Event, usize)>,
}

impl MarkedEventReceiver for EventSink {
    fn on_event(&mut self, ev: Event, mark: Marker) {
        self.events.push((ev, mark.index()));
    }
}

/// PyYAML `Reader.NON_PRINTABLE`：`[^\x09\x0A\x0D\x20-\x7E\x85\xA0-퟿-�\U00010000-\U0010ffff]`。
fn check_printable(text: &str) -> Result<(), YamlError> {
    for c in text.chars() {
        let ok = matches!(c,
            '\t' | '\n' | '\r' | '\u{20}'..='\u{7e}' | '\u{85}' | '\u{a0}'..='\u{d7ff}'
            | '\u{e000}'..='\u{fffd}' | '\u{10000}'..='\u{10ffff}');
        if !ok {
            return Err(YamlError {
                message: format!("unacceptable character #x{:04x}: special characters are not allowed", c as u32),
            });
        }
    }
    Ok(())
}

/// 注釋以外的 tab（PyYAML：任何 token 都不能以 tab 起頭，plain scalar 也在 tab 處結束）。
fn gap_has_tab(slice: &[char], before: Option<char>) -> bool {
    let mut in_comment = false;
    for (k, &ch) in slice.iter().enumerate() {
        if in_comment {
            if ch == '\n' || ch == '\r' {
                in_comment = false;
            }
            continue;
        }
        if ch == '#' {
            let prev = if k == 0 { before } else { Some(slice[k - 1]) };
            if prev.map_or(true, |p| matches!(p, ' ' | '\t' | '\n' | '\r')) {
                in_comment = true;
                continue;
            }
        }
        if ch == '\t' {
            return true;
        }
    }
    false
}

/// 引號純量內容的結尾（含收尾引號）之 index；找不到回傳 None。
fn quoted_end(chars: &[char], start: usize, double: bool) -> Option<usize> {
    let mut i = start + 1;
    while i < chars.len() {
        let c = chars[i];
        if double {
            if c == '\\' {
                i += 2;
                continue;
            }
            if c == '"' {
                return Some(i);
            }
        } else if c == '\'' {
            if chars.get(i + 1) == Some(&'\'') {
                i += 2;
                continue;
            }
            return Some(i);
        }
        i += 1;
    }
    None
}

/// 以 yaml-rust2 token 串流補上 PyYAML 與 yaml-rust2 的 scanner 差異（只在「PyYAML 必然拒絕」時回錯）。
fn validate_tokens(text: &str) -> Result<(), YamlError> {
    let chars: Vec<char> = text.chars().collect();
    let mut sc = Scanner::new(text.chars());
    let mut toks: Vec<(usize, TokenType)> = Vec::new();
    while let Some(Token(m, t)) = sc.next() {
        let end = matches!(t, TokenType::StreamEnd);
        toks.push((m.index(), t));
        if end {
            break;
        }
    }
    let err = |m: &str| YamlError { message: m.to_string() };

    // 1. 重複 anchor（每份文件獨立）、directive 規則、起首 `...`。
    let mut seen_anchor = std::collections::HashSet::new();
    let mut seen_version = false;
    let mut seen_handles = std::collections::HashSet::new();
    let mut first_content = true;
    for (_, t) in &toks {
        match t {
            TokenType::StreamStart(_) => {}
            TokenType::VersionDirective(major, _) => {
                if *major != 1 || seen_version {
                    return Err(err("found incompatible or duplicate YAML directive"));
                }
                seen_version = true;
            }
            TokenType::TagDirective(handle, _) => {
                if !seen_handles.insert(handle.clone()) {
                    return Err(err("found duplicate tag handle"));
                }
            }
            TokenType::DocumentEnd => {
                if first_content {
                    return Err(err("expected the node content, but found '<document end>'"));
                }
                seen_anchor.clear();
                seen_version = false;
                seen_handles.clear();
            }
            TokenType::DocumentStart => {
                first_content = false;
                seen_anchor.clear();
                seen_version = false;
                seen_handles.clear();
            }
            TokenType::Anchor(name) => {
                first_content = false;
                if !seen_anchor.insert(name.clone()) {
                    return Err(err("found duplicate anchor"));
                }
            }
            _ => first_content = false,
        }
    }

    // 2. Key／Value 配對與 flow 內 plain scalar 的 PyYAML 專屬規則。
    let mut stack: Vec<(bool, bool)> = Vec::new(); // (pending_key, is_flow)
    for (_, t) in &toks {
        match t {
            TokenType::BlockMappingStart | TokenType::BlockSequenceStart => {
                stack.push((false, false));
                if stack.len() > MAX_DEPTH {
                    return Err(err("recursion limit exceeded"));
                }
            }
            TokenType::FlowMappingStart | TokenType::FlowSequenceStart => {
                stack.push((false, true));
                if stack.len() > MAX_DEPTH {
                    return Err(err("recursion limit exceeded"));
                }
            }
            TokenType::BlockEnd | TokenType::FlowMappingEnd | TokenType::FlowSequenceEnd => {
                stack.pop();
            }
            TokenType::Key => {
                if let Some(top) = stack.last_mut() {
                    top.0 = true;
                }
            }
            TokenType::Value => match stack.last_mut() {
                Some(top) if top.0 => top.0 = false,
                _ => return Err(err("expected a key before ':'")),
            },
            TokenType::Scalar(TScalarStyle::Plain, v) if stack.last().map_or(false, |t| t.1) => {
                if v.contains('?') || v.starts_with(':') || v.starts_with('|') || v.starts_with('>') {
                    return Err(err("found a character that cannot appear in a flow plain scalar"));
                }
            }
            _ => {}
        }
    }

    // 3. tab 規則：區間 [token 起點, 下一個 token 起點) 內，注釋以外不得有 tab；引號與區塊純量內容除外。
    let mut starts: Vec<(usize, Option<TScalarStyle>)> = Vec::new();
    for (idx, t) in &toks {
        let style = if let TokenType::Scalar(st, _) = t { Some(*st) } else { None };
        match starts.last_mut() {
            Some(last) if last.0 == *idx => {
                if last.1.is_none() {
                    last.1 = style;
                }
            }
            _ => starts.push((*idx, style)),
        }
    }
    for (k, (s, style)) in starts.iter().enumerate() {
        let e = starts.get(k + 1).map_or(chars.len(), |n| n.0).min(chars.len());
        let s = (*s).min(chars.len());
        if s >= e {
            continue;
        }
        let before = if s == 0 { None } else { Some(chars[s - 1]) };
        let region_start = match style {
            Some(TScalarStyle::Literal) | Some(TScalarStyle::Folded) => continue,
            Some(TScalarStyle::SingleQuoted) | Some(TScalarStyle::DoubleQuoted) => {
                let double = matches!(style, Some(TScalarStyle::DoubleQuoted));
                match quoted_end(&chars, s, double) {
                    Some(q) => q + 1,
                    None => continue,
                }
            }
            _ => s,
        };
        if region_start < e {
            let b = if region_start == s { before } else { Some(chars[region_start - 1]) };
            if gap_has_tab(&chars[region_start..e], b) {
                return Err(err("found character '\\t' that cannot start any token"));
            }
        }
    }
    Ok(())
}

/// PyYAML 不檢查 flow 集合／引號純量延續行的縮排；yaml-rust2（YAML 1.2）會回 "invalid indentation"。
/// 在 flow 或引號純量內補空白縮排不改變語意（延續行的前導空白本來就會被摺疊掉），故逐行補齊後重試。
fn relax_flow_indent(text: &str) -> String {
    let mut cur: String = text.to_string();
    for _ in 0..10_000 {
        let chars: Vec<char> = cur.chars().collect();
        let mut sc = Scanner::new(cur.chars());
        let mut flow_open: Vec<usize> = Vec::new(); // 目前未關閉的 flow 起點 column
        let mut last_idx = 0usize;
        while let Some(Token(m, t)) = sc.next() {
            last_idx = m.index();
            match t {
                TokenType::FlowSequenceStart | TokenType::FlowMappingStart => flow_open.push(m.col()),
                TokenType::FlowSequenceEnd | TokenType::FlowMappingEnd => {
                    flow_open.pop();
                }
                TokenType::StreamEnd => return cur,
                _ => {}
            }
        }
        let Some(e) = sc.get_error() else { return cur };
        let info = e.info();
        // PyYAML 允許 flow 內單獨的 `-`（後接 , ] } 等）為 plain scalar；yaml-rust2 拒絕。語意等價的改寫是加引號。
        if info.starts_with("plain scalar cannot start with '-'") {
            let idx = e.marker().index().min(chars.len());
            if chars.get(idx) == Some(&'-') {
                let mut out: String = chars[..idx].iter().collect();
                out.push_str("'-'");
                out.extend(chars[idx + 1..].iter());
                cur = out;
                continue;
            }
            return cur;
        }
        if !info.contains("invalid indentation") {
            return cur;
        }
        let in_quoted = info.contains("quoted scalar");
        if flow_open.is_empty() && !in_quoted {
            return cur;
        }
        // 出錯行的行首 index 與目前縮排
        let idx = e.marker().index().min(chars.len());
        let mut ls = idx;
        while ls > 0 && chars[ls - 1] != '\n' && chars[ls - 1] != '\r' {
            ls -= 1;
        }
        let mut indent = 0;
        while ls + indent < chars.len() && chars[ls + indent] == ' ' {
            indent += 1;
        }
        // 目標縮排：外層 flow 起點（或引號起點所在行的縮排）+ 1，保證大於任何父層區塊縮排。
        let target = if let Some(&c) = flow_open.first() { c } else { last_idx_col(&chars, last_idx) + 1 };
        if indent >= target {
            return cur;
        }
        let pad: String = " ".repeat(target - indent);
        let mut out: String = chars[..ls].iter().collect();
        out.push_str(&pad);
        out.extend(chars[ls..].iter());
        cur = out;
    }
    cur
}

fn last_idx_col(chars: &[char], idx: usize) -> usize {
    let idx = idx.min(chars.len());
    let mut i = idx;
    while i > 0 && chars[i - 1] != '\n' && chars[i - 1] != '\r' {
        i -= 1;
    }
    idx - i
}

/// 由事件流建出單一文件的 `YValue`。
pub(super) fn build_document(text: &str) -> Result<YValue, YamlError> {
    let clean_text = text.strip_prefix('\u{feff}').unwrap_or(text);
    check_printable(clean_text)?;
    let relaxed = relax_flow_indent(clean_text);
    let clean_text: &str = &relaxed;
    validate_tokens(clean_text)?;

    let mut parser = Parser::new_from_str(clean_text);
    let mut sink = EventSink { events: Vec::new() };
    parser.load(&mut sink, true).map_err(|e| YamlError {
        message: e.to_string(),
    })?;

    let doc_start_count = sink
        .events
        .iter()
        .filter(|(e, _)| matches!(e, Event::DocumentStart))
        .count();
    if doc_start_count == 0 {
        return Ok(YValue::Null);
    }
    if doc_start_count > 1 {
        return Err(YamlError {
            message: "expected a single document in the stream".to_string(),
        });
    }

    let start_idx = sink
        .events
        .iter()
        .position(|(e, _)| matches!(e, Event::DocumentStart))
        .unwrap();
    let end_idx = sink
        .events
        .iter()
        .rposition(|(e, _)| matches!(e, Event::DocumentEnd))
        .unwrap();
    if start_idx + 1 >= end_idx {
        return Ok(YValue::Null);
    }

    let doc_events = &sink.events[start_idx + 1..end_idx];
    let chars: Vec<char> = clean_text.chars().collect();
    // 原文末端沒有換行、且最後一個節點是區塊純量（最後一行非空白、非注釋）→ 該純量內容止於 EOF，不含結尾換行。
    let eof_block_scalar = {
        let last_line = clean_text.rsplit(['\n', '\r']).next().unwrap_or("");
        let no_final_break = !clean_text.ends_with(['\n', '\r']);
        let t = last_line.trim_start_matches([' ', '\t']);
        let last_is_content = !t.is_empty() && !t.starts_with('#');
        let last_scalar = doc_events.iter().rposition(|(e, _)| matches!(e, Event::Scalar(..)));
        match last_scalar {
            Some(i)
                if no_final_break
                    && last_is_content
                    && matches!(doc_events[i].0, Event::Scalar(_, TScalarStyle::Literal | TScalarStyle::Folded, _, _))
                    && doc_events[i + 1..]
                        .iter()
                        .all(|(e, _)| matches!(e, Event::MappingEnd | Event::SequenceEnd)) =>
            {
                Some(i)
            }
            _ => None,
        }
    };
    let mut ast_parser = AstParser {
        events: doc_events,
        chars: &chars,
        eof_block_scalar,
        pos: 0,
        anchor_map: HashMap::new(),
        nodes: 0,
    };
    let (root, _) = ast_parser.parse_node(0)?;
    eval_node(&root, 0)
}
