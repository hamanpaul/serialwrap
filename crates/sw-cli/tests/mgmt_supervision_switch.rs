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
    assert!(o.index_of("service:hold_autostart") < o.index_of("rpc:daemon.handoff_stop"));
    assert!(
        o.index_of("rpc:daemon.handoff_stop") < o.index_of("service:release_autostart:true"),
        "拒絕後撤銷 hold 並還原 enabled"
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
        let session = match key {
            "interactive_session_id" => format!(r#"{{"com":"COM0","state":"READY","foreground_busy":false,"outstanding_commands":0,"interactive_session_id":{val},"recovering":false}}"#),
            "state" => format!(r#"{{"com":"COM0","state":{val},"foreground_busy":false,"outstanding_commands":0,"interactive_session_id":null,"recovering":false}}"#),
            _ => format!(r#"{{"com":"COM0","state":"READY","foreground_busy":false,"outstanding_commands":{val},"interactive_session_id":null,"recovering":false}}"#),
        };
        let t = wintask_env()
            .with_rpc_ok(EP, "health.ping", "{\"ok\":true}")
            .with_rpc_ok(EP, "mcu.status", "{\"flashing\":false,\"ok\":true}")
            .with_rpc_ok(
                EP,
                "session.list",
                &format!("{{\"ok\":true,\"sessions\":[{session}]}}"),
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
    let base = TestEnv::posix()
        .with_systemd(true)
        .with_config("supervision_mode: on-demand\n");
    let ep = base.default_socket();
    let t = base.with_idle_daemon(&ep, 4242).with_rpc_ok(
        &ep,
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
        ("DEVICE_STILL_HELD", "unknown")
    );
    assert_eq!(j["pids"][0], 4242);
    assert!(j["hint"].as_str().unwrap().contains("serialwrap daemon status"));
    assert!(j["hint"].as_str().unwrap().contains("serialwrap daemon stop"));
    assert!(!t.calls().iter().any(|c| c.get(1).map(String::as_str) == Some("/End")));
    assert!(methods(&t).iter().all(|m| m != "daemon.stop"));
    assert_eq!(t.config_bytes(), cfg);
}

#[test]
fn supervision_set_idle_uses_handoff_not_daemon_stop() {
    let base = TestEnv::posix()
        .with_systemd(true)
        .with_var("USER", "alice")
        .with_config("supervision_mode: on-demand\n");
    let ep = base.default_socket();
    let t = base.with_idle_daemon(&ep, 4321);
    let (rc, out, _) = t.run(&["supervision-mode", "set", "systemd-user"]);
    assert_eq!(rc, 0, "{out}");
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
    assert!(methods(&t).iter().all(|method| method != "daemon.handoff_stop"));
    assert!(t.calls().iter().all(|c| !c.join(" ").contains("systemctl --user stop")));
    assert_eq!(t.service().hold_state_now(), HoldState::NotHeld);
}

#[test]
fn supervision_set_no_daemon_precheck_relaunch_flash_refused() {
    let base = TestEnv::posix()
        .with_systemd(true)
        .with_var("USER", "alice")
        .with_supervisor_state("systemd-user", true, false);
    let ep = base.default_socket();
    let t = base
        .with_rpc_refused(&ep)
        .with_instance_appears_at("supervision_set:after_safety_check", 7070, &["flash"]);
    let (rc, out, _) = t.run(&["supervision-mode", "set", "on-demand"]);
    assert_eq!(rc, 2);
    let j = json(&out);
    assert_eq!(s(&j, "error_code"), "FLASHING_BUSY");
    assert_eq!(j["relaunched"]["pid"], 7070, "{out}");
    assert_eq!(s(&j, "handoff"), "refused");
    assert!(t.calls().iter().all(|c| !c.join(" ").contains("systemctl --user stop")));
    assert_eq!(t.service().hold_state_now(), HoldState::NotHeld);
}

#[test]
fn supervision_set_crash_after_hold_recovers_on_next_setup() {
    let base = TestEnv::posix()
        .with_systemd(true)
        .with_var("USER", "alice")
        .with_supervisor_state("systemd-user", true, true);
    let ep = base.default_socket();
    let t = base
        .with_idle_daemon(&ep, 4242)
        .with_command(&["systemctl", "--user", "show", "serialwrap", "-p", "Restart", "--value"], 0, "on-failure\n", "")
        .with_fault_at("supervision_set:after_hold");
    let (crash_rc, crash_out, _) = t.run(&["supervision-mode", "set", "on-demand"]);
    assert_eq!(crash_rc, 137, "{crash_out}");
    let hold = serde_json::from_slice::<serde_json::Value>(&std::fs::read(t.state_dir().join("autostart-hold.json")).unwrap()).unwrap();
    let expected_dropin = t.home().join(".config/systemd/user/serialwrap.service.d/90-serialwrap-update-hold.conf");
    // 以路徑比較（Windows 主機模擬 POSIX 時 `/` 與 `\\` 皆為分隔符，字串形狀可能混用）；Linux 主機等同字串比較。
    let dropin_of = |value: &serde_json::Value| std::path::PathBuf::from(value.as_str().expect("dropin_path 應為字串"));
    assert_eq!(dropin_of(&hold["dropin_path"]), expected_dropin);
    assert_eq!(hold["prior_restart"], "on-failure");
    assert_eq!(dropin_of(&hold["plan"]["dropin_path"]), expected_dropin);
    assert_eq!(hold["plan"]["prior_restart"], "on-failure");

    let (rc, out, _) = t.run(&["setup"]);
    assert_eq!(rc, 0, "{out}");
    let j = json(&out);
    assert_eq!(j["recovered_autostart_hold"]["action"], "restored");
    assert_eq!(t.service().hold_state_now(), HoldState::NotHeld);
}

#[test]
fn supervision_set_recover_ambiguous_state_refuses() {
    let t = TestEnv::posix()
        .with_idle_daemon(EP, 4242);
    let hold_path = t.state_dir().join("autostart-hold.json");
    t.write_file(
        &hold_path,
        b"{\"schema_version\":1,\"op\":\"supervision_set\",\"old_mode\":\"windows-task\",\"target_mode\":\"systemd-user\",\"prior_enabled\":true,\"with_sudo\":false,\"kind\":\"task_disabled\"}\n",
    );
    let hold_before = std::fs::read(&hold_path).unwrap();
    let (rc, out, _) = t.run(&["supervision-mode", "set", "on-demand"]);
    assert_eq!(rc, 2);
    let j = json(&out);
    assert_eq!(
        (s(&j, "error_code"), s(&j, "phase")),
        ("INTERNAL_ERROR", "supervision_set_recover")
    );
    assert_eq!(std::fs::read(&hold_path).unwrap(), hold_before);
    assert!(t.service().actions().is_empty(), "模糊狀態不得釋放或變更監管");
    assert!(methods(&t).iter().all(|method| method != "daemon.handoff_stop"));
}

#[test]
fn supervision_set_relaunched_busy_releases_hold() {
    let t = wintask_env()
        .with_supervisor_ignores_hold()
        .with_idle_daemon(EP, 4242);
    let (rc, out, _) = t.run(&["supervision-mode", "set", "on-demand"]);
    assert_eq!(rc, 2);
    let j = json(&out);
    assert_eq!(s(&j, "error_code"), "SESSION_BUSY", "{out}");
    assert_eq!(j["busy"][0]["com"], "COM0", "{out}");
    assert_eq!(j["relaunched"]["pid"], 9000, "{out}");
    assert_eq!(handoffs(&t), 2);
    assert_eq!(t.service().hold_state_now(), HoldState::NotHeld);
    assert!(!t.state_dir().join("autostart-hold.json").exists());
    assert!(t.service().actions().iter().all(|(_, action, _)| action != "hard_stop"));
    assert_eq!(t.service().query_now().enabled, Some(true));
    let order = t.event_order();
    assert!(order.index_of_nth("rpc:daemon.handoff_stop", 1) < order.index_of("service:release_autostart:true"));
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
    assert!(methods(&t).iter().all(|method| method != "daemon.handoff_stop"));
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
    assert_eq!(j_win["setup"]["applied"], false);
    assert!(j_win["setup"]["pending_sudo"].as_array().is_some());
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
    assert!(j["hint"].as_str().unwrap().contains("supervision-mode set"));
    assert_eq!(t.service().hold_state_now(), HoldState::Held);
    let hold_path = t.state_dir().join("autostart-hold.json");
    assert!(hold_path.exists());
    let hold: serde_json::Value = serde_json::from_slice::<()>(&std::fs::read(hold_path).unwrap()).unwrap();
    assert_eq!(hold["old_mode"], "windows-task");
    assert!(hold["created_at"].as_i64().is_some());
    assert_eq!(hold["plan"]["kind"], "task_disabled");
    assert_eq!(hold["plan"]["enabled_before"], true);
}

#[test]
fn supervision_set_timeout_does_not_leave_orphan_hold() {
    let t = wintask_env().with_daemon_alive_forever(EP, 4242);
    let (rc, out, _) = t.run(&["supervision-mode", "set", "on-demand"]);
    assert_eq!(rc, 1, "{out}");
    assert!(t.state_dir().join("autostart-hold.json").exists());

    t.rpc().stop_endpoint(&t.isolated_endpoint(EP));
    let (rc, out, _) = t.run(&["supervision-mode", "set", "on-demand"]);
    assert_eq!(rc, 0, "{out}");
    let response = json(&out);
    assert_eq!(response["recovered_autostart_hold"]["action"], "restored", "{out}");
    assert_eq!(response["setup"]["mode"], "on-demand", "{out}");
    assert_eq!(t.service().hold_state_now(), HoldState::NotHeld);
    assert!(!t.state_dir().join("autostart-hold.json").exists());
}

#[test]
fn supervision_set_force_idle_same_result_as_without() {
    let t1 = wintask_env().with_idle_daemon(EP, 4242);
    let (rc1, out1, _) = t1.run(&["supervision-mode", "set", "on-demand"]);

    let t2 = wintask_env().with_idle_daemon(EP, 4242);
    let (rc2, out2, _) = t2.run(&["supervision-mode", "set", "on-demand", "--force"]);

    assert_eq!((rc1, rc2), (0, 0));
    let j1 = json(&out1);
    let mut j2 = json(&out2);
    assert_eq!(j2["force_ignored"], true);
    if let sw_proto::pyjson::PyValue::Dict(ref mut object) = j2 {
        object.0.shift_remove("force_ignored");
    } else {
        panic!("supervision-mode set 必須輸出 JSON 物件");
    }
    assert_eq!(j1, j2, "--force 不得改變閒置切換結果");
    assert_eq!(handoffs(&t1), 1);
    assert_eq!(handoffs(&t2), 1);
    assert_eq!(t1.event_order().count_prefix("service:hard_stop"), 0);
    assert_eq!(t2.event_order().count_prefix("service:hard_stop"), 0);
}

#[test]
fn supervision_set_systemd_exit1_after_handoff_not_relaunched() {
    let base = TestEnv::posix()
        .with_systemd(true)
        .with_var("USER", "alice")
        .with_supervisor_state("systemd-user", true, true);
    let endpoint = base.default_socket();
    let t = base
        .with_idle_daemon(&endpoint, 4242)
        .with_restart_on_failure()
        .with_daemon_exit_code_after_handoff(1);
    let (rc, out, _) = t.run(&["supervision-mode", "set", "on-demand"]);
    assert_eq!(rc, 0, "{out}");
    assert_eq!(json(&out)["setup"]["mode"], "on-demand");
    let order = t.event_order();
    assert!(order.index_of("service:hold_autostart") < order.index_of("rpc:daemon.handoff_stop"));
    assert!(order.index_of("rpc:daemon.handoff_stop") < order.index_of("service:set_enabled:false"));
    assert!(order.index_of("service:set_enabled:false") < order.index_of("service:release_autostart:keep"));
    assert_eq!(order.count_prefix("service:relaunch:"), 0);
    assert!(t.service().relaunches().is_empty());
    assert!(t.service().actions().iter().all(|(_, action, _)| action != "hard_stop"));
    assert_eq!(handoffs(&t), 1);
    assert_eq!(t.service().hold_state_now(), HoldState::NotHeld);
    assert_eq!(t.service().query_now().enabled, Some(false));
    assert!(t.rpc().is_stopped(&t.isolated_endpoint(&endpoint)));
    assert!(t.config_bytes().windows(b"supervision_mode: on-demand".len()).any(|window| window == b"supervision_mode: on-demand"));
}

#[test]
fn supervision_set_release_failure_keeps_hold_record() {
    let t = wintask_env().with_idle_daemon(EP, 4242);
    t.service().respond(sw_pal::service_ctl::ServiceResult {
        ok: true,
        mode: "windows-task".into(),
        action: "hold_autostart".into(),
        rc: Some(0),
        stdout: None,
        stderr: None,
        error_code: None,
        hint: None,
        phase: None,
    });
    t.service().respond(sw_pal::service_ctl::ServiceResult {
        ok: false,
        mode: "windows-task".into(),
        action: "release_autostart".into(),
        rc: Some(1),
        stdout: None,
        stderr: Some("release failed".into()),
        error_code: Some("INTERNAL_ERROR".into()),
        hint: Some("hold remains".into()),
        phase: Some("autostart_release".into()),
    });

    let (rc, out, _) = t.run(&["supervision-mode", "set", "on-demand"]);
    assert_eq!(rc, 2);
    let response = json(&out);
    assert_eq!(response["reason"], "autostart_release_failed");
    assert!(response.get("autostart_release_error").is_some());
    assert!(t.state_dir().join("autostart-hold.json").exists());
}
