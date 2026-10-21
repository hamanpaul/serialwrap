//! `serialwrap daemon` 子命令與 daemon 定位（P4 DF-05 `locate_daemon_argv`；P9-T16 加入退役守門）。
//!
//! 紅燈階段替身：`locate_daemon_argv` 本體為 `todo!()`，`Ctx` 只保留定位所需的最小欄位，
//! 使 `retired_tests` 能編譯並於執行期失敗。P4 合入後以真實 `Ctx`／`TestEnv`（sw-pal fakes）取代。

use std::path::PathBuf;

/// 定位所需的最小執行環境（替身；P4 的 `Ctx` 含更多欄位）。
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
pub fn locate_daemon_argv(_ctx: &Ctx) -> Result<Vec<String>, DaemonLocateError> {
    todo!("P9-T16 紅燈：locate_daemon_argv 退役守門尚未實作")
}

#[cfg(test)]
mod retired_tests {
    use super::{locate_daemon_argv, Ctx, DaemonLocateError};
    use std::path::PathBuf;
    use std::sync::atomic::{AtomicUsize, Ordering};

    static SEQ: AtomicUsize = AtomicUsize::new(0);

    /// 夾具讀取（`tests/fixtures/`）。
    mod fixtures {
        fn read(name: &str) -> String {
            std::fs::read_to_string(format!("{}/tests/fixtures/{name}", env!("CARGO_MANIFEST_DIR")))
                .expect("夾具必須存在")
        }

        /// 已退役（stage 3），但被注入 `components.daemon.runtime`（應被忽略）。
        pub fn stage3_retired_but_runtime_injected() -> String {
            read("stage3_retired_but_runtime_injected.json")
        }

        /// 未退役的 stage 1B Python daemon（DF-05 第 3 步仍適用）。
        pub fn stage1b_python_daemon() -> String {
            read("stage1b_python_daemon.json")
        }
    }

    /// P4 `TestEnv` 替身：在暫存目錄寫入 manifest，依旗標決定 launcher 與同目錄 sibling 是否存在；
    /// `Drop` 時清除暫存目錄。P4 合入後以真實 TestEnv（sw-pal fakes）取代。
    struct TestEnv {
        root: PathBuf,
        manifest: String,
        launcher: bool,
        sibling: bool,
    }

    impl TestEnv {
        fn with_manifest(manifest: String) -> Self {
            let n = SEQ.fetch_add(1, Ordering::SeqCst);
            let root = std::env::temp_dir().join(format!("sw-cli-retired-{}-{n}", std::process::id()));
            Self { root, manifest, launcher: true, sibling: true }
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
                std::fs::write(exe_dir.join("serialwrapd"), b"stub").unwrap();
            }
            Ctx { manifest_path, exe_dir }
        }
    }

    impl Drop for TestEnv {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.root);
        }
    }

    #[test]
    fn retired_manifest_never_falls_back_to_python_runtime() {
        let env = TestEnv::with_manifest(fixtures::stage3_retired_but_runtime_injected())
            .without_launcher()
            .without_sibling();
        assert!(matches!(locate_daemon_argv(&env.ctx()), Err(DaemonLocateError::BinaryNotFound)));
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
