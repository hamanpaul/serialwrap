//! Stage 1A 委派路由與 manifest 固定入口執行。

use crate::ctx::CliCtx;
use crate::output;
use std::ffi::OsString;
use std::path::{Path, PathBuf};

/// 委派集合（arch §7.4、計畫 §0.3）；`cli_spec` 與 `run` 共用。
pub const DELEGATED_GROUPS: [&str; 7] = [
    "daemon",
    "connect",
    "benches",
    "supervision-mode",
    "service",
    "setup",
    "doctor",
];

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Route {
    Native,
    Delegate { group: &'static str },
}

/// 在任何有損 UTF-8 轉換之前執行：逐 token 以 `to_str()` 比對選項與群組名。
pub fn classify(argv_tail: &[OsString]) -> Route {
    const VALUE_OPTIONS: [&str; 5] = [
        "--socket",
        "--endpoint",
        "--bench",
        "--timeout",
        "--retries",
    ];
    let mut index = 0;
    while index < argv_tail.len() {
        let Some(token) = argv_tail[index].to_str() else {
            return Route::Native;
        };
        if token == "--" {
            let Some(group) = argv_tail.get(index + 1).and_then(|value| value.to_str()) else {
                return Route::Native;
            };
            return if DELEGATED_GROUPS.contains(&group) {
                Route::Delegate {
                    group: DELEGATED_GROUPS
                        .iter()
                        .copied()
                        .find(|item| *item == group)
                        .unwrap(),
                }
            } else {
                Route::Native
            };
        }
        if token.starts_with('-') && token != "-" {
            let option = token.split_once('=').map_or(token, |(option, _)| option);
            let takes_value = option.starts_with("--")
                && VALUE_OPTIONS
                    .iter()
                    .filter(|candidate| candidate.starts_with(option))
                    .count()
                    == 1;
            index += if takes_value && !token.contains('=') {
                2
            } else {
                1
            };
            continue;
        }
        return if DELEGATED_GROUPS.contains(&token) {
            Route::Delegate {
                group: DELEGATED_GROUPS
                    .iter()
                    .copied()
                    .find(|item| *item == token)
                    .unwrap(),
            }
        } else {
            Route::Native
        };
    }
    Route::Native
}

#[derive(Clone, Debug, PartialEq)]
pub enum DelegateError {
    Unavailable(String),
    Recursion,
}

/// sha256(各元素 `OsStr::as_encoded_bytes()` 以 `"\0"` 連接)。
pub fn argv_sha256(argv_tail: &[OsString]) -> String {
    let mut encoded = Vec::new();
    for (index, value) in argv_tail.iter().enumerate() {
        if index != 0 {
            encoded.push(0);
        }
        encoded.extend_from_slice(value.as_encoded_bytes());
    }
    sw_proto::pycompat::sha256_hex(&encoded)
}

/// `SERIALWRAP_DELEGATE_DEPTH`／`SERIALWRAP_DELEGATE_ARGV_SHA` 遞迴防護；回傳 incoming depth。
pub fn check_recursion(
    depth_raw: Option<&str>,
    sha_raw: Option<&str>,
    my_sha: &str,
) -> Result<u32, DelegateError> {
    let depth = match depth_raw {
        None => 0,
        Some(raw) if !raw.is_empty() && raw.bytes().all(|byte| byte.is_ascii_digit()) => {
            raw.parse::<u32>().map_err(|_| DelegateError::Recursion)?
        }
        Some(_) => return Err(DelegateError::Recursion),
    };
    if depth >= 2 || sha_raw == Some(my_sha) {
        return Err(DelegateError::Recursion);
    }
    Ok(depth)
}

pub struct DelegateTarget {
    pub argv: Vec<OsString>,
    pub manifest_path: PathBuf,
}

pub fn resolve_target(
    p: &sw_pal::ClientPlatform,
    argv_tail: &[OsString],
) -> Result<DelegateTarget, DelegateError> {
    let exe = p.env.current_exe().map_err(|_| {
        DelegateError::Unavailable("release manifest not found: current executable".to_string())
    })?;
    let exe_dir = exe.parent().ok_or_else(|| {
        DelegateError::Unavailable(format!("release manifest not found: {}", exe.display()))
    })?;
    let current_release_path = exe_dir.join("current-release.json");

    let manifest_path = if p.fs.exists(&current_release_path) {
        let bytes = p.fs.read(&current_release_path).map_err(|error| {
            DelegateError::Unavailable(format!(
                "invalid release manifest: current-release.json: {}",
                error.py_message
            ))
        })?;
        let text = std::str::from_utf8(&bytes).map_err(|error| {
            DelegateError::Unavailable(format!(
                "invalid release manifest: current-release.json: {error}"
            ))
        })?;
        let current = sw_proto::release_manifest::parse_current_release(text).map_err(|error| {
            DelegateError::Unavailable(format!(
                "invalid release manifest: current-release.json: {}",
                manifest_error_detail(error)
            ))
        })?;
        let bundle_root = exe_dir.parent().ok_or_else(|| {
            DelegateError::Unavailable(format!("release manifest not found: {}", exe.display()))
        })?;
        join_relative(bundle_root, &current.dir).join("release-manifest.json")
    } else {
        let real_exe = p.env.realpath(&exe).map_err(|_| {
            DelegateError::Unavailable(format!("release manifest not found: {}", exe.display()))
        })?;
        let version_dir = real_exe.parent().and_then(Path::parent).ok_or_else(|| {
            DelegateError::Unavailable(format!(
                "release manifest not found: {}",
                real_exe.display()
            ))
        })?;
        version_dir.join("release-manifest.json")
    };

    if !p.fs.exists(&manifest_path) {
        return Err(DelegateError::Unavailable(format!(
            "release manifest not found: {}",
            manifest_path.display()
        )));
    }
    let bytes = p.fs.read(&manifest_path).map_err(|error| {
        DelegateError::Unavailable(format!("invalid release manifest: {}", error.py_message))
    })?;
    let text = std::str::from_utf8(&bytes).map_err(|error| {
        DelegateError::Unavailable(format!("invalid release manifest: {error}"))
    })?;
    let manifest = sw_proto::release_manifest::parse_manifest(text).map_err(|error| {
        DelegateError::Unavailable(format!(
            "invalid release manifest: {}",
            manifest_error_detail(error)
        ))
    })?;

    let manifest_dir = manifest_path.parent().unwrap_or_else(|| Path::new(""));
    let entry_raw = &manifest.python_cli_delegate_entry[0];
    let entry_path = if Path::new(entry_raw).is_absolute() {
        PathBuf::from(entry_raw)
    } else {
        join_relative(manifest_dir, entry_raw)
    };
    if !p.fs.exists(&entry_path) || p.fs.is_dir(&entry_path) {
        return Err(DelegateError::Unavailable(format!(
            "delegate entry not found: {}",
            entry_path.display()
        )));
    }

    let mut argv = Vec::with_capacity(
        manifest
            .python_cli_delegate_entry
            .len()
            .saturating_add(argv_tail.len()),
    );
    argv.push(entry_path.into_os_string());
    argv.extend(
        manifest
            .python_cli_delegate_entry
            .iter()
            .skip(1)
            .map(OsString::from),
    );
    argv.extend(argv_tail.iter().cloned());

    Ok(DelegateTarget {
        argv,
        manifest_path,
    })
}

/// 只在錯誤時返回 1。
/// 以字串逐段接上 manifest／current-release.json 內的相對路徑（不 canonicalize、不解析 symlink）。
///
/// 檔案內容一律以 `/` 分段；逐段 `push` 讓 Windows 產生原生 `\\` 分隔的路徑（`CreateProcessW`
/// 的 `lpApplicationName` 與比對皆以此為準），POSIX 結果與 `Path::join` 相同。
fn join_relative(base: &Path, rel: &str) -> PathBuf {
    if Path::new(rel).is_absolute() {
        return PathBuf::from(rel);
    }
    let mut out = base.to_path_buf();
    for segment in rel.split('/').filter(|segment| !segment.is_empty()) {
        out.push(segment);
    }
    out
}

pub fn run_delegate(ctx: &mut CliCtx, argv_tail: &[OsString], group: &str) -> i32 {
    let argv_sha = argv_sha256(argv_tail);
    let depth = match check_recursion(
        ctx.p.env.var("SERIALWRAP_DELEGATE_DEPTH").as_deref(),
        ctx.p.env.var("SERIALWRAP_DELEGATE_ARGV_SHA").as_deref(),
        &argv_sha,
    ) {
        Ok(depth) => depth,
        Err(error) => return print_delegate_error(ctx, group, error),
    };
    let target = match resolve_target(ctx.p, argv_tail) {
        Ok(target) => target,
        Err(error) => return print_delegate_error(ctx, group, error),
    };

    let env_add = [
        (
            "SERIALWRAP_DELEGATE_DEPTH".to_string(),
            (depth + 1).to_string(),
        ),
        ("SERIALWRAP_DELEGATE_ARGV_SHA".to_string(), argv_sha),
        (
            "SERIALWRAP_RELEASE_MANIFEST".to_string(),
            target.manifest_path.to_string_lossy().into_owned(),
        ),
    ];
    let error = ctx.p.process.exec_or_wait(&target.argv, &env_add);
    print_delegate_error(
        ctx,
        group,
        DelegateError::Unavailable(format!("exec failed: {}", error.py_message)),
    )
}

fn print_delegate_error(ctx: &mut CliCtx, group: &str, error: DelegateError) -> i32 {
    let message = match error {
        DelegateError::Unavailable(reason) => format!("delegation target unavailable: {reason}"),
        DelegateError::Recursion => "delegation recursion detected".to_string(),
    };
    output::print_internal_error(&mut ctx.io, group, &message)
}

fn manifest_error_detail(error: sw_proto::release_manifest::ManifestError) -> String {
    match error {
        sw_proto::release_manifest::ManifestError::InvalidJson(detail)
        | sw_proto::release_manifest::ManifestError::Schema(detail) => detail,
    }
}
