//! Release manifest 與 current-release 解析（P3-T7／P4-T11；arch §10.1）。
//!
//! 保留 manifest 完整欄位供下游檢查，同時驗證必要結構及 P4 新增的 optional metadata 型別。

use crate::pyjson::{self, PyDict, PyInt, PyValue};
use std::fmt::{self, Display, Formatter};

/// 已驗證的 release manifest。
#[derive(Clone, Debug, PartialEq)]
pub struct ReleaseManifest {
    pub schema_version: i64,
    pub bundle_version: String,
    pub stage: String,
    pub target: String,
    pub git_revision: String,
    pub python_cli_delegate_entry: Vec<String>,
    /// Python daemon 的狀態格式版本；舊 manifest 可省略。
    pub state_schema: Option<String>,
    /// Task Scheduler XML 未代入樣板的 SHA-256；舊 manifest 可省略。
    pub task_xml_template_sha256: Option<String>,
    /// 頂層物件的完整內容（保留未知欄位）。
    pub raw: PyDict,
}

/// `current-release.json` 的內容。
#[derive(Clone, Debug, PartialEq)]
pub struct CurrentRelease {
    pub bundle_version: String,
    pub dir: String,
}

/// manifest 解析錯誤。
#[derive(Clone, Debug, PartialEq)]
pub enum ManifestError {
    /// 非合法 JSON（內容為 `pyjson` 的錯誤訊息）。
    InvalidJson(String),
    /// JSON 合法但不符合結構（內容為說明）。
    Schema(String),
}

impl Display for ManifestError {
    fn fmt(&self, f: &mut Formatter<'_>) -> fmt::Result {
        match self {
            ManifestError::InvalidJson(message) => write!(f, "manifest JSON 無效：{message}"),
            ManifestError::Schema(message) => write!(f, "manifest 結構無效：{message}"),
        }
    }
}

fn schema_err(msg: &str) -> ManifestError {
    ManifestError::Schema(msg.to_string())
}

fn parse_json_object(text: &str) -> Result<PyDict, ManifestError> {
    match pyjson::loads(text) {
        Err(e) => Err(ManifestError::InvalidJson(e.to_string())),
        Ok(PyValue::Dict(d)) => Ok(d),
        Ok(_) => Err(schema_err("頂層必須為 JSON 物件")),
    }
}

fn str_field(d: &PyDict, key: &str) -> Result<String, ManifestError> {
    match d.get(key) {
        Some(PyValue::Str(s)) => Ok(s.clone()),
        _ => Err(ManifestError::Schema(format!("欄位 {key} 缺少或非字串"))),
    }
}

fn optional_str_path(root: &PyDict, path: &[&str]) -> Result<Option<String>, ManifestError> {
    let Some((first, rest)) = path.split_first() else {
        return Ok(None);
    };
    let mut value = root.get(first);
    for key in rest {
        let Some(PyValue::Dict(parent)) = value else {
            return Ok(None);
        };
        value = parent.get(key);
    }
    match value {
        None => Ok(None),
        Some(PyValue::Str(s)) => Ok(Some(s.clone())),
        Some(_) => Err(schema_err(&format!("欄位 {} 必須為字串", path.join(".")))),
    }
}

fn is_sha256_hex(value: &str) -> bool {
    value.len() == 64
        && value
            .as_bytes()
            .iter()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(b))
}

/// 解析並驗證 release manifest（JSON 文字）。
pub fn parse_manifest(text: &str) -> Result<ReleaseManifest, ManifestError> {
    let root = parse_json_object(text)?;

    let schema_version = match root.get("schema_version") {
        Some(PyValue::Int(PyInt::Small(1))) => 1,
        Some(_) => return Err(schema_err("schema_version 必須為 1")),
        None => return Err(schema_err("缺少 schema_version")),
    };

    let bundle_version = str_field(&root, "bundle_version")?;
    let stage = str_field(&root, "stage")?;
    let target = str_field(&root, "target")?;
    let git_revision = str_field(&root, "git_revision")?;
    if let Some(pairing) = root.get("pairing") {
        let PyValue::Dict(pairing) = pairing else {
            return Err(schema_err("欄位 pairing 必須為物件"));
        };
        if !matches!(pairing.get("rule"), Some(PyValue::Str(rule)) if rule == "exact") {
            return Err(schema_err("欄位 pairing.rule 必須為 exact"));
        }
    }
    let state_schema = optional_str_path(&root, &["components", "daemon", "state_schema"])?;
    let task_xml_template_sha256 = optional_str_path(
        &root,
        &["components", "supervisor", "task_xml_template_sha256"],
    )?;
    if task_xml_template_sha256
        .as_deref()
        .is_some_and(|digest| !is_sha256_hex(digest))
    {
        return Err(schema_err(
            "欄位 components.supervisor.task_xml_template_sha256 必須為 64 位小寫十六進位",
        ));
    }

    let components = match root.get("components") {
        Some(PyValue::Dict(d)) => d,
        _ => return Err(schema_err("缺少 components 物件")),
    };
    let delegate = match components.get("python_cli_delegate") {
        Some(PyValue::Dict(d)) => d,
        _ => return Err(schema_err("缺少 components.python_cli_delegate 物件")),
    };
    let entry = match delegate.get("entry") {
        Some(PyValue::List(items)) => items,
        Some(_) => {
            return Err(schema_err(
                "components.python_cli_delegate.entry 必須為 list",
            ))
        }
        None => return Err(schema_err("缺少 components.python_cli_delegate.entry")),
    };
    if entry.is_empty() {
        return Err(schema_err("components.python_cli_delegate.entry 不得為空"));
    }
    let mut python_cli_delegate_entry = Vec::with_capacity(entry.len());
    for item in entry {
        match item {
            PyValue::Str(s) => python_cli_delegate_entry.push(s.clone()),
            _ => {
                return Err(schema_err(
                    "components.python_cli_delegate.entry 含非字串項目",
                ))
            }
        }
    }

    Ok(ReleaseManifest {
        schema_version,
        bundle_version,
        stage,
        target,
        git_revision,
        python_cli_delegate_entry,
        state_schema,
        task_xml_template_sha256,
        raw: root,
    })
}

/// 解析 `current-release.json`（欄位 `bundle_version`、`dir`，皆為字串）。
pub fn parse_current_release(text: &str) -> Result<CurrentRelease, ManifestError> {
    let root = parse_json_object(text)?;
    Ok(CurrentRelease {
        bundle_version: str_field(&root, "bundle_version")?,
        dir: str_field(&root, "dir")?,
    })
}

/// `components.com0com` 選用元件（P4-T19，SC-36，OD-15）：只在 Windows bundle 出現，缺席為合法狀態。
///
/// 紅燈階段（TDD）：欄位依計畫 Task 19 Produces；解析本體與 `com0com()` 由 P4-T19 實作。
#[derive(Clone, Debug, PartialEq)]
pub struct Com0comComponent {
    pub optional: bool,
    pub default_selected: bool,
    pub requires_elevation: bool,
    pub experimental: bool,
    pub version: String,
    pub sha256: String,
    /// 固定為 `"GPL-2.0"`。
    pub license: String,
    pub license_file: String,
    pub source_offer_file: String,
    pub provenance_file: String,
    pub helper: String,
    /// 預設 pair（`("COM20", "COM21")`）。
    pub default_pair: (String, String),
}

impl ReleaseManifest {
    /// 取得 `components.daemon.state_schema`；舊 manifest 回傳 `None`。
    pub fn state_schema(&self) -> Option<&str> {
        self.state_schema.as_deref()
    }

    /// 取得 `components.supervisor.task_xml_template_sha256`；舊 manifest 回傳 `None`。
    pub fn task_xml_template_sha256(&self) -> Option<&str> {
        self.task_xml_template_sha256.as_deref()
    }

    /// 取得 `components.com0com`；缺席回 `None`。
    pub fn com0com(&self) -> Option<Com0comComponent> {
        todo!("P4-T19: components.com0com 解析尚未實作")
    }
}
