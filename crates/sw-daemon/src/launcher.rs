//! `serialwrapd` launcher（arch §6.1、§10.1）。

use std::ffi::OsString;
use std::path::{Path, PathBuf};

use sw_pal::process::ExecOpts;
use sw_pal::FsRead;
use sw_proto::pyjson::PyValue;
use sw_proto::release_manifest::{ManifestError, ReleaseManifest};

/// launcher 失敗原因；失敗時 stderr 一行 `serialwrapd: <原因>`、exit 2。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum LauncherError {
    ManifestNotFound(String),
    ManifestInvalid(String),
    ProcessStart { program: String, reason: String },
    UnsupportedImplementation(String),
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
        }
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
    let implementation = daemon
        .get("implementation")
        .and_then(PyValue::as_str)
        .ok_or_else(|| {
            LauncherError::ManifestInvalid(
                "components.daemon.implementation 缺少或非字串".to_string(),
            )
        })?;
    if implementation != "python" {
        return Err(LauncherError::UnsupportedImplementation(
            implementation.to_string(),
        ));
    }

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
            let message = error.message().replace('\r', " ").replace('\n', " ");
            eprintln!("serialwrapd: {message}");
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
