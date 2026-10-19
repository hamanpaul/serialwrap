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
        std::fs::read(t.root().bin.join("serialwrap.exe")).unwrap(),
        t.version_bytes("0.4.0", "bin/serialwrap.exe")
    );
    assert_eq!(
        std::fs::read(t.root().bin.join("serialwrapd.exe")).unwrap(),
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
        std::fs::read(t.root().bin.join("serialwrap.exe")).unwrap(),
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
        o.index_of("run:schtasks /Change /TN serialwrap /DISABLE")
            < o.index_of("rpc:daemon.handoff_stop")
    );
    assert!(
        o.index_of("rpc:daemon.handoff_stop")
            < o.index_of("run:schtasks /Change /TN serialwrap /ENABLE"),
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
        std::fs::read(t.root().bin.join("serialwrap.exe")).unwrap(),
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
            std::fs::read(t.root().bin.join(exe)).unwrap(),
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
        .with_install_root()
        .with_installed("0.4.0")
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
        .with_install_root()
        .with_installed("0.4.0")
        .with_bundle("0.4.1")
        .with_rpc_ok(TestEnv::win_endpoint(), "health.ping", "{\"ok\":true}")
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
        .with_install_root()
        .with_installed("0.4.0")
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
    assert!(err.starts_with("serialwrap: setup failed: FLASHING_BUSY: "));
    assert_refused_untouched(&t, &j, "update", "FLASHING_BUSY", "0.4.0");
}

#[test]
fn rollback_force_does_not_override_flashing() {
    let t = TestEnv::windows()
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
        .with_install_root()
        .with_installed("0.4.0")
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
        .with_config("supervision_mode: windows-task\n")
        .with_install_root()
        .with_installed("0.4.0")
        .with_bundle("0.4.1")
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
        .with_config("supervision_mode: windows-task\n")
        .with_install_root()
        .with_installed("0.4.0")
        .with_installed("0.4.1")
        .with_current("0.4.1", Some("0.4.0"))
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
        .with_config("supervision_mode: windows-task\n")
        .with_install_root()
        .with_installed("0.4.0")
        .with_bundle("0.4.1")
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
        .with_install_root()
        .with_installed("0.4.0")
        .with_bundle("0.4.1")
        .with_new_daemon_reporting_version("0.4.0");
    let (rc, out, _) = t.run_from_bundle(&["setup"]);
    assert_eq!(rc, 2);
    let j = json(&out);
    assert_eq!(
        (s(&j, "error_code"), s(&j, "phase"), s(&j, "reason")),
        ("INTERNAL_ERROR", "update_start_and_verify", "pairing_mismatch")
    );
    assert!(b(&dict(&j, "update"), "reverted"));
    let cur = sw_cli::install_layout::read_current(&t.root()).unwrap().unwrap();
    assert_eq!(cur.bundle_version, "0.4.0");
    assert_eq!(
        std::fs::read(t.root().bin.join("serialwrap.exe")).unwrap(),
        t.version_bytes("0.4.0", "bin/serialwrap.exe")
    );
    assert!(t.backup_dir_of(&j).join("failure.json").exists());
    assert!(!t.root().root.join("install-journal.json").exists());
}

#[test]
fn journal_persists_original_snapshot_before_disable() {
    let t = TestEnv::windows()
        .with_install_root()
        .with_installed("0.4.0")
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
fn journal_recovery_restarts_original_on_demand_daemon_after_stop() {
    let t = TestEnv::windows()
        .with_install_root()
        .with_installed("0.4.0")
        .with_bundle("0.4.1")
        .with_config("supervision_mode: on-demand\n")
        .with_idle_daemon(TestEnv::win_endpoint(), 4242)
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
            .with_bundle("0.4.1")
            .with_supervisor_state("windows-task", enabled, running);
        if running {
            t = t
                .with_idle_daemon(TestEnv::win_endpoint(), 4242)
                .with_new_daemon_reporting_version("0.4.0");
        }
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
    assert!(
        t.calls().iter().all(|c| !c.join(" ").contains("/Create /XML")),
        "不得以未完成備份重新註冊"
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
        std::fs::read(t.root().bin.join("serialwrap.exe")).unwrap(),
        t.version_bytes("0.4.0", "bin/serialwrap.exe"),
        "entries_switching 前入口不得變動"
    );
}

#[test]
fn journal_recovery_with_unverifiable_backup_keeps_journal() {
    let t = TestEnv::windows()
        .with_install_root()
        .with_installed("0.4.0")
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
        .with_bundle("0.4.1")
        .with_config("supervision_mode: on-demand\n")
        .with_idle_daemon(TestEnv::win_endpoint(), 4242)
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
            < o.index_of_nth("service:set_enabled:true", 1),
        "拒絕後還原 enabled"
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
        .with_bundle("0.4.1")
        .with_supervisor_state("windows-task", true, true)
        .with_idle_daemon(TestEnv::win_endpoint(), 4242)
        .with_crash_at("start_and_verify:after_effect");
    let (cfg0, cur0) = (t.config_bytes(), std::fs::read(t.root().current_file).unwrap());
    assert_eq!(t.run_from_bundle(&["setup"]).0, 137);
    let mut t = t;
    t.clear_crash();
    let t = t
        .with_new_daemon_reporting_version("0.4.0")
        .with_new_daemon_handoff_script(&[BUSY_FLASH, BUSY_FLASH]);
    let j0 = t.journal_bytes().unwrap();
    assert_eq!(t.journal_json().unwrap()["step"], "start_and_verify");
    let h0 = handoffs(&t);
    let rec = t.recover().unwrap();
    assert!(!rec.ok && rec.journal_kept);
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
            .with_bundle("0.4.1")
            .with_supervisor_state("windows-task", true, true)
            .with_idle_daemon(TestEnv::win_endpoint(), 4242)
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
        match t.recover() {
            None => assert!(
                point.starts_with("lock:")
                    || point.starts_with("verify:")
                    || point.starts_with("safety_check:"),
                "{point}: 有副作用的點必須留下 journal"
            ),
            Some(rec) => assert!(rec.ok, "{point}: {:?}", rec.reason),
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
        .with_bundle("0.4.1")
        .with_supervisor_state("systemd-user", true, true)
        .with_idle_daemon(&sys_ep(&TestEnv::posix()), 4242)
        .with_restart_on_failure()
        .with_daemon_exit_code_after_handoff(1)
        .with_new_daemon_reporting_version("0.4.1");
    let (rc, out, _) = t.run_from_bundle(&["setup"]);
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
    let created = t
        .calls()
        .into_iter()
        .find(|c| c.get(1).map(String::as_str) == Some("/Create"))
        .unwrap();
    let xml = String::from_utf16(&utf16le_body(&std::fs::read(&created[3]).unwrap())).unwrap();
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
        .with_bundle("0.4.1")
        .with_config("supervision_mode: on-demand\n")
        .with_idle_daemon(TestEnv::win_endpoint(), 4242)
        .with_instance_appears_at("backup_started:after_config_copy", 5151, &[BUSY_FLASH]);
    let (cfg0, cur0) = (t.config_bytes(), std::fs::read(t.root().current_file).unwrap());
    let (rc, out, err) = t.run_from_bundle(&["setup"]);
    assert_eq!(rc, 2, "{out}");
    assert!(err.starts_with("serialwrap: setup failed: FLASHING_BUSY: "));
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
    assert!(
        o.index_of_nth("rpc:daemon.handoff_stop", 1)
            < o.index_of("service:release_autostart:keep"),
        "拒絕後撤銷 hold（on-demand 另釋放 autostart.lock）"
    );
    assert!(!t.autostart_lock_held(), "autostart.lock 已釋放");
    assert_back_to_original(&t, &cfg0, &cur0);
}

#[test]
fn journal_recovery_releases_persistent_systemd_hold() {
    let t = TestEnv::posix()
        .with_install_root()
        .with_installed("0.4.0")
        .with_bundle("0.4.1")
        .with_supervisor_state("systemd-user", true, true)
        .with_idle_daemon(&sys_ep(&TestEnv::posix()), 4242)
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
        o.index_of("service:release_autostart:true") < o.index_of("service:start"),
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
        .with_bundle("0.4.1")
        .with_supervisor_state("windows-task", true, true)
        .with_idle_daemon(TestEnv::win_endpoint(), 4242)
        .with_new_daemon_reporting_version("0.4.1");
    let (rc, out, _) = t.run_from_bundle(&["setup"]);
    assert_eq!(rc, 0);
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
        .with_install_root()
        .with_installed("0.4.0")
        .with_bundle("0.4.1");
    t.write_file(&t.root().root.join(".install.lock"), b"held\n");
    let (rc, out, _) = t.run_from_bundle(&["setup"]);
    assert_eq!(rc, 2);
    let j = json(&out);
    assert_eq!((s(&j, "error_code"), s(&j, "phase")), ("TIMEOUT", "install_lock"));
    assert!(t.rpc().requests().is_empty());
}

#[test]
fn update_verify_failed_no_side_effects() {
    let t = TestEnv::windows()
        .with_install_root()
        .with_installed("0.4.0")
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
        .with_install_root()
        .with_installed("0.4.0")
        .with_bundle("0.4.1");
    let manifest = r#"{"schema_version":1,"bundle_version":"0.4.1","stage":"rust-1b-test","target":"x86_64-pc-windows-msvc","git_revision":"test","state_schema":"python-state-v1","components":{"cli":{"implementation":"rust","version":"0.4.1","path":"bin/serialwrap.exe","sha256":"0000000000000000000000000000000000000000000000000000000000000000"}}}"#;
    t.write_file(&t.bundle_dir().join("release-manifest.json"), manifest.as_bytes());
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
        .with_install_root()
        .with_installed("0.4.0")
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
        .with_install_root()
        .with_installed("0.4.0")
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
        .with_install_root()
        .with_installed("0.4.0")
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
        .with_install_root()
        .with_installed("0.4.0")
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
        .with_install_root()
        .with_installed("0.4.0")
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
        .with_bundle("0.4.1")
        .with_supervisor_state("windows-task", true, true)
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
    assert!(acts(&t).iter().any(|a| a == "set_enabled:true"));
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
        .with_bundle("0.4.1")
        .with_supervisor_state("windows-task", true, true)
        .with_rpc_ok(TestEnv::win_endpoint(), "health.ping", "{\"ok\":true}")
        .with_rpc_ok(TestEnv::win_endpoint(), "mcu.status", "{\"flashing\":false,\"ok\":true}")
        .with_rpc_ok(TestEnv::win_endpoint(), "session.list", IDLE_LIST)
        .with_rpc_recv_timeout(TestEnv::win_endpoint(), "daemon.handoff_stop");
    let (rc, out, _) = t.run_from_bundle(&["setup"]);
    assert_eq!(rc, 2);
    let j = json(&out);
    assert_eq!(
        (s(&j, "error_code"), s(&j, "phase"), s(&j, "handoff_outcome")),
        ("TIMEOUT", "update_safety_check", "unknown")
    );
    assert!(acts(&t).iter().any(|a| a == "set_enabled:true"));
    assert!(!t.calls().iter().any(|c| c.get(1).map(String::as_str) == Some("/End")));
    assert!(t.rpc().requests().iter().all(|r| r.1 != "daemon.stop"));
}

#[test]
fn update_handoff_malformed_grant_refuses() {
    let t = TestEnv::windows()
        .with_config("supervision_mode: windows-task\n")
        .with_install_root()
        .with_installed("0.4.0")
        .with_bundle("0.4.1")
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
        .with_bundle("0.4.1")
        .with_supervisor_state("windows-task", true, true)
        .with_idle_daemon(TestEnv::win_endpoint(), 4242)
        .with_new_daemon_reporting_version("0.4.1");
    let (rc, out, _) = t.run_from_bundle(&["setup"]);
    assert_eq!(rc, 0);
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
        .with_bundle("0.4.1")
        .with_rpc_ok(TestEnv::win_endpoint(), "health.ping", "{\"ok\":true}");
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
            .with_install_root()
            .with_installed("0.4.0")
            .with_bundle("0.4.1")
            .with_rpc_ok(TestEnv::win_endpoint(), "health.ping", "{\"ok\":true}")
            .with_rpc_ok(TestEnv::win_endpoint(), "mcu.status", "{\"flashing\":false,\"ok\":true}")
            .with_rpc_ok(
                TestEnv::win_endpoint(),
                "session.list",
                &format!("{{\"ok\":true,\"sessions\":[{{\"com\":\"COM0\",{key}:{val},\"foreground_busy\":false,\"outstanding_commands\":0,\"recovering\":false}}]}}"),
            );
        let (rc, out, _) = t.run_from_bundle(&["setup", "--force"]);
        assert_eq!(rc, 2);
        assert_eq!(
            (s(&json(&out), "error_code"), s(&json(&out), "phase")),
            ("SESSION_BUSY", "update_safety_check")
        );
        assert!(t.service().actions().is_empty());
        assert!(t.rpc().requests().iter().all(|r| r.1 != "daemon.stop"));
    }
}

#[test]
fn update_force_does_not_skip_identity_or_probe() {
    let t = TestEnv::windows()
        .with_install_root()
        .with_installed("0.4.0")
        .with_bundle("0.4.1")
        .with_rpc_ok(TestEnv::win_endpoint(), "health.ping", "{\"ok\":true}");
    t.procs().fail();
    let (rc, out, _) = t.run_from_bundle(&["setup", "--force"]);
    assert_eq!(rc, 2);
    assert_eq!(s(&json(&out), "error_code"), "DEVICE_STILL_HELD");

    let t2 = TestEnv::windows()
        .with_install_root()
        .with_installed("0.4.0")
        .with_bundle("0.4.1")
        .with_rpc_ok(TestEnv::win_endpoint(), "health.ping", "{\"ok\":true}")
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
        .with_bundle("0.4.1")
        .with_supervisor_state("windows-task", true, true)
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
    assert!(acts(&t).iter().any(|a| a == "set_enabled:true"));
    assert!(t.rpc().requests().iter().all(|r| r.1 != "daemon.stop"));
    assert!(!t.calls().iter().any(|c| c.get(1).map(String::as_str) == Some("/End")));
}

#[test]
fn update_force_idle_same_result_as_without() {
    let t1 = TestEnv::windows()
        .with_install_root()
        .with_installed("0.4.0")
        .with_bundle("0.4.1")
        .with_supervisor_state("windows-task", true, true)
        .with_idle_daemon(TestEnv::win_endpoint(), 4242)
        .with_new_daemon_reporting_version("0.4.1");
    let (_, out1, _) = t1.run_from_bundle(&["setup"]);

    let t2 = TestEnv::windows()
        .with_install_root()
        .with_installed("0.4.0")
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
        .with_bundle("0.4.1")
        .with_supervisor_state("windows-task", true, true)
        .with_idle_daemon(TestEnv::win_endpoint(), 4242)
        .with_new_daemon_reporting_version("0.4.1");
    let (rc, _, _) = t.run_from_bundle(&["setup"]);
    assert_eq!(rc, 0);
    let o = t.event_order();
    assert!(o.index_of("run:schtasks /Change /TN serialwrap /DISABLE") < o.index_of("rpc:daemon.handoff_stop"));
    assert!(methods(&t).iter().all(|m| m != "daemon.stop"));
    assert!(!t.calls().iter().any(|c| c.get(1).map(String::as_str) == Some("/End")));
}

#[test]
fn update_old_daemon_never_exits_aborts_without_switch() {
    let t = TestEnv::windows()
        .with_install_root()
        .with_installed("0.4.0")
        .with_bundle("0.4.1")
        .with_supervisor_state("windows-task", true, true)
        .with_daemon_alive_forever(TestEnv::win_endpoint(), 4242);
    let (rc, out, _) = t.run_from_bundle(&["setup"]);
    assert_eq!(rc, 1);
    let j = json(&out);
    assert_eq!(
        (s(&j, "error_code"), s(&j, "phase")),
        ("TIMEOUT", "update_confirm_released")
    );
    assert!(acts(&t).iter().any(|a| a == "set_enabled:true"));
    assert_eq!(
        std::fs::read(t.root().bin.join("serialwrap.exe")).unwrap(),
        t.version_bytes("0.4.0", "bin/serialwrap.exe")
    );
    let j_file = t.journal_json().unwrap();
    assert_eq!(j_file["step"], "confirm_released");
    assert!(!j_file["original"]["daemon"].is_null());
}

#[test]
fn update_tty_still_held_posix_aborts() {
    let t = TestEnv::posix()
        .with_install_root()
        .with_installed("0.4.0")
        .with_bundle("0.4.1")
        .with_supervisor_state("systemd-user", true, true)
        .with_idle_daemon(&sys_ep(&TestEnv::posix()), 4242);
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
        .with_bundle("0.4.1")
        .with_supervisor_state("windows-task", true, true)
        .with_idle_daemon(TestEnv::win_endpoint(), 4242)
        .with_new_daemon_reporting_version("0.4.1");
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
        .with_bundle("0.4.1")
        .with_supervisor_state("windows-task", true, true)
        .with_idle_daemon(TestEnv::win_endpoint(), 4242)
        .with_rpc_ok(TestEnv::win_endpoint(), "health.status", "{\"ok\":false}");
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
        .with_bundle("0.4.1")
        .with_supervisor_state("windows-task", true, true)
        .with_idle_daemon(TestEnv::win_endpoint(), 4242)
        .with_rpc_ok(TestEnv::win_endpoint(), "wal.healthy", "{\"ok\":true,\"healthy\":false}");
    let (rc, out, _) = t.run_from_bundle(&["setup"]);
    assert_eq!(rc, 2);
    let j = json(&out);
    assert_eq!(s(&j, "reason"), "wal_unhealthy");
    assert!(b(&dict(&j, "update"), "reverted"));
}

#[test]
fn update_released_session_auto_attached_reverts() {
    let t = TestEnv::windows()
        .with_install_root()
        .with_installed("0.4.0")
        .with_bundle("0.4.1")
        .with_supervisor_state("windows-task", true, true)
        .with_idle_daemon(TestEnv::win_endpoint(), 4242)
        .with_rpc_ok(
            TestEnv::win_endpoint(),
            "session.list",
            "{\"ok\":true,\"sessions\":[{\"com\":\"COM1\",\"state\":\"READY\",\"foreground_busy\":false,\"outstanding_commands\":0,\"interactive_session_id\":null,\"recovering\":false}]}",
        );
    t.write_file(&t.state_dir().join("state.json"), b"{\"sessions\":{\"COM1\":{\"state\":\"RELEASED\"}}}\n");
    let (rc, out, _) = t.run_from_bundle(&["setup"]);
    assert_eq!(rc, 2);
    let j = json(&out);
    assert_eq!(s(&j, "reason"), "state_incompatible");
    assert!(b(&dict(&j, "update"), "reverted"));
}

#[test]
fn update_smoke_failure_reverts() {
    let t = TestEnv::windows()
        .with_install_root()
        .with_installed("0.4.0")
        .with_bundle("0.4.1")
        .with_supervisor_state("windows-task", true, true)
        .with_idle_daemon(TestEnv::win_endpoint(), 4242)
        .with_rpc_ok(TestEnv::win_endpoint(), "device.list", "{\"ok\":false}");
    let (rc, out, _) = t.run_from_bundle(&["setup"]);
    assert_eq!(rc, 2);
    let j = json(&out);
    assert_eq!(s(&j, "reason"), "smoke_failed");
    assert!(b(&dict(&j, "update"), "reverted"));
}

#[test]
fn update_materializes_assets_preserving_profiles() {
    let t = TestEnv::windows()
        .with_install_root()
        .with_installed("0.4.0")
        .with_bundle("0.4.1")
        .with_supervisor_state("windows-task", true, true)
        .with_idle_daemon(TestEnv::win_endpoint(), 4242)
        .with_new_daemon_reporting_version("0.4.1");
    let user_profile = b"custom: profile\n";
    t.write_file(&t.config_dir().join("profiles").join("default.yaml"), user_profile);
    let (rc, _, _) = t.run_from_bundle(&["setup"]);
    assert_eq!(rc, 0);
    assert_eq!(
        std::fs::read(t.config_dir().join("profiles").join("default.yaml")).unwrap(),
        user_profile,
        "既有 profile 必須被保留"
    );
}

#[test]
fn journal_recovery_after_crash_mid_switch() {
    let t = TestEnv::windows()
        .with_install_root()
        .with_installed("0.4.0")
        .with_bundle("0.4.1")
        .with_supervisor_state("windows-task", true, true)
        .with_idle_daemon(TestEnv::win_endpoint(), 4242)
        .with_crash_at("entries_switching:after_first_rename");
    assert_eq!(t.run_from_bundle(&["setup"]).0, 137);
    let mut t = t;
    t.clear_crash();
    let (rc, out, _) = t.run_from_bundle(&["setup"]);
    assert_eq!(rc, 0);
    let j = json(&out);
    assert_eq!(j["recovered_journal"]["ok"], true);
    assert_eq!(j["recovered_journal"]["from_step"], "entries_switching");
    assert_eq!(j["recovered_journal"]["backup_used"], true);
    assert!(t.journal_json().is_none());
}

#[test]
fn journal_write_failure_aborts_before_effect() {
    let t = TestEnv::windows()
        .with_install_root()
        .with_installed("0.4.0")
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
    let state_bytes = b"{\"sessions\":{\"COM0\":{\"active\":true}}}\n";
    t.write_file(&t.state_dir().join("state.json"), state_bytes);
    let (rc, _, _) = t.run(&["setup", "--rollback"]);
    assert_eq!(rc, 0);
    assert_eq!(
        std::fs::read(t.state_dir().join("state.json")).unwrap(),
        state_bytes
    );
}

#[test]
fn update_release_then_rollback_keeps_released() {
    let t = TestEnv::windows()
        .with_install_root()
        .with_installed("0.4.0")
        .with_installed("0.4.1")
        .with_current("0.4.1", Some("0.4.0"))
        .with_supervisor_state("windows-task", true, true)
        .with_idle_daemon(TestEnv::win_endpoint(), 4242)
        .with_new_daemon_reporting_version("0.4.0");
    t.write_file(&t.state_dir().join("state.json"), b"{\"sessions\":{\"COM1\":{\"state\":\"RELEASED\"}}}\n");
    let (rc, _, _) = t.run(&["setup", "--rollback"]);
    assert_eq!(rc, 0);
    let state = std::fs::read_to_string(t.state_dir().join("state.json")).unwrap();
    assert!(state.contains("RELEASED"));
}

#[test]
fn rollback_refuses_incompatible_state_schema() {
    let t = TestEnv::windows()
        .with_install_root()
        .with_installed("0.4.0")
        .with_installed("0.4.1")
        .with_current("0.4.1", Some("0.4.0"));
    let prev_manifest = r#"{"schema_version":1,"bundle_version":"0.4.0","stage":"rust-1b-test","target":"x86_64-pc-windows-msvc","git_revision":"test","state_schema":"old-v0","components":{"cli":{"implementation":"rust","version":"0.4.0","path":"bin/serialwrap.exe","sha256":"0000000000000000000000000000000000000000000000000000000000000000"}}}"#;
    t.write_file(&t.root().versions.join("0.4.0").join("release-manifest.json"), prev_manifest.as_bytes());
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
    assert!(t.calls().iter().any(|c| c.join(" ").contains("/Create /XML")));
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
    let (rc, _, _) = t.run(&["setup", "--rollback"]);
    assert_eq!(rc, 0);
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
        .with_spawn_exits(1);
    let (rc, out, _) = t.run(&["setup", "--rollback"]);
    assert_eq!(rc, 2);
    let j = json(&out);
    assert!(b(&dict(&j, "rollback"), "reverted"));
}

#[test]
fn systemd_rollback_daemon_reload_after_unit_restore() {
    let t = TestEnv::posix()
        .with_install_root()
        .with_installed("0.4.0")
        .with_installed("0.4.1")
        .with_current("0.4.1", Some("0.4.0"))
        .with_supervisor_state("systemd-user", true, true)
        .with_idle_daemon(&sys_ep(&TestEnv::posix()), 4242)
        .with_new_daemon_reporting_version("0.4.0");
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
    let (rc1, _, _) = t.run(&["setup", "--rollback"]);
    assert_eq!(rc1, 0);
    let cur1 = sw_cli::install_layout::read_current(&t.root()).unwrap().unwrap();
    assert_eq!(cur1.bundle_version, "0.4.0");

    let (rc2, _, _) = t.run(&["setup", "--rollback"]);
    assert_eq!(rc2, 0);
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
        std::fs::read(t.root().bin.join("serialwrap.exe")).unwrap(),
        t.version_bytes("0.4.0", "bin/serialwrap.exe")
    );
    assert_eq!(
        std::fs::read(t.root().bin.join("serialwrapd.exe")).unwrap(),
        t.version_bytes("0.4.0", "bin/serialwrapd.exe")
    );
}

#[test]
fn update_start_failure_writes_failure_json() {
    let t = TestEnv::windows()
        .with_install_root()
        .with_installed("0.4.0")
        .with_bundle("0.4.1")
        .with_supervisor_state("windows-task", true, true)
        .with_idle_daemon(TestEnv::win_endpoint(), 4242)
        .with_spawn_exits(1);
    let (rc, out, _) = t.run_from_bundle(&["setup"]);
    assert_eq!(rc, 2);
    let j = json(&out);
    assert!(t.backup_dir_of(&j).join("failure.json").exists());
}

#[test]
fn update_on_demand_not_running_skips_daemon_start() {
    let t = TestEnv::windows()
        .with_install_root()
        .with_installed("0.4.0")
        .with_bundle("0.4.1")
        .with_config("supervision_mode: on-demand\n");
    let (rc, _, _) = t.run_from_bundle(&["setup"]);
    assert_eq!(rc, 0);
    assert!(t.process().spawns().is_empty());
}

#[test]
fn fresh_install_materializes_profiles_and_sets_windows_task() {
    let t = TestEnv::windows()
        .with_install_root()
        .with_bundle("0.4.1");
    let (rc, out, _) = t.run_from_bundle(&["setup"]);
    assert_eq!(rc, 0);
    let j = json(&out);
    assert_eq!(j["install"]["to_version"], "0.4.1");
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
        .with_bundle("0.4.1")
        .with_supervisor_state("windows-task", true, true)
        .with_idle_daemon(TestEnv::win_endpoint(), 4242)
        .with_new_daemon_reporting_version("0.4.0")
        .with_new_daemon_handoff_script(&["{\"ok\":false,\"error_code\":\"METHOD_NOT_FOUND\"}"]);
    let (rc, out, _) = t.run_from_bundle(&["setup"]);
    assert_eq!(rc, 2);
    let j = json(&out);
    assert_eq!(j["update"]["reverted"], false);
    assert_eq!(j["update"]["revert_pending"], true);
    assert!(t.journal_json().is_some());
}

#[test]
fn revert_handoff_recv_timeout_no_hard_stop() {
    let t = TestEnv::windows()
        .with_install_root()
        .with_installed("0.4.0")
        .with_bundle("0.4.1")
        .with_supervisor_state("windows-task", true, true)
        .with_idle_daemon(TestEnv::win_endpoint(), 4242)
        .with_new_daemon_reporting_version("0.4.0")
        .with_rpc_recv_timeout(TestEnv::win_endpoint(), "wal.healthy")
        .with_new_daemon_handoff_script(&["<recv_timeout>"]);
    let (rc, out, _) = t.run_from_bundle(&["setup"]);
    assert_eq!(rc, 2);
    let j = json(&out);
    assert_eq!(j["update"]["revert_pending"], true);
    assert!(!t.calls().iter().any(|c| c.get(1).map(String::as_str) == Some("/End")));
    assert!(t.journal_json().is_some());
}

#[test]
fn revert_new_daemon_rpc_down_process_alive_defers() {
    let t = TestEnv::windows()
        .with_install_root()
        .with_installed("0.4.0")
        .with_bundle("0.4.1")
        .with_supervisor_state("windows-task", true, true)
        .with_idle_daemon(TestEnv::win_endpoint(), 4242)
        .with_rpc_refused(TestEnv::win_endpoint())
        .with_procs(vec![DaemonProc::win(5555, TestEnv::win_endpoint())]);
    let (rc, out, _) = t.run_from_bundle(&["setup"]);
    assert_eq!(rc, 2);
    let j = json(&out);
    assert_eq!(j["update"]["revert_pending"], true);
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
    assert_eq!(rc, 2);
    let j = json(&out);
    assert_eq!(j["rollback"]["reverted"], false);
    assert_eq!(j["rollback"]["revert_pending"], true);
}

#[test]
fn update_systemd_system_hold_needs_sudo() {
    let t = TestEnv::posix()
        .with_install_root()
        .with_installed("0.4.0")
        .with_bundle("0.4.1")
        .with_supervisor_state("systemd-system", true, true)
        .with_idle_daemon(&sys_ep(&TestEnv::posix()), 4242);
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
        .with_bundle("0.4.1")
        .with_supervisor_state("systemd-user", true, true)
        .with_idle_daemon(&sys_ep(&TestEnv::posix()), 4242);
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
    let (rc, out, _) = t.run_from_bundle(&["setup"]);
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
        .with_bundle("0.4.1")
        .with_supervisor_state("systemd-user", true, false)
        .with_rpc_refused(&sys_ep(&TestEnv::posix()))
        .with_new_daemon_reporting_version("0.4.1");
    let (rc, _, _) = t.run_from_bundle(&["setup"]);
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
        .with_bundle("0.4.1")
        .with_config("supervision_mode: on-demand\n")
        .with_idle_daemon(TestEnv::win_endpoint(), 4242)
        .with_new_daemon_reporting_version("0.4.1");
    let (rc, _, _) = t.run_from_bundle(&["setup"]);
    assert_eq!(rc, 0);
}

#[test]
fn update_busy_handoff_releases_hold() {
    let t = TestEnv::posix()
        .with_install_root()
        .with_installed("0.4.0")
        .with_bundle("0.4.1")
        .with_supervisor_state("systemd-user", true, true)
        .with_idle_daemon(&sys_ep(&TestEnv::posix()), 4242)
        .with_rpc_ok(
            &sys_ep(&TestEnv::posix()),
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
        .with_bundle("0.4.1")
        .with_supervisor_state("systemd-user", true, true)
        .with_idle_daemon(&sys_ep(&TestEnv::posix()), 4242)
        .with_supervisor_ignores_hold()
        .with_restart_on_failure()
        .with_daemon_exit_code_after_handoff(1)
        .with_new_daemon_reporting_version("0.4.1");
    let (rc, out, _) = t.run_from_bundle(&["setup"]);
    assert_eq!(rc, 0);
    assert_eq!(json(&out)["ok"], true);
}

#[test]
fn update_hard_stop_never_targets_relaunched_identity() {
    let t = TestEnv::windows()
        .with_install_root()
        .with_installed("0.4.0")
        .with_bundle("0.4.1")
        .with_supervisor_state("windows-task", true, true)
        .with_idle_daemon(TestEnv::win_endpoint(), 4242)
        .with_new_daemon_reporting_version("0.4.1");
    let (rc, _, _) = t.run_from_bundle(&["setup"]);
    assert_eq!(rc, 0);
    assert!(acts(&t).iter().all(|a| a != "hard_stop"));
}

#[test]
fn update_relaunch_loop_aborts_without_switch() {
    let t = TestEnv::posix()
        .with_install_root()
        .with_installed("0.4.0")
        .with_bundle("0.4.1")
        .with_supervisor_state("systemd-user", true, true)
        .with_idle_daemon(&sys_ep(&TestEnv::posix()), 4242);
    let (rc, out, _) = t.run_from_bundle(&["setup"]);
    assert_eq!(rc, 2);
    let j = json(&out);
    assert_eq!(s(&j, "phase"), "update_confirm_released");
}

#[test]
fn update_instance_before_start_rehandoff() {
    let t = TestEnv::posix()
        .with_install_root()
        .with_installed("0.4.0")
        .with_bundle("0.4.1")
        .with_supervisor_state("systemd-user", true, true)
        .with_idle_daemon(&sys_ep(&TestEnv::posix()), 4242)
        .with_new_daemon_reporting_version("0.4.1");
    let (rc, out, _) = t.run_from_bundle(&["setup"]);
    assert_eq!(rc, 0);
    assert_eq!(json(&out)["ok"], true);
}

#[test]
fn revert_rehold_before_new_daemon_handoff() {
    let t = TestEnv::posix()
        .with_install_root()
        .with_installed("0.4.0")
        .with_bundle("0.4.1")
        .with_supervisor_state("systemd-user", true, true)
        .with_idle_daemon(&sys_ep(&TestEnv::posix()), 4242)
        .with_restart_on_failure()
        .with_new_daemon_reporting_version("0.4.0");
    let (rc, out, _) = t.run_from_bundle(&["setup"]);
    assert_eq!(rc, 2);
    assert!(b(&dict(&json(&out), "update"), "reverted"));
}

#[test]
fn revert_deferred_releases_hold() {
    let t = TestEnv::posix()
        .with_install_root()
        .with_installed("0.4.0")
        .with_bundle("0.4.1")
        .with_supervisor_state("systemd-user", true, true)
        .with_idle_daemon(&sys_ep(&TestEnv::posix()), 4242)
        .with_new_daemon_reporting_version("0.4.0")
        .with_new_daemon_handoff_script(&[BUSY_PUSH]);
    let (rc, out, _) = t.run_from_bundle(&["setup"]);
    assert_eq!(rc, 2);
    let j = json(&out);
    assert_eq!(j["update"]["revert_pending"], true);
    assert_eq!(t.service().hold_state_now(), HoldState::NotHeld);
}

#[test]
fn journal_recovery_release_failure_keeps_journal() {
    let t = TestEnv::posix()
        .with_install_root()
        .with_installed("0.4.0")
        .with_bundle("0.4.1")
        .with_supervisor_state("systemd-user", true, true)
        .with_idle_daemon(&sys_ep(&TestEnv::posix()), 4242)
        .with_crash_at("backup_complete:after_effect");
    assert_eq!(t.run_from_bundle(&["setup"]).0, 137);
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
}

#[test]
fn update_interrupt_injection_every_point_releases_hold() {
    for point in CRASH_POINTS {
        let t = TestEnv::posix()
            .with_install_root()
            .with_installed("0.4.0")
            .with_bundle("0.4.1")
            .with_supervisor_state("systemd-user", true, true)
            .with_idle_daemon(&sys_ep(&TestEnv::posix()), 4242)
            .with_restart_on_failure()
            .with_crash_at(point);
        let t = if point.starts_with("revert_to_previous:") {
            t.with_new_daemon_reporting_version("0.4.0")
        } else {
            t
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
