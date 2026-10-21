//! `serialwrapd` launcher（arch §6.1、§10.1；P9-T16 退役守門）。

use std::ffi::OsString;
use std::fmt::{self, Display, Formatter};
use std::path::{Path, PathBuf};

use sw_pal::process::ExecOpts;
use sw_pal::FsRead;
use sw_proto::pyjson::{PyDict, PyValue};
use sw_proto::release_manifest::{ManifestError, ReleaseManifest};

/// launcher 失敗原因；失敗時 stderr 一行 `serialwrapd: <原因>`、exit 2。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum LauncherError {
    ManifestNotFound(String),
    ManifestInvalid(String),
    ProcessStart { program: String, reason: String },
    UnsupportedImplementation(String),
    /// 退役 manifest 的 daemon 仍宣告 `implementation=="python"`（被竄改）；不啟動 Python 子行程（P9-T16）。
    PythonDaemonRetired,
}

/// P9-T16 介面別名：啟動決策的錯誤型別即 [`LauncherError`]。
pub type LaunchError = LauncherError;

/// 啟動計畫（P9-T16）：Rust daemon 原生執行，或未退役 bundle 的 Python 子行程 argv（entry 原樣）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum LaunchPlan {
    RustNative,
    PythonChild { argv: Vec<String> },
}

impl LauncherError {
    fn message(&self) -> String {
        match self {
            Self::ManifestNotFound(path) => format!("release manifest 不存在：{path}"),
            Self::ManifestInvalid(reason) => format!("release manifest 無效：{reason}"),
            Self::ProcessStart { program, reason } => {
                format!("無法啟動 daemon 程序 {program}：{reason}")
            }
            Self::UnsupportedImplementation(value) => {
                format!("不支援 daemon implementation：{value}")
            }
            Self::PythonDaemonRetired => {
                "INTERNAL_ERROR: python daemon retired by release manifest".to_string()
            }
        }
    }
}

/// stderr 一行的錯誤輸出（沿用既有 `serialwrapd: ` 前綴；換行字元一律轉為空白）。
impl Display for LauncherError {
    fn fmt(&self, formatter: &mut Formatter<'_>) -> fmt::Result {
        let message = self.message().replace('\r', " ").replace('\n', " ");
        write!(formatter, "serialwrapd: {message}")
    }
}

fn daemon_implementation(manifest: &ReleaseManifest) -> Result<&str, LauncherError> {
    manifest
        .raw
        .get("components")
        .and_then(|value| match value {
            PyValue::Dict(components) => components.get("daemon"),
            _ => None,
        })
        .and_then(|value| match value {
            PyValue::Dict(daemon) => daemon.get("implementation"),
            _ => None,
        })
        .and_then(PyValue::as_str)
        .ok_or_else(|| {
            LauncherError::ManifestInvalid(
                "components.daemon.implementation 缺少或非字串".to_string(),
            )
        })
}

/// `components.daemon` 物件（`parse_manifest` 已保證存在；此處仍做防禦性檢查）。
fn daemon_dict(manifest: &ReleaseManifest) -> Result<&PyDict, LauncherError> {
    manifest
        .raw
        .get("components")
        .and_then(as_dict)
        .and_then(|components| components.get("daemon"))
        .and_then(as_dict)
        .ok_or_else(|| LauncherError::ManifestInvalid("缺少 components.daemon 物件".to_string()))
}

/// `components.daemon.entry`：必須為非空陣列。`launcher_argv`（路徑解析）與 `resolve_launch`（原樣保留）共用（P9-T16 F4）。
fn daemon_entry(daemon: &PyDict) -> Result<&[PyValue], LauncherError> {
    let entry = daemon
        .get("entry")
        .and_then(PyValue::as_array)
        .ok_or_else(|| {
            LauncherError::ManifestInvalid("components.daemon.entry 缺少或非陣列".to_string())
        })?;
    if entry.is_empty() {
        return Err(LauncherError::ManifestInvalid(
            "components.daemon.entry 不得為空".to_string(),
        ));
    }
    Ok(entry.as_slice())
}

/// 退役守門（P9-T16）：`python_daemon_retired()` 為真而 implementation 為 python → `PythonDaemonRetired`。
/// `launch` 於 `launcher_argv` 之前呼叫，確保被竄改的退役 manifest 不會組出 Python 子行程 argv。
///
/// 【縱深防禦】`parse_manifest` 已以 `validate_retirement` 先行拒絕竄改的退役檔（`RetirementInconsistent`），
/// 故經 `launch` 的實際執行路徑不可達；此檢查保留為 `resolve_launch` 與 `launch` 的第二道防線，
/// 不改變 manifest 解析層的錯誤訊息。
fn refuse_retired_python(manifest: &ReleaseManifest) -> Result<(), LauncherError> {
    if manifest.python_daemon_retired() && daemon_implementation(manifest)? == "python" {
        return Err(LauncherError::PythonDaemonRetired);
    }
    Ok(())
}

/// 依 manifest 決定啟動方式（P9-T16）。退役 manifest 只允許原生 Rust daemon。
pub fn resolve_launch(manifest: &ReleaseManifest) -> Result<LaunchPlan, LaunchError> {
    refuse_retired_python(manifest)?;
    match daemon_implementation(manifest)? {
        "rust" => Ok(LaunchPlan::RustNative),
        "python" => {
            let argv = daemon_entry(daemon_dict(manifest)?)?
                .iter()
                .map(|argument| {
                    argument.as_str().map(str::to_owned).ok_or_else(|| {
                        LauncherError::ManifestInvalid(
                            "components.daemon.entry 含非字串項目".to_string(),
                        )
                    })
                })
                .collect::<Result<Vec<_>, _>>()?;
            Ok(LaunchPlan::PythonChild { argv })
        }
        other => Err(LauncherError::UnsupportedImplementation(other.to_string())),
    }
}

/// 由 manifest 的 `components.daemon.entry` 組出實際 argv。
///
/// manifest 內以 `/` 分隔的相對路徑以 manifest 目錄為基準；參數與 `argv_tail` 原樣保留。
pub fn launcher_argv(
    manifest_dir: &Path,
    manifest: &ReleaseManifest,
    argv_tail: &[OsString],
) -> Result<Vec<OsString>, LauncherError> {
    let components = manifest
        .raw
        .get("components")
        .and_then(as_dict)
        .ok_or_else(|| LauncherError::ManifestInvalid("缺少 components 物件".to_string()))?;
    let daemon = components
        .get("daemon")
        .and_then(as_dict)
        .ok_or_else(|| LauncherError::ManifestInvalid("缺少 components.daemon 物件".to_string()))?;
    let implementation = daemon_implementation(manifest)?;
    if implementation != "python" {
        return Err(LauncherError::UnsupportedImplementation(
            implementation.to_string(),
        ));
    }

    let entry = daemon_entry(daemon)?;

    let mut argv = Vec::with_capacity(entry.len() + argv_tail.len());
    for value in entry {
        let Some(value) = value.as_str() else {
            return Err(LauncherError::ManifestInvalid(
                "components.daemon.entry 含非字串項目".to_string(),
            ));
        };
        if argv.is_empty()
            && !Path::new(value).is_absolute()
            && !value.contains('/')
            && !is_windows_absolute(value)
        {
            return Err(LauncherError::ManifestInvalid(
                "components.daemon.entry[0] 必須是絕對路徑或 bundle 相對路徑".to_string(),
            ));
        }
        if is_relative_entry(value) {
            argv.push(manifest_dir.join(value).into_os_string());
        } else {
            argv.push(OsString::from(value));
        }
    }
    argv.extend(argv_tail.iter().cloned());
    Ok(argv)
}

fn as_dict(value: &PyValue) -> Option<&sw_proto::pyjson::PyDict> {
    match value {
        PyValue::Dict(value) => Some(value),
        _ => None,
    }
}

fn is_relative_entry(value: &str) -> bool {
    if value.starts_with('-')
        || Path::new(value).is_absolute()
        || !value.contains('/')
        || value.chars().any(char::is_whitespace)
        || value.contains(['\'', '"', ';'])
    {
        return false;
    }
    let bytes = value.as_bytes();
    let drive_absolute = bytes.len() >= 3
        && bytes[0].is_ascii_alphabetic()
        && bytes[1] == b':'
        && matches!(bytes[2], b'/' | b'\\');
    !drive_absolute
}

fn is_windows_absolute(value: &str) -> bool {
    let bytes = value.as_bytes();
    value.starts_with("\\\\")
        || bytes.len() >= 3
            && bytes[0].is_ascii_alphabetic()
            && bytes[1] == b':'
            && matches!(bytes[2], b'/' | b'\\')
}

fn manifest_error_detail(error: ManifestError) -> String {
    match error {
        ManifestError::InvalidJson(detail) | ManifestError::Schema(detail) => detail,
        other @ ManifestError::RetirementInconsistent(_) => other.to_string(),
    }
}

fn current_release_manifest_path(
    fs: &dyn FsRead,
    exe_dir: &Path,
) -> Result<Option<PathBuf>, LauncherError> {
    let pointer = exe_dir.join("current-release.json");
    if !fs.exists(&pointer) {
        return Ok(None);
    }
    let bytes = fs.read(&pointer).map_err(|error| {
        LauncherError::ManifestInvalid(format!(
            "current-release.json 無法讀取：{}",
            error.py_message
        ))
    })?;
    let text = std::str::from_utf8(&bytes).map_err(|error| {
        LauncherError::ManifestInvalid(format!("current-release.json 不是 UTF-8：{error}"))
    })?;
    let current = sw_proto::release_manifest::parse_current_release(text)
        .map_err(|error| LauncherError::ManifestInvalid(manifest_error_detail(error)))?;
    let segments: Vec<&str> = current.dir.split('/').collect();
    if current.dir.is_empty()
        || current.dir.contains('\\')
        || current.dir.contains(':')
        || current.dir.starts_with('/')
        || segments
            .iter()
            .any(|segment| segment.is_empty() || *segment == "." || *segment == "..")
    {
        return Err(LauncherError::ManifestInvalid(
            "current-release.json 的 dir 必須是安全的 bundle 相對路徑".to_string(),
        ));
    }
    let bundle_root = exe_dir.parent().ok_or_else(|| {
        LauncherError::ManifestNotFound(format!("{} 上層目錄不足", exe_dir.display()))
    })?;
    let mut version_dir = bundle_root.to_path_buf();
    for segment in segments {
        version_dir.push(segment);
    }
    Ok(Some(version_dir.join("release-manifest.json")))
}

/// 定位 manifest（`current_exe` 上兩層）→ `launcher_argv` → 以 PAL 程序原語承接。
/// `argv` 為完整 argv（`argv[0]` 為本程式）。回傳程序結束碼。
pub fn run(argv: Vec<OsString>, platform: &sw_pal::Platform) -> i32 {
    match launch(argv, platform) {
        Ok(code) => code,
        Err(error) => {
            eprintln!("{error}");
            2
        }
    }
}

fn launch(argv: Vec<OsString>, platform: &sw_pal::Platform) -> Result<i32, LauncherError> {
    let current_exe = platform
        .env
        .current_exe()
        .map_err(|error| LauncherError::ManifestInvalid(error.py_message))?;
    let exe_dir = current_exe.parent().ok_or_else(|| {
        LauncherError::ManifestNotFound(format!("{} 上層目錄不足", current_exe.display()))
    })?;
    let manifest_path = match current_release_manifest_path(platform.fs.as_ref(), exe_dir)? {
        Some(path) => path,
        None => {
            let real_exe = platform.env.realpath(&current_exe).map_err(|error| {
                LauncherError::ManifestNotFound(error.py_message)
            })?;
            let version_dir = real_exe.parent().and_then(Path::parent).ok_or_else(|| {
                LauncherError::ManifestNotFound(format!("{} 上層目錄不足", real_exe.display()))
            })?;
            version_dir.join("release-manifest.json")
        }
    };
    let manifest_dir = manifest_path.parent().ok_or_else(|| {
        LauncherError::ManifestNotFound(manifest_path.display().to_string())
    })?;
    let manifest_bytes = platform.fs.read(&manifest_path).map_err(|_| {
        LauncherError::ManifestNotFound(manifest_path.to_string_lossy().into_owned())
    })?;
    let manifest_text = std::str::from_utf8(&manifest_bytes)
        .map_err(|error| LauncherError::ManifestInvalid(format!("manifest 不是 UTF-8：{error}")))?;
    let manifest = sw_proto::release_manifest::parse_manifest(manifest_text)
        .map_err(|error| LauncherError::ManifestInvalid(manifest_error_detail(error)))?;
    // P9-T16：退役 manifest 不得啟動 Python daemon（先於 launcher_argv 判定）。
    refuse_retired_python(&manifest)?;
    let tail = argv.get(1..).unwrap_or_default();
    let child_argv = launcher_argv(manifest_dir, &manifest, tail)?;
    let error = platform.process.exec_or_wait_opts(
        &child_argv,
        &[],
        ExecOpts {
            no_window: true,
            kill_on_close: true,
        },
    );
    let program = child_argv
        .first()
        .map(|value| value.to_string_lossy().into_owned())
        .unwrap_or_else(|| "<missing argv0>".to_string());
    Err(LauncherError::ProcessStart {
        program,
        reason: error.py_message,
    })
}

#[cfg(test)]
mod tests {
    use super::current_release_manifest_path;
    use std::path::{Path, PathBuf};
    use sw_pal::fakes::MemFs;

    #[test]
    fn current_release_resolves_manifest_from_memfs() {
        let fs = MemFs::default();
        let exe_dir = Path::new("/install/bin");
        fs.files.lock().unwrap().insert(
            exe_dir.join("current-release.json"),
            Ok(br#"{"bundle_version":"0.4.0","dir":"versions/0.4.0"}"#.to_vec()),
        );
        assert_eq!(
            current_release_manifest_path(&fs, exe_dir).unwrap(),
            Some(PathBuf::from("/install/versions/0.4.0/release-manifest.json"))
        );
    }

    #[test]
    fn current_release_rejects_parent_traversal() {
        let fs = MemFs::default();
        let exe_dir = Path::new("/install/bin");
        fs.files.lock().unwrap().insert(
            exe_dir.join("current-release.json"),
            Ok(br#"{"bundle_version":"0.4.0","dir":"../../outside"}"#.to_vec()),
        );
        assert!(current_release_manifest_path(&fs, exe_dir).is_err());
    }
}
