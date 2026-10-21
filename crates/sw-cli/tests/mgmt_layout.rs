//! P4-T11 安裝佈局、release manifest 驗證與版本配對 gate（§9.5、§12.1、SC-25）的回歸測試。
//!
//! 替身與 `TestEnv` 由 P4-T1（`tests/support/mod.rs`）提供；本檔以 `CliCtx`（經 [`Rig`]）呼叫需要 `ctx` 的介面。
//! 規格來源：`docs/rust-refactor/plans/05-stage1b-rust-cli-mgmt.md` Task 11；盤點 `inventory/07-platform-mgmt.md`。

mod common;
mod support;

use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::{Duration, SystemTime};

use support::*;
use sw_cli::ctx::{CliCtx, TextIo};
use sw_cli::install_layout::*;
use sw_pal::fakes::{
    platform, posix_caps, real_fs, windows_caps, FakeClock, FakeProcScan, FakeRpcConnector,
    FakeSysEnv,
};
use sw_pal::SysEnv;
use sw_proto::pyjson::{PyDict, PyValue};
use sw_proto::pypath::{HomeLookup, PathFlavor};
use sw_proto::release_manifest::{parse_current_release, parse_manifest};
use sw_proto::runtime_paths::{EnvSnapshot, NativeTraits, RuntimePaths};

/// `CliCtx` 的擁有者（測試用）：平台、環境快照與路徑由此持有，[`Rig::ctx`] 以借用產生 `CliCtx`。
struct Rig {
    p: sw_pal::ClientPlatform,
    env: EnvSnapshot,
    paths: RuntimePaths,
    clock: Arc<FakeClock>,
    out: Vec<u8>,
    err: Vec<u8>,
}

/// `HomeLookup` 轉接（與 `sw_cli::run` 內的 `PlatformHome` 同義）。
struct PlatformHome<'a>(&'a dyn SysEnv);

impl HomeLookup for PlatformHome<'_> {
    fn current_user_home(&self) -> Option<String> {
        self.0.current_user_home()
    }
    fn user_home(&self, user: &str) -> Option<String> {
        self.0.user_home(user)
    }
}

impl Rig {
    /// 與 `sw_cli::run` 相同的環境快照與路徑解析（Windows 大小寫不敏感）。
    fn new(p: sw_pal::ClientPlatform, clock: Arc<FakeClock>) -> Self {
        let (env, path_flavor) = match p.caps.path_flavor {
            sw_pal::PalPathFlavor::Windows => (
                EnvSnapshot::from_pairs_fold_upper(p.env.vars()),
                PathFlavor::Windows,
            ),
            sw_pal::PalPathFlavor::Posix => {
                (EnvSnapshot::from_pairs(p.env.vars()), PathFlavor::Posix)
            }
        };
        let native = NativeTraits {
            default_endpoint_is_tcp: p.caps.native_backend == sw_pal::BackendKind::Win,
            path_flavor,
        };
        let home = PlatformHome(p.env.as_ref());
        let paths = RuntimePaths::resolve(&env, native, &home).expect("RuntimePaths 解析失敗");
        Rig {
            p,
            env,
            paths,
            clock,
            out: Vec::new(),
            err: Vec::new(),
        }
    }

    /// `TestEnv` 的平台（隔離 tmp、FaultFs 等替身皆沿用）。
    fn from_env(t: &TestEnv) -> Self {
        Rig::new(t.platform(), Arc::clone(t.clock()))
    }

    fn ctx(&mut self) -> CliCtx<'_> {
        CliCtx {
            p: &self.p,
            env: self.env.clone(),
            paths: self.paths.clone(),
            io: TextIo::new(&mut self.out, &mut self.err, self.p.caps.text_newline),
            verbose: 0,
            cli_version: sw_proto::version::version(),
            autostart_lock: None,
        }
    }
}

/// 不經 `TestEnv` 的自訂環境：安裝根解析與 exe 分類需要與 `TestEnv` 預設不同的 `SERIALWRAP_*`、`current_exe`、`local_appdata`。
fn custom_rig(
    windows: bool,
    home: &Path,
    vars: &[(&str, &str)],
    exe: &Path,
    local_appdata: Option<&str>,
) -> Rig {
    let clock = FakeClock::new();
    let mut builder = FakeSysEnv::builder()
        .home(home)
        .current_exe(exe)
        .user_identity("tester")
        .var("HOME", &home.display().to_string());
    for (key, value) in vars {
        builder = builder.var(key, value);
    }
    if let Some(appdata) = local_appdata {
        builder = builder.local_appdata(Path::new(appdata));
    }
    let caps = if windows {
        windows_caps()
    } else {
        posix_caps()
    };
    let p = platform()
        .env(builder.build())
        .fs(real_fs())
        .clock(clock.clone())
        .rpc(FakeRpcConnector::new())
        .procs(FakeProcScan::new(Vec::new()))
        .caps(caps)
        .build();
    Rig::new(p, clock)
}

fn sha_bytes(body: &[u8]) -> String {
    sw_proto::pycompat::sha256_hex(body)
}

/// 安裝根樹快照（相對路徑、大小、mtime），供「失敗時不得改動」斷言。
fn tree_snapshot(dir: &Path) -> Vec<(String, u64, SystemTime)> {
    fn walk(dir: &Path, rel: &str, out: &mut Vec<(String, u64, SystemTime)>) {
        let mut entries: Vec<std::fs::DirEntry> = std::fs::read_dir(dir)
            .expect("目錄讀取失敗")
            .map(|e| e.expect("目錄項目讀取失敗"))
            .collect();
        entries.sort_by_key(|e| e.file_name());
        for entry in entries {
            let meta = entry.metadata().expect("metadata 讀取失敗");
            let name = format!("{rel}{}", entry.file_name().to_string_lossy());
            out.push((name.clone(), meta.len(), meta.modified().expect("mtime")));
            if meta.is_dir() {
                walk(&entry.path(), &format!("{name}/"), out);
            }
        }
    }
    let mut out = Vec::new();
    walk(dir, "", &mut out);
    out
}

/// `PyDict` 的字串欄位（斷言輔助）。
fn dict_str(d: &PyDict, key: &str) -> String {
    match d.get(key) {
        Some(PyValue::Str(s)) => s.clone(),
        other => panic!("欄位 {key} 不是字串：{other:?}"),
    }
}

/// 版本配對的全相符 digest 清單（`check_pairing` 的 `(component_path, actual_sha256)`）。
fn matching_digests(fx: &BundleFixture) -> Vec<(String, String)> {
    vec![
        ("bin/serialwrap".to_string(), fx.cli_sha.clone()),
        ("bin/serialwrapd".to_string(), fx.launcher_sha.clone()),
        ("py/bin/python3".to_string(), fx.runtime_sha.clone()),
        ("stable_entry:serialwrap".to_string(), fx.cli_sha.clone()),
        (
            "stable_entry:serialwrapd".to_string(),
            fx.launcher_sha.clone(),
        ),
    ]
}

#[test]
fn windows_copy_switch_keeps_old_and_writes_new() {
    let t = TestEnv::windows().with_install_root();
    let mut rig = Rig::from_env(&t);
    let ctx = rig.ctx();
    let root = resolve_install_root(&ctx);
    t.write_file(&root.bin.join("serialwrap.exe"), b"old-cli");
    let v = make_version_dir(
        &root.root,
        "0.4.0",
        &[
            ("bin/serialwrap.exe", &b"new-cli"[..]),
            ("bin/serialwrapd.exe", &b"new-d"[..]),
        ],
    );
    let rep = switch_entries(&ctx, &root, &v).unwrap();
    assert_eq!(
        std::fs::read(root.bin.join("serialwrap.exe")).unwrap(),
        b"new-cli"
    );
    let old = rep
        .old_files
        .iter()
        .find(|p| {
            p.file_name()
                .unwrap()
                .to_string_lossy()
                .starts_with("serialwrap.exe.old.")
        })
        .unwrap();
    assert_eq!(std::fs::read(old).unwrap(), b"old-cli");
    assert!(!root.bin.join("serialwrap.exe.new").exists());
}

#[test]
fn switch_failure_mid_way_restores_previous_entries() {
    let t = TestEnv::windows()
        .with_install_root()
        .with_fs_fault_replace_suffix("serialwrapd.exe.new");
    let mut rig = Rig::from_env(&t);
    let ctx = rig.ctx();
    let root = resolve_install_root(&ctx);
    t.write_file(&root.bin.join("serialwrap.exe"), b"old-cli");
    t.write_file(&root.bin.join("serialwrapd.exe"), b"old-d");
    let v = make_version_dir(
        &root.root,
        "0.4.0",
        &[
            ("bin/serialwrap.exe", &b"new-cli"[..]),
            ("bin/serialwrapd.exe", &b"new-d"[..]),
        ],
    );
    assert!(switch_entries(&ctx, &root, &v).is_err());
    assert_eq!(
        std::fs::read(root.bin.join("serialwrap.exe")).unwrap(),
        b"old-cli"
    );
    assert_eq!(
        std::fs::read(root.bin.join("serialwrapd.exe")).unwrap(),
        b"old-d"
    );
}

#[test]
fn switch_entries_validates_all_sources_before_touching_existing_entries() {
    let t = TestEnv::windows().with_install_root();
    let mut rig = Rig::from_env(&t);
    let ctx = rig.ctx();
    let root = resolve_install_root(&ctx);
    t.write_file(&root.bin.join("serialwrap.exe"), b"old-cli");
    t.write_file(&root.bin.join("serialwrapd.exe"), b"old-daemon");
    let version_dir = root.root.join("versions").join("0.4.0");
    t.write_file(&version_dir.join("bin/serialwrap.exe"), b"new-cli");

    assert!(switch_entries(&ctx, &root, &version_dir).is_err());
    assert_eq!(
        std::fs::read(root.bin.join("serialwrap.exe")).unwrap(),
        b"old-cli"
    );
    assert_eq!(
        std::fs::read(root.bin.join("serialwrapd.exe")).unwrap(),
        b"old-daemon"
    );
    assert!(!root.bin.join("serialwrap.exe.new").exists());
}

#[test]
fn switch_entries_rejects_non_regular_source_before_mutation() {
    let t = TestEnv::windows().with_install_root();
    let mut rig = Rig::from_env(&t);
    let ctx = rig.ctx();
    let root = resolve_install_root(&ctx);
    t.write_file(&root.bin.join("serialwrap.exe"), b"old-cli");
    t.write_file(&root.bin.join("serialwrapd.exe"), b"old-daemon");
    let version_dir = root.root.join("versions").join("0.4.0");
    t.write_file(&version_dir.join("bin/serialwrap.exe"), b"new-cli");
    std::fs::create_dir_all(version_dir.join("bin/serialwrapd.exe"))
        .expect("建立非一般檔案入口目錄");

    assert!(switch_entries(&ctx, &root, &version_dir).is_err());
    assert_eq!(
        std::fs::read(root.bin.join("serialwrap.exe")).unwrap(),
        b"old-cli"
    );
    assert_eq!(
        std::fs::read(root.bin.join("serialwrapd.exe")).unwrap(),
        b"old-daemon"
    );
    assert!(!root.bin.join("serialwrap.exe.new").exists());
}

#[test]
fn switch_entries_rejects_relative_version_dir_without_mutation() {
    let t = TestEnv::windows().with_install_root();
    let mut rig = Rig::from_env(&t);
    let ctx = rig.ctx();
    let root = resolve_install_root(&ctx);
    t.write_file(&root.bin.join("serialwrap.exe"), b"old-cli");
    t.write_file(&root.bin.join("serialwrapd.exe"), b"old-daemon");

    assert!(switch_entries(&ctx, &root, Path::new("versions/0.4.0")).is_err());
    assert_eq!(
        std::fs::read(root.bin.join("serialwrap.exe")).unwrap(),
        b"old-cli"
    );
    assert_eq!(
        std::fs::read(root.bin.join("serialwrapd.exe")).unwrap(),
        b"old-daemon"
    );
}

#[test]
fn cleanup_old_entries_removes_only_generated_entry_backups() {
    let t = TestEnv::windows().with_install_root();
    let mut rig = Rig::from_env(&t);
    let ctx = rig.ctx();
    let root = resolve_install_root(&ctx);
    for name in [
        "serialwrap.exe.old.20261020-123456",
        "serialwrapd.exe.old.20261020-123456-001",
    ] {
        t.write_file(&root.bin.join(name), b"generated backup");
    }
    for name in [
        "deploy.old.sh",
        "notes.old.txt",
        "serialwrap.exe.old.not-a-timestamp",
    ] {
        t.write_file(&root.bin.join(name), b"unrelated file");
    }
    let version_dir = make_version_dir(
        &root.root,
        "0.4.0",
        &[
            ("bin/serialwrap.exe", &b"new-cli"[..]),
            ("bin/serialwrapd.exe", &b"new-daemon"[..]),
        ],
    );

    switch_entries(&ctx, &root, &version_dir).expect("有效版本應切換");
    assert!(!root.bin.join("serialwrap.exe.old.20261020-123456").exists());
    assert!(!root
        .bin
        .join("serialwrapd.exe.old.20261020-123456-001")
        .exists());
    for name in [
        "deploy.old.sh",
        "notes.old.txt",
        "serialwrap.exe.old.not-a-timestamp",
    ] {
        assert!(root.bin.join(name).exists(), "不得刪除無關檔案 {name}");
    }
}

#[test]
fn switch_failure_reports_revert_failure() {
    let t = TestEnv::windows()
        .with_install_root()
        .with_fs_fault_replace_suffix("serialwrapd.exe.new")
        .with_fs_fault_replace_source_contains("serialwrap.exe.old.");
    let mut rig = Rig::from_env(&t);
    let ctx = rig.ctx();
    let root = resolve_install_root(&ctx);
    t.write_file(&root.bin.join("serialwrap.exe"), b"old-cli");
    t.write_file(&root.bin.join("serialwrapd.exe"), b"old-daemon");
    let version_dir = make_version_dir(
        &root.root,
        "0.4.0",
        &[
            ("bin/serialwrap.exe", &b"new-cli"[..]),
            ("bin/serialwrapd.exe", &b"new-daemon"[..]),
        ],
    );

    let err = switch_entries(&ctx, &root, &version_dir).expect_err("回復失敗必須回報");
    assert!(format!("{err:?}").contains("RevertFailed"), "err={err:?}");
}

#[test]
fn revert_entries_attempts_remaining_entries_after_one_failure() {
    let t = TestEnv::windows()
        .with_install_root()
        .with_fs_fault_replace_suffix("serialwrap.exe.old.missing");
    let mut rig = Rig::from_env(&t);
    let ctx = rig.ctx();
    let root = resolve_install_root(&ctx);
    t.write_file(&root.bin.join("serialwrap.exe"), b"new-cli");
    t.write_file(&root.bin.join("serialwrapd.exe"), b"new-daemon");
    let report = SwitchReport {
        replaced: vec![
            ("serialwrapd.exe".to_string(), None),
            ("serialwrap.exe".to_string(), None),
        ],
        old_files: vec![root.bin.join("serialwrap.exe.old.missing")],
    };

    let err = revert_entries(&ctx, &root, &report).expect_err("第一個入口回復失敗應回報");
    assert!(
        format!("{err:?}").contains("RevertEntriesFailed"),
        "err={err:?}"
    );
    assert!(
        !root.bin.join("serialwrapd.exe").exists(),
        "後續入口仍須嘗試回復"
    );
    assert_eq!(
        std::fs::read(root.bin.join("serialwrap.exe")).unwrap(),
        b"new-cli"
    );
}

#[test]
fn pairing_reports_daemon_version_mismatch() {
    let t = TestEnv::posix();
    let fx = write_bundle(&t.bundle_dir(), "0.4.0");
    let m = fx.parsed();
    let d = check_pairing(&m, "0.4.0", Some("0.3.2"), &[]);
    assert_eq!(d.len(), 1);
    assert_eq!(
        (
            d[0].field.as_str(),
            d[0].expected.as_str(),
            d[0].actual.as_str()
        ),
        ("daemon_version", "0.4.0", "0.3.2")
    );
}

#[test]
fn manifest_optional_metadata_rejects_non_strings() {
    let base = r#"{"schema_version":1,"bundle_version":"0.4.0","stage":"1A","target":"x86_64-unknown-linux-gnu","git_revision":"0123456789abcdef0123456789abcdef01234567","components":{"python_cli_delegate":{"entry":["py/bin/python3"]},"daemon":{"state_schema":1},"supervisor":{"task_xml_template_sha256":"0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"}}}"#;
    assert!(
        parse_manifest(base).is_err(),
        "daemon.state_schema 非字串時必須拒絕"
    );

    let bad_template = base.replace(
        "\"state_schema\":1",
        "\"state_schema\":\"python-state-v0\"",
    ).replace(
        "\"task_xml_template_sha256\":\"0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef\"",
        "\"task_xml_template_sha256\":true",
    );
    assert!(
        parse_manifest(&bad_template).is_err(),
        "supervisor.task_xml_template_sha256 非字串時必須拒絕"
    );

    let valid = base.replace("\"state_schema\":1", "\"state_schema\":\"python-state-v0\"");
    let bad_hash = valid.replace(
        "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
        "not-a-sha256",
    );
    assert!(
        parse_manifest(&bad_hash).is_err(),
        "task_xml_template_sha256 必須為 64 位小寫十六進位"
    );
    let manifest = parse_manifest(&valid).expect("optional metadata 為字串時應可解析");
    assert_eq!(manifest.state_schema(), Some("python-state-v0"));
    assert_eq!(
        manifest.task_xml_template_sha256(),
        Some("0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef")
    );
}

#[test]
fn task_xml_template_hash_is_golden_and_includes_working_directory_slot() {
    let template = sw_pal::service_ctl::TASK_XML_TEMPLATE;
    assert!(template.contains("{WORKING_DIRECTORY}"));
    assert_eq!(
        sha_bytes(template.as_bytes()),
        "d7a97770f2f0a3b41e51cdfaa95366391cf7f5e6ab523b0b2685542f24740965"
    );
}

#[test]
fn install_root_env_override() {
    let home = tempfile::tempdir().unwrap();
    let exe = home.path().join("bin").join("serialwrap");
    let mut rig = custom_rig(
        false,
        home.path(),
        &[("SERIALWRAP_INSTALL_ROOT", "/x")],
        &exe,
        None,
    );
    let ctx = rig.ctx();
    let root = resolve_install_root(&ctx);
    assert_eq!(root.root, PathBuf::from("/x"));
    assert_eq!(root.bin, PathBuf::from("/x/bin"));
}

#[test]
fn windows_install_root_override_preserves_absolute_host_path() {
    let home = tempfile::tempdir().unwrap();
    let expected = home.path().join("install");
    let expected_text = expected.display().to_string();
    let exe = home.path().join("serialwrap.exe");
    let mut rig = custom_rig(
        true,
        home.path(),
        &[("SERIALWRAP_INSTALL_ROOT", expected_text.as_str())],
        &exe,
        None,
    );
    let ctx = rig.ctx();
    let root = resolve_install_root(&ctx);
    assert_eq!(root.root, expected);
    assert_eq!(
        root.bin,
        PathBuf::from(format!("{}\\bin", expected.display()))
    );
    assert!(root.bin.is_absolute(), "假 Windows 下仍須保留 tmp 絕對路徑");
}

#[test]
fn install_root_windows_default() {
    let home = tempfile::tempdir().unwrap();
    let exe = home.path().join("serialwrap.exe");
    let mut rig = custom_rig(true, home.path(), &[], &exe, Some(r"C:\U\a\AppData\Local"));
    let ctx = rig.ctx();
    let root = resolve_install_root(&ctx);
    assert_eq!(
        root.root.to_string_lossy(),
        r"C:\U\a\AppData\Local\Programs\serialwrap"
    );
    assert_eq!(
        root.current_file.to_string_lossy(),
        r"C:\U\a\AppData\Local\Programs\serialwrap\bin\current-release.json"
    );
    assert_eq!(root.entry_style, EntryStyle::Copy);
    assert_eq!(
        root.entry_names,
        vec!["serialwrap.exe".to_string(), "serialwrapd.exe".to_string()]
    );
}

#[test]
fn install_root_posix_default_is_data_dir() {
    let home = tempfile::tempdir().unwrap();
    let exe = home.path().join("bin").join("serialwrap");
    let mut rig = custom_rig(false, home.path(), &[], &exe, None);
    let ctx = rig.ctx();
    let data_dir = ctx.paths.data_dir.clone();
    let root = resolve_install_root(&ctx);
    assert_eq!(root.root.to_string_lossy(), data_dir);
    assert_eq!(root.bin, home.path().join(".local").join("bin"));
    assert_eq!(root.entry_style, EntryStyle::Symlink);
    assert_eq!(
        root.entry_names,
        vec!["serialwrap".to_string(), "serialwrapd".to_string()]
    );
}

#[test]
fn posix_symlink_switch_atomic() {
    let t = TestEnv::posix();
    let mut rig = Rig::from_env(&t);
    let ctx = rig.ctx();
    let root = resolve_install_root(&ctx);
    assert_eq!(root.entry_style, EntryStyle::Symlink);
    t.write_file(&root.bin.join("serialwrap"), b"old-cli");
    let v = make_version_dir(
        &root.root,
        "0.4.0",
        &[
            ("bin/serialwrap", &b"new-cli"[..]),
            ("bin/serialwrapd", &b"new-d"[..]),
        ],
    );
    let rep = switch_entries(&ctx, &root, &v).unwrap();
    let link = root.bin.join("serialwrap");
    assert!(std::fs::symlink_metadata(&link)
        .unwrap()
        .file_type()
        .is_symlink());
    assert_eq!(
        std::fs::canonicalize(&link).unwrap(),
        std::fs::canonicalize(v.join("bin").join("serialwrap")).unwrap()
    );
    let old = rep
        .old_files
        .iter()
        .find(|p| {
            p.file_name()
                .unwrap()
                .to_string_lossy()
                .starts_with("serialwrap.old.")
        })
        .expect("原本的一般檔應保存為 .old.<ts>");
    assert_eq!(std::fs::read(old).unwrap(), b"old-cli");
}

#[test]
fn stage_version_idempotent() {
    let t = TestEnv::posix();
    let mut rig = Rig::from_env(&t);
    let ctx = rig.ctx();
    let root = resolve_install_root(&ctx);
    let bundle = t.bundle_dir();
    let fx = write_bundle(&bundle, "0.4.0");
    let m = fx.parsed();
    let first = stage_version(&ctx, &root, &bundle, &m).expect("首次 stage 應成功");
    let mtime_first = std::fs::metadata(first.join("bin").join("serialwrap"))
        .unwrap()
        .modified()
        .unwrap();
    // mtime 解析度不足時重複複製也會得到相同值，故先等待超過一般檔案系統的時間粒度。
    std::thread::sleep(Duration::from_millis(1100));
    let second = stage_version(&ctx, &root, &bundle, &m).expect("第二次 stage 應冪等");
    let mtime_second = std::fs::metadata(second.join("bin").join("serialwrap"))
        .unwrap()
        .modified()
        .unwrap();
    assert_eq!(first, second);
    assert_eq!(
        mtime_first, mtime_second,
        "相同 bundle 的第二次 stage 不得重複複製"
    );
}

#[test]
fn stage_version_existing_corrupt_refused() {
    let t = TestEnv::posix();
    let mut rig = Rig::from_env(&t);
    let ctx = rig.ctx();
    let root = resolve_install_root(&ctx);
    let bundle = t.bundle_dir();
    let fx = write_bundle(&bundle, "0.4.0");
    let m = fx.parsed();
    let first = stage_version(&ctx, &root, &bundle, &m).expect("首次 stage 應成功");
    t.write_file(&first.join("bin").join("serialwrap"), b"tampered");
    let err = stage_version(&ctx, &root, &bundle, &m).expect_err("損毀的既有版本目錄必須拒絕");
    assert!(
        matches!(err, LayoutError::ExistingCorrupt(_)),
        "err={err:?}"
    );
    assert_eq!(
        std::fs::read(first.join("bin").join("serialwrap")).unwrap(),
        b"tampered",
        "拒絕時不得覆寫既有版本目錄"
    );
}

#[test]
fn stage_version_rejects_non_semver_path_components() {
    let t = TestEnv::posix();
    let mut rig = Rig::from_env(&t);
    let ctx = rig.ctx();
    let root = resolve_install_root(&ctx);
    let bundle = t.bundle_dir();
    let fx = write_bundle(&bundle, "0.4.0");
    let mut manifest = fx.parsed();
    manifest.bundle_version = "..".to_string();

    let err = stage_version(&ctx, &root, &bundle, &manifest)
        .expect_err("bundle_version 不得成為路徑元件");
    assert!(matches!(err, LayoutError::Parse { .. }), "err={err:?}");
    assert!(!root.versions.exists(), "拒絕不合法版本時不得建立版本目錄");
}

#[test]
fn verify_bundle_detects_tree_tamper() {
    let t = TestEnv::posix();
    let mut rig = Rig::from_env(&t);
    let ctx = rig.ctx();
    let bundle = t.bundle_dir();
    let fx = write_bundle(&bundle, "0.4.0");
    let m = fx.parsed();
    verify_bundle(&ctx, &bundle, &m).expect("原始 bundle 必須通過驗證");
    let target = bundle.join("py").join("sw_core").join("cli.py");
    let mut body = std::fs::read(&target).unwrap();
    let last = body.len() - 1;
    body[last] ^= 0x01;
    std::fs::write(&target, &body).unwrap();
    let diffs = verify_bundle(&ctx, &bundle, &m)
        .expect_err("改動 py/sw_core/cli.py 一個 byte 後必須回報差異");
    assert!(
        diffs.iter().any(|d| d.field == "tree_sha256:daemon"),
        "diffs={diffs:?}"
    );
}

#[test]
fn verify_windows_bundle_fixture_uses_native_temp_paths() {
    let t = TestEnv::windows();
    let mut rig = Rig::from_env(&t);
    let ctx = rig.ctx();
    let bundle = t.bundle_dir();
    let fx = write_complete_bundle(&bundle, "0.4.0", "x86_64-pc-windows-gnu", true, &[], &[]);

    verify_bundle(&ctx, &bundle, &fx.parsed())
        .expect("假 Windows 平台也應以宿主暫存路徑驗證完整 bundle");
}

#[test]
fn verify_bundle_rejects_windows_drive_component_paths() {
    let t = TestEnv::windows();
    let mut rig = Rig::from_env(&t);
    let ctx = rig.ctx();
    let bundle = t.bundle_dir();
    let fx = write_complete_bundle(&bundle, "0.4.0", "x86_64-pc-windows-gnu", true, &[], &[]);
    for path in ["C:outside-cli", "C:/outside-cli"] {
        let manifest = fx.manifest.replace(
            "\"path\":\"bin/serialwrap.exe\"",
            &format!("\"path\":\"{path}\""),
        );
        t.write_file(&bundle.join("release-manifest.json"), manifest.as_bytes());
        let m = parse_manifest(&manifest).expect("危險路徑由 bundle verifier 拒絕");
        let diffs = verify_bundle(&ctx, &bundle, &m).expect_err("Windows 磁碟機路徑不得逃逸");
        assert!(
            diffs
                .iter()
                .any(|diff| diff.field == "sha256:bin/serialwrap"),
            "危險路徑須產生 CLI digest 差異：{diffs:?}"
        );
    }
}

#[test]
fn verify_bundle_checks_declared_and_undeclared_python_bytecode() {
    let t = TestEnv::posix();
    let mut rig = Rig::from_env(&t);
    let ctx = rig.ctx();
    let bundle = t.bundle_dir();
    let fx = write_bundle(&bundle, "0.4.0");
    let m = fx.parsed();
    verify_bundle(&ctx, &bundle, &m).expect("含已宣告 pyc 的 pristine bundle 應通過");

    let pyc = bundle.join("py/sw_core/__pycache__/cli.cpython-312.pyc");
    t.write_file(&pyc, b"tampered bytecode");
    let diffs = verify_bundle(&ctx, &bundle, &m).expect_err("已宣告 pyc 被竄改必須失敗");
    assert!(diffs
        .iter()
        .any(|d| d.field == "sha256:py/sw_core/__pycache__/cli.cpython-312.pyc"));

    t.write_file(&pyc, b"compiled cli\n");
    std::fs::remove_file(&pyc).expect("刪除測試 pyc 失敗");
    let diffs = verify_bundle(&ctx, &bundle, &m).expect_err("已宣告 pyc 遺失必須失敗");
    assert!(diffs
        .iter()
        .any(|d| d.field == "sha256:py/sw_core/__pycache__/cli.cpython-312.pyc"));

    t.write_file(
        &bundle.join("py/sw_core/__pycache__/undeclared.pyc"),
        b"extra",
    );
    let diffs = verify_bundle(&ctx, &bundle, &m).expect_err("未宣告 pyc 必須失敗");
    assert!(diffs
        .iter()
        .any(|d| d.field == "sha256:py/sw_core/__pycache__/undeclared.pyc"));
}

#[test]
fn verify_bundle_rejects_missing_digest_and_component_paths_that_escape() {
    let t = TestEnv::posix();
    let mut rig = Rig::from_env(&t);
    let ctx = rig.ctx();
    let bundle = t.bundle_dir();
    let fx = write_bundle(&bundle, "0.4.0");
    let cli_line = format!("{}  bin/serialwrap\n", fx.cli_sha);

    let mut incomplete = fx
        .manifest
        .replace(&format!(r#","sha256":"{}""#, fx.cli_sha), "");
    assert_ne!(incomplete, fx.manifest, "測試須移除 CLI digest 欄位");
    t.write_file(&bundle.join("release-manifest.json"), incomplete.as_bytes());
    let m = parse_manifest(&incomplete).expect("缺 digest 的結構性 fixture 應可供 verifier 拒絕");
    std::fs::remove_file(bundle.join("bin/serialwrap")).expect("刪除 CLI fixture 失敗");
    let sums_path = bundle.join("SHA256SUMS");
    let sums = std::fs::read_to_string(&sums_path).expect("讀取 SHA256SUMS 失敗");
    t.write_file(&sums_path, sums.replace(&cli_line, "").as_bytes());
    let diffs = verify_bundle(&ctx, &bundle, &m).expect_err("缺 digest 與檔案都不得形成假通過");
    assert!(diffs.iter().any(|d| d.field == "sha256:bin/serialwrap"));

    let outside = bundle.parent().unwrap().join("outside-cli");
    t.write_file(&outside, format!("cli-0.4.0\n").as_bytes());
    for escaped_path in ["../outside-cli".to_string(), outside.display().to_string()] {
        incomplete = fx.manifest.replace(
            "\"path\":\"bin/serialwrap\"",
            // JSON 字串跳脫（Windows 絕對路徑含 `\\`，直接內插會成為無效跳脫序列）。
            &format!("\"path\":\"{}\"", escaped_path.replace('\\', "\\\\")),
        );
        t.write_file(&bundle.join("release-manifest.json"), incomplete.as_bytes());
        let m = parse_manifest(&incomplete).expect("逃逸路徑 fixture 應可解析後由 verifier 拒絕");
        let diffs = verify_bundle(&ctx, &bundle, &m).expect_err("component path 不得離開 bundle");
        assert!(diffs.iter().any(|d| d.field == "sha256:bin/serialwrap"));
    }
}

#[test]
#[ignore = "pytest 以真實 mkbundle.sh --dev 輸出設定 SERIALWRAP_TEST_MKBUNDLE_DIR"]
fn real_mkbundle_dev_output_verifies() {
    let bundle = std::env::var_os("SERIALWRAP_TEST_MKBUNDLE_DIR")
        .map(PathBuf::from)
        .expect("pytest 必須提供真實 dev bundle 目錄");
    let manifest_text = std::fs::read_to_string(bundle.join("release-manifest.json"))
        .expect("讀取真實 release manifest 失敗");
    let manifest = parse_manifest(&manifest_text).expect("真實 release manifest 必須可解析");
    let t = TestEnv::posix();
    let mut rig = Rig::from_env(&t);
    let ctx = rig.ctx();
    verify_bundle(&ctx, &bundle, &manifest)
        .expect("mkbundle.sh --dev 產物必須通過 Rust verify_bundle");
}

#[test]
fn stage_version_refuses_missing_malformed_or_mismatched_existing_manifest() {
    for damage in ["missing", "malformed", "mismatched"] {
        let t = TestEnv::posix();
        let mut rig = Rig::from_env(&t);
        let ctx = rig.ctx();
        let root = resolve_install_root(&ctx);
        let bundle = t.bundle_dir();
        let fx = write_bundle(&bundle, "0.4.0");
        let m = fx.parsed();
        let installed = stage_version(&ctx, &root, &bundle, &m).expect("初次 stage 應成功");
        let manifest = installed.join("release-manifest.json");
        match damage {
            "missing" => std::fs::remove_file(&manifest).expect("移除 manifest 失敗"),
            "malformed" => t.write_file(&manifest, b"not json"),
            _ => {
                let altered = fx.manifest.replace(
                    "\"bundle_version\":\"0.4.0\"",
                    "\"bundle_version\":\"0.4.0\",\"x-fixture\":\"changed\"",
                );
                t.write_file(&manifest, altered.as_bytes());
            }
        }
        let err = stage_version(&ctx, &root, &bundle, &m)
            .expect_err("既有版本的 manifest 缺失、損壞或不同時必須拒絕");
        assert!(
            matches!(err, LayoutError::ExistingCorrupt(_)),
            "damage={damage} err={err:?}"
        );
    }
}

#[test]
fn stage_version_verification_error_caps_rendered_diffs() {
    let t = TestEnv::posix();
    let mut rig = Rig::from_env(&t);
    let ctx = rig.ctx();
    let root = resolve_install_root(&ctx);
    let bundle = t.bundle_dir();
    let fx = write_bundle(&bundle, "0.4.0");
    let m = fx.parsed();
    for n in 0..25 {
        t.write_file(&bundle.join(format!("extra-{n:02}.dat")), b"unlisted");
    }

    let err = stage_version(&ctx, &root, &bundle, &m).expect_err("未宣告檔案不得 stage");
    let rendered = err.to_string();
    assert!(rendered.contains("另有 5 項差異"), "rendered={rendered}");
}

#[test]
fn tree_sha256_matches_python_reference() {
    let t = TestEnv::posix();
    let mut rig = Rig::from_env(&t);
    let ctx = rig.ctx();
    let golden = common::data_root()
        .join("contracts")
        .join("golden")
        .join("tree_sha256.ndjson");
    let text = std::fs::read_to_string(&golden).expect("P1 golden 讀取失敗");
    let mut cases = 0usize;
    for (n, line) in text.lines().enumerate() {
        let PyValue::Dict(case) = json(line) else {
            panic!("golden #{n} 不是 JSON 物件");
        };
        let expected = match case.get("tree_sha256") {
            Some(PyValue::Str(s)) => s.clone(),
            other => panic!("golden #{n} 缺少 tree_sha256：{other:?}"),
        };
        let PyValue::Dict(files) = case.get("files").cloned().unwrap_or(PyValue::Null) else {
            panic!("golden #{n} 缺少 files");
        };
        let root = t.bundle_dir().join(format!("golden-{n}"));
        for (rel, body) in files.0.iter() {
            let PyValue::Str(body) = body else {
                panic!("golden #{n} 的 {rel} 不是字串");
            };
            t.write_file(&root.join(rel), body.as_bytes());
        }
        // 排除案例：`__pycache__` 目錄與 `*.pyc` 不得改變雜湊結果。
        t.write_file(
            &root.join("__pycache__").join("m.cpython-312.pyc"),
            b"\x00bytecode",
        );
        t.write_file(&root.join("z.pyc"), b"bytecode");
        let actual = tree_sha256(&ctx, &root).unwrap_or_else(|e| panic!("tree_sha256 失敗：{e}"));
        assert_eq!(actual, expected, "golden #{n} 與 P1 參考值不符");
        cases += 1;
    }
    assert!(cases >= 4, "golden 案例數不足：{cases}");
}

#[test]
fn pairing_item_matches_p1_shape() {
    let t = TestEnv::posix();
    let bundle = t.bundle_dir();
    let fx = write_bundle(&bundle, "0.4.0");
    let m = fx.parsed();
    let unreachable = pairing_item(&m, &bundle, "0.4.0", None);
    assert_eq!(
        PyValue::Dict(unreachable),
        json(
            r#"{"advisory":false,"check":"version_pairing","detail":"daemon_unreachable","mismatches":["daemon_unreachable"],"ok":false}"#
        )
    );
    let all_ok = pairing_item(&m, &bundle, "0.4.0", Some("0.4.0"));
    assert_eq!(
        PyValue::Dict(all_ok),
        json(
            r#"{"advisory":false,"check":"version_pairing","detail":"ok","mismatches":[],"ok":true}"#
        )
    );
    // 只改 CLI 位元組：mismatches 恰為 cli_sha256（P1 判定順序的第一個檔案項）。
    t.write_file(&bundle.join("bin").join("serialwrap"), b"tampered-cli");
    let tampered = pairing_item(&m, &bundle, "0.4.0", Some("0.4.0"));
    assert_eq!(
        PyValue::Dict(tampered),
        json(
            r#"{"advisory":false,"check":"version_pairing","detail":"cli_sha256","mismatches":["cli_sha256"],"ok":false}"#
        )
    );
}

#[test]
fn pairing_item_rejects_non_exact_rule() {
    let t = TestEnv::posix();
    let bundle = t.bundle_dir();
    let fx = write_bundle(&bundle, "0.4.0");
    let mut m = fx.parsed();
    let mut pairing = PyDict::new();
    pairing.insert("rule", PyValue::Str("loose".to_string()));
    pairing.insert("cli_version", PyValue::Str("0.4.0".to_string()));
    pairing.insert("daemon_version", PyValue::Str("0.4.0".to_string()));
    m.raw.insert("pairing", PyValue::Dict(pairing));

    let item = pairing_item(&m, &bundle, "0.4.0", Some("0.4.0"));
    assert_eq!(item.get("ok"), Some(&PyValue::Bool(false)));
    assert_eq!(
        item.get("mismatches"),
        Some(&PyValue::List(vec![PyValue::Str(
            "pairing_rule".to_string()
        )]))
    );
}

#[test]
fn install_lock_second_updater_times_out_without_changes() {
    let t = TestEnv::posix();
    let mut rig = Rig::from_env(&t);
    let clock = Arc::clone(&rig.clock);
    let ctx = rig.ctx();
    let root = resolve_install_root(&ctx);
    let lock_path = root.root.join(".install.lock");
    // 第一個 updater：另一個 open file description 持有獨占鎖（與另一個行程等價的 flock 語意）。
    let _holder = real_fs()
        .file_lock_exclusive(&lock_path, false)
        .expect("第一個 updater 應取得鎖");
    let before = tree_snapshot(&root.root);
    let slept_before: f64 = clock.sleeps_s().iter().sum();
    let err = match install_lock(&ctx, &root) {
        Ok(_) => panic!("鎖被持有時第二個 updater 不得取得鎖"),
        Err(d) => d,
    };
    assert_eq!(dict_str(&err, "error_code"), "TIMEOUT");
    assert_eq!(dict_str(&err, "phase"), "install_lock");
    assert_eq!(
        dict_str(&err, "message"),
        format!(
            "另一個 setup／更新／回滾正在進行（{}）",
            lock_path.display()
        )
    );
    let waited = clock.sleeps_s().iter().sum::<f64>() - slept_before;
    assert!(
        (5.0..=5.2).contains(&waited),
        "應於 5 s（FakeClock）後才回 TIMEOUT，實際等待 {waited} s"
    );
    assert_eq!(
        tree_snapshot(&root.root),
        before,
        "取鎖失敗時安裝根樹（含 mtime）不得改變"
    );
}

#[test]
fn install_lock_creates_missing_root_for_first_install() {
    let t = TestEnv::posix();
    let mut rig = Rig::from_env(&t);
    let ctx = rig.ctx();
    let root = resolve_install_root(&ctx);
    std::fs::remove_dir_all(&root.root).expect("移除 TestEnv 預建安裝根");
    assert!(!root.root.exists(), "首次安裝測試起點必須沒有安裝根");

    let _guard = install_lock(&ctx, &root).expect("首次安裝應先建立安裝根再取得鎖");
    assert!(root.root.is_dir());
    assert!(root.root.join(".install.lock").is_file());
}

#[test]
fn install_lock_error_uses_clean_pal_message() {
    let t = TestEnv::posix();
    let mut rig = Rig::from_env(&t);
    let ctx = rig.ctx();
    let root = resolve_install_root(&ctx);
    std::fs::remove_dir_all(&root.root).expect("移除測試安裝根");
    std::fs::write(&root.root, b"not a directory").expect("建立阻擋目錄的測試檔");

    let error = install_lock(&ctx, &root).expect_err("安裝根為一般檔案時應回錯");
    let message = dict_str(&error, "message");
    assert!(
        !message.contains("PalError {"),
        "不得把 PAL Debug 輸出給使用者：{message}"
    );
    assert!(message.contains("安裝根"), "應提供可讀錯誤：{message}");
}

#[test]
fn verify_bundle_errors_do_not_include_pal_debug_structs() {
    let t = TestEnv::posix();
    write_bundle(&t.bundle_dir(), "0.4.0");
    std::fs::remove_dir_all(t.bundle_dir().join("py/sw_core"))
        .expect("移除 package 樹以注入讀取錯誤");
    let m = parse_manifest(
        &std::fs::read_to_string(t.bundle_dir().join("release-manifest.json")).unwrap(),
    )
    .unwrap();
    let mut rig = Rig::from_env(&t);
    let ctx = rig.ctx();

    let diffs = verify_bundle(&ctx, &t.bundle_dir(), &m).expect_err("缺 package 時應失敗");
    assert!(!diffs.is_empty());
    for diff in diffs {
        assert!(
            !diff.expected.contains("PalError {") && !diff.actual.contains("PalError {"),
            "不得洩漏 PAL Debug 輸出：{diff:?}"
        );
    }
}

#[test]
fn locate_manifest_env_override_first() {
    // 存在的 SERIALWRAP_RELEASE_MANIFEST 優先。
    let t = TestEnv::posix();
    write_bundle(&t.bundle_dir(), "0.4.0");
    let alt_dir = t.home().join("alt");
    write_bundle(&alt_dir, "0.9.9");
    let alt_manifest = alt_dir.join("release-manifest.json");
    let t = t.with_var(
        "SERIALWRAP_RELEASE_MANIFEST",
        &alt_manifest.display().to_string(),
    );
    let mut rig = Rig::from_env(&t);
    let ctx = rig.ctx();
    let (path, m) = locate_manifest(&ctx).expect("存在的 override 應優先被採用");
    assert_eq!(path, alt_manifest);
    assert_eq!(m.bundle_version, "0.9.9");

    // 不存在的 override 退回 realpath(current_exe) 上兩層（bin/ 的父目錄）的 release-manifest.json。
    let t2 = TestEnv::posix();
    write_bundle(&t2.bundle_dir(), "0.4.0");
    let missing = t2.home().join("missing-release-manifest.json");
    let t2 = t2.with_var(
        "SERIALWRAP_RELEASE_MANIFEST",
        &missing.display().to_string(),
    );
    let mut rig2 = Rig::from_env(&t2);
    let ctx2 = rig2.ctx();
    let (path2, m2) = locate_manifest(&ctx2).expect("不存在的 override 應退回 realpath 規則");
    assert_eq!(path2, t2.bundle_dir().join("release-manifest.json"));
    assert_eq!(m2.bundle_version, "0.4.0");
}

#[test]
fn current_release_roundtrip_and_unknown_keys() {
    let t = TestEnv::posix();
    let mut rig = Rig::from_env(&t);
    let ctx = rig.ctx();
    let root = resolve_install_root(&ctx);
    let cur = CurrentRelease {
        bundle_version: "0.4.0".to_string(),
        dir: "versions/0.4.0".to_string(),
        previous: Some(ReleaseRef {
            bundle_version: "0.3.2".to_string(),
            dir: "versions/0.3.2".to_string(),
        }),
        backup_dir: Some("backups/20261019-000000".to_string()),
        switched_at: "2026-10-19T00:00:00Z".to_string(),
    };
    write_current(&ctx, &root, &cur).expect("寫入 current-release.json 應成功");
    assert_eq!(read_current(&root).unwrap(), Some(cur));

    // 含未知鍵 `x` 的檔案仍可讀；P3 的讀取端（parse_current_release）同樣忽略未知鍵。
    t.write_file(
        &root.current_file,
        br#"{"bundle_version":"0.4.0","dir":"versions/0.4.0","previous":null,"backup_dir":null,"switched_at":"2026-10-19T00:00:00Z","x":1}"#,
    );
    let back = read_current(&root)
        .unwrap()
        .expect("current-release.json 存在時應回傳 Some");
    assert_eq!(back.bundle_version, "0.4.0");
    let text = std::fs::read_to_string(&root.current_file).unwrap();
    let p3 = parse_current_release(&text).expect("P3 讀取端必須忽略未知鍵");
    assert_eq!(p3.dir, "versions/0.4.0");
}

#[test]
fn classify_exe_three_cases() {
    let home = tempfile::tempdir().unwrap();
    let install = home.path().join("install");
    let install_str = install.display().to_string();
    let vars = [("SERIALWRAP_INSTALL_ROOT", install_str.as_str())];

    let exe_installed = install
        .join("versions")
        .join("0.4.0")
        .join("bin")
        .join("serialwrap");
    let mut rig = custom_rig(false, home.path(), &vars, &exe_installed, None);
    let ctx = rig.ctx();
    let root = resolve_install_root(&ctx);
    assert_eq!(
        classify_exe(&ctx, &root),
        ExeLocation::InstalledVersion("0.4.0".to_string())
    );

    let exe_stable = install.join("bin").join("serialwrap");
    let mut rig = custom_rig(false, home.path(), &vars, &exe_stable, None);
    let ctx = rig.ctx();
    let root = resolve_install_root(&ctx);
    assert_eq!(classify_exe(&ctx, &root), ExeLocation::StableEntry);

    let exe_external = home.path().join("bundle").join("bin").join("serialwrap");
    let mut rig = custom_rig(false, home.path(), &vars, &exe_external, None);
    let ctx = rig.ctx();
    let root = resolve_install_root(&ctx);
    assert!(
        matches!(classify_exe(&ctx, &root), ExeLocation::External(_)),
        "安裝根之外的 exe 應分類為 External"
    );
}

#[test]
fn pairing_exact_all_ok() {
    let t = TestEnv::posix();
    let fx = write_bundle(&t.bundle_dir(), "0.4.0");
    let m = fx.parsed();
    let d = check_pairing(&m, "0.4.0", Some("0.4.0"), &matching_digests(&fx));
    assert!(d.is_empty(), "全相符時不得有差異：{d:?}");
}

#[test]
fn pairing_rejects_component_paths_outside_bundle() {
    let t = TestEnv::posix();
    let fx = write_bundle(&t.bundle_dir(), "0.4.0");
    let manifest = fx
        .manifest
        .replace("\"path\":\"bin/serialwrap\"", "\"path\":\"../outside-cli\"");
    let m = parse_manifest(&manifest).expect("逃逸路徑由 pairing gate 拒絕，不由基本 parser 拒絕");
    let mut digests = matching_digests(&fx);
    digests[0].0 = "../outside-cli".to_string();

    let diffs = check_pairing(&m, "0.4.0", Some("0.4.0"), &digests);
    assert!(
        diffs
            .iter()
            .any(|diff| diff.field == "sha256:../outside-cli"
                && diff.expected == "bundle-relative path"),
        "component digest 不得由 bundle 外路徑滿足：{diffs:?}"
    );
}

#[test]
fn pairing_undeclared_digest_never_matches_missing_evidence() {
    let t = TestEnv::posix();
    let fx = write_bundle(&t.bundle_dir(), "0.4.0");
    let m = fx.parsed();
    let diffs = check_pairing(
        &m,
        "0.4.0",
        Some("0.4.0"),
        &[("not-declared".to_string(), "missing".to_string())],
    );
    assert!(
        diffs.iter().any(|diff| diff.field == "sha256:not-declared"),
        "未宣告 digest 不得因 actual 文字為 missing 而通過：{diffs:?}"
    );
}

#[test]
fn pairing_item_respects_symlink_or_copy_package_directories() {
    let t = TestEnv::posix();
    let bundle = t.bundle_dir();
    let fx = write_bundle(&bundle, "0.4.0");
    let outside = bundle.parent().unwrap().join("external-package");
    t.write_file(&outside.join("injected.py"), b"outside");
    let link = bundle.join("py/sw_core/external");
    let link_kind = t.with_cli_ctx(|ctx| {
        ctx.p
            .fs
            .symlink_or_copy_dir(&link, &outside)
            .expect("建立 package 目錄 symlink／複本失敗")
    });
    let m = fx.parsed();

    let item = pairing_item(&m, &bundle, "0.4.0", Some("0.4.0"));
    let expected_ok = link_kind == sw_pal::fs_ops::LinkKind::Symlink;
    assert_eq!(
        item.get("ok"),
        Some(&PyValue::Bool(expected_ok)),
        "symlink 目錄不納入樹雜湊；權限不足而複製的目錄則屬套件內容"
    );
}

#[test]
fn pairing_daemon_absent_skips_daemon_field() {
    let t = TestEnv::posix();
    let fx = write_bundle(&t.bundle_dir(), "0.4.0");
    let m = fx.parsed();
    let d = check_pairing(&m, "0.4.0", None, &matching_digests(&fx));
    assert!(
        d.iter().all(|x| x.field != "daemon_version"),
        "daemon_version=None 時不得出現 daemon_version 差異：{d:?}"
    );
    assert!(d.is_empty(), "其餘欄位全相符時應無差異：{d:?}");
}

#[test]
#[ignore = "BLOCKED-OWNER: 原生 Windows 執行中 exe 改名（擁有者 host；scripts/rust/win-test.sh --package sw-cli -- mgmt_layout）"]
fn windows_switch_running_exe_rename() {
    let t = TestEnv::windows().with_install_root();
    let mut rig = Rig::from_env(&t);
    let ctx = rig.ctx();
    let root = resolve_install_root(&ctx);
    // 以系統的 PING.EXE 複本分別充當「正在執行的舊 CLI」與「新版 CLI」。
    let system_root = std::env::var("SystemRoot").unwrap_or_else(|_| r"C:\Windows".to_string());
    let ping = PathBuf::from(system_root).join("System32").join("PING.EXE");
    let ping_bytes = std::fs::read(&ping).expect("PING.EXE 讀取失敗");
    std::fs::create_dir_all(&root.bin).unwrap();
    std::fs::write(root.bin.join("serialwrap.exe"), &ping_bytes).unwrap();
    let mut running = std::process::Command::new(root.bin.join("serialwrap.exe"))
        .args(["-n", "30", "127.0.0.1"])
        .stdout(std::process::Stdio::null())
        .spawn()
        .expect("啟動舊 CLI 失敗");
    let v = make_version_dir(
        &root.root,
        "0.4.0",
        &[
            ("bin/serialwrap.exe", &ping_bytes[..]),
            ("bin/serialwrapd.exe", &ping_bytes[..]),
        ],
    );
    let rep = switch_entries(&ctx, &root, &v).expect("執行中 exe 應可改名後切換");
    assert!(rep.old_files.iter().any(|p| p
        .file_name()
        .unwrap()
        .to_string_lossy()
        .starts_with("serialwrap.exe.old.")));
    assert!(root.bin.join("serialwrap.exe").exists());
    let _ = running.kill();
}
