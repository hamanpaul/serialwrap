//! `benches.yaml` 載入與 bench 代號解析（P4-T17；逐字對齊 inventory §9.1）。

use std::collections::BTreeMap;

use crate::pycompat::py_strip;
use crate::pyjson::PyInt;
use crate::yaml11::{py_str_of, safe_load, YValue};

const ALLOWED_ROOT_KEYS: &[&str] = &["benches"];
const ALLOWED_ENTRY_KEYS: &[&str] = &[
    "autossh",
    "local_port",
    "remote_socket",
    "ssh_opts",
    "target",
];

/// 一個 bench 代號的已驗證內容。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BenchEntry {
    pub code: String,
    pub target: String,
    pub remote_socket: Option<String>,
    pub remote_port: Option<u16>,
    pub local_port: u16,
    pub ssh_opts: Vec<String>,
    pub autossh: bool,
}

/// bench 設定錯誤：錯誤碼與訊息沿用 Python 基線。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BenchConfigError {
    pub code: &'static str,
    pub message: String,
}

fn invalid(message: impl Into<String>) -> BenchConfigError {
    BenchConfigError {
        code: "INVALID_BENCH_CONFIG",
        message: message.into(),
    }
}

fn unknown(code: &str) -> BenchConfigError {
    BenchConfigError {
        code: "UNKNOWN_BENCH_CODE",
        message: format!("unknown bench code: {code}"),
    }
}

fn mapping<'a>(
    value: &'a YValue,
    context: &str,
) -> Result<&'a [(YValue, YValue)], BenchConfigError> {
    match value {
        YValue::Map(map) => Ok(&map.0),
        _ => Err(invalid(format!("{context} 必須是 object"))),
    }
}

fn sorted_names<'a>(names: impl IntoIterator<Item = &'a str>) -> String {
    let mut names: Vec<&str> = names.into_iter().collect();
    names.sort_unstable();
    names.join(", ")
}

fn string_field<'a>(
    value: &'a YValue,
    field: &str,
    code: &str,
) -> Result<&'a str, BenchConfigError> {
    let YValue::Str(raw) = value else {
        return Err(invalid(format!("bench \"{code}\" 的 {field} 必須是字串")));
    };
    let value = py_strip(raw);
    if value.is_empty() {
        return Err(invalid(format!("bench \"{code}\" 的 {field} 不可為空")));
    }
    Ok(value)
}

fn validate_target(target: &str, code: &str) -> Result<(), BenchConfigError> {
    let mut parts = target.split('@');
    let user = parts.next().unwrap_or_default();
    let host = parts.next().unwrap_or_default();
    if parts.next().is_some()
        || user.is_empty()
        || host.is_empty()
        || py_strip(user) != user
        || py_strip(host) != host
    {
        return Err(invalid(format!(
            "bench \"{code}\" 的 target 必須是 user@host 形式"
        )));
    }
    Ok(())
}

fn parse_entry(code: &str, raw: &YValue) -> Result<BenchEntry, BenchConfigError> {
    let fields = mapping(raw, &format!("bench \"{code}\""))?;
    let mut present = BTreeMap::new();
    let mut unexpected = Vec::new();
    for (key, value) in fields {
        if let YValue::Str(key) = key {
            if ALLOWED_ENTRY_KEYS.contains(&key.as_str()) {
                present.insert(key.as_str(), value);
                continue;
            }
        }
        unexpected.push(py_str_of(key));
    }
    if !unexpected.is_empty() {
        unexpected.sort();
        return Err(invalid(format!(
            "bench \"{code}\" 包含不支援欄位 {}；benches.yaml 必須維持 provider-neutral，只能使用 {}",
            unexpected.join(", "),
            sorted_names(ALLOWED_ENTRY_KEYS.iter().copied()),
        )));
    }

    let missing: Vec<&str> = ALLOWED_ENTRY_KEYS
        .iter()
        .copied()
        .filter(|key| !present.contains_key(key))
        .collect();
    if !missing.is_empty() {
        return Err(invalid(format!(
            "bench \"{code}\" 缺少必要欄位 {}",
            sorted_names(missing),
        )));
    }

    let target = string_field(present["target"], "target", code)?;
    validate_target(target, code)?;
    let remote_socket = string_field(present["remote_socket"], "remote_socket", code)?;
    let local_port = match present["local_port"] {
        YValue::Int(PyInt::Small(port)) if (1..=65535).contains(port) => *port as u16,
        YValue::Int(_) => {
            return Err(invalid(format!(
                "bench \"{code}\" 的 local_port 必須介於 1 到 65535"
            )))
        }
        _ => {
            return Err(invalid(format!(
                "bench \"{code}\" 的 local_port 必須是整數"
            )))
        }
    };
    let ssh_opts = match present["ssh_opts"] {
        YValue::Seq(options) => {
            let mut result = Vec::with_capacity(options.len());
            for option in options {
                let YValue::Str(option) = option else {
                    return Err(invalid(format!(
                        "bench \"{code}\" 的 ssh_opts 必須是字串陣列"
                    )));
                };
                result.push(option.clone());
            }
            result
        }
        _ => {
            return Err(invalid(format!(
                "bench \"{code}\" 的 ssh_opts 必須是字串陣列"
            )))
        }
    };
    let autossh = match present["autossh"] {
        YValue::Bool(value) => *value,
        _ => return Err(invalid(format!("bench \"{code}\" 的 autossh 必須是布林值"))),
    };

    Ok(BenchEntry {
        code: code.to_string(),
        target: target.to_string(),
        remote_socket: Some(remote_socket.to_string()),
        remote_port: None,
        local_port,
        ssh_opts,
        autossh,
    })
}

/// 解析 `benches.yaml` 全文（YAML 文字）。
pub fn load_benches(text: &str) -> Result<BTreeMap<String, BenchEntry>, BenchConfigError> {
    let loaded = safe_load(text).map_err(|error| BenchConfigError {
        code: "INTERNAL_ERROR",
        message: error.to_string(),
    })?;
    let root = if matches!(&loaded, YValue::Null) {
        Vec::new()
    } else {
        mapping(&loaded, "benches.yaml")?.to_vec()
    };

    let mut unknown_roots = Vec::new();
    for (key, _) in &root {
        if !matches!(key, YValue::Str(value) if ALLOWED_ROOT_KEYS.contains(&value.as_str())) {
            unknown_roots.push(py_str_of(key));
        }
    }
    if !unknown_roots.is_empty() {
        unknown_roots.sort();
        return Err(invalid(format!(
            "benches.yaml 包含不支援根欄位 {}；只能使用 benches",
            unknown_roots.join(", "),
        )));
    }

    let benches_value = root.iter().find_map(|(key, value)| {
        matches!(key, YValue::Str(key) if key == "benches").then_some(value)
    });
    let empty = YValue::Map(Default::default());
    let entries = mapping(benches_value.unwrap_or(&empty), "benches.yaml 的 benches")?;

    let mut benches = BTreeMap::new();
    for (raw_code, raw_entry) in entries {
        let YValue::Str(raw_code) = raw_code else {
            return Err(invalid("bench \"<root>\" 的 code 必須是字串"));
        };
        let code = py_strip(raw_code);
        if code.is_empty() {
            return Err(invalid("bench \"<root>\" 的 code 不可為空"));
        }
        benches.insert(code.to_string(), parse_entry(code, raw_entry)?);
    }
    Ok(benches)
}

/// 依代號取出 bench；未知代號 → `UNKNOWN_BENCH_CODE`。
pub fn resolve<'a>(
    benches: &'a BTreeMap<String, BenchEntry>,
    code: &str,
) -> Result<&'a BenchEntry, BenchConfigError> {
    benches.get(code).ok_or_else(|| unknown(code))
}
