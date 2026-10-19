//! Release manifest 版本配對判定（P4-T11 承接；doctor 的 `version_pairing` 與 update／rollback 共用）。

use std::fs;
use std::io::{Error as IoError, ErrorKind};
use std::path::{Path, PathBuf};

use sw_proto::digest::sha256_hex;
use sw_proto::pyjson::{PyDict, PyValue};
use sw_proto::release_manifest::ReleaseManifest;

/// 單一配對差異（`field` 名稱：`cli_version`、`daemon_version`、`bundle_version`、檔案與樹狀摘要）。
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PairingDiff {
    pub field: String,
    pub expected: String,
    pub actual: String,
}

fn value<'a>(m: &'a ReleaseManifest, dotted: &str) -> Option<&'a PyValue> {
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

fn string<'a>(m: &'a ReleaseManifest, dotted: &str) -> Option<&'a str> {
    match value(m, dotted)? {
        PyValue::Str(s) => Some(s),
        _ => None,
    }
}

fn expected_digest<'a>(m: &'a ReleaseManifest, path: &str) -> Option<&'a str> {
    for (path_field, digest_field) in [
        ("components.cli.path", "components.cli.sha256"),
        (
            "components.daemon.launcher.path",
            "components.daemon.launcher.sha256",
        ),
        (
            "components.daemon.runtime.path",
            "components.daemon.runtime.sha256",
        ),
    ] {
        if string(m, path_field) == Some(path) {
            return string(m, digest_field);
        }
    }
    match path {
        "tree_sha256:daemon" => string(m, "components.daemon.package.tree_sha256"),
        "tree_sha256:python_cli_delegate" => {
            string(m, "components.python_cli_delegate.tree_sha256")
        }
        _ => None,
    }
    .or_else(|| {
        if path.eq_ignore_ascii_case("stable_entry:serialwrap")
            || path.eq_ignore_ascii_case("stable_entry:serialwrap.exe")
        {
            string(m, "components.cli.sha256")
        } else if path.eq_ignore_ascii_case("stable_entry:serialwrapd")
            || path.eq_ignore_ascii_case("stable_entry:serialwrapd.exe")
        {
            string(m, "components.daemon.launcher.sha256")
        } else {
            None
        }
    })
}

fn safe_component_path(value: &str) -> bool {
    let normalized = value.replace('\\', "/");
    let bytes = normalized.as_bytes();
    let windows_drive = bytes.len() >= 2 && bytes[0].is_ascii_alphabetic() && bytes[1] == b':';
    !normalized.is_empty()
        && !normalized.starts_with('/')
        && !windows_drive
        && !normalized
            .split('/')
            .any(|part| part == ".." || part.is_empty())
}

fn push_diff(diffs: &mut Vec<PairingDiff>, field: &str, expected: &str, actual: &str) {
    diffs.push(PairingDiff {
        field: field.to_string(),
        expected: expected.to_string(),
        actual: actual.to_string(),
    });
}

/// update／rollback 內部用的結構化配對檢查（`pairing.rule == "exact"`）。
/// `digests` 為 `(component_path, actual_sha256)`，須與 manifest 宣告相同。
pub fn check_pairing(
    m: &ReleaseManifest,
    cli_version: &str,
    daemon_version: Option<&str>,
    digests: &[(String, String)],
) -> Vec<PairingDiff> {
    let mut diffs = Vec::new();
    let bundle = m.bundle_version.as_str();
    if string(m, "pairing.rule") != Some("exact") {
        push_diff(
            &mut diffs,
            "pairing.rule",
            "exact",
            string(m, "pairing.rule").unwrap_or("missing"),
        );
    }

    let declared_cli = string(m, "pairing.cli_version").unwrap_or("missing");
    let component_cli = string(m, "components.cli.version").unwrap_or("missing");
    if cli_version != bundle || declared_cli != bundle || component_cli != bundle {
        let actual = if cli_version != bundle {
            cli_version
        } else if declared_cli != bundle {
            declared_cli
        } else {
            component_cli
        };
        push_diff(&mut diffs, "cli_version", bundle, actual);
    }
    if component_cli != bundle {
        push_diff(&mut diffs, "bundle_version", bundle, component_cli);
    }

    if let Some(actual_daemon) = daemon_version {
        let declared_daemon = string(m, "pairing.daemon_version").unwrap_or("missing");
        let component_daemon = string(m, "components.daemon.version").unwrap_or("missing");
        if actual_daemon != bundle || declared_daemon != bundle || component_daemon != bundle {
            let actual = if actual_daemon != bundle {
                actual_daemon
            } else if declared_daemon != bundle {
                declared_daemon
            } else {
                component_daemon
            };
            push_diff(&mut diffs, "daemon_version", bundle, actual);
        }
        if component_daemon != bundle {
            push_diff(&mut diffs, "bundle_version", bundle, component_daemon);
        }
    }

    for (path, actual) in digests {
        let field = if path.starts_with("tree_sha256:") || path.starts_with("stable_entry:") {
            path.clone()
        } else {
            format!("sha256:{path}")
        };
        if !path.starts_with("tree_sha256:")
            && !path.starts_with("stable_entry:")
            && !safe_component_path(path)
        {
            push_diff(&mut diffs, &field, "bundle-relative path", path);
            continue;
        }
        match expected_digest(m, path) {
            Some(expected) if expected == actual => {}
            Some(expected) => push_diff(&mut diffs, &field, expected, actual),
            None => push_diff(&mut diffs, &field, "declared component digest", actual),
        }
    }
    diffs
}

fn manifest_path(manifest_dir: &Path, value: Option<&str>) -> Option<PathBuf> {
    let value = value?;
    let normalized = value.replace('\\', "/");
    if !safe_component_path(&normalized) {
        return None;
    }
    Some(manifest_dir.join(normalized))
}

fn file_matches(manifest_dir: &Path, m: &ReleaseManifest, path_key: &str, hash_key: &str) -> bool {
    let (Some(path), Some(expected)) = (
        manifest_path(manifest_dir, string(m, path_key)),
        string(m, hash_key),
    ) else {
        return false;
    };
    fs::read(path)
        .map(|body| sha256_hex(&body) == expected)
        .unwrap_or(false)
}

fn package_tree_sha256(manifest_dir: &Path) -> std::io::Result<String> {
    crate::tree_digest::digest_tree(
        &crate::tree_digest::NativeTreeSource,
        &manifest_dir.join("py").join("sw_core"),
        "sw_core/",
        true,
    )
    .map_err(|message| IoError::new(ErrorKind::Other, message))
}

fn valid_sha256(value: Option<&str>) -> bool {
    value.is_some_and(|s| {
        s.len() == 64
            && s.as_bytes()
                .iter()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(b))
    })
}

/// doctor 的 `version_pairing` 項目（逐字照 P1 `check_version_pairing` 的判定與 `mismatches` 順序）。
/// daemon 不可達時 `daemon_version` 為 `None`。
pub fn pairing_item(
    m: &ReleaseManifest,
    manifest_dir: &Path,
    cli_version: &str,
    daemon_version: Option<&str>,
) -> PyDict {
    let mut mismatches = Vec::new();
    let bundle = m.bundle_version.as_str();
    if string(m, "pairing.rule") != Some("exact") {
        mismatches.push("pairing_rule".to_string());
    }
    let cli_declared = string(m, "pairing.cli_version") == Some(bundle)
        && string(m, "components.cli.version") == Some(bundle);
    if cli_version != bundle || !cli_declared {
        mismatches.push("cli_version".to_string());
    }

    let daemon_declared = string(m, "pairing.daemon_version") == Some(bundle)
        && string(m, "components.daemon.version") == Some(bundle);
    if daemon_version.is_none() {
        if !daemon_declared {
            mismatches.push("daemon_version".to_string());
        }
        mismatches.push("daemon_unreachable".to_string());
    } else if daemon_version != Some(bundle) || !daemon_declared {
        mismatches.push("daemon_version".to_string());
    }

    if !file_matches(
        manifest_dir,
        m,
        "components.cli.path",
        "components.cli.sha256",
    ) {
        mismatches.push("cli_sha256".to_string());
    }
    if !file_matches(
        manifest_dir,
        m,
        "components.daemon.launcher.path",
        "components.daemon.launcher.sha256",
    ) {
        mismatches.push("launcher_sha256".to_string());
    }
    if !file_matches(
        manifest_dir,
        m,
        "components.daemon.runtime.path",
        "components.daemon.runtime.sha256",
    ) {
        mismatches.push("runtime_sha256".to_string());
    }

    let actual_tree = package_tree_sha256(manifest_dir).ok();
    if string(m, "components.daemon.package.tree_sha256") != actual_tree.as_deref() {
        mismatches.push("package_tree_sha256".to_string());
    }
    if string(m, "components.python_cli_delegate.tree_sha256") != actual_tree.as_deref() {
        mismatches.push("python_cli_delegate_tree_sha256".to_string());
    }

    for (key, relative, mismatch) in [
        (
            "components.assets.skill_sha256",
            "py/sw_core/assets/skill/SKILL.md",
            "skill_sha256",
        ),
        (
            "components.assets.skill_windows_sha256",
            "py/sw_core/assets/skill/SKILL_WINDOWS.md",
            "skill_windows_sha256",
        ),
    ] {
        let matches = string(m, key)
            .and_then(|expected| {
                fs::read(manifest_dir.join(relative))
                    .ok()
                    .map(|b| (expected, b))
            })
            .is_some_and(|(expected, body)| sha256_hex(&body) == expected);
        if !matches {
            mismatches.push(mismatch.to_string());
        }
    }
    if !valid_sha256(string(m, "components.assets.profiles_sha256")) {
        mismatches.push("profiles_sha256_format".to_string());
    }
    if !valid_sha256(string(m, "components.supervisor.definition_sha256")) {
        mismatches.push("supervisor_definition_sha256_format".to_string());
    }

    let mut item = PyDict::new();
    item.insert("advisory", PyValue::Bool(false));
    item.insert("check", PyValue::Str("version_pairing".to_string()));
    item.insert(
        "detail",
        PyValue::Str(if mismatches.is_empty() {
            "ok".to_string()
        } else {
            mismatches.join(", ")
        }),
    );
    item.insert(
        "mismatches",
        PyValue::List(mismatches.into_iter().map(PyValue::Str).collect()),
    );
    let ok = matches!(item.get("mismatches"), Some(PyValue::List(items)) if items.is_empty());
    item.insert("ok", PyValue::Bool(ok));
    item
}
