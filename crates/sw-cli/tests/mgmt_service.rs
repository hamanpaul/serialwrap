//! P4-T6 `config.yaml` 寫入、`service`、`supervision-mode` 查詢（DF-07）測試。
//!
//! 依據 `docs/rust-refactor/plans/05-stage1b-rust-cli-mgmt.md` Task 6 規範撰寫。

mod support;

use support::*;

#[test]
fn service_on_demand_prints_no_systemd_and_mirrors_stderr() {
    let t = TestEnv::posix(); // tmp HOME + 隔離 RuntimePaths + FakeSysEnv
    let (rc, out, err) = t.run(&["service", "start"]);
    assert_eq!(rc, 2);
    assert_eq!(
        out,
        "{\"action\":\"start\",\"error_code\":\"NO_SYSTEMD\",\"hint\":\"目前為 on-demand 監管模式（無 systemd 服務）。使用 `serialwrap daemon start/stop`。\",\"mode\":\"on-demand\",\"ok\":false}\n"
    );
    assert_eq!(
        err,
        "serialwrap: service start failed: NO_SYSTEMD（hint: 目前為 on-demand 監管模式（無 systemd 服務）。使用 `serialwrap daemon start/stop`。）\n"
    );
}

#[test]
fn supervision_mode_prints_plain_text_unvalidated() {
    let t = TestEnv::posix().with_config("supervision_mode: foo\n");
    assert_eq!(
        t.run(&["supervision-mode"]),
        (0, "foo\n".to_string(), String::new())
    );
}

#[test]
fn supervision_mode_bad_yaml_is_one_stderr_line_exit1() {
    let t = TestEnv::posix().with_config("supervision_mode: [unterminated\n");
    let (rc, out, err) = t.run(&["supervision-mode"]);
    assert_eq!((rc, out.as_str()), (1, ""));
    assert!(err.starts_with(&format!(
        "serialwrap: config.yaml 解析失敗: {}",
        t.config_path().display()
    )));
    assert_eq!(err.matches('\n').count(), 1);
}

#[test]
fn service_systemd_user_status_passthrough() {
    let t = TestEnv::posix()
        .with_config("supervision_mode: systemd-user\n")
        .with_real_service()
        .with_command(
            &["systemctl", "--user", "status", "serialwrap"],
            3,
            "● serialwrap.service - active (running)\n",
            "",
        );
    let (rc, out, err) = t.run(&["service", "status"]);
    assert_eq!(rc, 2);
    assert!(out.contains("\"rc\":3"));
    assert!(out.contains("\"mode\":\"systemd-user\""));
    assert!(out.contains("\"action\":\"status\""));
    assert!(err.starts_with("serialwrap: service status failed: "));
}

#[test]
fn service_systemd_system_start_needs_sudo() {
    let t = TestEnv::posix()
        .with_config("supervision_mode: systemd-system\n")
        .with_real_service();
    let (rc, out, err) = t.run(&["service", "start"]);
    assert_eq!(rc, 2);
    assert!(out.contains("\"error_code\":\"NEEDS_SUDO\""));
    assert!(err.contains("NEEDS_SUDO"));
    assert!(t.calls().is_empty(), "needs_sudo 時不得呼叫系統指令");
}

#[test]
fn service_windows_task_start_runs_schtasks() {
    let t = TestEnv::windows()
        .with_config("supervision_mode: windows-task\n")
        .with_real_service()
        .with_command(
            &["schtasks", "/Run", "/TN", "serialwrap"],
            0,
            "SUCCESS: Attempted to run the scheduled task \"serialwrap\".\n",
            "",
        );
    let (rc, out, _err) = t.run(&["service", "start"]);
    assert_eq!(rc, 0);
    assert!(out.contains("\"ok\":true"));
    assert!(out.contains("\"action\":\"start\""));
    assert!(out.contains("\"mode\":\"windows-task\""));
    assert_eq!(
        t.calls(),
        vec![vec!["schtasks", "/Run", "/TN", "serialwrap"]]
    );
}

#[test]
fn service_bad_yaml_json_internal_error() {
    let t = TestEnv::posix().with_config("supervision_mode: [unterminated\n");
    let (rc, out, err) = t.run(&["service", "start"]);
    assert_eq!(rc, 1);
    assert!(out.contains("\"error_code\":\"INTERNAL_ERROR\""));
    assert!(out.contains("\"ok\":false"));
    assert!(out.contains("config.yaml 解析失敗:"));
    assert!(err.starts_with("serialwrap: service start failed: INTERNAL_ERROR: config.yaml 解析失敗:"));
}

#[test]
fn service_invalid_action_argparse_exit2() {
    let t = TestEnv::posix();
    let (rc, _out, err) = t.run(&["service", "reload"]);
    assert_eq!(rc, 2);
    assert!(err.contains("argument action: invalid choice: 'reload' (choose from 'start', 'stop', 'restart', 'status')"));
}

#[test]
fn config_save_never_writes_system_fallback() {
    let t = TestEnv::posix();
    let sys_cfg = t.system_config_dir().join("config.yaml");
    let sys_content = "supervision_mode: systemd-system\n";
    t.write_file(&sys_cfg, sys_content.as_bytes());

    t.with_cli_ctx(|ctx| {
        let write_p = sw_cli::endpoint_resolve::config_write_path(ctx);
        assert_eq!(write_p, t.config_path());
        assert_ne!(write_p, sys_cfg);

        let cfg = sw_proto::config_types::RuntimeConfig::from_text(Some("supervision_mode: on-demand\n")).unwrap();
        sw_cli::endpoint_resolve::save_runtime_config(ctx, &cfg).expect("save_runtime_config 失敗");
    });

    let sys_after = std::fs::read_to_string(&sys_cfg).unwrap();
    assert_eq!(sys_after, sys_content, "系統 fallback 檔位元組不變");

    let user_cfg = std::fs::read_to_string(t.config_path()).unwrap();
    assert!(user_cfg.contains("supervision_mode: on-demand"));

    let custom_p = t.config_dir().join("custom.yaml");
    let t_custom = t.with_var("SERIALWRAP_CONFIG", custom_p.to_str().unwrap());
    t_custom.with_cli_ctx(|ctx| {
        let write_p = sw_cli::endpoint_resolve::config_write_path(ctx);
        assert_eq!(write_p, custom_p);

        let cfg = sw_proto::config_types::RuntimeConfig::from_text(Some("supervision_mode: windows-task\n")).unwrap();
        sw_cli::endpoint_resolve::save_runtime_config(ctx, &cfg).expect("save_runtime_config 失敗");
    });
    let custom_after = std::fs::read_to_string(&custom_p).unwrap();
    assert!(custom_after.contains("supervision_mode: windows-task"));
}
