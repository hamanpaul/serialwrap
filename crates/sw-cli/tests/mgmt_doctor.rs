//! P4-T13 `doctor` 全部檢查原生與 version_pairing（SC-25）失敗測試。
mod support;

use support::*;
use sw_pal::fakes::FakeRegistry;
use sw_pal::proc_scan::{DaemonProc, ProcIdentity};
use sw_proto::pyjson::{PyDict, PyValue};

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
    let t = TestEnv::posix().with_manifest_runtime_ok("3.12.7");
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
            assert!(!d.contains_key("fix") && d.contains_key("mismatches"));
        }
    }
}

#[test]
fn overall_ok_ignores_advisory_failures_only() {
    let t = TestEnv::posix()
        .with_manifest_runtime_ok("3.12.7")
        .all_required_ok()
        .with_which_missing("jq");
    let doc1 = json(&t.run(&["doctor"]).1);
    assert!(b(&doc1, "ok"));
    let t2 = TestEnv::posix()
        .with_manifest_runtime_ok("3.12.7")
        .all_required_ok()
        .without_group("dialout");
    let doc2 = json(&t2.run(&["doctor"]).1);
    assert!(!b(&doc2, "ok"));
}

#[test]
fn windows_check_order() {
    let t = TestEnv::windows().with_manifest_runtime_ok("3.12.7");
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
            assert!(!d.contains_key("fix") && d.contains_key("mismatches"));
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
        .with_config("socket: tcp://127.0.0.1:1\n")
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
            live_path.display()
        )
    );
}

#[test]
fn bootloader_prompts_not_materialized() {
    let t = TestEnv::posix();
    let live_path = t.config_dir().join("profiles").join("default.yaml");
    let (rc, out, _) = t.run(&["doctor"]);
    assert_eq!(rc, 0);
    let doc = json(&out);
    let check = find_check(&doc, "profile_bootloader_prompts");
    assert!(b(check, "ok"));
    assert_eq!(
        s(check, "detail"),
        format!("線上 profile 未物化（{}），略過", live_path.display())
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
    let t = TestEnv::posix().with_manifest_runtime_ok("3.12.7");
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
    let t = TestEnv::posix().with_manifest_runtime_ok("3.12.7");
    let ep = t.default_socket();
    let t = t.with_rpc_ok(&ep, "health.status", r#"{"ok":true,"version":"0.3.2"}"#);
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
    let bad_manifest = r#"{"schema_version":1,"bundle_version":"test","stage":"rust-1b-test"}"#;
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
fn doctor_exit_always_zero() {
    let t = TestEnv::posix();
    let (rc, out, _) = t.run(&["doctor"]);
    assert_eq!(rc, 0);
    let doc = json(&out);
    assert!(!b(&doc, "ok"));
}
