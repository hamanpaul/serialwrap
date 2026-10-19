//! P4-T12 `supervision-mode set` 交接護欄（SC-22，R1-F3）的失敗測試。
//!
//! 紅燈階段（TDD）：`run_supervision_switch` 與交接護欄尚未實作完成，
//! 本檔案的案例應以 `not implemented: P4-T12` 或斷言不符失敗，不得因語法或編譯錯誤失敗。
//! 規格來源：`docs/rust-refactor/plans/05-stage1b-rust-cli-mgmt.md` Task 12；盤點 `inventory/07-platform-mgmt.md` §12.1。

mod support;

use support::*;

const EP: &str = "tcp://127.0.0.1:48700";
const IDLE_LIST: &str = "{\"ok\":true,\"sessions\":[{\"com\":\"COM0\",\"state\":\"READY\",\"foreground_busy\":false,\"outstanding_commands\":0,\"interactive_session_id\":null,\"recovering\":false}]}";
const FLASHING_LIST: &str = "{\"ok\":true,\"sessions\":[{\"com\":\"COM0\",\"state\":\"FLASHING\",\"foreground_busy\":false,\"outstanding_commands\":0,\"interactive_session_id\":null,\"recovering\":false}]}";

fn wintask_env() -> TestEnv {
    TestEnv::windows()
        .with_supervisor_state("windows-task", true, true)
        .with_procs(vec![DaemonProc::win(4242, EP)])
}

fn assert_switch_refused_untouched(
    t: &TestEnv,
    j: &serde_json::Value,
    code: &str,
    cfg_before: &[u8],
) {
    assert_eq!(
        (s(j, "error_code"), s(j, "phase")),
        (code, "supervision_set_safety_check")
    );
    assert_eq!(s(j, "previous_mode"), "windows-task");
    assert!(j.get("setup").is_none(), "不得進入 reconcile");
    assert!(t.service().actions().is_empty(), "不得 disable／stop／End／install 監管");
    assert!(t.calls().iter().all(|c| !matches!(
        c.first().map(String::as_str),
        Some("schtasks" | "systemctl" | "sc.exe" | "sudo")
    )));
    assert!(
        t.rpc()
            .requests()
            .iter()
            .all(|r| r.1 != "daemon.stop" && r.1 != "daemon.handoff_stop"),
        "不得要求 daemon 停止或交接"
    );
    assert_eq!(t.config_bytes(), cfg_before, "config 位元組不變");
    assert_eq!(t.service().query_now().enabled, Some(true));
    assert_eq!(t.rpc_now(EP, "health.ping")["ok"], true, "daemon 仍在服務");
}

#[test]
fn supervision_set_force_refuses_flashing() {
    let t = wintask_env()
        .with_rpc_ok(EP, "health.ping", "{\"ok\":true}")
        .with_rpc_ok(EP, "mcu.status", "{\"flashing\":true,\"ok\":true}")
        .with_rpc_ok(EP, "session.list", FLASHING_LIST);
    let cfg = t.config_bytes();
    let (rc, out, _) = t.run(&["supervision-mode", "set", "on-demand", "--force"]);
    assert_eq!(rc, 2, "{out}");
    let j = json(&out);
    assert_eq!(
        s(&j, "message"),
        "flash 進行中，拒絕切換監管模式（--force 不適用，請待 flash 完成後重試）"
    );
    assert_eq!(j["force_ignored"], true);
    assert_switch_refused_untouched(&t, &j, "FLASHING_BUSY", &cfg);
}

#[test]
fn supervision_set_probe_failure_has_no_side_effects() {
    for (probe, force) in [
        ("mcu.status", false),
        ("mcu.status", true),
        ("session.list", false),
    ] {
        let mut t = wintask_env().with_rpc_ok(EP, "health.ping", "{\"ok\":true}");
        t = if probe == "mcu.status" {
            t.with_rpc_recv_timeout(EP, "mcu.status")
                .with_rpc_ok(EP, "session.list", IDLE_LIST)
        } else {
            t.with_rpc_ok(EP, "mcu.status", "{\"flashing\":false,\"ok\":true}")
                .with_rpc_recv_timeout(EP, "session.list")
        };
        let cfg = t.config_bytes();
        let mut args = vec!["supervision-mode", "set", "on-demand"];
        if force {
            args.push("--force");
        }
        let (rc, out, _) = t.run(&args);
        assert_eq!(rc, 2, "{probe} {force}: {out}");
        let j = json(&out);
        assert_eq!(
            (s(&j, "probe"), s(&j, "health")),
            (probe, "unknown")
        );
        assert_eq!(
            j.get("force_ignored")
                .and_then(|v| v.as_bool())
                .unwrap_or(false),
            force
        );
        assert_switch_refused_untouched(&t, &j, "TIMEOUT", &cfg);
    }
}

#[test]
fn supervision_set_file_push_after_probe_handoff_refused() {
    let t = wintask_env()
        .with_rpc_ok(EP, "health.ping", "{\"ok\":true}")
        .with_rpc_ok(EP, "mcu.status", "{\"flashing\":false,\"ok\":true}")
        .with_rpc_ok(EP, "session.list", IDLE_LIST)
        .with_rpc_ok(
            EP,
            "daemon.handoff_stop",
            "{\"ok\":false,\"error_code\":\"SESSION_BUSY\",\"busy\":[{\"com\":\"COM0\",\"state\":\"READY\",\"foreground_busy\":true,\"outstanding_commands\":0,\"interactive_session_id\":null,\"recovering\":false,\"ops\":[\"file.push\"]}]}",
        );
    let cfg = t.config_bytes();
    let (rc, out, _) = t.run(&["supervision-mode", "set", "on-demand", "--force"]);
    assert_eq!(rc, 2, "{out}");
    let j = json(&out);
    assert_eq!(
        (s(&j, "error_code"), s(&j, "phase"), s(&j, "handoff")),
        ("SESSION_BUSY", "supervision_set_safety_check", "refused")
    );
    assert_eq!(j["busy"][0]["ops"][0], "file.push");
    assert_eq!(rpc_params(&t, "daemon.handoff_stop")["reason"], "supervision_set");
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
    assert_eq!(t.config_bytes(), cfg);
    assert_eq!(t.service().query_now().enabled, Some(true));
}

#[test]
fn supervision_set_probe_bad_response_refuses() {
    for resp in [
        "{\"ok\":true}",
        "{\"ok\":false,\"error_code\":\"INTERNAL_ERROR\"}",
    ] {
        let t = wintask_env()
            .with_rpc_ok(EP, "health.ping", "{\"ok\":true}")
            .with_rpc_ok(EP, "mcu.status", resp);
        let cfg = t.config_bytes();
        let (rc, out, _) = t.run(&["supervision-mode", "set", "on-demand"]);
        assert_eq!(rc, 2);
        let j = json(&out);
        assert_eq!(s(&j, "phase"), "supervision_set_safety_check");
        assert_switch_refused_untouched(&t, &j, s(&j, "error_code"), &cfg);
    }
}

#[test]
fn supervision_set_ping_down_process_alive_refuses() {
    let t = wintask_env()
        .with_rpc_refused(EP)
        .with_procs(vec![DaemonProc::win(4242, EP)]);
    let cfg = t.config_bytes();
    let (rc, out, _) = t.run(&["supervision-mode", "set", "on-demand", "--force"]);
    assert_eq!(rc, 2);
    let j = json(&out);
    assert_eq!(
        (s(&j, "error_code"), s(&j, "phase")),
        ("DEVICE_STILL_HELD", "supervision_set_safety_check")
    );
    assert_eq!(j["pids"][0], 4242);
    assert_eq!(t.config_bytes(), cfg);
}

#[test]
fn supervision_set_procscan_failure_refuses() {
    let t = wintask_env().with_rpc_refused(EP);
    t.procs().fail();
    let cfg = t.config_bytes();
    let (rc, out, _) = t.run(&["supervision-mode", "set", "on-demand", "--force"]);
    assert_eq!(rc, 2);
    let j = json(&out);
    assert_eq!(
        (s(&j, "error_code"), s(&j, "phase")),
        ("DEVICE_STILL_HELD", "supervision_set_safety_check")
    );
    assert_eq!(j["holder_unknown"], true);
    assert_eq!(t.config_bytes(), cfg);
}

#[test]
fn supervision_set_refuses_interactive_or_recovering_or_command() {
    for (key, val) in [
        ("interactive_session_id", "\"L1\""),
        ("state", "\"RECOVERING\""),
        ("outstanding_commands", "1"),
    ] {
        let t = wintask_env()
            .with_rpc_ok(EP, "health.ping", "{\"ok\":true}")
            .with_rpc_ok(EP, "mcu.status", "{\"flashing\":false,\"ok\":true}")
            .with_rpc_ok(
                EP,
                "session.list",
                &format!("{{\"ok\":true,\"sessions\":[{{\"com\":\"COM0\",{key}:{val},\"foreground_busy\":false,\"outstanding_commands\":0,\"interactive_session_id\":null,\"recovering\":false}}]}}"),
            );
        let cfg = t.config_bytes();
        let (rc, out, _) = t.run(&["supervision-mode", "set", "on-demand", "--force"]);
        assert_eq!(rc, 2);
        let j = json(&out);
        assert_eq!(
            (s(&j, "error_code"), s(&j, "phase")),
            ("SESSION_BUSY", "supervision_set_safety_check")
        );
        assert_eq!(j["busy"][0]["com"], "COM0");
        assert_switch_refused_untouched(&t, &j, "SESSION_BUSY", &cfg);
    }
}

#[test]
fn supervision_set_flash_races_handoff_refused() {
    let t = TestEnv::posix()
        .with_config("supervision_mode: on-demand\n")
        .with_idle_daemon(&TestEnv::posix().default_socket(), 4242)
        .with_rpc_ok(
            &TestEnv::posix().default_socket(),
            "daemon.handoff_stop",
            "{\"ok\":false,\"error_code\":\"FLASHING_BUSY\",\"busy\":[{\"com\":\"COM0\",\"state\":\"FLASHING\",\"foreground_busy\":false,\"outstanding_commands\":0,\"interactive_session_id\":null,\"recovering\":false,\"ops\":[\"flash\"]}]}",
        );
    let (rc, out, _) = t.run(&["supervision-mode", "set", "systemd-user", "--force"]);
    assert_eq!(rc, 2);
    let j = json(&out);
    assert_eq!(
        (s(&j, "error_code"), s(&j, "handoff")),
        ("FLASHING_BUSY", "refused")
    );
    assert!(methods(&t).iter().all(|m| m != "daemon.stop"));
    assert!(t.calls().iter().all(|c| !c.join(" ").contains("systemctl")));
}

#[test]
fn supervision_set_handoff_unsupported_refuses_without_stop() {
    let t = wintask_env()
        .with_rpc_ok(EP, "health.ping", "{\"ok\":true}")
        .with_rpc_ok(EP, "mcu.status", "{\"flashing\":false,\"ok\":true}")
        .with_rpc_ok(EP, "session.list", IDLE_LIST)
        .with_rpc_ok(EP, "daemon.handoff_stop", "{\"ok\":false,\"error_code\":\"METHOD_NOT_FOUND\"}");
    let cfg = t.config_bytes();
    let (rc, out, _) = t.run(&["supervision-mode", "set", "on-demand"]);
    assert_eq!(rc, 2);
    let j = json(&out);
    assert_eq!(
        (s(&j, "error_code"), s(&j, "probe"), s(&j, "handoff_outcome")),
        ("METHOD_NOT_FOUND", "daemon.handoff_stop", "refused")
    );
    assert!(methods(&t).iter().all(|m| m != "daemon.stop"));
    assert!(!t.calls().iter().any(|c| c.get(1).map(String::as_str) == Some("/End")));
    assert_eq!(t.config_bytes(), cfg);
}

#[test]
fn supervision_set_handoff_recv_timeout_no_hard_stop() {
    let t = wintask_env()
        .with_rpc_ok(EP, "health.ping", "{\"ok\":true}")
        .with_rpc_ok(EP, "mcu.status", "{\"flashing\":false,\"ok\":true}")
        .with_rpc_ok(EP, "session.list", IDLE_LIST)
        .with_rpc_recv_timeout(EP, "daemon.handoff_stop");
    let cfg = t.config_bytes();
    let (rc, out, _) = t.run(&["supervision-mode", "set", "on-demand"]);
    assert_eq!(rc, 2);
    let j = json(&out);
    assert_eq!(
        (s(&j, "error_code"), s(&j, "handoff_outcome")),
        ("TIMEOUT", "unknown")
    );
    assert!(!t.calls().iter().any(|c| c.get(1).map(String::as_str) == Some("/End")));
    assert!(methods(&t).iter().all(|m| m != "daemon.stop"));
    assert_eq!(t.config_bytes(), cfg);
}

#[test]
fn supervision_set_idle_uses_handoff_not_daemon_stop() {
    let ep = TestEnv::posix().default_socket();
    let t = TestEnv::posix()
        .with_systemd(true)
        .with_var("USER", "alice")
        .with_config("supervision_mode: on-demand\n")
        .with_idle_daemon(&ep, 4321);
    let (rc, out, _) = t.run(&["supervision-mode", "set", "systemd-user"]);
    assert_eq!(rc, 0);
    let j = json(&out);
    assert_eq!(s(&j, "previous_mode"), "on-demand");
    assert_eq!(j["setup"]["mode"], "systemd-user");
    assert!(methods(&t).iter().all(|m| m != "daemon.stop"));
    assert_eq!(handoffs(&t), 1);
    assert_eq!(rpc_params(&t, "daemon.handoff_stop")["reason"], "supervision_set");
    let o = t.event_order();
    assert!(o.index_of("rpc:daemon.handoff_stop") < o.index_of("run:systemctl --user start serialwrap"));
}

#[test]
fn supervision_set_no_daemon_switches_without_stop_rpc() {
    let ep = TestEnv::posix().default_socket();
    let t = TestEnv::posix()
        .with_systemd(true)
        .with_var("USER", "alice")
        .with_supervisor_state("systemd-user", true, false)
        .with_rpc_refused(&ep);
    let (rc, _, _) = t.run(&["supervision-mode", "set", "on-demand"]);
    assert_eq!(rc, 0);
    assert!(methods(&t).is_empty());
    assert!(t.calls().iter().all(|c| !c.join(" ").contains("systemctl --user stop")));
    assert_eq!(t.service().hold_state_now(), HoldState::NotHeld);
}

#[test]
fn supervision_set_no_daemon_precheck_relaunch_flash_refused() {
    let ep = TestEnv::posix().default_socket();
    let t = TestEnv::posix()
        .with_systemd(true)
        .with_var("USER", "alice")
        .with_supervisor_state("systemd-user", true, false)
        .with_rpc_refused(&ep)
        .with_instance_appears_at("supervision_set:after_safety_check", 7070, &["flash"]);
    let (rc, out, _) = t.run(&["supervision-mode", "set", "on-demand"]);
    assert_eq!(rc, 2);
    let j = json(&out);
    assert_eq!(s(&j, "error_code"), "FLASHING_BUSY");
    assert_eq!(j["relaunched"]["pid"], 7070);
    assert_eq!(s(&j, "handoff"), "refused");
    assert!(t.calls().iter().all(|c| !c.join(" ").contains("systemctl --user stop")));
    assert_eq!(t.service().hold_state_now(), HoldState::NotHeld);
}

#[test]
fn supervision_set_crash_after_hold_recovers_on_next_setup() {
    let ep = TestEnv::posix().default_socket();
    let t = TestEnv::posix()
        .with_systemd(true)
        .with_var("USER", "alice")
        .with_supervisor_state("systemd-user", true, true)
        .with_idle_daemon(&ep, 4242)
        .with_fault_at("supervision_set:after_hold");
    assert_eq!(t.run(&["supervision-mode", "set", "on-demand"]).0, 137);

    let (rc, out, _) = t.run(&["setup"]);
    assert_eq!(rc, 0);
    let j = json(&out);
    assert_eq!(j["recovered_autostart_hold"]["action"], "restored");
    assert_eq!(t.service().hold_state_now(), HoldState::NotHeld);
}

#[test]
fn supervision_set_recover_ambiguous_state_refuses() {
    let ep = TestEnv::posix().default_socket();
    let t = TestEnv::posix()
        .with_systemd(true)
        .with_var("USER", "alice")
        .with_supervisor_state("systemd-user", true, true)
        .with_idle_daemon(&ep, 4242);
    t.write_file(
        &t.state_dir().join("autostart-hold.json"),
        b"{\"mode\":\"systemd-user\",\"target\":\"on-demand\",\"prior_enabled\":true}\n",
    );
    let (rc, out, _) = t.run(&["supervision-mode", "set", "on-demand"]);
    assert_eq!(rc, 2);
    let j = json(&out);
    assert_eq!(s(&j, "phase"), "supervision_set_recover");
}

#[test]
fn supervision_set_relaunched_busy_releases_hold() {
    let t = wintask_env()
        .with_supervisor_ignores_hold()
        .with_idle_daemon(EP, 4242);
    let (rc, out, _) = t.run(&["supervision-mode", "set", "on-demand"]);
    assert_eq!(rc, 2);
    let j = json(&out);
    assert!(j.get("relaunched").is_some());
}

#[test]
fn supervision_set_hold_record_write_failure_no_side_effects() {
    let t = wintask_env()
        .with_idle_daemon(EP, 4242)
        .with_fs_fault_replace_suffix("autostart-hold.json");
    let cfg = t.config_bytes();
    let (rc, out, _) = t.run(&["supervision-mode", "set", "on-demand"]);
    assert_eq!(rc, 2);
    let j = json(&out);
    assert_eq!(s(&j, "reason"), "autostart_hold_record");
    assert!(t.service().actions().is_empty());
    assert!(methods(&t).is_empty());
    assert_eq!(t.config_bytes(), cfg);
}

#[test]
fn supervision_set_same_mode_or_pending_only_never_hands_off() {
    let t = wintask_env().with_idle_daemon(EP, 4242);
    let (rc, _, _) = t.run(&["supervision-mode", "set", "windows-task"]);
    assert_eq!(rc, 0);
    assert_eq!(handoffs(&t), 0);
    assert_eq!(t.rpc_now(EP, "health.ping")["ok"], true);

    let (_rc_win, out_win, _) = t.run(&["supervision-mode", "set", "windows-service"]);
    let j_win = json(&out_win);
    assert_eq!(j_win["applied"], false);
    assert_eq!(j_win["pending_sudo"], true);
}

#[test]
fn supervision_set_timeout_after_grant_exit1() {
    let t = wintask_env().with_daemon_alive_forever(EP, 4242);
    let (rc, out, _) = t.run(&["supervision-mode", "set", "on-demand"]);
    assert_eq!(rc, 1);
    let j = json(&out);
    assert_eq!(
        (s(&j, "error_code"), s(&j, "phase")),
        ("TIMEOUT", "reconcile_wait_old_exit")
    );
    assert_eq!(t.service().hold_state_now(), HoldState::Held);
    assert!(t.state_dir().join("autostart-hold.json").exists());
}

#[test]
fn supervision_set_force_idle_same_result_as_without() {
    let t1 = wintask_env()
        .with_rpc_ok(EP, "health.ping", "{\"ok\":true}")
        .with_rpc_ok(EP, "mcu.status", "{\"flashing\":false,\"ok\":true}")
        .with_rpc_ok(EP, "session.list", IDLE_LIST)
        .with_rpc_ok(EP, "daemon.handoff_stop", "{\"ok\":true,\"token\":\"11223344556677889900aabbccddeeff\"}");
    let (_, out1, _) = t1.run(&["supervision-mode", "set", "on-demand"]);

    let t2 = wintask_env()
        .with_rpc_ok(EP, "health.ping", "{\"ok\":true}")
        .with_rpc_ok(EP, "mcu.status", "{\"flashing\":false,\"ok\":true}")
        .with_rpc_ok(EP, "session.list", IDLE_LIST)
        .with_rpc_ok(EP, "daemon.handoff_stop", "{\"ok\":true,\"token\":\"11223344556677889900aabbccddeeff\"}");
    let (_, out2, _) = t2.run(&["supervision-mode", "set", "on-demand", "--force"]);

    let j1 = json(&out1);
    let j2 = json(&out2);
    assert_eq!(j1["ok"], j2["ok"]);
    assert_eq!(j2["force_ignored"], true);
}
