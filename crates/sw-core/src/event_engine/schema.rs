//! 規則 schema 驗證（P7 Task 11；`event_engine/schema.py`，09 §3.1）。

use sw_proto::pycompat::{py_int_parse_checked, py_truthy};
use sw_proto::pyfmt::py_str;
use sw_proto::pyjson::{PyDict, PyInt, PyValue};
use sw_proto::pyre::{self, redos, CompileError, PyFlags};

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
        int_sat(&self.cooldown_ms)
    }
    /// 夾到 `Duration::MAX`（永不到期）。
    pub fn timeout(&self) -> std::time::Duration {
        let ms = int_sat(&self.timeout_ms);
        if ms <= 0 {
            return std::time::Duration::ZERO;
        }
        let secs = ms / 1000;
        match u64::try_from(secs) {
            Ok(s) => std::time::Duration::new(s, ((ms % 1000) as u32) * 1_000_000),
            Err(_) => std::time::Duration::MAX,
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum RuleError {
    /// RuleSchemaError。
    Schema(String),
    /// 其他 ValueError。registry 一併攔截的 OverflowError（`int(inf)`、regex 重複次數溢位）與
    /// RecursionError（regex 巢狀過深）也歸此類：呼叫端只取 `str(exc)`。
    Value(String),
    /// TypeError。
    Type(String),
}

const VALID_KIND: [&str; 2] = ["agent", "tool"];
const VALID_SCOPE: [&str; 3] = ["any", "command_output", "spontaneous"];
const VALID_LEVEL: [&str; 6] = ["CRITL", "ENMR", "ERR", "INFO", "NOTYS", "WARN"];
const VALID_PATTERN_KIND: [&str; 2] = ["contains", "regex"];
/// `_REGEX_MAX_LEN`（以 Python 字元數計）。
const REGEX_MAX_LEN: usize = 512;
const REDOS_MESSAGE: &str =
    "pattern.value 結構上可能 catastrophic backtracking（ReDoS：≥2 個回溯量詞／backreference／\
量詞單元含 alternation 或巢狀量詞）；無 google-re2 時一律拒絕。請改寫為單一非歧義量詞形式，\
或安裝 google-re2（pip install 'serialwrap[redos]'）以線性引擎免疫此限制";

fn require(cond: bool, msg: impl Into<String>) -> Result<(), RuleError> {
    if cond {
        Ok(())
    } else {
        Err(RuleError::Schema(msg.into()))
    }
}

/// `"['a', 'b']"`：`sorted(set)` 的 repr（集合元素皆為無需跳脫的 ASCII）。
fn sorted_repr(items: &[&str]) -> String {
    let quoted: Vec<String> = items.iter().map(|s| format!("'{s}'")).collect();
    format!("[{}]", quoted.join(", "))
}

/// `value in {…str…}`：只有 str 可能相等；list／dict 不可雜湊 → TypeError。
fn in_str_set(v: Option<&PyValue>, set: &[&str]) -> Result<bool, RuleError> {
    match v {
        Some(PyValue::Str(s)) => Ok(set.contains(&s.as_str())),
        Some(PyValue::List(_)) => Err(RuleError::Type("unhashable type: 'list'".into())),
        Some(PyValue::Dict(_)) => Err(RuleError::Type("unhashable type: 'dict'".into())),
        _ => Ok(false),
    }
}

/// `_NAME_RE.match(s)`：`$` 允許尾端單一 `\n`。
fn name_matches(s: &str) -> bool {
    let body = s.strip_suffix('\n').unwrap_or(s);
    let mut chars = body.chars();
    let is_head = |c: char| c.is_ascii_lowercase() || c.is_ascii_digit();
    match chars.next() {
        Some(c) if is_head(c) => {}
        _ => return false,
    }
    let rest: Vec<char> = chars.collect();
    rest.len() <= 63 && rest.iter().all(|&c| is_head(c) || c == '-')
}

/// `str(obj.get(k) or "")`。
fn str_or_empty(v: Option<&PyValue>) -> String {
    match v {
        Some(v) if py_truthy(v) => py_str(v),
        _ => String::new(),
    }
}

/// `str(obj.get(k, default))`。
fn str_or(v: Option<&PyValue>, default: &str) -> String {
    v.map_or_else(|| default.to_string(), py_str)
}

fn type_name(v: &PyValue) -> &'static str {
    match v {
        PyValue::Null => "NoneType",
        PyValue::Bool(_) => "bool",
        PyValue::Int(_) => "int",
        PyValue::Float(_) => "float",
        PyValue::Str(_) => "str",
        PyValue::List(_) => "list",
        PyValue::Dict(_) => "dict",
    }
}

/// `int(v)`（JSON 值可出現的型別）。
fn py_int(v: &PyValue) -> Result<PyInt, RuleError> {
    match v {
        PyValue::Int(i) => Ok(i.clone()),
        PyValue::Bool(b) => Ok(PyInt::Small(i128::from(*b))),
        PyValue::Float(f) => float_to_int(*f),
        PyValue::Str(s) => py_int_parse_checked(s).map_err(|e| RuleError::Value(e.py_message(s))),
        other => Err(RuleError::Type(format!(
            "int() argument must be a string, a bytes-like object or a real number, not '{}'",
            type_name(other)
        ))),
    }
}

/// `int(float)`：向零截斷，超出 i128 時以精確十進位字串表示。
fn float_to_int(f: f64) -> Result<PyInt, RuleError> {
    if f.is_nan() {
        return Err(RuleError::Value(
            "cannot convert float NaN to integer".into(),
        ));
    }
    if f.is_infinite() {
        return Err(RuleError::Value(
            "cannot convert float infinity to integer".into(),
        ));
    }
    let t = f.trunc();
    if t.abs() < 1.0e38 {
        return Ok(PyInt::Small(t as i128));
    }
    // |t| ≥ 2^126：必為整數，= mantissa × 2^exp（exp > 0）。以 10^9 為基底的大數做精確轉換。
    let bits = t.abs().to_bits();
    let exp = ((bits >> 52) & 0x7ff) as i32 - 1075;
    let mantissa = (bits & ((1u64 << 52) - 1)) | (1u64 << 52);
    let mut limbs: Vec<u64> = vec![mantissa % 1_000_000_000, mantissa / 1_000_000_000];
    for _ in 0..exp {
        let mut carry = 0u64;
        for limb in &mut limbs {
            let v = *limb * 2 + carry;
            *limb = v % 1_000_000_000;
            carry = v / 1_000_000_000;
        }
        if carry > 0 {
            limbs.push(carry);
        }
    }
    while limbs.len() > 1 && limbs.last() == Some(&0) {
        limbs.pop();
    }
    let mut s = String::new();
    if t < 0.0 {
        s.push('-');
    }
    let mut it = limbs.iter().rev();
    if let Some(top) = it.next() {
        s.push_str(&top.to_string());
    }
    for limb in it {
        s.push_str(&format!("{limb:09}"));
    }
    Ok(PyInt::Big(s))
}

fn int_is_negative(i: &PyInt) -> bool {
    match i {
        PyInt::Small(n) => *n < 0,
        PyInt::Big(s) => s.starts_with('-'),
    }
}

fn int_is_positive(i: &PyInt) -> bool {
    match i {
        PyInt::Small(n) => *n > 0,
        PyInt::Big(s) => !s.starts_with('-'),
    }
}

fn int_sat(i: &PyInt) -> i128 {
    match i {
        PyInt::Small(n) => *n,
        PyInt::Big(s) if s.starts_with('-') => i128::MIN,
        PyInt::Big(_) => i128::MAX,
    }
}

/// `obj.get("schema_version") == 1`（Python 數值相等：1、1.0、True 皆成立）。
fn eq_one(v: Option<&PyValue>) -> bool {
    match v {
        Some(PyValue::Int(PyInt::Small(1))) | Some(PyValue::Bool(true)) => true,
        Some(PyValue::Float(f)) => *f == 1.0,
        _ => false,
    }
}

/// pyre 編譯／解析錯誤 → Python 例外類別。`re.error` 轉為 RuleSchemaError，其餘依 registry 的攔截集合歸類。
fn regex_error(err: CompileError) -> RuleError {
    match err {
        CompileError::Re(e) if e.value_error => RuleError::Value(e.msg),
        CompileError::Re(e) => {
            RuleError::Schema(format!("pattern.value is not a valid regex: {e}"))
        }
        CompileError::Overflow(m) => RuleError::Value(m),
        CompileError::Recursion => RuleError::Value("maximum recursion depth exceeded".into()),
        // 語法有效（Python 可編譯）但本引擎無法忠實執行：schema 層與 Python 同樣放行。
        CompileError::Untranslatable(_) => unreachable!("呼叫端已先過濾"),
    }
}

/// 編譯 regex；`Untranslatable` 視同成功（Python 端 `re.compile` 成功）。
fn check_compiles(value: &str, flags: PyFlags) -> Result<(), RuleError> {
    match pyre::compile(value, flags) {
        Ok(_) | Err(CompileError::Untranslatable(_)) => Ok(()),
        Err(e) => Err(regex_error(e)),
    }
}

pub fn validate_rule_dict(obj: &PyValue) -> Result<Rule, RuleError> {
    let PyValue::Dict(d) = obj else {
        return Err(RuleError::Schema("rule must be a JSON object".into()));
    };
    require(eq_one(d.get("schema_version")), "schema_version must be 1")?;
    let owner = str_or_empty(d.get("owner"));
    let name = str_or_empty(d.get("name"));
    require(name_matches(&owner), "owner must match [a-z0-9-]{1,64}")?;
    require(name_matches(&name), "name must match [a-z0-9-]{1,64}")?;
    let derived_id = format!("{owner}.{name}");
    let rid_ok = match d.get("rule_id") {
        None => true,
        Some(v) => *v == derived_id,
    };
    // Python 以 f-string `'{derived_id}'` 直接嵌入（非 repr）：名稱允許的尾端換行原樣出現在訊息中。
    require(rid_ok, format!("rule_id must equal '{derived_id}'"))?;

    require(
        in_str_set(d.get("kind"), &VALID_KIND)?,
        format!("kind must be one of {}", sorted_repr(&VALID_KIND)),
    )?;
    let kind = d["kind"].as_str().unwrap_or_default().to_string();

    let selectors_raw = match d.get("selectors") {
        Some(PyValue::List(l)) if !l.is_empty() => l,
        _ => {
            return Err(RuleError::Schema(
                "selectors must be a non-empty list".into(),
            ))
        }
    };
    let mut selectors = Vec::with_capacity(selectors_raw.len());
    for s in selectors_raw {
        match s {
            PyValue::Str(s) if !s.is_empty() => selectors.push(s.clone()),
            _ => {
                return Err(RuleError::Schema(
                    "each selector must be a non-empty string".into(),
                ))
            }
        }
    }

    let profile = str_or(d.get("profile"), "ALL");
    let level = str_or(d.get("level"), "INFO");
    require(
        VALID_LEVEL.contains(&level.as_str()),
        format!("level must be one of {}", sorted_repr(&VALID_LEVEL)),
    )?;

    let Some(PyValue::Dict(pat_raw)) = d.get("pattern") else {
        return Err(RuleError::Schema("pattern must be an object".into()));
    };
    let pflags = str_or(pat_raw.get("flags"), "");
    require(
        in_str_set(pat_raw.get("kind"), &VALID_PATTERN_KIND)?,
        "pattern.kind must be 'contains' or 'regex'",
    )?;
    let pkind = pat_raw["kind"].as_str().unwrap_or_default().to_string();
    let pvalue = match pat_raw.get("value") {
        Some(PyValue::Str(s)) if !s.is_empty() => s.clone(),
        _ => {
            return Err(RuleError::Schema(
                "pattern.value must be non-empty string".into(),
            ))
        }
    };
    if pkind == "regex" {
        require(
            pvalue.chars().count() <= REGEX_MAX_LEN,
            format!("pattern.value regex 過長（上限 {REGEX_MAX_LEN} 字元，避免 ReDoS）"),
        )?;
        let pflags_int = flags_from_string(&pflags)?;
        check_compiles(&pvalue, pflags_int)?;
        // 本實作無 google-re2：一律落標準 re 路徑，結構上可疑即 fail-closed（EVT-D-S15）。
        let suspicious = match redos::maybe_redos_suspicious(&pvalue, pflags_int) {
            Ok(b) => b,
            Err(CompileError::Untranslatable(_)) => false,
            Err(e) => return Err(regex_error(e)),
        };
        require(!suspicious, REDOS_MESSAGE)?;
    } else {
        flags_from_string(&pflags)?;
    }
    let pattern = Pattern {
        kind: pkind,
        value: pvalue,
        flags: pflags,
    };

    let scope = str_or(d.get("scope"), "spontaneous");
    require(
        VALID_SCOPE.contains(&scope.as_str()),
        format!("scope must be one of {}", sorted_repr(&VALID_SCOPE)),
    )?;

    let max_fires = match d.get("max_fires") {
        None | Some(PyValue::Null) => None,
        Some(v @ PyValue::Bool(_)) => Some(v.clone()),
        Some(v @ PyValue::Int(i)) if !int_is_negative(i) => Some(v.clone()),
        Some(_) => {
            return Err(RuleError::Schema(
                "max_fires must be null or non-negative int".into(),
            ))
        }
    };
    let cooldown_ms = match d.get("cooldown_ms") {
        None => PyInt::Small(0),
        Some(v) => py_int(v)?,
    };
    let timeout_ms = match d.get("timeout_ms") {
        None => PyInt::Small(10000),
        Some(v) => py_int(v)?,
    };
    require(!int_is_negative(&cooldown_ms), "cooldown_ms must be >= 0")?;
    require(int_is_positive(&timeout_ms), "timeout_ms must be > 0")?;

    let Some(PyValue::Dict(h_raw)) = d.get("handler") else {
        return Err(RuleError::Schema("handler must be an object".into()));
    };
    require(
        h_raw.get_truthy("exec") ^ h_raw.get_truthy("shell"),
        "handler must have exactly one of 'exec' (list[str]) or 'shell' (str)",
    )?;
    let handler = match h_raw.get("exec") {
        Some(h_exec) if !h_exec.is_null() => {
            let items = match h_exec {
                PyValue::List(l) => l
                    .iter()
                    .map(|x| match x {
                        PyValue::Str(s) if !s.is_empty() => Some(s.clone()),
                        _ => None,
                    })
                    .collect::<Option<Vec<String>>>(),
                _ => None,
            };
            let Some(items) = items else {
                return Err(RuleError::Schema(
                    "handler.exec must be a non-empty list of non-empty strings".into(),
                ));
            };
            Handler {
                exec: Some(items),
                shell: None,
            }
        }
        _ => match h_raw.get("shell") {
            Some(PyValue::Str(s)) if !s.is_empty() => Handler {
                exec: None,
                shell: Some(s.clone()),
            },
            _ => {
                return Err(RuleError::Schema(
                    "handler.shell must be non-empty string".into(),
                ))
            }
        },
    };

    Ok(Rule {
        schema_version: 1,
        owner,
        name,
        rule_id: derived_id,
        kind,
        selectors,
        profile,
        level,
        pattern,
        scope,
        max_fires,
        cooldown_ms,
        timeout_ms,
        handler,
        auto_enable_com_on_load: d.get("auto_enable_com_on_load").is_none_or(py_truthy),
        debug: d.get("debug").is_some_and(py_truthy),
        raw: d.clone(),
    })
}

/// i/s/m；其他 → `Schema("unsupported regex flag: {ch}")`（DF-08）。
pub fn flags_from_string(s: &str) -> Result<PyFlags, RuleError> {
    PyFlags::from_rule_flags(s)
        .map_err(|ch| RuleError::Schema(format!("unsupported regex flag: {ch}")))
}

pub const NAME_RE: &str = r"^[a-z0-9][a-z0-9-]{0,63}$";

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn float_to_int_matches_python_exact_digits() {
        assert_eq!(
            float_to_int(1e40).unwrap(),
            PyInt::Big("10000000000000000303786028427003666890752".into())
        );
        assert_eq!(
            float_to_int(-3.7e38).unwrap(),
            PyInt::Big("-370000000000000012449008871413764849664".into())
        );
        assert_eq!(float_to_int(-2.9).unwrap(), PyInt::Small(-2));
        assert!(matches!(float_to_int(f64::NAN), Err(RuleError::Value(_))));
    }
}
