//! `%LOCALAPPDATA%\Programs\serialwrap\versions\<v>\` 與穩定入口安裝佈局（P4-T11）。

use std::collections::{BTreeMap, BTreeSet};
use std::fmt::{self, Display, Formatter};
use std::io::{Error as IoError, ErrorKind};
use std::path::{Path, PathBuf};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use sw_pal::fs_ops::FileLockGuard;
use sw_pal::{PalErrorKind, PalPathFlavor, SysEnv};
use sw_proto::digest::sha256_hex;
use sw_proto::error_codes::ErrorCode;
use sw_proto::pyjson::{self, DumpOpts, PyDict, PyValue, Sep};
use sw_proto::pypath::{self, HomeLookup, PathFlavor};
use sw_proto::release_manifest::{parse_manifest, ReleaseManifest};

use crate::ctx::CliCtx;
pub use crate::pairing::{check_pairing, pairing_item, PairingDiff};

/// 計畫中的 `Ctx`（即 [`CliCtx`]；見 05 計畫 §0.2）。
pub type Ctx<'a> = CliCtx<'a>;

const CHECKSUM_FILE_NAME: &str = concat!("SHA", "256SUMS");

/// 入口檔的放置方式（Windows 複製、POSIX symlink）。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum EntryStyle {
    /// POSIX：`<bin>/<name>` 為指向 `versions/<v>/bin/<name>` 的 symlink。
    Symlink,
    /// Windows：`<bin>\<name>` 為複本（執行中 exe 以改名保留舊檔）。
    Copy,
}

/// 安裝根目錄的解析結果（計畫 Task 11「Produces」）。
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct InstallRoot {
    pub root: PathBuf,
    pub versions: PathBuf,
    pub bin: PathBuf,
    pub backups: PathBuf,
    pub supervisor: PathBuf,
    pub current_file: PathBuf,
    pub entry_style: EntryStyle,
    pub entry_names: Vec<String>,
}

/// `current-release.json` 內的版本參照（`previous` 欄位）。
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ReleaseRef {
    pub bundle_version: String,
    pub dir: String,
}

/// `current-release.json` 的完整內容（含目前與上一版的備份中繼資料）。
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct CurrentRelease {
    pub bundle_version: String,
    pub dir: String,
    pub previous: Option<ReleaseRef>,
    pub backup_dir: Option<String>,
    pub switched_at: String,
}

/// 安裝佈局錯誤。本 Phase 不新增錯誤碼；呼叫端依 §9.2 既有碼映射。
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum LayoutError {
    /// `versions/<v>` 已存在但驗證失敗：不覆寫，回報。
    ExistingCorrupt(PathBuf),
    /// 檔案系統操作失敗。
    Io { path: PathBuf, message: String },
    /// `current-release.json` 或 manifest 無法解析。
    Parse { path: PathBuf, message: String },
    /// 無法定位 release manifest。
    ManifestNotFound(String),
    /// bundle 的 manifest、元件或 SHA256SUMS 驗證失敗；保留差異供呼叫端回報 CHECKSUM_VERIFY_FAILED。
    BundleVerificationFailed {
        path: PathBuf,
        diffs: Vec<PairingDiff>,
    },
    /// 多個入口回復都已嘗試，但至少一個失敗。
    RevertEntriesFailed(Vec<LayoutError>),
    /// 入口切換失敗，且回復也失敗；呼叫端必須保留安裝 journal。
    RevertFailed {
        original: Box<LayoutError>,
        revert: Box<LayoutError>,
    },
}

/// 執行中 CLI 的位置分類。
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum ExeLocation {
    /// `versions/<v>/bin/<name>`。
    InstalledVersion(String),
    /// 穩定入口 `<bin>/<name>`。
    StableEntry,
    /// 其他位置（例如開發 bundle 的解壓目錄）。
    External(PathBuf),
}

/// `switch_entries` 的結果；`revert_entries` 以此還原。
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SwitchReport {
    /// 每個入口的 (名稱, 切換前的舊目標或 None)。
    pub replaced: Vec<(String, Option<String>)>,
    /// 本次產生的 `*.old.<ts>` 保存檔。
    pub old_files: Vec<PathBuf>,
}

struct PlatformHome<'a>(&'a dyn SysEnv);

impl HomeLookup for PlatformHome<'_> {
    fn current_user_home(&self) -> Option<String> {
        self.0.current_user_home()
    }

    fn user_home(&self, user: &str) -> Option<String> {
        self.0.user_home(user)
    }
}

fn path_flavor(ctx: &Ctx<'_>) -> PathFlavor {
    match ctx.p.caps.path_flavor {
        PalPathFlavor::Posix => PathFlavor::Posix,
        PalPathFlavor::Windows => PathFlavor::Windows,
    }
}

fn join_path(ctx: &Ctx<'_>, base: &Path, child: &str) -> PathBuf {
    let base = base.to_string_lossy();
    PathBuf::from(pypath::join(path_flavor(ctx), &base, &[child]))
}

fn expanded_path(ctx: &Ctx<'_>, raw: &str) -> PathBuf {
    let flavor = path_flavor(ctx);
    let home = PlatformHome(ctx.p.env.as_ref());
    let expanded = pypath::expanduser(flavor, raw, &ctx.env, &home);
    PathBuf::from(pypath::normpath(flavor, &expanded))
}

fn expanded_path_without_normalizing(ctx: &Ctx<'_>, raw: &str) -> PathBuf {
    let flavor = path_flavor(ctx);
    let home = PlatformHome(ctx.p.env.as_ref());
    PathBuf::from(pypath::expanduser(flavor, raw, &ctx.env, &home))
}

impl Display for LayoutError {
    fn fmt(&self, f: &mut Formatter<'_>) -> fmt::Result {
        match self {
            LayoutError::ExistingCorrupt(path) => {
                write!(f, "既有版本驗證失敗：{}", path.display())
            }
            LayoutError::Io { path, message } => {
                write!(f, "檔案操作失敗（{}）：{message}", path.display())
            }
            LayoutError::Parse { path, message } => {
                write!(f, "資料無效（{}）：{message}", path.display())
            }
            LayoutError::ManifestNotFound(message) => f.write_str(message),
            LayoutError::BundleVerificationFailed { path, diffs } => {
                write!(
                    f,
                    "bundle 驗證失敗（{}）：{}",
                    path.display(),
                    diff_summary(diffs)
                )
            }
            LayoutError::RevertEntriesFailed(errors) => {
                let details = errors
                    .iter()
                    .map(ToString::to_string)
                    .collect::<Vec<_>>()
                    .join("; ");
                write!(f, "入口回復失敗：{details}")
            }
            LayoutError::RevertFailed { original, revert } => {
                write!(f, "入口切換失敗：{original}；回復失敗：{revert}")
            }
        }
    }
}

fn diff_summary(diffs: &[PairingDiff]) -> String {
    const LIMIT: usize = 20;
    let rendered = diffs
        .iter()
        .take(LIMIT)
        .map(|diff| {
            format!(
                "{}: expected {}, actual {}",
                diff.field, diff.expected, diff.actual
            )
        })
        .collect::<Vec<_>>()
        .join("; ");
    if diffs.len() > LIMIT {
        format!("{rendered}; 另有 {} 項差異", diffs.len() - LIMIT)
    } else {
        rendered
    }
}

fn layout_io(path: &Path, err: impl Display) -> LayoutError {
    LayoutError::Io {
        path: path.to_path_buf(),
        message: err.to_string(),
    }
}

fn pal_error_message(err: &sw_pal::PalError) -> String {
    if err.py_message.is_empty() {
        err.strerror.clone()
    } else {
        err.py_message.clone()
    }
}

fn layout_pal_io(path: &Path, err: sw_pal::PalError) -> LayoutError {
    LayoutError::Io {
        path: path.to_path_buf(),
        message: pal_error_message(&err),
    }
}

fn pal_io<T>(path: &Path, result: sw_pal::PalResult<T>) -> Result<T, LayoutError> {
    result.map_err(|err| layout_pal_io(path, err))
}

/// 安裝根目錄解析（`SERIALWRAP_INSTALL_ROOT` > Windows `%LOCALAPPDATA%\Programs\serialwrap` > POSIX `DATA_DIR`）。
pub fn resolve_install_root(ctx: &Ctx<'_>) -> InstallRoot {
    let flavor = path_flavor(ctx);
    let override_root = ctx
        .env
        .get("SERIALWRAP_INSTALL_ROOT")
        .map(str::trim)
        .filter(|value| !value.is_empty());
    let has_override = override_root.is_some();
    let root = if let Some(value) = override_root {
        expanded_path_without_normalizing(ctx, value)
    } else if flavor == PathFlavor::Windows {
        let local = ctx
            .p
            .env
            .local_appdata()
            .unwrap_or_else(|| PathBuf::from(ctx.paths.data_dir.clone()));
        join_path(ctx, &local, "Programs\\serialwrap")
    } else {
        PathBuf::from(&ctx.paths.data_dir)
    };

    let entry_style = if flavor == PathFlavor::Windows {
        EntryStyle::Copy
    } else {
        EntryStyle::Symlink
    };
    let entry_names = match entry_style {
        EntryStyle::Copy => vec!["serialwrap.exe".to_string(), "serialwrapd.exe".to_string()],
        EntryStyle::Symlink => vec!["serialwrap".to_string(), "serialwrapd".to_string()],
    };
    let versions = join_path(ctx, &root, "versions");
    let bin = if flavor == PathFlavor::Windows || has_override {
        join_path(ctx, &root, "bin")
    } else {
        expanded_path(ctx, "~/.local/bin")
    };
    let current_file = if flavor == PathFlavor::Windows {
        join_path(ctx, &bin, "current-release.json")
    } else {
        join_path(ctx, &root, "current-release.json")
    };

    InstallRoot {
        backups: join_path(ctx, &root, "backups"),
        supervisor: join_path(ctx, &root, "supervisor"),
        root,
        versions,
        bin,
        current_file,
        entry_style,
        entry_names,
    }
}

fn object_field<'a>(dict: &'a PyDict, name: &str) -> Result<&'a PyValue, String> {
    dict.get(name).ok_or_else(|| format!("缺少欄位 {name}"))
}

fn string_value(value: &PyValue, field: &str) -> Result<String, String> {
    match value {
        PyValue::Str(s) => Ok(s.clone()),
        _ => Err(format!("欄位 {field} 必須為字串")),
    }
}

fn optional_string_value(value: Option<&PyValue>, field: &str) -> Result<Option<String>, String> {
    match value {
        None | Some(PyValue::Null) => Ok(None),
        Some(PyValue::Str(s)) => Ok(Some(s.clone())),
        Some(_) => Err(format!("欄位 {field} 必須為字串或 null")),
    }
}

/// 讀取 `current-release.json`；不存在回 `Ok(None)`，未知鍵忽略。
pub fn read_current(root: &InstallRoot) -> Result<Option<CurrentRelease>, LayoutError> {
    let bytes = match std::fs::read(&root.current_file) {
        Ok(bytes) => bytes,
        Err(err) if err.kind() == ErrorKind::NotFound => return Ok(None),
        Err(err) => return Err(layout_io(&root.current_file, err)),
    };
    let text = String::from_utf8(bytes).map_err(|err| LayoutError::Parse {
        path: root.current_file.clone(),
        message: format!("JSON 必須為 UTF-8：{err}"),
    })?;
    let parsed = pyjson::loads(&text).map_err(|err| LayoutError::Parse {
        path: root.current_file.clone(),
        message: err.to_string(),
    })?;
    let PyValue::Dict(doc) = parsed else {
        return Err(LayoutError::Parse {
            path: root.current_file.clone(),
            message: "current-release.json 頂層必須為物件".to_string(),
        });
    };

    let bundle_version = object_field(&doc, "bundle_version")
        .and_then(|v| string_value(v, "bundle_version"))
        .map_err(|message| LayoutError::Parse {
            path: root.current_file.clone(),
            message,
        })?;
    let dir = object_field(&doc, "dir")
        .and_then(|v| string_value(v, "dir"))
        .map_err(|message| LayoutError::Parse {
            path: root.current_file.clone(),
            message,
        })?;
    let previous = match doc.get("previous") {
        None | Some(PyValue::Null) => None,
        Some(PyValue::Dict(prev)) => {
            let version = object_field(prev, "bundle_version")
                .and_then(|v| string_value(v, "previous.bundle_version"))
                .map_err(|message| LayoutError::Parse {
                    path: root.current_file.clone(),
                    message,
                })?;
            let dir = object_field(prev, "dir")
                .and_then(|v| string_value(v, "previous.dir"))
                .map_err(|message| LayoutError::Parse {
                    path: root.current_file.clone(),
                    message,
                })?;
            Some(ReleaseRef {
                bundle_version: version,
                dir,
            })
        }
        Some(_) => {
            return Err(LayoutError::Parse {
                path: root.current_file.clone(),
                message: "欄位 previous 必須為物件或 null".to_string(),
            })
        }
    };
    let backup_dir =
        optional_string_value(doc.get("backup_dir"), "backup_dir").map_err(|message| {
            LayoutError::Parse {
                path: root.current_file.clone(),
                message,
            }
        })?;
    let switched_at = optional_string_value(doc.get("switched_at"), "switched_at")
        .map_err(|message| LayoutError::Parse {
            path: root.current_file.clone(),
            message,
        })?
        .unwrap_or_default();

    Ok(Some(CurrentRelease {
        bundle_version,
        dir,
        previous,
        backup_dir,
        switched_at,
    }))
}

fn release_ref_value(reference: &ReleaseRef) -> PyValue {
    let mut value = PyDict::new();
    value.insert(
        "bundle_version",
        PyValue::Str(reference.bundle_version.clone()),
    );
    value.insert("dir", PyValue::Str(reference.dir.clone()));
    PyValue::Dict(value)
}

/// 原子寫入 `current-release.json`（DISK_STABLE 的縮排二格格式）。
pub fn write_current(
    ctx: &Ctx<'_>,
    root: &InstallRoot,
    cur: &CurrentRelease,
) -> Result<(), LayoutError> {
    let mut doc = PyDict::new();
    doc.insert(
        "backup_dir",
        cur.backup_dir
            .as_ref()
            .map_or(PyValue::Null, |s| PyValue::Str(s.clone())),
    );
    doc.insert("bundle_version", PyValue::Str(cur.bundle_version.clone()));
    doc.insert("dir", PyValue::Str(cur.dir.clone()));
    doc.insert(
        "previous",
        cur.previous
            .as_ref()
            .map_or(PyValue::Null, release_ref_value),
    );
    doc.insert("switched_at", PyValue::Str(cur.switched_at.clone()));
    let opts = DumpOpts {
        sep: Sep::Indent(2),
        ensure_ascii: false,
        sort_keys: true,
        allow_nan: true,
    };
    let mut bytes = pyjson::dumps_dict(&doc, &opts).into_bytes();
    bytes.push(b'\n');

    let parent = root.current_file.parent().unwrap_or(&root.root);
    pal_io(parent, ctx.p.fs.mkdirs(parent))?;
    pal_io(
        &root.current_file,
        ctx.p.fs.atomic_write(&root.current_file, &bytes, None),
    )
}

fn normalized_path(ctx: &Ctx<'_>, path: &Path) -> String {
    let flavor = path_flavor(ctx);
    let path = without_verbatim_prefix(path);
    let raw = path.to_string_lossy();
    let raw = if flavor == PathFlavor::Posix && is_windows_drive_path(&raw) {
        raw.replace('\\', "/")
    } else {
        raw.into_owned()
    };
    let normalized = pypath::normpath(flavor, &raw);
    if flavor == PathFlavor::Windows {
        normalized.to_lowercase()
    } else {
        normalized
    }
}

fn is_windows_drive_path(path: &str) -> bool {
    let bytes = path.as_bytes();
    bytes.len() >= 3
        && bytes[0].is_ascii_alphabetic()
        && bytes[1] == b':'
        && matches!(bytes[2], b'\\' | b'/')
}

fn has_windows_drive_prefix(path: &str) -> bool {
    let bytes = path.as_bytes();
    bytes.len() >= 2 && bytes[0].is_ascii_alphabetic() && bytes[1] == b':'
}

/// 移除 Windows 長路徑 API 加入的 verbatim prefix，維持 Python `realpath` 的可見形式。
fn without_verbatim_prefix(path: &Path) -> PathBuf {
    let text = path.to_string_lossy();
    if let Some(unc) = text.strip_prefix("\\\\?\\UNC\\") {
        PathBuf::from(format!("\\\\{unc}"))
    } else if let Some(rest) = text.strip_prefix("\\\\?\\") {
        PathBuf::from(rest)
    } else {
        path.to_path_buf()
    }
}

fn path_prefix_remainder<'a>(root: &str, path: &'a str, flavor: PathFlavor) -> Option<&'a str> {
    if path == root {
        return Some("");
    }
    let separator = if flavor == PathFlavor::Windows {
        '\\'
    } else {
        '/'
    };
    let mut prefix = root.to_string();
    if !prefix.ends_with(separator) {
        prefix.push(separator);
    }
    path.strip_prefix(&prefix)
}

/// 判定執行中 exe 的位置。
pub fn classify_exe(ctx: &Ctx<'_>, root: &InstallRoot) -> ExeLocation {
    let exe = ctx
        .p
        .env
        .current_exe()
        .ok()
        .map(|path| ctx.p.env.realpath(&path).unwrap_or(path))
        .unwrap_or_default();
    let exe_text = normalized_path(ctx, &exe);
    let flavor = path_flavor(ctx);
    let versions_text = normalized_path(ctx, &root.versions);
    if let Some(remainder) = path_prefix_remainder(&versions_text, &exe_text, flavor) {
        let pieces: Vec<&str> = remainder
            .split(['/', '\\'])
            .filter(|part| !part.is_empty())
            .collect();
        if pieces.len() >= 3 && pieces[1].eq_ignore_ascii_case("bin") {
            if root
                .entry_names
                .iter()
                .any(|name| name.eq_ignore_ascii_case(pieces[2]))
            {
                return ExeLocation::InstalledVersion(pieces[0].to_string());
            }
        }
    }

    let bin_text = normalized_path(ctx, &root.bin);
    if let Some(remainder) = path_prefix_remainder(&bin_text, &exe_text, flavor) {
        if root
            .entry_names
            .iter()
            .any(|name| name.eq_ignore_ascii_case(remainder))
        {
            return ExeLocation::StableEntry;
        }
    }

    let parent = exe.parent().unwrap_or(&exe);
    let external = if parent
        .file_name()
        .is_some_and(|name| name.to_string_lossy().eq_ignore_ascii_case("bin"))
    {
        parent.parent().unwrap_or(parent)
    } else {
        parent
    };
    ExeLocation::External(external.to_path_buf())
}

fn raw_value<'a>(m: &'a ReleaseManifest, dotted: &str) -> Option<&'a PyValue> {
    let mut parts = dotted.split('.');
    let mut node = m.raw.get(parts.next()?)?;
    for key in parts {
        let PyValue::Dict(d) = node else {
            return None;
        };
        node = d.get(key)?;
    }
    Some(node)
}

fn raw_string<'a>(m: &'a ReleaseManifest, dotted: &str) -> Option<&'a str> {
    match raw_value(m, dotted)? {
        PyValue::Str(s) => Some(s),
        _ => None,
    }
}

fn add_pairing_diff(
    diffs: &mut Vec<PairingDiff>,
    field: impl Into<String>,
    expected: impl Into<String>,
    actual: impl Into<String>,
) {
    diffs.push(PairingDiff {
        field: field.into(),
        expected: expected.into(),
        actual: actual.into(),
    });
}

fn manifest_path(_ctx: &Ctx<'_>, base: &Path, raw: &str) -> PathBuf {
    base.join(raw.replace('\\', "/"))
}

fn check_file_digest(
    ctx: &Ctx<'_>,
    base: &Path,
    m: &ReleaseManifest,
    path_field: Option<&str>,
    hash_field: &str,
    fallback_path: &str,
    field_name: &str,
    diffs: &mut Vec<PairingDiff>,
) {
    let Some(expected) = raw_string(m, hash_field) else {
        add_pairing_diff(diffs, field_name, "declared SHA-256", "undeclared");
        return;
    };
    let relative = match path_field {
        Some(key) => match raw_string(m, key) {
            Some(path) => path,
            None => {
                add_pairing_diff(
                    diffs,
                    field_name,
                    "declared bundle-relative path",
                    "undeclared",
                );
                return;
            }
        },
        None => fallback_path,
    };
    if !safe_sum_path(path_flavor(ctx), relative) {
        add_pairing_diff(diffs, field_name, "bundle-relative path", relative);
        return;
    }
    let path = manifest_path(ctx, base, relative);
    let actual = match ctx.p.fs.read(&path) {
        Ok(bytes) => sha256_hex(&bytes),
        Err(err) => format!("unreadable:{}", pal_error_message(&err)),
    };
    if expected != actual {
        add_pairing_diff(diffs, field_name, expected, actual);
    }
}

fn tree_sha256_with_prefix(ctx: &Ctx<'_>, root: &Path, rel_prefix: &str) -> Result<String, String> {
    crate::tree_digest::digest_tree(
        &crate::tree_digest::PalTreeSource(ctx.p.fs.as_ref()),
        root,
        rel_prefix,
        true,
    )
}

fn collect_all_files(ctx: &Ctx<'_>, root: &Path) -> Result<Vec<String>, String> {
    let files = crate::tree_digest::collect_file_paths(
        &crate::tree_digest::PalTreeSource(ctx.p.fs.as_ref()),
        root,
        "",
        false,
    )?;
    let mut paths: Vec<String> = files.into_iter().map(|(relative, _)| relative).collect();
    paths.sort_by(|a, b| a.as_bytes().cmp(b.as_bytes()));
    Ok(paths)
}

fn com0com_digest(
    ctx: &Ctx<'_>,
    dir: &Path,
    component: &sw_proto::release_manifest::Com0comComponent,
) -> Result<String, String> {
    let root = manifest_path(ctx, dir, component.path.trim_end_matches('/'));
    let mut paths = collect_all_files(ctx, &root)?;
    paths.sort_by(|a, b| a.as_bytes().cmp(b.as_bytes()));
    if paths.is_empty() {
        return Err("optional/com0com 目錄沒有檔案".into());
    }
    let mut content = Vec::new();
    for relative in paths {
        if !safe_sum_path(path_flavor(ctx), &relative) {
            return Err(format!("com0com 元件含不安全路徑：{relative}"));
        }
        let bytes = ctx
            .p
            .fs
            .read(&root.join(&relative))
            .map_err(|error| pal_error_message(&error))?;
        content.extend_from_slice(component.path.as_bytes());
        content.extend_from_slice(relative.as_bytes());
        content.push(0);
        content.extend_from_slice(&bytes);
    }
    Ok(sha256_hex(&content))
}

fn safe_sum_path(flavor: PathFlavor, relative: &str) -> bool {
    if relative.is_empty()
        || pypath::isabs(flavor, relative)
        || Path::new(relative).is_absolute()
        || has_windows_drive_prefix(relative)
        || relative.starts_with("\\\\")
    {
        return false;
    }
    !relative
        .replace('\\', "/")
        .split('/')
        .any(|component| component == ".." || component.is_empty())
}

fn verify_sha256_sums(ctx: &Ctx<'_>, dir: &Path, diffs: &mut Vec<PairingDiff>) {
    let sums_path = dir.join(CHECKSUM_FILE_NAME);
    let bytes = match ctx.p.fs.read(&sums_path) {
        Ok(bytes) => bytes,
        Err(err) => {
            add_pairing_diff(
                diffs,
                "sha256:SHA256SUMS",
                "checksum manifest",
                format!("unreadable:{}", pal_error_message(&err)),
            );
            return;
        }
    };
    let text = match String::from_utf8(bytes) {
        Ok(text) => text,
        Err(err) => {
            add_pairing_diff(
                diffs,
                "sha256:SHA256SUMS",
                "UTF-8 checksum manifest",
                err.to_string(),
            );
            return;
        }
    };
    let flavor = path_flavor(ctx);
    let mut declared = BTreeMap::<String, String>::new();
    for line in text.lines() {
        let Some((digest, relative)) = line.split_once("  ") else {
            add_pairing_diff(
                diffs,
                "sha256:SHA256SUMS",
                "<64 lowercase hex>  <path>",
                line,
            );
            continue;
        };
        let digest_valid = digest.len() == 64
            && digest
                .as_bytes()
                .iter()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(b));
        if !digest_valid || !safe_sum_path(flavor, relative) {
            add_pairing_diff(
                diffs,
                format!("sha256:{relative}"),
                "安全的相對路徑與 SHA-256",
                line,
            );
            continue;
        }
        if declared
            .insert(relative.to_string(), digest.to_string())
            .is_some()
        {
            add_pairing_diff(
                diffs,
                format!("sha256:{relative}"),
                "唯一 checksum 項目",
                "重複項目",
            );
        }
    }

    let actual_paths = match collect_all_files(ctx, dir) {
        Ok(paths) => paths,
        Err(err) => {
            add_pairing_diff(diffs, "sha256:SHA256SUMS", "可列舉的 bundle 檔案", err);
            return;
        }
    };
    let actual_set: BTreeSet<String> = actual_paths
        .into_iter()
        .filter(|path| path != "release-manifest.json" && path != CHECKSUM_FILE_NAME)
        .collect();

    for relative in &actual_set {
        match declared.get(relative) {
            None => add_pairing_diff(
                diffs,
                format!("sha256:{relative}"),
                "checksum 項目",
                "missing",
            ),
            Some(expected) => {
                let path = dir.join(relative);
                let actual = ctx
                    .p
                    .fs
                    .read(&path)
                    .map(|body| sha256_hex(&body))
                    .unwrap_or_else(|err| format!("unreadable:{}", pal_error_message(&err)));
                if expected != &actual {
                    add_pairing_diff(diffs, format!("sha256:{relative}"), expected, actual);
                }
            }
        }
    }
    for (relative, expected) in &declared {
        if !actual_set.contains(relative) {
            add_pairing_diff(
                diffs,
                format!("sha256:{relative}"),
                expected,
                "missing file",
            );
        }
    }
}

/// 驗證 bundle 目錄：component 檔案與程式樹摘要，以及 `SHA256SUMS`。
fn verify_manifest_matches(
    ctx: &Ctx<'_>,
    dir: &Path,
    expected: &ReleaseManifest,
    diffs: &mut Vec<PairingDiff>,
) {
    let path = dir.join("release-manifest.json");
    let bytes = match ctx.p.fs.read(&path) {
        Ok(bytes) => bytes,
        Err(err) => {
            add_pairing_diff(
                diffs,
                "release-manifest.json",
                "valid manifest matching verifier input",
                format!("unreadable:{}", pal_error_message(&err)),
            );
            return;
        }
    };
    let text = match String::from_utf8(bytes) {
        Ok(text) => text,
        Err(_) => {
            add_pairing_diff(
                diffs,
                "release-manifest.json",
                "valid manifest matching verifier input",
                "invalid UTF-8",
            );
            return;
        }
    };
    match parse_manifest(&text) {
        Ok(actual) if actual.raw == expected.raw => {}
        Ok(_) => add_pairing_diff(
            diffs,
            "release-manifest.json",
            "manifest matching verifier input",
            "different manifest",
        ),
        Err(_) => add_pairing_diff(
            diffs,
            "release-manifest.json",
            "valid manifest matching verifier input",
            "invalid manifest",
        ),
    }
}

pub fn verify_bundle(
    ctx: &Ctx<'_>,
    dir: &Path,
    m: &ReleaseManifest,
) -> Result<(), Vec<PairingDiff>> {
    let mut diffs = Vec::new();
    verify_manifest_matches(ctx, dir, m, &mut diffs);
    for (path_field, hash_field, fallback, field) in [
        (
            Some("components.cli.path"),
            "components.cli.sha256",
            "bin/serialwrap",
            "sha256:bin/serialwrap",
        ),
        (
            Some("components.daemon.launcher.path"),
            "components.daemon.launcher.sha256",
            "bin/serialwrapd",
            "sha256:bin/serialwrapd",
        ),
        (
            Some("components.daemon.runtime.path"),
            "components.daemon.runtime.sha256",
            "py/bin/python3",
            "sha256:py/bin/python3",
        ),
        (
            None,
            "components.assets.skill_sha256",
            "py/sw_core/assets/skill/SKILL.md",
            "sha256:py/sw_core/assets/skill/SKILL.md",
        ),
        (
            None,
            "components.assets.skill_windows_sha256",
            "py/sw_core/assets/skill/SKILL_WINDOWS.md",
            "sha256:py/sw_core/assets/skill/SKILL_WINDOWS.md",
        ),
        (
            None,
            "components.supervisor.definition_sha256",
            "py/sw_core/systemd_units.py",
            "sha256:py/sw_core/systemd_units.py",
        ),
    ] {
        check_file_digest(
            ctx, dir, m, path_field, hash_field, fallback, field, &mut diffs,
        );
    }

    if let Some(component) = m.com0com() {
        check_file_digest(
            ctx,
            dir,
            m,
            Some("components.com0com.helper.path"),
            "components.com0com.helper.sha256",
            "",
            "components.com0com.helper.sha256",
            &mut diffs,
        );
        match com0com_digest(ctx, dir, &component) {
            Ok(actual) if actual == component.sha256 => {}
            Ok(actual) => add_pairing_diff(
                &mut diffs,
                "components.com0com.sha256",
                &component.sha256,
                actual,
            ),
            Err(actual) => add_pairing_diff(
                &mut diffs,
                "components.com0com.sha256",
                &component.sha256,
                actual,
            ),
        }
    }

    let package_tree = tree_sha256_with_prefix(ctx, &dir.join("py").join("sw_core"), "sw_core/");
    match package_tree {
        Ok(actual) => {
            for (field, expected_field) in [
                (
                    "tree_sha256:daemon",
                    "components.daemon.package.tree_sha256",
                ),
                (
                    "tree_sha256:python_cli_delegate",
                    "components.python_cli_delegate.tree_sha256",
                ),
            ] {
                match raw_string(m, expected_field) {
                    Some(expected) if expected == actual => {}
                    Some(expected) => add_pairing_diff(&mut diffs, field, expected, &actual),
                    None => {
                        add_pairing_diff(&mut diffs, field, "declared tree SHA-256", "undeclared")
                    }
                }
            }
        }
        Err(actual) => add_pairing_diff(
            &mut diffs,
            "tree_sha256:daemon",
            raw_string(m, "components.daemon.package.tree_sha256")
                .unwrap_or("declared tree SHA-256"),
            actual,
        ),
    }

    match tree_sha256_with_prefix(ctx, &dir.join("py/sw_core/assets/profiles"), "") {
        Ok(actual) => match raw_string(m, "components.assets.profiles_sha256") {
            Some(expected) if expected == actual => {}
            Some(expected) => {
                add_pairing_diff(&mut diffs, "tree_sha256:profiles", expected, actual)
            }
            None => add_pairing_diff(
                &mut diffs,
                "tree_sha256:profiles",
                "declared tree SHA-256",
                "undeclared",
            ),
        },
        Err(actual) => add_pairing_diff(
            &mut diffs,
            "tree_sha256:profiles",
            raw_string(m, "components.assets.profiles_sha256").unwrap_or("declared tree SHA-256"),
            actual,
        ),
    }

    verify_sha256_sums(ctx, dir, &mut diffs);
    if diffs.is_empty() {
        Ok(())
    } else {
        Err(diffs)
    }
}

fn parse_error(path: &Path, message: impl Into<String>) -> LayoutError {
    LayoutError::Parse {
        path: path.to_path_buf(),
        message: message.into(),
    }
}

/// 複製 bundle 到 `versions/<v>.staging`、驗證後改名為 `versions/<v>`；已存在且驗證通過則冪等回傳。
pub fn stage_version(
    ctx: &Ctx<'_>,
    root: &InstallRoot,
    bundle_dir: &Path,
    m: &ReleaseManifest,
) -> Result<PathBuf, LayoutError> {
    let version = &m.bundle_version;
    let version_parts: Vec<&str> = version.split('.').collect();
    if version_parts.len() != 3
        || version_parts
            .iter()
            .any(|part| part.is_empty() || !part.bytes().all(|b| b.is_ascii_digit()))
    {
        return Err(parse_error(bundle_dir, "bundle_version 格式不合法"));
    }
    let destination = root.versions.join(version);
    if ctx.p.fs.exists(&destination) {
        return match verify_bundle(ctx, &destination, m) {
            Ok(()) => Ok(destination),
            Err(_) => Err(LayoutError::ExistingCorrupt(destination)),
        };
    }

    let staging = root.versions.join(format!("{version}.staging"));
    pal_io(&root.versions, ctx.p.fs.mkdirs(&root.versions))?;
    if ctx.p.fs.exists(&staging) {
        pal_io(&staging, ctx.p.fs.remove_tree(&staging))?;
    }
    pal_io(&staging, ctx.p.fs.copy_tree(bundle_dir, &staging))?;
    if let Err(diffs) = verify_bundle(ctx, &staging, m) {
        let _ = ctx.p.fs.remove_tree(&staging);
        return Err(LayoutError::BundleVerificationFailed {
            path: staging,
            diffs,
        });
    }
    pal_io(&destination, ctx.p.fs.replace(&staging, &destination))?;
    pal_io(&root.versions, ctx.p.fs.sync_dir(&root.versions))?;
    Ok(destination)
}

fn utc_stamp() -> String {
    let seconds = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs() as i64;
    let days = seconds.div_euclid(86_400);
    let day_seconds = seconds.rem_euclid(86_400);
    let z = days + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 }.div_euclid(146_097);
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1_460 + doe / 36_524 - doe / 146_096) / 365;
    let mut year = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let day = doy - (153 * mp + 2) / 5 + 1;
    let month = mp + if mp < 10 { 3 } else { -9 };
    year += i64::from(month <= 2);
    let hour = day_seconds / 3_600;
    let minute = (day_seconds % 3_600) / 60;
    let second = day_seconds % 60;
    format!("{year:04}{month:02}{day:02}-{hour:02}{minute:02}{second:02}")
}

fn old_path(ctx: &Ctx<'_>, root: &InstallRoot, name: &str) -> PathBuf {
    let stamp = utc_stamp();
    let first = root.bin.join(format!("{name}.old.{stamp}"));
    if !ctx.p.fs.exists(&first) {
        return first;
    }
    for suffix in 1..=999u16 {
        let candidate = root.bin.join(format!("{name}.old.{stamp}-{suffix:03}"));
        if !ctx.p.fs.exists(&candidate) {
            return candidate;
        }
    }
    root.bin.join(format!("{name}.old.{stamp}-overflow"))
}

fn remove_if_present(ctx: &Ctx<'_>, path: &Path) -> Result<(), LayoutError> {
    let is_link = ctx
        .p
        .fs
        .read_link(path)
        .map_err(|err| layout_pal_io(path, err))?
        .is_some();
    if ctx.p.fs.exists(path) || is_link {
        pal_io(path, ctx.p.fs.remove_file(path))?;
    }
    Ok(())
}

fn cleanup_old_entries(ctx: &Ctx<'_>, root: &InstallRoot) {
    let Ok(names) = ctx.p.fs.list_dir(&root.bin) else {
        return;
    };
    for name in names {
        if generated_old_entry_name(root, &name) {
            let _ = ctx.p.fs.remove_file(&root.bin.join(name));
        }
    }
}

fn generated_old_entry_name(root: &InstallRoot, name: &str) -> bool {
    root.entry_names.iter().any(|entry| {
        let Some(rest) = name.strip_prefix(&format!("{entry}.old.")) else {
            return false;
        };
        let bytes = rest.as_bytes();
        if bytes.len() < 15
            || bytes[8] != b'-'
            || !bytes
                .iter()
                .enumerate()
                .all(|(index, byte)| index == 8 || byte.is_ascii_digit() || index > 14)
        {
            return false;
        }
        let Some(suffix) = rest.get(15..) else {
            return false;
        };
        suffix.is_empty()
            || suffix == "-overflow"
            || (suffix.len() == 4
                && suffix.starts_with('-')
                && suffix.as_bytes()[1..].iter().all(u8::is_ascii_digit))
    })
}

fn switch_one(
    ctx: &Ctx<'_>,
    root: &InstallRoot,
    version_dir: &Path,
    name: &str,
    report: &mut SwitchReport,
) -> Result<(), LayoutError> {
    let current = root.bin.join(name);
    let staged = root.bin.join(format!("{name}.new"));
    let source = version_dir.join("bin").join(name);
    remove_if_present(ctx, &staged)?;

    match root.entry_style {
        EntryStyle::Copy => {
            pal_io(&staged, ctx.p.fs.copy_preserving(&source, &staged))?;
            let exists = ctx.p.fs.exists(&current)
                || ctx
                    .p
                    .fs
                    .read_link(&current)
                    .map_err(|err| layout_pal_io(&current, err))?
                    .is_some();
            if exists {
                let backup = old_path(ctx, root, name);
                pal_io(&current, ctx.p.fs.replace(&current, &backup))?;
                report.old_files.push(backup);
            }
            report.replaced.push((name.to_string(), None));
            pal_io(&current, ctx.p.fs.replace(&staged, &current))?;
        }
        EntryStyle::Symlink => {
            pal_io(&staged, ctx.p.fs.symlink_file(&staged, &source))?;
            let old_link = ctx
                .p
                .fs
                .read_link(&current)
                .map_err(|err| layout_pal_io(&current, err))?;
            let exists = ctx.p.fs.exists(&current) || old_link.is_some();
            if exists && old_link.is_none() {
                let backup = old_path(ctx, root, name);
                pal_io(&current, ctx.p.fs.replace(&current, &backup))?;
                report.old_files.push(backup);
                report.replaced.push((name.to_string(), None));
            } else {
                report.replaced.push((
                    name.to_string(),
                    old_link.map(|path| path.to_string_lossy().into_owned()),
                ));
            }
            pal_io(&current, ctx.p.fs.replace(&staged, &current))?;
        }
    }
    Ok(())
}

/// 把穩定入口切換到 `version_dir`（Windows 複製＋改名保存；POSIX symlink 原子覆蓋）。
pub fn switch_entries(
    ctx: &Ctx<'_>,
    root: &InstallRoot,
    version_dir: &Path,
) -> Result<SwitchReport, LayoutError> {
    if !version_dir.is_absolute() {
        return Err(parse_error(version_dir, "版本目錄必須是絕對路徑"));
    }
    for name in &root.entry_names {
        let source = version_dir.join("bin").join(name);
        let link = ctx
            .p
            .fs
            .read_link(&source)
            .map_err(|err| layout_pal_io(&source, err))?;
        if !ctx.p.fs.exists(&source) || !ctx.p.fs.is_file(&source) || link.is_some() {
            return Err(parse_error(&source, "版本入口必須是已存在的一般檔案"));
        }
    }
    pal_io(&root.bin, ctx.p.fs.mkdirs(&root.bin))?;
    cleanup_old_entries(ctx, root);
    let mut report = SwitchReport {
        replaced: Vec::new(),
        old_files: Vec::new(),
    };
    for name in &root.entry_names {
        if let Err(err) = switch_one(ctx, root, version_dir, name, &mut report) {
            let revert = revert_entries(ctx, root, &report);
            let mut cleanup_errors = Vec::new();
            for entry in &root.entry_names {
                let staged = root.bin.join(format!("{entry}.new"));
                if let Err(cleanup_error) = remove_if_present(ctx, &staged) {
                    cleanup_errors.push(cleanup_error);
                }
            }
            let revert = match (revert, cleanup_errors.is_empty()) {
                (Ok(()), true) => return Err(err),
                (Ok(()), false) => LayoutError::RevertEntriesFailed(cleanup_errors),
                (Err(revert), true) => revert,
                (Err(revert), false) => {
                    let mut errors = match revert {
                        LayoutError::RevertEntriesFailed(errors) => errors,
                        other => vec![other],
                    };
                    errors.extend(cleanup_errors);
                    LayoutError::RevertEntriesFailed(errors)
                }
            };
            return Err(LayoutError::RevertFailed {
                original: Box::new(err),
                revert: Box::new(revert),
            });
        }
    }
    Ok(report)
}

fn backup_for<'a>(report: &'a SwitchReport, name: &str) -> Option<&'a Path> {
    report.old_files.iter().find_map(|path| {
        let file = path.file_name()?.to_string_lossy();
        file.starts_with(&format!("{name}.old."))
            .then_some(path.as_path())
    })
}

/// 依 `report` 反向還原入口（切換失敗或後續步驟失敗時使用）。
pub fn revert_entries(
    ctx: &Ctx<'_>,
    root: &InstallRoot,
    report: &SwitchReport,
) -> Result<(), LayoutError> {
    let mut errors = Vec::new();
    for (name, old_target) in report.replaced.iter().rev() {
        let current = root.bin.join(name);
        let staged = root.bin.join(format!("{name}.new"));
        if let Err(err) = remove_if_present(ctx, &staged) {
            errors.push(err);
        }
        if let Some(backup) = backup_for(report, name) {
            if let Err(err) = pal_io(&current, ctx.p.fs.replace(backup, &current)) {
                errors.push(err);
            }
        } else if let Some(target) = old_target {
            let restore = root.bin.join(format!("{name}.restore"));
            if let Err(err) = remove_if_present(ctx, &restore) {
                errors.push(err);
            }
            match pal_io(&restore, ctx.p.fs.symlink_file(&restore, Path::new(target))) {
                Ok(()) => {
                    if let Err(err) = pal_io(&current, ctx.p.fs.replace(&restore, &current)) {
                        errors.push(err);
                    }
                }
                Err(err) => errors.push(err),
            }
        } else if let Err(err) = remove_if_present(ctx, &current) {
            errors.push(err);
        }
    }
    if errors.is_empty() {
        Ok(())
    } else {
        Err(LayoutError::RevertEntriesFailed(errors))
    }
}

/// 部署 Python 程式碼的樹狀雜湊（逐字照 P1 `tree_sha256`：排除 `__pycache__` 與 `*.pyc`，relpath 以 `/`，依位元組序）。
pub fn tree_sha256(ctx: &Ctx<'_>, py_root: &Path) -> std::io::Result<String> {
    tree_sha256_with_prefix(ctx, py_root, "").map_err(|err| IoError::new(ErrorKind::Other, err))
}

fn load_manifest(ctx: &Ctx<'_>, path: &Path) -> Result<(PathBuf, ReleaseManifest), LayoutError> {
    let bytes = pal_io(path, ctx.p.fs.read(path))?;
    let text = String::from_utf8(bytes)
        .map_err(|err| parse_error(path, format!("manifest 非 UTF-8：{err}")))?;
    let manifest = parse_manifest(&text)
        .map_err(|err| parse_error(path, format!("release manifest 無效：{err}")))?;
    Ok((path.to_path_buf(), manifest))
}

fn manifest_exists(ctx: &Ctx<'_>, path: &Path) -> bool {
    ctx.p.fs.exists(path) && !ctx.p.fs.is_dir(path)
}

/// 定位 release manifest（`SERIALWRAP_RELEASE_MANIFEST` > realpath(current_exe) 上兩層 > Windows `current-release.json` 的 dir）。
pub fn locate_manifest(ctx: &Ctx<'_>) -> Result<(PathBuf, ReleaseManifest), LayoutError> {
    if let Some(override_path) = ctx
        .env
        .get("SERIALWRAP_RELEASE_MANIFEST")
        .map(str::trim)
        .filter(|value| !value.is_empty())
    {
        let path = expanded_path(ctx, override_path);
        if manifest_exists(ctx, &path) {
            return load_manifest(ctx, &path);
        }
    }

    if let Ok(exe) = ctx.p.env.current_exe() {
        let real = without_verbatim_prefix(&ctx.p.env.realpath(&exe).unwrap_or(exe));
        let bundle_root = real.parent().and_then(Path::parent);
        if let Some(bundle_root) = bundle_root {
            let candidate = bundle_root.join("release-manifest.json");
            if manifest_exists(ctx, &candidate) {
                return load_manifest(ctx, &candidate);
            }
        }
    }

    if path_flavor(ctx) == PathFlavor::Windows {
        let root = resolve_install_root(ctx);
        if let Some(current) = read_current(&root)? {
            let install_dir = if pypath::isabs(path_flavor(ctx), &current.dir) {
                PathBuf::from(&current.dir)
            } else {
                join_path(ctx, &root.root, &current.dir)
            };
            let candidate = install_dir.join("release-manifest.json");
            if manifest_exists(ctx, &candidate) {
                return load_manifest(ctx, &candidate);
            }
        }
    }

    Err(LayoutError::ManifestNotFound(
        "找不到 release-manifest.json".to_string(),
    ))
}

fn dict_string(dict: &mut PyDict, key: &str, value: impl Into<String>) {
    dict.insert(key, PyValue::Str(value.into()));
}

fn lock_error(code: ErrorCode, message: String) -> PyDict {
    let mut result = PyDict::new();
    result.insert("error_code", PyValue::Str(code.as_str().to_string()));
    result.insert("message", PyValue::Str(message));
    result.insert("ok", PyValue::Bool(false));
    dict_string(&mut result, "phase", "install_lock");
    result
}

/// 取得 `<root>/.install.lock`；每 0.2 s 非阻塞嘗試，5 s 內取不到回 `TIMEOUT`／`phase:"install_lock"` 的 PyDict。
pub fn install_lock(ctx: &Ctx<'_>, root: &InstallRoot) -> Result<FileLockGuard, PyDict> {
    let path = root.root.join(".install.lock");
    if let Err(err) = ctx.p.fs.mkdirs(&root.root) {
        return Err(lock_error(
            ErrorCode::InternalError,
            format!(
                "無法建立安裝根 {}：{}",
                root.root.display(),
                pal_error_message(&err)
            ),
        ));
    }
    let started = ctx.p.clock.mono_ns();
    loop {
        match ctx.p.fs.file_lock_exclusive(&path, false) {
            Ok(guard) => return Ok(guard),
            Err(err) if err.kind == PalErrorKind::WouldBlock => {
                let elapsed = ctx.p.clock.mono_ns().saturating_sub(started);
                if elapsed >= 5_000_000_000 {
                    return Err(lock_error(
                        ErrorCode::Timeout,
                        format!("另一個 setup／更新／回滾正在進行（{}）", path.display()),
                    ));
                }
                ctx.p.clock.sleep(Duration::from_millis(200));
            }
            Err(err) => {
                return Err(lock_error(
                    ErrorCode::InternalError,
                    format!(
                        "無法取得安裝鎖 {}：{}",
                        path.display(),
                        pal_error_message(&err)
                    ),
                ));
            }
        }
    }
}
