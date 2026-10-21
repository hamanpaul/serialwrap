//! release-manifest 解析與 Python daemon 退役守門。
//!
//! 完整 schema 由 `contracts/release_manifest.schema.json` 與 Python release gate 驗證；
//! Rust launcher 只解析其執行所需欄位，並在啟動前強制退役一致性。

use serde_json::Value;
use std::sync::LazyLock;

/// manifest 解析錯誤。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ManifestError {
    /// JSON 格式錯誤。
    InvalidJson(String),
    /// 不符合 parser 所需欄位或型別。
    Schema(String),
    /// 退役標記與 daemon 欄位不一致。
    RetirementInconsistent(&'static str),
}

/// `components`／頂層 `retirement` 標記；有效值只有 `"removed"`。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Retirement {
    pub python_daemon: String,
}

/// 已解析的 release manifest。
#[derive(Debug, Clone)]
pub struct ReleaseManifest {
    pub schema_version: u32,
    pub bundle_version: String,
    pub stage: String,
    pub target: String,
    pub git_revision: String,
    /// P3 的 `components.python_cli_delegate.entry`；沒有委派元件時為 `None`。
    pub python_cli_delegate_entry: Option<Vec<String>>,
    /// 原始 JSON（供 launcher 等讀取 `components.daemon.*`）。
    pub raw: Value,
}

impl ReleaseManifest {
    /// 解析 manifest 並驗證退役一致性。
    pub fn parse(text: &str) -> Result<ReleaseManifest, ManifestError> {
        let raw: Value = serde_json::from_str(text)
            .map_err(|error| ManifestError::InvalidJson(error.to_string()))?;
        let root = raw
            .as_object()
            .ok_or_else(|| ManifestError::Schema("root must be an object".into()))?;
        let schema_version =
            root.get("schema_version")
                .and_then(Value::as_f64)
                .filter(|version| *version == 1.0)
                .ok_or_else(|| ManifestError::Schema("schema_version".into()))? as u32;
        let bundle_version = required_string(&raw, "bundle_version")?;
        let stage = required_string(&raw, "stage")?;
        required_string(&raw, "created_at")?;
        let target = required_string(&raw, "target")?;
        let git_revision = required_string(&raw, "git_revision")?;
        required_string(&raw, "contract_revision")?;

        let components = raw
            .get("components")
            .and_then(Value::as_object)
            .ok_or_else(|| ManifestError::Schema("components".into()))?;
        for key in ["cli", "daemon", "assets", "supervisor"] {
            if !components.get(key).is_some_and(Value::is_object) {
                return Err(ManifestError::Schema(format!("components.{key}")));
            }
        }
        let daemon = components
            .get("daemon")
            .and_then(Value::as_object)
            .expect("前面已確認 daemon 是物件");
        for key in ["implementation", "version", "git_revision"] {
            required_nested_string(daemon, key, &format!("components.daemon.{key}"))?;
        }
        let launcher = daemon
            .get("launcher")
            .and_then(Value::as_object)
            .ok_or_else(|| ManifestError::Schema("components.daemon.launcher".into()))?;
        for key in ["path", "sha256"] {
            required_nested_string(launcher, key, &format!("components.daemon.launcher.{key}"))?;
        }

        let delegate_entry = match components.get("python_cli_delegate") {
            None => None,
            Some(delegate) => match delegate.get("entry") {
                None => None,
                Some(Value::Array(values)) if !values.is_empty() => {
                    let mut argv = Vec::with_capacity(values.len());
                    for value in values {
                        let argument = value
                            .as_str()
                            .filter(|argument| !argument.trim().is_empty())
                            .ok_or_else(|| {
                                ManifestError::Schema("components.python_cli_delegate.entry".into())
                            })?;
                        argv.push(argument.to_owned());
                    }
                    Some(argv)
                }
                Some(_) => {
                    return Err(ManifestError::Schema(
                        "components.python_cli_delegate.entry".into(),
                    ))
                }
            },
        };

        let manifest = ReleaseManifest {
            schema_version,
            bundle_version,
            stage,
            target,
            git_revision,
            python_cli_delegate_entry: delegate_entry,
            raw,
        };
        validate_retirement(&manifest)?;
        Ok(manifest)
    }

    /// 只回傳有效的靜態退役標記；標記內容直接從原始 manifest 讀取。
    pub fn retirement(&self) -> Option<&Retirement> {
        if !self.python_daemon_retired() {
            return None;
        }
        static RETIRED: LazyLock<Retirement> = LazyLock::new(|| Retirement {
            python_daemon: "removed".to_owned(),
        });
        Some(&RETIRED)
    }

    /// `retirement.python_daemon == "removed"`。
    pub fn python_daemon_retired(&self) -> bool {
        self.raw
            .pointer("/retirement/python_daemon")
            .and_then(Value::as_str)
            == Some("removed")
    }
}

/// 解析 manifest 的共用入口。
pub fn parse_manifest(text: &str) -> Result<ReleaseManifest, ManifestError> {
    ReleaseManifest::parse(text)
}

/// 檢查退役 manifest 不含任何 Python daemon 或 CLI runtime 欄位。
pub fn validate_retirement(manifest: &ReleaseManifest) -> Result<(), ManifestError> {
    let Some(marker) = manifest.raw.get("retirement") else {
        if manifest.stage == "3" {
            return Err(ManifestError::RetirementInconsistent(
                "stage 3 requires retirement marker",
            ));
        }
        if manifest.python_cli_delegate_entry.is_none() {
            return Err(ManifestError::Schema(
                "components.python_cli_delegate.entry".into(),
            ));
        }
        return Ok(());
    };

    let object = marker
        .as_object()
        .ok_or(ManifestError::RetirementInconsistent("retirement"))?;
    if object.keys().any(|key| key != "python_daemon") {
        return Err(ManifestError::RetirementInconsistent(
            "retirement additional properties",
        ));
    }
    if object.get("python_daemon").and_then(Value::as_str) != Some("removed") {
        return Err(ManifestError::RetirementInconsistent(
            "retirement.python_daemon",
        ));
    }
    if manifest.stage != "3" {
        return Err(ManifestError::RetirementInconsistent(
            "retirement requires stage 3",
        ));
    }

    let components = manifest.raw.get("components").and_then(Value::as_object);
    let daemon = components
        .and_then(|value| value.get("daemon"))
        .and_then(Value::as_object);
    if daemon
        .and_then(|value| value.get("implementation"))
        .and_then(Value::as_str)
        != Some("rust")
    {
        return Err(ManifestError::RetirementInconsistent(
            "retired daemon must be rust",
        ));
    }
    if components.is_some_and(|value| value.contains_key("python_cli_delegate")) {
        return Err(ManifestError::RetirementInconsistent(
            "retired manifest contains python_cli_delegate",
        ));
    }
    if daemon.is_some_and(|value| {
        ["entry", "runtime", "package"]
            .iter()
            .any(|key| value.contains_key(*key))
    }) {
        return Err(ManifestError::RetirementInconsistent(
            "retired daemon contains python runtime fields",
        ));
    }
    Ok(())
}

fn required_string(raw: &Value, key: &str) -> Result<String, ManifestError> {
    raw.get(key)
        .and_then(Value::as_str)
        .filter(|value| !value.trim().is_empty())
        .map(str::to_owned)
        .ok_or_else(|| ManifestError::Schema(key.to_owned()))
}

fn required_nested_string(
    raw: &serde_json::Map<String, Value>,
    key: &str,
    path: &str,
) -> Result<(), ManifestError> {
    raw.get(key)
        .and_then(Value::as_str)
        .filter(|value| !value.trim().is_empty())
        .map(|_| ())
        .ok_or_else(|| ManifestError::Schema(path.to_owned()))
}
