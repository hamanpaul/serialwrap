//! P4-T11 安裝佈局、release manifest 驗證與版本配對 gate（§9.5、§12.1、SC-25）的失敗測試。
//!
//! 紅燈階段（TDD）：`sw_cli::install_layout`／`sw_cli::pairing` 的函式本體為 `unimplemented!("P4-T11: …")`，
//! 本檔案的案例應以 `not implemented: P4-T11` panic 或斷言不符失敗，不得因編譯錯誤或 fixture 錯誤失敗。
//! 替身與 `TestEnv` 由 P4-T1（`tests/support/mod.rs`）提供；本 Task 只將 `TestEnv::platform()` 由私有改為公開，
//! 讓本檔以 `CliCtx`（經 [`Rig`]）呼叫需要 `ctx` 的介面。
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
use sw_proto::release_manifest::{parse_current_release, parse_manifest, ReleaseManifest};
use sw_proto::runtime_paths::{EnvSnapshot, NativeTraits, RuntimePaths};

/// 測試用 bundle 的 git revision（符合 schema 的 40 位小寫 hex）。
const REV: &str = "0123456789abcdef0123456789abcdef01234567";

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

/// P1 golden 演算法的測試端參考實作（與 `sw_core.release_manifest.tree_sha256` 同一定義）：
/// 依 relpath 的位元組序排序，彙總 `"<relpath>\0<sha256>\n"` 後再取 sha256。
fn ref_tree(files: &[(String, Vec<u8>)]) -> String {
    let mut sorted: Vec<&(String, Vec<u8>)> = files.iter().collect();
    sorted.sort_by(|a, b| a.0.as_bytes().cmp(b.0.as_bytes()));
    let mut acc = String::new();
    for (rel, body) in sorted {
        acc.push_str(&format!("{rel}\0{}\n", sha_bytes(body)));
    }
    sha_bytes(acc.as_bytes())
}

/// 遞迴收集 `base` 下的一般檔（排除 `__pycache__` 目錄與 `*.pyc`），relpath 以 `/` 連接並加上 `prefix`。
fn collect_tree(base: &Path, prefix: &str) -> Vec<(String, Vec<u8>)> {
    fn walk(dir: &Path, rel: &str, out: &mut Vec<(String, Vec<u8>)>) {
        let mut entries: Vec<std::fs::DirEntry> = std::fs::read_dir(dir)
            .expect("目錄讀取失敗")
            .map(|e| e.expect("目錄項目讀取失敗"))
            .collect();
        entries.sort_by_key(|e| e.file_name());
        for entry in entries {
            let name = entry.file_name().to_string_lossy().into_owned();
            let path = entry.path();
            if path.is_dir() {
                if name == "__pycache__" {
                    continue;
                }
                walk(&path, &format!("{rel}{name}/"), out);
            } else if !name.ends_with(".pyc") {
                let body = std::fs::read(&path).expect("檔案讀取失敗");
                out.push((format!("{rel}{name}"), body));
            }
        }
    }
    let mut out = Vec::new();
    walk(base, prefix, &mut out);
    out
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

/// 寫入後與 `mkbundle.sh --dev` 同形狀的 bundle 指紋（檔案與 manifest 皆已落地）。
struct BundleFixture {
    manifest: String,
    cli_sha: String,
    launcher_sha: String,
    runtime_sha: String,
}

impl BundleFixture {
    fn parsed(&self) -> ReleaseManifest {
        parse_manifest(&self.manifest).unwrap_or_else(|e| panic!("manifest 解析失敗：{e:?}"))
    }
}

/// 在 `dir` 寫入 bundle（`bin/serialwrap`、`bin/serialwrapd`、`py/bin/python3`、`py/sw_core/…`）與
/// `release-manifest.json`，並回傳與之一致的 manifest。
///
/// 摘要慣例與 P1 `sw_core.release_manifest` 相同：daemon 套件樹的 relpath 以 `sw_core/` 開頭（相對於套件父目錄 `py/`）；
/// `profiles_sha256` 為 `py/sw_core/assets/profiles` 的一般樹雜湊（relpath 相對於該目錄）；
/// SKILL 與 supervisor 定義為單檔 sha256。
fn write_bundle(dir: &Path, v: &str) -> BundleFixture {
    let files: Vec<(&str, Vec<u8>)> = vec![
        ("bin/serialwrap", format!("cli-{v}\n").into_bytes()),
        ("bin/serialwrapd", format!("launcher-{v}\n").into_bytes()),
        ("py/bin/python3", format!("runtime-{v}\n").into_bytes()),
        ("py/sw_core/__init__.py", b"VERSION = 1\n".to_vec()),
        ("py/sw_core/cli.py", format!("# cli {v}\n").into_bytes()),
        ("py/sw_core/systemd_units.py", b"UNITS = []\n".to_vec()),
        ("py/sw_core/assets/skill/SKILL.md", b"skill\n".to_vec()),
        (
            "py/sw_core/assets/skill/SKILL_WINDOWS.md",
            b"skill windows\n".to_vec(),
        ),
        (
            "py/sw_core/assets/profiles/default.yaml",
            b"profile: default\n".to_vec(),
        ),
    ];
    for (rel, body) in &files {
        let path = dir.join(rel);
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent).expect("bundle 目錄建立失敗");
        }
        std::fs::write(&path, body).expect("bundle 檔案寫入失敗");
    }
    let body_of =
        |rel: &str| -> Vec<u8> { std::fs::read(dir.join(rel)).expect("bundle 讀取失敗") };
    let cli_sha = sha_bytes(&body_of("bin/serialwrap"));
    let launcher_sha = sha_bytes(&body_of("bin/serialwrapd"));
    let runtime_sha = sha_bytes(&body_of("py/bin/python3"));
    let pkg_tree = ref_tree(&collect_tree(&dir.join("py").join("sw_core"), "sw_core/"));
    let profiles_tree = ref_tree(&collect_tree(
        &dir.join("py")
            .join("sw_core")
            .join("assets")
            .join("profiles"),
        "",
    ));
    let skill_sha = sha_bytes(&body_of("py/sw_core/assets/skill/SKILL.md"));
    let skill_win_sha = sha_bytes(&body_of("py/sw_core/assets/skill/SKILL_WINDOWS.md"));
    let supervisor_sha = sha_bytes(&body_of("py/sw_core/systemd_units.py"));
    let manifest = format!(
        r#"{{"schema_version":1,"bundle_version":"{v}","stage":"1A","created_at":"2026-10-19T00:00:00Z","target":"x86_64-unknown-linux-gnu","git_revision":"{REV}","contract_revision":"{REV}","components":{{"cli":{{"implementation":"rust","version":"{v}","git_revision":"{REV}","path":"bin/serialwrap","sha256":"{cli_sha}"}},"daemon":{{"implementation":"python","version":"{v}","git_revision":"{REV}","launcher":{{"path":"bin/serialwrapd","sha256":"{launcher_sha}"}},"entry":["py/bin/python3","-I","-B","-m","sw_core.cli"],"runtime":{{"path":"py/bin/python3","python_version":"3.12.3","sha256":"{runtime_sha}"}},"package":{{"name":"serialwrap","version":"{v}","wheel_sha256":null,"tree_sha256":"{pkg_tree}"}}}},"python_cli_delegate":{{"entry":["py/bin/python3","-I","-B","-m","sw_core.cli"],"tree_sha256":"{pkg_tree}"}},"assets":{{"skill_sha256":"{skill_sha}","skill_windows_sha256":"{skill_win_sha}","profiles_sha256":"{profiles_tree}"}},"supervisor":{{"default_mode":"on-demand","definition_sha256":"{supervisor_sha}"}}}},"pairing":{{"rule":"exact","cli_version":"{v}","daemon_version":"{v}"}}}}"#
    );
    std::fs::write(dir.join("release-manifest.json"), &manifest).expect("manifest 寫入失敗");
    BundleFixture {
        manifest,
        cli_sha,
        launcher_sha,
        runtime_sha,
    }
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
fn verify_bundle_detects_tree_tamper() {
    let t = TestEnv::posix();
    let mut rig = Rig::from_env(&t);
    let ctx = rig.ctx();
    let bundle = t.bundle_dir();
    let fx = write_bundle(&bundle, "0.4.0");
    let m = fx.parsed();
    if let Err(diffs) = verify_bundle(&ctx, &bundle, &m) {
        assert!(
            diffs.iter().all(|d| d.field != "tree_sha256:daemon"),
            "未改動前 daemon 樹不得報差異：{diffs:?}"
        );
    }
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
