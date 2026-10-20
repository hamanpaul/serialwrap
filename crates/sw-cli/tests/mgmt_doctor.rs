//! P4-T13 `doctor` 全部檢查原生與 version_pairing（SC-25）失敗測試。
mod support;

use std::time::Duration;

use support::*;
use sw_cli::doctor_cmd::{PYSERIAL_CODE, PYYAML_CODE, PY_VERSION_CODE};
use sw_pal::fakes::FakeRegistry;
use sw_pal::proc_scan::{DaemonProc, ProcIdentity};
use sw_pal::PalErrorKind;
use sw_proto::pyjson::{PyDict, PyValue};

/// 固定修復提示（07 §7.3／Task 13 步驟 1：runtime 無法定位或無法執行）。
const FIX_REINSTALL: &str = "重新安裝發佈組合（serialwrap setup）";

/// 測試用 daemon runtime 絕對 argv 首項（與 doctor 執行時一致）。
fn runtime_path(t: &TestEnv) -> String {
    t.bundle_dir()
        .join("py")
        .join("bin")
        .join("python3")
        .to_string_lossy()
        .into_owned()
}

/// `TestEnv::posix()` 下 doctor 以 POSIX 旗標（Python `posixpath.join`）接上 `default.yaml`；
/// Linux 主機與 `Path::join` 相同，Windows 主機模擬 POSIX 時分隔符為 `/`（profile 目錄本身仍為主機路徑）。
fn posix_flavor_profile_path(t: &TestEnv) -> String {
    format!("{}/default.yaml", t.config_dir().join("profiles").display())
}

trait PyDictExt {
    fn contains_key(&self, k: &str) -> bool;
}

impl PyDictExt for PyDict {
    fn contains_key(&self, k: &str) -> bool {
        self.0.contains_key(k)
    }
}

trait AsDict {
    fn as_dict(&self) -> &PyDict;
}

impl AsDict for PyValue {
    fn as_dict(&self) -> &PyDict {
        match self {
            PyValue::Dict(d) => d,
            other => panic!("expected dict, got {other:?}"),
        }
    }
}

trait GetKey {
    fn get_key(&self, key: &str) -> Option<&PyValue>;
}

impl GetKey for PyValue {
    fn get_key(&self, key: &str) -> Option<&PyValue> {
        match self {
            PyValue::Dict(d) => d.get(key),
            _ => None,
        }
    }
}

impl GetKey for PyDict {
    fn get_key(&self, key: &str) -> Option<&PyValue> {
        self.get(key)
    }
}

impl<T: GetKey> GetKey for &T {
    fn get_key(&self, key: &str) -> Option<&PyValue> {
        (**self).get_key(key)
    }
}

fn list<'a>(val: &'a (impl GetKey + ?Sized), key: &str) -> &'a [PyValue] {
    match val.get_key(key) {
        Some(PyValue::List(l)) => l.as_slice(),
        other => panic!("expected list at key {key}, got {other:?}"),
    }
}

fn s<'a>(val: &'a (impl GetKey + ?Sized), key: &str) -> &'a str {
    match val.get_key(key) {
        Some(PyValue::Str(s)) => s.as_str(),
        other => panic!("expected str at key {key}, got {other:?}"),
    }
}

fn b(val: &(impl GetKey + ?Sized), key: &str) -> bool {
    match val.get_key(key) {
        Some(PyValue::Bool(b)) => *b,
        other => panic!("expected bool at key {key}, got {other:?}"),
    }
}

fn find_check<'a>(doc: &'a PyValue, name: &str) -> &'a PyDict {
    list(doc, "checks")
        .iter()
        .map(|c| c.as_dict())
        .find(|d| s(d, "check") == name)
        .unwrap_or_else(|| panic!("check {name} not found in output"))
}

#[test]
fn linux_check_order_and_advisory_stamp() {
    let t = TestEnv::posix().with_doctor_bundle("3.12.7");
    let (rc, out, _) = t.run(&["doctor"]);
    assert_eq!(rc, 0);
    let doc = json(&out);
    let names: Vec<String> = list(&doc, "checks")
        .iter()
        .map(|c| s(c.as_dict(), "check").to_string())
        .collect();
    assert_eq!(
        names,
        [
            "python",
            "pyyaml",
            "serialwrap_on_path",
            "serialwrapd_on_path",
            "other_serialwrap_installs",
            "dialout",
            "serialwrap_minicom_on_path",
            "jq_on_path",
            "minicom_on_path",
            "systemd",
            "supervision_mode",
            "single_daemon",
            "endpoint_reachable",
            "wal_dir",
            "wal_writable",
            "profile_bootloader_prompts",
            "devices",
            "wsl_systemd",
            "version_pairing"
        ]
    );
    for c in list(&doc, "checks") {
        let d = c.as_dict();
        let expect_adv = sw_cli::doctor_cmd::DOCTOR_ADVISORY_CHECKS.contains(&s(d, "check"));
        assert_eq!(b(d, "advisory"), expect_adv, "{}", s(d, "check"));
        if b(d, "ok") && d.contains_key("fix") {
            assert_eq!(s(d, "fix"), "");
        }
        if s(d, "check") == "version_pairing" {
            // 與 Python 基線（sw_core/doctor_cmd.py `_check_version_pairing`）相同：version_pairing 帶 fix（ok 時為 ""）。
            assert!(d.contains_key("fix") && d.contains_key("mismatches"));
        }
    }
}

#[test]
fn overall_ok_ignores_advisory_failures_only() {
    let t = TestEnv::posix()
        .with_doctor_bundle("3.12.7")
        .all_required_ok()
        .with_which_missing("jq");
    let doc1 = json(&t.run(&["doctor"]).1);
    assert!(b(&doc1, "ok"));
    let t2 = TestEnv::posix()
        .with_doctor_bundle("3.12.7")
        .all_required_ok()
        .without_group("dialout");
    let doc2 = json(&t2.run(&["doctor"]).1);
    assert!(!b(&doc2, "ok"));
}

#[test]
fn windows_check_order() {
    // devices（Windows）需要 SERIALCOMM 登錄檢視；未配置時替身會 panic（support 契約），此處給空登錄。
    let t = TestEnv::windows()
        .with_fake_registry(FakeRegistry::new(vec![], &[]))
        .with_doctor_bundle("3.12.7");
    let (rc, out, _) = t.run(&["doctor"]);
    assert_eq!(rc, 0);
    let doc = json(&out);
    let names: Vec<String> = list(&doc, "checks")
        .iter()
        .map(|c| s(c.as_dict(), "check").to_string())
        .collect();
    assert_eq!(
        names,
        [
            "python",
            "pyyaml",
            "pyserial",
            "serialwrap_on_path",
            "serialwrapd_on_path",
            "other_serialwrap_installs",
            "supervision_mode",
            "daemon_endpoint",
            "wal_dir",
            "wal_writable",
            "profile_bootloader_prompts",
            "devices",
            "version_pairing"
        ]
    );
    for c in list(&doc, "checks") {
        let d = c.as_dict();
        let expect_adv = sw_cli::doctor_cmd::DOCTOR_ADVISORY_CHECKS_WIN.contains(&s(d, "check"));
        assert_eq!(b(d, "advisory"), expect_adv, "{}", s(d, "check"));
        if b(d, "ok") && d.contains_key("fix") {
            assert_eq!(s(d, "fix"), "");
        }
        if s(d, "check") == "version_pairing" {
            // 與 Python 基線（sw_core/doctor_cmd.py `_check_version_pairing`）相同：version_pairing 帶 fix（ok 時為 ""）。
            assert!(d.contains_key("fix") && d.contains_key("mismatches"));
        }
    }
}

#[test]
fn other_installs_single() {
    let t = TestEnv::posix().with_which_all("serialwrap", &["/bin/serialwrap"]);
    let (rc, out, _) = t.run(&["doctor"]);
    assert_eq!(rc, 0);
    let doc = json(&out);
    let check = find_check(&doc, "other_serialwrap_installs");
    assert!(b(check, "ok"));
    assert_eq!(s(check, "detail"), "僅偵測到目前這份");
    assert!(!t
        .calls()
        .iter()
        .any(|cmd| cmd.contains(&"--version".to_string())));
}

#[test]
fn other_installs_multi_versions_differ() {
    let t = TestEnv::posix()
        .with_which_all("serialwrap", &["/a/serialwrap", "/b/serialwrap"])
        .with_command(&["/a/serialwrap", "--version"], 0, "serialwrap 0.3.2\n", "")
        .with_command(&["/b/serialwrap", "--version"], 0, "serialwrap 0.4.0\n", "");
    let (rc, out, _) = t.run(&["doctor"]);
    assert_eq!(rc, 0);
    let doc = json(&out);
    let check = find_check(&doc, "other_serialwrap_installs");
    assert!(!b(check, "ok"));
    assert_eq!(
        s(check, "detail"),
        "/a/serialwrap=serialwrap 0.3.2；/b/serialwrap=serialwrap 0.4.0"
    );
    // `--version` 探測為固定 2.0 s（不讀 --timeout／--retries）。
    let version_timeouts: Vec<_> = t
        .calls()
        .iter()
        .zip(t.sysenv_timeouts())
        .filter(|(argv, _)| argv.last().map(String::as_str) == Some("--version"))
        .map(|(_, timeout)| timeout)
        .collect();
    assert_eq!(version_timeouts.len(), 2);
    assert!(version_timeouts.iter().all(|timeout| *timeout == Some(Duration::from_secs(2))));
}

#[test]
fn single_daemon_multi_open() {
    let p1 = DaemonProc {
        pid: 1001,
        socket: None,
        tree: vec![ProcIdentity {
            pid: 1001,
            start_ticks: None,
        }],
    };
    let p2 = DaemonProc {
        pid: 1002,
        socket: None,
        tree: vec![ProcIdentity {
            pid: 1002,
            start_ticks: None,
        }],
    };
    let t = TestEnv::posix().with_procs(vec![p1, p2]);
    let (rc, out, _) = t.run(&["doctor"]);
    assert_eq!(rc, 0);
    let doc = json(&out);
    let check = find_check(&doc, "single_daemon");
    assert!(!b(check, "ok"));
    assert_eq!(s(check, "detail"), "2 個 serialwrapd 在跑（偵測到多開）");
}

#[test]
fn endpoint_reachable_no_daemons() {
    let t = TestEnv::posix().with_procs(vec![]);
    let (rc, out, _) = t.run(&["doctor"]);
    assert_eq!(rc, 0);
    let doc = json(&out);
    let check = find_check(&doc, "endpoint_reachable");
    assert!(b(check, "ok"));
    let ep = t.default_socket();
    assert_eq!(
        s(check, "detail"),
        format!("未偵測到執行中的 serialwrapd（on-demand 模式正常）；本 client 解析到 {ep}（來源：預設（SOCKET_PATH，受 SERIALWRAP_STATE_DIR/XDG 環境變數影響））")
    );
}

#[test]
fn endpoint_reachable_tcp_always_alive_posix() {
    let p1 = DaemonProc {
        pid: 1001,
        socket: Some("tcp://127.0.0.1:1".into()),
        tree: vec![ProcIdentity {
            pid: 1001,
            start_ticks: None,
        }],
    };
    let t = TestEnv::posix()
        // runtime config 的鍵為 socket_path（runtime_config.py `socket_path()`）；原 fixture 寫成 socket 而未被讀取。
        .with_config("socket_path: tcp://127.0.0.1:1\n")
        .with_procs(vec![p1]);
    let (rc, out, _) = t.run(&["doctor"]);
    assert_eq!(rc, 0);
    let doc = json(&out);
    let check = find_check(&doc, "endpoint_reachable");
    assert!(b(check, "ok"));
    assert!(s(check, "detail").contains("可連上執行中 daemon"));
}

#[test]
fn endpoint_reachable_unreachable() {
    let p1 = DaemonProc {
        pid: 1001,
        socket: Some("/run/other2.sock".into()),
        tree: vec![ProcIdentity {
            pid: 1001,
            start_ticks: None,
        }],
    };
    let p2 = DaemonProc {
        pid: 1002,
        socket: Some("/run/other1.sock".into()),
        tree: vec![ProcIdentity {
            pid: 1002,
            start_ticks: None,
        }],
    };
    let t = TestEnv::posix().with_procs(vec![p1, p2]);
    let (rc, out, _) = t.run(&["doctor"]);
    assert_eq!(rc, 0);
    let doc = json(&out);
    let check = find_check(&doc, "endpoint_reachable");
    assert!(!b(check, "ok"));
    assert!(s(check, "detail").contains("連不上；目前執行中 daemon 綁在 /run/other1.sock、/run/other2.sock"));
}

#[test]
fn wal_dir_mismatch() {
    let t = TestEnv::posix().with_var("SERIALWRAP_WAL_DIR", "/a");
    let ep = t.default_socket();
    let t = t.with_rpc_ok(
        &ep,
        "health.status",
        r#"{"ok":true,"wal_path":"/b/raw.wal.ndjson"}"#,
    );
    let (rc, out, _) = t.run(&["doctor"]);
    assert_eq!(rc, 0);
    let doc = json(&out);
    let check = find_check(&doc, "wal_dir");
    assert!(!b(check, "ok"));
    assert_eq!(
        s(check, "detail"),
        "daemon 實際生效 WAL_DIR=/b，與 shell SERIALWRAP_WAL_DIR=/a 不一致"
    );
}

#[test]
fn wal_writable_unhealthy_reasons() {
    let t = TestEnv::posix();
    let ep = t.default_socket();
    let payload = r#"{"ok":true,"wal":{"healthy":false,"wal_dir":"/wal/test","wal_dir_exists":true,"wal_dir_writable":false,"current_seq":5,"wal_file_exists":false,"write_failures":2,"last_write_error":"E"}}"#;
    let t = t.with_rpc_ok(&ep, "health.status", payload);
    let (rc, out, _) = t.run(&["doctor"]);
    assert_eq!(rc, 0);
    let doc = json(&out);
    let check = find_check(&doc, "wal_writable");
    assert!(!b(check, "ok"));
    assert_eq!(
        s(check, "detail"),
        "WAL 目錄 /wal/test 異常：目錄不可寫；已寫過 5 筆紀錄但現行 WAL 檔不存在；append 失敗 2 次（最後一次：E）"
    );
}

#[test]
fn wal_writable_recreated_suffix() {
    let t = TestEnv::posix();
    let ep = t.default_socket();
    let payload = r#"{"ok":true,"wal":{"healthy":true,"wal_dir":"/wal/test","current_seq":10,"recreated_count":2}}"#;
    let t = t.with_rpc_ok(&ep, "health.status", payload);
    let (rc, out, _) = t.run(&["doctor"]);
    assert_eq!(rc, 0);
    let doc = json(&out);
    let check = find_check(&doc, "wal_writable");
    assert!(b(check, "ok"));
    assert!(s(check, "detail").ends_with("；曾自癒重建 2 次（消失期間的紀錄無法復原）"));
}

#[test]
fn bootloader_prompts_drift() {
    let t = TestEnv::posix();
    let live_path = t.config_dir().join("profiles").join("default.yaml");
    t.write_file(
        &live_path,
        b"profiles:\n  brcm-template:\n    platform: broadcom\n",
    );
    let (rc, out, _) = t.run(&["doctor"]);
    assert_eq!(rc, 0);
    let doc = json(&out);
    let check = find_check(&doc, "profile_bootloader_prompts");
    assert!(!b(check, "ok"));
    assert_eq!(
        s(check, "detail"),
        format!(
            "線上 {} 的 brcm-template 缺 bootloader_prompts（出貨資產有）",
            posix_flavor_profile_path(&t)
        )
    );
}

#[test]
fn bootloader_prompts_not_materialized() {
    let t = TestEnv::posix();
    let (rc, out, _) = t.run(&["doctor"]);
    assert_eq!(rc, 0);
    let doc = json(&out);
    let check = find_check(&doc, "profile_bootloader_prompts");
    assert!(b(check, "ok"));
    assert_eq!(
        s(check, "detail"),
        format!("線上 profile 未物化（{}），略過", posix_flavor_profile_path(&t))
    );
}

#[test]
fn devices_linux_counts_dangling() {
    let t = TestEnv::posix();
    let by_id = t.by_id_dir();
    t.write_file(&by_id.join("usb-normal-device"), b"");
    // dangling symlink 以 PAL 測試替身建立（不寫平台 cfg）；平台不允許建立 symlink 時退為一般檔案。
    let dangling = by_id.join("usb-dangling-device");
    if sw_pal::fakes::real_fs()
        .symlink_file(&dangling, std::path::Path::new("/nonexistent/target"))
        .is_err()
    {
        t.write_file(&dangling, b"");
    }
    let (rc, out, _) = t.run(&["doctor"]);
    assert_eq!(rc, 0);
    let doc = json(&out);
    let check = find_check(&doc, "devices");
    assert!(b(check, "ok"));
    assert_eq!(s(check, "detail"), "2");
}

#[test]
fn devices_windows_excluded_suffix() {
    let reg = FakeRegistry::new(
        vec![
            ("Device0", "COM1"),
            ("Device1", "COM2"),
            ("Device2", "COM3"),
        ],
        &["COM1", "COM2"],
    );
    let t = TestEnv::windows().with_fake_registry(reg);
    let (rc, out, _) = t.run(&["doctor"]);
    assert_eq!(rc, 0);
    let doc = json(&out);
    let check = find_check(&doc, "devices");
    assert!(b(check, "ok"));
    assert_eq!(s(check, "detail"), "1（COM3）；排除 2（藍牙/exclude_coms）");
}

#[test]
fn devices_windows_registry_error() {
    let reg = FakeRegistry::new(vec![], &[]);
    reg.fail();
    let t = TestEnv::windows().with_fake_registry(reg);
    let (rc, out, _) = t.run(&["doctor"]);
    assert_eq!(rc, 0);
    let doc = json(&out);
    let check = find_check(&doc, "devices");
    assert!(!b(check, "ok"));
    assert_eq!(s(check, "detail"), "無法讀取 SERIALCOMM");
}

#[test]
fn python_runtime_missing_manifest() {
    let t = TestEnv::posix();
    let (rc, out, _) = t.run(&["doctor"]);
    assert_eq!(rc, 0);
    let doc = json(&out);
    let py_check = find_check(&doc, "python");
    let yaml_check = find_check(&doc, "pyyaml");
    assert!(!b(py_check, "ok"));
    assert_eq!(
        s(py_check, "detail"),
        "無法定位 daemon Python runtime（找不到 release manifest）"
    );
    assert!(!b(yaml_check, "ok"));
    assert_eq!(
        s(yaml_check, "detail"),
        "無法定位 daemon Python runtime（找不到 release manifest）"
    );
    let names: Vec<String> = list(&doc, "checks")
        .iter()
        .map(|c| s(c.as_dict(), "check").to_string())
        .collect();
    assert!(!names.contains(&"version_pairing".to_string()));
}

#[test]
fn version_pairing_daemon_down_is_unreachable() {
    let t = TestEnv::posix().with_doctor_bundle("3.12.7");
    let (rc, out, _) = t.run(&["doctor"]);
    assert_eq!(rc, 0);
    let doc = json(&out);
    assert!(!b(&doc, "ok"));
    let check = find_check(&doc, "version_pairing");
    assert_eq!(b(check, "advisory"), false);
    assert_eq!(s(check, "check"), "version_pairing");
    assert_eq!(s(check, "detail"), "daemon_unreachable");
    assert_eq!(b(check, "ok"), false);
    let mismatches: Vec<String> = list(check, "mismatches")
        .iter()
        .map(|v| match v {
            PyValue::Str(s) => s.clone(),
            _ => String::new(),
        })
        .collect();
    assert_eq!(mismatches, ["daemon_unreachable"]);
}

#[test]
fn version_pairing_mismatch_detail() {
    let t = TestEnv::posix().with_doctor_bundle("3.12.7");
    let ep = t.default_socket();
    // 與 CLI 版本（workspace 0.3.2）不同的舊版本，確保只產生 daemon_version 不符。
    let t = t.with_rpc_ok(&ep, "health.status", r#"{"ok":true,"version":"0.2.9"}"#);
    let (rc, out, _) = t.run(&["doctor"]);
    assert_eq!(rc, 0);
    let doc = json(&out);
    let check = find_check(&doc, "version_pairing");
    assert_eq!(b(check, "ok"), false);
    assert_eq!(s(check, "detail"), "daemon_version");
    let mismatches: Vec<String> = list(check, "mismatches")
        .iter()
        .map(|v| match v {
            PyValue::Str(s) => s.clone(),
            _ => String::new(),
        })
        .collect();
    assert_eq!(mismatches, ["daemon_version"]);
}

#[test]
fn version_pairing_invalid_manifest() {
    let t = TestEnv::posix();
    // 完整 manifest 但缺 `pairing`（計畫 Task 13 表列案例）；Python 基線依 _REQUIRED 順序回報第一個缺漏鍵。
    let full = manifest_fixture(sw_cli::CLI_VERSION, &"0".repeat(64));
    let mut root = match json(&full) {
        PyValue::Dict(d) => d,
        other => panic!("manifest 應為物件：{other:?}"),
    };
    root.0.shift_remove("pairing");
    let bad_manifest = sw_proto::pyjson::dumps_dict(&root, &sw_proto::pyjson::DISK_STABLE);
    t.write_file(
        &t.bundle_dir().join("release-manifest.json"),
        bad_manifest.as_bytes(),
    );
    let (rc, out, _) = t.run(&["doctor"]);
    assert_eq!(rc, 0);
    let doc = json(&out);
    let check = find_check(&doc, "version_pairing");
    assert_eq!(b(check, "ok"), false);
    assert_eq!(s(check, "detail"), "invalid release manifest: pairing");
    let mismatches: Vec<String> = list(check, "mismatches")
        .iter()
        .map(|v| match v {
            PyValue::Str(s) => s.clone(),
            _ => String::new(),
        })
        .collect();
    assert!(mismatches.is_empty());
}

#[test]
fn pyyaml_import_failure_is_not_installed_with_fix() {
    // runtime 存在、可執行但匯入失敗（rc≠0）：07 §7.3 的 未安裝 與 安裝 PyYAML 提示。
    let t = TestEnv::posix().with_doctor_bundle_except("3.12.7", &[PYYAML_CODE]);
    let rt = runtime_path(&t);
    let t = t.with_command(&[rt.as_str(), "-I", "-c", PYYAML_CODE], 1, "", "ModuleNotFoundError: yaml");
    let (rc, out, _) = t.run(&["doctor"]);
    assert_eq!(rc, 0);
    let doc = json(&out);
    let check = find_check(&doc, "pyyaml");
    assert!(!b(check, "ok"));
    assert_eq!(s(check, "detail"), "未安裝");
    assert_eq!(s(check, "fix"), "安裝 PyYAML（pipx 安裝會自動帶入）");
}

#[test]
fn windows_pyserial_import_failure_is_not_installed_with_fix() {
    let t = TestEnv::windows()
        .with_fake_registry(FakeRegistry::new(vec![], &[]))
        .with_doctor_bundle_except("3.12.7", &[PYSERIAL_CODE]);
    let rt = runtime_path(&t);
    let t = t.with_command(&[rt.as_str(), "-I", "-c", PYSERIAL_CODE], 1, "", "ModuleNotFoundError: serial");
    let (rc, out, _) = t.run(&["doctor"]);
    assert_eq!(rc, 0);
    let doc = json(&out);
    let check = find_check(&doc, "pyserial");
    assert!(!b(check, "ok"));
    assert_eq!(s(check, "detail"), "未安裝");
    assert_eq!(
        s(check, "fix"),
        "安裝 pyserial（pipx 於 Windows 自動帶入；release exe 已內嵌）"
    );
}

#[test]
fn python_runtime_nonzero_rc_is_unrunnable() {
    let t = TestEnv::posix().with_doctor_bundle_except("3.12.7", &[PY_VERSION_CODE]);
    let rt = runtime_path(&t);
    let t = t.with_command(&[rt.as_str(), "-I", "-c", PY_VERSION_CODE], 2, "", "boom");
    let (rc, out, _) = t.run(&["doctor"]);
    assert_eq!(rc, 0);
    let doc = json(&out);
    let check = find_check(&doc, "python");
    assert!(!b(check, "ok"));
    assert_eq!(s(check, "detail"), "無法執行 daemon Python runtime（rc=2）");
    assert_eq!(s(check, "fix"), FIX_REINSTALL);
}

#[test]
fn runtime_spawn_error_is_unrunnable_not_not_installed() {
    // spawn 失敗屬「無法執行」，不是「未安裝」（後者只指 runtime 有跑且匯入失敗）。
    let t = TestEnv::posix().with_doctor_bundle_except("3.12.7", &[PYYAML_CODE]);
    let rt = runtime_path(&t);
    let t = t.with_command_err(&[rt.as_str(), "-I", "-c", PYYAML_CODE], PalErrorKind::NotFound, "no such file");
    let (rc, out, _) = t.run(&["doctor"]);
    assert_eq!(rc, 0);
    let doc = json(&out);
    let check = find_check(&doc, "pyyaml");
    assert!(!b(check, "ok"));
    assert!(s(check, "detail").starts_with("無法執行 daemon Python runtime（"));
    assert_eq!(s(check, "fix"), FIX_REINSTALL);
}

#[test]
fn runtime_timeout_is_unrunnable_not_not_installed() {
    let t = TestEnv::posix().with_doctor_bundle_except("3.12.7", &[PYYAML_CODE]);
    let rt = runtime_path(&t);
    let t = t.with_command(&[rt.as_str(), "-I", "-c", PYYAML_CODE], -1, "", "TIMEOUT");
    let (rc, out, _) = t.run(&["doctor"]);
    assert_eq!(rc, 0);
    let doc = json(&out);
    let check = find_check(&doc, "pyyaml");
    assert!(!b(check, "ok"));
    assert_eq!(s(check, "detail"), "無法執行 daemon Python runtime（逾時（5 s））");
    assert_eq!(s(check, "fix"), FIX_REINSTALL);
}

#[test]
fn version_pairing_unreadable_manifest() {
    // 位元組非 UTF-8：Python `load_release_manifest` 為 unreadable（非「找不到」）。
    let t = TestEnv::posix();
    t.write_file(
        &t.bundle_dir().join("release-manifest.json"),
        &[0xffu8, 0xfe, b'{', b'}'],
    );
    let (rc, out, _) = t.run(&["doctor"]);
    assert_eq!(rc, 0);
    let doc = json(&out);
    let check = find_check(&doc, "version_pairing");
    assert!(!b(check, "ok"));
    assert_eq!(s(check, "detail"), "invalid release manifest: unreadable");
    assert!(list(check, "mismatches").is_empty());
    let last = list(&doc, "checks").last().expect("checks 非空").as_dict();
    assert_eq!(s(last, "check"), "version_pairing");
}

#[test]
fn version_pairing_parse_only_optional_field_key() {
    // 結構驗證通過、僅 sw_proto 嚴格解析拒絕的可選欄位：回報該欄位 key，不得為 unreadable。
    let t = TestEnv::posix();
    let manifest = manifest_fixture(sw_cli::CLI_VERSION, &"0".repeat(64)).replacen(
        "\"task_xml_template_sha256\":\"",
        "\"task_xml_template_sha256\":\"ZZ",
        1,
    );
    t.write_file(&t.bundle_dir().join("release-manifest.json"), manifest.as_bytes());
    let (rc, out, _) = t.run(&["doctor"]);
    assert_eq!(rc, 0);
    let doc = json(&out);
    let check = find_check(&doc, "version_pairing");
    assert!(!b(check, "ok"));
    assert_eq!(
        s(check, "detail"),
        "invalid release manifest: components.supervisor.task_xml_template_sha256"
    );
    assert!(list(check, "mismatches").is_empty());
}

#[test]
fn version_pairing_schema_version_float_one_is_valid() {
    // Python 基線接受 schema_version 1.0；doctor 不得回報 invalid（其餘 mismatch 來自測試 manifest 的零摘要）。
    let t = TestEnv::posix();
    let manifest = manifest_fixture(sw_cli::CLI_VERSION, &"0".repeat(64)).replacen(
        "\"schema_version\":1,",
        "\"schema_version\":1.0,",
        1,
    );
    assert!(manifest.contains("\"schema_version\":1.0,"));
    t.write_file(&t.bundle_dir().join("release-manifest.json"), manifest.as_bytes());
    let (rc, out, _) = t.run(&["doctor"]);
    assert_eq!(rc, 0);
    let doc = json(&out);
    let check = find_check(&doc, "version_pairing");
    assert!(!s(check, "detail").starts_with("invalid release manifest"));
}

#[test]
fn runtime_path_outside_bundle_is_not_executed() {
    // 絕對路徑 runtime 不得被執行（與 pairing 的 bundle 內相對路徑限制一致）。
    let t = TestEnv::posix();
    let manifest = manifest_fixture(sw_cli::CLI_VERSION, &"0".repeat(64)).replacen(
        "\"runtime\":{\"path\":\"py/bin/python3\"",
        "\"runtime\":{\"path\":\"/usr/bin/python3\"",
        1,
    );
    assert!(manifest.contains("/usr/bin/python3"));
    t.write_file(&t.bundle_dir().join("release-manifest.json"), manifest.as_bytes());
    let (rc, out, _) = t.run(&["doctor"]);
    assert_eq!(rc, 0);
    let doc = json(&out);
    let py = find_check(&doc, "python");
    assert!(!b(py, "ok"));
    assert_eq!(
        s(py, "detail"),
        "無法定位 daemon Python runtime（invalid release manifest: components.daemon.runtime.path）"
    );
    assert!(!t
        .calls()
        .iter()
        .any(|argv| argv.first().map(String::as_str) == Some("/usr/bin/python3")));
}

#[test]
fn doctor_ignores_timeout_and_retries() {
    // doctor 的固定逾時（health.status 0.5 s、runtime 5 s、--version 2 s）不讀 --timeout／--retries（01 §4.6）。
    let t = TestEnv::posix().with_doctor_bundle("3.12.7");
    let (rc, _out, _) = t.run(&["--timeout", "99", "--retries", "5", "doctor"]);
    assert_eq!(rc, 0);
    let rpc = t.rpc().timeouts_s();
    assert!(!rpc.is_empty());
    assert!(rpc.iter().all(|seconds| (seconds - 0.5).abs() < 1e-9), "health.status 逾時應為 0.5 s：{rpc:?}");
    let runtime_timeouts: Vec<_> = t
        .calls()
        .iter()
        .zip(t.sysenv_timeouts())
        .filter(|(argv, _)| argv.get(1).map(String::as_str) == Some("-I"))
        .map(|(_, timeout)| timeout)
        .collect();
    assert!(!runtime_timeouts.is_empty());
    assert!(runtime_timeouts.iter().all(|timeout| *timeout == Some(Duration::from_secs(5))));
}

#[test]
fn doctor_exit_always_zero() {
    let t = TestEnv::posix();
    let (rc, out, _) = t.run(&["doctor"]);
    assert_eq!(rc, 0);
    let doc = json(&out);
    assert!(!b(&doc, "ok"));
}
