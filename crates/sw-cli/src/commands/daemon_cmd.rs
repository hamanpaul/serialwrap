//! `serialwrap daemon` 子命令與 daemon 定位（P4 DF-05 `locate_daemon_argv`；P9-T16 加入退役守門）。
//!
//! 定位順序依 DF-05 契約；退役 manifest 不得回退到 Python daemon。

use std::path::{Path, PathBuf};
use sw_proto::release_manifest::ReleaseManifest;

/// daemon 定位所需的執行環境。
pub struct Ctx {
    /// `release-manifest.json` 所在路徑。
    pub manifest_path: PathBuf,
    /// 目前 CLI 執行檔所在目錄（DF-05 第 2 步的同目錄 `serialwrapd`）。
    pub exe_dir: PathBuf,
}

/// daemon 定位錯誤（沿用既有 `DAEMON_BINARY_NOT_FOUND`，不新增錯誤碼）。
#[derive(Debug, PartialEq, Eq)]
pub enum DaemonLocateError {
    BinaryNotFound,
}

/// DF-05 定位順序：`components.daemon.launcher.path` → 同目錄 `serialwrapd<suffix>` →
/// `components.daemon.runtime.path` ＋ `-I -B -m sw_core.daemon`（退役 manifest 一律跳過第 3 步）→ `BinaryNotFound`。
pub fn locate_daemon_argv(ctx: &Ctx) -> Result<Vec<String>, DaemonLocateError> {
    let text = match std::fs::read_to_string(&ctx.manifest_path) {
        Ok(text) => text,
        Err(_) => return sibling_argv(&ctx.exe_dir).ok_or(DaemonLocateError::BinaryNotFound),
    };
    let manifest = match ReleaseManifest::parse(&text) {
        Ok(manifest) => manifest,
        Err(_) => return sibling_argv(&ctx.exe_dir).ok_or(DaemonLocateError::BinaryNotFound),
    };
    let manifest_dir = ctx.manifest_path.parent().unwrap_or_else(|| Path::new("."));
    locate_from_manifest(&manifest, manifest_dir, &ctx.exe_dir)
}

fn sibling_argv(exe_dir: &Path) -> Option<Vec<String>> {
    let sibling = exe_dir.join(format!("serialwrapd{}", std::env::consts::EXE_SUFFIX));
    sibling
        .is_file()
        .then(|| vec![sibling.to_string_lossy().into_owned()])
}

fn locate_from_manifest(
    manifest: &ReleaseManifest,
    manifest_dir: &Path,
    exe_dir: &Path,
) -> Result<Vec<String>, DaemonLocateError> {
    let components = manifest
        .raw
        .get("components")
        .and_then(|value| value.as_object());
    let daemon = components.and_then(|components| components.get("daemon"));

    if let Some(path) = daemon.and_then(|daemon| {
        daemon
            .get("launcher")
            .and_then(|launcher| launcher.get("path"))
            .and_then(|path| path.as_str())
    }) {
        let path = PathBuf::from(path);
        let resolved = if path.is_absolute() {
            path
        } else {
            manifest_dir.join(path)
        };
        if resolved.is_file() {
            return Ok(vec![resolved.to_string_lossy().into_owned()]);
        }
    }

    if let Some(argv) = sibling_argv(exe_dir) {
        return Ok(argv);
    }

    // 退役後即使 raw manifest 被注入 runtime.path，也不允許回退到 Python daemon。
    if manifest.python_daemon_retired() {
        return Err(DaemonLocateError::BinaryNotFound);
    }
    let runtime = daemon
        .and_then(|daemon| {
            daemon
                .get("runtime")
                .and_then(|value| value.get("path"))
                .and_then(|path| path.as_str())
        })
        .ok_or(DaemonLocateError::BinaryNotFound)?;
    let runtime = PathBuf::from(runtime);
    let runtime = if runtime.is_absolute() {
        runtime
    } else {
        manifest_dir.join(runtime)
    };
    Ok(vec![
        runtime.to_string_lossy().into_owned(),
        "-I".into(),
        "-B".into(),
        "-m".into(),
        "sw_core.daemon".into(),
    ])
}

#[cfg(test)]
mod retired_tests {
    use super::{locate_daemon_argv, locate_from_manifest, Ctx, DaemonLocateError};
    use std::path::{Path, PathBuf};
    use std::sync::atomic::{AtomicUsize, Ordering};
    use sw_proto::release_manifest::ReleaseManifest;

    static SEQ: AtomicUsize = AtomicUsize::new(0);

    /// 夾具讀取（`tests/fixtures/`）。
    mod fixtures {
        fn read(name: &str) -> String {
            std::fs::read_to_string(format!(
                "{}/tests/fixtures/{name}",
                env!("CARGO_MANIFEST_DIR")
            ))
            .expect("夾具必須存在")
        }

        /// 合法的退役 Stage 3 manifest。
        pub fn stage3_retired_rust() -> String {
            read("stage3_retired_rust.json")
        }

        /// 未退役的 stage 1B Python daemon（DF-05 第 3 步仍適用）。
        pub fn stage1b_python_daemon() -> String {
            read("stage1b_python_daemon.json")
        }
    }

    /// 在暫存目錄建立 manifest 與定位測試所需的檔案；`Drop` 時清除目錄。
    struct TestEnv {
        root: PathBuf,
        manifest: String,
        launcher: bool,
        sibling: bool,
    }

    impl TestEnv {
        fn with_manifest(manifest: String) -> Self {
            let n = SEQ.fetch_add(1, Ordering::SeqCst);
            let root =
                std::env::temp_dir().join(format!("sw-cli-retired-{}-{n}", std::process::id()));
            Self {
                root,
                manifest,
                launcher: true,
                sibling: true,
            }
        }

        fn without_launcher(mut self) -> Self {
            self.launcher = false;
            self
        }

        fn without_sibling(mut self) -> Self {
            self.sibling = false;
            self
        }

        fn ctx(&self) -> Ctx {
            let exe_dir = self.root.join("exe");
            std::fs::create_dir_all(&exe_dir).unwrap();
            let manifest_path = self.root.join("release-manifest.json");
            std::fs::write(&manifest_path, &self.manifest).unwrap();
            if self.launcher {
                std::fs::create_dir_all(self.root.join("bin")).unwrap();
                std::fs::write(self.root.join("bin").join("serialwrapd"), b"stub").unwrap();
            }
            if self.sibling {
                std::fs::write(
                    exe_dir.join(format!("serialwrapd{}", std::env::consts::EXE_SUFFIX)),
                    b"stub",
                )
                .unwrap();
            }
            Ctx {
                manifest_path,
                exe_dir,
            }
        }
    }

    impl Drop for TestEnv {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.root);
        }
    }

    #[test]
    fn retired_manifest_never_falls_back_to_python_runtime() {
        let parsed = ReleaseManifest::parse(&fixtures::stage3_retired_rust()).unwrap();
        let mut raw = parsed.raw.clone();
        let runtime = raw["components"]["daemon"]["launcher"].clone();
        raw["components"]["daemon"]["runtime"] = runtime;
        let manifest = ReleaseManifest {
            schema_version: parsed.schema_version,
            bundle_version: parsed.bundle_version,
            stage: parsed.stage,
            target: parsed.target,
            git_revision: parsed.git_revision,
            python_cli_delegate_entry: parsed.python_cli_delegate_entry,
            raw,
        };
        assert!(matches!(
            locate_from_manifest(&manifest, Path::new("bundle"), Path::new("exe")),
            Err(DaemonLocateError::BinaryNotFound)
        ));
    }

    #[test]
    fn manifestless_lookup_uses_same_directory_sibling() {
        let env = TestEnv::with_manifest(fixtures::stage3_retired_rust()).without_launcher();
        let ctx = env.ctx();
        std::fs::remove_file(&ctx.manifest_path).unwrap();
        let expected = ctx
            .exe_dir
            .join(format!("serialwrapd{}", std::env::consts::EXE_SUFFIX));
        assert_eq!(
            locate_daemon_argv(&ctx).unwrap(),
            [expected.to_string_lossy()]
        );
    }

    #[test]
    fn injected_python_runtime_manifest_never_launches_runtime() {
        let env = TestEnv::with_manifest(
            include_str!("../../tests/fixtures/stage3_retired_but_runtime_injected.json")
                .to_owned(),
        )
        .without_launcher()
        .without_sibling();
        assert!(matches!(
            locate_daemon_argv(&env.ctx()),
            Err(DaemonLocateError::BinaryNotFound)
        ));
    }

    #[test]
    fn sibling_lookup_rejects_wrong_platform_suffix() {
        let env = TestEnv::with_manifest(fixtures::stage3_retired_rust())
            .without_launcher()
            .without_sibling();
        let ctx = env.ctx();
        let wrong_name = if std::env::consts::EXE_SUFFIX.is_empty() {
            "serialwrapd.exe"
        } else {
            "serialwrapd"
        };
        std::fs::write(ctx.exe_dir.join(wrong_name), b"wrong platform").unwrap();

        assert!(matches!(
            locate_daemon_argv(&ctx),
            Err(DaemonLocateError::BinaryNotFound)
        ));
    }

    #[test]
    fn unretired_keeps_df05_order() {
        let env = TestEnv::with_manifest(fixtures::stage1b_python_daemon())
            .without_launcher()
            .without_sibling();
        let argv = locate_daemon_argv(&env.ctx()).unwrap();
        assert_eq!(&argv[1..], ["-I", "-B", "-m", "sw_core.daemon"]);
    }
}
