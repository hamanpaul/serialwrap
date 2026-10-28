//! P7 Task 17（08-slice23-login-events.md）：session RPC 接線（self_test／recover／attach／log_*）的 Slice 2.3 行為。
//!
//! 06 T18 已建立分支與參數轉型；本檔以 `ServiceRig`（真 `SessionManager`＋`Service`）回歸鎖定轉型並驗 2.3 新行為。
//! Python 基線：`service.py` 的 session.* 分支、`session_manager.recover_session`／`attach_session`／`log_*`。
//! 未承接（BLOCKED-UPSTREAM，見 p7-local-windows-todo）：`attach_human_lease_branch_last_error_field`（P6 human lease）、
//! `attach_credentials_unresolved_last_error_field`／`attach_resets_credentials_warned`／`attach_state_result_mapping`
//! 的 FLASHING／RECOVERING 子案（需 Task 7 收斂表與 FLASHING 夾具）。
#![cfg(feature = "test-fakes")]
mod support;

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Duration;

use support::*;
use sw_proto::pyjson::{PyDict, PyValue};

fn keys(d: &PyDict) -> Vec<&str> {
    d.0.keys().map(String::as_str).collect()
}

const PASSTHROUGH: &str = "profiles:\n  u:\n    platform: passthrough\n    prompt_regex: '=> '\n    ready_probe: 'echo x'\ntargets:\n  - {com: COM0, profile: u, device_by_id: /s/by-id/usb-A}\n";

fn passthrough_attached() -> ServiceRig {
    let rig = ServiceRig::build(RigSpec {
        yaml: PASSTHROUGH.to_string(),
        devices: vec![(BY_ID_A.into(), REAL_A.into())],
        explicit_target_count: 1,
        allowlist: None,
        wait_ready: vec![],
    });
    rig.wait_state("COM0", "ATTACHED");
    rig
}

/// 背景推進 FakeClock（每 20 ms 推 1 s 並送 Tick），直到 `stop` 設立。
fn drive_clock(rig: &ServiceRig, stop: Arc<AtomicBool>) -> std::thread::JoinHandle<()> {
    let clock = rig.clock.clone();
    let mgr = rig.mgr.clone();
    std::thread::spawn(move || {
        while !stop.load(Ordering::SeqCst) {
            clock.advance(Duration::from_secs(1));
            mgr.on_tick();
            std::thread::sleep(Duration::from_millis(20));
        }
    })
}

#[test]
fn self_test_invalid_args_has_no_command_capable() {
    // service 層 INVALID_ARGS 早退，不補 command_capable（06 T18 回歸）。
    let rig = ServiceRig::ready_shell();
    let r = rig.call("session.self_test", "{}");
    assert_eq!(keys(&r), ["ok", "error_code", "version"]);
    assert_eq!(r["error_code"], PyValue::from("INVALID_ARGS"));
}

#[test]
fn self_test_bad_timeout_before_selector_is_exception() {
    // `py_float(params.get("timeout_s") or 2.0)` 在 selector 驗證之前：非法數字字串即使 selector 空也是 EXCEPTION。
    let rig = ServiceRig::ready_shell();
    let err = rig.call_err("session.self_test", r#"{"timeout_s":"x"}"#);
    assert!(err.message.contains("could not convert string to float"), "{}", err.message);
}

#[test]
fn self_test_not_found_has_lease_ctx_and_command_capable_false() {
    let rig = ServiceRig::ready_shell();
    let r = rig.call("session.self_test", r#"{"selector":"COM9"}"#);
    assert_eq!(r["error_code"], PyValue::from("SESSION_NOT_FOUND"));
    assert_eq!(r["selector"], PyValue::from("COM9"));
    for k in ["interactive_owner", "human_attached", "human_active", "recovery_mode"] {
        assert!(r.get(k).is_some(), "缺 {k}：{r:?}");
    }
    assert_eq!(r["command_capable"], PyValue::Bool(false));
}

#[test]
fn recover_force_string_false_is_true() {
    // `force = bool(params.get("force"))`：`"false"` 為非空字串 → 真，ATTACHED 走 force（clear＋reattach）而非 REPROBE。
    let rig = passthrough_attached();
    let stop = Arc::new(AtomicBool::new(false));
    let driver = drive_clock(&rig, stop.clone());
    let r = rig.call("session.recover", r#"{"selector":"COM0","force":"false"}"#);
    stop.store(true, Ordering::SeqCst);
    driver.join().unwrap();
    assert_eq!(r["action"], PyValue::from("FORCE_CLEAR_REATTACH"), "{r:?}");
    // 對照：force 缺省 → REPROBE（passthrough 不 probe，SC-35）。
    let rig = passthrough_attached();
    let r = rig.call("session.recover", r#"{"selector":"COM0"}"#);
    assert_eq!(r["action"], PyValue::from("REPROBE"), "{r:?}");
}

#[test]
fn attach_passthrough_attached_ok_no_probe() {
    // SC-35：passthrough（有 ready_probe）ATTACHED 的 session.attach → `{"ok":true,"session":…}`、零 TX、仍 ATTACHED。
    let rig = passthrough_attached();
    let before = rig.device(REAL_A).write_calls();
    let r = rig.call("session.attach", r#"{"selector":"COM0"}"#);
    assert_eq!(keys(&r), ["ok", "session", "version"]);
    assert_eq!(r["ok"], PyValue::Bool(true));
    assert_eq!(rig.device(REAL_A).write_calls(), before, "passthrough 不得 probe");
    assert_eq!(
        rig.session("COM0").unwrap()["state"],
        PyValue::from("ATTACHED")
    );
}

#[test]
fn log_rpcs_shapes() {
    // `log_responses` golden 的鍵序（service 層另附 `version`）；`session.log_status` 屬可安全重送的唯讀方法。
    let rig = ServiceRig::ready_shell();
    let st = rig.call("session.log_status", r#"{"selector":"COM0"}"#);
    assert_eq!(keys(&st), ["ok", "active", "session", "version"]);
    let stop = rig.call("session.log_stop", r#"{"selector":"COM0"}"#);
    assert_eq!(keys(&stop), ["ok", "error_code", "session", "version"]);
    assert_eq!(stop["error_code"], PyValue::from("NO_ACTIVE_CAPTURE"));
    for m in ["session.log_start", "session.log_stop", "session.log_status"] {
        let r = rig.call(m, r#"{"selector":"COM9"}"#);
        assert_eq!(keys(&r), ["ok", "error_code", "selector", "version"], "{m}");
        assert_eq!(r["error_code"], PyValue::from("SESSION_NOT_FOUND"), "{m}");
    }
    assert!(sw_proto::constants::RETRYABLE_READONLY_METHODS.contains(&"session.log_status"));
    assert!(!sw_proto::constants::RETRYABLE_READONLY_METHODS.contains(&"session.log_start"));
}

#[test]
fn session_public_fields_p7() {
    // `session.get_state` 的 session 鍵序＝Python `SessionRuntime.to_public_dict()`（golden `log_responses:001` 擷取）。
    let rig = ServiceRig::ready_shell();
    let s = rig.session("COM0").expect("COM0");
    assert_eq!(
        keys(&s),
        [
            "session_id", "profile", "com", "alias", "act_no", "device_by_id", "platform", "profile_source",
            "command_capable", "state", "last_error", "last_error_detail", "detached_at", "last_ready_at", "vtty",
            "attached_real_path", "bridge_generation", "recovering", "interactive_session_id", "foreground_busy",
            "fg_cmd_expected_duration_s", "console_count", "last_state_change_at", "last_rx_at", "last_tx_at",
            "last_probe_at", "reprobe_attempts", "next_reprobe_at", "reprobe_exhausted", "boot_quiet_remaining_s",
            "ready_reconfirm_pending", "ready_reconfirm_failed", "ready_reconfirm_remaining_s", "idle_for_ms",
            "last_rx_age_s", "last_tx_age_s", "rx_bytes_last_10s", "rx_rate_bps", "outstanding_commands",
            "activity_classification", "released_by", "released_at", "released_reason", "capture",
            "drain_stalled", "drain_timeout_epoch",
        ]
    );
    // 2.3 起 reprobe 欄位為實值：READY 時 attempts 0、未排程、未耗盡。
    assert_eq!(s["reprobe_attempts"], PyValue::from(0));
    assert_eq!(s["next_reprobe_at"], PyValue::Null);
    assert_eq!(s["reprobe_exhausted"], PyValue::Bool(false));
}

// --- Task 17 剩餘列（Task 7 OD-35 後解除阻擋）：帳密未解析的 attach 回應與再告警（SC-34、#140）。

const CRED_SHELL: &str = "profiles:\n  c:\n    platform: shell\n    prompt_regex: '(?m)^root@t:.*# '\n    login_regex: 'login: ?$'\n    password_regex: 'Password: ?$'\n    user_env: SW_T_U\n    pass_env: SW_T_P\n    env_file: /nonexistent/s21.env\n    timeout_s: 2.0\ntargets:\n  - {com: COM0, profile: c, device_by_id: /s/by-id/usb-A}\n";

fn cred_attached() -> ServiceRig {
    let rig = ServiceRig::build(RigSpec {
        yaml: CRED_SHELL.to_string(),
        devices: vec![(BY_ID_A.into(), REAL_A.into())],
        explicit_target_count: 1,
        allowlist: None,
        wait_ready: vec![],
    });
    rig.wait_state("COM0", "ATTACHED");
    rig
}

fn credentials_metas(rig: &ServiceRig) -> usize {
    let r = rig.call("log.tail_raw", r#"{"selector":"COM0","limit":500}"#);
    match &r["records"] {
        PyValue::List(rows) => rows
            .iter()
            .filter(|row| {
                let PyValue::Dict(d) = row else { return false };
                d.get("dir").and_then(PyValue::as_str) == Some("META")
                    && sw_proto::pyjson::dumps_dict(d, &sw_proto::pyjson::RPC_WIRE)
                        .contains("credentials_unresolved")
            })
            .count(),
        other => panic!("records：{other:?}"),
    }
}

#[test]
fn attach_credentials_unresolved_last_error_field() {
    // SC-34（OD-14 #1）：ATTACHED＋CREDENTIALS_UNRESOLVED，`session.attach` 再探仍未解析 → `_attach_state_result`：
    // `{"ok":false,"error_code":"SESSION_NOT_READY","last_error":"CREDENTIALS_UNRESOLVED","last_error_detail":null,
    // "session":…}`（error_code 不得為 CREDENTIALS_UNRESOLVED）。
    let rig = cred_attached();
    let r = rig.call("session.attach", r#"{"selector":"COM0"}"#);
    assert_eq!(
        keys(&r),
        ["ok", "error_code", "last_error", "last_error_detail", "session", "version"]
    );
    assert_eq!(r["ok"], PyValue::Bool(false));
    assert_eq!(r["error_code"], PyValue::from("SESSION_NOT_READY"));
    assert_eq!(r["last_error"], PyValue::from("CREDENTIALS_UNRESOLVED"));
    assert_eq!(r["last_error_detail"], PyValue::Null);
    match &r["session"] {
        PyValue::Dict(s) => {
            assert_eq!(s["state"], PyValue::from("ATTACHED"));
            assert_eq!(s["last_error"], PyValue::from("CREDENTIALS_UNRESOLVED"));
        }
        other => panic!("session：{other:?}"),
    }
}

#[test]
fn attach_resets_credentials_warned() {
    // #140：手動 attach 為重新介入（`credentials_warned = False`）——仍未解析時再告警一次（WAL META +1）。
    let rig = cred_attached();
    let before = credentials_metas(&rig);
    assert_eq!(before, 1, "首次 attach 告警一次");
    rig.call("session.attach", r#"{"selector":"COM0"}"#);
    assert_eq!(credentials_metas(&rig), before + 1);
}
