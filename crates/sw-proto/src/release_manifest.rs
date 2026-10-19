//! Release manifest 與 current-release 解析（P3-T7；arch §10.1）。
//!
//! 結構驗證依照計畫文字：`schema_version` 必須為整數 1；`components.python_cli_delegate.entry` 必須為非空、
//! 元素皆為字串的 list。`contracts/release_manifest.schema.json` 為 P1-T18 交付物，本 worktree 尚未存在；
//! 此處以計畫文字實作，缺口由 `tests/release_manifest.rs::test_schema_contract_present` 明示。

use crate::pyjson::{self, PyDict, PyInt, PyValue};

/// 已驗證的 release manifest。
#[derive(Clone, Debug, PartialEq)]
pub struct ReleaseManifest {
    pub schema_version: i64,
    pub bundle_version: String,
    pub stage: String,
    pub target: String,
    pub git_revision: String,
    pub python_cli_delegate_entry: Vec<String>,
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
    /// 取得 `components.com0com`；缺席回 `None`。
    pub fn com0com(&self) -> Option<Com0comComponent> {
        todo!("P4-T19: components.com0com 解析尚未實作")
    }
}
