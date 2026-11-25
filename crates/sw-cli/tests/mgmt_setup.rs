//! P4-T10 `setup`（資產物化、WSL systemd、reconcile）與 `supervision-mode set` 參數驗證、reconcile OldStop 的回歸測試。
//!
//! 本檔驗證 P4-T10 的資產物化、WSL systemd、reconcile 與監管模式參數驗證。
//! 替身與 `TestEnv` 由 P4-T1（`tests/support/mod.rs`）提供。
//! 規格來源：`docs/rust-refactor/plans/05-stage1b-rust-cli-mgmt.md` Task 10；盤點 `inventory/07-platform-mgmt.md` §5、§6。

mod common;
mod support;

use std::collections::BTreeSet;
use std::sync::Arc;
use std::time::Duration;
use support::*;

use sw_proto::pyjson::PyValue;

const OK_PING: &str = r#"{"ok":true,"pid":1234,"version":"test"}"#;

fn dict_field(v: &PyValue, key: &str) -> PyValue {
    match v {
        PyValue::Dict(d) => d
            .get(key)
            .unwrap_or_else(|| panic!("key not found: {key}"))
            .clone(),
        _ => panic!("not a dict: {v:?}"),
    }
}

fn str_field(v: &PyValue, key: &str) -> String {
    match dict_field(v, key) {
        PyValue::Str(s) => s,
        other => panic!("key {key} not a string: {other:?}"),
    }
}

fn without_root_socket_path(bytes: &[u8]) -> Vec<u8> {
    let text = std::str::from_utf8(bytes).expect("config.yaml 必須是 UTF-8");
    text.split_inclusive('\n')
        .filter(|line| !line.starts_with("socket_path:"))
        .flat_map(str::as_bytes)
        .copied()
        .collect()
}

fn argv_list_field(v: &PyValue, key: &str) -> Vec<Vec<String>> {
    match dict_field(v, key) {
        PyValue::List(l) => l
            .iter()
            .map(|item| match item {
                PyValue::List(sub) => sub
                    .iter()
                    .map(|elem| match elem {
                        PyValue::Str(s) => s.clone(),
                        other => panic!("expected string in argv: {other:?}"),
                    })
                    .collect(),
                other => panic!("expected list in argv list: {other:?}"),
            })
            .collect(),
        other => panic!("key {key} not a list: {other:?}"),
    }
}

#[test]
fn s1m241_setup_assessment_matrix() {
    use sw_cli::setup_cmd::{daemon_code_assessment, DaemonCodeAssessment};

    fn assert_case(
        daemon_running: bool,
        daemon_fingerprint: Option<&str>,
        installed_fingerprint: Option<&str>,
        expected: DaemonCodeAssessment,
    ) {
        assert_eq!(
            daemon_code_assessment(daemon_running, daemon_fingerprint, installed_fingerprint),
            expected
        );
    }

    assert_case(
        false,
        Some("daemon-old"),
        Some("installed-new"),
        DaemonCodeAssessment {
            checked: false,
            stale: false,
            reason: Some("daemon_unreachable".to_string()),
            daemon: Some("daemon-old".to_string()),
            installed: Some("installed-new".to_string()),
        },
    );
    assert_case(
        true,
        Some("daemon-old"),
        None,
        DaemonCodeAssessment {
            checked: false,
            stale: false,
            reason: Some("installed_fingerprint_unavailable".to_string()),
            daemon: Some("daemon-old".to_string()),
            installed: None,
        },
    );
    assert_case(
        true,
        Some("daemon-old"),
        Some(""),
        DaemonCodeAssessment {
            checked: false,
            stale: false,
            reason: Some("installed_fingerprint_unavailable".to_string()),
            daemon: Some("daemon-old".to_string()),
            installed: Some("".to_string()),
        },
    );
    assert_case(
        true,
        None,
        Some("installed-new"),
        DaemonCodeAssessment {
            checked: true,
            stale: true,
            reason: Some("daemon_no_fingerprint".to_string()),
            daemon: None,
            installed: Some("installed-new".to_string()),
        },
    );
    assert_case(
        true,
        Some("same"),
        Some("same"),
        DaemonCodeAssessment {
            checked: true,
            stale: false,
            reason: Some("match".to_string()),
            daemon: Some("same".to_string()),
            installed: Some("same".to_string()),
        },
    );
    assert_case(
        true,
        Some("daemon-old"),
        Some("installed-new"),
        DaemonCodeAssessment {
            checked: true,
            stale: true,
            reason: Some("fingerprint_mismatch".to_string()),
            daemon: Some("daemon-old".to_string()),
            installed: Some("installed-new".to_string()),
        },
    );
}

#[test]
fn inst4_windows_default_port_selects_first_bindable_port() {
    let mut attempts = Vec::new();
    let selection = sw_cli::setup_cmd::select_windows_setup_endpoint(
        "tcp://127.0.0.1:48700",
        true,
        |_, port| {
            attempts.push(port);
            port == 48703
        },
    )
    .expect("應找到第一個可 bind 埠");

    assert_eq!(selection.endpoint, "tcp://127.0.0.1:48703");
    assert!(selection.auto_selected);
    assert_eq!(attempts, vec![48700, 48701, 48702, 48703]);
}

#[test]
fn inst4_windows_explicit_port_bind_failure_does_not_fall_back() {
    let mut attempts = Vec::new();
    let result = sw_cli::setup_cmd::select_windows_setup_endpoint(
        "tcp://127.0.0.1:49001",
        false,
        |_, port| {
            attempts.push(port);
            false
        },
    );

    assert!(result.is_err());
    assert_eq!(attempts, vec![49001]);
}

#[test]
fn inst4_windows_port_probe_uses_configured_loopback_host() {
    let mut attempts = Vec::new();
    let selection = sw_cli::setup_cmd::select_windows_setup_endpoint(
        "tcp://[::1]:49001",
        false,
        |host, port| {
            attempts.push((host.to_string(), port));
            true
        },
    )
    .expect("loopback 埠可 bind");

    assert_eq!(selection.endpoint, "tcp://[::1]:49001");
    assert_eq!(attempts, vec![("::1".to_string(), 49001)]);
}

#[test]
fn inst4_setup_ping_timeout_with_released_lock_allows_setup() {
    assert_eq!(
        sw_cli::setup_cmd::setup_daemon_presence_after_ping(false, || Ok(false)),
        sw_cli::setup_cmd::SetupDaemonPresence::StoppedByInstanceLock
    );
}

#[test]
fn inst4_setup_ping_timeout_with_held_lock_prevents_second_daemon() {
    assert_eq!(
        sw_cli::setup_cmd::setup_daemon_presence_after_ping(false, || Ok(true)),
        sw_cli::setup_cmd::SetupDaemonPresence::RunningByInstanceLock
    );
}

#[test]
fn inst4_setup_ping_timeout_with_unknown_lock_fails_closed() {
    assert_eq!(
        sw_cli::setup_cmd::setup_daemon_presence_after_ping(false, || Err(())),
        sw_cli::setup_cmd::SetupDaemonPresence::Unknown
    );
}

/// 建立真正的安裝目錄形狀，讓 `setup` 由正式 reconcile 路徑讀取 manifest。
fn s1m241_installed_setup_env(installed_fingerprint: Option<&str>) -> TestEnv {
    let version = sw_cli::CLI_VERSION;
    let t = TestEnv::posix()
        .with_real_service()
        .with_config("supervision_mode: on-demand\n");
    let root = t.bundle_dir();
    let version_dir = root.join("versions").join(version);
    support::write_complete_bundle(
        &version_dir,
        version,
        "x86_64-unknown-linux-gnu",
        false,
        &[],
        &[],
    );

    if let Some(fingerprint) = installed_fingerprint {
        let manifest_path = version_dir.join("release-manifest.json");
        let manifest = std::fs::read_to_string(&manifest_path).expect("manifest 讀取失敗");
        let needle = "\"state_schema\":\"python-state-v0\"";
        let replacement = format!("\"code_fingerprint\":\"{fingerprint}\",{needle}");
        let updated = manifest.replacen(needle, &replacement, 1);
        assert_ne!(updated, manifest, "測試 manifest 缺少 daemon state_schema");
        std::fs::write(&manifest_path, updated).expect("manifest 指紋寫入失敗");
    }

    let stable_bin = root.join("bin");
    std::fs::create_dir_all(&stable_bin).expect("安裝入口目錄建立失敗");
    for name in ["serialwrap", "serialwrapd"] {
        std::fs::copy(version_dir.join("bin").join(name), stable_bin.join(name))
            .expect("安裝入口複製失敗");
    }
    let current = format!(r#"{{"bundle_version":"{version}","dir":"versions/{version}"}}"#);
    std::fs::write(root.join("current-release.json"), current)
        .expect("current-release.json 寫入失敗");

    let manifest = version_dir.join("release-manifest.json");
    t.with_var("SERIALWRAP_INSTALL_ROOT", root.to_str().expect("安裝根路徑非 UTF-8"))
        .with_var(
            "SERIALWRAP_RELEASE_MANIFEST",
            manifest.to_str().expect("manifest 路徑非 UTF-8"),
        )
}

#[test]
fn s1m241_assessment_matching_ping_is_reported_once() {
    const FINGERPRINT: &str = "a1b2c3d4e5f6";
    let t = s1m241_installed_setup_env(Some(FINGERPRINT));
    let endpoint = t.default_socket();
    let t = t
        .with_rpc_ok(
            &endpoint,
            "health.ping",
            &format!(
                r#"{{"ok":true,"pid":1234,"version":"{}","code_fingerprint":"{FINGERPRINT}"}}"#,
                sw_cli::CLI_VERSION
            ),
        )
        .with_rpc_ok(&endpoint, "health.status", r#"{"ok":true,"pid":1234}"#)
        .with_rpc_ok(&endpoint, "mcu.status", r#"{"ok":true,"flashing":false}"#);

    let (rc, out, err) = t.run(&["setup", "--on-demand"]);
    assert_eq!(rc, 0, "setup 失敗：stdout={out:?}, stderr={err:?}");
    assert_eq!(err, "");
    assert_eq!(out.lines().count(), 1, "setup stdout 必須只有一行 JSON");
    let result = json(&out);
    let daemon_code = dict_field(&result, "daemon_code");
    assert_eq!(str_field(&daemon_code, "action"), "none");
    assert_eq!(str_field(&daemon_code, "daemon"), FINGERPRINT);
    assert_eq!(str_field(&daemon_code, "installed"), FINGERPRINT);
    assert!(matches!(
        &daemon_code,
        PyValue::Dict(fields) if fields.get("checked").is_none()
    ));
    assert_eq!(t.rpc().request_count(&endpoint, "health.ping"), 1);
    assert_eq!(t.rpc().request_count(&endpoint, "mcu.status"), 1);
    assert!(t.calls().iter().all(|argv| {
        !argv.iter().any(|arg| arg == "is-active" || arg == "restart")
    }));
}

#[test]
fn s1m241_assessment_is_omitted_without_installed_fingerprint() {
    const DAEMON_FINGERPRINT: &str = "a1b2c3d4e5f6";
    let t = s1m241_installed_setup_env(None);
    let endpoint = t.default_socket();
    let t = t
        .with_rpc_ok(
            &endpoint,
            "health.ping",
            &format!(r#"{{"ok":true,"pid":1234,"code_fingerprint":"{DAEMON_FINGERPRINT}"}}"#),
        )
        .with_rpc_ok(&endpoint, "health.status", r#"{"ok":true,"pid":1234}"#)
        .with_rpc_ok(&endpoint, "mcu.status", r#"{"ok":true,"flashing":false}"#);

    let (rc, out, err) = t.run(&["setup", "--on-demand"]);
    assert_eq!(rc, 0, "setup 失敗：stdout={out:?}, stderr={err:?}");
    let result = json(&out);
    assert!(matches!(
        &result,
        PyValue::Dict(fields) if fields.get("daemon_code").is_none()
    ));
    assert_eq!(t.rpc().request_count(&endpoint, "health.ping"), 1);
    assert_eq!(t.rpc().request_count(&endpoint, "mcu.status"), 1);
}

#[test]
fn s1m241_assessment_is_omitted_when_manifest_is_missing() {
    const FINGERPRINT: &str = "a1b2c3d4e5f6";
    let t = s1m241_installed_setup_env(Some(FINGERPRINT));
    let endpoint = t.default_socket();
    std::fs::remove_file(
        t.bundle_dir()
            .join("versions")
            .join(sw_cli::CLI_VERSION)
            .join("release-manifest.json"),
    )
    .expect("測試 manifest 移除失敗");
    let t = t
        .with_rpc_ok(
            &endpoint,
            "health.ping",
            &format!(r#"{{"ok":true,"pid":1234,"code_fingerprint":"{FINGERPRINT}"}}"#),
        )
        .with_rpc_ok(&endpoint, "health.status", r#"{"ok":true,"pid":1234}"#)
        .with_rpc_ok(&endpoint, "mcu.status", r#"{"ok":true,"flashing":false}"#);

    let (rc, out, err) = t.run(&["setup", "--on-demand"]);
    assert_eq!(rc, 0, "setup 失敗：stdout={out:?}, stderr={err:?}");
    let result = json(&out);
    assert!(matches!(
        &result,
        PyValue::Dict(fields) if fields.get("daemon_code").is_none()
    ));
    assert_eq!(t.rpc().request_count(&endpoint, "health.ping"), 1);
    assert_eq!(t.rpc().request_count(&endpoint, "mcu.status"), 1);
}

#[test]
fn s1m241_assessment_is_omitted_when_initial_ping_is_unreachable() {
    const FINGERPRINT: &str = "a1b2c3d4e5f6";
    let t = s1m241_installed_setup_env(Some(FINGERPRINT));
    let endpoint = t.default_socket();
    let t = t.with_rpc_ok(&endpoint, "mcu.status", r#"{"ok":false}"#);

    let (rc, out, err) = t.run(&["setup", "--on-demand"]);
    assert_eq!(rc, 0, "setup 失敗：stdout={out:?}, stderr={err:?}");
    let result = json(&out);
    assert!(matches!(
        &result,
        PyValue::Dict(fields) if fields.get("daemon_code").is_none()
    ));
    assert_eq!(t.rpc().request_count(&endpoint, "health.ping"), 1);
    assert_eq!(t.rpc().request_count(&endpoint, "mcu.status"), 1);
}

fn s1m241_setup_stale_service_is_restarted_and_verified_case() {
    const DAEMON_FINGERPRINT: &str = "deadbeef0001";
    const INSTALLED_FINGERPRINT: &str = "a1b2c3d4e5f6";
    let t = s1m241_installed_setup_env(Some(INSTALLED_FINGERPRINT));
    let endpoint = t.default_socket();
    let t = t
        .with_config("supervision_mode: systemd-user\n")
        .with_systemd(true)
        .with_command(
            &["systemctl", "--user", "is-active", "serialwrap"],
            0,
            "active\n",
            "",
        )
        .with_command(&["systemctl", "--user", "daemon-reload"], 0, "", "")
        .with_command(&["systemctl", "--user", "enable", "serialwrap"], 0, "", "")
        .with_command(&["systemctl", "--user", "restart", "serialwrap"], 0, "", "")
        .with_rpc_ok(&endpoint, "health.status", r#"{"ok":true,"pid":1234}"#)
        .with_rpc_ok(&endpoint, "mcu.status", r#"{"ok":true,"flashing":false}"#);
    t.rpc().respond_seq(
        &endpoint,
        "health.ping",
        &[
            &format!(r#"{{"ok":true,"code_fingerprint":"{DAEMON_FINGERPRINT}"}}"#),
            &format!(r#"{{"ok":true,"code_fingerprint":"{INSTALLED_FINGERPRINT}"}}"#),
        ],
    );

    let (rc, out, err) = t.run(&["setup", "--user"]);
    assert_eq!(rc, 0, "setup 失敗：stdout={out:?}, stderr={err:?}");
    assert_eq!(err, "");
    assert_eq!(out.lines().count(), 1, "setup stdout 必須只有一行 JSON");
    let result = json(&out);
    let daemon_code = match &result {
        PyValue::Dict(fields) => fields
            .get("daemon_code")
            .unwrap_or_else(|| panic!("daemon_code 缺失：stdout={out:?}, stderr={err:?}"))
            .clone(),
        other => panic!("setup 回應不是 object：{other:?}; stdout={out:?}"),
    };
    assert_eq!(str_field(&daemon_code, "action"), "restarted");
    assert_eq!(str_field(&daemon_code, "after"), "match");
    assert_eq!(str_field(&daemon_code, "daemon"), DAEMON_FINGERPRINT);
    assert_eq!(str_field(&daemon_code, "installed"), INSTALLED_FINGERPRINT);
    let setup = dict_field(&result, "setup");
    assert_eq!(str_field(&setup, "daemon_restart"), "ran");
    assert_eq!(t.rpc().request_count(&endpoint, "health.ping"), 2);
    assert_eq!(t.rpc().request_count(&endpoint, "mcu.status"), 1);
    let calls = t.calls();
    let position = |expected: &[&str]| {
        calls
            .iter()
            .position(|argv| argv.iter().map(String::as_str).eq(expected.iter().copied()))
            .unwrap_or_else(|| panic!("missing command {expected:?} in {calls:?}"))
    };
    assert!(
        position(&["systemctl", "--user", "daemon-reload"])
            < position(&["systemctl", "--user", "enable", "serialwrap"])
    );
    assert!(
        position(&["systemctl", "--user", "enable", "serialwrap"])
            < position(&["systemctl", "--user", "restart", "serialwrap"])
    );
    assert_eq!(
        calls
            .iter()
            .filter(|argv| argv.as_slice() == ["systemctl", "--user", "restart", "serialwrap"])
            .count(),
        1,
        "restart 必須只執行一次：{calls:?}"
    );
}

#[test]
fn s1m241_assessment_stale_systemd_restarts_and_verifies() {
    s1m241_setup_stale_service_is_restarted_and_verified_case();
}

#[test]
fn s1m241_setup_stale_service_is_restarted_and_verified() {
    s1m241_setup_stale_service_is_restarted_and_verified_case();
}

#[test]
fn s1m241_setup_stale_daemon_without_fingerprint_is_restarted() {
    const INSTALLED_FINGERPRINT: &str = "a1b2c3d4e5f6";
    let t = s1m241_installed_setup_env(Some(INSTALLED_FINGERPRINT));
    let endpoint = t.default_socket();
    let t = t
        .with_config("supervision_mode: systemd-user\n")
        .with_systemd(true)
        .with_command(
            &["systemctl", "--user", "is-active", "serialwrap"],
            0,
            "active\n",
            "",
        )
        .with_command(&["systemctl", "--user", "daemon-reload"], 0, "", "")
        .with_command(&["systemctl", "--user", "enable", "serialwrap"], 0, "", "")
        .with_command(&["systemctl", "--user", "restart", "serialwrap"], 0, "", "")
        .with_rpc_ok(&endpoint, "health.status", r#"{"ok":true,"pid":1234}"#)
        .with_rpc_ok(&endpoint, "mcu.status", r#"{"ok":true,"flashing":false}"#);
    t.rpc().respond_seq(
        &endpoint,
        "health.ping",
        &[
            r#"{"ok":true,"pid":1234,"version":"test"}"#,
            &format!(r#"{{"ok":true,"code_fingerprint":"{INSTALLED_FINGERPRINT}"}}"#),
        ],
    );

    let (rc, out, err) = t.run(&["setup", "--user"]);
    assert_eq!(rc, 0, "setup 失敗：stdout={out:?}, stderr={err:?}");
    assert_eq!(err, "");
    let result = json(&out);
    let daemon_code = dict_field(&result, "daemon_code");
    assert_eq!(str_field(&daemon_code, "reason"), "daemon_no_fingerprint");
    assert_eq!(str_field(&daemon_code, "action"), "restarted");
    assert_eq!(str_field(&daemon_code, "after"), "match");
    assert_eq!(
        str_field(&dict_field(&result, "setup"), "daemon_restart"),
        "ran"
    );
    assert_eq!(t.rpc().request_count(&endpoint, "health.ping"), 2);
    assert_eq!(
        t.calls()
            .iter()
            .filter(|argv| argv.as_slice() == ["systemctl", "--user", "restart", "serialwrap"])
            .count(),
        1
    );
}

#[test]
fn s1m241_setup_system_scope_with_sudo_restarts_after_enable() {
    const DAEMON_FINGERPRINT: &str = "deadbeef0001";
    const INSTALLED_FINGERPRINT: &str = "a1b2c3d4e5f6";
    let t = s1m241_installed_setup_env(Some(INSTALLED_FINGERPRINT));
    let endpoint = t.default_socket();
    let system_socket = sw_cli::endpoint_resolve::SYSTEMD_SYSTEM_SOCKET;
    let t = t
        .with_config("supervision_mode: systemd-system\n")
        .with_systemd(true)
        .with_command(&["systemctl", "is-active", "serialwrap"], 0, "active\n", "")
        .with_rpc_ok(&endpoint, "health.status", r#"{"ok":true,"pid":1234}"#)
        .with_rpc_ok(&endpoint, "mcu.status", r#"{"ok":true,"flashing":false}"#)
        .with_rpc_ok(
            &endpoint,
            "health.ping",
            &format!(r#"{{"ok":true,"code_fingerprint":"{DAEMON_FINGERPRINT}"}}"#),
        )
        .with_rpc_ok(
            system_socket,
            "health.ping",
            &format!(r#"{{"ok":true,"code_fingerprint":"{INSTALLED_FINGERPRINT}"}}"#),
        );
    // reconcile 會把 systemd-system 的 socket 寫成系統 socket；重啟後的輪詢必須打那裡。

    let (rc, out, err) = t.run(&["setup", "--system", "--with-sudo"]);
    assert_eq!(rc, 0, "setup 失敗：stdout={out:?}, stderr={err:?}");
    assert_eq!(err, "");
    let result = json(&out);
    let daemon_code = dict_field(&result, "daemon_code");
    assert_eq!(str_field(&daemon_code, "reason"), "fingerprint_mismatch");
    assert_eq!(str_field(&daemon_code, "daemon"), DAEMON_FINGERPRINT);
    assert_eq!(str_field(&daemon_code, "installed"), INSTALLED_FINGERPRINT);
    assert_eq!(str_field(&daemon_code, "action"), "restarted");
    assert_eq!(str_field(&daemon_code, "after"), "match");
    assert_eq!(
        str_field(&dict_field(&result, "setup"), "daemon_restart"),
        "ran"
    );
    let calls = t.calls();
    let position = |expected: &[&str]| {
        calls
            .iter()
            .position(|argv| argv.iter().map(String::as_str).eq(expected.iter().copied()))
            .unwrap_or_else(|| panic!("missing command {expected:?} in {calls:?}"))
    };
    assert!(
        position(&["sudo", "systemctl", "daemon-reload"])
            < position(&["sudo", "systemctl", "enable", "serialwrap"])
    );
    assert!(
        position(&["sudo", "systemctl", "enable", "serialwrap"])
            < position(&["sudo", "systemctl", "restart", "serialwrap"])
    );
    assert_eq!(
        calls
            .iter()
            .filter(|argv| argv.as_slice() == ["sudo", "systemctl", "restart", "serialwrap"])
            .count(),
        1,
        "sudo restart 必須只執行一次：{calls:?}"
    );
    assert_eq!(t.rpc().request_count(&endpoint, "health.ping"), 1);
    assert_eq!(t.rpc().request_count(system_socket, "health.ping"), 1);
}

#[test]
fn s1m241_assessment_stale_systemd_pending_sudo_is_reported() {
    const DAEMON_FINGERPRINT: &str = "deadbeef0001";
    const INSTALLED_FINGERPRINT: &str = "a1b2c3d4e5f6";
    let t = s1m241_installed_setup_env(Some(INSTALLED_FINGERPRINT));
    let endpoint = t.default_socket();
    let t = t
        .with_config("supervision_mode: systemd-system\n")
        .with_systemd(true)
        .with_command(&["systemctl", "is-active", "serialwrap"], 0, "active\n", "")
        .with_rpc_ok(&endpoint, "health.status", r#"{"ok":true,"pid":1234}"#)
        .with_rpc_ok(&endpoint, "mcu.status", r#"{"ok":true,"flashing":false}"#);
    t.rpc().respond_seq(
        &endpoint,
        "health.ping",
        &[&format!(
            r#"{{"ok":true,"code_fingerprint":"{DAEMON_FINGERPRINT}"}}"#
        )],
    );

    let (rc, out, err) = t.run(&["setup", "--system"]);
    assert_eq!(rc, 0, "setup 失敗：stdout={out:?}, stderr={err:?}");
    assert_eq!(err, "");
    let result = json(&out);
    let daemon_code = dict_field(&result, "daemon_code");
    assert_eq!(str_field(&daemon_code, "action"), "pending_sudo");
    assert_eq!(
        str_field(&daemon_code, "hint"),
        "system scope 需 root：請執行 pending_sudo 內的 restart 指令，或加 --with-sudo 重跑 setup"
    );
    assert_eq!(
        str_field(&dict_field(&result, "setup"), "daemon_restart"),
        "pending_sudo"
    );
    let setup = dict_field(&result, "setup");
    assert!(argv_list_field(&setup, "pending_sudo")
        .iter()
        .any(|argv| argv == &["sudo", "systemctl", "restart", "serialwrap"]));
    assert!(matches!(
        &daemon_code,
        PyValue::Dict(fields) if fields.get("after").is_none()
    ));
    assert_eq!(t.rpc().request_count(&endpoint, "health.ping"), 1);
    assert!(t
        .calls()
        .iter()
        .all(|argv| { !argv.iter().any(|arg| arg == "sudo" || arg == "restart") }));
}

#[test]
fn s1m241_assessment_stale_systemd_restart_failure_is_reported() {
    const DAEMON_FINGERPRINT: &str = "deadbeef0001";
    const INSTALLED_FINGERPRINT: &str = "a1b2c3d4e5f6";
    let t = s1m241_installed_setup_env(Some(INSTALLED_FINGERPRINT));
    let endpoint = t.default_socket();
    let t = t
        .with_config("supervision_mode: systemd-user\n")
        .with_systemd(true)
        .with_command(
            &["systemctl", "--user", "is-active", "serialwrap"],
            0,
            "active\n",
            "",
        )
        .with_command(&["systemctl", "--user", "daemon-reload"], 0, "", "")
        .with_command(&["systemctl", "--user", "enable", "serialwrap"], 0, "", "")
        .with_command(
            &["systemctl", "--user", "restart", "serialwrap"],
            1,
            "",
            "失敗",
        )
        .with_rpc_ok(&endpoint, "health.status", r#"{"ok":true,"pid":1234}"#)
        .with_rpc_ok(&endpoint, "mcu.status", r#"{"ok":true,"flashing":false}"#);
    t.rpc().respond_seq(
        &endpoint,
        "health.ping",
        &[&format!(
            r#"{{"ok":true,"code_fingerprint":"{DAEMON_FINGERPRINT}"}}"#
        )],
    );

    let (rc, out, err) = t.run(&["setup", "--user"]);
    assert_eq!(rc, 0, "setup 失敗：stdout={out:?}, stderr={err:?}");
    assert_eq!(err, "");
    let result = json(&out);
    let daemon_code = dict_field(&result, "daemon_code");
    assert_eq!(str_field(&daemon_code, "action"), "restart_failed");
    assert_eq!(
        str_field(&daemon_code, "hint"),
        "systemctl restart 失敗；請手動執行 `serialwrap service restart` 並查 journalctl"
    );
    assert_eq!(
        str_field(&dict_field(&result, "setup"), "daemon_restart"),
        "failed"
    );
}

#[test]
fn s1m241_assessment_restart_after_unreachable_is_reported() {
    const DAEMON_FINGERPRINT: &str = "deadbeef0001";
    const INSTALLED_FINGERPRINT: &str = "a1b2c3d4e5f6";
    let t = s1m241_installed_setup_env(Some(INSTALLED_FINGERPRINT));
    let endpoint = t.default_socket();
    let t = t
        .with_config("supervision_mode: systemd-user\n")
        .with_systemd(true)
        .with_command(
            &["systemctl", "--user", "is-active", "serialwrap"],
            0,
            "active\n",
            "",
        )
        .with_command(&["systemctl", "--user", "daemon-reload"], 0, "", "")
        .with_command(&["systemctl", "--user", "enable", "serialwrap"], 0, "", "")
        .with_command(&["systemctl", "--user", "restart", "serialwrap"], 0, "", "")
        .with_rpc_ok(&endpoint, "health.status", r#"{"ok":true,"pid":1234}"#)
        .with_rpc_ok(&endpoint, "mcu.status", r#"{"ok":true,"flashing":false}"#);
    t.rpc().respond_seq(
        &endpoint,
        "health.ping",
        &[
            &format!(r#"{{"ok":true,"code_fingerprint":"{DAEMON_FINGERPRINT}"}}"#),
            r#"{"ok":false}"#,
        ],
    );

    let (rc, out, err) = t.run(&["setup", "--user"]);
    assert_eq!(rc, 0, "setup 失敗：stdout={out:?}, stderr={err:?}");
    assert!(err.contains("after=unreachable"), "stderr={err:?}");
    assert_eq!(out.lines().count(), 1, "setup stdout 必須只有一行 JSON");
    let result = json(&out);
    let daemon_code = dict_field(&result, "daemon_code");
    assert_eq!(str_field(&daemon_code, "action"), "restarted");
    assert_eq!(str_field(&daemon_code, "after"), "unreachable");
    assert_eq!(t.rpc().request_count(&endpoint, "health.ping"), 22);
    assert_eq!(t.rpc().request_count(&endpoint, "mcu.status"), 1);
    assert!(t
        .calls()
        .iter()
        .any(|argv| { argv == &["systemctl", "--user", "restart", "serialwrap"] }));
}

fn s1m241_failure_setup_fixture(post_restart_pings: &[&str], restart_rc: i32) -> (TestEnv, String) {
    const DAEMON_FINGERPRINT: &str = "deadbeef0001";
    const INSTALLED_FINGERPRINT: &str = "a1b2c3d4e5f6";

    let t = s1m241_installed_setup_env(Some(INSTALLED_FINGERPRINT));
    let endpoint = t.default_socket();
    let t = t
        .with_config("supervision_mode: systemd-user\n")
        .with_systemd(true)
        .with_command(
            &["systemctl", "--user", "is-active", "serialwrap"],
            0,
            "active\n",
            "",
        )
        .with_command(&["systemctl", "--user", "daemon-reload"], 0, "", "")
        .with_command(&["systemctl", "--user", "enable", "serialwrap"], 0, "", "")
        .with_command(
            &["systemctl", "--user", "restart", "serialwrap"],
            restart_rc,
            "",
            if restart_rc == 0 {
                ""
            } else {
                "restart failed"
            },
        )
        .with_rpc_ok(&endpoint, "health.status", r#"{"ok":true,"pid":1234}"#)
        .with_rpc_ok(&endpoint, "mcu.status", r#"{"ok":true,"flashing":false}"#);

    let mut pings = vec![format!(
        r#"{{"ok":true,"code_fingerprint":"{DAEMON_FINGERPRINT}"}}"#
    )];
    pings.extend(post_restart_pings.iter().map(|line| (*line).to_string()));
    let ping_refs: Vec<&str> = pings.iter().map(String::as_str).collect();
    t.rpc().respond_seq(&endpoint, "health.ping", &ping_refs);
    (t, endpoint)
}

fn s1m241_failure_assert_report(
    rc: i32,
    stdout: &str,
    stderr: &str,
    expected_action: &str,
    expected_after: Option<&str>,
    expected_hint: Option<&str>,
    expected_stderr: &str,
) -> PyValue {
    assert_eq!(
        rc, 0,
        "setup 應以 exit 0 結束：stdout={stdout:?}, stderr={stderr:?}"
    );
    assert_eq!(
        stdout.lines().count(),
        1,
        "stdout 必須恰為單一 JSON：{stdout:?}"
    );
    assert_eq!(stderr, expected_stderr, "stderr 必須逐字符合 Python 基線");

    let response = json(stdout);
    let daemon_code = dict_field(&response, "daemon_code");
    assert_eq!(str_field(&daemon_code, "action"), expected_action);
    match expected_after {
        Some(after) => assert_eq!(str_field(&daemon_code, "after"), after),
        None => assert!(
            matches!(
                &daemon_code,
                PyValue::Dict(fields) if fields.get("after").is_none()
            ),
            "不應輸出 after：{daemon_code:?}"
        ),
    }
    match expected_hint {
        Some(hint) => assert_eq!(str_field(&daemon_code, "hint"), hint),
        None => assert!(
            matches!(
                &daemon_code,
                PyValue::Dict(fields) if fields.get("hint").is_none()
            ),
            "不應輸出 hint：{daemon_code:?}"
        ),
    }
    assert!(
        matches!(
            &daemon_code,
            PyValue::Dict(fields) if fields.get("checked").is_none()
        ),
        "checked 不得外洩：{daemon_code:?}"
    );
    response
}

#[test]
fn s1m241_failure_after_mismatch_preserves_python_stderr_and_exit_zero() {
    let (t, endpoint) =
        s1m241_failure_setup_fixture(&[r#"{"ok":true,"code_fingerprint":"different0001"}"#], 0);
    let (rc, stdout, stderr) = t.run(&["setup", "--user"]);
    let hint = "serialwrap: setup 已重啟 daemon，但它仍未回報與安裝版相同的程式碼指紋（after=mismatch）；用 `serialwrap daemon status` 看 code_path，確認 systemd unit 的 ExecStart 指向這份安裝";

    let response = s1m241_failure_assert_report(
        rc,
        &stdout,
        &stderr,
        "restarted",
        Some("mismatch"),
        Some(hint),
        &format!("{hint}\n"),
    );
    let daemon_code = dict_field(&response, "daemon_code");
    assert_eq!(str_field(&daemon_code, "reason"), "fingerprint_mismatch");
    assert_eq!(str_field(&daemon_code, "daemon"), "deadbeef0001");
    assert_eq!(str_field(&daemon_code, "installed"), "a1b2c3d4e5f6");
    assert_eq!(t.rpc().request_count(&endpoint, "health.ping"), 2);
    assert!(
        t.clock().sleeps_s().is_empty(),
        "首次成功但指紋不同時須立即結束驗證"
    );
}

#[test]
fn s1m241_failure_after_unreachable_preserves_python_stderr_and_exit_zero() {
    let (t, endpoint) = s1m241_failure_setup_fixture(&[r#"{"ok":false}"#], 0);
    let (rc, stdout, stderr) = t.run(&["setup", "--user"]);
    let hint = "serialwrap: setup 已重啟 daemon，但它仍未回報與安裝版相同的程式碼指紋（after=unreachable）；用 `serialwrap daemon status` 看 code_path，確認 systemd unit 的 ExecStart 指向這份安裝";

    s1m241_failure_assert_report(
        rc,
        &stdout,
        &stderr,
        "restarted",
        Some("unreachable"),
        Some(hint),
        &format!("{hint}\n"),
    );
    assert_eq!(t.rpc().request_count(&endpoint, "health.ping"), 22);
    assert_eq!(t.clock().elapsed(), Duration::from_secs(10));
    assert_eq!(t.clock().sleeps_s().len(), 20);
    assert!(t.clock().sleeps_s().iter().all(|sleep| *sleep == 0.5));
}

#[test]
fn s1m241_failure_restart_error_preserves_python_hint_without_postcheck() {
    let (t, endpoint) =
        s1m241_failure_setup_fixture(&[r#"{"ok":true,"code_fingerprint":"a1b2c3d4e5f6"}"#], 1);
    let (rc, stdout, stderr) = t.run(&["setup", "--user"]);
    let hint = "systemctl restart 失敗；請手動執行 `serialwrap service restart` 並查 journalctl";

    let response =
        s1m241_failure_assert_report(rc, &stdout, &stderr, "restart_failed", None, Some(hint), "");
    assert_eq!(
        str_field(&dict_field(&response, "setup"), "daemon_restart"),
        "failed"
    );
    assert_eq!(t.rpc().request_count(&endpoint, "health.ping"), 1);
    assert_eq!(
        t.calls()
            .iter()
            .filter(|argv| argv.as_slice() == ["systemctl", "--user", "restart", "serialwrap"])
            .count(),
        1,
        "restart 失敗時只能執行一次，且不可進入成功後驗證"
    );
}

#[test]
fn s1m241_failure_retries_unreachable_then_reports_match() {
    let (t, endpoint) = s1m241_failure_setup_fixture(
        &[
            r#"{"ok":false}"#,
            r#"{"ok":true,"code_fingerprint":"a1b2c3d4e5f6"}"#,
        ],
        0,
    );
    let (rc, stdout, stderr) = t.run(&["setup", "--user"]);

    let response =
        s1m241_failure_assert_report(rc, &stdout, &stderr, "restarted", Some("match"), None, "");
    let daemon_code = dict_field(&response, "daemon_code");
    assert_eq!(str_field(&daemon_code, "daemon"), "deadbeef0001");
    assert_eq!(str_field(&daemon_code, "installed"), "a1b2c3d4e5f6");
    assert_eq!(t.rpc().request_count(&endpoint, "health.ping"), 3);
    assert_eq!(t.clock().elapsed(), Duration::from_millis(500));
    assert_eq!(t.clock().sleeps_s(), vec![0.5]);
}

#[test]
fn s1m241_skips_no_restart_flag() {
    const DAEMON_FINGERPRINT: &str = "deadbeef0001";
    const INSTALLED_FINGERPRINT: &str = "a1b2c3d4e5f6";
    let t = s1m241_installed_setup_env(Some(INSTALLED_FINGERPRINT));
    let endpoint = t.default_socket();
    let t = t
        .with_config("supervision_mode: systemd-user\n")
        .with_systemd(true)
        .with_command(
            &["systemctl", "--user", "is-active", "serialwrap"],
            0,
            "active\n",
            "",
        )
        .with_rpc_ok(
            &endpoint,
            "health.ping",
            &format!(
                r#"{{"ok":true,"pid":1234,"version":"{}","code_fingerprint":"{DAEMON_FINGERPRINT}"}}"#,
                sw_cli::CLI_VERSION
            ),
        )
        .with_rpc_ok(&endpoint, "health.status", r#"{"ok":true,"pid":1234}"#)
        .with_rpc_ok(&endpoint, "mcu.status", r#"{"ok":true,"flashing":false}"#);

    let (rc, out, err) = t.run(&["setup", "--user", "--no-restart"]);
    assert_eq!(rc, 0, "setup 失敗：stdout={out:?}, stderr={err:?}");
    assert_eq!(err, "");
    assert_eq!(out.lines().count(), 1, "setup stdout 必須只有一行 JSON");
    let result = json(&out);
    let daemon_code = dict_field(&result, "daemon_code");
    assert_eq!(str_field(&daemon_code, "action"), "skipped_no_restart_flag");
    assert_eq!(
        str_field(&daemon_code, "hint"),
        "已依 --no-restart 略過；準備好後執行 `serialwrap service restart`"
    );
    assert_eq!(str_field(&daemon_code, "daemon"), DAEMON_FINGERPRINT);
    assert_eq!(str_field(&daemon_code, "installed"), INSTALLED_FINGERPRINT);
    assert_eq!(dict_field(&daemon_code, "stale"), PyValue::Bool(true));
    assert!(matches!(
        &daemon_code,
        PyValue::Dict(fields) if fields.get("after").is_none()
    ));
    assert!(matches!(
        dict_field(&result, "setup"),
        PyValue::Dict(fields) if fields.get("daemon_restart").is_none()
    ));
    assert_eq!(t.rpc().request_count(&endpoint, "health.ping"), 1);
    assert_eq!(t.rpc().request_count(&endpoint, "mcu.status"), 1);
    assert!(t.clock().sleeps_s().is_empty(), "跳過時不得輪詢指紋");
    assert!(
        t.calls().iter().all(|argv| {
            !argv.iter().any(|arg| {
                arg == "is-active" || arg == "restart" || arg == "start" || arg == "stop"
            })
        }),
        "--no-restart 不得查服務或追加重啟：{:?}",
        t.calls()
    );
}

#[test]
fn s1m241_skips_decision_priority() {
    struct Case {
        old: &'static str,
        flags: &'static [&'static str],
        flashing: bool,
        action: &'static str,
        hint: Option<&'static str>,
    }
    // 各列同時符合後續條件，鎖住 Python _run_setup 的第一個命中分支。
    let cases = [
        Case {
            old: "systemd-user",
            flags: &["--user", "--no-restart", "--force"],
            flashing: true,
            action: "skipped_no_restart_flag",
            hint: Some("已依 --no-restart 略過；準備好後執行 `serialwrap service restart`"),
        },
        Case {
            old: "systemd-user",
            flags: &["--on-demand", "--force"],
            flashing: true,
            action: "mode_transition",
            hint: None,
        },
        Case {
            old: "on-demand",
            flags: &["--on-demand", "--force"],
            flashing: true,
            action: "manual_restart",
            hint: Some("on-demand 模式不自動重啟；請執行 `serialwrap daemon stop` 後 `serialwrap daemon start`"),
        },
        Case {
            old: "systemd-user",
            flags: &["--user", "--force"],
            flashing: true,
            action: "skipped_flashing",
            hint: Some("flash 進行中不重啟 daemon；完成後執行 `serialwrap service restart`"),
        },
        Case {
            old: "systemd-user",
            flags: &["--user"],
            flashing: false,
            action: "service_inactive",
            hint: Some("在跑的 daemon 不是 systemd 服務啟動的（可能是手動 start 的）；先停掉它，再 `serialwrap service restart`"),
        },
        Case {
            old: "systemd-system",
            flags: &["--system"],
            flashing: false,
            action: "service_inactive",
            hint: Some("在跑的 daemon 不是 systemd 服務啟動的（可能是手動 start 的）；先停掉它，再 `serialwrap service restart`"),
        },
    ];

    for case in cases {
        let t = s1m241_installed_setup_env(Some("a1b2c3d4e5f6"))
            .with_config(&format!("supervision_mode: {}\n", case.old))
            .with_systemd(true)
            .with_command(
                &["systemctl", "--user", "is-active", "serialwrap"],
                3,
                "inactive\n",
                "",
            )
            .with_command(
                &["systemctl", "is-active", "serialwrap"],
                3,
                "inactive\n",
                "",
            )
            .with_command(&["systemctl", "--user", "stop", "serialwrap"], 0, "", "")
            .with_command(&["systemctl", "--user", "disable", "serialwrap"], 0, "", "");
        let endpoint = t.default_socket();
        let transitioning = case.action == "mode_transition";
        let t = if transitioning {
            // ping、flash、身分查詢後，停止舊服務；第 4 次連線證明已退出。
            t.with_daemon_alive_then_gone(&endpoint, 1234, 4)
                .with_gone_ping_event()
        } else {
            t
        };
        let t = t
            .with_rpc_ok(
                &endpoint,
                "health.ping",
                &format!(
                    r#"{{"ok":true,"pid":1234,"version":"{}","code_fingerprint":"deadbeef0001"}}"#,
                    sw_cli::CLI_VERSION
                ),
            )
            .with_rpc_ok(&endpoint, "health.status", r#"{"ok":true,"pid":1234}"#)
            .with_rpc_ok(
                &endpoint,
                "mcu.status",
                &format!(r#"{{"ok":true,"flashing":{}}}"#, case.flashing),
            );
        let mut argv = vec!["--endpoint", &endpoint, "setup"];
        argv.extend_from_slice(case.flags);
        let (rc, out, err) = t.run(&argv);
        assert_eq!(rc, 0, "{argv:?}：stdout={out:?}, stderr={err:?}");
        assert_eq!(err, "", "{argv:?}");
        assert_eq!(out.lines().count(), 1, "{argv:?}");
        let result = json(&out);
        let daemon_code = dict_field(&result, "daemon_code");
        assert_eq!(str_field(&daemon_code, "action"), case.action, "{argv:?}");
        assert_eq!(str_field(&daemon_code, "reason"), "fingerprint_mismatch");
        assert_eq!(dict_field(&daemon_code, "stale"), PyValue::Bool(true));
        assert_eq!(str_field(&daemon_code, "daemon"), "deadbeef0001");
        assert_eq!(str_field(&daemon_code, "installed"), "a1b2c3d4e5f6");
        let PyValue::Dict(fields) = &daemon_code else {
            panic!("daemon_code 必須是物件")
        };
        assert_eq!(
            fields.get("hint"),
            case.hint.map(|hint| PyValue::Str(hint.into())).as_ref()
        );
        assert!(fields.get("after").is_none(), "跳過時不得驗證新指紋");
        assert!(fields.get("checked").is_none(), "內部欄位不得出現在輸出");
        let setup = dict_field(&result, "setup");
        let PyValue::Dict(fields) = &setup else {
            panic!("setup 必須是物件")
        };
        assert!(fields.get("daemon_restart").is_none());
        assert_eq!(
            dict_field(&setup, "transitioned"),
            PyValue::Bool(transitioning)
        );
        assert!(argv_list_field(&setup, "ran")
            .iter()
            .all(|cmd| !cmd.iter().any(|arg| arg == "restart" || arg == "start")));
        assert!(argv_list_field(&setup, "pending_sudo")
            .iter()
            .all(|cmd| !cmd.iter().any(|arg| arg == "restart" || arg == "start")));
        assert!(t
            .calls()
            .iter()
            .all(|cmd| !cmd.iter().any(|arg| arg == "restart" || arg == "start")));
        let queries: Vec<_> = t
            .calls()
            .into_iter()
            .filter(|cmd| cmd.iter().any(|arg| arg == "is-active"))
            .collect();
        let expected_queries = if case.action == "service_inactive" {
            vec![if case.old == "systemd-user" {
                vec!["systemctl", "--user", "is-active", "serialwrap"]
            } else {
                vec!["systemctl", "is-active", "serialwrap"]
            }]
        } else {
            vec![]
        };
        assert_eq!(queries, expected_queries, "{argv:?}");
        // 舊 daemon 的退出探測在連線階段被拒，沒有送出第 2 個 ping 請求。
        assert_eq!(t.rpc().request_count(&endpoint, "health.ping"), 1);
        assert_eq!(
            t.rpc().connect_count(&endpoint),
            if transitioning { 4 } else { 3 }
        );
        if transitioning {
            let order = t.event_order();
            assert!(
                order.index_of("run:systemctl --user stop serialwrap")
                    < order.index_of("rpc:health.ping#gone")
            );
        }
        assert_eq!(t.rpc().request_count(&endpoint, "mcu.status"), 1);
        assert!(
            t.clock().sleeps_s().is_empty(),
            "跳過時不得進入重啟後輪詢：{argv:?}"
        );
    }
}

#[test]
fn s1m241_skips_no_restart_flag_preserves_mode_transition() {
    for no_restart in [false, true] {
        let t = s1m241_installed_setup_env(Some("a1b2c3d4e5f6"))
            .with_systemd(true)
            .with_var("USER", "alice");
        let endpoint = t.default_socket();
        let t = t
            .with_daemon_alive_then_gone(&endpoint, 1234, 5)
            .with_gone_ping_event()
            .with_rpc_ok(
                &endpoint,
                "health.ping",
                r#"{"ok":true,"pid":1234,"code_fingerprint":"deadbeef0001"}"#,
            )
            .with_rpc_ok(&endpoint, "mcu.status", r#"{"ok":true,"flashing":true}"#);
        let mut argv = vec!["setup", "--user", "--force"];
        if no_restart {
            argv.push("--no-restart");
        }
        let (rc, out, err) = t.run(&argv);
        assert_eq!(rc, 0, "{argv:?}：stdout={out:?}, stderr={err:?}");
        let result = json(&out);
        let daemon_code = dict_field(&result, "daemon_code");
        assert_eq!(
            str_field(&daemon_code, "action"),
            if no_restart {
                "skipped_no_restart_flag"
            } else {
                "mode_transition"
            }
        );
        if no_restart {
            assert_eq!(
                str_field(&daemon_code, "hint"),
                "已依 --no-restart 略過；準備好後執行 `serialwrap service restart`"
            );
        } else {
            assert!(matches!(&daemon_code, PyValue::Dict(fields) if fields.get("hint").is_none()));
        }
        assert!(matches!(&daemon_code, PyValue::Dict(fields) if fields.get("after").is_none()));
        let setup = dict_field(&result, "setup");
        assert_eq!(dict_field(&setup, "transitioned"), PyValue::Bool(true));
        assert!(matches!(&setup, PyValue::Dict(fields) if fields.get("daemon_restart").is_none()));
        assert_eq!(
            argv_list_field(&setup, "ran"),
            vec![
                vec!["serialwrap", "daemon", "stop"],
                vec!["systemctl", "--user", "daemon-reload"],
                vec!["systemctl", "--user", "enable", "serialwrap"],
                vec!["systemctl", "--user", "start", "serialwrap"],
                vec!["loginctl", "enable-linger", "alice"],
            ]
        );
        assert!(t
            .calls()
            .iter()
            .all(|cmd| !cmd.iter().any(|arg| arg == "restart" || arg == "is-active")));
        let order = t.event_order();
        assert!(order.index_of("rpc:daemon.stop") < order.index_of("rpc:health.ping#gone"));
        assert!(
            order.index_of("rpc:health.ping#gone")
                < order.index_of("run:systemctl --user start serialwrap")
        );
        assert_eq!(t.rpc().request_count(&endpoint, "daemon.stop"), 1);
        assert_eq!(t.rpc().request_count(&endpoint, "health.ping"), 1);
        assert_eq!(
            t.rpc().connect_count(&endpoint),
            5,
            "包含退出探測，不得追加指紋驗證連線"
        );
        assert!(t.clock().sleeps_s().is_empty(), "模式轉換不得追加指紋輪詢");
    }
}

#[test]
fn s1m241_skips_flashing_without_force_before_materialize() {
    for no_restart in [false, true] {
        let t = s1m241_installed_setup_env(Some("a1b2c3d4e5f6"))
            .with_config("supervision_mode: systemd-user\n")
            .with_systemd(true);
        let config_before = std::fs::read(t.config_path()).unwrap();
        let endpoint = t.default_socket();
        let t = t
            .with_rpc_ok(
                &endpoint,
                "health.ping",
                r#"{"ok":true,"code_fingerprint":"deadbeef0001"}"#,
            )
            .with_rpc_ok(&endpoint, "mcu.status", r#"{"ok":true,"flashing":true}"#);
        let mut argv = vec!["setup", "--user"];
        if no_restart {
            argv.push("--no-restart");
        }
        let (rc, out, err) = t.run(&argv);
        assert_eq!(rc, 2, "{argv:?}：stdout={out:?}, stderr={err:?}");
        let result = json(&out);
        assert_eq!(dict_field(&result, "ok"), PyValue::Bool(false));
        assert_eq!(str_field(&result, "error_code"), "FLASHING_BUSY");
        assert_eq!(
            str_field(&result, "message"),
            "flash 進行中，拒絕 setup（可用 --force 覆寫）"
        );
        assert!(
            !t.config_dir().join("profiles").exists(),
            "不得先物化 profiles"
        );
        assert!(
            !t.home().join(".local/bin/serialwrap-minicom").exists(),
            "不得先物化 wrapper"
        );
        assert_eq!(std::fs::read(t.config_path()).unwrap(), config_before);
        assert!(t.calls().is_empty(), "拒絕前不得執行監管命令");
        assert_eq!(t.rpc().request_count(&endpoint, "health.ping"), 1);
        assert_eq!(t.rpc().request_count(&endpoint, "mcu.status"), 1);
        assert!(t.clock().sleeps_s().is_empty());
    }
}

#[test]
fn s1m241_setup_restart_decision_order() {
    use sw_cli::setup_cmd::{
        daemon_code_assessment, select_daemon_restart_action, DaemonRestartAction,
    };
    use sw_pal::traits::SupervisionMode;

    assert_eq!(DaemonRestartAction::NotChecked.as_str(), None);
    assert_eq!(DaemonRestartAction::None.as_str(), Some("none"));
    assert_eq!(
        DaemonRestartAction::SkippedNoRestartFlag.as_str(),
        Some("skipped_no_restart_flag")
    );
    assert_eq!(
        DaemonRestartAction::ModeTransition.as_str(),
        Some("mode_transition")
    );
    assert_eq!(
        DaemonRestartAction::ManualRestart.as_str(),
        Some("manual_restart")
    );
    assert_eq!(
        DaemonRestartAction::SkippedFlashing.as_str(),
        Some("skipped_flashing")
    );
    assert_eq!(
        DaemonRestartAction::ServiceInactive.as_str(),
        Some("service_inactive")
    );
    assert_eq!(DaemonRestartAction::Restart.as_str(), Some("restart"));

    let stale = daemon_code_assessment(true, Some("daemon-old"), Some("installed-new"));

    let t = TestEnv::posix().with_command(
        &["systemctl", "--user", "is-active", "serialwrap"],
        0,
        "active\n",
        "",
    );
    t.with_cli_ctx(|ctx| {
        assert_eq!(
            select_daemon_restart_action(
                ctx,
                &SupervisionMode::SystemdUser,
                &SupervisionMode::SystemdSystem,
                &stale,
                true,
                true,
                true,
            ),
            DaemonRestartAction::SkippedNoRestartFlag
        );
    });
    assert!(t.calls().is_empty(), "--no-restart 必須先於其他判斷");

    let t = TestEnv::posix();
    t.with_cli_ctx(|ctx| {
        assert_eq!(
            select_daemon_restart_action(
                ctx,
                &SupervisionMode::SystemdUser,
                &SupervisionMode::OnDemand,
                &stale,
                false,
                true,
                true,
            ),
            DaemonRestartAction::ModeTransition
        );
    });
    assert!(t.calls().is_empty(), "模式轉換不應查詢服務狀態");

    let t = TestEnv::posix();
    t.with_cli_ctx(|ctx| {
        assert_eq!(
            select_daemon_restart_action(
                ctx,
                &SupervisionMode::OnDemand,
                &SupervisionMode::OnDemand,
                &stale,
                false,
                true,
                true,
            ),
            DaemonRestartAction::ManualRestart
        );
    });
    assert!(t.calls().is_empty(), "on-demand 不應查詢 systemd");

    let t = TestEnv::posix().with_command(
        &["systemctl", "--user", "is-active", "serialwrap"],
        0,
        "active\n",
        "",
    );
    t.with_cli_ctx(|ctx| {
        assert_eq!(
            select_daemon_restart_action(
                ctx,
                &SupervisionMode::SystemdUser,
                &SupervisionMode::SystemdUser,
                &stale,
                false,
                true,
                true,
            ),
            DaemonRestartAction::SkippedFlashing
        );
    });
    assert!(
        t.calls().is_empty(),
        "flash 中不得因 --force 查狀態或安排重啟"
    );

    let windows = TestEnv::windows();
    windows.with_cli_ctx(|ctx| {
        assert_eq!(
            select_daemon_restart_action(
                ctx,
                &SupervisionMode::WindowsTask,
                &SupervisionMode::WindowsTask,
                &stale,
                false,
                false,
                false,
            ),
            DaemonRestartAction::ServiceInactive
        );
    });
    assert!(windows.calls().is_empty(), "Windows 模式不得擴張成自動重啟");

    let inactive = TestEnv::posix().with_command(
        &["systemctl", "--user", "is-active", "serialwrap"],
        3,
        "active\n",
        "",
    );
    inactive.with_cli_ctx(|ctx| {
        assert_eq!(
            select_daemon_restart_action(
                ctx,
                &SupervisionMode::SystemdUser,
                &SupervisionMode::SystemdUser,
                &stale,
                false,
                false,
                false,
            ),
            DaemonRestartAction::ServiceInactive
        );
    });
    assert_eq!(
        inactive.calls(),
        vec![vec![
            "systemctl".to_string(),
            "--user".to_string(),
            "is-active".to_string(),
            "serialwrap".to_string()
        ]]
    );

    let active = TestEnv::posix().with_command(
        &["systemctl", "--user", "is-active", "serialwrap"],
        0,
        "active\n",
        "",
    );
    active.with_cli_ctx(|ctx| {
        assert_eq!(
            select_daemon_restart_action(
                ctx,
                &SupervisionMode::SystemdUser,
                &SupervisionMode::SystemdUser,
                &stale,
                false,
                false,
                false,
            ),
            DaemonRestartAction::Restart
        );
    });
    assert_eq!(active.calls().len(), 1, "符合條件時只查一次 is-active");

    let matching = daemon_code_assessment(true, Some("same"), Some("same"));
    let same = TestEnv::posix();
    same.with_cli_ctx(|ctx| {
        assert_eq!(
            select_daemon_restart_action(
                ctx,
                &SupervisionMode::SystemdUser,
                &SupervisionMode::SystemdUser,
                &matching,
                false,
                false,
                false,
            ),
            DaemonRestartAction::None
        );
    });
    assert!(same.calls().is_empty(), "指紋相同時不得查詢 is-active");

    let unchecked = daemon_code_assessment(false, None, Some("installed-new"));
    let unreachable = TestEnv::posix();
    unreachable.with_cli_ctx(|ctx| {
        assert_eq!(
            select_daemon_restart_action(
                ctx,
                &SupervisionMode::SystemdUser,
                &SupervisionMode::SystemdUser,
                &unchecked,
                false,
                false,
                false,
            ),
            DaemonRestartAction::NotChecked
        );
    });
    assert!(unreachable.calls().is_empty());
}

#[test]
fn s1m241_setup_active_requires_exact_active() {
    use sw_cli::setup_cmd::service_is_active;
    use sw_pal::traits::SupervisionMode;

    let active_user = TestEnv::posix().with_command(
        &["systemctl", "--user", "is-active", "serialwrap"],
        0,
        " \t\u{001c}active\u{001f}\r\n",
        "",
    );
    active_user.with_cli_ctx(|ctx| {
        assert!(service_is_active(ctx, &SupervisionMode::SystemdUser));
    });
    assert_eq!(active_user.calls().len(), 1);

    let active_system =
        TestEnv::posix().with_command(&["systemctl", "is-active", "serialwrap"], 0, "active\n", "");
    active_system.with_cli_ctx(|ctx| {
        assert!(service_is_active(ctx, &SupervisionMode::SystemdSystem));
    });
    assert_eq!(
        active_system.calls(),
        vec![vec![
            "systemctl".to_string(),
            "is-active".to_string(),
            "serialwrap".to_string()
        ]]
    );

    for (rc, stdout) in [(1, "active\n"), (0, "activating\n"), (0, "Active\n")] {
        let t = TestEnv::posix().with_command(
            &["systemctl", "--user", "is-active", "serialwrap"],
            rc,
            stdout,
            "",
        );
        t.with_cli_ctx(|ctx| {
            assert!(!service_is_active(ctx, &SupervisionMode::SystemdUser));
        });
        assert_eq!(t.calls().len(), 1);
    }

    let non_systemd = TestEnv::posix();
    non_systemd.with_cli_ctx(|ctx| {
        for mode in [
            SupervisionMode::OnDemand,
            SupervisionMode::WindowsTask,
            SupervisionMode::WindowsService,
            SupervisionMode::Other("custom".to_string()),
        ] {
            assert!(!service_is_active(ctx, &mode));
        }
    });
    assert!(
        non_systemd.calls().is_empty(),
        "非 systemd 模式不得執行 systemctl"
    );
}

#[test]
fn on_demand_to_systemd_user_stops_waits_then_starts() {
    let t = TestEnv::posix()
        .with_real_service()
        .with_systemd(true)
        .with_var("USER", "alice");
    let ep = t.default_socket();
    // setup 會先 ping、查 flash、讀 health.status 並送 daemon.stop，等待用的失敗 ping 是第 5 次連線。
    let t = t
        .with_daemon_alive_then_gone(&ep, 4321, /*connects_until_gone*/ 5)
        .with_gone_ping_event();
    let (rc, out, _) = t.run(&["setup", "--user"]);
    assert_eq!(rc, 0, "{out}");
    let setup = dict_field(&json(&out), "setup");
    assert_eq!(str_field(&setup, "mode"), "systemd-user");
    assert_eq!(
        argv_list_field(&setup, "ran"),
        vec![
            vec!["serialwrap", "daemon", "stop"],
            vec!["systemctl", "--user", "daemon-reload"],
            vec!["systemctl", "--user", "enable", "serialwrap"],
            vec!["systemctl", "--user", "start", "serialwrap"],
            vec!["loginctl", "enable-linger", "alice"]
        ]
    );
    let order = t.event_order();
    assert!(order.index_of("rpc:daemon.stop") < order.index_of("rpc:health.ping#gone"));
    assert!(
        order.index_of("rpc:health.ping#gone")
            < order.index_of("run:systemctl --user start serialwrap")
    );
    assert_eq!(
        std::fs::read_to_string(t.home().join(".config/systemd/user/serialwrap.service")).unwrap(),
        sw_cli::setup_cmd::render_user_unit("%h/.local/bin/serialwrapd")
    );
}

#[test]
fn reconcile_waits_on_status_pid_not_unrelated_socketless_daemon() {
    // SC-12 回歸（P4 gate）：health.status 回報 pid 時只等該 pid 結束（Python `daemon_process_alive(pid)`）；
    // 同機另一個未帶 --socket 的 serialwrapd（socket 不明，例如宿主的正式 daemon）不得被當成舊 daemon 而等到逾時。
    let t = TestEnv::posix()
        .with_real_service()
        .with_systemd(true)
        .with_var("USER", "alice");
    let ep = t.default_socket();
    let t = t
        .with_daemon_alive_then_gone(&ep, 4321, /*connects_until_gone*/ 5)
        .with_procs(vec![DaemonProc {
            pid: 551,
            socket: None,
            tree: vec![ProcIdentity { pid: 551, start_ticks: None }],
        }]);
    let (rc, out, _) = t.run(&["setup", "--user"]);
    assert_eq!(rc, 0, "{out}");
    let setup = dict_field(&json(&out), "setup");
    assert_eq!(str_field(&setup, "mode"), "systemd-user");
    let start: Vec<String> = ["systemctl", "--user", "start", "serialwrap"].iter().map(|s| s.to_string()).collect();
    assert!(argv_list_field(&setup, "ran").contains(&start), "{out}");
}

#[test]
fn reconcile_aborts_when_old_daemon_never_exits() {
    let t = TestEnv::posix().with_real_service().with_systemd(true);
    let ep = t.default_socket();
    let t = t.with_daemon_alive_forever(&ep, 4321);
    let (rc, out, _) = t.run(&["setup", "--user"]);
    assert_eq!(rc, 1);
    let j = json(&out);
    let r = if match &j {
        PyValue::Dict(d) => d.get("error_code").is_some(),
        _ => false,
    } {
        j.clone()
    } else {
        dict_field(&j, "setup").clone()
    };
    assert_eq!(
        (
            str_field(&r, "error_code").as_str(),
            str_field(&r, "phase").as_str()
        ),
        ("TIMEOUT", "reconcile_wait_old_exit")
    );
    assert_eq!(dict_field(&r, "ok"), PyValue::Bool(false));
    assert_eq!(str_field(&r, "mode"), "on-demand");
    assert_eq!(str_field(&r, "requested_mode"), "systemd-user");
    assert_eq!(dict_field(&r, "transitioned"), PyValue::Bool(false));
    assert_eq!(dict_field(&r, "applied"), PyValue::Bool(false));
    assert_eq!(
        argv_list_field(&r, "ran"),
        vec![vec!["serialwrap", "daemon", "stop"]]
    );
    assert!(argv_list_field(&r, "pending_sudo").is_empty());
    assert!(!t
        .calls()
        .iter()
        .any(|c| c == &["systemctl", "--user", "start", "serialwrap"]));
    assert!(
        !t.config_path().exists()
            || !std::fs::read_to_string(t.config_path())
                .unwrap()
                .contains("systemd-user")
    );
}

#[test]
fn wsl_without_sudo_never_runs_sudo_install() {
    let t = TestEnv::posix()
        .with_real_service()
        .with_wsl(true)
        .with_systemd(false);
    let (rc, out, _) = t.run(&["setup", "--on-demand"]);
    assert_eq!(rc, 0);
    assert!(!t
        .calls()
        .iter()
        .any(|c| c.first().map(String::as_str) == Some("sudo")));
    assert!(!match &json(&out) {
        PyValue::Dict(d) => d.get("needs_restart").is_some(),
        _ => false,
    });
    let setup = dict_field(&json(&out), "setup");
    let pending = argv_list_field(&setup, "pending_sudo");
    assert_eq!(pending.len(), 1);
    assert_eq!(pending[0][0], "sudo");
    assert_eq!(pending[0][1], "install");
}

#[test]
fn materialize_profiles_not_overwritten_without_force() {
    let t = TestEnv::posix().with_real_service();
    let env_snap = sw_proto::runtime_paths::EnvSnapshot::from_pairs(vec![]);
    let dirs = sw_cli::setup_cmd::user_dirs(&env_snap, &t.home());
    let default_yaml = dirs.config.join("profiles").join("default.yaml");
    std::fs::create_dir_all(default_yaml.parent().unwrap()).unwrap();
    std::fs::write(&default_yaml, b"x").unwrap();

    t.with_cli_ctx(|ctx| {
        sw_cli::setup_cmd::materialize_assets(ctx, &dirs, false).unwrap();
    });
    assert_eq!(std::fs::read(&default_yaml).unwrap(), b"x");

    t.with_cli_ctx(|ctx| {
        sw_cli::setup_cmd::materialize_assets(ctx, &dirs, true).unwrap();
    });
    let asset = sw_cli::assets::PROFILE_ASSETS
        .iter()
        .find(|a| a.rel == "profiles/default.yaml")
        .expect("profiles/default.yaml asset missing");
    assert_eq!(std::fs::read(&default_yaml).unwrap(), asset.bytes);
}

#[test]
fn materialize_skill_always_refreshed_and_linked() {
    let t = TestEnv::posix().with_real_service();
    let env_snap = sw_proto::runtime_paths::EnvSnapshot::from_pairs(vec![]);
    let dirs = sw_cli::setup_cmd::user_dirs(&env_snap, &t.home());
    t.with_cli_ctx(|ctx| {
        sw_cli::setup_cmd::materialize_assets(ctx, &dirs, false).unwrap();
    });
    let skill_path = dirs.data.join("skill").join("SKILL.md");
    let asset = sw_cli::assets::SKILL_ASSETS
        .iter()
        .find(|a| a.rel == "skill/SKILL.md")
        .expect("SKILL.md asset missing");
    assert_eq!(
        sha256_hex(&std::fs::read_to_string(&skill_path).unwrap()),
        sha256_hex(std::str::from_utf8(asset.bytes).unwrap())
    );
    assert_eq!(
        std::fs::read_link(&dirs.agents_skill_link).unwrap(),
        dirs.data.join("skill")
    );
}

#[test]
fn materialize_removes_legacy_mcp_link_only_when_matching() {
    let t = TestEnv::posix().with_real_service();
    let env_snap = sw_proto::runtime_paths::EnvSnapshot::from_pairs(vec![]);
    let dirs = sw_cli::setup_cmd::user_dirs(&env_snap, &t.home());
    let mcp_link = dirs.legacy_agents_skill_link.clone();
    std::fs::create_dir_all(mcp_link.parent().unwrap()).unwrap();
    let target_mcp = t
        .home()
        .join(".local/share/serialwrap/custom-skills/serialwrap-mcp");
    std::fs::create_dir_all(&target_mcp).unwrap();
    // 以 PAL 測試替身建立目錄 symlink（不寫平台 cfg；Windows 無權限時退為複製，DF-16）。
    let mcp_link_kind = sw_pal::fakes::real_fs()
        .symlink_or_copy_dir(&mcp_link, &target_mcp)
        .unwrap();

    let other_link = mcp_link.parent().unwrap().join("other-skill");
    let other_target = t.home().join("other");
    std::fs::create_dir_all(&other_target).unwrap();
    let _other_link_kind = sw_pal::fakes::real_fs()
        .symlink_or_copy_dir(&other_link, &other_target)
        .unwrap();

    t.with_cli_ctx(|ctx| {
        sw_cli::setup_cmd::materialize_assets(ctx, &dirs, false).unwrap();
    });
    match mcp_link_kind {
        sw_pal::fs_ops::LinkKind::Symlink => assert!(
            !mcp_link.exists() && std::fs::symlink_metadata(&mcp_link).is_err(),
            "legacy mcp symlink should be removed"
        ),
        sw_pal::fs_ops::LinkKind::Copied => assert!(
            mcp_link.is_dir(),
            "legacy mcp copy should be preserved because it is not a symlink"
        ),
    }
    assert!(other_link.exists(), "unrelated link should be preserved");
}

#[test]
fn materialize_wrappers_posix_only() {
    let t_posix = TestEnv::posix().with_real_service();
    let env_snap = sw_proto::runtime_paths::EnvSnapshot::from_pairs(vec![]);
    let dirs_posix = sw_cli::setup_cmd::user_dirs(&env_snap, &t_posix.home());
    t_posix.with_cli_ctx(|ctx| {
        sw_cli::setup_cmd::materialize_assets(ctx, &dirs_posix, false).unwrap();
    });
    for &(rel, name) in sw_cli::assets::WRAPPER_MAP {
        let path = dirs_posix.bin.join(name);
        assert!(path.exists(), "wrapper {name} should exist in bin");
        // 平台有 POSIX 權限位時才斷言 0o755（`file_mode` 於 Windows 回 None）。
        if let Some(mode) = sw_pal::fakes::file_mode(&path) {
            assert_eq!(mode & 0o777, 0o755, "wrapper {name} mode");
        }
        let asset = sw_cli::assets::TOOL_ASSETS
            .iter()
            .find(|a| a.rel == format!("tools/{rel}"))
            .unwrap();
        assert_eq!(std::fs::read(&path).unwrap(), asset.bytes);
    }

    let t_win = TestEnv::windows().with_real_service();
    let dirs_win = sw_cli::setup_cmd::user_dirs(&env_snap, &t_win.home());
    t_win.with_cli_ctx(|ctx| {
        sw_cli::setup_cmd::materialize_assets(ctx, &dirs_win, false).unwrap();
    });
    assert!(
        !dirs_win.bin.exists(),
        "bin should not be created on windows"
    );
}

#[test]
fn assets_embedded_sha256_match_repo() {
    let repo_assets = common::data_root().join("sw_core/assets");
    let all: Vec<&[sw_cli::assets::Asset]> = vec![
        sw_cli::assets::PROFILE_ASSETS,
        sw_cli::assets::SKILL_ASSETS,
        sw_cli::assets::TOOL_ASSETS,
    ];
    let mut embedded = BTreeSet::new();
    for set in all {
        for asset in set {
            assert!(
                embedded.insert(asset.rel.to_string()),
                "重複嵌入資產 {}",
                asset.rel
            );
            let disk_file = repo_assets.join(asset.rel);
            let disk_bytes = std::fs::read(&disk_file)
                .unwrap_or_else(|e| panic!("failed to read disk asset {disk_file:?}: {e}"));
            assert_eq!(
                sha256_hex(asset.bytes),
                sha256_hex(&disk_bytes),
                "sha256 mismatch for {}",
                asset.rel
            );
        }
    }
    fn collect_files(root: &std::path::Path, dir: &std::path::Path, files: &mut BTreeSet<String>) {
        for entry in std::fs::read_dir(dir).unwrap() {
            let path = entry.unwrap().path();
            if path.is_dir() {
                collect_files(root, &path, files);
            } else {
                files.insert(
                    path.strip_prefix(root)
                        .unwrap()
                        .to_string_lossy()
                        .replace('\\', "/"),
                );
            }
        }
    }
    let mut on_disk = BTreeSet::new();
    for directory in ["profiles", "skill", "tools"] {
        collect_files(&repo_assets, &repo_assets.join(directory), &mut on_disk);
    }
    assert_eq!(embedded, on_disk, "內嵌資產清單須涵蓋來源目錄全部檔案");
}

#[test]
fn legacy_detect_shape() {
    let t = TestEnv::posix().with_real_service();
    let legacy_dir = t.home().join(".paul_tools/serialwrap");
    std::fs::create_dir_all(&legacy_dir).unwrap();
    let detected = t.with_cli_ctx(|ctx| sw_cli::setup_cmd::detect_legacy_install(ctx, &t.home()));
    let d = detected.expect("legacy install should be detected");
    for k in [
        "hint",
        "minicom_symlink",
        "path",
        "serialwrap",
        "state_json",
    ] {
        assert!(d.get(k).is_some(), "missing key: {k}");
    }
    assert_eq!(
        str_field(&PyValue::Dict(d), "hint"),
        "偵測到舊版 ~/.paul_tools 安裝。建議退役步驟：1) 停掉舊 daemon（若仍在跑）；2) 移除 shadow 二進位 ~/.paul_tools/serialwrap 與 minicom 符號連結；3) 從 shell rc 移除 ~/.paul_tools 的 PATH export。新版改以 pipx 安裝（~/.local/bin）並以 XDG 路徑運作，不需 ~/.paul_tools。本指令不會自動刪除任何檔案。"
    );
}

#[test]
fn setup_bad_config_yaml_reports_internal_error_before_side_effects() {
    let t = TestEnv::posix()
        .with_real_service()
        .with_config("supervision_mode: [broken\n");
    let (rc, out, _) = t.run(&["setup", "--on-demand"]);
    assert_eq!(rc, 1);
    assert_eq!(str_field(&json(&out), "error_code"), "INTERNAL_ERROR");
    assert!(t.rpc().requests().is_empty());
    assert!(t.calls().is_empty());
    assert!(!t.home().join(".config/profiles").exists());
}

#[test]
fn setup_rollback_stub_is_rejected_before_side_effects() {
    let t = TestEnv::posix().with_real_service();
    let (rc, out, _) = t.run(&["setup", "--rollback"]);
    assert_eq!(rc, 2);
    assert_eq!(str_field(&json(&out), "error_code"), "INVALID_ARGS");
    assert!(t.rpc().requests().is_empty());
    assert!(t.calls().is_empty());
    assert!(!t.config_dir().join("profiles").exists());
    assert!(!t.home().join(".local/share/serialwrap").exists());
    assert!(!t
        .home()
        .join(".config/systemd/user/serialwrap.service")
        .exists());
}

#[test]
fn setup_waits_for_matching_process_when_on_demand_ping_is_down() {
    let base = TestEnv::posix()
        .with_real_service()
        .with_systemd(true)
        .with_config("supervision_mode: on-demand\n");
    let ep = base.default_socket();
    let before = base.config_bytes();
    let t = base
        .with_rpc_refused(&ep)
        .with_procs(vec![sw_pal::proc_scan::DaemonProc {
            pid: 9100,
            socket: Some(ep.clone()),
            tree: Vec::new(),
        }]);
    let (rc, out, _) = t.run(&["setup", "--user"]);
    assert_eq!(rc, 1);
    let result = json(&out);
    assert_eq!(str_field(&result, "error_code"), "TIMEOUT");
    assert_eq!(str_field(&result, "phase"), "reconcile_wait_old_exit");
    assert_eq!(str_field(&result, "requested_mode"), "systemd-user");
    assert!(t
        .calls()
        .iter()
        .all(|argv| !argv.iter().any(|part| part == "start")));
    assert_eq!(t.config_bytes(), before);
    assert!(t.clock().elapsed() >= Duration::from_secs(10));
}

#[test]
fn setup_fails_closed_when_old_daemon_scan_fails() {
    let base = TestEnv::posix()
        .with_real_service()
        .with_systemd(true)
        .with_config("supervision_mode: on-demand\n");
    let ep = base.default_socket();
    let before = base.config_bytes();
    let t = base.with_rpc_refused(&ep).with_procs(Vec::new());
    t.procs().fail();
    let (rc, out, _) = t.run(&["setup", "--user"]);
    assert_eq!(rc, 1);
    assert_eq!(str_field(&json(&out), "error_code"), "TIMEOUT");
    assert_eq!(str_field(&json(&out), "phase"), "reconcile_wait_old_exit");
    assert!(t
        .calls()
        .iter()
        .all(|argv| !argv.iter().any(|part| part == "start")));
    assert_eq!(t.config_bytes(), before);
}

#[test]
fn reconcile_timeout_keeps_wsl_pending_sudo_commands() {
    let t = TestEnv::posix()
        .with_real_service()
        .with_wsl(true)
        .with_systemd(false)
        .with_config("supervision_mode: on-demand\n");
    let ep = t.default_socket();
    let t = t.with_daemon_alive_forever(&ep, 4321);
    let (rc, out, _) = t.run(&["setup", "--user"]);
    assert_eq!(rc, 1);
    let result = json(&out);
    assert_eq!(str_field(&result, "error_code"), "TIMEOUT");
    let pending = argv_list_field(&result, "pending_sudo");
    assert!(pending.iter().any(|argv| {
        argv.first().is_some_and(|part| part == "sudo")
            && argv.get(1).is_some_and(|part| part == "install")
            && argv.last().is_some_and(|part| part == "/etc/wsl.conf")
    }));
    assert!(t
        .calls()
        .iter()
        .all(|argv| !argv.iter().any(|part| part == "start")));
}

#[test]
fn wslconf_merge_matches_configparser_golden() {
    let golden_path = common::data_root().join("crates/sw-cli/tests/golden/wslconf.ndjson");
    let text = std::fs::read_to_string(&golden_path)
        .unwrap_or_else(|e| panic!("failed to read golden file: {e}"));
    let mut count = 0;
    for line in text.lines() {
        if line.trim().is_empty() {
            continue;
        }
        count += 1;
        let v = json(line);
        let c = match &v {
            PyValue::Dict(d) => match d.get("case") {
                Some(PyValue::Str(s)) => s.clone(),
                _ => "".into(),
            },
            _ => "".into(),
        };
        let input = match &v {
            PyValue::Dict(d) => match d.get("input") {
                Some(PyValue::Str(s)) => s.clone(),
                _ => "".into(),
            },
            _ => "".into(),
        };
        let expected = match &v {
            PyValue::Dict(d) => match d.get("expected") {
                Some(PyValue::Str(s)) => s.clone(),
                _ => "".into(),
            },
            _ => "".into(),
        };
        let actual = sw_cli::wslconf::merge_wsl_conf_systemd(&input);
        assert_eq!(actual, expected, "case: {c}");
    }
    assert_eq!(count, 17, "expected 17 golden cases");
}

#[test]
fn wsl_systemd_target_with_sudo_installs() {
    let t = TestEnv::posix()
        .with_real_service()
        .with_wsl(true)
        .with_systemd(false);
    let (rc, out, _) = t.run(&["setup", "--user", "--with-sudo"]);
    assert_eq!(rc, 0);
    assert!(t.calls().iter().any(|c| c.len() == 6
        && c[0] == "sudo"
        && c[1] == "install"
        && c[2] == "-m"
        && c[3] == "0644"
        && c[5] == "/etc/wsl.conf"));
    let j = json(&out);
    assert_eq!(
        match &j {
            PyValue::Dict(d) => d.get("needs_restart"),
            _ => None,
        },
        Some(&PyValue::Bool(true))
    );
}

#[test]
fn flashing_busy_before_materialize() {
    let t = TestEnv::posix().with_real_service();
    let ep = t.default_socket();
    let t = t.with_rpc_ok(&ep, "health.ping", OK_PING).with_rpc_ok(
        &ep,
        "mcu.status",
        r#"{"ok":true,"flashing":true}"#,
    );
    let (rc, out, _) = t.run(&["setup", "--user"]);
    assert_eq!(rc, 2);
    let j = json(&out);
    assert_eq!(str_field(&j, "error_code"), "FLASHING_BUSY");
    let profiles_dir = t.config_dir().join("profiles");
    assert!(
        !profiles_dir.exists(),
        "profiles directory should not exist"
    );
}

#[test]
fn same_mode_systemd_user_enables_without_start() {
    let t = TestEnv::posix()
        .with_real_service()
        .with_systemd(true)
        .with_config("supervision_mode: systemd-user\n");
    let (rc, out, _) = t.run(&["setup", "--user"]);
    assert_eq!(rc, 0);
    let setup = dict_field(&json(&out), "setup");
    let ran = argv_list_field(&setup, "ran");
    assert!(!ran.iter().any(|cmd| cmd.contains(&"start".to_string())));
}

#[test]
fn system_without_sudo_pending_only() {
    let t = TestEnv::posix().with_real_service().with_systemd(true);
    let (rc, out, _) = t.run(&["setup", "--system"]);
    assert_eq!(rc, 0);
    let setup = dict_field(&json(&out), "setup");
    assert_eq!(
        match &setup {
            PyValue::Dict(d) => d.get("applied"),
            _ => None,
        },
        Some(&PyValue::Bool(false))
    );
    assert_eq!(str_field(&setup, "mode"), "on-demand");
    let pending = argv_list_field(&setup, "pending_sudo");
    assert_eq!(pending.len(), 6);
    assert!(pending.iter().any(|cmd| cmd.contains(&"start".to_string())));
    assert!(
        !t.config_path().exists()
            || !std::fs::read_to_string(t.config_path())
                .unwrap()
                .contains("systemd-system")
    );
}

#[test]
fn system_with_sudo_unit_content() {
    let t = TestEnv::posix()
        .with_real_service()
        .with_systemd(true)
        .with_var("USER", "caller")
        .with_which("serialwrapd", "/decoy/bin/serialwrapd");
    let (rc, _, _) = t.run(&["setup", "--system", "--with-sudo"]);
    assert_eq!(rc, 0);
    let install_root = t.install_root();
    let expected_exec = format!(
        "{}/bin/serialwrapd --socket /run/serialwrap/serialwrapd.sock --profile-dir /etc/serialwrap/profiles",
        install_root.display()
    );
    let expected_unit = sw_cli::setup_cmd::render_system_unit(&expected_exec, "caller");
    let staged_unit = t.home().join(".local/share/serialwrap/serialwrap.service");
    assert_eq!(std::fs::read_to_string(staged_unit).unwrap(), expected_unit);
    assert!(!expected_unit.contains("SERIALWRAP_STATE_DIR"));
}

#[test]
fn out_of_system_without_sudo_blocks() {
    let t = TestEnv::posix()
        .with_real_service()
        .with_systemd(true)
        .with_config("supervision_mode: systemd-system\n");
    let (rc, out, _) = t.run(&["setup", "--user"]);
    assert_eq!(rc, 0);
    let setup = dict_field(&json(&out), "setup");
    assert_eq!(
        match &setup {
            PyValue::Dict(d) => d.get("applied"),
            _ => None,
        },
        Some(&PyValue::Bool(false))
    );
    assert!(!t
        .calls()
        .iter()
        .any(|c| c.contains(&"stop".to_string()) || c.contains(&"disable".to_string())));
    let pending = argv_list_field(&setup, "pending_sudo");
    assert!(pending
        .iter()
        .any(|cmd| cmd.contains(&"stop".to_string()) || cmd.contains(&"disable".to_string())));
}

#[test]
fn df10_system_state_migration_target() {
    let base = TestEnv::posix();
    let run_user_home = base.home().parent().unwrap().join("run-user-home");
    std::fs::create_dir_all(&run_user_home).unwrap();
    let t = base
        .with_real_service()
        .with_systemd(true)
        .with_var("USER", "caller")
        .with_var("SUDO_USER", "tester")
        .with_var("HOME_tester", &run_user_home.display().to_string())
        .with_config("supervision_mode: systemd-user\n");
    let user_state = t.state_dir().join("state.json");
    std::fs::write(&user_state, b"legacy-state").unwrap();
    let dest_state = run_user_home.join(".local/state/serialwrap/state.json");
    let (rc, _, _) = t.run(&["setup", "--system", "--with-sudo"]);
    assert_eq!(rc, 0);
    assert_eq!(std::fs::read(dest_state).unwrap(), b"legacy-state");
    let expected_exec = format!(
        "{}/bin/serialwrapd --socket /run/serialwrap/serialwrapd.sock --profile-dir /etc/serialwrap/profiles",
        t.install_root().display()
    );
    let expected_unit = sw_cli::setup_cmd::render_system_unit(&expected_exec, "tester");
    assert_eq!(
        std::fs::read_to_string(t.home().join(".local/share/serialwrap/serialwrap.service"))
            .unwrap(),
        expected_unit
    );
}

#[test]
fn setup_continues_after_linger_command_failure() {
    let t = TestEnv::posix()
        .with_real_service()
        .with_systemd(true)
        .with_var("USER", "alice")
        .with_command(&["loginctl", "enable-linger", "alice"], 1, "", "denied");
    let (rc, out, _) = t.run(&["setup", "--user"]);
    assert_eq!(rc, 0, "{out}");
    assert!(std::fs::read_to_string(t.config_path())
        .unwrap()
        .contains("supervision_mode: systemd-user"));
    let setup = dict_field(&json(&out), "setup");
    assert!(argv_list_field(&setup, "ran")
        .iter()
        .any(|argv| { argv == &["loginctl", "enable-linger", "alice"] }));
}

#[test]
fn preserve_remote_tcp_socket_on_demand() {
    let t = TestEnv::posix()
        .with_real_service()
        .with_config("supervision_mode: on-demand\nsocket_path: tcp://127.0.0.1:49001\n");
    let (rc, _, _) = t.run(&["setup", "--on-demand"]);
    assert_eq!(rc, 0);
    let cfg_text = std::fs::read_to_string(t.config_path()).unwrap();
    assert!(cfg_text.contains("tcp://127.0.0.1:49001"));
}

#[test]
fn windows_default_target_is_windows_task() {
    let t = TestEnv::windows().with_real_service();
    let (rc, out, _) = t.run(&["setup"]);
    assert_eq!(rc, 0);
    let setup = dict_field(&json(&out), "setup");
    let ran = argv_list_field(&setup, "ran");
    assert!(ran.len() >= 2);
    assert_eq!(ran[0][0], "schtasks");
    assert_eq!(ran[0][1], "/Create");
    assert_eq!(ran[0][2], "/XML");
    assert_eq!(ran[1], vec!["schtasks", "/Run", "/TN", "serialwrap"]);
    let xml_path = &ran[0][3];
    let xml_bytes = std::fs::read(xml_path).unwrap();
    assert!(
        xml_bytes.starts_with(&[0xff, 0xfe]),
        "Task Scheduler XML 應為 UTF-16LE BOM"
    );
    let xml_units = xml_bytes[2..]
        .chunks_exact(2)
        .map(|bytes| u16::from_le_bytes([bytes[0], bytes[1]]))
        .collect::<Vec<_>>();
    let xml_content = String::from_utf16(&xml_units).unwrap();
    let install_root = t.install_root();
    assert!(xml_content.contains(&format!(
        r#"<Command>{}\bin\serialwrapd.exe</Command>"#,
        install_root.display()
    )));
    assert!(xml_content.contains(r#"<Arguments>--socket tcp://127.0.0.1:48700</Arguments>"#));
}

#[test]
fn windows_task_to_on_demand_graceful_stop_order() {
    let t = TestEnv::windows()
        .with_real_service()
        .with_config("supervision_mode: windows-task\n");
    let ep = TestEnv::win_endpoint();
    let t = t.with_idle_daemon(ep, 1234).with_gone_ping_event();
    let (rc, out, _) = t.run(&["setup", "--on-demand"]);
    assert_eq!(rc, 0);
    let order = t.event_order();
    let change_idx = order.index_of("run:schtasks /Change /TN serialwrap /DISABLE");
    let stop_idx = order.index_of("rpc:daemon.stop");
    let gone_idx = order.index_of("rpc:health.ping#gone");
    assert!(change_idx < stop_idx);
    assert!(stop_idx < gone_idx);
    let setup = dict_field(&json(&out), "setup");
    let ran = argv_list_field(&setup, "ran");
    assert!(!ran.iter().any(|cmd| cmd.contains(&"/End".to_string())));
}

#[test]
fn windows_task_to_on_demand_end_only_after_timeout() {
    let t = TestEnv::windows()
        .with_real_service()
        .with_config("supervision_mode: windows-task\n");
    let ep = TestEnv::win_endpoint();
    let t = t.with_daemon_alive_forever(ep, 1234);
    let (rc, out, _) = t.run(&["setup", "--on-demand"]);
    assert_eq!(rc, 1);
    let j = json(&out);
    assert_eq!(str_field(&j, "error_code"), "TIMEOUT");
    assert_eq!(str_field(&j, "phase"), "reconcile_wait_old_exit");
    let ran = argv_list_field(&j, "ran");
    let disable = ran
        .iter()
        .position(|argv| argv == &["schtasks", "/Change", "/TN", "serialwrap", "/DISABLE"])
        .unwrap();
    let stop = ran
        .iter()
        .position(|argv| argv == &["serialwrap", "daemon", "stop"])
        .unwrap();
    let end = ran
        .iter()
        .position(|argv| argv == &["schtasks", "/End", "/TN", "serialwrap"])
        .unwrap();
    assert!(disable < stop && stop < end);
    assert!(t.clock().elapsed() >= Duration::from_secs(15));
    let order = t.event_order();
    assert!(order.index_of("rpc:daemon.stop") < order.index_of("run:schtasks /End /TN serialwrap"));
    let cfg_text = std::fs::read_to_string(t.config_path()).unwrap();
    assert!(!cfg_text.contains("on-demand"));
}

#[test]
fn windows_service_target_pending_only() {
    let t = TestEnv::windows().with_real_service();
    let (rc, out, _) = t.run(&["setup", "--windows-service"]);
    assert_eq!(rc, 0);
    let setup = dict_field(&json(&out), "setup");
    assert_eq!(
        match &setup {
            PyValue::Dict(d) => d.get("applied"),
            _ => None,
        },
        Some(&PyValue::Bool(false))
    );
    let pending = argv_list_field(&setup, "pending_sudo");
    assert_eq!(pending.len(), 3);
    let warnings = match dict_field(&setup, "warnings") {
        PyValue::List(l) => l
            .iter()
            .map(|w| match w {
                PyValue::Str(s) => s.clone(),
                _ => "".into(),
            })
            .collect::<Vec<_>>(),
        _ => vec![],
    };
    assert_eq!(
        warnings,
        vec!["windows-service 依 OD-12 須完成多使用者認證／授權後才可部署；本版只列出 pending_sudo 指令，不自動安裝"]
    );
}

#[test]
fn user_flag_on_windows_invalid_args() {
    let t = TestEnv::windows().with_real_service();
    let (rc, out, _) = t.run(&["setup", "--user"]);
    assert_eq!(rc, 2);
    let j = json(&out);
    assert_eq!(str_field(&j, "error_code"), "INVALID_ARGS");
    assert_eq!(
        str_field(&j, "message"),
        "監管模式 systemd-user 不適用於本平台（可用：on-demand、windows-task、windows-service）"
    );
}

#[test]
fn supervision_set_invalid_mode_no_probe() {
    let t = TestEnv::posix()
        .with_real_service()
        .with_config("supervision_mode: on-demand\n");
    let cfg_before = t.config_bytes();
    let (rc, out, _) = t.run(&["supervision-mode", "set", "bogus", "--force"]);
    assert_eq!(rc, 2);
    let j = json(&out);
    assert_eq!(str_field(&j, "error_code"), "INVALID_ARGS");
    assert!(t.rpc().requests().is_empty());
    assert!(t.calls().is_empty());
    assert_eq!(t.config_bytes(), cfg_before);

    let t_win = TestEnv::windows()
        .with_real_service()
        .with_config("supervision_mode: on-demand\n");
    let cfg_win_before = t_win.config_bytes();
    let (rc_win, out_win, _) = t_win.run(&["supervision-mode", "set", "systemd-user"]);
    assert_eq!(rc_win, 2);
    let j_win = json(&out_win);
    assert_eq!(str_field(&j_win, "error_code"), "INVALID_ARGS");
    assert!(t_win.rpc().requests().is_empty());
    assert!(t_win.calls().is_empty());
    assert_eq!(t_win.config_bytes(), cfg_win_before);
}

#[test]
fn reconcile_handed_off_never_sends_daemon_stop() {
    for old_mode in [
        sw_pal::traits::SupervisionMode::OnDemand,
        sw_pal::traits::SupervisionMode::WindowsTask,
        sw_pal::traits::SupervisionMode::SystemdUser,
    ] {
        let t = TestEnv::posix().with_real_service().with_systemd(true);
        let ep = t.default_socket();
        let token = "0".repeat(32);
        let req = sw_cli::setup_cmd::ReconcileRequest {
            old_mode: old_mode.clone(),
            target_mode: sw_pal::traits::SupervisionMode::OnDemand,
            home: t.home(),
            daemon_running: true,
            daemon_id: sw_cli::graceful_stop::DaemonIdentity::default(),
            old_endpoint: ep.clone(),
            any_flashing: false,
            with_sudo: false,
            force: false,
            hold_target: false,
            socket_path: None,
            config_path: t.config_path(),
            old_stop: sw_cli::setup_cmd::OldStop::HandedOff {
                token,
                ran: vec![vec![
                    "serialwrap".into(),
                    "daemon".into(),
                    "handoff_stop".into(),
                ]],
            },
            restart_daemon: false,
            preserve_config_format: false,
        };
        t.with_cli_ctx(|ctx| {
            let res = sw_cli::setup_cmd::reconcile(ctx, &req).unwrap();
            let reqs = t.rpc().requests();
            assert!(!reqs
                .iter()
                .any(|(_, m, _)| m == "daemon.stop" || m == "daemon.handoff_stop"));
            assert!(!t
                .calls()
                .iter()
                .any(|cmd| cmd.contains(&"/End".to_string())
                    || cmd.contains(&"/DISABLE".to_string())));
            let ran = argv_list_field(&PyValue::Dict(res), "ran");
            assert_eq!(ran[0], vec!["serialwrap", "daemon", "handoff_stop"]);
            assert!(!ran
                .iter()
                .any(|cmd| cmd == &vec!["serialwrap", "daemon", "stop"]));
            if old_mode == sw_pal::traits::SupervisionMode::SystemdUser {
                assert!(ran
                    .iter()
                    .any(|cmd| cmd == &vec!["systemctl", "--user", "disable", "serialwrap"]));
                assert!(!ran
                    .iter()
                    .any(|cmd| cmd == &vec!["systemctl", "--user", "stop", "serialwrap"]));
            }
        });
    }

    let t2_base = TestEnv::posix().with_real_service().with_systemd(true);
    let ep2 = t2_base.default_socket();
    let t2 = t2_base.with_procs(vec![sw_pal::proc_scan::DaemonProc {
        pid: 9100,
        socket: None,
        tree: Vec::new(),
    }]);
    let req2 = sw_cli::setup_cmd::ReconcileRequest {
        old_mode: sw_pal::traits::SupervisionMode::OnDemand,
        target_mode: sw_pal::traits::SupervisionMode::SystemdUser,
        home: t2.home(),
        daemon_running: true,
        daemon_id: sw_cli::graceful_stop::DaemonIdentity::default(),
        old_endpoint: ep2,
        any_flashing: false,
        with_sudo: false,
        force: false,
        hold_target: false,
        socket_path: None,
        config_path: t2.config_path(),
        old_stop: sw_cli::setup_cmd::OldStop::HandedOff {
            token: "0".repeat(32),
            ran: vec![vec![
                "serialwrap".into(),
                "daemon".into(),
                "handoff_stop".into(),
            ]],
        },
        restart_daemon: false,
        preserve_config_format: false,
    };
    t2.with_cli_ctx(|ctx| {
        let err = sw_cli::setup_cmd::reconcile(ctx, &req2).unwrap_err();
        match err {
            sw_cli::setup_cmd::ReconcileError::WaitOldExitTimeout { .. } => {}
            other => panic!("expected WaitOldExitTimeout, got {other:?}"),
        }
        assert!(!t2
            .calls()
            .iter()
            .any(|cmd| cmd.contains(&"stop".to_string()) || cmd.contains(&"/End".to_string())));
        assert!(
            !t2.config_path().exists()
                || !std::fs::read_to_string(t2.config_path())
                    .unwrap()
                    .contains("systemd-user")
        );
    });
}

#[test]
fn reconcile_stops_old_matrix() {
    let t = TestEnv::posix().with_real_service();
    let ep = t.default_socket();
    t.with_cli_ctx(|ctx| {
        let mut req = sw_cli::setup_cmd::ReconcileRequest {
            old_mode: sw_pal::traits::SupervisionMode::SystemdUser,
            target_mode: sw_pal::traits::SupervisionMode::SystemdUser,
            home: t.home(),
            daemon_running: true,
            daemon_id: sw_cli::graceful_stop::DaemonIdentity::default(),
            old_endpoint: ep.clone(),
            any_flashing: false,
            with_sudo: false,
            force: false,
            hold_target: false,
            socket_path: None,
            config_path: t.config_path(),
            old_stop: sw_cli::setup_cmd::OldStop::Reconcile,
            restart_daemon: false,
            preserve_config_format: false,
        };
        assert!(!sw_cli::setup_cmd::reconcile_stops_old(ctx, &req));

        req.old_mode = sw_pal::traits::SupervisionMode::WindowsTask;
        req.target_mode = sw_pal::traits::SupervisionMode::WindowsTask;
        assert!(!sw_cli::setup_cmd::reconcile_stops_old(ctx, &req));

        req.old_mode = sw_pal::traits::SupervisionMode::OnDemand;
        req.target_mode = sw_pal::traits::SupervisionMode::SystemdSystem;
        req.with_sudo = false;
        assert!(!sw_cli::setup_cmd::reconcile_stops_old(ctx, &req));

        req.target_mode = sw_pal::traits::SupervisionMode::WindowsService;
        assert!(!sw_cli::setup_cmd::reconcile_stops_old(ctx, &req));
        req.old_mode = sw_pal::traits::SupervisionMode::WindowsService;
        req.target_mode = sw_pal::traits::SupervisionMode::OnDemand;
        assert!(!sw_cli::setup_cmd::reconcile_stops_old(ctx, &req));

        req.old_mode = sw_pal::traits::SupervisionMode::OnDemand;
        req.target_mode = sw_pal::traits::SupervisionMode::SystemdUser;
        assert!(sw_cli::setup_cmd::reconcile_stops_old(ctx, &req));

        req.old_mode = sw_pal::traits::SupervisionMode::WindowsTask;
        req.target_mode = sw_pal::traits::SupervisionMode::OnDemand;
        assert!(sw_cli::setup_cmd::reconcile_stops_old(ctx, &req));

        assert!(t.calls().is_empty());
        assert!(t.rpc().requests().is_empty());
    });
}

#[test]
fn reconcile_setup_path_unchanged_plain_stop() {
    let t = TestEnv::posix()
        .with_real_service()
        .with_systemd(true)
        .with_var("USER", "alice");
    let ep = t.default_socket();
    let t = t
        .with_daemon_alive_then_gone(&ep, 4321, 5)
        .with_gone_ping_event();
    let (rc, out, _) = t.run(&["setup", "--user"]);
    assert_eq!(rc, 0);
    let setup = dict_field(&json(&out), "setup");
    let ran = argv_list_field(&setup, "ran");
    assert_eq!(
        ran,
        vec![
            vec!["serialwrap", "daemon", "stop"],
            vec!["systemctl", "--user", "daemon-reload"],
            vec!["systemctl", "--user", "enable", "serialwrap"],
            vec!["systemctl", "--user", "start", "serialwrap"],
            vec!["loginctl", "enable-linger", "alice"]
        ]
    );
    assert!(t
        .rpc()
        .requests()
        .iter()
        .any(|(_, method, _)| method == "daemon.stop"));
    let order = t.event_order();
    assert!(order.index_of("rpc:daemon.stop") < order.index_of("rpc:health.ping#gone"));
}

#[test]
fn supervision_set_bad_words() {
    let t = TestEnv::posix().with_real_service();
    let (rc, out, _) = t.run(&["supervision-mode", "set"]);
    assert_eq!(rc, 2);
    let j = json(&out);
    assert_eq!(str_field(&j, "error_code"), "INVALID_ARGS");
    assert_eq!(
        str_field(&j, "message"),
        "用法：serialwrap supervision-mode [set MODE]"
    );
}

// 一般 setup（Task 10 reconcile，雙邊 A/B 對齊）不得在安裝根新增 `.install.lock`：
// 安裝鎖只由 update／rollback／首次安裝持有（Task 11／12）。修正前一般 setup 也會建立鎖檔，
// 使 `test_setup_ab_on_demand_isolated` 的安裝根樹與 Python 基線不一致。
#[test]
fn plain_setup_does_not_create_install_lock() {
    let t = TestEnv::posix()
        .with_real_service()
        .with_config("supervision_mode: on-demand\n");
    let (rc, out, _) = t.run(&["setup", "--on-demand"]);
    assert_eq!(rc, 0, "{out}");
    assert!(!t.install_root().join(".install.lock").exists());
}

// 安裝根已有鎖檔（曾安裝／更新進行中）時，一般 setup 仍先取鎖，與 updater 互斥：持鎖者不放 → TIMEOUT、無任何 RPC。
#[test]
fn plain_setup_waits_for_existing_install_lock_holder() {
    let t = TestEnv::posix()
        .with_real_service()
        .with_config("supervision_mode: on-demand\n")
        .with_install_lock_holder();
    let (rc, out, _) = t.run(&["setup", "--on-demand"]);
    assert_eq!(rc, 2, "{out}");
    let result = json(&out);
    assert_eq!(str_field(&result, "error_code"), "TIMEOUT");
    assert_eq!(str_field(&result, "phase"), "install_lock");
    assert!(t.rpc().requests().is_empty());
}

#[test]
fn p277_supervision_switch_waits_for_install_lock_before_hold() {
    let t = TestEnv::windows()
        .with_real_service()
        .with_config("supervision_mode: windows-task\n")
        .with_install_lock_holder();
    let config_before = t.config_bytes();

    let (rc, out, _) = t.run(&["supervision-mode", "set", "on-demand"]);

    assert_eq!(rc, 2, "監管切換須等待 windows-task 生命週期鎖：{out}");
    let response = json(&out);
    assert_eq!(str_field(&response, "error_code"), "TIMEOUT");
    assert_eq!(str_field(&response, "phase"), "install_lock");
    assert!(t.calls().is_empty(), "取得鎖前不得操作監管工作");
    assert!(t.rpc().requests().is_empty(), "取得鎖前不得停止 daemon");
    assert!(!t.state_dir().join("autostart-hold.json").exists());
    assert_eq!(t.config_bytes(), config_before);
}

fn restart_request(
    t: &TestEnv,
    old_mode: sw_pal::traits::SupervisionMode,
    target_mode: sw_pal::traits::SupervisionMode,
    with_sudo: bool,
    restart_daemon: bool,
) -> sw_cli::setup_cmd::ReconcileRequest {
    sw_cli::setup_cmd::ReconcileRequest {
        old_mode,
        target_mode,
        home: t.home(),
        daemon_running: false,
        daemon_id: sw_cli::graceful_stop::DaemonIdentity::default(),
        old_endpoint: t.default_socket(),
        any_flashing: false,
        with_sudo,
        force: false,
        hold_target: false,
        socket_path: Some(t.default_socket()),
        config_path: t.config_path(),
        old_stop: sw_cli::setup_cmd::OldStop::Reconcile,
        restart_daemon,
        preserve_config_format: false,
    }
}

#[test]
fn s1m241_reconcile_restart_after_refresh() {
    use sw_pal::traits::SupervisionMode;

    for (mode, with_sudo, reload, enable, restart) in [
        (
            SupervisionMode::SystemdUser,
            false,
            vec!["systemctl", "--user", "daemon-reload"],
            vec!["systemctl", "--user", "enable", "serialwrap"],
            vec!["systemctl", "--user", "restart", "serialwrap"],
        ),
        (
            SupervisionMode::SystemdSystem,
            true,
            vec!["sudo", "systemctl", "daemon-reload"],
            vec!["sudo", "systemctl", "enable", "serialwrap"],
            vec!["sudo", "systemctl", "restart", "serialwrap"],
        ),
    ] {
        let t = TestEnv::posix().with_real_service().with_systemd(true);
        let req = restart_request(&t, mode.clone(), mode, with_sudo, true);
        t.with_cli_ctx(|ctx| {
            let result = sw_cli::setup_cmd::reconcile(ctx, &req).unwrap();
            assert_eq!(
                str_field(&PyValue::Dict(result.clone()), "daemon_restart"),
                "ran"
            );
        });
        let calls = t.calls();
        let position = |expected: &[&str]| {
            calls
                .iter()
                .position(|argv| argv.iter().map(String::as_str).eq(expected.iter().copied()))
                .unwrap_or_else(|| panic!("missing command {expected:?} in {calls:?}"))
        };
        assert!(position(&reload) < position(&enable));
        assert!(position(&enable) < position(&restart));
    }

    let t = TestEnv::posix().with_real_service().with_systemd(true);
    let req = restart_request(
        &t,
        SupervisionMode::SystemdSystem,
        SupervisionMode::SystemdSystem,
        false,
        true,
    );
    t.with_cli_ctx(|ctx| {
        let result = sw_cli::setup_cmd::reconcile(ctx, &req).unwrap();
        assert_eq!(
            str_field(&PyValue::Dict(result.clone()), "daemon_restart"),
            "pending_sudo"
        );
        assert!(argv_list_field(&PyValue::Dict(result), "pending_sudo")
            .last()
            .is_some_and(|argv| argv == &["sudo", "systemctl", "restart", "serialwrap"]));
    });
    assert!(t.calls().is_empty());
}

#[test]
fn s1m241_reconcile_restart_outcomes() {
    use sw_pal::traits::SupervisionMode;

    let failed = TestEnv::posix()
        .with_real_service()
        .with_systemd(true)
        .with_command(
            &["systemctl", "--user", "restart", "serialwrap"],
            1,
            "",
            "failed",
        );
    let req = restart_request(
        &failed,
        SupervisionMode::SystemdUser,
        SupervisionMode::SystemdUser,
        false,
        true,
    );
    failed.with_cli_ctx(|ctx| {
        let result = sw_cli::setup_cmd::reconcile(ctx, &req).unwrap();
        assert_eq!(
            str_field(&PyValue::Dict(result), "daemon_restart"),
            "failed"
        );
    });

    let no_restart = TestEnv::posix().with_real_service().with_systemd(true);
    let req = restart_request(
        &no_restart,
        SupervisionMode::SystemdUser,
        SupervisionMode::SystemdUser,
        false,
        false,
    );
    no_restart.with_cli_ctx(|ctx| {
        let result = sw_cli::setup_cmd::reconcile(ctx, &req).unwrap();
        assert!(result.get("daemon_restart").is_none());
    });
    assert!(!no_restart
        .calls()
        .iter()
        .any(|argv| argv.iter().any(|arg| arg == "restart")));

    let transition = TestEnv::posix().with_real_service().with_systemd(true);
    let req = restart_request(
        &transition,
        SupervisionMode::OnDemand,
        SupervisionMode::SystemdUser,
        false,
        true,
    );
    transition.with_cli_ctx(|ctx| {
        let result = sw_cli::setup_cmd::reconcile(ctx, &req).unwrap();
        assert!(matches!(result.get("daemon_restart"), Some(PyValue::Null)));
    });
    assert!(!transition
        .calls()
        .iter()
        .any(|argv| argv.iter().any(|arg| arg == "restart")));
}

#[test]
fn s1m241_verify_restart_fingerprint() {
    let expected = "a1b2c3d4e5f6";

    let temporarily_unavailable = TestEnv::posix();
    let endpoint = temporarily_unavailable.default_socket();
    let temporarily_unavailable = temporarily_unavailable.with_rpc_refused_then_ok(
        &endpoint,
        "health.ping",
        &format!(r#"{{"ok":true,"code_fingerprint":"{expected}"}}"#),
    );
    temporarily_unavailable.with_cli_ctx(|ctx| {
        assert_eq!(
            sw_cli::setup_cmd::verify_daemon_code_after_restart(
                ctx,
                &sw_cli::argparse::Namespace::default(),
                Some(expected),
            ),
            "match"
        );
    });
    assert_eq!(
        temporarily_unavailable.clock().elapsed(),
        Duration::from_millis(500)
    );
    assert_eq!(temporarily_unavailable.rpc().timeouts_s(), vec![0.5, 0.5]);

    let mismatch = TestEnv::posix();
    let endpoint = mismatch.default_socket();
    mismatch.rpc().respond_seq(
        &endpoint,
        "health.ping",
        &[
            r#"{"ok":true,"code_fingerprint":"different"}"#,
            r#"{"ok":true,"code_fingerprint":"a1b2c3d4e5f6"}"#,
        ],
    );
    mismatch.with_cli_ctx(|ctx| {
        assert_eq!(
            sw_cli::setup_cmd::verify_daemon_code_after_restart(
                ctx,
                &sw_cli::argparse::Namespace::default(),
                Some(expected),
            ),
            "mismatch"
        );
    });
    assert_eq!(mismatch.rpc().request_count(&endpoint, "health.ping"), 1);
    assert!(mismatch.clock().sleeps_s().is_empty());

    let missing_fingerprint = TestEnv::posix();
    let endpoint = missing_fingerprint.default_socket();
    let missing_fingerprint =
        missing_fingerprint.with_rpc_ok(&endpoint, "health.ping", r#"{"ok":true}"#);
    missing_fingerprint.with_cli_ctx(|ctx| {
        assert_eq!(
            sw_cli::setup_cmd::verify_daemon_code_after_restart(
                ctx,
                &sw_cli::argparse::Namespace::default(),
                Some(expected),
            ),
            "mismatch"
        );
    });
    assert!(missing_fingerprint.clock().sleeps_s().is_empty());

    let unreachable = TestEnv::posix();
    unreachable.with_cli_ctx(|ctx| {
        assert_eq!(
            sw_cli::setup_cmd::verify_daemon_code_after_restart(
                ctx,
                &sw_cli::argparse::Namespace::default(),
                Some(expected),
            ),
            "unreachable"
        );
    });
    assert_eq!(unreachable.clock().elapsed(), Duration::from_secs(10));
    assert_eq!(unreachable.clock().sleeps_s().len(), 20);
    assert!(unreachable
        .clock()
        .sleeps_s()
        .iter()
        .all(|sleep| *sleep == 0.5));
    assert_eq!(unreachable.rpc().timeouts_s().len(), 21);
    assert!(unreachable
        .rpc()
        .timeouts_s()
        .iter()
        .all(|timeout| *timeout == 0.5));
}

/// reconcile 已把 config.yaml 寫成 canonical socket 後，驗證輪詢不得再打呼叫端凍結的舊 endpoint。
#[test]
fn s1m_c1_verify_uses_socket_rewritten_by_reconcile() {
    let expected = "a1b2c3d4e5f6";
    let t = TestEnv::posix();
    let old_endpoint = format!("{}/old-custom.sock", t.run_dir().display());
    let canonical = t.default_socket();
    let t = t.with_config(&format!(
        "supervision_mode: systemd-user\nsocket_path: {canonical}\n"
    ));
    t.rpc().respond(
        &old_endpoint,
        "health.ping",
        r#"{"ok":true,"code_fingerprint":"stale-old"}"#,
    );
    t.rpc().respond(
        &canonical,
        "health.ping",
        &format!(r#"{{"ok":true,"code_fingerprint":"{expected}"}}"#),
    );

    t.with_cli_ctx(|ctx| {
        assert_eq!(
            sw_cli::setup_cmd::verify_daemon_code_after_restart(
                ctx,
                &sw_cli::argparse::Namespace::default(),
                Some(expected),
            ),
            "match"
        );
    });
    assert_eq!(t.rpc().request_count(&canonical, "health.ping"), 1);
    assert_eq!(t.rpc().request_count(&old_endpoint, "health.ping"), 0);
}

/// 明示 `--socket` 每一輪都優先於已改寫的 config.yaml，與 Python `_resolve_local_endpoint_info` 相同。
#[test]
fn s1m_c1_explicit_socket_overrides_reconciled_config() {
    let expected = "a1b2c3d4e5f6";
    let t = TestEnv::posix();
    let old_endpoint = format!("{}/old-custom.sock", t.run_dir().display());
    let canonical = t.default_socket();
    let t = t.with_config(&format!(
        "supervision_mode: systemd-user\nsocket_path: {canonical}\n"
    ));
    t.rpc().respond(
        &old_endpoint,
        "health.ping",
        r#"{"ok":true,"code_fingerprint":"stale-old"}"#,
    );
    t.rpc().respond(
        &canonical,
        "health.ping",
        &format!(r#"{{"ok":true,"code_fingerprint":"{expected}"}}"#),
    );
    let mut ns = sw_cli::argparse::Namespace::default();
    ns.vals.insert(
        "socket".to_string(),
        sw_cli::argparse::ArgValue::Str(old_endpoint.clone()),
    );

    t.with_cli_ctx(|ctx| {
        assert_eq!(
            sw_cli::setup_cmd::verify_daemon_code_after_restart(ctx, &ns, Some(expected)),
            "mismatch"
        );
    });
    assert_eq!(t.rpc().request_count(&old_endpoint, "health.ping"), 1);
    assert_eq!(t.rpc().request_count(&canonical, "health.ping"), 0);
}

/// 每一輪都重新解析：舊 socket 第一輪仍可連但 ping 未就緒，下一輪起拒絕後改走 canonical。
#[test]
fn s1m_c1_verify_reresolves_endpoint_each_poll() {
    let expected = "a1b2c3d4e5f6";
    let t = TestEnv::posix();
    let old_endpoint = format!("{}/old-custom.sock", t.run_dir().display());
    let canonical = t.default_socket();
    let t = t.with_config(&format!(
        "supervision_mode: systemd-user\nsocket_path: {old_endpoint}\n"
    ));
    t.rpc()
        .respond(&old_endpoint, "health.ping", r#"{"ok":false}"#);
    t.rpc().refuse_from_connect(&old_endpoint, 3);
    t.rpc().respond(
        &canonical,
        "health.ping",
        &format!(r#"{{"ok":true,"code_fingerprint":"{expected}"}}"#),
    );

    t.with_cli_ctx(|ctx| {
        assert_eq!(
            sw_cli::setup_cmd::verify_daemon_code_after_restart(
                ctx,
                &sw_cli::argparse::Namespace::default(),
                Some(expected),
            ),
            "match"
        );
    });
    assert_eq!(t.clock().elapsed(), Duration::from_millis(500));
    assert_eq!(t.rpc().request_count(&old_endpoint, "health.ping"), 1);
    assert_eq!(t.rpc().request_count(&canonical, "health.ping"), 1);
}

/// setup 前 config 指向可連的自訂 socket；reconcile 寫回 canonical 並重啟後，驗證必須打新 socket。
#[test]
fn s1m_c1_setup_verify_polls_reconciled_socket() {
    const DAEMON_FINGERPRINT: &str = "deadbeef0001";
    const INSTALLED_FINGERPRINT: &str = "a1b2c3d4e5f6";
    let t = s1m241_installed_setup_env(Some(INSTALLED_FINGERPRINT));
    let old_endpoint = format!("{}/old-custom.sock", t.run_dir().display());
    let canonical = t.default_socket();
    let t = t
        .with_config(&format!(
            "supervision_mode: systemd-user\nsocket_path: {old_endpoint}\n"
        ))
        .with_systemd(true)
        .with_command(
            &["systemctl", "--user", "is-active", "serialwrap"],
            0,
            "active\n",
            "",
        )
        .with_command(&["systemctl", "--user", "daemon-reload"], 0, "", "")
        .with_command(&["systemctl", "--user", "enable", "serialwrap"], 0, "", "")
        .with_command(&["systemctl", "--user", "restart", "serialwrap"], 0, "", "")
        .with_rpc_ok(&old_endpoint, "health.status", r#"{"ok":true,"pid":1234}"#)
        .with_rpc_ok(
            &old_endpoint,
            "mcu.status",
            r#"{"ok":true,"flashing":false}"#,
        )
        .with_rpc_ok(
            &old_endpoint,
            "health.ping",
            &format!(r#"{{"ok":true,"code_fingerprint":"{DAEMON_FINGERPRINT}"}}"#),
        )
        .with_rpc_ok(
            &canonical,
            "health.ping",
            &format!(r#"{{"ok":true,"code_fingerprint":"{INSTALLED_FINGERPRINT}"}}"#),
        );

    let (rc, out, err) = t.run(&["setup", "--user"]);
    assert_eq!(rc, 0, "setup 失敗：stdout={out:?}, stderr={err:?}");
    let result = json(&out);
    let daemon_code = dict_field(&result, "daemon_code");
    assert_eq!(str_field(&daemon_code, "action"), "restarted");
    assert_eq!(
        str_field(&daemon_code, "after"),
        "match",
        "重啟後必須重新解析 endpoint；stdout={out} stderr={err} old_pings={} canonical_pings={}",
        t.rpc().request_count(&old_endpoint, "health.ping"),
        t.rpc().request_count(&canonical, "health.ping"),
    );
    assert_eq!(t.rpc().request_count(&canonical, "health.ping"), 1);
    assert_eq!(t.rpc().request_count(&old_endpoint, "health.ping"), 1);
}

#[test]
fn bundle_setup_uses_explicit_endpoint_over_config() {
    let t = TestEnv::windows()
        .with_process()
        .with_bundle("0.5.1")
        .with_config("socket_path: tcp://127.0.0.1:49001\n")
        .with_rpc_refused("tcp://127.0.0.1:49003");
    let (rc, out, err) = t.run_from_bundle(&["--endpoint", "tcp://127.0.0.1:49003", "setup"]);
    assert_eq!(rc, 0, "bundle setup 應使用命令列指定的 49003；stdout={out} stderr={err}");
    assert!(
        t.rpc().connect_count("tcp://127.0.0.1:49003") >= 1,
        "safety_check 與安裝流程必須向命令列指定的 49003 發起連線"
    );
    assert_eq!(
        t.rpc().connect_count("tcp://127.0.0.1:49001"),
        0,
        "safety_check 不得向 config 的 49001 發起連線"
    );
}

#[test]
fn bundle_setup_ping_timeout_with_released_lock_proceeds() {
    let t = TestEnv::windows()
        .with_bundle("0.5.1")
        .with_rpc_refused_kind("tcp://127.0.0.1:48700", sw_pal::PalErrorKind::TimedOut);
    let (rc, out, err) = t.run_from_bundle(&["setup"]);
    assert_eq!(rc, 0, "ping 逾時但鎖未被持有時應視為未執行並繼續安裝；stdout={out} stderr={err}");
}

#[test]
fn bundle_setup_auto_selects_port_when_default_port_is_unavailable() {
    let endpoint = "tcp://127.0.0.1:48716";
    let mock_listen = std::sync::Arc::new(MockRpcListenerFactory::with_filter(|port| port >= 48716));
    let t = TestEnv::windows()
        .with_real_service()
        .with_process()
        .with_bundle("0.5.1")
        .with_rpc_listen(mock_listen);
    t.rpc().respond(endpoint, "health.ping", r#"{"ok":true,"pid":1234,"version":"0.5.1"}"#);
    t.rpc().respond(
        endpoint,
        "health.status",
        r#"{"ok":true,"pid":1234,"version":"0.5.1","wal":{"healthy":true}}"#,
    );
    t.rpc().respond(endpoint, "session.list", r#"{"ok":true,"sessions":[]}"#);
    t.rpc().respond(endpoint, "alias.list", r#"{"ok":true,"aliases":[]}"#);
    t.rpc().respond(endpoint, "device.list", r#"{"ok":true,"devices":[]}"#);
    t.rpc().refuse_first(endpoint, 1);
    let (rc, _out, err) = t.run_from_bundle(&["setup"]);
    assert_eq!(rc, 0, "bundle setup 應成功完成；stdout={_out} stderr={err}");
    assert!(
        err.contains("selected port 48716"),
        "stderr 應輸出選用 48716 的說明：{err}"
    );

    // 1. 核對 config.yaml 寫回的新埠
    let config = std::fs::read_to_string(t.config_path()).expect("讀取 config.yaml");
    assert!(
        config.contains("socket_path: 'tcp://127.0.0.1:48716'"),
        "config.yaml 應保存選用的 socket_path：{config}"
    );

    // 2. 核對 Windows 排程工作啟動參數同步改用該埠
    let calls = t.calls();
    let schtasks_create = calls
        .iter()
        .find(|argv| argv.len() >= 4 && argv[0] == "schtasks" && argv[1] == "/Create" && argv[2] == "/XML")
        .unwrap_or_else(|| panic!("找不到 schtasks /Create 呼叫：{calls:?}"));
    let xml_path = &schtasks_create[3];
    let xml_bytes = std::fs::read(xml_path)
        .unwrap_or_else(|e| panic!("找不到 task XML {xml_path}: {e}"));
    let xml_units = xml_bytes[2..]
        .chunks_exact(2)
        .map(|bytes| u16::from_le_bytes([bytes[0], bytes[1]]))
        .collect::<Vec<_>>();
    let xml_content = String::from_utf16(&xml_units).unwrap();
    assert!(
        xml_content.contains(r#"<Arguments>--socket tcp://127.0.0.1:48716</Arguments>"#),
        "Task XML Arguments 應包含 --socket tcp://127.0.0.1:48716：{xml_content}"
    );
}

#[test]
fn bundle_setup_selected_port_starts_fake_daemon_and_checks_health() {
    let endpoint = "tcp://127.0.0.1:48716";
    let mock_listen = std::sync::Arc::new(MockRpcListenerFactory::with_filter(|port| port >= 48716));
    let t = TestEnv::windows()
        .with_config("supervision_mode: on-demand\n")
        .with_bundle("0.5.1")
        .with_process()
        .with_rpc_listen(mock_listen);
    let counter = t.process().spawn_counter();
    t.service().set_start_spawn_counter(Arc::clone(&counter));
    t.rpc().gate_on_spawn(endpoint, Arc::clone(&counter));
    t.rpc().respond(endpoint, "health.ping", r#"{"ok":true,"pid":1234,"version":"0.5.1"}"#);
    t.rpc().respond(
        endpoint,
        "health.status",
        r#"{"ok":true,"pid":1234,"version":"0.5.1","wal":{"healthy":true}}"#,
    );
    t.rpc().respond(endpoint, "session.list", r#"{"ok":true,"sessions":[]}"#);
    t.rpc().respond(endpoint, "alias.list", r#"{"ok":true,"aliases":[]}"#);
    t.rpc().respond(endpoint, "device.list", r#"{"ok":true,"devices":[]}"#);
    t.rpc().revive_after_spawn(endpoint, counter, 1234);

    let (rc, out, err) = t.run_from_bundle(&["setup", "--on-demand"]);

    assert_eq!(rc, 0, "bundle setup 應啟動新埠 daemon 並通過健康檢查；stdout={out} stderr={err}");
    assert!(!t.process().spawns().is_empty(), "正式 bundle 首次安裝應啟動 fake daemon");
    assert_eq!(
        t.rpc().request_count(endpoint, "health.ping"),
        1,
        "健康檢查必須打到自動選用的新埠"
    );
}

#[test]
fn bundle_setup_on_demand_auto_port_preserves_other_config_bytes() {
    let original = "# 使用者註解保留\nsupervision_mode: on-demand # 模式註解\nwindows:\n  exclude_coms: [COM5]\n# 結尾註解保留\n";
    let endpoint = "tcp://127.0.0.1:48716";
    let mock_listen = std::sync::Arc::new(MockRpcListenerFactory::with_filter(|port| port >= 48716));
    let t = TestEnv::windows()
        .with_config(original)
        .with_bundle("0.5.1")
        .with_process()
        .with_rpc_listen(mock_listen);
    let counter = t.process().spawn_counter();
    t.rpc().gate_on_spawn(endpoint, Arc::clone(&counter));
    t.rpc().respond(endpoint, "health.ping", r#"{"ok":true,"pid":1234,"version":"0.5.1"}"#);
    t.rpc().respond(
        endpoint,
        "health.status",
        r#"{"ok":true,"pid":1234,"version":"0.5.1","wal":{"healthy":true}}"#,
    );
    t.rpc().respond(endpoint, "session.list", r#"{"ok":true,"sessions":[]}"#);
    t.rpc().respond(endpoint, "alias.list", r#"{"ok":true,"aliases":[]}"#);
    t.rpc().respond(endpoint, "device.list", r#"{"ok":true,"devices":[]}"#);
    t.rpc().revive_after_spawn(endpoint, counter, 1234);

    let (rc, _out, err) = t.run_from_bundle(&["setup", "--on-demand"]);

    assert_eq!(rc, 0, "bundle on-demand 首次安裝應成功；stderr={err}");
    let config = t.config_bytes();
    assert!(
        String::from_utf8_lossy(&config).contains("socket_path: 'tcp://127.0.0.1:48716'"),
        "config.yaml 應保存選用的 socket_path：{}",
        String::from_utf8_lossy(&config)
    );
    assert_eq!(
        without_root_socket_path(&config),
        original.as_bytes(),
        "正式 bundle 首次安裝除 socket_path 外必須逐位元組保留原始 config"
    );
}

#[test]
fn bundle_setup_nonempty_root_flow_mapping_fails_closed_with_manual_hint() {
    let original = "{log_dir: /logs}\n";
    let mock_listen = std::sync::Arc::new(MockRpcListenerFactory::with_filter(|port| port >= 48716));
    let t = TestEnv::windows()
        .with_config(original)
        .with_bundle("0.5.1")
        .with_process()
        .with_rpc_listen(mock_listen);

    let (rc, out, err) = t.run_from_bundle(&["setup"]);

    assert_ne!(rc, 0, "非空根 flow mapping 不得被自動選埠改寫");
    assert_eq!(str_field(&json(&out), "error_code"), "INTERNAL_ERROR");
    assert_eq!(t.config_bytes(), original.as_bytes());
    assert!(
        err.contains("socket_path") && err.contains("SERIALWRAP_TCP_PORT"),
        "stderr 必須提示手動設定 socket_path 或 SERIALWRAP_TCP_PORT：{err}"
    );
}

#[test]
fn bundle_setup_auto_port_supervisor_query_failure_keeps_original_config() {
    let original = "# 保留註解\nsupervision_mode: windows-task\nwindows:\n  exclude_coms: [COM5]\n";
    let mock_listen = std::sync::Arc::new(MockRpcListenerFactory::with_filter(|port| port >= 48716));
    let t = TestEnv::windows()
        .with_supervisor_state("windows-task", true, false)
        .with_config(original)
        .with_bundle("0.5.1")
        .with_rpc_listen(mock_listen);
    t.service().query_script(vec![TaskQuery::QueryFailed(
        "受控 supervisor 查詢失敗".to_string(),
    )]);

    let (rc, out, _err) = t.run_from_bundle(&["setup"]);

    assert_ne!(rc, 0, "supervisor 查詢失敗時 setup 必須中止");
    assert_eq!(str_field(&json(&out), "error_code"), "INTERNAL_ERROR");
    assert_eq!(t.config_bytes(), original.as_bytes());
}

#[test]
fn bundle_setup_auto_port_switch_failure_keeps_original_config() {
    let original = "# 使用者註解\nsupervision_mode: on-demand\nwindows:\n  exclude_coms: [COM5]\n";
    let mock_listen = std::sync::Arc::new(MockRpcListenerFactory::with_filter(|port| port >= 48716));
    let t = TestEnv::windows()
        .with_config(original)
        .with_bundle("0.5.1")
        .with_rpc_listen(mock_listen)
        .with_fs_fault_replace_suffix("serialwrap.exe.new");

    let (rc, _out, _err) = t.run_from_bundle(&["setup"]);

    assert_ne!(rc, 0, "穩定入口切換失敗時 setup 必須回報失敗");
    assert_eq!(t.config_bytes(), original.as_bytes());
}

#[test]
fn bundle_setup_auto_port_health_failure_keeps_original_config() {
    let original = "# 使用者註解\nsupervision_mode: on-demand\nwindows:\n  exclude_coms: [COM5]\n";
    let mock_listen = std::sync::Arc::new(MockRpcListenerFactory::with_filter(|port| port >= 48716));
    let t = TestEnv::windows()
        .with_config(original)
        .with_bundle("0.5.1")
        .with_rpc_listen(mock_listen);

    let (rc, out, _err) = t.run_from_bundle(&["setup"]);

    assert_ne!(rc, 0, "受控 daemon 未通過新埠健康檢查時 setup 必須回報失敗");
    assert_eq!(str_field(&json(&out), "error_code"), "TIMEOUT");
    assert_eq!(t.config_bytes(), original.as_bytes());
}

#[test]
fn bundle_setup_auto_port_switch_failure_without_config_restores_absence() {
    let mock_listen = std::sync::Arc::new(MockRpcListenerFactory::with_filter(|port| port >= 48716));
    let t = TestEnv::windows()
        .with_bundle("0.5.1")
        .with_rpc_listen(mock_listen)
        .with_fs_fault_replace_suffix("serialwrap.exe.new");

    assert!(!t.config_path().exists(), "首次安裝前 config.yaml 應不存在");
    let (rc, _out, err) = t.run_from_bundle(&["setup", "--on-demand"]);

    assert_ne!(rc, 0, "穩定入口切換失敗時 setup 必須回報失敗");
    assert!(err.contains("selected port 48716"), "測試必須走自動選埠路徑：{err}");
    assert!(
        !t.config_path().exists(),
        "原本沒有 config.yaml 時，入口切換失敗的交易回復不得留下自動選埠建立的檔案"
    );
}

#[test]
fn bundle_setup_auto_port_health_failure_without_config_restores_absence() {
    let mock_listen = std::sync::Arc::new(MockRpcListenerFactory::with_filter(|port| port >= 48716));
    let t = TestEnv::windows()
        .with_bundle("0.5.1")
        .with_process()
        .with_rpc_listen(mock_listen);

    assert!(!t.config_path().exists(), "首次安裝前 config.yaml 應不存在");
    let (rc, out, err) = t.run_from_bundle(&["setup", "--on-demand"]);

    assert_ne!(rc, 0, "daemon 健康檢查逾時時 setup 必須回報失敗");
    assert_eq!(str_field(&json(&out), "error_code"), "TIMEOUT");
    assert!(err.contains("selected port 48716"), "測試必須走自動選埠路徑：{err}");
    assert!(
        !t.config_path().exists(),
        "原本沒有 config.yaml 時，健康檢查失敗的交易回復不得留下自動選埠建立的檔案"
    );
}

#[test]
fn bundle_setup_auto_port_interrupted_recovery_without_config_reselects_port() {
    let mock_listen = std::sync::Arc::new(MockRpcListenerFactory::with_filter(|port| port >= 48716));
    let t = TestEnv::windows()
        .with_bundle("0.5.1")
        .with_process()
        .with_rpc_listen(mock_listen)
        .with_crash_at("entries_switching:after_effect");

    assert!(!t.config_path().exists(), "首次安裝前 config.yaml 應不存在");
    let (crash_rc, _out, _err) = t.run_from_bundle(&["setup", "--on-demand"]);
    assert_eq!(crash_rc, 137, "注入的交易中斷應留下待復原 journal");
    assert!(t.config_path().exists(), "中斷點應位於自動選埠 config 寫入之後");
    assert!(t.journal_json().is_some(), "中斷後應保留交易 journal");

    let mut t = t;
    t.clear_crash();
    let (rc, out, err) = t.run_from_bundle(&["setup", "--on-demand"]);

    assert!(
        err.contains("selected port 48716"),
        "journal 復原後重試必須再次自動選埠：{err}"
    );
    assert_ne!(rc, 0, "自動選埠後沒有健康回應時重試安裝必須失敗");
    assert_eq!(str_field(&json(&out), "error_code"), "TIMEOUT");
    assert!(
        !t.config_path().exists(),
        "journal 復原與重試失敗後，原本不存在的 config.yaml 必須維持不存在"
    );
    assert!(t.journal_json().is_none(), "交易回復成功後應清除 journal");
}

#[cfg(all(test, unix))]
fn posix_file_mode(path: &std::path::Path) -> u32 {
    let metadata = sw_pal::fs_ops::file_metadata_fingerprint(path, false)
        .expect("讀取檔案 metadata")
        .expect("POSIX metadata 應可用");
    let mode = metadata.split(';').find_map(|field| field.strip_prefix("mode=")).expect("metadata 應包含 mode");
    u32::from_str_radix(mode, 8).expect("mode 八進位")
}

#[cfg(all(test, unix))]
fn posix_file_owner_group(path: &std::path::Path) -> (u32, u32) {
    let metadata = sw_pal::fs_ops::file_metadata_fingerprint(path, false)
        .expect("讀取檔案 owner/group")
        .expect("POSIX metadata 應可用");
    let field = |name| metadata.split(';').find_map(|field| field.strip_prefix(name)).expect("metadata 欄位存在").parse().expect("metadata 整數");
    (field("uid="), field("gid="))
}

#[cfg(all(test, unix))]
fn posix_set_file_mode(path: &std::path::Path, mode: u32) {
    sw_pal::fs_ops::posix::test_set_path_mode(path, mode).expect("設定測試檔案 mode");
}

#[cfg(all(test, unix))]
fn posix_secondary_group() -> u32 {
    let (primary, groups) = sw_pal::fs_ops::posix::test_process_groups().expect("讀取 POSIX 測試行程 groups");
    groups
        .into_iter()
        .find(|group| *group != primary)
        .expect("POSIX owner/group 測試需要一個不同於 primary group 的 supplementary group")
}

#[cfg(all(test, unix))]
fn posix_setgid_directory(path: &std::path::Path, group: u32) {
    sw_pal::fs_ops::posix::test_set_path_group(path, group).expect("設定測試目錄 group");
    posix_set_file_mode(path, posix_file_mode(path) | 0o2000);
}

/// inst4-f2 V051-a0b00f2-1：journal 綁定原交易 config；切換環境路徑時 fail closed。
#[cfg(all(test, unix))]
#[test]
fn inst4_f2_interrupted_setup_refuses_changed_config_path() {
    let t = TestEnv::posix().with_bundle(CURRENT_VERSION).with_process();
    let config_a = t.config_dir().join("transaction-a.yaml");
    let config_b = t.config_dir().join("user-b.yaml");
    let config_b_original = "# 使用者設定 B\nsupervision_mode: on-demand\nsocket_path: tcp://127.0.0.1:48731\n".as_bytes();
    let config_a_text = config_a.display().to_string();
    let mut t = t
        .with_var("SERIALWRAP_CONFIG", &config_a_text)
        .with_crash_at("entries_switching:after_effect");

    assert!(!config_a.exists(), "交易前 A 必須不存在");
    let (crash_rc, _out, _err) = t.run_from_bundle(&["setup", "--on-demand"]);
    assert_eq!(crash_rc, 137, "注入的交易中斷應留下待復原 journal");
    let config_a_after_crash = std::fs::read(&config_a).expect("交易應已建立 A");
    let journal_before = t.journal_bytes().expect("交易中斷後必須留下 journal");

    std::fs::write(&config_b, config_b_original).expect("建立使用者設定 B");
    posix_set_file_mode(&config_b, 0o640);
    let config_b_mode = posix_file_mode(&config_b);
    let config_b_text = config_b.display().to_string();
    t.clear_crash();
    t = t.with_var("SERIALWRAP_CONFIG", &config_b_text);

    let (rc, out, err) = t.run_from_bundle(&["setup", "--on-demand"]);

    assert_eq!(std::fs::read(&config_b).expect("B 必須保留"), config_b_original);
    assert_eq!(posix_file_mode(&config_b), config_b_mode, "B 的 mode 必須完整保留");
    assert_eq!(rc, 2, "config 路徑與 journal 不一致時 setup 必須安全停止：{out} {err}");
    assert!(out.contains("config_path_mismatch"), "應回報 config 路徑不一致：{out}");
    assert!(err.contains(&config_b_text), "stderr 應指出當次解析到的 config 路徑：{err}");
    assert_eq!(std::fs::read(&config_a).expect("安全停止不得改寫 A"), config_a_after_crash);
    assert_eq!(t.journal_bytes().as_deref(), Some(journal_before.as_slice()), "拒絕復原時 journal 必須保留且不改寫");
}

/// inst4-f2 V051-774ad4a-1：既有 config 在中斷後被修改時，setup 不得以舊快照覆寫。
#[cfg(all(test, unix))]
#[test]
fn inst4_f2_interrupted_recovery_refuses_modified_existing_config() {
    let original = b"supervision_mode: on-demand\nsocket_path: /run/serialwrap/original.sock\n";
    let mut t = TestEnv::posix()
        .with_config(std::str::from_utf8(original).expect("config UTF-8"))
        .with_installed("0.4.0")
        .with_current("0.4.0", None)
        .with_bundle(CURRENT_VERSION)
        .with_crash_at("entries_switching:after_effect");
    posix_set_file_mode(&t.config_path(), 0o640);

    let (crash_rc, _out, _err) = t.run_from_bundle(&["setup", "--on-demand"]);
    assert_eq!(crash_rc, 137, "注入的交易中斷應留下待復原 journal");
    let journal_before = t.journal_bytes().expect("交易中斷後必須留下 journal");

    let user_edit = "# 中斷後的使用者修改\nsupervision_mode: on-demand\nsocket_path: /run/serialwrap/user-edit.sock\n".as_bytes();
    std::fs::write(t.config_path(), user_edit).expect("模擬中斷後使用者修改 config");
    posix_set_file_mode(&t.config_path(), 0o600);
    let edited_bytes = std::fs::read(t.config_path()).expect("讀取使用者修改後的 config");
    let edited_mode = posix_file_mode(&t.config_path());
    let edited_owner_group = posix_file_owner_group(&t.config_path());

    t.clear_crash();
    let (rc, out, err) = t.run_from_bundle(&["setup", "--on-demand"]);

    assert_eq!(rc, 2, "無法確認 config 屬於交易時 setup 必須安全停止：{out} {err}");
    assert!(out.contains("config_ownership_unknown"), "應回報 config 歸屬無法確認：{out}");
    assert!(err.contains("config") && err.contains("journal"), "stderr 應說明 config 與 journal 保留狀態：{err}");
    assert_eq!(std::fs::read(t.config_path()).expect("安全停止不得改寫 config"), edited_bytes);
    assert_eq!(posix_file_mode(&t.config_path()), edited_mode, "安全停止不得改寫使用者 mode");
    assert_eq!(posix_file_owner_group(&t.config_path()), edited_owner_group, "安全停止不得改寫使用者 owner/group");
    assert_eq!(t.journal_bytes().as_deref(), Some(journal_before.as_slice()), "安全停止時 journal 必須原樣保留");
}

/// inst4-f2 V051-a0b00f2-2：切換失敗後回復既有 POSIX config 的內容與 mode。
#[cfg(all(test, unix))]
#[test]
fn inst4_f2_switch_failure_restores_existing_config_mode() {
    let original = b"supervision_mode: on-demand\nsocket_path: /run/serialwrap/test.sock\n";
    let t = TestEnv::posix()
        .with_config(std::str::from_utf8(original).expect("config UTF-8"))
        .with_installed("0.4.0")
        .with_current("0.4.0", None)
        .with_bundle(CURRENT_VERSION)
        .with_fs_fault_replace_suffix(".new");
    posix_set_file_mode(&t.config_path(), 0o640);
    let original_owner_group = posix_file_owner_group(&t.config_path());
    let config_dir = t.config_path().parent().expect("config parent").to_path_buf();
    posix_setgid_directory(&config_dir, posix_secondary_group());

    let (rc, out, _err) = t.run_from_bundle(&["setup", "--on-demand"]);

    assert_eq!(rc, 2, "入口切換注入失敗時 setup 必須回報失敗：{out}");
    assert!(out.contains("\"reverted\":true"), "切換失敗必須完成回復：{out}");
    assert_eq!(std::fs::read(t.config_path()).expect("config 必須存在"), original);
    assert_eq!(posix_file_mode(&t.config_path()), 0o640, "回復必須保留原始 0640 mode");
    assert_eq!(posix_file_owner_group(&t.config_path()), original_owner_group, "切換失敗回復必須保留原始 owner/group");
}

/// inst4-f2 V051-a0b00f2-2：journal 中斷復原後回復既有 POSIX config 的內容與 mode。
#[cfg(all(test, unix))]
#[test]
fn inst4_f2_interrupted_recovery_restores_existing_config_mode() {
    let original = b"supervision_mode: on-demand\nsocket_path: /run/serialwrap/test.sock\n";
    let mut t = TestEnv::posix()
        .with_config(std::str::from_utf8(original).expect("config UTF-8"))
        .with_installed("0.4.0")
        .with_current("0.4.0", None)
        .with_bundle(CURRENT_VERSION)
        .with_crash_at("entries_switching:after_effect");
    posix_set_file_mode(&t.config_path(), 0o640);
    let original_owner_group = posix_file_owner_group(&t.config_path());
    let config_dir = t.config_path().parent().expect("config parent").to_path_buf();
    posix_setgid_directory(&config_dir, posix_secondary_group());

    let (crash_rc, _out, _err) = t.run_from_bundle(&["setup", "--on-demand"]);
    assert_eq!(crash_rc, 137, "注入的交易中斷應留下待復原 journal");
    assert!(t.journal_json().is_some(), "交易中斷後必須留下 journal");
    t.clear_crash();

    let recovery = t.recover().expect("journal");

    assert!(recovery.ok, "中斷復原必須成功：{recovery:?}");
    assert_eq!(std::fs::read(t.config_path()).expect("config 必須存在"), original);
    assert_eq!(posix_file_mode(&t.config_path()), 0o640, "journal 復原必須保留原始 0640 mode");
    assert_eq!(posix_file_owner_group(&t.config_path()), original_owner_group, "中斷復原必須保留原始 owner/group");
}

#[test]
fn bundle_setup_ping_timeout_with_held_instance_lock_refuses_second_daemon() {
    let endpoint = TestEnv::win_endpoint();
    let t = TestEnv::windows()
        .with_config("supervision_mode: windows-task\n")
        .with_bundle("0.5.1")
        .with_process()
        .with_rpc_refused_kind(endpoint, sw_pal::PalErrorKind::TimedOut);
    let held = t.with_cli_ctx(|ctx| {
        ctx.p.instance_lock
            .acquire(
                std::path::Path::new(&ctx.paths.lock_path),
                &sw_pal::EndpointSpec::Tcp {
                    host: "127.0.0.1".to_string(),
                    port: 48700,
                },
            )
            .expect("測試需先持有 daemon singleton lock")
    });

    let (rc, out, _err) = t.run_from_bundle(&["setup"]);

    assert_eq!(rc, 2, "ping 逾時且 singleton lock 仍被持有時不得啟動第二個 daemon");
    assert_eq!(str_field(&json(&out), "error_code"), "DEVICE_STILL_HELD");
    assert!(t.process().spawns().is_empty(), "不得 spawn 第二個 daemon");
    drop(held);
}

#[test]
fn bundle_setup_explicit_port_bind_failure_does_not_auto_select() {
    let mock_listen = std::sync::Arc::new(MockRpcListenerFactory::with_filter(|port| port >= 48716));
    let t = TestEnv::windows()
        .with_process()
        .with_bundle("0.5.1")
        .with_real_service()
        .with_rpc_listen(mock_listen);
    // 明確指定無法 bind 的 48705（非預設埠或明確指定）
    let (rc, _out, _err) = t.run_from_bundle(&["--endpoint", "tcp://127.0.0.1:48705", "setup"]);
    assert_ne!(rc, 0, "明確指定的埠無法 bind 時不得自動換埠");
    let config = std::fs::read_to_string(t.config_path()).unwrap_or_default();
    assert!(
        !config.contains("48716"),
        "明確指定失敗時不得寫入自動選取的埠：{config}"
    );
}



