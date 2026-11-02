//! P4-T12 `setup` 更新與 `setup --rollback`（SC-23）的失敗測試。
//!
//! 紅燈階段（TDD）：`run_update`、`run_rollback` 與 `recover_journal` 尚未實作完成，
//! 本檔案的案例應以 `not implemented: P4-T12` 或斷言不符失敗，不得因語法或編譯錯誤失敗。
//! 規格來源：`docs/rust-refactor/plans/05-stage1b-rust-cli-mgmt.md` Task 12；盤點 `inventory/07-platform-mgmt.md` §12.1。

mod support;

use support::*;

const IDLE_LIST: &str = "{\"ok\":true,\"sessions\":[{\"com\":\"COM0\",\"state\":\"READY\",\"foreground_busy\":false,\"outstanding_commands\":0,\"interactive_session_id\":null,\"recovering\":false}]}";
const BUSY_PUSH: &str = "{\"ok\":false,\"error_code\":\"SESSION_BUSY\",\"busy\":[{\"com\":\"COM0\",\"state\":\"READY\",\"foreground_busy\":true,\"outstanding_commands\":0,\"interactive_session_id\":null,\"recovering\":false,\"ops\":[\"file.push\"]}]}";
const BUSY_FLASH: &str = "{\"ok\":false,\"error_code\":\"FLASHING_BUSY\",\"busy\":[{\"com\":\"COM0\",\"state\":\"FLASHING\",\"foreground_busy\":false,\"outstanding_commands\":0,\"interactive_session_id\":null,\"recovering\":false,\"ops\":[\"flash\"]}]}";

fn acts(t: &TestEnv) -> Vec<String> {
    t.service().actions().into_iter().map(|a| a.1).collect()
}

fn methods(t: &TestEnv) -> Vec<String> {
    t.rpc().requests().iter().map(|r| r.1.clone()).collect()
}

fn handoffs(t: &TestEnv) -> usize {
    methods(t).iter().filter(|m| *m == "daemon.handoff_stop").count()
}

fn sys_ep(t: &TestEnv) -> String {
    t.default_socket()
}

fn assert_back_to_original(t: &TestEnv, cfg0: &[u8], cur0: &[u8]) {
    assert_eq!(
        std::fs::read(t.root().current_file).unwrap(),
        cur0,
        "current-release.json 必須逐位元組回到原值"
    );
    assert_eq!(
        t.entry_bytes("serialwrap.exe"),
        t.version_bytes("0.4.0", "bin/serialwrap.exe")
    );
    assert_eq!(
        t.entry_bytes("serialwrapd.exe"),
        t.version_bytes("0.4.0", "bin/serialwrapd.exe")
    );
    assert_eq!(t.config_bytes(), cfg0, "config.yaml 必須回到原值");
    assert!(t.journal_json().is_none() && !t.root().root.join("install-journal.snapshot").exists());
}

fn assert_refused_untouched(
    t: &TestEnv,
    j: &serde_json::Value,
    key: &str,
    code: &str,
    cur_ver: &str,
) {
    assert_eq!(
        (s(j, "error_code"), s(j, "phase")),
        (code, "update_safety_check")
    );
    let steps = j[key]["steps"].as_array().unwrap();
    assert_eq!(
        steps.last().unwrap()["step"],
        "safety_check",
        "不得進入 stop_supervisor"
    );
    assert_eq!(steps.last().unwrap()["detail"]["force_ignored"], true);
    assert!(
        steps.last().unwrap()["detail"].get("skipped").is_none(),
        "busy 判定不得被略過"
    );
    assert!(t.service().actions().is_empty(), "不得 disable／stop／End 監管");
    assert!(
        t.rpc()
            .requests()
            .iter()
            .all(|r| r.1 != "daemon.stop" && r.1 != "daemon.handoff_stop"),
        "不得要求 daemon 停止或交接"
    );
    assert_eq!(
        sw_cli::install_layout::read_current(&t.root())
            .unwrap()
            .unwrap()
            .bundle_version,
        cur_ver
    );
    assert_eq!(
        t.entry_bytes("serialwrap.exe"),
        t.version_bytes(cur_ver, "bin/serialwrap.exe")
    );
}

fn assert_handoff_refused(
    t: &TestEnv,
    j: &serde_json::Value,
    key: &str,
    code: &str,
    cur_ver: &str,
) {
    assert_eq!(
        (s(j, "error_code"), s(j, "phase")),
        (code, "update_safety_check")
    );
    assert_eq!(s(j, "handoff"), "refused");
    let steps = j[key]["steps"].as_array().unwrap();
    assert_eq!(steps.last().unwrap()["step"], "stop_supervisor");
    let rpcs: Vec<String> = t.rpc().requests().iter().map(|r| r.1.clone()).collect();
    assert_eq!(
        rpcs.iter().filter(|m| *m == "daemon.handoff_stop").count(),
        1,
        "交接只問一次、不重試"
    );
    assert!(
        rpcs.iter().all(|m| m != "daemon.stop"),
        "不得改送一般 daemon.stop"
    );
    let o = t.event_order();
    assert!(
        o.index_of("service:hold_autostart")
            < o.index_of("rpc:daemon.handoff_stop")
    );
    assert!(
        o.index_of("rpc:daemon.handoff_stop")
            < o.index_of("service:release_autostart:true"),
        "拒絕後還原 enabled"
    );
    assert!(
        !t.calls()
            .iter()
            .any(|c| c.get(1).map(String::as_str) == Some("/End")),
        "不得硬停"
    );
    assert_eq!(
        sw_cli::install_layout::read_current(&t.root())
            .unwrap()
            .unwrap()
            .bundle_version,
        cur_ver
    );
    assert_eq!(
        t.entry_bytes("serialwrap.exe"),
        t.version_bytes(cur_ver, "bin/serialwrap.exe")
    );
    assert!(!t.root().root.join("install-journal.json").exists());
}

fn assert_revert_deferred_untouched(t: &TestEnv, j0: Option<&[u8]>, new_ver: &str) {
    assert!(
        methods(t).iter().all(|m| m != "daemon.stop"),
        "回退／復原不得送一般 daemon.stop"
    );
    assert!(
        !t.calls()
            .iter()
            .any(|c| c.get(1).map(String::as_str) == Some("/End")),
        "交接被拒或不明時不得硬停"
    );
    assert_eq!(
        sw_cli::install_layout::read_current(&t.root())
            .unwrap()
            .unwrap()
            .bundle_version,
        new_ver,
        "不得切換回 Previous"
    );
    for exe in ["serialwrap.exe", "serialwrapd.exe"] {
        assert_eq!(
            t.entry_bytes(exe),
            t.version_bytes(new_ver, &format!("bin/{exe}")),
            "{exe} 不得被還原"
        );
    }
    let st = t.service().query_now();
    assert_eq!(
        (st.enabled, st.running),
        (Some(true), true),
        "新版繼續服務其已受理的操作、監管 enabled 還原為交接前值"
    );
    assert!(
        t.journal_json().is_some() && t.root().root.join("install-journal.snapshot").exists(),
        "journal 與 snapshot_dir 必須保留"
    );
    if let Some(j0) = j0 {
        assert_eq!(t.journal_bytes().unwrap(), j0, "journal 必須逐位元組不變");
    }
}

#[test]
fn rollback_without_previous_is_unavailable() {
    let t = TestEnv::windows().with_install_root().with_current("0.4.0", None);
    let (rc, out, err) = t.run(&["setup", "--rollback"]);
    assert_eq!(rc, 2);
    let j = json(&out);
    assert_eq!(
        (s(&j, "error_code"), s(&j, "phase")),
        ("INVALID_ARGS", "rollback_verify")
    );
    assert_eq!(
        s(&j, "message"),
        "沒有可回滾的 Previous 版本（未記錄 previous）"
    );
    assert!(err.starts_with("serialwrap: setup failed: INVALID_ARGS: "));
}

#[test]
fn update_refuses_while_file_transfer_in_progress() {
    let t = TestEnv::windows()
        .with_supervisor_state("windows-task", false, false)
        .with_install_root()
        .with_installed("0.4.0")
        .with_current("0.4.0", None)
        .with_bundle("0.4.1")
        .with_rpc_ok(TestEnv::win_endpoint(), "health.ping", "{\"ok\":true}")
        .with_rpc_ok(TestEnv::win_endpoint(), "mcu.status", "{\"flashing\":false,\"ok\":true}")
        .with_rpc_ok(
            TestEnv::win_endpoint(),
            "session.list",
            "{\"ok\":true,\"sessions\":[{\"com\":\"COM0\",\"state\":\"READY\",\"foreground_busy\":true,\"outstanding_commands\":1,\"interactive_session_id\":null,\"recovering\":false}]}",
        );
    let (rc, out, _) = t.run_from_bundle(&["setup"]);
    assert_eq!(rc, 2);
    assert_eq!(
        (s(&json(&out), "error_code"), s(&json(&out), "phase")),
        ("SESSION_BUSY", "update_safety_check")
    );
    assert!(t.service().actions().is_empty(), "不得停止監管");
}

#[test]
fn update_safety_probe_timeout_refuses() {
    let t = TestEnv::windows()
        .with_supervisor_state("windows-task", false, false)
        .with_install_root()
        .with_installed("0.4.0")
        .with_current("0.4.0", None)
        .with_bundle("0.4.1")
        .with_rpc_ok(TestEnv::win_endpoint(), "health.ping", "{\"ok\":true}")
        .with_rpc_ok(TestEnv::win_endpoint(), "mcu.status", "{\"flashing\":false,\"ok\":true}")
        .with_rpc_recv_timeout(TestEnv::win_endpoint(), "session.list");
    let (rc, out, _) = t.run_from_bundle(&["setup"]);
    assert_eq!(rc, 2);
    let j = json(&out);
    assert_eq!(
        (s(&j, "phase"), s(&j, "probe")),
        ("update_safety_check", "session.list")
    );
    assert!(t.service().actions().is_empty() && t.rpc().requests().iter().all(|r| r.1 != "daemon.stop"));
}

#[test]
fn update_force_does_not_override_flashing() {
    let t = TestEnv::windows()
        .with_supervisor_state("windows-task", false, false)
        .with_install_root()
        .with_installed("0.4.0")
        .with_current("0.4.0", None)
        .with_bundle("0.4.1")
        .with_rpc_ok(TestEnv::win_endpoint(), "health.ping", "{\"ok\":true}")
        .with_rpc_ok(TestEnv::win_endpoint(), "mcu.status", "{\"flashing\":true,\"ok\":true}")
        .with_rpc_ok(
            TestEnv::win_endpoint(),
            "session.list",
            "{\"ok\":true,\"sessions\":[{\"com\":\"COM0\",\"state\":\"FLASHING\",\"foreground_busy\":false,\"outstanding_commands\":0,\"interactive_session_id\":null,\"recovering\":false}]}",
        );
    let (rc, out, err) = t.run_from_bundle(&["setup", "--force"]);
    assert_eq!(rc, 2);
    let j = json(&out);
    assert_eq!(
        s(&j, "message"),
        "flash 進行中，拒絕更新／回滾（--force 不適用，請待 flash 完成後重試）"
    );
    assert!(err.starts_with("serialwrap: setup failed: FLASHING_BUSY: "), "out={out} err={err}");
    assert_refused_untouched(&t, &j, "update", "FLASHING_BUSY", "0.4.0");
}

#[test]
fn rollback_force_does_not_override_flashing() {
    let t = TestEnv::windows()
        .with_supervisor_state("windows-task", false, false)
        .with_install_root()
        .with_installed("0.4.0")
        .with_installed("0.4.1")
        .with_current("0.4.1", Some("0.4.0"))
        .with_rpc_ok(TestEnv::win_endpoint(), "health.ping", "{\"ok\":true}")
        .with_rpc_ok(TestEnv::win_endpoint(), "mcu.status", "{\"flashing\":true,\"ok\":true}")
        .with_rpc_ok(
            TestEnv::win_endpoint(),
            "session.list",
            "{\"ok\":true,\"sessions\":[{\"com\":\"COM0\",\"state\":\"FLASHING\",\"foreground_busy\":false,\"outstanding_commands\":0,\"interactive_session_id\":null,\"recovering\":false}]}",
        );
    let (rc, out, _) = t.run(&["setup", "--rollback", "--force"]);
    assert_eq!(rc, 2);
    assert_refused_untouched(&t, &json(&out), "rollback", "FLASHING_BUSY", "0.4.1");
}

#[test]
fn update_force_does_not_override_file_transfer() {
    let t = TestEnv::windows()
        .with_supervisor_state("windows-task", false, false)
        .with_install_root()
        .with_installed("0.4.0")
        .with_current("0.4.0", None)
        .with_bundle("0.4.1")
        .with_rpc_ok(TestEnv::win_endpoint(), "health.ping", "{\"ok\":true}")
        .with_rpc_ok(TestEnv::win_endpoint(), "mcu.status", "{\"flashing\":false,\"ok\":true}")
        .with_rpc_ok(
            TestEnv::win_endpoint(),
            "session.list",
            "{\"ok\":true,\"sessions\":[{\"com\":\"COM0\",\"state\":\"READY\",\"foreground_busy\":true,\"outstanding_commands\":1,\"interactive_session_id\":null,\"recovering\":false}]}",
        );
    let (rc, out, _) = t.run_from_bundle(&["setup", "--force"]);
    assert_eq!(rc, 2);
    let j = json(&out);
    assert_refused_untouched(&t, &j, "update", "SESSION_BUSY", "0.4.0");
    assert_eq!(j["busy"][0]["com"], "COM0");
}

#[test]
fn rollback_force_does_not_override_file_transfer() {
    let t = TestEnv::windows()
        .with_supervisor_state("windows-task", false, false)
        .with_install_root()
        .with_installed("0.4.0")
        .with_installed("0.4.1")
        .with_current("0.4.1", Some("0.4.0"))
        .with_rpc_ok(TestEnv::win_endpoint(), "health.ping", "{\"ok\":true}")
        .with_rpc_ok(TestEnv::win_endpoint(), "mcu.status", "{\"flashing\":false,\"ok\":true}")
        .with_rpc_ok(
            TestEnv::win_endpoint(),
            "session.list",
            "{\"ok\":true,\"sessions\":[{\"com\":\"COM0\",\"state\":\"READY\",\"foreground_busy\":true,\"outstanding_commands\":1,\"interactive_session_id\":null,\"recovering\":false}]}",
        );
    let (rc, out, _) = t.run(&["setup", "--rollback", "--force"]);
    assert_eq!(rc, 2);
    assert_refused_untouched(&t, &json(&out), "rollback", "SESSION_BUSY", "0.4.1");
}

#[test]
fn update_file_push_after_last_probe() {
    let t = TestEnv::windows()
        .with_supervisor_state("windows-task", true, true)
        .with_install_root()
        .with_installed("0.4.0")
        .with_current("0.4.0", None)
        .with_bundle("0.4.1")
        .with_idle_daemon(TestEnv::win_endpoint(), 4242)
        .with_rpc_ok(TestEnv::win_endpoint(), "health.ping", "{\"ok\":true}")
        .with_rpc_ok(TestEnv::win_endpoint(), "mcu.status", "{\"flashing\":false,\"ok\":true}")
        .with_rpc_ok(TestEnv::win_endpoint(), "session.list", IDLE_LIST)
        .with_rpc_ok(
            TestEnv::win_endpoint(),
            "daemon.handoff_stop",
            "{\"ok\":false,\"error_code\":\"SESSION_BUSY\",\"busy\":[{\"com\":\"COM0\",\"state\":\"READY\",\"foreground_busy\":true,\"outstanding_commands\":0,\"interactive_session_id\":null,\"recovering\":false,\"ops\":[\"file.push\"]}]}",
        );
    let (rc, out, err) = t.run_from_bundle(&["setup"]);
    assert_eq!(rc, 2, "{out}");
    let j = json(&out);
    assert!(err.starts_with("serialwrap: setup failed: SESSION_BUSY: "));
    assert_handoff_refused(&t, &j, "update", "SESSION_BUSY", "0.4.0");
    assert_eq!(j["busy"][0]["ops"][0], "file.push");
    assert_eq!(rpc_params(&t, "daemon.handoff_stop")["reason"], "update");
}

#[test]
fn update_flash_races_prepare() {
    let t = TestEnv::windows()
        .with_supervisor_state("windows-task", true, true)
        .with_install_root()
        .with_installed("0.4.0")
        .with_installed("0.4.1")
        .with_current("0.4.1", Some("0.4.0"))
        .with_idle_daemon(TestEnv::win_endpoint(), 4242)
        .with_rpc_ok(TestEnv::win_endpoint(), "health.ping", "{\"ok\":true}")
        .with_rpc_ok(TestEnv::win_endpoint(), "mcu.status", "{\"flashing\":false,\"ok\":true}")
        .with_rpc_ok(TestEnv::win_endpoint(), "session.list", IDLE_LIST)
        .with_rpc_ok(
            TestEnv::win_endpoint(),
            "daemon.handoff_stop",
            "{\"ok\":false,\"error_code\":\"FLASHING_BUSY\",\"busy\":[{\"com\":\"COM0\",\"state\":\"FLASHING\",\"foreground_busy\":false,\"outstanding_commands\":0,\"interactive_session_id\":null,\"recovering\":false,\"ops\":[\"flash\"]}]}",
        );
    let (rc, out, _) = t.run(&["setup", "--rollback", "--force"]);
    assert_eq!(rc, 2, "{out}");
    let j = json(&out);
    assert_handoff_refused(&t, &j, "rollback", "FLASHING_BUSY", "0.4.1");
    assert_eq!(rpc_params(&t, "daemon.handoff_stop")["reason"], "rollback");
}

#[test]
fn update_handoff_unsupported_daemon_refuses_without_stop() {
    let t = TestEnv::windows()
        .with_supervisor_state("windows-task", true, true)
        .with_install_root()
        .with_installed("0.4.0")
        .with_current("0.4.0", None)
        .with_bundle("0.4.1")
        .with_idle_daemon(TestEnv::win_endpoint(), 4242)
        .with_rpc_ok(TestEnv::win_endpoint(), "health.ping", "{\"ok\":true}")
        .with_rpc_ok(TestEnv::win_endpoint(), "mcu.status", "{\"flashing\":false,\"ok\":true}")
        .with_rpc_ok(TestEnv::win_endpoint(), "session.list", IDLE_LIST)
        .with_rpc_ok(
            TestEnv::win_endpoint(),
            "daemon.handoff_stop",
            "{\"ok\":false,\"error_code\":\"METHOD_NOT_FOUND\"}",
        );
    let (rc, out, _) = t.run_from_bundle(&["setup"]);
    assert_eq!(rc, 2);
    let j = json(&out);
    assert_eq!(
        (
            s(&j, "error_code"),
            s(&j, "phase"),
            s(&j, "probe"),
            s(&j, "handoff_outcome")
        ),
        (
            "METHOD_NOT_FOUND",
            "update_safety_check",
            "daemon.handoff_stop",
            "refused"
        )
    );
    assert!(t.rpc().requests().iter().all(|r| r.1 != "daemon.stop"));
    assert!(!t.calls().iter().any(|c| c.get(1).map(String::as_str) == Some("/End")));
}

#[test]
fn pairing_failure_after_switch_reverts_to_complete_previous() {
    let t = TestEnv::windows()
        .with_supervisor_state("windows-task", false, false)
        .with_install_root()
        .with_installed("0.4.0")
        .with_current("0.4.0", None)
        .with_bundle("0.4.1")
        .with_new_daemon_reporting_version("0.4.0");
    let (rc, out, _) = t.run_from_bundle(&["setup"]);
    assert_eq!(rc, 2, "{out}");
    let j = json(&out);
    assert_eq!(
        (s(&j, "error_code"), s(&j, "phase"), s(&j, "reason")),
        ("INTERNAL_ERROR", "update_start_and_verify", "pairing_mismatch")
    );
    assert!(b(&dict(&j, "update"), "reverted"), "{out}");
    let cur = sw_cli::install_layout::read_current(&t.root()).unwrap().unwrap();
    assert_eq!(cur.bundle_version, "0.4.0");
    assert_eq!(
        t.entry_bytes("serialwrap.exe"),
        t.version_bytes("0.4.0", "bin/serialwrap.exe")
    );
    assert!(t.backup_dir_of(&j).join("failure.json").exists());
    assert!(!t.root().root.join("install-journal.json").exists());
}

#[test]
fn revert_rehands_daemon_that_appears_after_registration_restore() {
    let t = TestEnv::posix()
        .with_install_root()
        .with_installed("0.4.0")
        .with_current("0.4.0", None)
        .with_bundle("0.4.1")
        .with_systemd(true)
        .with_var("USER", "alice")
        .with_supervisor_state("systemd-user", true, true)
        .with_idle_daemon_default(4242)
        .with_restart_on_failure()
        .with_new_daemon_reporting_version("0.4.0")
        .with_instance_appears_at(
            "restore_supervisor_and_daemon:after_registration",
            6161,
            &[],
        );
    let (rc, out, _) = t.run_from_bundle(&["setup"]);
    assert_eq!(rc, 2, "{out}");
    assert!(b(&dict(&json(&out), "update"), "reverted"), "{out}");
    assert_eq!(handoffs(&t), 3, "恢復註冊後出現的 daemon 必須先重新交接");
    let events = t.event_order();
    assert!(
        events.index_of_nth("rpc:daemon.handoff_stop", 2)
            < events.index_of_nth("service:release_autostart:true", 1),
        "新 daemon 的交接必須先於解除 hold"
    );
    assert_eq!(
        sw_cli::install_layout::read_current(&t.root())
            .unwrap()
            .unwrap()
            .bundle_version,
        "0.4.0"
    );
    assert_eq!(t.service().hold_state_now(), HoldState::NotHeld);
}

#[test]
fn journal_persists_original_snapshot_before_disable() {
    let t = TestEnv::windows()
        .with_install_root()
        .with_installed("0.4.0")
        .with_current("0.4.0", None)
        .with_bundle("0.4.1")
        .with_supervisor_state("windows-task", true, true)
        .with_idle_daemon(TestEnv::win_endpoint(), 4242)
        .with_crash_at("stop_supervisor:before_effect");
    let cfg0 = t.config_bytes();
    assert_eq!(t.run_from_bundle(&["setup"]).0, 137);
    assert!(acts(&t).is_empty(), "crash 點之前不得有任何停用");
    let j = t.journal_json().expect("停用前 journal 必須已落盤");
    assert_eq!(j["step"], "stop_supervisor");
    let o = &j["original"];
    assert_eq!(o["current"]["bundle_version"], "0.4.0");
    assert_eq!(
        (
            &o["supervisor"]["mode"],
            &o["supervisor"]["enabled"],
            &o["supervisor"]["running"]
        ),
        (
            &serde_json::json!("windows-task"),
            &serde_json::json!(true),
            &serde_json::json!(true)
        )
    );
    assert_eq!(o["daemon"]["id"]["pid"], 4242);
    assert_eq!(o["daemon_start"], "supervised");
    assert_eq!(
        o["entries"][0]["sha256"],
        sha256_hex(&t.version_bytes("0.4.0", "bin/serialwrap.exe"))
    );
    assert_eq!(o["config_sha256"], sha256_hex(&cfg0));
    assert!(j["backup_dir"].is_null() && j["backup_manifest_sha256"].is_null());
    let sdir = std::path::PathBuf::from(j["snapshot_dir"].as_str().unwrap());
    assert!(
        sdir.join("registered.task.xml").exists()
            && sdir.join("config.yaml").exists()
            && sdir.join("entries.json").exists()
    );
}

#[test]
fn journal_recovery_rejects_missing_or_mismatched_snapshot_without_side_effects() {
    for (damage, expected_reason) in [
        ("missing", "snapshot_missing"),
        ("mismatch", "snapshot_mismatch"),
    ] {
        let t = TestEnv::windows()
            .with_install_root()
            .with_installed("0.4.0")
            .with_current("0.4.0", None)
            .with_bundle("0.4.1")
            .with_supervisor_state("windows-task", true, true)
            .with_idle_daemon(TestEnv::win_endpoint(), 4242)
            .with_crash_at("stop_supervisor:before_effect");
        assert_eq!(t.run_from_bundle(&["setup"]).0, 137, "{damage}");

        let journal_path = t.root().root.join("install-journal.json");
        let journal_before = std::fs::read(&journal_path).expect("journal must exist");
        let journal = serde_json::from_slice::<serde_json::Value>(&journal_before).unwrap();
        let snapshot_dir = std::path::PathBuf::from(journal["snapshot_dir"].as_str().unwrap());
        if damage == "missing" {
            std::fs::remove_dir_all(&snapshot_dir).expect("remove snapshot");
        } else {
            std::fs::write(snapshot_dir.join("config.yaml"), b"tampered: true\n")
                .expect("tamper snapshot");
        }
        let requests_before_recovery = t.rpc().requests().len();
        let actions_before_recovery = acts(&t);

        let recovered = t.recover().expect("residual journal");
        assert!(!recovered.ok, "{damage}: {recovered:?}");
        assert!(recovered.journal_kept, "{damage}: journal must remain");
        assert_eq!(recovered.reason.as_deref(), Some(expected_reason));
        assert_eq!(std::fs::read(&journal_path).unwrap(), journal_before, "{damage}: journal bytes changed");
        assert_eq!(acts(&t), actions_before_recovery, "{damage}: recovery must not mutate supervisor state");
        assert_eq!(t.rpc().requests().len(), requests_before_recovery, "{damage}: recovery must not send RPCs");
    }
}

#[test]
fn journal_recovery_restarts_original_on_demand_daemon_after_stop() {
    let t = TestEnv::windows()
        .with_install_root()
        .with_installed("0.4.0")
        .with_current("0.4.0", None)
        .with_bundle("0.4.1")
        .with_config("supervision_mode: on-demand\n")
        .with_idle_daemon(TestEnv::win_endpoint(), 4242)
        .with_new_daemon_reporting_version("0.4.1")
        .with_rpc_after_relaunch("health.status", "{\"ok\":true,\"pid\":5252,\"version\":\"0.4.0\",\"wal\":{\"healthy\":true}}")
        .with_crash_at("confirm_released:after_effect");
    let (cfg0, cur0) = (t.config_bytes(), std::fs::read(t.root().current_file).unwrap());
    assert_eq!(t.run_from_bundle(&["setup"]).0, 137);
    assert!(t.rpc().requests().iter().any(|r| r.1 == "daemon.handoff_stop"));
    assert!(t.rpc().requests().iter().all(|r| r.1 != "daemon.stop"));
    let mut t = t;
    t.clear_crash();
    let t = t.with_new_daemon_reporting_version("0.4.0");
    let rec = t.recover().expect("殘留 journal");
    assert!(rec.ok, "{:?}", rec.reason);
    assert_eq!(
        (rec.from_step, rec.backup_used, rec.journal_kept),
        ("confirm_released", false, false)
    );
    let sp = t.process().spawns();
    assert_eq!(sp.len(), 1, "停止前 daemon 健康在跑 → 必須還原 running");
    assert!(!sp[0].1.is_empty(), "啟動舊 daemon 必須傳入載入後的完整環境");
    assert_eq!(
        std::path::Path::new(&sp[0].0[0]),
        t.root().bin.join("serialwrapd.exe"),
        "以舊穩定入口啟動"
    );
    assert_eq!(rec.restored["daemon_version"], "0.4.0");
    assert!(acts(&t).is_empty(), "on-demand 不得動監管");
    assert_back_to_original(&t, &cfg0, &cur0);
}

#[test]
fn journal_recovery_restores_original_enabled_and_running_windows_task() {
    for (enabled, running) in [(true, true), (true, false), (false, false)] {
        let mut t = TestEnv::windows()
            .with_install_root()
            .with_installed("0.4.0")
        .with_current("0.4.0", None)
            .with_bundle("0.4.1")
            .with_supervisor_state("windows-task", enabled, running);
        if running {
            t = t.with_idle_daemon(TestEnv::win_endpoint(), 4242);
        }
        t = t.with_new_daemon_reporting_version("0.4.0");
        let mut t = t.with_crash_at("confirm_released:after_effect");
        assert_eq!(t.run_from_bundle(&["setup"]).0, 137);
        let before = acts(&t).len();
        t.clear_crash();
        let rec = t.recover().unwrap();
        assert!(rec.ok, "enabled={enabled} running={running}: {:?}", rec.reason);
        let after: Vec<String> = acts(&t)[before..].to_vec();
        assert_eq!(
            after.iter().any(|a| a == "set_enabled:true"),
            enabled,
            "enabled 必須回到快照值（原為 disabled 者不得被啟用）"
        );
        assert_eq!(
            after.iter().filter(|a| a.as_str() == "start").count(),
            running as usize,
            "running 必須回到快照值"
        );
        if running {
            assert!(
                t.event_order().index_of("service:set_enabled:true")
                    < t.event_order().index_of("service:start")
            );
        }
        assert_eq!(
            (
                &rec.restored["supervisor"]["enabled"],
                &rec.restored["supervisor"]["running"]
            ),
            (&serde_json::json!(enabled), &serde_json::json!(running))
        );
        assert!(t.journal_json().is_none());
    }
}

#[test]
fn journal_recovery_backup_started_never_uses_partial_backup() {
    let t = TestEnv::windows()
        .with_install_root()
        .with_installed("0.4.0")
        .with_current("0.4.0", None)
        .with_bundle("0.4.1")
        .with_supervisor_state("windows-task", true, true)
        .with_idle_daemon(TestEnv::win_endpoint(), 4242)
        .with_crash_at("backup_started:after_config_copy");
    let (cfg0, cur0) = (t.config_bytes(), std::fs::read(t.root().current_file).unwrap());
    assert_eq!(t.run_from_bundle(&["setup"]).0, 137);
    let j = t.journal_json().unwrap();
    assert_eq!(j["step"], "backup_started");
    assert!(j["backup_manifest_sha256"].is_null());
    let bdir = std::path::PathBuf::from(j["backup_dir"].as_str().unwrap());
    assert!(!bdir.exists(), "未提交的備份只能在 *.partial");
    let mut t = t;
    t.clear_crash();
    let t = t.with_new_daemon_reporting_version("0.4.0");
    let rec = t.recover().unwrap();
    assert!(rec.ok);
    assert_eq!((rec.from_step, rec.backup_used), ("backup_started", false));
    let registration_calls = t.calls().into_iter().filter(|call| call.join(" ").contains("/Create /XML")).collect::<Vec<_>>();
    assert!(
        // 以路徑元件比對（Windows 原生為 `\\` 分隔）：/XML 參數必須是 journal snapshot 內的 held XML。
        registration_calls.iter().all(|call| call.iter().any(|arg| {
            std::path::Path::new(arg).ends_with("install-journal.snapshot/registered.task.held.xml")
        })),
        "只能使用完整 journal snapshot 還原註冊，不得引用未完成備份：{registration_calls:?}"
    );
    assert!(
        std::path::PathBuf::from(format!("{}.incomplete", bdir.display())).exists(),
        "partial 備份改名保留為證據"
    );
    assert!(!bdir.exists());
    assert!(acts(&t).iter().any(|a| a == "start"));
    assert_back_to_original(&t, &cfg0, &cur0);
}

#[test]
fn switch_never_starts_before_backup_committed() {
    let t = TestEnv::windows()
        .with_install_root()
        .with_installed("0.4.0")
        .with_current("0.4.0", None)
        .with_bundle("0.4.1")
        .with_supervisor_state("windows-task", true, true)
        .with_idle_daemon(TestEnv::win_endpoint(), 4242)
        .with_crash_at("entries_switching:before_effect");
    assert_eq!(t.run_from_bundle(&["setup"]).0, 137);
    let j = t.journal_json().unwrap();
    assert_eq!(j["step"], "entries_switching");
    let bdir = std::path::PathBuf::from(j["backup_dir"].as_str().unwrap());
    let m = std::fs::read(bdir.join("manifest.json")).expect("切換前備份必須已提交");
    assert_eq!(j["backup_manifest_sha256"], sha256_hex(&m));
    assert!(!std::path::PathBuf::from(format!("{}.partial", bdir.display())).exists());
    assert_eq!(
        t.entry_bytes("serialwrap.exe"),
        t.version_bytes("0.4.0", "bin/serialwrap.exe"),
        "entries_switching 前入口不得變動"
    );
}

#[test]
fn journal_recovery_with_unverifiable_backup_keeps_journal() {
    let t = TestEnv::windows()
        .with_install_root()
        .with_installed("0.4.0")
        .with_current("0.4.0", None)
        .with_bundle("0.4.1")
        .with_supervisor_state("windows-task", true, true)
        .with_idle_daemon(TestEnv::win_endpoint(), 4242)
        .with_crash_at("entries_switching:after_first_rename");
    assert_eq!(t.run_from_bundle(&["setup"]).0, 137);
    let bdir = std::path::PathBuf::from(
        t.journal_json().unwrap()["backup_dir"]
            .as_str()
            .unwrap()
            .to_owned(),
    );
    t.write_file(&bdir.join("config.yaml"), b"tampered\n");
    let (j0, before) = (t.journal_bytes().unwrap(), acts(&t).len());
    let mut t = t;
    t.clear_crash();
    let (rc, out, _) = t.run_from_bundle(&["setup"]);
    assert_eq!(rc, 2);
    let o = json(&out);
    assert_eq!(
        (s(&o, "error_code"), s(&o, "phase"), s(&o, "reason")),
        ("INTERNAL_ERROR", "install_recover", "backup_incomplete")
    );
    assert_eq!(o["recovered_journal"]["journal_kept"], true);
    assert_eq!(
        t.journal_bytes().unwrap(),
        j0,
        "復原失敗時 journal 必須逐位元組保留"
    );
    assert_eq!(acts(&t).len(), before, "fail-closed：不得啟動任何版本");
    assert!(t.process().spawns().is_empty());
    assert!(
        t.calls().iter().all(|c| !c.join(" ").contains("/Create /XML")),
        "不得以未驗證備份還原"
    );
}

#[test]
fn journal_recovery_failure_keeps_journal_and_skips_op() {
    let t = TestEnv::windows()
        .with_install_root()
        .with_installed("0.4.0")
        .with_current("0.4.0", None)
        .with_bundle("0.4.1")
        .with_config("supervision_mode: on-demand\n")
        .with_idle_daemon(TestEnv::win_endpoint(), 4242)
        .with_new_daemon_reporting_version("0.4.1")
        .with_crash_at("confirm_released:after_effect");
    assert_eq!(t.run_from_bundle(&["setup"]).0, 137);
    let j0 = t.journal_bytes().unwrap();
    let mut t = t;
    t.clear_crash();
    let t = t.with_spawn_exits(1);
    let (rc, out, _) = t.run_from_bundle(&["setup"]);
    assert_eq!(rc, 2);
    let o = json(&out);
    assert_eq!(
        (s(&o, "error_code"), s(&o, "phase")),
        ("DAEMON_EXITED", "install_recover")
    );
    assert_eq!(o["recovered_journal"]["ok"], false);
    assert_eq!(t.journal_bytes().unwrap(), j0);
    assert!(o.get("update").is_none(), "復原失敗不得繼續本次 update");
    assert_eq!(
        t.rpc()
            .requests()
            .iter()
            .filter(|r| r.1 == "daemon.handoff_stop")
            .count(),
        1,
        "不得再跑一次 update 的停止流程"
    );
    assert!(t.rpc().requests().iter().all(|r| r.1 != "daemon.stop"));
    assert_eq!(
        sw_cli::install_layout::read_current(&t.root())
            .unwrap()
            .unwrap()
            .bundle_version,
        "0.4.0"
    );
}

#[test]
fn revert_refuses_while_new_daemon_pushing() {
    let t = TestEnv::windows()
        .with_install_root()
        .with_installed("0.4.0")
        .with_current("0.4.0", None)
        .with_bundle("0.4.1")
        .with_supervisor_state("windows-task", true, true)
        .with_idle_daemon(TestEnv::win_endpoint(), 4242)
        .with_new_daemon_reporting_version("0.4.0")
        .with_new_daemon_handoff_script(&[BUSY_PUSH]);
    let (cfg0, cur0) = (t.config_bytes(), std::fs::read(t.root().current_file).unwrap());
    let (rc, out, err) = t.run_from_bundle(&["setup"]);
    assert_eq!(rc, 2, "{out}");
    assert!(err.starts_with("serialwrap: setup failed: INTERNAL_ERROR: "));
    let j = json(&out);
    assert_eq!(
        (s(&j, "error_code"), s(&j, "phase"), s(&j, "reason")),
        ("INTERNAL_ERROR", "update_start_and_verify", "pairing_mismatch")
    );
    let u = &j["update"];
    assert_eq!(
        (&u["reverted"], &u["revert_pending"]),
        (&serde_json::json!(false), &serde_json::json!(true))
    );
    let re = &u["revert_error"];
    assert_eq!(
        (
            re["error_code"].as_str(),
            re["phase"].as_str(),
            re["handoff"].as_str()
        ),
        (
            Some("SESSION_BUSY"),
            Some("update_revert"),
            Some("refused")
        )
    );
    assert_eq!(re["busy"][0]["ops"][0], "file.push");
    assert_eq!(
        handoffs(&t),
        2,
        "步驟 3 停舊版 1 次＋步驟 7 停新版 1 次，不重試"
    );
    assert_eq!(
        rpc_params_nth(&t, "daemon.handoff_stop", 1)["reason"],
        "revert"
    );
    let o = t.event_order();
    assert!(
        o.index_of("service:start")
            < o.index_of_nth("rpc:daemon.handoff_stop", 1),
        "交接對象是步驟 6 啟動的新版"
    );
    assert!(
        o.index_of_nth("rpc:daemon.handoff_stop", 1)
            < o.index_of_nth("service:release_autostart:true", 1),
        "拒絕後撤銷 autostart hold 並還原 enabled"
    );
    assert_revert_deferred_untouched(&t, None, "0.4.1");
    assert_eq!(
        t.journal_json().unwrap()["step"],
        "start_and_verify",
        "交接被拒前不得推進到 revert_to_previous"
    );
    assert_eq!(
        serde_json::from_slice::<serde_json::Value>(
            &std::fs::read(t.backup_dir_of(&j).join("failure.json")).unwrap()
        )
        .unwrap()["revert_deferred"],
        true
    );
    let rec = t.recover().expect("殘留 journal");
    assert!(rec.ok, "{:?}", rec.reason);
    assert_eq!(
        (rec.from_step, rec.backup_used),
        ("start_and_verify", true)
    );
    assert_eq!(handoffs(&t), 3);
    assert_eq!(
        rpc_params_nth(&t, "daemon.handoff_stop", 2)["reason"],
        "revert"
    );
    assert!(methods(&t).iter().all(|m| m != "daemon.stop"));
    assert_back_to_original(&t, &cfg0, &cur0);
    assert_eq!(
        t.rpc_now(TestEnv::win_endpoint(), "health.status")["version"],
        "0.4.0"
    );
}

#[test]
fn journal_recovery_refuses_while_new_daemon_flashing() {
    let t = TestEnv::windows()
        .with_install_root()
        .with_installed("0.4.0")
        .with_current("0.4.0", None)
        .with_bundle("0.4.1")
        .with_supervisor_state("windows-task", true, true)
        .with_idle_daemon(TestEnv::win_endpoint(), 4242)
        .with_new_daemon_reporting_version("0.4.1")
        .with_crash_at("start_and_verify:after_effect");
    let (cfg0, cur0) = (t.config_bytes(), std::fs::read(t.root().current_file).unwrap());
    assert_eq!(t.run_from_bundle(&["setup"]).0, 137);
    let mut t = t;
    t.clear_crash();
    let grant = format!(r#"{{"ok":true,"stopping":true,"handoff_token":"{}"}}"#, &sha256_hex("handoff-recovery-4242")[..32]);
    t.rpc().respond_seq(TestEnv::win_endpoint(), "daemon.handoff_stop", &[BUSY_FLASH, BUSY_FLASH, &grant]);
    let j0 = t.journal_bytes().unwrap();
    assert_eq!(t.journal_json().unwrap()["step"], "start_and_verify");
    let h0 = handoffs(&t);
    let rec = t.recover().unwrap();
    assert!(!rec.ok && rec.journal_kept, "{rec:?}");
    assert_eq!(
        (rec.from_step, rec.error_code),
        ("start_and_verify", Some("FLASHING_BUSY"))
    );
    assert_eq!(
        (
            &rec.detail["handoff"],
            &rec.detail["busy"][0]["ops"][0]
        ),
        (
            &serde_json::json!("refused"),
            &serde_json::json!("flash")
        )
    );
    assert_eq!(handoffs(&t), h0 + 1);
    assert_eq!(
        rpc_params_nth(&t, "daemon.handoff_stop", h0)["reason"],
        "revert"
    );
    assert_revert_deferred_untouched(&t, Some(&j0), "0.4.1");
    let (rc, out, _) = t.run_from_bundle(&["setup"]);
    assert_eq!(rc, 2);
    let o = json(&out);
    assert_eq!(
        (s(&o, "error_code"), s(&o, "phase")),
        ("FLASHING_BUSY", "install_recover")
    );
    assert_eq!(
        (
            &o["recovered_journal"]["ok"],
            &o["recovered_journal"]["journal_kept"]
        ),
        (&serde_json::json!(false), &serde_json::json!(true))
    );
    assert_eq!(o["recovered_journal"]["detail"]["handoff"], "refused");
    assert!(o.get("update").is_none(), "復原未完成不得繼續本次 update");
    assert_revert_deferred_untouched(&t, Some(&j0), "0.4.1");
    let rec = t.recover().unwrap();
    assert!(rec.ok, "{:?}", rec.reason);
    assert!(methods(&t).iter().all(|m| m != "daemon.stop"));
    assert!(
        !t.calls()
            .iter()
            .any(|c| c.get(1).map(String::as_str) == Some("/End")),
        "閒置新版正常結束時不得硬停"
    );
    assert_back_to_original(&t, &cfg0, &cur0);
}

#[test]
fn update_interrupt_injection_every_point_recovers_to_previous() {
    for point in CRASH_POINTS {
        let t = TestEnv::windows()
            .with_install_root()
            .with_installed("0.4.0")
        .with_current("0.4.0", None)
            .with_bundle("0.4.1")
            .with_supervisor_state("windows-task", true, true)
            .with_idle_daemon(TestEnv::win_endpoint(), 4242)
            .with_new_daemon_reporting_version("0.4.1")
            .with_crash_at(point);
        let t = if point.starts_with("revert_to_previous:") {
            t.with_new_daemon_reporting_version("0.4.0")
        } else {
            t
        };
        let (cfg0, cur0) = (t.config_bytes(), std::fs::read(t.root().current_file).unwrap());
        assert_eq!(t.run_from_bundle(&["setup"]).0, 137, "{point}");
        let mut t = t;
        t.clear_crash();
        let t = t.with_new_daemon_reporting_version("0.4.0");
        let t = if *point == "start_and_verify:after_effect" || *point == "start_and_verify:started" {
            t.with_rpc_after_relaunch_seq("health.status", &[
                "{\"ok\":true,\"pid\":5252,\"version\":\"0.4.1\",\"wal\":{\"healthy\":true}}",
                "{\"ok\":true,\"pid\":5252,\"version\":\"0.4.0\",\"wal\":{\"healthy\":true}}",
            ])
        } else {
            t
        };
        match t.recover() {
            None => assert!(
                point.starts_with("lock:")
                    || point.starts_with("verify:")
                    || point.starts_with("safety_check:")
                    || *point == "revert_to_previous:after_effect",
                "{point}: 未完成的復原必須留下 journal"
            ),
            Some(rec) => assert!(rec.ok, "{point}: {rec:?}"),
        }
        assert_back_to_original(&t, &cfg0, &cur0);
        let st = t.service().query_now();
        assert_eq!(
            (st.enabled, st.running),
            (Some(true), true),
            "{point}: 監管必須回到原始 enabled／running"
        );
        assert_eq!(
            t.rpc_now(TestEnv::win_endpoint(), "health.status")["version"],
            "0.4.0",
            "{point}"
        );
        assert!(
            t.rpc().requests().iter().all(|r| r.1 != "daemon.stop"),
            "{point}: update、步驟 7 回退與 journal 復原皆不得送一般 daemon.stop（R2-F6）"
        );
    }
}

#[test]
fn crash_points_cover_every_journal_step() {
    for step in [
        "lock",
        "verify",
        "safety_check",
        "snapshot_taken",
        "stop_supervisor",
        "confirm_released",
        "backup_started",
        "backup_complete",
        "entries_switching",
        "start_and_verify",
        "revert_to_previous",
    ] {
        for edge in ["before_effect", "after_effect"] {
            assert!(
                CRASH_POINTS.contains(&format!("{step}:{edge}").as_str()),
                "{step}:{edge}"
            );
        }
    }
    assert!(
        CRASH_POINTS.contains(&"backup_started:after_config_copy")
            && CRASH_POINTS.contains(&"entries_switching:after_first_rename")
    );
    assert!(
        CRASH_POINTS.contains(&"start_and_verify:started"),
        "R2-F6：新版已對外服務、驗收前的中斷點"
    );
}

#[test]
fn update_systemd_exit1_after_handoff_not_relaunched() {
    let t = TestEnv::posix()
        .with_install_root()
        .with_installed("0.4.0")
        .with_current("0.4.0", None)
        .with_bundle("0.4.1")
        .with_systemd(true)
        .with_var("USER", "alice")
        .with_supervisor_state("systemd-user", true, true)
        .with_idle_daemon_default(4242)
        .with_restart_on_failure()
        .with_daemon_exit_code_after_handoff(1)
        .with_new_daemon_reporting_version("0.4.1");
    let (rc, out, _) = t.run_from_bundle(&["setup", "--user"]);
    assert_eq!(rc, 0, "{out}");
    assert_eq!(json(&out)["update"]["to_version"], "0.4.1");
    let o = t.event_order();
    assert!(
        o.index_of("journal:stop_supervisor")
            < o.index_of("service:hold_autostart"),
        "hold 前 journal 已記 original.autostart"
    );
    assert!(
        o.index_of("service:hold_autostart")
            < o.index_of("rpc:daemon.handoff_stop"),
        "交接前必須先抑制拉起（§12.1 步驟 3）"
    );
    assert!(
        o.index_of("journal:entries_switching")
            < o.index_of("service:release_autostart:true"),
        "切換完成前 hold 不得撤銷"
    );
    assert!(
        o.index_of("service:release_autostart:true")
            < o.index_of("service:start"),
        "撤銷 hold 後才由 updater 自己啟動新版"
    );
    assert_eq!(
        o.count_prefix("service:relaunch:"),
        0,
        "exit 1 後不得被 Restart=on-failure 拉起"
    );
    assert!(t.service().relaunches().is_empty());
    assert!(acts(&t).iter().all(|a| a != "hard_stop"), "不得硬停");
    assert_eq!(
        acts(&t).iter().filter(|a| a.as_str() == "start").count(),
        1,
        "只有步驟 6 啟動新版一次"
    );
    assert_eq!(handoffs(&t), 1);
    assert!(methods(&t).iter().all(|m| m != "daemon.stop"));
    assert_eq!(
        t.service().hold_state_now(),
        HoldState::NotHeld,
        "完成後必須恢復原設定"
    );
    let st = t.service().query_now();
    assert_eq!((st.enabled, st.running), (Some(true), true));
    let steps = json(&out)["update"]["steps"].as_array().unwrap().clone();
    let ss = steps.iter().find(|x| x["step"] == "stop_supervisor").unwrap();
    assert_eq!(ss["detail"]["autostart_hold"]["kind"], "systemd_dropin");
    assert_eq!(ss["detail"]["autostart_hold"]["prior_restart"], "on-failure");
    assert_eq!(
        sw_cli::install_layout::read_current(&t.root())
            .unwrap()
            .unwrap()
            .bundle_version,
        "0.4.1"
    );
}

#[test]
fn update_windows_task_exit1_after_handoff_not_relaunched() {
    let t = TestEnv::windows()
        .with_install_root()
        .with_installed("0.4.0")
        .with_current("0.4.0", None)
        .with_bundle("0.4.1")
        .with_supervisor_state("windows-task", true, true)
        .with_idle_daemon(TestEnv::win_endpoint(), 4242)
        .with_restart_on_failure()
        .with_daemon_exit_code_after_handoff(1)
        .with_new_daemon_reporting_version("0.4.1");
    let (rc, out, _) = t.run_from_bundle(&["setup"]);
    assert_eq!(rc, 0, "{out}");
    let o = t.event_order();
    assert_eq!(o.count_prefix("service:relaunch:"), 0);
    assert!(o.index_of("service:install") < o.index_of("service:release_autostart:true"));
    let xml = String::from_utf16(&utf16le_body(
        &std::fs::read(t.root().supervisor.join("serialwrap.task.xml")).unwrap(),
    ))
    .unwrap();
    assert!(
        xml.contains("<AllowStartOnDemand>true</AllowStartOnDemand>\r\n    <Enabled>false</Enabled>"),
        "hold 期間以停用狀態註冊"
    );
    assert!(acts(&t).iter().all(|a| a != "hard_stop"));
    assert_eq!(t.service().hold_state_now(), HoldState::NotHeld);
}

#[test]
fn update_relaunched_instance_during_backup_rehandoff_refuses_flash() {
    let t = TestEnv::windows()
        .with_install_root()
        .with_installed("0.4.0")
        .with_current("0.4.0", None)
        .with_bundle("0.4.1")
        .with_config("supervision_mode: on-demand\n")
        .with_idle_daemon(TestEnv::win_endpoint(), 4242)
        .with_instance_appears_at("backup_started:after_config_copy", 5151, &["flash"]);
    let (cfg0, cur0) = (t.config_bytes(), std::fs::read(t.root().current_file).unwrap());
    let (rc, out, err) = t.run_from_bundle(&["setup"]);
    assert_eq!(rc, 2, "{out}");
    assert!(err.starts_with("serialwrap: setup failed: FLASHING_BUSY: "), "out={out} err={err}");
    let j = json(&out);
    assert_eq!(
        (s(&j, "error_code"), s(&j, "phase"), s(&j, "handoff")),
        ("FLASHING_BUSY", "update_confirm_released", "refused")
    );
    assert_eq!(
        (j["relaunched"]["pid"].as_i64(), j["busy"][0]["ops"][0].as_str()),
        (Some(5151), Some("flash"))
    );
    assert_eq!(handoffs(&t), 2, "舊實例 1 次＋新實例 1 次（新 token）");
    assert_eq!(
        (
            rpc_params_nth(&t, "daemon.handoff_stop", 0)["reason"].as_str(),
            rpc_params_nth(&t, "daemon.handoff_stop", 1)["reason"].as_str()
        ),
        (Some("update"), Some("update"))
    );
    assert!(methods(&t).iter().all(|m| m != "daemon.stop"));
    assert!(t.process().spawns().is_empty(), "不得啟動任何版本");
    assert_eq!(
        t.rpc_now(TestEnv::win_endpoint(), "health.status")["pid"],
        5151,
        "新實例繼續服務、flash 未被截斷"
    );
    let o = t.event_order();
    assert_eq!(o.count_prefix("journal:entries_switching"), 0, "切換從未開始");
    assert_eq!(o.count_prefix("service:"), 0, "on-demand 沒有 supervisor，不呼叫 service API");
    assert!(!t.autostart_lock_held(), "autostart.lock 已釋放");
    assert_back_to_original(&t, &cfg0, &cur0);
}

#[test]
fn journal_recovery_releases_persistent_systemd_hold() {
    let t = TestEnv::posix()
        .with_install_root()
        .with_installed("0.4.0")
        .with_current("0.4.0", None)
        .with_bundle("0.4.1")
        .with_systemd(true)
        .with_var("USER", "alice")
        .with_supervisor_state("systemd-user", true, true)
        .with_idle_daemon_default(4242)
        .with_restart_on_failure()
        .with_crash_at("backup_complete:after_effect");
    assert_eq!(t.run_from_bundle(&["setup"]).0, 137);
    assert_eq!(
        t.service().hold_state_now(),
        HoldState::Held,
        "updater 被殺後 hold 仍有效"
    );
    assert_eq!(
        t.service()
            .action(&SupervisionMode::SystemdUser, ServiceAction::Start, false)
            .ok,
        false,
        "RefuseManualStart：外部 start 被拒"
    );
    assert!(t.service().relaunches().is_empty());
    let mut t = t;
    t.clear_crash();
    let t = t.with_new_daemon_reporting_version("0.4.0");
    let rec = t.recover().expect("殘留 journal");
    assert!(rec.ok, "{:?}", rec.reason);
    assert_eq!((rec.from_step, rec.backup_used), ("backup_complete", false));
    let o = t.event_order();
    assert!(
        o.index_of("service:release_autostart:true") < o.index_of_nth("service:start", 1),
        "先撤銷 hold 再還原 running"
    );
    assert_eq!(t.service().hold_state_now(), HoldState::NotHeld);
    assert_eq!(
        t.rpc_now(&sys_ep(&t), "health.status")["version"],
        "0.4.0"
    );
    assert!(t.journal_json().is_none());
}

#[test]
fn update_happy_path_steps_in_order() {
    let t = TestEnv::windows()
        .with_install_root()
        .with_installed("0.4.0")
        .with_current("0.4.0", None)
        .with_bundle("0.4.1")
        .with_supervisor_state("windows-task", true, true)
        .with_idle_daemon(TestEnv::win_endpoint(), 4242)
        .with_new_daemon_reporting_version("0.4.1");
    let (rc, out, _) = t.run_from_bundle(&["setup"]);
    assert_eq!(rc, 0, "{out}");
    let j = json(&out);
    let step_names: Vec<&str> = j["update"]["steps"]
        .as_array()
        .unwrap()
        .iter()
        .map(|s| s["step"].as_str().unwrap())
        .collect();
    assert_eq!(
        step_names,
        vec![
            "lock",
            "verify",
            "safety_check",
            "stop_supervisor",
            "confirm_released",
            "backup_and_switch",
            "start_and_verify"
        ]
    );
    assert_eq!(j["update"]["to_version"], "0.4.1");
    assert!(t.journal_json().is_none());
}

#[test]
fn update_lock_contention_no_side_effects() {
    let t = TestEnv::windows()
        .with_process()
        .with_install_root()
        .with_installed("0.4.0")
        .with_current("0.4.0", None)
        .with_bundle("0.4.1")
        .with_install_lock_holder();
    let (rc, out, _) = t.run_from_bundle(&["setup"]);
    assert_eq!(rc, 2);
    let j = json(&out);
    assert_eq!((s(&j, "error_code"), s(&j, "phase")), ("TIMEOUT", "install_lock"));
    assert!(t.rpc().requests().is_empty());
}

#[test]
fn update_lock_contention_does_not_recover_residual_journal() {
    let mut t = TestEnv::windows()
        .with_install_root()
        .with_installed("0.4.0")
        .with_current("0.4.0", None)
        .with_bundle("0.4.1")
        .with_crash_at("lock:after_effect");
    assert_eq!(t.run_from_bundle(&["setup"]).0, 137);
    let journal_before = t.journal_bytes().expect("中斷後應保留 journal");
    t.clear_crash();
    t.acquire_install_lock();

    let (rc, out, _) = t.run_from_bundle(&["setup"]);

    assert_eq!(rc, 2, "{out}");
    assert_eq!(s(&json(&out), "phase"), "install_lock");
    assert_eq!(t.journal_bytes().as_deref(), Some(journal_before.as_slice()));
}

#[test]
fn update_journal_persists_systemd_hold_plan_before_hold_side_effect() {
    let t = TestEnv::posix()
        .with_process()
        .with_install_root()
        .with_installed("0.4.0")
        .with_current("0.4.0", None)
        .with_bundle("0.4.1")
        .with_idle_daemon_default(4242)
        .with_supervisor_state("systemd-user", true, true)
        .with_command(
            &["systemctl", "--user", "show", "serialwrap", "-p", "Restart", "--value"],
            0,
            "on-failure\n",
            "",
        )
        .with_crash_at("stop_supervisor:after_hold");

    assert_eq!(t.run_from_bundle(&["setup"]).0, 137);

    let journal = t.journal_json().expect("hold 中斷後 journal 必須存在");
    assert_eq!(s(&journal, "step"), "stop_supervisor");
    let original = dict(&journal, "original");
    let autostart = dict(original, "autostart");
    assert_eq!(s(autostart, "kind"), "systemd_dropin");
    assert!(s(autostart, "dropin_path").ends_with("serialwrap.service.d/90-serialwrap-update-hold.conf"));
    assert_eq!(s(autostart, "prior_restart"), "on-failure");
}

#[test]
fn update_verify_failed_no_side_effects() {
    let t = TestEnv::windows()
        .with_install_root()
        .with_installed("0.4.0")
        .with_current("0.4.0", None)
        .with_bundle("0.4.1");
    t.write_file(&t.bundle_dir().join("bin").join("serialwrap"), b"tampered\n");
    let (rc, out, _) = t.run_from_bundle(&["setup"]);
    assert_eq!(rc, 2);
    let j = json(&out);
    assert_eq!(
        (s(&j, "error_code"), s(&j, "phase")),
        ("CHECKSUM_VERIFY_FAILED", "update_verify")
    );
    assert!(t.rpc().requests().is_empty());
    assert!(!t.root().versions.join("0.4.1").exists());
}

#[test]
fn update_refuses_incompatible_state_schema() {
    let t = TestEnv::windows()
        .with_supervisor_state("windows-task", false, false)
        .with_install_root()
        .with_installed("0.4.0")
        .with_current("0.4.0", None)
        .with_bundle("0.4.1");
    let manifest_path = t.bundle_dir().join("release-manifest.json");
    let manifest = std::fs::read_to_string(&manifest_path)
        .unwrap()
        .replace("python-state-v0", "python-state-v1");
    t.write_file(&manifest_path, manifest.as_bytes());
    let (rc, out, _) = t.run_from_bundle(&["setup"]);
    assert_eq!(rc, 2);
    let j = json(&out);
    assert_eq!(
        (s(&j, "error_code"), s(&j, "phase"), s(&j, "reason")),
        ("INVALID_ARGS", "update_verify", "state_schema_incompatible")
    );
    assert!(t.rpc().requests().is_empty());
}

#[test]
fn update_flashing_busy() {
    let t = TestEnv::windows()
        .with_supervisor_state("windows-task", false, false)
        .with_install_root()
        .with_installed("0.4.0")
        .with_current("0.4.0", None)
        .with_bundle("0.4.1")
        .with_rpc_ok(TestEnv::win_endpoint(), "health.ping", "{\"ok\":true}")
        .with_rpc_ok(TestEnv::win_endpoint(), "mcu.status", "{\"flashing\":true,\"ok\":true}")
        .with_rpc_ok(
            TestEnv::win_endpoint(),
            "session.list",
            "{\"ok\":true,\"sessions\":[{\"com\":\"COM0\",\"state\":\"FLASHING\",\"foreground_busy\":false,\"outstanding_commands\":0,\"interactive_session_id\":null,\"recovering\":false}]}",
        );
    let (rc, out, _) = t.run_from_bundle(&["setup"]);
    assert_eq!(rc, 2);
    let j = json(&out);
    assert_eq!(
        (s(&j, "error_code"), s(&j, "phase")),
        ("FLASHING_BUSY", "update_safety_check")
    );
    assert_eq!(
        s(&j, "message"),
        "flash 進行中，拒絕更新／回滾（--force 不適用，請待 flash 完成後重試）"
    );
}

#[test]
fn update_busy_background_command() {
    let t = TestEnv::windows()
        .with_supervisor_state("windows-task", false, false)
        .with_install_root()
        .with_installed("0.4.0")
        .with_current("0.4.0", None)
        .with_bundle("0.4.1")
        .with_rpc_ok(TestEnv::win_endpoint(), "health.ping", "{\"ok\":true}")
        .with_rpc_ok(TestEnv::win_endpoint(), "mcu.status", "{\"flashing\":false,\"ok\":true}")
        .with_rpc_ok(
            TestEnv::win_endpoint(),
            "session.list",
            "{\"ok\":true,\"sessions\":[{\"com\":\"COM0\",\"state\":\"READY\",\"foreground_busy\":false,\"outstanding_commands\":2,\"interactive_session_id\":null,\"recovering\":false}]}",
        );
    let (rc, out, _) = t.run_from_bundle(&["setup"]);
    assert_eq!(rc, 2);
    assert_eq!(
        (s(&json(&out), "error_code"), s(&json(&out), "phase")),
        ("SESSION_BUSY", "update_safety_check")
    );
}

#[test]
fn update_busy_interactive_lease() {
    let t = TestEnv::windows()
        .with_supervisor_state("windows-task", false, false)
        .with_install_root()
        .with_installed("0.4.0")
        .with_current("0.4.0", None)
        .with_bundle("0.4.1")
        .with_rpc_ok(TestEnv::win_endpoint(), "health.ping", "{\"ok\":true}")
        .with_rpc_ok(TestEnv::win_endpoint(), "mcu.status", "{\"flashing\":false,\"ok\":true}")
        .with_rpc_ok(
            TestEnv::win_endpoint(),
            "session.list",
            "{\"ok\":true,\"sessions\":[{\"com\":\"COM0\",\"state\":\"READY\",\"foreground_busy\":false,\"outstanding_commands\":0,\"interactive_session_id\":\"L1\",\"recovering\":false}]}",
        );
    let (rc, out, _) = t.run_from_bundle(&["setup"]);
    assert_eq!(rc, 2);
    assert_eq!(
        (s(&json(&out), "error_code"), s(&json(&out), "phase")),
        ("SESSION_BUSY", "update_safety_check")
    );
}

#[test]
fn update_busy_recovering() {
    let t = TestEnv::windows()
        .with_supervisor_state("windows-task", false, false)
        .with_install_root()
        .with_installed("0.4.0")
        .with_current("0.4.0", None)
        .with_bundle("0.4.1")
        .with_rpc_ok(TestEnv::win_endpoint(), "health.ping", "{\"ok\":true}")
        .with_rpc_ok(TestEnv::win_endpoint(), "mcu.status", "{\"flashing\":false,\"ok\":true}")
        .with_rpc_ok(
            TestEnv::win_endpoint(),
            "session.list",
            "{\"ok\":true,\"sessions\":[{\"com\":\"COM0\",\"state\":\"RECOVERING\",\"foreground_busy\":false,\"outstanding_commands\":0,\"interactive_session_id\":null,\"recovering\":true}]}",
        );
    let (rc, out, _) = t.run_from_bundle(&["setup"]);
    assert_eq!(rc, 2);
    assert_eq!(
        (s(&json(&out), "error_code"), s(&json(&out), "phase")),
        ("SESSION_BUSY", "update_safety_check")
    );
}

#[test]
fn update_safety_malformed_session_list_refuses() {
    let t = TestEnv::windows()
        .with_supervisor_state("windows-task", false, false)
        .with_install_root()
        .with_installed("0.4.0")
        .with_current("0.4.0", None)
        .with_bundle("0.4.1")
        .with_rpc_ok(TestEnv::win_endpoint(), "health.ping", "{\"ok\":true}")
        .with_rpc_ok(TestEnv::win_endpoint(), "mcu.status", "{\"flashing\":false,\"ok\":true}")
        .with_rpc_ok(TestEnv::win_endpoint(), "session.list", "{\"ok\":true}");
    let (rc, out, _) = t.run_from_bundle(&["setup"]);
    assert_eq!(rc, 2);
    let j = json(&out);
    assert_eq!(
        (s(&j, "error_code"), s(&j, "phase"), s(&j, "probe")),
        ("INVALID_RESPONSE", "update_safety_check", "session.list")
    );
}

#[test]
fn update_busy_appears_after_disable_aborts() {
    let t = TestEnv::windows()
        .with_config("supervision_mode: windows-task\n")
        .with_install_root()
        .with_installed("0.4.0")
        .with_current("0.4.0", None)
        .with_bundle("0.4.1")
        .with_supervisor_state("windows-task", true, true)
        .with_idle_daemon(TestEnv::win_endpoint(), 4242)
        .with_rpc_ok(TestEnv::win_endpoint(), "health.ping", "{\"ok\":true}")
        .with_rpc_ok(TestEnv::win_endpoint(), "mcu.status", "{\"flashing\":false,\"ok\":true}")
        .with_rpc_ok(TestEnv::win_endpoint(), "session.list", IDLE_LIST)
        .with_rpc_ok(
            TestEnv::win_endpoint(),
            "daemon.handoff_stop",
            "{\"ok\":false,\"error_code\":\"SESSION_BUSY\",\"busy\":[{\"com\":\"COM0\",\"state\":\"READY\",\"foreground_busy\":true,\"outstanding_commands\":0,\"interactive_session_id\":null,\"recovering\":false,\"ops\":[\"command.submit\"]}]}",
        );
    let (rc, out, _) = t.run_from_bundle(&["setup"]);
    assert_eq!(rc, 2);
    let j = json(&out);
    assert_eq!(
        (s(&j, "error_code"), s(&j, "phase"), s(&j, "handoff")),
        ("SESSION_BUSY", "update_safety_check", "refused")
    );
    assert!(t.event_order().index_of("service:release_autostart:true") > 0);
    assert_eq!(t.service().query_now().enabled, Some(true));
    assert!(t.rpc().requests().iter().all(|r| r.1 != "daemon.stop"));
    assert!(!t.calls().iter().any(|c| c.get(1).map(String::as_str) == Some("/End")));
    assert!(!t.root().root.join("install-journal.json").exists());
}

#[test]
fn update_handoff_recv_timeout_no_hard_stop() {
    let t = TestEnv::windows()
        .with_config("supervision_mode: windows-task\n")
        .with_install_root()
        .with_installed("0.4.0")
        .with_current("0.4.0", None)
        .with_bundle("0.4.1")
        .with_supervisor_state("windows-task", true, true)
        .with_idle_daemon(TestEnv::win_endpoint(), 4242)
        .with_rpc_ok(TestEnv::win_endpoint(), "health.ping", "{\"ok\":true}")
        .with_rpc_ok(TestEnv::win_endpoint(), "mcu.status", "{\"flashing\":false,\"ok\":true}")
        .with_rpc_ok(TestEnv::win_endpoint(), "session.list", IDLE_LIST)
        .with_rpc_recv_timeout(TestEnv::win_endpoint(), "daemon.handoff_stop");
    let (rc, out, _) = t.run_from_bundle(&["setup"]);
    assert_eq!(rc, 2);
    let j = json(&out);
    assert_eq!(
        (s(&j, "error_code"), s(&j, "phase"), s(&j, "reason"), s(&j, "handoff_outcome")),
        ("DEVICE_STILL_HELD", "update_safety_check", "daemon_presence_unknown", "unknown")
    );
    assert_eq!(j["pids"][0], 4242);
    assert!(j["hint"].as_str().unwrap().contains("serialwrap daemon status"));
    assert!(j["hint"].as_str().unwrap().contains("serialwrap daemon stop"));
    assert!(t.event_order().index_of("service:release_autostart:true") > 0);
    assert_eq!(t.service().query_now().enabled, Some(true));
    assert!(!t.calls().iter().any(|c| c.get(1).map(String::as_str) == Some("/End")));
    assert!(t.rpc().requests().iter().all(|r| r.1 != "daemon.stop"));
}

#[test]
fn update_handoff_malformed_grant_refuses() {
    let t = TestEnv::windows()
        .with_config("supervision_mode: windows-task\n")
        .with_install_root()
        .with_installed("0.4.0")
        .with_current("0.4.0", None)
        .with_bundle("0.4.1")
        .with_idle_daemon(TestEnv::win_endpoint(), 4242)
        .with_rpc_ok(TestEnv::win_endpoint(), "health.ping", "{\"ok\":true}")
        .with_rpc_ok(TestEnv::win_endpoint(), "mcu.status", "{\"flashing\":false,\"ok\":true}")
        .with_rpc_ok(TestEnv::win_endpoint(), "session.list", IDLE_LIST)
        .with_rpc_ok(TestEnv::win_endpoint(), "daemon.handoff_stop", "{\"ok\":true,\"stopping\":true}");
    let (rc, out, _) = t.run_from_bundle(&["setup"]);
    assert_eq!(rc, 2);
    let j = json(&out);
    assert_eq!(
        (s(&j, "error_code"), s(&j, "probe")),
        ("INVALID_RESPONSE", "daemon.handoff_stop")
    );
    assert!(!t.calls().iter().any(|c| c.get(1).map(String::as_str) == Some("/End")));
}

#[test]
fn update_never_sends_plain_daemon_stop() {
    let t = TestEnv::windows()
        .with_install_root()
        .with_installed("0.4.0")
        .with_current("0.4.0", None)
        .with_bundle("0.4.1")
        .with_supervisor_state("windows-task", true, true)
        .with_idle_daemon(TestEnv::win_endpoint(), 4242)
        .with_new_daemon_reporting_version("0.4.1");
    let (rc, out, _) = t.run_from_bundle(&["setup"]);
    assert_eq!(rc, 0, "{out}");
    assert!(methods(&t).iter().all(|m| m != "daemon.stop"));
    assert_eq!(handoffs(&t), 1);
    let j = json(&out);
    let ss = j["update"]["steps"]
        .as_array()
        .unwrap()
        .iter()
        .find(|s| s["step"] == "stop_supervisor")
        .unwrap();
    assert!(ss["detail"].get("handoff_token").is_some());
}

#[test]
fn update_rpc_down_child_holds_com_blocks() {
    let t = TestEnv::windows()
        .with_install_root()
        .with_installed("0.4.0")
        .with_current("0.4.0", None)
        .with_bundle("0.4.1")
        .with_rpc_refused(TestEnv::win_endpoint())
        .with_procs(vec![DaemonProc::win(4242, TestEnv::win_endpoint())]);
    let (rc, out, _) = t.run_from_bundle(&["setup"]);
    assert_eq!(rc, 2);
    let j = json(&out);
    assert_eq!(
        (s(&j, "error_code"), s(&j, "phase")),
        ("DEVICE_STILL_HELD", "update_safety_check")
    );
    assert_eq!(j["pids"][0], 4242);
}

#[test]
fn update_identity_unknown_blocks() {
    let t = TestEnv::windows()
        .with_install_root()
        .with_installed("0.4.0")
        .with_current("0.4.0", None)
        .with_bundle("0.4.1")
        .with_rpc_ok(TestEnv::win_endpoint(), "health.ping", "{\"ok\":true}")
        .with_rpc_ok(TestEnv::win_endpoint(), "mcu.status", "{\"ok\":true,\"flashing\":false}")
        .with_rpc_ok(TestEnv::win_endpoint(), "session.list", IDLE_LIST);
    t.procs().fail();
    let (rc, out, _) = t.run_from_bundle(&["setup"]);
    assert_eq!(rc, 2);
    let j = json(&out);
    assert_eq!(
        (s(&j, "error_code"), s(&j, "phase")),
        ("DEVICE_STILL_HELD", "update_safety_check")
    );
    assert_eq!(j["holder_unknown"], true);
    assert!(t.rpc().requests().iter().all(|r| r.1 != "daemon.stop"));
}

#[test]
fn update_force_does_not_override_interactive_or_recovering() {
    for (key, val) in [("interactive_session_id", "\"L1\""), ("state", "\"RECOVERING\"")] {
        let t = TestEnv::windows()
            .with_supervisor_state("windows-task", false, false)
            .with_install_root()
            .with_installed("0.4.0")
        .with_current("0.4.0", None)
            .with_bundle("0.4.1")
            .with_rpc_ok(TestEnv::win_endpoint(), "health.ping", "{\"ok\":true}")
            .with_rpc_ok(TestEnv::win_endpoint(), "mcu.status", "{\"flashing\":false,\"ok\":true}")
            .with_rpc_ok(
                TestEnv::win_endpoint(),
                "session.list",
                &format!("{{\"ok\":true,\"sessions\":[{{\"com\":\"COM0\",\"{key}\":{val},\"foreground_busy\":false,\"outstanding_commands\":0,\"recovering\":false}}]}}"),
            );
        let (rc, out, _) = t.run_from_bundle(&["setup", "--force"]);
        assert_eq!(rc, 2);
        assert_eq!(
            (s(&json(&out), "error_code"), s(&json(&out), "phase")),
            ("SESSION_BUSY", "update_safety_check"),
            "{out}; requests={:?}",
            t.rpc().requests()
        );
        assert!(t.service().actions().is_empty());
        assert!(t.rpc().requests().iter().all(|r| r.1 != "daemon.stop"));
    }
}

#[test]
fn update_force_does_not_skip_identity_or_probe() {
    let t = TestEnv::windows()
        .with_supervisor_state("windows-task", false, false)
        .with_install_root()
        .with_installed("0.4.0")
        .with_current("0.4.0", None)
        .with_bundle("0.4.1")
        .with_rpc_ok(TestEnv::win_endpoint(), "health.ping", "{\"ok\":true}")
        .with_rpc_ok(TestEnv::win_endpoint(), "mcu.status", "{\"ok\":true,\"flashing\":false}")
        .with_rpc_ok(TestEnv::win_endpoint(), "session.list", IDLE_LIST);
    t.procs().fail();
    let (rc, out, _) = t.run_from_bundle(&["setup", "--force"]);
    assert_eq!(rc, 2);
    assert_eq!(s(&json(&out), "error_code"), "DEVICE_STILL_HELD", "{out}");

    let t2 = TestEnv::windows()
        .with_supervisor_state("windows-task", false, false)
        .with_install_root()
        .with_installed("0.4.0")
        .with_bundle("0.4.1")
        .with_rpc_ok(TestEnv::win_endpoint(), "health.ping", "{\"ok\":true}")
        .with_rpc_ok(TestEnv::win_endpoint(), "mcu.status", "{\"ok\":true,\"flashing\":false}")
        .with_rpc_recv_timeout(TestEnv::win_endpoint(), "session.list");
    let (rc2, out2, _) = t2.run_from_bundle(&["setup", "--force"]);
    assert_eq!(rc2, 2);
    assert_eq!(s(&json(&out2), "probe"), "session.list");
}

#[test]
fn update_force_recheck_after_disable_still_aborts() {
    let t = TestEnv::windows()
        .with_config("supervision_mode: windows-task\n")
        .with_install_root()
        .with_installed("0.4.0")
        .with_current("0.4.0", None)
        .with_bundle("0.4.1")
        .with_supervisor_state("windows-task", true, true)
        .with_idle_daemon(TestEnv::win_endpoint(), 4242)
        .with_rpc_ok(TestEnv::win_endpoint(), "health.ping", "{\"ok\":true}")
        .with_rpc_ok(TestEnv::win_endpoint(), "mcu.status", "{\"flashing\":false,\"ok\":true}")
        .with_rpc_ok(TestEnv::win_endpoint(), "session.list", IDLE_LIST)
        .with_rpc_ok(
            TestEnv::win_endpoint(),
            "daemon.handoff_stop",
            "{\"ok\":false,\"error_code\":\"SESSION_BUSY\",\"busy\":[{\"com\":\"COM0\",\"state\":\"READY\",\"foreground_busy\":true,\"outstanding_commands\":0,\"interactive_session_id\":null,\"recovering\":false,\"ops\":[\"command.submit\"]}]}",
        );
    let (rc, out, _) = t.run_from_bundle(&["setup", "--force"]);
    assert_eq!(rc, 2);
    assert_eq!(s(&json(&out), "error_code"), "SESSION_BUSY");
    assert!(acts(&t).iter().any(|a| a == "release_autostart:true"));
    assert!(t.rpc().requests().iter().all(|r| r.1 != "daemon.stop"));
    assert!(!t.calls().iter().any(|c| c.get(1).map(String::as_str) == Some("/End")));
}

#[test]
fn update_force_idle_same_result_as_without() {
    let t1 = TestEnv::windows()
        .with_install_root()
        .with_installed("0.4.0")
        .with_current("0.4.0", None)
        .with_bundle("0.4.1")
        .with_supervisor_state("windows-task", true, true)
        .with_idle_daemon(TestEnv::win_endpoint(), 4242)
        .with_new_daemon_reporting_version("0.4.1");
    let (_, out1, _) = t1.run_from_bundle(&["setup"]);

    let t2 = TestEnv::windows()
        .with_install_root()
        .with_installed("0.4.0")
        .with_current("0.4.0", None)
        .with_bundle("0.4.1")
        .with_supervisor_state("windows-task", true, true)
        .with_idle_daemon(TestEnv::win_endpoint(), 4242)
        .with_new_daemon_reporting_version("0.4.1");
    let (_, out2, _) = t2.run_from_bundle(&["setup", "--force"]);

    let j1 = json(&out1);
    let j2 = json(&out2);
    assert_eq!(j1["ok"], j2["ok"]);
    assert_eq!(
        j2["update"]["steps"]
            .as_array()
            .unwrap()
            .iter()
            .find(|s| s["step"] == "safety_check")
            .unwrap()["detail"]["force_ignored"],
        true
    );
}

#[test]
fn update_windows_task_graceful_before_switch() {
    let t = TestEnv::windows()
        .with_install_root()
        .with_installed("0.4.0")
        .with_current("0.4.0", None)
        .with_bundle("0.4.1")
        .with_supervisor_state("windows-task", true, true)
        .with_idle_daemon(TestEnv::win_endpoint(), 4242)
        .with_new_daemon_reporting_version("0.4.1");
    let (rc, _, _) = t.run_from_bundle(&["setup"]);
    assert_eq!(rc, 0);
    let o = t.event_order();
    assert!(o.index_of("service:hold_autostart") < o.index_of("rpc:daemon.handoff_stop"));
    assert!(methods(&t).iter().all(|m| m != "daemon.stop"));
    assert!(!t.calls().iter().any(|c| c.get(1).map(String::as_str) == Some("/End")));
}

#[test]
fn update_old_daemon_never_exits_aborts_without_switch() {
    let t = TestEnv::windows()
        .with_install_root()
        .with_installed("0.4.0")
        .with_current("0.4.0", None)
        .with_bundle("0.4.1")
        .with_supervisor_state("windows-task", true, true)
        .with_daemon_alive_forever(TestEnv::win_endpoint(), 4242);
    let (rc, out, _) = t.run_from_bundle(&["setup"]);
    assert_eq!(rc, 2, "{out}");
    let j = json(&out);
    assert_eq!(
        (s(&j, "error_code"), s(&j, "phase")),
        ("TIMEOUT", "update_confirm_released")
    );
    assert_eq!(t.service().hold_state_now(), HoldState::Held);
    assert_eq!(
        t.entry_bytes("serialwrap.exe"),
        t.version_bytes("0.4.0", "bin/serialwrap.exe")
    );
    let j_file = t.journal_json().unwrap();
    assert_eq!(j_file["step"], "confirm_released");
    assert!(!j_file["original"]["daemon"].is_null());
}

#[test]
fn update_tty_still_held_posix_aborts() {
    let base = TestEnv::posix();
    let endpoint = base.default_socket();
    let t = base
        .with_install_root()
        .with_installed("0.4.0")
        .with_current("0.4.0", None)
        .with_bundle("0.4.1")
        .with_systemd(true)
        .with_var("USER", "alice")
        .with_supervisor_state("systemd-user", true, true)
        .with_idle_daemon_default(4242)
        .with_new_daemon_reporting_version("0.4.1")
        .with_rpc_ok(&endpoint, "session.list", "{\"ok\":true,\"sessions\":[{\"com\":\"COM0\",\"state\":\"READY\",\"attached_real_path\":\"/isolated/test-tty\",\"foreground_busy\":false,\"outstanding_commands\":0,\"interactive_session_id\":null,\"recovering\":false}]}")
        .with_rpc_after_relaunch("session.list", "{\"ok\":true,\"sessions\":[{\"com\":\"COM0\",\"state\":\"READY\",\"attached_real_path\":\"/isolated/test-tty\",\"foreground_busy\":false,\"outstanding_commands\":0,\"interactive_session_id\":null,\"recovering\":false}]}");
    t.procs().set_serialwrapd_tty_holders(&[4242]);
    let (rc, out, _) = t.run_from_bundle(&["setup"]);
    assert_eq!(rc, 2);
    let j = json(&out);
    assert_eq!(
        (s(&j, "error_code"), s(&j, "phase")),
        ("DEVICE_STILL_HELD", "update_confirm_released")
    );
    assert!(t.process().spawns().is_empty());
}

#[test]
fn update_backup_contains_required_files() {
    let t = TestEnv::windows()
        .with_install_root()
        .with_installed("0.4.0")
        .with_current("0.4.0", None)
        .with_bundle("0.4.1")
        .with_supervisor_state("windows-task", true, true)
        .with_idle_daemon(TestEnv::win_endpoint(), 4242)
        .with_new_daemon_reporting_version("0.4.1");
    t.write_file(&t.state_dir().join("state.json"), b"{}\n");
    t.write_file(&t.state_dir().join("benches.state.json"), b"{}\n");
    t.write_file(&t.config_dir().join("profiles").join("default.yaml"), b"defaults: {}\n");
    let (rc, out, _) = t.run_from_bundle(&["setup"]);
    assert_eq!(rc, 0);
    let j = json(&out);
    let bdir = t.backup_dir_of(&j);
    for req in [
        "config.yaml",
        "state.json",
        "benches.state.json",
        "profiles/default.yaml",
        "supervisor/serialwrap.task.xml",
        "registration.json",
        "registered.task.xml",
        "entries.json",
        "manifest.json",
    ] {
        assert!(bdir.join(req).exists(), "備份缺少必要檔案：{req}");
    }
}

#[test]
fn update_version_ok_but_health_fails_reverts() {
    let t = TestEnv::windows()
        .with_install_root()
        .with_installed("0.4.0")
        .with_current("0.4.0", None)
        .with_bundle("0.4.1")
        .with_supervisor_state("windows-task", true, true)
        .with_idle_daemon(TestEnv::win_endpoint(), 4242)
        .with_new_daemon_reporting_version("0.4.1")
        .with_rpc_after_relaunch("health.status", "{\"ok\":false}");
    let (rc, out, _) = t.run_from_bundle(&["setup"]);
    assert_eq!(rc, 2);
    let j = json(&out);
    assert_eq!(
        (s(&j, "error_code"), s(&j, "reason")),
        ("INTERNAL_ERROR", "health_failed")
    );
    assert!(b(&dict(&j, "update"), "reverted"));
}

#[test]
fn update_wal_unhealthy_reverts() {
    let t = TestEnv::windows()
        .with_install_root()
        .with_installed("0.4.0")
        .with_current("0.4.0", None)
        .with_bundle("0.4.1")
        .with_supervisor_state("windows-task", true, true)
        .with_idle_daemon(TestEnv::win_endpoint(), 4242)
        .with_new_daemon_reporting_version("0.4.1")
        .with_rpc_after_relaunch("health.status", "{\"ok\":true,\"pid\":5252,\"version\":\"0.4.1\",\"wal\":{\"healthy\":false}}");
    let (rc, out, _) = t.run_from_bundle(&["setup"]);
    assert_eq!(rc, 2, "{out}");
    let j = json(&out);
    assert_eq!(s(&j, "reason"), "wal_unhealthy");
    assert!(b(&dict(&j, "update"), "reverted"));
}

#[test]
fn update_revert_keeps_journal_when_previous_tty_holder_remains() {
    let t = TestEnv::windows()
        .with_install_root()
        .with_installed("0.4.0")
        .with_current("0.4.0", None)
        .with_bundle("0.4.1")
        .with_supervisor_state("windows-task", true, true)
        .with_idle_daemon(TestEnv::win_endpoint(), 4242)
        .with_rpc_ok(
            TestEnv::win_endpoint(),
            "session.list",
            "{\"ok\":true,\"sessions\":[{\"com\":\"COM0\",\"state\":\"READY\",\"attached_real_path\":\"COM20\",\"foreground_busy\":false,\"outstanding_commands\":0,\"interactive_session_id\":null,\"recovering\":false}]}",
        )
        .with_new_daemon_reporting_version("0.4.1")
        .with_rpc_after_relaunch("health.status", "{\"ok\":false}");
    t.procs().holders_script(vec![
        (Vec::new(), sw_pal::proc_scan::HolderScanStatus::Ok),
        (vec![8888], sw_pal::proc_scan::HolderScanStatus::Ok),
    ]);

    let (rc, out, _) = t.run_from_bundle(&["setup"]);

    assert_eq!(rc, 2, "{out}");
    let response = json(&out);
    let update = dict(&response, "update");
    assert_eq!(b(update, "reverted"), false, "{out}");
    assert_eq!(b(update, "revert_pending"), true, "{out}");
    let revert_error = dict(update, "revert_error");
    assert_eq!(s(revert_error, "error_code"), "DEVICE_STILL_HELD", "{out}");
    assert_eq!(s(revert_error, "phase"), "update_confirm_released", "{out}");
    assert_eq!(revert_error["pids"], json("[8888]"));
    let current = std::fs::read_to_string(&t.root().current_file).expect("current-release.json 應保留");
    assert_eq!(s(&json(&current), "bundle_version"), "0.4.1");
    let journal = t.journal_json().expect("失敗回退需保留 journal");
    assert_eq!(s(&journal, "step"), "start_and_verify");
}

#[test]
fn update_released_session_auto_attached_reverts() {
    let t = TestEnv::windows()
        .with_install_root()
        .with_installed("0.4.0")
        .with_current("0.4.0", None)
        .with_bundle("0.4.1")
        .with_supervisor_state("windows-task", true, true)
        .with_idle_daemon(TestEnv::win_endpoint(), 4242)
        .with_rpc_ok(TestEnv::win_endpoint(), "session.list", "{\"ok\":true,\"sessions\":[{\"com\":\"COM1\",\"state\":\"RELEASED\",\"foreground_busy\":false,\"outstanding_commands\":0,\"interactive_session_id\":null,\"recovering\":false}]}")
        .with_new_daemon_reporting_version("0.4.1")
        .with_rpc_after_relaunch(
            "session.list",
            "{\"ok\":true,\"sessions\":[{\"com\":\"COM1\",\"state\":\"READY\",\"foreground_busy\":false,\"outstanding_commands\":0,\"interactive_session_id\":null,\"recovering\":false}]}",
        );
    t.write_file(&t.state_dir().join("state.json"), b"{\"released\":{\"COM1\":{\"released_by\":\"test\"}}}\n");
    let (rc, out, _) = t.run_from_bundle(&["setup"]);
    assert_eq!(rc, 2, "{out}");
    let j = json(&out);
    assert_eq!(s(&j, "reason"), "state_incompatible");
    assert!(b(&dict(&j, "update"), "reverted"));
}

#[test]
fn update_accepts_ready_and_released_sessions_together() {
    let t = TestEnv::windows()
        .with_install_root()
        .with_installed("0.4.0")
        .with_current("0.4.0", None)
        .with_bundle("0.4.1")
        .with_supervisor_state("windows-task", true, true)
        .with_idle_daemon(TestEnv::win_endpoint(), 4242)
        .with_rpc_ok(TestEnv::win_endpoint(), "session.list", "{\"ok\":true,\"sessions\":[{\"com\":\"COM0\",\"state\":\"READY\",\"attached_real_path\":\"COM20\",\"foreground_busy\":false,\"outstanding_commands\":0,\"interactive_session_id\":null,\"recovering\":false},{\"com\":\"COM1\",\"state\":\"RELEASED\",\"foreground_busy\":false,\"outstanding_commands\":0,\"interactive_session_id\":null,\"recovering\":false}]}")
        .with_new_daemon_reporting_version("0.4.1")
        .with_rpc_after_relaunch("session.list", "{\"ok\":true,\"sessions\":[{\"com\":\"COM0\",\"state\":\"READY\",\"foreground_busy\":false,\"outstanding_commands\":0,\"interactive_session_id\":null,\"recovering\":false},{\"com\":\"COM1\",\"state\":\"RELEASED\",\"foreground_busy\":false,\"outstanding_commands\":0,\"interactive_session_id\":null,\"recovering\":false}]}");
    t.write_file(&t.state_dir().join("state.json"), b"{\"released\":{\"COM1\":{\"released_by\":\"test\"}}}\n");
    t.procs().holders(Vec::new(), sw_pal::proc_scan::HolderScanStatus::Unsupported);
    let (rc, out, _) = t.run_from_bundle(&["setup"]);
    assert_eq!(rc, 0, "{out}");
    let called = methods(&t);
    assert!(!called.iter().any(|method| method == "wal.healthy"));
    assert!(called.iter().any(|method| method == "alias.list"));
    let confirm = json(&out)["update"]["steps"].as_array().unwrap().iter()
        .find(|step| step["step"] == "confirm_released").unwrap().clone();
    assert_eq!(confirm["detail"]["holders_status"], "unsupported");
}

#[test]
fn update_smoke_failure_reverts() {
    let t = TestEnv::windows()
        .with_install_root()
        .with_installed("0.4.0")
        .with_current("0.4.0", None)
        .with_bundle("0.4.1")
        .with_supervisor_state("windows-task", true, true)
        .with_idle_daemon(TestEnv::win_endpoint(), 4242)
        .with_new_daemon_reporting_version("0.4.1")
        .with_rpc_after_relaunch("device.list", "{\"ok\":false}");
    let (rc, out, _) = t.run_from_bundle(&["setup"]);
    assert_eq!(rc, 2, "{out}");
    let j = json(&out);
    assert_eq!(s(&j, "reason"), "smoke_failed");
    assert!(b(&dict(&j, "update"), "reverted"));
}

#[test]
fn update_materializes_assets_preserving_profiles() {
    let t = TestEnv::posix()
        .with_install_root()
        .with_installed("0.4.0")
        .with_current("0.4.0", None)
        .with_bundle("0.4.1")
        .with_systemd(true)
        .with_var("USER", "alice")
        .with_supervisor_state("systemd-user", true, true)
        .with_idle_daemon_default(4242)
        .with_new_daemon_reporting_version("0.4.1");
    let user_profile = b"custom: profile\n";
    t.write_file(&t.config_dir().join("profiles").join("default.yaml"), user_profile);
    let skill = t.home().join(".local/share/serialwrap/skill/SKILL.md");
    let wrapper = t.home().join(".local/bin/serialwrap-minicom");
    t.write_file(&skill, b"stale skill\n");
    t.write_file(&wrapper, b"stale wrapper\n");
    let config_before = t.config_bytes();
    let (rc, out, _) = t.run_from_bundle(&["setup"]);
    assert_eq!(rc, 0);
    assert_eq!(json(&out)["update"]["to_version"], "0.4.1");
    assert_eq!(
        std::fs::read(t.config_dir().join("profiles").join("default.yaml")).unwrap(),
        user_profile,
        "既有 profile 必須被保留"
    );
    assert_eq!(
        std::fs::read(&skill).unwrap(),
        sw_cli::assets::SKILL_ASSETS.iter().find(|asset| asset.rel == "skill/SKILL.md").unwrap().bytes,
        "更新需重寫 tampered SKILL.md"
    );
    assert_eq!(
        std::fs::read(&wrapper).unwrap(),
        sw_cli::assets::TOOL_ASSETS.iter().find(|asset| asset.rel == "tools/minicom_router.sh").unwrap().bytes,
        "更新需重寫 tampered wrapper"
    );
    assert_eq!(t.config_bytes(), config_before, "更新不得重寫既有模式設定");
}

#[test]
fn journal_recovery_after_crash_mid_switch() {
    let t = TestEnv::windows()
        .with_install_root()
        .with_installed("0.4.0")
        .with_current("0.4.0", None)
        .with_bundle("0.4.1")
        .with_supervisor_state("windows-task", true, true)
        .with_idle_daemon(TestEnv::win_endpoint(), 4242)
        .with_new_daemon_reporting_version("0.4.1")
        .with_rpc_after_relaunch_seq("health.status", &[
            "{\"ok\":true,\"pid\":5252,\"version\":\"0.4.0\",\"wal\":{\"healthy\":true}}",
            "{\"ok\":true,\"pid\":5252,\"version\":\"0.4.1\",\"wal\":{\"healthy\":true}}",
            "{\"ok\":true,\"pid\":5252,\"version\":\"0.4.1\",\"wal\":{\"healthy\":true}}",
        ])
        .with_crash_at("entries_switching:after_first_rename");
    assert_eq!(t.run_from_bundle(&["setup"]).0, 137);
    let mut t = t;
    t.clear_crash();
    let (rc, out, _) = t.run_from_bundle(&["setup"]);
    assert_eq!(rc, 0, "{out}");
    let j = json(&out);
    assert_eq!(j["recovered_journal"]["ok"], true);
    assert_eq!(j["recovered_journal"]["from_step"], "entries_switching");
    assert_eq!(j["recovered_journal"]["backup_used"], true);
    assert!(t.journal_json().is_none());
}

#[test]
fn journal_recovery_keeps_journal_when_tty_holder_remains() {
    let t = TestEnv::windows()
        .with_install_root()
        .with_installed("0.4.0")
        .with_current("0.4.0", None)
        .with_bundle("0.4.1")
        .with_supervisor_state("windows-task", true, true)
        .with_idle_daemon(TestEnv::win_endpoint(), 4242)
        .with_rpc_ok(TestEnv::win_endpoint(), "session.list", "{\"ok\":true,\"sessions\":[{\"com\":\"COM0\",\"state\":\"READY\",\"attached_real_path\":\"COM20\",\"foreground_busy\":false,\"outstanding_commands\":0,\"interactive_session_id\":null,\"recovering\":false}]}")
        .with_new_daemon_reporting_version("0.4.0")
        .with_rpc_after_relaunch("session.list", "{\"ok\":true,\"sessions\":[{\"com\":\"COM0\",\"state\":\"READY\",\"attached_real_path\":\"COM20\",\"foreground_busy\":false,\"outstanding_commands\":0,\"interactive_session_id\":null,\"recovering\":false}]}")
        .with_crash_at("entries_switching:after_first_rename");
    assert_eq!(t.run_from_bundle(&["setup"]).0, 137);
    let journal_before = t.journal_bytes().expect("切換中斷後需保留 journal");
    t.procs().holders(vec![8888], sw_pal::proc_scan::HolderScanStatus::Ok);
    let mut t = t;
    t.clear_crash();

    let (rc, out, _) = t.run_from_bundle(&["setup"]);

    assert_eq!(rc, 2, "{out}");
    let response = json(&out);
    assert_eq!(response["recovered_journal"]["ok"], false, "{out}");
    assert_eq!(response["recovered_journal"]["reason"], "tty_holder_remains", "{out}");
    assert_eq!(t.journal_bytes().as_deref(), Some(journal_before.as_slice()));
}

#[test]
fn journal_write_failure_aborts_before_effect() {
    let t = TestEnv::windows()
        .with_install_root()
        .with_installed("0.4.0")
        .with_current("0.4.0", None)
        .with_bundle("0.4.1")
        .with_fs_fault_replace_suffix("install-journal.json");
    let (rc, out, _) = t.run_from_bundle(&["setup"]);
    assert_eq!(rc, 2);
    let j = json(&out);
    assert_eq!(
        (s(&j, "error_code"), s(&j, "phase"), s(&j, "reason")),
        ("INTERNAL_ERROR", "update_journal", "journal_write_failed")
    );
    assert!(acts(&t).is_empty());
    assert!(t.rpc().requests().is_empty());
}

#[test]
fn rollback_keeps_current_state_same_schema() {
    let t = TestEnv::windows()
        .with_install_root()
        .with_installed("0.4.0")
        .with_installed("0.4.1")
        .with_current("0.4.1", Some("0.4.0"))
        .with_supervisor_state("windows-task", true, true)
        .with_idle_daemon(TestEnv::win_endpoint(), 4242)
        .with_new_daemon_reporting_version("0.4.0");
    t.with_previous_backup_mode("windows-task");
    let state_bytes = b"{\"sessions\":{\"COM0\":{\"active\":true}}}\n";
    t.write_file(&t.state_dir().join("state.json"), state_bytes);
    let config_before = t.config_bytes();
    let (rc, out, _) = t.run(&["setup", "--rollback"]);
    assert_eq!(rc, 0, "{out}");
    assert_eq!(json(&out)["rollback"]["to_version"], "0.4.0");
    assert_eq!(
        std::fs::read(t.state_dir().join("state.json")).unwrap(),
        state_bytes
    );
    assert_eq!(t.config_bytes(), config_before, "rollback 不得重寫同模式 config");
}

#[test]
fn update_release_then_rollback_keeps_released() {
    let t = TestEnv::windows()
        .with_install_root()
        .with_installed("0.4.0")
        .with_current("0.4.0", None)
        .with_bundle("0.4.1")
        .with_supervisor_state("windows-task", true, true)
        .with_idle_daemon(TestEnv::win_endpoint(), 4242)
        .with_new_daemon_reporting_version("0.4.1");
    let released_state = b"{\"released\":{\"COM1\":{\"released_by\":\"test\"}}}\n";
    t.write_file(&t.state_dir().join("state.json"), released_state);
    let (update_rc, update_out, _) = t.run_from_bundle(&["setup"]);
    assert_eq!(update_rc, 0, "{update_out}");
    assert_eq!(json(&update_out)["update"]["to_version"], "0.4.1");
    assert_eq!(std::fs::read(t.state_dir().join("state.json")).unwrap(), released_state);
    let (rollback_rc, rollback_out, _) = t.run(&["setup", "--rollback"]);
    assert_eq!(rollback_rc, 0, "{rollback_out}");
    assert_eq!(json(&rollback_out)["rollback"]["to_version"], "0.4.0");
    assert_eq!(sw_cli::install_layout::read_current(&t.root()).unwrap().unwrap().bundle_version, "0.4.0");
    assert_eq!(std::fs::read(t.state_dir().join("state.json")).unwrap(), released_state);
}

#[test]
fn rollback_refuses_incompatible_state_schema() {
    let t = TestEnv::windows()
        .with_install_root()
        .with_installed("0.4.0")
        .with_installed("0.4.1")
        .with_current("0.4.1", Some("0.4.0"));
    let prev_path = t.root().versions.join("0.4.0").join("release-manifest.json");
    let prev_manifest = std::fs::read_to_string(&prev_path)
        .unwrap()
        .replace("python-state-v0", "old-v0");
    t.write_file(&prev_path, prev_manifest.as_bytes());
    let (rc, out, _) = t.run(&["setup", "--rollback"]);
    assert_eq!(rc, 2);
    let j = json(&out);
    assert_eq!(
        (s(&j, "error_code"), s(&j, "phase"), s(&j, "reason")),
        ("INVALID_ARGS", "rollback_verify", "state_schema_incompatible")
    );
}

#[test]
fn rollback_reregisters_previous_task() {
    let query = [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        "try { (Get-ScheduledTask -TaskName 'serialwrap' -ErrorAction Stop).State.ToString() } catch { if ($_.CategoryInfo.Category -eq 'ObjectNotFound') { 'NotFound' } else { exit 3 } }",
    ];
    let t = TestEnv::windows()
        .with_config("supervision_mode: windows-task\n")
        .with_install_root()
        .with_installed("0.4.0")
        .with_installed("0.4.1")
        .with_current("0.4.1", Some("0.4.0"))
        .with_real_service()
        .with_command_seq(&query, &[(0, "Running\n", ""), (0, "Disabled\n", "")])
        .with_command_raw(
            &["schtasks", "/Query", "/TN", "serialwrap", "/XML"],
            0,
            b"<Task><Settings><Enabled>true</Enabled></Settings></Task>",
            b"",
        )
        .with_idle_daemon(TestEnv::win_endpoint(), 4242)
        .with_new_daemon_reporting_version("0.4.0");
    t.with_previous_backup_mode("windows-task");
    let (rc, out, err) = t.run(&["setup", "--rollback"]);
    assert_eq!(rc, 0, "out={out} err={err}");
    let calls = t.calls();
    let create = calls
        .iter()
        .position(|call| call.get(1).is_some_and(|arg| arg == "/Create") && call.get(2).is_some_and(|arg| arg == "/XML"))
        .expect("Previous Task 必須以 XML 重註冊");
    let enable = calls
        .iter()
        .position(|call| call.get(1).is_some_and(|arg| arg == "/Change") && call.last().is_some_and(|arg| arg == "/ENABLE"))
        .expect("還原 Previous Task 的 enabled 狀態");
    let start = calls
        .iter()
        .position(|call| call.get(1).is_some_and(|arg| arg == "/Run"))
        .expect("啟動 Previous Task");
    assert!(create < enable && enable < start, "重註冊、解除 hold、啟動順序錯誤：{calls:?}");
    let xml_path = &calls[create][3];
    let xml_bytes = std::fs::read(xml_path).unwrap();
    let xml_units = xml_bytes[2..]
        .chunks_exact(2)
        .map(|bytes| u16::from_le_bytes([bytes[0], bytes[1]]))
        .collect::<Vec<_>>();
    let xml = String::from_utf16(&xml_units).unwrap();
    assert!(xml.contains("<Enabled>false</Enabled>"), "重註冊時 Task 必須維持 disabled：{xml}");
}

#[test]
fn rollback_cross_mode_windows_task_to_on_demand() {
    let t = TestEnv::windows()
        .with_install_root()
        .with_installed("0.4.0")
        .with_installed("0.4.1")
        .with_current("0.4.1", Some("0.4.0"))
        .with_supervisor_state("windows-task", true, true)
        .with_idle_daemon(TestEnv::win_endpoint(), 4242)
        .with_new_daemon_reporting_version("0.4.0");
    t.with_previous_backup_mode("on-demand");
    let config_before = t.config_bytes();
    let (rc, out, _) = t.run(&["setup", "--rollback"]);
    assert_eq!(rc, 0, "{out}");
    assert_eq!(json(&out)["rollback"]["to_version"], "0.4.0");
    assert_eq!(sw_cli::install_layout::read_current(&t.root()).unwrap().unwrap().bundle_version, "0.4.0");
    assert!(t.config_bytes().windows(b"supervision_mode: on-demand".len()).any(|window| window == b"supervision_mode: on-demand"));
    assert_ne!(t.config_bytes(), config_before, "跨模式 rollback 必須更新 config mode");
    assert!(acts(&t).iter().any(|action| action == "uninstall"));
    assert!(!acts(&t).iter().any(|action| action == "start"), "on-demand 不得啟動監管項");
    assert_eq!(t.process().spawn_count(), 1, "on-demand rollback 應直接啟動舊 daemon");
}

#[test]
fn rollback_cross_mode_failure_recovers_current() {
    let t = TestEnv::windows()
        .with_install_root()
        .with_installed("0.4.0")
        .with_installed("0.4.1")
        .with_current("0.4.1", Some("0.4.0"))
        .with_supervisor_state("windows-task", true, true)
        .with_idle_daemon(TestEnv::win_endpoint(), 4242)
        .with_new_daemon_reporting_version("0.4.0")
        .with_rpc_after_relaunch("health.status", "{\"ok\":true,\"pid\":5252,\"version\":\"0.4.1\",\"wal\":{\"healthy\":true}}")
        .with_spawn_exits(1);
    t.with_previous_backup_mode("on-demand");
    let config_before = t.config_bytes();
    let (rc, out, _) = t.run(&["setup", "--rollback"]);
    assert_eq!(rc, 2, "{out}");
    let j = json(&out);
    assert_eq!(j["rollback"]["reverted"], true, "{out}");
    assert_eq!(j["rollback"]["revert_pending"], false, "{out}");
    assert_eq!(sw_cli::install_layout::read_current(&t.root()).unwrap().unwrap().bundle_version, "0.4.1");
    assert_eq!(t.config_bytes(), config_before, "跨模式 rollback 失敗後須還原目前 mode 設定");
    assert_eq!(t.service().query_now().enabled, Some(true));
    assert!(acts(&t).iter().any(|action| action == "uninstall"));
    assert!(acts(&t).iter().any(|action| action == "start"));
    assert_eq!(t.process().spawn_count(), 1, "只嘗試啟動 Previous 的 on-demand daemon 一次");
}

#[test]
fn systemd_rollback_daemon_reload_after_unit_restore() {
    let t = TestEnv::posix()
        .with_install_root()
        .with_installed("0.4.0")
        .with_installed("0.4.1")
        .with_current("0.4.1", Some("0.4.0"))
        .with_supervisor_state("systemd-user", true, true)
        .with_idle_daemon_default(4242)
        .with_new_daemon_reporting_version("0.4.0");
    t.with_previous_backup_mode("systemd-user");
    let (rc, _, _) = t.run(&["setup", "--rollback"]);
    assert_eq!(rc, 0);
    let o = t.event_order();
    assert!(o.index_of("run:systemctl --user daemon-reload") < o.index_of("service:start"));
}

#[test]
fn rollback_twice_rolls_forward() {
    let t = TestEnv::windows()
        .with_install_root()
        .with_installed("0.4.0")
        .with_installed("0.4.1")
        .with_current("0.4.1", Some("0.4.0"))
        .with_supervisor_state("windows-task", true, true)
        .with_idle_daemon(TestEnv::win_endpoint(), 4242)
        .with_new_daemon_reporting_version("0.4.0");
    let (rc1, out1, _) = t.run(&["setup", "--rollback"]);
    assert_eq!(rc1, 0, "{out1}");
    let cur1 = sw_cli::install_layout::read_current(&t.root()).unwrap().unwrap();
    assert_eq!(cur1.bundle_version, "0.4.0");

    t.rpc().respond_after_relaunch(TestEnv::win_endpoint(), "health.status", "{\"ok\":true,\"pid\":5353,\"version\":\"0.4.1\",\"wal\":{\"healthy\":true}}");
    let (rc2, out2, _) = t.run(&["setup", "--rollback"]);
    assert_eq!(rc2, 0, "{out2}");
    let cur2 = sw_cli::install_layout::read_current(&t.root()).unwrap().unwrap();
    assert_eq!(cur2.bundle_version, "0.4.1");
}

#[test]
fn rollback_not_cli_only() {
    let t = TestEnv::windows()
        .with_install_root()
        .with_installed("0.4.0")
        .with_installed("0.4.1")
        .with_current("0.4.1", Some("0.4.0"))
        .with_supervisor_state("windows-task", true, true)
        .with_idle_daemon(TestEnv::win_endpoint(), 4242)
        .with_new_daemon_reporting_version("0.4.0");
    let (rc, _, _) = t.run(&["setup", "--rollback"]);
    assert_eq!(rc, 0);
    assert_eq!(
        t.entry_bytes("serialwrap.exe"),
        t.version_bytes("0.4.0", "bin/serialwrap.exe")
    );
    assert_eq!(
        t.entry_bytes("serialwrapd.exe"),
        t.version_bytes("0.4.0", "bin/serialwrapd.exe")
    );
}

#[test]
fn update_start_failure_writes_failure_json() {
    let t = TestEnv::windows()
        .with_install_root()
        .with_installed("0.4.0")
        .with_current("0.4.0", None)
        .with_bundle("0.4.1")
        .with_config("supervision_mode: on-demand\n")
        .with_idle_daemon(TestEnv::win_endpoint(), 4242)
        .with_spawn_exits(1);
    let (rc, out, _) = t.run_from_bundle(&["setup", "--on-demand"]);
    assert_eq!(rc, 2);
    let j = json(&out);
    let failure_path = t.backup_dir_of(&j).join("failure.json");
    assert!(failure_path.exists());
    let failure = json(&std::fs::read_to_string(failure_path).unwrap());
    assert_eq!(failure["error_code"], j["error_code"]);
    assert_eq!(failure["phase"], "update_start_and_verify");
    assert_eq!(failure["failed_step"], "start_and_verify");
    assert_eq!(failure["returncode"], 1);
    let steps = failure["steps"].as_array().unwrap();
    assert_eq!(steps.last().unwrap()["step"], "start_and_verify");
    assert_eq!(steps.last().unwrap()["ok"], false);
    assert_eq!(steps.last().unwrap()["detail"]["returncode"], 1);
}

#[test]
fn update_on_demand_not_running_skips_daemon_start() {
    let t = TestEnv::windows()
        .with_supervisor_state("on-demand", false, false)
        .with_process()
        .with_install_root()
        .with_installed("0.4.0")
        .with_current("0.4.0", None)
        .with_bundle("0.4.1")
        .with_config("supervision_mode: on-demand\n")
        .with_new_daemon_reporting_version("0.4.1");
    let (rc, _, _) = t.run_from_bundle(&["setup", "--on-demand"]);
    assert_eq!(rc, 0);
    assert!(t.process().spawns().is_empty());
}

#[test]
fn fresh_install_materializes_profiles_and_sets_windows_task() {
    let t = TestEnv::windows()
        .with_process()
        .with_install_root()
        .with_real_service()
        .with_bundle("0.4.1")
        .with_new_daemon_reporting_version("0.4.1");
    let (rc, out, _) = t.run_from_bundle(&["setup"]);
    assert_eq!(rc, 0, "{out}");
    let j = json(&out);
    assert_eq!(j["install"]["to_version"], "0.4.1");
    assert!(matches!(j["setup"], sw_proto::pyjson::PyValue::Dict(_)), "首次安裝需回報 reconcile 結果：{out}");
    assert!(j["legacy"].is_null(), "未發現 legacy 安裝時需明確回報 null：{out}");
    assert!(j["doctor_hint"].as_str().is_some(), "首次安裝需提供 doctor_hint：{out}");
    assert!(j["console_hint"].as_str().is_some(), "首次安裝需提供 console_hint：{out}");
    assert!(t.config_dir().join("profiles").join("default.yaml").exists());
    assert!(t.calls().iter().any(|c| c.join(" ").contains("/Create /XML")));
    let cur = sw_cli::install_layout::read_current(&t.root()).unwrap().unwrap();
    assert!(cur.previous.is_none());
}

#[test]
fn revert_handoff_unsupported_new_daemon_keeps_journal() {
    let t = TestEnv::windows()
        .with_install_root()
        .with_installed("0.4.0")
        .with_current("0.4.0", None)
        .with_bundle("0.4.1")
        .with_supervisor_state("windows-task", true, true)
        .with_idle_daemon(TestEnv::win_endpoint(), 4242)
        .with_new_daemon_reporting_version("0.4.0")
        .with_new_daemon_handoff_script(&["{\"ok\":false,\"error_code\":\"METHOD_NOT_FOUND\"}"]);
    let (rc, out, _) = t.run_from_bundle(&["setup"]);
    assert_eq!(rc, 2, "{out}");
    let j = json(&out);
    assert_eq!(j["update"]["reverted"], false);
    assert_eq!(j["update"]["revert_pending"], true);
    assert_eq!(j["update"]["revert_error"]["error_code"], "METHOD_NOT_FOUND");
    assert_eq!(j["update"]["revert_error"]["phase"], "update_revert");
    assert_eq!(j["update"]["revert_error"]["probe"], "daemon.handoff_stop");
    assert_eq!(j["update"]["revert_error"]["handoff_outcome"], "refused");
    let hint = j["update"]["revert_error"]["hint"].as_str().unwrap();
    assert!(hint.contains("serialwrap daemon status") && hint.contains("serialwrap daemon stop"));
    assert_eq!(sw_cli::install_layout::read_current(&t.root()).unwrap().unwrap().bundle_version, "0.4.1");
    assert!(t.rpc().requests().iter().all(|request| request.1 != "daemon.stop"));
    assert!(!t.calls().iter().any(|call| call.get(1).map(String::as_str) == Some("/End")));
    assert!(t.journal_json().is_some());
}

#[test]
fn revert_handoff_recv_timeout_no_hard_stop() {
    let t = TestEnv::windows()
        .with_install_root()
        .with_installed("0.4.0")
        .with_current("0.4.0", None)
        .with_bundle("0.4.1")
        .with_supervisor_state("windows-task", true, true)
        .with_idle_daemon(TestEnv::win_endpoint(), 4242)
        .with_new_daemon_reporting_version("0.4.0")
        .with_rpc_recv_timeout(TestEnv::win_endpoint(), "alias.list")
        .with_new_daemon_handoff_script(&["<recv_timeout>"]);
    let (rc, out, _) = t.run_from_bundle(&["setup"]);
    assert_eq!(rc, 2, "{out}");
    let j = json(&out);
    assert_eq!(j["error_code"], "INTERNAL_ERROR");
    assert_eq!(j["update"]["revert_pending"], true);
    assert_eq!(j["update"]["revert_error"]["error_code"], "DEVICE_STILL_HELD");
    assert_eq!(j["update"]["revert_error"]["probe"], "daemon.handoff_stop");
    assert_eq!(j["update"]["revert_error"]["handoff_outcome"], "unknown");
    assert!(!j["update"]["revert_error"]["pids"].as_array().unwrap().is_empty());
    assert!(j["update"]["revert_error"]["hint"].as_str().unwrap().contains("serialwrap daemon status"));
    assert!(!t.calls().iter().any(|c| c.get(1).map(String::as_str) == Some("/End")));
    assert!(t.rpc().requests().iter().all(|request| request.1 != "daemon.stop"));
    assert!(t.journal_json().is_some());
}

#[test]
fn revert_new_daemon_rpc_down_process_alive_defers() {
    let t = TestEnv::windows()
        .with_install_root()
        .with_installed("0.4.0")
        .with_current("0.4.0", None)
        .with_bundle("0.4.1")
        .with_supervisor_state("windows-task", true, true)
        .with_idle_daemon(TestEnv::win_endpoint(), 4242)
        .with_new_daemon_rpc_down();
    let (rc, out, _) = t.run_from_bundle(&["setup"]);
    assert_eq!(rc, 2, "{out}");
    let j = json(&out);
    assert_eq!(j["update"]["revert_pending"], true, "{out}");
    assert_eq!(j["update"]["revert_error"]["error_code"], "DEVICE_STILL_HELD", "{out}");
    assert_eq!(j["update"]["revert_error"]["probe"], "daemon.handoff_stop", "{out}");
    assert_eq!(j["update"]["revert_error"]["handoff_outcome"], "refused", "{out}");
    assert!(!j["update"]["revert_error"]["pids"].as_array().unwrap().is_empty(), "{out}");
    assert!(t.journal_json().is_some());
}

#[test]
fn rollback_revert_refuses_while_previous_daemon_pushing() {
    let t = TestEnv::windows()
        .with_install_root()
        .with_installed("0.4.0")
        .with_installed("0.4.1")
        .with_current("0.4.1", Some("0.4.0"))
        .with_supervisor_state("windows-task", true, true)
        .with_idle_daemon(TestEnv::win_endpoint(), 4242)
        .with_new_daemon_reporting_version("0.4.1")
        .with_new_daemon_handoff_script(&[BUSY_PUSH]);
    let (rc, out, _) = t.run(&["setup", "--rollback"]);
    assert_eq!(rc, 2, "{out}");
    let j = json(&out);
    assert_eq!(j["rollback"]["reverted"], false);
    assert_eq!(j["rollback"]["revert_pending"], true);
    assert_eq!(j["rollback"]["revert_error"]["error_code"], "SESSION_BUSY", "{out}");
    assert_eq!(j["rollback"]["revert_error"]["phase"], "update_revert", "{out}");
    assert_eq!(j["rollback"]["revert_error"]["handoff"], "refused", "{out}");
    assert_eq!(j["rollback"]["revert_error"]["busy"][0]["ops"][0], "file.push", "{out}");
    assert_eq!(sw_cli::install_layout::read_current(&t.root()).unwrap().unwrap().bundle_version, "0.4.0", "Previous 已切換，但 busy handoff 阻止回復 Current；journal 應保留供重試");
    assert_eq!(t.rpc().requests().iter().filter(|request| request.1 == "daemon.handoff_stop").count(), 2);
    assert!(t.rpc().requests().iter().all(|request| request.1 != "daemon.stop"));
    assert!(!t.calls().iter().any(|call| call.get(1).map(String::as_str) == Some("/End")));
    assert!(t.journal_json().is_some());
}

#[test]
fn update_systemd_system_hold_needs_sudo() {
    let t = TestEnv::posix()
        .with_install_root()
        .with_installed("0.4.0")
        .with_current("0.4.0", None)
        .with_bundle("0.4.1")
        .with_supervisor_state("systemd-system", true, true)
        .with_idle_daemon_default(4242);
    t.service().respond(sw_pal::service_ctl::ServiceResult {
        ok: false,
        mode: "systemd-system".into(),
        action: "hold_autostart".into(),
        rc: Some(1),
        stdout: None,
        stderr: None,
        error_code: Some("NEEDS_SUDO".into()),
        hint: Some("需要 sudo".into()),
        phase: Some("autostart_hold".into()),
    });
    let (rc, out, _) = t.run_from_bundle(&["setup"]);
    assert_eq!(rc, 2);
    let j = json(&out);
    assert_eq!(
        (s(&j, "error_code"), s(&j, "phase")),
        ("NEEDS_SUDO", "update_autostart_hold")
    );
    assert!(t.rpc().requests().iter().all(|r| r.1 != "daemon.handoff_stop" && r.1 != "daemon.stop"));
}

#[test]
fn update_hold_failure_sends_no_stop_rpc() {
    let t = TestEnv::posix()
        .with_install_root()
        .with_installed("0.4.0")
        .with_current("0.4.0", None)
        .with_bundle("0.4.1")
        .with_systemd(true)
        .with_var("USER", "alice")
        .with_supervisor_state("systemd-user", true, true)
        .with_idle_daemon_default(4242)
        .with_new_daemon_reporting_version("0.4.1");
    t.service().respond(sw_pal::service_ctl::ServiceResult {
        ok: false,
        mode: "systemd-user".into(),
        action: "hold_autostart".into(),
        rc: Some(1),
        stdout: None,
        stderr: None,
        error_code: Some("INTERNAL_ERROR".into()),
        hint: None,
        phase: Some("autostart_hold".into()),
    });
    let (rc, out, _) = t.run_from_bundle(&["setup", "--on-demand"]);
    assert_eq!(rc, 2);
    let j = json(&out);
    assert_eq!(
        (s(&j, "error_code"), s(&j, "phase"), s(&j, "reason")),
        ("INTERNAL_ERROR", "update_autostart_hold", "autostart_hold_unverified")
    );
    assert!(t.rpc().requests().iter().all(|r| r.1 != "daemon.handoff_stop" && r.1 != "daemon.stop"));
}

#[test]
fn update_no_daemon_still_holds_until_switch() {
    let t = TestEnv::posix()
        .with_install_root()
        .with_installed("0.4.0")
        .with_current("0.4.0", None)
        .with_bundle("0.4.1")
        .with_supervisor_state("systemd-user", true, false)
        .with_rpc_refused_default()
        .with_new_daemon_reporting_version("0.4.1");
    let (rc, _, _) = t.run_from_bundle(&["setup", "--user"]);
    assert_eq!(rc, 0);
    let o = t.event_order();
    assert!(o.index_of("journal:stop_supervisor") < o.index_of("service:hold_autostart"));
    assert!(o.index_of("journal:entries_switching") < o.index_of("service:release_autostart:true"));
    assert!(methods(&t).iter().all(|m| m != "daemon.handoff_stop" && m != "daemon.stop"));
}

#[test]
fn update_on_demand_holds_autostart_lock_until_new_daemon_started() {
    let t = TestEnv::windows()
        .with_install_root()
        .with_installed("0.4.0")
        .with_current("0.4.0", None)
        .with_bundle("0.4.1")
        .with_config("supervision_mode: on-demand\n")
        .with_idle_daemon(TestEnv::win_endpoint(), 4242)
        .with_new_daemon_reporting_version("0.4.1");
    t.process().observe_lock_at_spawn(t.run_dir().join("autostart.lock"));
    let (rc, out, _) = t.run_from_bundle(&["setup", "--on-demand"]);
    assert_eq!(rc, 0, "{out}");
    assert_eq!(t.process().spawn_count(), 1);
    assert_eq!(t.process().spawn_lock_held(), vec![true], "spawn 時仍須持有 autostart.lock");
    assert!(!t.autostart_lock_held(), "驗收完成後必須釋放 autostart.lock");
    let start_step = json(&out)["update"]["steps"].as_array().unwrap().iter()
        .find(|step| step["step"] == "start_and_verify").unwrap().clone();
    assert_eq!(start_step["detail"]["daemon_version"], "0.4.1");
}

#[test]
fn update_busy_handoff_releases_hold() {
    let t = TestEnv::posix()
        .with_install_root()
        .with_installed("0.4.0")
        .with_current("0.4.0", None)
        .with_bundle("0.4.1")
        .with_supervisor_state("systemd-user", true, true)
        .with_idle_daemon_default(4242)
        .with_rpc_ok_default(
            "daemon.handoff_stop",
            "{\"ok\":false,\"error_code\":\"SESSION_BUSY\",\"busy\":[{\"com\":\"COM0\",\"state\":\"READY\",\"foreground_busy\":true,\"outstanding_commands\":0,\"interactive_session_id\":null,\"recovering\":false,\"ops\":[\"file.push\"]}]}",
        );
    let (rc, _, _) = t.run_from_bundle(&["setup"]);
    assert_eq!(rc, 2);
    let o = t.event_order();
    assert!(o.index_of("rpc:daemon.handoff_stop") < o.index_of("service:release_autostart:true"));
    assert_eq!(t.service().hold_state_now(), HoldState::NotHeld);
}

#[test]
fn update_relaunched_instance_rehandoff_ok_continues() {
    let t = TestEnv::posix()
        .with_install_root()
        .with_installed("0.4.0")
        .with_current("0.4.0", None)
        .with_bundle("0.4.1")
        .with_supervisor_state("systemd-user", true, true)
        .with_idle_daemon_default(4242)
        .with_supervisor_ignores_hold()
        .with_restart_on_failure()
        .with_daemon_exit_code_after_handoff(1)
        .with_new_daemon_reporting_version("0.4.1")
        .with_rpc_after_relaunch_seq("health.status", &[
            "{\"ok\":true,\"pid\":5252,\"version\":\"0.4.0\",\"wal\":{\"healthy\":true}}",
            "{\"ok\":true,\"pid\":5252,\"version\":\"0.4.1\",\"wal\":{\"healthy\":true}}",
        ]);
    let (rc, out, _) = t.run_from_bundle(&["setup"]);
    assert_eq!(rc, 0, "{out}");
    let response = json(&out);
    assert_eq!(response["ok"], true);
    let start_step = response["update"]["steps"].as_array().unwrap().iter()
        .find(|step| step["step"] == "start_and_verify").unwrap();
    assert_eq!(start_step["detail"]["daemon_version"], "0.4.1");
    let stop_step = response["update"]["steps"].as_array().unwrap().iter()
        .find(|step| step["step"] == "stop_supervisor").unwrap();
    let chain = stop_step["detail"]["handoff_chain"].as_array().unwrap();
    assert_eq!(chain.len(), 2);
    assert_eq!(chain[0]["pid"], 4242);
    assert_eq!(chain[1]["pid"], 9000);
    assert_eq!(chain[1]["relaunched"], true);
    assert_ne!(chain[0]["token"], chain[1]["token"]);
    assert_eq!(handoffs(&t), 2);
    assert!(acts(&t).iter().all(|action| action != "hard_stop"));
    assert_eq!(sw_cli::install_layout::read_current(&t.root()).unwrap().unwrap().bundle_version, "0.4.1");
}

#[test]
fn update_hard_stop_never_targets_relaunched_identity() {
    let t = TestEnv::windows()
        .with_install_root()
        .with_installed("0.4.0")
        .with_current("0.4.0", None)
        .with_bundle("0.4.1")
        .with_supervisor_state("windows-task", true, true)
        .with_idle_daemon(TestEnv::win_endpoint(), 4242)
        .with_supervisor_ignores_hold()
        .with_restart_on_failure()
        .with_daemon_exit_code_after_handoff(1)
        .with_new_daemon_reporting_version("0.4.1")
        .with_rpc_after_relaunch("health.status", r#"{"ok":true,"pid":9000,"version":"0.4.0"}"#)
        .with_new_daemon_handoff_script(&[BUSY_PUSH]);
    let config_before = t.config_bytes();
    let (rc, out, _) = t.run_from_bundle(&["setup"]);
    assert_eq!(rc, 2, "{out}");
    let response = json(&out);
    assert_eq!(response["error_code"], "SESSION_BUSY", "{out}");
    assert_eq!(response["relaunched"]["pid"], 9000);
    assert_eq!(response["busy"][0]["com"], "COM0");
    assert_eq!(handoffs(&t), 2);
    assert!(acts(&t).iter().all(|a| a != "hard_stop"));
    assert_eq!(config_before, t.config_bytes());
    assert_eq!(sw_cli::install_layout::read_current(&t.root()).unwrap().unwrap().bundle_version, "0.4.0");
    assert_eq!(t.service().hold_state_now(), HoldState::NotHeld);
}

#[test]
fn update_relaunch_loop_aborts_without_switch() {
    let t = TestEnv::posix()
        .with_install_root()
        .with_installed("0.4.0")
        .with_current("0.4.0", None)
        .with_bundle("0.4.1")
        .with_systemd(true)
        .with_var("USER", "alice")
        .with_supervisor_state("systemd-user", true, true)
        .with_idle_daemon_default(4242)
        .with_supervisor_ignores_hold()
        .with_restart_on_failure()
        .with_daemon_exit_code_after_handoff(1)
        .with_new_daemon_handoff_pids(&[9000, 9001, 9002]);
    let config_before = t.config_bytes();
    let (rc, out, _) = t.run_from_bundle(&["setup"]);
    assert_eq!(rc, 2, "{out}");
    let j = json(&out);
    assert_eq!((s(&j, "error_code"), s(&j, "phase"), s(&j, "reason")),
        ("DEVICE_STILL_HELD", "update_confirm_released", "relaunch_loop"), "{out}");
    assert_eq!(j["pids"].as_array().unwrap().len(), 3);
    assert_eq!(handoffs(&t), 3);
    assert_eq!(t.event_order().count_prefix("journal:entries_switching"), 0);
    assert!(acts(&t).iter().all(|action| action != "hard_stop"));
    assert_eq!(config_before, t.config_bytes());
    assert_eq!(sw_cli::install_layout::read_current(&t.root()).unwrap().unwrap().bundle_version, "0.4.0");
    assert_eq!(t.service().hold_state_now(), HoldState::NotHeld);
}

#[test]
fn update_instance_before_start_rehandoff() {
    let t = TestEnv::posix()
        .with_install_root()
        .with_installed("0.4.0")
        .with_current("0.4.0", None)
        .with_bundle("0.4.1")
        .with_systemd(true)
        .with_var("USER", "alice")
        .with_supervisor_state("systemd-user", true, true)
        .with_idle_daemon_default(4242)
        .with_instance_appears_at("entries_switching:after_effect", 5252, &[])
        .with_new_daemon_reporting_version("0.4.1");
    let (rc, out, _) = t.run_from_bundle(&["setup"]);
    assert_eq!(rc, 0, "{out}");
    assert_eq!(json(&out)["ok"], true);
    assert_eq!(handoffs(&t), 2, "切換後出現的 5252 必須先重新交接");
    assert!(acts(&t).iter().all(|action| action != "hard_stop"));
    let order = t.event_order();
    assert!(order.index_of_nth("rpc:daemon.handoff_stop", 1) < order.index_of("service:release_autostart:true"));
    assert!(order.index_of("service:release_autostart:true") < order.index_of("service:start"));
    assert_eq!(sw_cli::install_layout::read_current(&t.root()).unwrap().unwrap().bundle_version, "0.4.1");
}

#[test]
fn revert_rehold_before_new_daemon_handoff() {
    let t = TestEnv::posix()
        .with_install_root()
        .with_installed("0.4.0")
        .with_current("0.4.0", None)
        .with_bundle("0.4.1")
        .with_systemd(true)
        .with_supervisor_state("systemd-user", true, true)
        .with_idle_daemon_default(4242)
        .with_restart_on_failure()
        .with_new_daemon_reporting_version("0.4.0");
    let (rc, out, _) = t.run_from_bundle(&["setup"]);
    assert_eq!(rc, 2, "{out}");
    let j = json(&out);
    assert_eq!(j["update"]["reverted"], true, "{out}");
    assert_eq!(j["update"]["revert_pending"], false, "{out}");
    assert_eq!(j["update"]["revert_error"], serde_json::Value::Null);
    assert_eq!(handoffs(&t), 2, "原 daemon 與新版 daemon 都必須各自交接");
    let order = t.event_order();
    assert!(order.index_of_nth("service:hold_autostart", 1) < order.index_of_nth("rpc:daemon.handoff_stop", 1));
    assert_eq!(t.service().hold_state_now(), HoldState::NotHeld);
    assert!(!t.autostart_lock_held(), "on-demand rollback 完成後必須釋放 autostart.lock");
    assert!(acts(&t).iter().all(|action| action != "hard_stop"));
    assert_eq!(sw_cli::install_layout::read_current(&t.root()).unwrap().unwrap().bundle_version, "0.4.0");
    assert!(t.journal_json().is_none());
}

#[test]
fn revert_deferred_releases_hold() {
    let t = TestEnv::posix()
        .with_install_root()
        .with_installed("0.4.0")
        .with_current("0.4.0", None)
        .with_bundle("0.4.1")
        .with_supervisor_state("systemd-user", true, true)
        .with_idle_daemon_default(4242)
        .with_new_daemon_reporting_version("0.4.0")
        .with_new_daemon_handoff_script(&[BUSY_PUSH]);
    let (rc, out, _) = t.run_from_bundle(&["setup"]);
    assert_eq!(rc, 2, "{out}");
    let j = json(&out);
    assert_eq!(j["error_code"], "INTERNAL_ERROR");
    assert_eq!(j["phase"], "update_start_and_verify");
    assert_eq!(j["update"]["revert_pending"], true);
    assert_eq!(j["update"]["reverted"], false);
    assert_eq!(j["update"]["revert_error"]["error_code"], "SESSION_BUSY");
    assert_eq!(j["update"]["revert_error"]["phase"], "update_revert");
    assert_eq!(j["update"]["revert_error"]["handoff"], "refused");
    assert_eq!(j["update"]["revert_error"]["busy"][0]["ops"][0], "file.push");
    let journal_path = t.root().root.join("install-journal.json");
    let journal_before = std::fs::read(&journal_path).unwrap();
    let journal_json = serde_json::from_slice::<serde_json::Value>(&journal_before).unwrap();
    assert_eq!(journal_json["step"], "start_and_verify");
    let endpoint = t.isolated_endpoint(&t.default_socket());
    t.rpc().respond(&endpoint, "daemon.handoff_stop", "{\"ok\":true,\"stopping\":true,\"handoff_token\":\"0123456789abcdef0123456789abcdef\"}");
    let recovered = t.recover().expect("deferred revert must remain recoverable");
    assert!(recovered.ok, "{:?}", recovered.reason);
    assert_eq!(recovered.restored["daemon_version"], "0.4.0");
    assert_eq!(sw_cli::install_layout::read_current(&t.root()).unwrap().unwrap().bundle_version, "0.4.0");
    assert!(!journal_path.exists());
    assert_eq!(t.service().hold_state_now(), HoldState::NotHeld);
}

#[test]
fn revert_deferred_on_demand_recovery_waits_for_new_daemon_ordered_shutdown() {
    // S1 出口 A/B B 輪（2026-10-30）黑箱 test_revert_new_daemon_push_not_aborted_l15 的確定性對應：on-demand 更新驗證失敗，
    // 回退因 file.push 延後（revert_pending）；push 完成後 recover_journal 對 0.4.1 daemon 交接成功，daemon 於 3 s 排空窗口後
    // 關閉監聽，SC-11 有序關機使行程於 grant 後 12 s 才結束。舊實作 grant 起 10 s 即回 TIMEOUT／handoff_timeout 並保留 journal。
    let t = TestEnv::posix()
        .with_install_root()
        .with_installed("0.4.0")
        .with_current("0.4.0", None)
        .with_bundle("0.4.1")
        .with_supervisor_state("on-demand", true, true)
        .with_config("supervision_mode: on-demand\n")
        .with_idle_daemon_default(4242)
        .with_new_daemon_reporting_version("0.4.0")
        .with_new_daemon_handoff_script(&[BUSY_PUSH]);
    let (rc, out, _) = t.run_from_bundle(&["setup", "--on-demand"]);
    assert_eq!(rc, 2, "{out}");
    let j = json(&out);
    assert_eq!(j["update"]["revert_pending"], true, "{out}");
    assert_eq!(j["update"]["revert_error"]["error_code"], "SESSION_BUSY", "{out}");
    let journal_path = t.root().root.join("install-journal.json");
    assert!(journal_path.exists());
    let endpoint = t.isolated_endpoint(&t.default_socket());
    let new_pid = 5252;
    t.rpc().respond(&endpoint, "daemon.handoff_stop", "{\"ok\":true,\"stopping\":true,\"handoff_token\":\"0123456789abcdef0123456789abcdef\"}");
    t.rpc().clear_stop_after_send(&endpoint, "daemon.handoff_stop");
    let rpc = std::sync::Arc::clone(t.rpc());
    let clock = std::sync::Arc::clone(t.clock());
    let gate_endpoint = endpoint.clone();
    let handoffs_before = t.rpc().request_count(&endpoint, "daemon.handoff_stop");
    let granted_at = std::sync::Mutex::new(None);
    let listener_closed = std::sync::atomic::AtomicBool::new(false);
    t.procs().alive_while(new_pid, std::sync::Arc::new(move || {
        if rpc.request_count(&gate_endpoint, "daemon.handoff_stop") == handoffs_before {
            return !rpc.is_stopped(&gate_endpoint);
        }
        let now = clock.elapsed();
        let at = *granted_at.lock().unwrap_or_else(|e| e.into_inner()).get_or_insert(now);
        if now < at + std::time::Duration::from_secs(3) {
            return true;
        }
        if !listener_closed.swap(true, std::sync::atomic::Ordering::SeqCst) {
            rpc.stop_endpoint(&gate_endpoint);
        }
        // 12 s 前為有序關機中的舊行程；之後只剩復原後重新拉起的實例（endpoint 復活）才算存活。
        now < at + std::time::Duration::from_secs(12) || !rpc.is_stopped(&gate_endpoint)
    }));
    let recovered = t.recover().expect("deferred revert must remain recoverable");
    assert!(recovered.ok, "{:?} {:?} {:?}", recovered.error_code, recovered.reason, recovered.message);
    assert_eq!(recovered.restored["daemon_version"], "0.4.0");
    assert_eq!(sw_cli::install_layout::read_current(&t.root()).unwrap().unwrap().bundle_version, "0.4.0");
    assert!(!journal_path.exists());
}

#[test]
fn journal_recovery_release_failure_keeps_journal() {
    let t = TestEnv::posix()
        .with_install_root()
        .with_installed("0.4.0")
        .with_current("0.4.0", None)
        .with_bundle("0.4.1")
        .with_supervisor_state("systemd-user", true, true)
        .with_idle_daemon_default(4242)
        .with_new_daemon_reporting_version("0.4.0")
        .with_crash_at("backup_complete:after_effect");
    assert_eq!(t.run_from_bundle(&["setup"]).0, 137);
    let journal_path = t.root().root.join("install-journal.json");
    let journal_before = std::fs::read(&journal_path).unwrap();
    t.service().respond(sw_pal::service_ctl::ServiceResult {
        ok: false,
        mode: "systemd-user".into(),
        action: "release_autostart:true".into(),
        rc: Some(1),
        stdout: None,
        stderr: None,
        error_code: Some("INTERNAL_ERROR".into()),
        hint: None,
        phase: Some("autostart_release".into()),
    });
    let rec = t.recover().unwrap();
    assert_eq!(rec.ok, false);
    assert_eq!(rec.journal_kept, true);
    assert_eq!(rec.error_code, Some("INTERNAL_ERROR"));
    assert_eq!(rec.reason.as_deref(), Some("autostart_release_failed"));
    assert_eq!(std::fs::read(&journal_path).unwrap(), journal_before, "釋放失敗時 journal 必須逐位元組保留");
    t.service().respond(sw_pal::service_ctl::ServiceResult {
        ok: true,
        mode: "systemd-user".into(),
        action: "release_autostart:true".into(),
        rc: Some(0),
        stdout: None,
        stderr: None,
        error_code: None,
        hint: None,
        phase: Some("autostart_release".into()),
    });
    let recovered = t.recover().expect("release failure must be retryable");
    assert!(recovered.ok, "{:?}", recovered.reason);
    assert!(!journal_path.exists());
    assert_eq!(recovered.from_step, "backup_complete");
}

#[test]
fn update_interrupt_injection_every_point_releases_hold() {
    for point in CRASH_POINTS {
        let t = TestEnv::posix()
            .with_install_root()
            .with_installed("0.4.0")
        .with_current("0.4.0", None)
            .with_bundle("0.4.1")
            .with_supervisor_state("systemd-user", true, true)
            .with_idle_daemon_default(4242)
            .with_new_daemon_reporting_version("0.4.1")
            .with_restart_on_failure()
            .with_crash_at(point);
        let t = if point.starts_with("revert_to_previous:") {
            t.with_new_daemon_reporting_version("0.4.0")
        } else {
            t
        };
        let t = if *point == "start_and_verify:after_effect" || *point == "start_and_verify:started" || point.starts_with("revert_to_previous:") {
            t
        } else {
            t.with_rpc_after_relaunch("health.status", "{\"ok\":true,\"pid\":5252,\"version\":\"0.4.0\",\"wal\":{\"healthy\":true}}")
        };
        assert_eq!(t.run_from_bundle(&["setup"]).0, 137, "{point}");
        assert!(t.service().relaunches().is_empty(), "{point}: 中斷前不得 relaunch");
        let mut t = t;
        t.clear_crash();
        let t = t.with_new_daemon_reporting_version("0.4.0");
        let _ = t.recover();
        assert_eq!(t.service().hold_state_now(), HoldState::NotHeld, "{point}: 復原後 hold 必須撤銷");
    }
}
