//! daemon 啟動器，依 release manifest 選擇原生 Rust 或未退役的 Python daemon。
use sw_proto::release_manifest::ReleaseManifest;

/// 啟動計畫：原生 Rust daemon，或（僅未退役的舊 bundle）Python 子行程。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum LaunchPlan {
    RustNative,
    PythonChild { argv: Vec<String> },
}

/// 啟動錯誤。`PythonDaemonRetired` 對應 stderr `serialwrapd: INTERNAL_ERROR: python daemon retired by release manifest`（exit 2）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum LaunchError {
    PythonDaemonRetired,
    ManifestInvalid(String),
}

/// 依 manifest 決定啟動方式。`python_daemon_retired()` 為真而 implementation 為 python（被竄改的 manifest）→ `PythonDaemonRetired`。
pub fn resolve_launch(manifest: &ReleaseManifest) -> Result<LaunchPlan, LaunchError> {
    let daemon = manifest
        .raw
        .get("components")
        .and_then(|components| components.get("daemon"))
        .ok_or_else(|| LaunchError::ManifestInvalid("components.daemon missing".into()))?;
    let implementation = daemon
        .get("implementation")
        .and_then(|value| value.as_str())
        .ok_or_else(|| {
            LaunchError::ManifestInvalid("components.daemon.implementation missing".into())
        })?;

    if manifest.python_daemon_retired() && implementation == "python" {
        return Err(LaunchError::PythonDaemonRetired);
    }
    match implementation {
        "rust" => Ok(LaunchPlan::RustNative),
        "python" => {
            let entry = daemon
                .get("entry")
                .and_then(|value| value.as_array())
                .filter(|argv| !argv.is_empty())
                .ok_or_else(|| {
                    LaunchError::ManifestInvalid("components.daemon.entry missing".into())
                })?;
            let mut argv = Vec::with_capacity(entry.len());
            for argument in entry {
                argv.push(
                    argument
                        .as_str()
                        .filter(|value| !value.trim().is_empty())
                        .ok_or_else(|| {
                            LaunchError::ManifestInvalid("components.daemon.entry invalid".into())
                        })?
                        .to_owned(),
                );
            }
            Ok(LaunchPlan::PythonChild { argv })
        }
        other => Err(LaunchError::ManifestInvalid(format!(
            "unsupported daemon implementation: {other}"
        ))),
    }
}

impl std::fmt::Display for LaunchError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            LaunchError::PythonDaemonRetired => formatter.write_str(
                "serialwrapd: INTERNAL_ERROR: python daemon retired by release manifest",
            ),
            LaunchError::ManifestInvalid(detail) => {
                write!(formatter, "serialwrapd: INTERNAL_ERROR: {detail}")
            }
        }
    }
}
