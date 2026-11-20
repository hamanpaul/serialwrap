//! 對齊 `sw_core/source_policy.py` 的外部來源檢查與分類。

use crate::error_codes::ErrorCode;
use crate::pycompat::py_strip;
use crate::pyjson::{PyDict, PyValue};

pub const SOURCE_CLASS_HUMAN: &str = "human";
pub const SOURCE_CLASS_AGENT: &str = "agent";
pub const DEFAULT_HUMAN_NAME: &str = "cli";
const RESERVED_PREFIXES: [&str; 1] = ["system"];

const SOURCE_HINT: &str = " 與 `system:*` 是 daemon 內部保留的來源前綴（WAL 歸因與內部恢復旗標），外部呼叫不可使用。自動化請用 `agent:<名>`，人工操作請用 `human:<名>`；逾時行為差異見 skill「`--source` 的語意」。";

/// `check_source` 的結果；錯誤非空代表外部輸入必須在呼叫 daemon 前拒絕。
pub struct SourceCheck {
    pub source: String,
    pub normalized_from: Option<String>,
    pub error: Option<PyDict>,
    pub raw_source: Option<String>,
}

impl SourceCheck {
    /// 只辨認外部原始輸入的空名 human，不從正規化後字串推定權限。
    pub fn is_bare_human(&self) -> bool {
        let Some(raw) = self.raw_source.as_deref() else {
            return false;
        };
        let (head, tail) = split_source(py_strip(raw));
        casefold(head) == "human" && tail.is_none_or(|value| py_strip(value).is_empty())
    }
}

/// 與 Python `classify_source` 相同：只認大小寫完全相同的 `human:` 前綴。
pub fn classify_source(source: &str) -> &'static str {
    if source.starts_with("human:") {
        SOURCE_CLASS_HUMAN
    } else {
        SOURCE_CLASS_AGENT
    }
}

/// 驗證來源字串；外部 CLI 只使用 `error` 做拒絕判斷，RPC 仍傳原始輸入給 daemon。
pub fn check_source(
    raw: Option<&str>,
    default: &str,
    field: &str,
    normalize_human: bool,
) -> SourceCheck {
    let text = raw.unwrap_or_default().to_string();
    if py_strip(&text).is_empty() {
        return SourceCheck {
            source: default.to_string(),
            normalized_from: None,
            error: None,
            raw_source: Some(text),
        };
    }

    let (head, tail) = split_source(&text);
    let token = casefold(py_strip(head));
    if RESERVED_PREFIXES.contains(&token.as_str()) {
        let mut error = PyDict::new();
        error.insert("ok", PyValue::Bool(false));
        error.insert(
            "error_code",
            PyValue::Str(ErrorCode::SourceReserved.as_str().to_string()),
        );
        error.insert("field", PyValue::Str(field.to_string()));
        error.insert("value", PyValue::Str(text.clone()));
        error.insert("reserved_prefix", PyValue::Str(token.clone()));
        error.insert("hint", PyValue::Str(format!("`{token}`{SOURCE_HINT}")));
        return SourceCheck {
            source: text.clone(),
            normalized_from: None,
            error: Some(error),
            raw_source: Some(text),
        };
    }

    if token == "human" && normalize_human {
        let name = tail.map(py_strip).filter(|name| !name.is_empty());
        let canonical = format!("human:{}", name.unwrap_or(DEFAULT_HUMAN_NAME));
        let normalized_from = (canonical != text).then(|| text.clone());
        return SourceCheck {
            source: canonical,
            normalized_from,
            error: None,
            raw_source: Some(text),
        };
    }

    SourceCheck {
        source: text.clone(),
        normalized_from: None,
        error: None,
        raw_source: Some(text),
    }
}

fn split_source(source: &str) -> (&str, Option<&str>) {
    match source.split_once(':') {
        Some((head, tail)) => (head, Some(tail)),
        None => (source, None),
    }
}

/// 對照 Python `casefold()` 的來源 token；除一般 Unicode 小寫外，`system` 比對
/// 也需涵蓋長 s 與會展開成 `st` 的兩個拉丁連字。
fn casefold(value: &str) -> String {
    let mut folded = String::new();
    for ch in value.chars() {
        match ch {
            '\u{017f}' => folded.push('s'),
            '\u{fb05}' | '\u{fb06}' => folded.push_str("st"),
            _ => folded.extend(ch.to_lowercase()),
        }
    }
    folded
}
