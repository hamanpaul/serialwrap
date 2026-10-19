//! Stage 1A 委派目標定位與執行環境回歸測試（P3-T16）。

use std::collections::BTreeMap;
use std::ffi::OsString;
use std::path::PathBuf;
use std::sync::Arc;

use sw_cli::delegate::{argv_sha256, resolve_target, DelegateError, DelegateTarget};
use sw_pal::fakes::{platform as fake_platform, FakeEnv, MemFs, RecordingProcess};
use sw_pal::{ClientPlatform, FsOps, ProcessCtl, SysEnv};

struct Fixture {
    root: PathBuf,
    exe: PathBuf,
    env: Arc<FakeEnv>,
    fs: Arc<MemFs>,
    process: Arc<RecordingProcess>,
    platform: ClientPlatform,
}

impl Fixture {
    fn new(depth: Option<&str>) -> Self {
        let root = std::env::current_dir()
            .expect("工作目錄可讀取")
            .join("target")
            .join("delegate-test-bundle");
        let exe = root.join("bin").join("serialwrap");
        let env = Arc::new(FakeEnv {
            exe: exe.clone(),
            ..FakeEnv::default()
        });
        {
            let mut vars = env.vars.lock().unwrap_or_else(|error| error.into_inner());
            vars.insert("HOME".to_string(), root.join("home").display().to_string());
            if let Some(depth) = depth {
                vars.insert("SERIALWRAP_DELEGATE_DEPTH".to_string(), depth.to_string());
            }
        }
        let fs = Arc::new(MemFs::default());
        let process = Arc::new(RecordingProcess::default());
        let env_trait: Arc<dyn SysEnv> = env.clone();
        let fs_trait: Arc<dyn FsOps> = fs.clone();
        let process_trait: Arc<dyn ProcessCtl> = process.clone();
        let platform = fake_platform()
            .env(env_trait)
            .fs(fs_trait)
            .process(process_trait)
            .build();
        Self {
            root,
            exe,
            env,
            fs,
            process,
            platform,
        }
    }

    fn current_release_path(&self) -> PathBuf {
        self.exe
            .parent()
            .expect("executable has a parent directory")
            .join("current-release.json")
    }

    fn manifest_path(&self) -> PathBuf {
        self.root
            .join("versions")
            .join("0.3.2")
            .join("release-manifest.json")
    }

    fn entry_path(&self) -> PathBuf {
        self.root
            .join("versions")
            .join("0.3.2")
            .join("py")
            .join("bin")
            .join("python3")
    }

    fn install_current_release(&self) {
        self.write_file(
            self.current_release_path(),
            br#"{"bundle_version":"0.3.2","dir":"versions/0.3.2"}"#,
        );
    }

    fn install_manifest(&self) {
        self.write_file(self.manifest_path(), &manifest("py/bin/python3"));
    }

    fn write_file(&self, path: PathBuf, bytes: &[u8]) {
        self.fs
            .files
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .insert(path, Ok(bytes.to_vec()));
    }

    fn write_entry(&self) {
        self.write_file(self.entry_path(), b"");
    }
}

fn manifest(entry: &str) -> Vec<u8> {
    format!(
        r#"{{"schema_version":1,"bundle_version":"0.3.2","stage":"1A","target":"x86_64-unknown-linux-gnu","git_revision":"test","components":{{"python_cli_delegate":{{"entry":["{entry}","-I","-B","-m","sw_core.cli"]}}}}}}"#
    )
    .into_bytes()
}

fn argv_tail() -> Vec<OsString> {
    vec![OsString::from("doctor")]
}

fn unavailable(result: Result<DelegateTarget, DelegateError>) -> String {
    match result {
        Err(DelegateError::Unavailable(message)) => message,
        Err(DelegateError::Recursion) => panic!("預期委派定位錯誤，實際為 recursion"),
        Ok(_) => panic!("預期委派定位失敗，實際成功"),
    }
}

#[test]
fn current_release_resolves_manifest_without_parent_components() {
    let fixture = Fixture::new(None);
    fixture.install_current_release();
    fixture.install_manifest();
    fixture.write_entry();

    let target = resolve_target(&fixture.platform, &argv_tail()).unwrap();

    assert_eq!(target.manifest_path, fixture.manifest_path());
    assert!(!target
        .manifest_path
        .components()
        .any(|component| component == std::path::Component::ParentDir));
    assert_eq!(target.argv[0], fixture.entry_path().as_os_str());
    assert_eq!(
        target.argv[1..],
        ["-I", "-B", "-m", "sw_core.cli", "doctor"]
    );
}

#[test]
fn invalid_current_release_is_reported() {
    let fixture = Fixture::new(None);
    fixture.write_file(fixture.current_release_path(), b"{");

    let message = unavailable(resolve_target(&fixture.platform, &argv_tail()));

    assert!(message.starts_with("invalid release manifest: current-release.json:"));
}

#[test]
fn invalid_manifest_is_reported() {
    let fixture = Fixture::new(None);
    fixture.install_current_release();
    fixture.write_file(fixture.manifest_path(), b"{");

    let message = unavailable(resolve_target(&fixture.platform, &argv_tail()));

    assert!(message.starts_with("invalid release manifest:"));
}

#[test]
fn missing_delegate_entry_is_reported() {
    let fixture = Fixture::new(None);
    fixture.install_current_release();
    fixture.install_manifest();

    let message = unavailable(resolve_target(&fixture.platform, &argv_tail()));

    assert!(message.starts_with("delegate entry not found:"));
}

#[test]
fn directory_delegate_entry_is_reported() {
    let fixture = Fixture::new(None);
    fixture.install_current_release();
    fixture.install_manifest();
    fixture
        .fs
        .dirs
        .lock()
        .unwrap_or_else(|error| error.into_inner())
        .insert(fixture.entry_path());

    let message = unavailable(resolve_target(&fixture.platform, &argv_tail()));

    assert!(message.starts_with("delegate entry not found:"));
}

#[test]
fn run_delegate_increments_depth_and_executes_once_with_three_environment_values() {
    let fixture = Fixture::new(Some("1"));
    fixture.install_current_release();
    fixture.install_manifest();
    fixture.write_entry();
    let argv = vec![OsString::from("serialwrap"), OsString::from("doctor")];
    let expected_sha = argv_sha256(&argv_tail());
    let mut stdout = Vec::new();
    let mut stderr = Vec::new();

    let rc = sw_cli::run(argv, &fixture.platform, &mut stdout, &mut stderr);

    assert_eq!(rc, 1);
    let calls = fixture
        .process
        .calls
        .lock()
        .unwrap_or_else(|error| error.into_inner());
    assert_eq!(calls.len(), 1);
    let (child_argv, env_add) = &calls[0];
    assert_eq!(child_argv[0], fixture.entry_path().as_os_str());
    assert_eq!(
        &child_argv[1..],
        ["-I", "-B", "-m", "sw_core.cli", "doctor"]
    );
    let child_env: BTreeMap<_, _> = env_add.iter().cloned().collect();
    assert_eq!(child_env.len(), 3);
    assert_eq!(child_env["SERIALWRAP_DELEGATE_DEPTH"], "2");
    assert_eq!(child_env["SERIALWRAP_DELEGATE_ARGV_SHA"], expected_sha);
    assert_eq!(
        child_env["SERIALWRAP_RELEASE_MANIFEST"],
        fixture.manifest_path().display().to_string()
    );
    assert!(String::from_utf8(stdout)
        .unwrap()
        .contains("exec failed: recording process does not execute"));
    assert!(String::from_utf8(stderr)
        .unwrap()
        .contains("serialwrap: doctor failed: INTERNAL_ERROR:"));
    drop(calls);
    assert_eq!(
        fixture.env.var("SERIALWRAP_DELEGATE_DEPTH").as_deref(),
        Some("1")
    );
}
