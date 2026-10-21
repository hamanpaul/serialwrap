//! Release manifest 與 current-release 解析（P3-T7／P4-T11；arch §10.1）。
//!
//! 保留 manifest 完整欄位供下游檢查，同時驗證必要結構及 P4 新增的 optional metadata 型別。
//! P9-T16：加入 Python daemon 退役守門（`retirement` 靜態標記與 `validate_retirement`）。

use crate::pyjson::{self, PyDict, PyInt, PyValue};
use std::fmt::{self, Display, Formatter};
use std::sync::LazyLock;

/// 已驗證的 release manifest。
#[derive(Clone, Debug, PartialEq)]
pub struct ReleaseManifest {
    pub schema_version: i64,
    pub bundle_version: String,
    pub stage: String,
    pub target: String,
    pub git_revision: String,
    /// P3 的 `components.python_cli_delegate.entry`；僅退役 manifest 可缺（P9-T16）。
    pub python_cli_delegate_entry: Option<Vec<String>>,
    /// Python daemon 的狀態格式版本；舊 manifest 可省略。
    pub state_schema: Option<String>,
    /// Task Scheduler XML 未代入樣板的 SHA-256；舊 manifest 可省略。
    pub task_xml_template_sha256: Option<String>,
    /// 頂層物件的完整內容（保留未知欄位）。
    pub raw: PyDict,
}

/// 頂層 `retirement` 靜態標記（P9-T16）；有效值只有 `python_daemon == "removed"`。
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Retirement {
    pub python_daemon: String,
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
    /// 退役標記與 daemon／delegate 欄位不一致（P9-T16）。
    RetirementInconsistent(&'static str),
}

impl Display for ManifestError {
    fn fmt(&self, f: &mut Formatter<'_>) -> fmt::Result {
        match self {
            ManifestError::InvalidJson(message) => write!(f, "manifest JSON 無效：{message}"),
            ManifestError::Schema(message) => write!(f, "manifest 結構無效：{message}"),
            ManifestError::RetirementInconsistent(detail) => {
                write!(f, "manifest 退役標記不一致：{detail}")
            }
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
    // P9-T16：只有帶 `retirement` 標記的退役 manifest 可以省略委派元件（Stage 3 CLI 已全原生）。
    let retired_marker = root.get("retirement").is_some();
    // stage 3 缺退役標記一律以 RetirementInconsistent 拒絕，不論是否帶 delegate（先於 delegate 的 Schema 判定）。
    if stage == "3" && !retired_marker {
        return Err(ManifestError::RetirementInconsistent(
            "stage 3 requires retirement marker",
        ));
    }
    let python_cli_delegate_entry = match components.get("python_cli_delegate") {
        None if retired_marker => None,
        None => return Err(schema_err("缺少 components.python_cli_delegate 物件")),
        Some(PyValue::Dict(delegate)) => Some(parse_delegate_entry(delegate)?),
        Some(_) => return Err(schema_err("缺少 components.python_cli_delegate 物件")),
    };

    let manifest = ReleaseManifest {
        schema_version,
        bundle_version,
        stage,
        target,
        git_revision,
        python_cli_delegate_entry,
        state_schema,
        task_xml_template_sha256,
        raw: root,
    };
    validate_retirement(&manifest)?;
    Ok(manifest)
}

fn parse_delegate_entry(delegate: &PyDict) -> Result<Vec<String>, ManifestError> {
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
    Ok(python_cli_delegate_entry)
}

/// 檢查退役守門（P9-T16）：`retirement` 只允許 `python_daemon == "removed"`，且退役 manifest 不得含任何
/// Python daemon 或 CLI runtime 欄位。未退役 manifest 維持 P3 行為（必須帶 delegate entry）。
pub fn validate_retirement(manifest: &ReleaseManifest) -> Result<(), ManifestError> {
    let Some(marker) = manifest.raw.get("retirement") else {
        if manifest.stage == "3" {
            return Err(ManifestError::RetirementInconsistent(
                "stage 3 requires retirement marker",
            ));
        }
        if manifest.python_cli_delegate_entry.is_none() {
            return Err(schema_err("缺少 components.python_cli_delegate 物件"));
        }
        return Ok(());
    };

    let PyValue::Dict(object) = marker else {
        return Err(ManifestError::RetirementInconsistent("retirement"));
    };
    if object.0.keys().any(|key| key != "python_daemon") {
        return Err(ManifestError::RetirementInconsistent(
            "retirement additional properties",
        ));
    }
    if !matches!(object.get("python_daemon"), Some(PyValue::Str(value)) if value == "removed") {
        return Err(ManifestError::RetirementInconsistent(
            "retirement.python_daemon",
        ));
    }
    if manifest.stage != "3" {
        return Err(ManifestError::RetirementInconsistent(
            "retirement requires stage 3",
        ));
    }

    let components = match manifest.raw.get("components") {
        Some(PyValue::Dict(components)) => Some(components),
        _ => None,
    };
    let daemon = components.and_then(|value| match value.get("daemon") {
        Some(PyValue::Dict(daemon)) => Some(daemon),
        _ => None,
    });
    if !matches!(daemon.and_then(|value| value.get("implementation")), Some(PyValue::Str(value)) if value == "rust")
    {
        return Err(ManifestError::RetirementInconsistent(
            "retired daemon must be rust",
        ));
    }
    if components.is_some_and(|value| value.get("python_cli_delegate").is_some()) {
        return Err(ManifestError::RetirementInconsistent(
            "retired manifest contains python_cli_delegate",
        ));
    }
    if daemon.is_some_and(|value| {
        ["entry", "runtime", "package"]
            .iter()
            .any(|key| value.get(key).is_some())
    }) {
        return Err(ManifestError::RetirementInconsistent(
            "retired daemon contains python runtime fields",
        ));
    }
    Ok(())
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
    /// 解析並驗證 release manifest（與 [`parse_manifest`] 相同；P9-T16 測試與呼叫端的方法入口）。
    pub fn parse(text: &str) -> Result<ReleaseManifest, ManifestError> {
        parse_manifest(text)
    }

    /// 頂層 `retirement` 標記（只回傳有效的 `removed`；其餘內容由 [`validate_retirement`] 拒絕）。
    pub fn retirement(&self) -> Option<&Retirement> {
        if !self.python_daemon_retired() {
            return None;
        }
        static RETIRED: LazyLock<Retirement> = LazyLock::new(|| Retirement {
            python_daemon: "removed".to_owned(),
        });
        Some(&RETIRED)
    }

    /// `retirement.python_daemon == "removed"`；直接讀原始 manifest，不依賴解析後欄位。
    pub fn python_daemon_retired(&self) -> bool {
        matches!(
            self.raw.get("retirement"),
            Some(PyValue::Dict(marker))
                if matches!(marker.get("python_daemon"), Some(PyValue::Str(value)) if value == "removed")
        )
    }

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
