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
        // Python 基線 `load_release_manifest` 以 `!= 1` 判定，故 1.0 亦有效（布林已由 PyValue::Bool 分流拒絕）。
        Some(PyValue::Float(f)) if *f == 1.0 => 1,
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

    if let Some(PyValue::Dict(comps)) = root.get("components") {
        if let Some(com0com) = comps.get("com0com") {
            let PyValue::Dict(com0com) = com0com else {
                return Err(schema_err("欄位 components.com0com 必須為物件"));
            };
            let _ = parse_com0com(com0com)?;
        }
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
#[derive(Clone, Debug, PartialEq)]
pub struct Com0comComponent {
    pub optional: bool,
    pub default_selected: bool,
    pub requires_elevation: bool,
    pub experimental: bool,
    pub version: String,
    pub path: String,
    pub sha256: String,
    /// 固定為 `"GPL-2.0"`。
    pub license: String,
    pub license_file: String,
    pub source_offer_file: String,
    pub provenance_file: String,
    pub helper: Com0comHelper,
    /// 預設 pair（`("COM20", "COM21")`）。
    pub default_pair: (String, String),
}

/// 提權 helper 的 bundle 路徑與完整性摘要。
#[derive(Clone, Debug, PartialEq)]
pub struct Com0comHelper {
    pub path: String,
    pub sha256: String,
}

fn required_bool(d: &PyDict, key: &str) -> Result<bool, ManifestError> {
    match d.get(key) {
        Some(PyValue::Bool(value)) => Ok(*value),
        _ => Err(ManifestError::Schema(format!(
            "欄位 components.com0com.{key} 缺少或非布林值"
        ))),
    }
}

fn required_string(d: &PyDict, key: &str) -> Result<String, ManifestError> {
    match d.get(key) {
        Some(PyValue::Str(value)) if !value.is_empty() => Ok(value.clone()),
        _ => Err(ManifestError::Schema(format!(
            "欄位 components.com0com.{key} 缺少或非字串"
        ))),
    }
}

fn parse_com0com(d: &PyDict) -> Result<Com0comComponent, ManifestError> {
    let optional = required_bool(d, "optional")?;
    let default_selected = required_bool(d, "default_selected")?;
    let requires_elevation = required_bool(d, "requires_elevation")?;
    let experimental = required_bool(d, "experimental")?;
    let version = required_string(d, "version")?;
    let path = required_string(d, "path")?;
    let sha256 = required_string(d, "sha256")?;
    let license = required_string(d, "license")?;
    let license_file = required_string(d, "license_file")?;
    let source_offer_file = required_string(d, "source_offer_file")?;
    let provenance_file = required_string(d, "provenance_file")?;
    let helper = match d.get("helper") {
        Some(PyValue::Dict(helper)) => Com0comHelper {
            path: required_string(helper, "path")?,
            sha256: required_string(helper, "sha256")?,
        },
        _ => return Err(schema_err("欄位 components.com0com.helper 必須為物件")),
    };
    let default_pair = match d.get("default_pair") {
        Some(PyValue::List(items)) if items.len() == 2 => match (&items[0], &items[1]) {
            (PyValue::Str(a), PyValue::Str(b)) => (a.clone(), b.clone()),
            _ => {
                return Err(schema_err(
                    "欄位 components.com0com.default_pair 必須含兩個字串",
                ))
            }
        },
        _ => {
            return Err(schema_err(
                "欄位 components.com0com.default_pair 必須為兩項陣列",
            ))
        }
    };

    if !optional || default_selected || !requires_elevation {
        return Err(schema_err(
            "components.com0com 必須為選用、預設未選取且需要提權",
        ));
    }
    if version != "3.0.0.0" {
        return Err(schema_err("components.com0com.version 必須為 3.0.0.0"));
    }
    if path != "optional/com0com/" {
        return Err(schema_err(
            "components.com0com.path 必須為 optional/com0com/",
        ));
    }
    if !is_sha256_hex(&sha256) {
        return Err(schema_err(
            "欄位 components.com0com.sha256 必須為 64 位小寫十六進位",
        ));
    }
    if !is_sha256_hex(&helper.sha256) {
        return Err(schema_err(
            "欄位 components.com0com.helper.sha256 必須為 64 位小寫十六進位",
        ));
    }
    if license != "GPL-2.0" {
        return Err(schema_err("components.com0com.license 必須為 GPL-2.0"));
    }
    if license_file != "optional/com0com/LICENSE-GPLv2.txt"
        || source_offer_file != "optional/com0com/SOURCE.md"
        || provenance_file != "optional/com0com/PROVENANCE.json"
        || !matches!(
            helper.path.as_str(),
            "bin/sw-com0com-helper" | "bin/sw-com0com-helper.exe"
        )
    {
        return Err(schema_err("components.com0com 含不安全的 bundle 相對路徑"));
    }
    if default_pair.0.eq_ignore_ascii_case(&default_pair.1)
        || [&default_pair.0, &default_pair.1].iter().any(|port| {
            let Some(digits) = port.strip_prefix("COM") else {
                return true;
            };
            digits.is_empty() || !digits.bytes().all(|byte| byte.is_ascii_digit())
        })
    {
        return Err(schema_err(
            "components.com0com.default_pair 必須為兩個不同 COM 埠",
        ));
    }

    Ok(Com0comComponent {
        optional,
        default_selected,
        requires_elevation,
        experimental,
        version,
        path,
        sha256,
        license,
        license_file,
        source_offer_file,
        provenance_file,
        helper,
        default_pair,
    })
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
        let Some(PyValue::Dict(components)) = self.raw.get("components") else {
            return None;
        };
        let Some(PyValue::Dict(com0com)) = components.get("com0com") else {
            return None;
        };
        parse_com0com(com0com).ok()
    }
}
