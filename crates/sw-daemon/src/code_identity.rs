//! daemon 啟動時捕捉 bundle 指紋與路徑。

use std::path::Path;

use sw_proto::release_manifest::parse_manifest;

#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub(crate) struct CodeIdentity {
    pub code_fingerprint: Option<String>,
    pub code_path: Option<String>,
}

/// 由啟動時解析出的 manifest 路徑讀取一次，不使用編譯期或其他來源回退。
pub(crate) fn capture(
    executable: Option<&Path>,
    manifest_path: Option<&Path>,
    read_manifest: impl FnOnce(&Path) -> Option<Vec<u8>>,
) -> CodeIdentity {
    let code_path = executable
        .and_then(|path| path.parent()?.parent())
        .map(|path| path.to_string_lossy().into_owned());
    let code_fingerprint = manifest_path
        .and_then(read_manifest)
        .and_then(|bytes| String::from_utf8(bytes).ok())
        .and_then(|text| parse_manifest(&text).ok())
        .and_then(|manifest| manifest.code_fingerprint);
    CodeIdentity {
        code_fingerprint,
        code_path,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicUsize, Ordering};

    const MANIFEST: &str = r#"{"schema_version":1,"bundle_version":"1.0.0","stage":"dev","target":"x86_64-unknown-linux-gnu","git_revision":"abc123","components":{"daemon":{"code_fingerprint":"0123456789ab"},"python_cli_delegate":{"entry":["bin/serialwrap"]}}}"#;

    #[test]
    fn manifest_fingerprint_is_captured_once_with_bundle_root() {
        let reads = AtomicUsize::new(0);
        let identity = capture(
            Some(Path::new("/opt/serialwrap/bin/serialwrapd")),
            Some(Path::new("/opt/serialwrap/release-manifest.json")),
            |path| {
                assert_eq!(path, Path::new("/opt/serialwrap/release-manifest.json"));
                reads.fetch_add(1, Ordering::SeqCst);
                Some(MANIFEST.as_bytes().to_vec())
            },
        );
        assert_eq!(reads.load(Ordering::SeqCst), 1);
        assert_eq!(identity.code_fingerprint.as_deref(), Some("0123456789ab"));
        assert_eq!(identity.code_path.as_deref(), Some("/opt/serialwrap"));
    }

    #[test]
    fn missing_manifest_fingerprint_stays_unknown_without_fallback() {
        let identity = capture(
            Some(Path::new("/opt/serialwrap/bin/serialwrapd")),
            Some(Path::new("/opt/serialwrap/release-manifest.json")),
            |_| {
                Some(
                    MANIFEST
                        .replace("\"code_fingerprint\":\"0123456789ab\"", "")
                        .into_bytes(),
                )
            },
        );
        assert_eq!(identity.code_fingerprint, None);
        assert_eq!(identity.code_path.as_deref(), Some("/opt/serialwrap"));
    }

    #[test]
    fn unreadable_manifest_keeps_fingerprint_unknown() {
        let calls = AtomicUsize::new(0);
        let identity = capture(
            Some(Path::new("/opt/serialwrap/bin/serialwrapd")),
            Some(Path::new("/opt/serialwrap/release-manifest.json")),
            |_| {
                calls.fetch_add(1, Ordering::SeqCst);
                None
            },
        );
        assert_eq!(calls.load(Ordering::SeqCst), 1);
        assert_eq!(identity.code_fingerprint, None);
    }

    #[test]
    fn absent_executable_or_manifest_has_no_fingerprint_or_path() {
        let identity = capture(None, None, |_| panic!("無 manifest 路徑時不可讀取"));
        assert_eq!(identity, CodeIdentity::default());
    }
}
