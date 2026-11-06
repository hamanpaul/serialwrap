//! P4-T19 Windows 安裝的 com0com 選用元件回歸與契約測試（SC-36，OD-15）。
//!
//! 替身與 `TestEnv` 由 P4-T1（`tests/support/mod.rs`）提供；本檔只使用計畫 Task 19 的 Produces 與 Task 11 的
//! `verify_bundle(ctx, dir, m)`。`component_digest` 為測試端的 fixture 規則，須與 P4-T19 mkbundle／verify 規則一致。
//! 規格來源：`docs/rust-refactor/plans/05-stage1b-rust-cli-mgmt.md` Task 19。
mod support;

use std::path::{Path, PathBuf};

use support::*;
use sw_cli::ctx::{CliCtx, TextIo};
use sw_cli::install_layout::verify_bundle;
use sw_cli::pairing::pairing_item;
use sw_cli::setup_cmd::OptionalStep;
use sw_pal::{native_client, BackendKind, ElevatedOutcome, PalErrorKind, PalPathFlavor};
use sw_proto::pyjson::PyValue;
use sw_proto::pypath::{HomeLookup, PathFlavor};
use sw_proto::release_manifest::{parse_manifest, ReleaseManifest};
use sw_proto::runtime_paths::{EnvSnapshot, NativeTraits, RuntimePaths};

/// 計畫 Task 19 的實驗性提示（逐字）。
const EXPERIMENTAL_LINE: &str = "serialwrap: com0com 選用元件為實驗性（Spike 1 未全數通過）";
/// 預設 pair（`CNCA0=COM20`、`CNCB0=COM21`）；`setup` 不另加 pair 參數。
const PAIR_ARG: &str = "CNCA0=COM20,CNCB0=COM21";
/// 提權 helper 的 bundle 相對路徑。
const HELPER_REL: &str = "bin/sw-com0com-helper.exe";

/// `optional/com0com/` 的 fixture 檔案（相對 bundle 根）；內容為測試替身，不是真的 com0com 套件。
fn component_files() -> Vec<(&'static str, Vec<u8>)> {
    vec![
        ("optional/com0com/setupc.exe", b"fixture-setupc".to_vec()),
        ("optional/com0com/com0com.inf", b"fixture-inf".to_vec()),
        ("optional/com0com/LICENSE-GPLv2.txt", b"fixture-license".to_vec()),
        ("optional/com0com/SOURCE.md", b"fixture-source-offer".to_vec()),
        ("optional/com0com/PROVENANCE.json", b"{}".to_vec()),
    ]
}

/// 元件 `sha256` 的 fixture 規則（測試定義）：依相對路徑排序，對每個檔案串接 `路徑 NUL 內容`，再取 sha256。
fn component_digest(files: &[(&str, Vec<u8>)]) -> String {
    let mut sorted: Vec<&(&str, Vec<u8>)> = files.iter().collect();
    sorted.sort_by(|a, b| a.0.cmp(b.0));
    let mut buf: Vec<u8> = Vec::new();
    for (rel, body) in sorted {
        buf.extend_from_slice(rel.as_bytes());
        buf.push(0);
        buf.extend_from_slice(body);
    }
    sw_proto::pycompat::sha256_hex(&buf)
}

/// 計畫 Task 19 Produces 的 `components.com0com` JSON 物件（`experimental` 預設 true）。
fn com0com_component_json(digest: &str) -> String {
    let helper_sha = sw_proto::digest::sha256_hex(b"fixture-helper");
    format!(
        r#"{{"optional":true,"default_selected":false,"requires_elevation":true,"experimental":true,"version":"3.0.0.0","path":"optional/com0com/","sha256":"{digest}","license":"GPL-2.0","license_file":"optional/com0com/LICENSE-GPLv2.txt","source_offer_file":"optional/com0com/SOURCE.md","provenance_file":"optional/com0com/PROVENANCE.json","helper":{{"path":"{HELPER_REL}","sha256":"{helper_sha}"}},"default_pair":["COM20","COM21"]}}"#
    )
}

/// Windows 測試 bundle 的 manifest（形狀同 `mkbundle.sh`；`component` 為 `None` 時不含 `components.com0com`）。
fn manifest_text(v: &str, cli_sha: &str, component: Option<String>) -> String {
    let mut components = format!(
        r#""cli":{{"implementation":"rust","version":"{v}","path":"bin/serialwrap","sha256":"{cli_sha}"}},"python_cli_delegate":{{"entry":["py/bin/python3","-I","delegate_sentinel.py"]}}"#
    );
    if let Some(c) = component {
        components.push_str(&format!(r#","com0com":{c}"#));
    }
    format!(
        r#"{{"schema_version":1,"bundle_version":"{v}","stage":"rust-1b-test","target":"x86_64-pc-windows-gnu","git_revision":"test","components":{{{components}}}}}"#
    )
}

/// 寫出 Windows 測試 bundle：`bin/serialwrap`、`bin/sw-com0com-helper.exe`、manifest；`with_component` 為真時附 `optional/com0com/`。
fn write_bundle(t: &TestEnv, v: &str, with_component: bool) {
    let root = t.bundle_dir();
    let mut files = vec![(HELPER_REL, b"fixture-helper".to_vec())];
    let mut components = Vec::new();
    if with_component {
        let component_files = component_files();
        let digest = component_digest(&component_files);
        components.push(("com0com", com0com_component_json(&digest)));
        files.extend(component_files);
    }
    write_complete_bundle(
        &root,
        v,
        "x86_64-pc-windows-gnu",
        true,
        &files,
        &components,
    );
}

/// 已啟動 `windows-task` 監管、附 process 替身的 Windows 測試環境，bundle 版本為 `v`。
fn windows_env(v: &str, with_component: bool) -> TestEnv {
    let t = TestEnv::windows()
        .with_process()
        .with_supervisor_state(sw_pal::traits::SupervisionMode::WindowsTask, true, true);
    write_bundle(&t, v, with_component);
    t
}

/// `RuntimePaths` 解析用的 home 查詢（測試環境不需要使用者家目錄）。
struct NoHome;

impl HomeLookup for NoHome {
    fn current_user_home(&self) -> Option<String> {
        None
    }
    fn user_home(&self, _user: &str) -> Option<String> {
        None
    }
}

/// 以 `TestEnv` 的平台與環境組出 `CliCtx`（與 `sw_cli::run` 的組裝一致），執行 `body`。
fn with_ctx<R>(t: &TestEnv, body: impl FnOnce(&mut CliCtx<'_>) -> R) -> R {
    let p = t.platform();
    let windows = p.caps.path_flavor == PalPathFlavor::Windows;
    let vars = p.env.vars();
    let env = if windows {
        EnvSnapshot::from_pairs_fold_upper(vars)
    } else {
        EnvSnapshot::from_pairs(vars)
    };
    let native = NativeTraits {
        default_endpoint_is_tcp: p.caps.native_backend == BackendKind::Win,
        path_flavor: if windows {
            PathFlavor::Windows
        } else {
            PathFlavor::Posix
        },
    };
    let paths = RuntimePaths::resolve(&env, native, &NoHome).expect("RuntimePaths 解析失敗");
    let mut out: Vec<u8> = Vec::new();
    let mut err: Vec<u8> = Vec::new();
    let mut ctx = CliCtx {
        p: &p,
        env,
        paths,
        io: TextIo::new(&mut out, &mut err, p.caps.text_newline),
        verbose: 0,
        cli_version: sw_proto::version::version(),
        autostart_lock: None,
    };
    body(&mut ctx)
}

/// `verify_bundle` 的結果轉為字串錯誤（避免綁定 `PairingDiff` 的欄位命名）。
fn verify_fmt(t: &TestEnv, dir: &Path, m: &ReleaseManifest) -> Result<(), String> {
    with_ctx(t, |ctx| verify_bundle(ctx, dir, m)).map_err(|diff| format!("{diff:?}"))
}

fn load_manifest(t: &TestEnv) -> ReleaseManifest {
    let text = std::fs::read_to_string(t.bundle_dir().join("release-manifest.json"))
        .expect("bundle manifest 應存在");
    parse_manifest(&text).expect("bundle manifest 應可解析")
}

fn svec(items: &[&str]) -> Vec<String> {
    items.iter().map(|item| (*item).to_string()).collect()
}

fn helper_exe(t: &TestEnv) -> PathBuf {
    let root = t.root();
    root.versions.join("0.9.1").join(HELPER_REL)
}

fn assert_no_optional_component(out: &str) {
    assert!(
        matches!(&out_json(out), PyValue::Dict(dict) if dict.get("optional_components").is_none()),
        "未安裝 com0com pair 時不應輸出 optional_components：{out}"
    );
}

/// 以 `PyValue::Dict` 取欄位（缺欄位即 panic 並印出原值）。
fn field<'a>(value: &'a PyValue, key: &str) -> &'a PyValue {
    match value {
        PyValue::Dict(d) => d
            .get(key)
            .unwrap_or_else(|| panic!("缺少欄位 {key}：{value:?}")),
        other => panic!("預期 JSON 物件，實得 {other:?}"),
    }
}

fn out_json(stdout: &str) -> PyValue {
    json(stdout.trim())
}

/// `optional_components.com0com` 子物件（不存在即 panic）。
fn com0com_result(stdout: &str) -> PyValue {
    let root = out_json(stdout);
    let optional = field(&root, "optional_components").clone();
    field(&optional, "com0com").clone()
}

/// 基本安裝必須成功（exit 0、`ok:true`）：選用元件的失敗不得拉低基本安裝結果。
fn assert_base_ok(rc: i32, out: &str, err: &str) {
    assert_eq!(rc, 0, "基本安裝結果不得因選用元件而失敗；stdout={out} stderr={err}");
    assert_eq!(field(&out_json(out), "ok"), &PyValue::Bool(true), "stdout={out}");
}

/// 基本安裝與 update 路徑都不得提權，且 `spawn_daemon` 的 argv 不得含 `runas`。
fn assert_no_elevation(t: &TestEnv) {
    let elevated = t.process().elevated_spawns();
    assert!(elevated.is_empty(), "基本安裝不得提權：{elevated:?}");
    let spawns = t.process().spawns();
    assert!(
        !spawns
            .iter()
            .any(|(argv, _, _)| argv.iter().any(|arg| arg.contains("runas"))),
        "一般行程 argv 不得含 runas：{spawns:?}"
    );
}

/// 恰一次提權，回傳 `(exe, args)`。
fn only_elevation(t: &TestEnv) -> (PathBuf, Vec<String>) {
    let mut all = t.process().elevated_spawns();
    assert_eq!(all.len(), 1, "應恰有一次提權：{all:?}");
    all.remove(0)
}

#[test]
fn manifest_com0com_component_optional() {
    let text = manifest_text(
        "0.9.1",
        &sha256_hex("bundle"),
        Some(com0com_component_json(&"0".repeat(64))),
    );
    let m = parse_manifest(&text).expect("含 components.com0com 的 manifest 應可解析");
    let c = m.com0com().expect("components.com0com 應存在");
    assert!(c.optional);
    assert!(!c.default_selected, "預設不勾選（OD-15）");
    assert!(c.requires_elevation);
    assert!(c.experimental, "Spike 1 剩餘項通過前必須為實驗性");
    assert_eq!(c.license, "GPL-2.0");
    assert_eq!(c.version, "3.0.0.0");
}

#[test]
fn malformed_com0com_helper_fields_rejected() {
    let helper_sha = sha256_hex("fixture-helper");
    let component = com0com_component_json(&"0".repeat(64));
    let invalid_sha = component.replace(
        &format!("\"sha256\":\"{helper_sha}\"}}"),
        "\"sha256\":\"bad\"}}",
    );
    assert!(
        parse_manifest(&manifest_text("0.9.1", &sha256_hex("bundle"), Some(invalid_sha))).is_err(),
        "無效 helper digest 必須拒絕"
    );
    let invalid_path = component.replace(HELPER_REL, "bin/../sw-com0com-helper.exe");
    assert!(
        parse_manifest(&manifest_text("0.9.1", &sha256_hex("bundle"), Some(invalid_path))).is_err(),
        "不安全 helper 路徑必須拒絕"
    );
}

#[test]
fn bundle_without_component_verifies() {
    let t = windows_env("0.9.1", false);
    let m = load_manifest(&t);
    assert!(
        m.com0com().is_none(),
        "未打包元件的 bundle 不得出現 components.com0com"
    );
    assert_eq!(
        verify_fmt(&t, &t.bundle_dir(), &m),
        Ok(()),
        "元件缺席仍為有效 bundle（SC-36）"
    );
}

#[test]
fn bundle_component_sha_mismatch_rejected() {
    let t = windows_env("0.9.1", true);
    let dir = t.bundle_dir();
    let m = load_manifest(&t);
    assert_eq!(
        verify_fmt(&t, &dir, &m),
        Ok(()),
        "未竄改的元件 bundle 必須通過驗證"
    );
    t.write_file(
        &dir.join("optional").join("com0com").join("setupc.exe"),
        b"tampered",
    );
    let err = verify_fmt(&t, &dir, &m).expect_err("竄改 optional/com0com/ 任一檔後必須拒絕");
    assert!(
        err.contains("components.com0com.sha256"),
        "差異必須指出 com0com 元件的 sha256 不符：{err}"
    );
}

#[test]
fn bundle_helper_sha_mismatch_rejected() {
    let t = windows_env("0.9.1", true);
    let dir = t.bundle_dir();
    let m = load_manifest(&t);
    t.write_file(&dir.join(HELPER_REL), b"tampered-helper");
    let err = verify_fmt(&t, &dir, &m).expect_err("竄改 helper 後必須拒絕");
    assert!(
        err.contains("components.com0com.helper.sha256"),
        "差異必須指出 helper sha256：{err}"
    );
}

#[test]
fn version_pairing_checks_optional_component_and_helper_digests() {
    let t = windows_env("0.9.1", true);
    let bundle = t.bundle_dir();
    let manifest = load_manifest(&t);
    let names = |item: &sw_proto::pyjson::PyDict| match item.get("mismatches") {
        Some(PyValue::List(values)) => values
            .iter()
            .filter_map(|value| match value {
                PyValue::Str(value) => Some(value.clone()),
                _ => None,
            })
            .collect::<Vec<_>>(),
        other => panic!("version_pairing.mismatches 必須為字串陣列：{other:?}"),
    };
    let initial = pairing_item(&manifest, &bundle, "0.9.1", Some("0.9.1"));
    assert!(!names(&initial).iter().any(|name| name.starts_with("com0com")));

    t.write_file(&bundle.join(HELPER_REL), b"tampered-helper");
    let helper_changed = pairing_item(&manifest, &bundle, "0.9.1", Some("0.9.1"));
    assert!(names(&helper_changed).iter().any(|name| name == "com0com_helper_sha256"));

    t.write_file(&bundle.join(HELPER_REL), b"fixture-helper");
    t.write_file(
        &bundle.join("optional/com0com/setupc.exe"),
        b"tampered-setupc",
    );
    let component_changed = pairing_item(&manifest, &bundle, "0.9.1", Some("0.9.1"));
    assert!(names(&component_changed).iter().any(|name| name == "com0com_sha256"));
}

#[test]
fn base_install_never_elevates() {
    // 首次安裝。
    let first_install = windows_env("0.9.1", true);
    let (rc, out, err) = first_install.run_from_bundle(&["setup"]);
    assert_eq!(rc, 0, "首次安裝應成功；stdout={out} stderr={err}");
    assert_no_optional_component(&out);
    assert_no_elevation(&first_install);

    // update（既有 Current 0.9.0）。
    let update = windows_env("0.9.1", true).with_current("0.9.0", None);
    let (rc, out, err) = update.run_from_bundle(&["setup"]);
    assert_eq!(rc, 0, "update 應成功；stdout={out} stderr={err}");
    assert_no_elevation(&update);
}

#[test]
fn plain_setup_reports_existing_pair_without_elevation() {
    let t = windows_env("0.9.1", true);
    t.registry().set_com0com_pairs(&[("COM20", "COM21")]);
    let (rc, out, err) = t.run_from_bundle(&["setup"]);
    assert_eq!(rc, 0, "setup 應成功；stdout={out} stderr={err}");
    let component = com0com_result(&out);
    assert_eq!(field(&component, "installed"), &PyValue::Bool(true));
    assert_no_elevation(&t);
}

#[test]
fn update_reports_existing_pair_even_when_new_bundle_omits_component() {
    let t = windows_env("0.9.1", false).with_current("0.9.0", None);
    t.registry().set_com0com_pairs(&[("COM20", "COM21")]);
    let (rc, out, err) = t.run_from_bundle(&["setup"]);
    assert_eq!(rc, 0, "update 應成功；stdout={out} stderr={err}");
    assert_eq!(field(&com0com_result(&out), "installed"), &PyValue::Bool(true));
    assert_no_elevation(&t);
}

#[test]
fn with_com0com_elevates_exactly_helper() {
    let t = windows_env("0.9.1", true);
    let (rc, out, err) = t.run_from_bundle(&["setup", "--with-com0com"]);
    assert_base_ok(rc, &out, &err);
    let (exe, args) = only_elevation(&t);
    assert_eq!(exe, helper_exe(&t), "提權對象必須是 bundle 內的 helper");
    assert_eq!(args, svec(&["install", "--pair", PAIR_ARG]));
}

#[test]
fn uac_declined_keeps_base_ok() {
    let t = windows_env("0.9.1", true);
    t.process().elevation_result(ElevatedOutcome::Declined);
    let (rc, out, err) = t.run_from_bundle(&["setup", "--with-com0com"]);
    assert_base_ok(rc, &out, &err);
    let expected = json(
        r#"{"installed":false,"reason":"elevation_declined","helper_rc":null,"experimental":true}"#,
    );
    assert_eq!(com0com_result(&out), expected, "stdout={out}");
}

#[test]
fn helper_failure_reported() {
    let t = windows_env("0.9.1", true);
    t.process().elevation_result(ElevatedOutcome::Exited(3));
    let (rc, out, err) = t.run_from_bundle(&["setup", "--with-com0com"]);
    assert_base_ok(rc, &out, &err);
    let expected = json(
        r#"{"installed":false,"reason":"helper_failed","helper_rc":3,"experimental":true}"#,
    );
    assert_eq!(com0com_result(&out), expected, "stdout={out}");
}

#[test]
fn component_absent_with_flag() {
    let t = windows_env("0.9.1", false);
    let (rc, out, err) = t.run_from_bundle(&["setup", "--with-com0com"]);
    assert_base_ok(rc, &out, &err);
    let c = com0com_result(&out);
    assert_eq!(field(&c, "installed"), &PyValue::Bool(false), "stdout={out}");
    assert_eq!(
        field(&c, "reason"),
        &PyValue::Str("component_absent".to_string()),
        "stdout={out}"
    );
    assert!(
        t.process().elevated_spawns().is_empty(),
        "元件缺席時不得提權"
    );
}

#[test]
fn existing_pair_not_reinstalled() {
    let t = windows_env("0.9.1", true);
    t.registry().set_com0com_pairs(&[("COM20", "COM21")]);
    let (rc, out, err) = t.run_from_bundle(&["setup", "--with-com0com"]);
    assert_base_ok(rc, &out, &err);
    assert!(
        t.process().elevated_spawns().is_empty(),
        "既有同名 pair 時不得重複提權建立（冪等）"
    );
    let c = com0com_result(&out);
    assert_eq!(field(&c, "installed"), &PyValue::Bool(true), "stdout={out}");
    assert_eq!(field(&c, "reason"), &PyValue::Null, "stdout={out}");
}

#[test]
fn registry_probe_error_does_not_skip_elevated_install() {
    let t = windows_env("0.9.1", true);
    t.registry().fail();
    let (rc, out, err) = t.run_from_bundle(&["setup", "--with-com0com"]);
    assert_base_ok(rc, &out, &err);
    let (exe, args) = only_elevation(&t);
    assert_eq!(exe, helper_exe(&t));
    assert_eq!(args, svec(&["install", "--pair", PAIR_ARG]));
}

#[test]
fn tampered_optional_payload_blocks_install_and_remove_before_elevation() {
    for step in [
        OptionalStep::InstallCom0com {
            pair: ("COM20".into(), "COM21".into()),
        },
        OptionalStep::RemoveCom0com,
    ] {
        let t = windows_env("0.9.1", true);
        t.write_file(
            &t.bundle_dir().join("optional/com0com/setupc.exe"),
            b"tampered-setupc",
        );
        let result = with_ctx(&t, |ctx| {
            sw_cli::setup_cmd::run_optional_step(ctx, &t.bundle_dir(), step)
        });
        assert_eq!(field(&PyValue::Dict(result.clone()), "reason"), &PyValue::Str("helper_failed".into()));
        assert!(t.process().elevated_spawns().is_empty());
    }
}

#[test]
fn remove_com0com_plan() {
    let t = windows_env("0.9.1", true);
    let (setup_rc, setup_out, setup_err) = t.run_from_bundle(&["setup"]);
    assert_eq!(setup_rc, 0, "準備已安裝版本失敗：{setup_out} {setup_err}");
    let (rc, out, err) = t.run_from_bundle(&["setup", "--remove-com0com"]);
    assert_base_ok(rc, &out, &err);
    let (exe, args) = only_elevation(&t);
    assert_eq!(exe, helper_exe(&t));
    assert_eq!(args, svec(&["remove", "--pair", PAIR_ARG]));
}

#[test]
fn remove_uses_installed_bundle_ignoring_manifest_override() {
    let t = windows_env("0.9.1", true);
    let mut installed_files = vec![(HELPER_REL, b"fixture-helper".to_vec())];
    let component_files = component_files();
    let digest = component_digest(&component_files);
    installed_files.extend(component_files);
    let installed_component = [("com0com", com0com_component_json(&digest))];
    write_complete_bundle(
        &t.root().versions.join("0.9.1"),
        "0.9.1",
        "x86_64-pc-windows-gnu",
        true,
        &installed_files,
        &installed_component,
    );
    let t = t.with_current("0.9.1", None);
    let alternate = t.home().join("alternate/release-manifest.json");
    t.write_file(
        &alternate,
        manifest_text(
            "9.9.9",
            &sha256_hex("alternate-cli"),
            Some(com0com_component_json(&"0".repeat(64))),
        )
        .as_bytes(),
    );
    let t = t.with_var(
        "SERIALWRAP_RELEASE_MANIFEST",
        &alternate.display().to_string(),
    );
    let (rc, out, err) = t.run_from_bundle(&["setup", "--remove-com0com"]);
    assert_base_ok(rc, &out, &err);
    let (exe, _) = only_elevation(&t);
    assert_eq!(exe, helper_exe(&t), "不得採用環境覆寫中的 helper 路徑");
}

#[test]
fn rollback_rejects_com0com_flags() {
    let t = windows_env("0.9.1", true);
    let (rc, out, err) = t.run_from_bundle(&["setup", "--rollback", "--remove-com0com"]);
    assert_eq!(rc, 2, "互斥參數應 exit 2；stdout={out} stderr={err}");
    assert_eq!(field(&out_json(&out), "error_code"), &PyValue::Str("INVALID_ARGS".into()));
    assert!(t.process().elevated_spawns().is_empty());
}

#[test]
fn standalone_remove_failure_is_nonzero() {
    let t = windows_env("0.9.1", true);
    let (setup_rc, setup_out, setup_err) = t.run_from_bundle(&["setup"]);
    assert_eq!(setup_rc, 0, "準備已安裝版本失敗：{setup_out} {setup_err}");
    t.process().elevation_result(ElevatedOutcome::Declined);
    let (rc, out, err) = t.run_from_bundle(&["setup", "--remove-com0com"]);
    assert_eq!(rc, 2, "獨立移除遭拒應回報失敗；stdout={out} stderr={err}");
    assert_eq!(field(&out_json(&out), "ok"), &PyValue::Bool(false));
}

#[test]
fn rollback_does_not_touch_driver() {
    let t = windows_env("0.9.1", false).with_current("0.9.1", Some("0.9.0"));
    t.registry().set_com0com_pairs(&[("COM20", "COM21")]);
    let (rc, out, err) = t.run_from_bundle(&["setup", "--rollback"]);
    assert_base_ok(rc, &out, &err);
    assert!(
        t.process().elevated_spawns().is_empty(),
        "serialwrap 本體回滾不得呼叫 helper 或移除 driver"
    );
    let c = com0com_result(&out);
    assert_eq!(
        field(&c, "left_installed"),
        &PyValue::Bool(true),
        "既有 pair 仍在時必須回報 left_installed:true；stdout={out}"
    );
}

#[test]
fn experimental_stderr_line() {
    let t = windows_env("0.9.1", true);
    let (rc, out, err) = t.run_from_bundle(&["setup", "--with-com0com"]);
    assert_base_ok(rc, &out, &err);
    let hits = err.lines().filter(|line| *line == EXPERIMENTAL_LINE).count();
    assert_eq!(hits, 1, "stderr 應恰含一行實驗性提示；stderr={err}");
}

#[test]
fn spawn_elevated_unsupported_on_posix() {
    let p = native_client();
    if p.caps.path_flavor != PalPathFlavor::Posix {
        // Windows 原生 UAC 路徑需互動，由擁有者 host gate 覆蓋；本案只約束 POSIX 語意。
        return;
    }
    let err = p
        .process
        .spawn_elevated(Path::new("/nonexistent/sw-com0com-helper"), &[])
        .expect_err("POSIX 不支援提權啟動");
    assert_eq!(err.kind, PalErrorKind::Unsupported);
}
