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
    // #276：先恢復啟用（中斷的 stop 可能留下停用狀態），再 /Run。
    assert_eq!(
        t.calls(),
        vec![
            vec!["schtasks", "/Change", "/TN", "serialwrap", "/ENABLE"],
            vec!["schtasks", "/Run", "/TN", "serialwrap"],
        ]
    );
}

#[test]
fn service_windows_task_start_keeps_disabled_under_autostart_hold() {
    // #276：更新／監管切換的 hold 存在時，停用狀態屬於 hold，start 不得自行恢復啟用。
    let t = TestEnv::windows()
        .with_config("supervision_mode: windows-task\n")
        .with_real_service();
    t.write_file(
        &t.state_dir().join("autostart-hold.json"),
        b"{\"schema_version\":1,\"op\":\"supervision_set\"}",
    );
    let (_rc, _out, _err) = t.run(&["service", "start"]);
    assert_eq!(
        t.calls(),
        vec![vec!["schtasks", "/Run", "/TN", "serialwrap"]]
    );
}

#[test]
fn p277_service_start_preserves_interrupted_update_hold() {
    let mut t = TestEnv::windows()
        .with_install_root()
        .with_installed("0.4.0")
        .with_current("0.4.0", None)
        .with_bundle(CURRENT_VERSION)
        .with_supervisor_state("windows-task", true, true)
        .with_idle_daemon(TestEnv::win_endpoint(), 4242)
        .with_new_daemon_reporting_version(CURRENT_VERSION)
        .with_crash_at("stop_supervisor:after_hold")
        .with_command(
            &["schtasks", "/Run", "/TN", "serialwrap"],
            1,
            "",
            "ERROR: The task is disabled.\n",
        );

    let (setup_rc, _, setup_err) = t.run_from_bundle(&["setup"]);
    assert_eq!(
        setup_rc, 137,
        "setup 未在 hold 後的 crash point 中斷：{setup_err}"
    );

    let journal_before = t.journal_bytes().expect("setup 中斷後必須留下真實 journal");
    assert_eq!(t.journal_json().unwrap()["step"], "stop_supervisor");
    let hold_path = t.state_dir().join("autostart-hold.json");
    assert!(
        !hold_path.exists(),
        "windows-task update hold 不使用 switch 檔"
    );
    let supervisor = std::sync::Arc::clone(t.service());
    assert_eq!(
        supervisor.hold_state_now(),
        sw_pal::service_ctl::HoldState::Held
    );
    let calls_before_start = t.calls().len();

    t.clear_crash();
    t.use_real_service();
    let (rc, out, err) = t.run(&["service", "start"]);

    assert_eq!(rc, 2, "停用的工作 /Run 失敗仍須沿用 exit 2：{out}");
    assert!(out.contains("\"ok\":false"));
    assert!(out.contains("\"action\":\"start\""));
    assert!(out.contains("\"rc\":1"));
    assert!(!out.contains("\"phase\""), "不得新增 phase：{out}");
    assert!(
        !out.contains("install-journal.json"),
        "提示只寫入 stderr：{out}"
    );
    assert_eq!(
        &t.calls()[calls_before_start..],
        vec![vec!["schtasks", "/Run", "/TN", "serialwrap"]],
        "update hold 下只可 /Run，不得 /ENABLE"
    );
    assert!(err.contains("UNKNOWN_ERROR"), "沿用既有錯誤輸出：{err}");
    assert!(
        err.contains("更新／回滾進行中"),
        "stderr 缺少更新 hold 說明：{err}"
    );
    assert!(
        err.contains("install-journal.json"),
        "stderr 缺少 journal 名稱：{err}"
    );
    assert!(
        err.contains("請待 serialwrap setup 完成"),
        "stderr 缺少等待提示：{err}"
    );
    assert_eq!(
        err.matches("install-journal.json").count(),
        1,
        "journal hold 提示應只輸出一次：{err}"
    );
    assert_eq!(
        t.journal_bytes().as_deref(),
        Some(journal_before.as_slice()),
        "start 不得改寫 journal"
    );
    assert_eq!(
        supervisor.hold_state_now(),
        sw_pal::service_ctl::HoldState::Held,
        "start 不得釋放停用 hold"
    );
}

#[test]
fn p277_service_start_reports_switch_hold() {
    let t = TestEnv::windows()
        .with_config("supervision_mode: windows-task\n")
        .with_real_service()
        .with_command(
            &["schtasks", "/Run", "/TN", "serialwrap"],
            0,
            "SUCCESS: Attempted to run the scheduled task \"serialwrap\".\n",
            "",
        );
    t.write_file(
        &t.state_dir().join("autostart-hold.json"),
        b"{\"schema_version\":1,\"op\":\"supervision_set\"}",
    );

    let (rc, out, err) = t.run(&["service", "start"]);

    assert_eq!(rc, 0, "switch hold 下沿用原 /Run 結果：{out}");
    assert!(
        !out.contains("\"hint\""),
        "hold 說明不得增加 JSON 欄位：{out}"
    );
    assert!(
        !out.contains("監管模式切換進行中"),
        "hold 說明只寫入 stderr：{out}"
    );
    assert!(
        err.contains("監管模式切換進行中"),
        "stderr 缺少 switch hold 說明：{err}"
    );
    assert!(
        err.contains("autostart-hold.json"),
        "stderr 缺少 switch 檔名：{err}"
    );
    assert!(
        err.contains("請待 serialwrap setup 完成"),
        "stderr 缺少等待提示：{err}"
    );
    assert_eq!(
        err.matches("autostart-hold.json").count(),
        1,
        "switch hold 提示應只輸出一次：{err}"
    );
    assert_eq!(
        t.calls(),
        vec![vec!["schtasks", "/Run", "/TN", "serialwrap"]]
    );
}

#[test]
fn service_windows_task_start_enable_failure_skips_run() {
    let t = TestEnv::windows()
        .with_config("supervision_mode: windows-task\n")
        .with_real_service()
        .with_command(
            &["schtasks", "/Change", "/TN", "serialwrap", "/ENABLE"],
            1,
            "",
            "ERROR: Access is denied.\n",
        );
    let (rc, out, err) = t.run(&["service", "start"]);
    assert_eq!(rc, 2);
    assert!(out.contains("\"ok\":false"));
    assert!(out.contains("\"error_code\":\"INTERNAL_ERROR\""));
    assert!(out.contains("\"phase\":\"service_enable\""));
    assert!(out.contains("\"action\":\"start\""));
    assert!(err.starts_with("serialwrap: service start failed: "));
    assert_eq!(
        t.calls(),
        vec![vec!["schtasks", "/Change", "/TN", "serialwrap", "/ENABLE"]],
        "恢復啟用失敗時不得再 /Run"
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

/// Python 基線 `hint`（`service_action` 的 on-demand 分支）。
const ON_DEMAND_HINT: &str =
    "目前為 on-demand 監管模式（無 systemd 服務）。使用 `serialwrap daemon start/stop`。";

#[test]
fn service_non_string_mode_bool_keys_match_python_json() {
    // Python：json.dumps 把 bool key 轉為 "true"／"false"；sort_keys 依原始 key 排序（False < True）。
    let t = TestEnv::posix().with_config("supervision_mode: {true: 1, false: 2}\n");
    let (rc, out, err) = t.run(&["service", "start"]);
    assert_eq!(rc, 2);
    assert_eq!(
        out,
        format!(
            "{{\"action\":\"start\",\"error_code\":\"NO_SYSTEMD\",\"hint\":\"{ON_DEMAND_HINT}\",\"mode\":{{\"false\":2,\"true\":1}},\"ok\":false}}\n"
        )
    );
    assert!(err.starts_with("serialwrap: service start failed: NO_SYSTEMD"));
}

#[test]
fn service_non_string_mode_null_key_matches_python_json() {
    let t = TestEnv::posix().with_config("supervision_mode: {null: 1}\n");
    let (rc, out, _err) = t.run(&["service", "start"]);
    assert_eq!(rc, 2);
    assert!(out.contains("\"mode\":{\"null\":1}"), "{out}");
}

#[test]
fn service_non_string_mode_date_reports_internal_error_json() {
    // Python CLI 邊界會把 json.dumps 的 TypeError 轉為 INTERNAL_ERROR JSON；未執行任何監管命令。
    let t = TestEnv::posix().with_config("supervision_mode: 2020-01-01\n");
    let (rc, out, err) = t.run(&["service", "start"]);
    assert_eq!(rc, 1);
    assert_eq!(
        out,
        "{\"error_code\":\"INTERNAL_ERROR\",\"message\":\"Object of type date is not JSON serializable\",\"ok\":false}\n"
    );
    assert_eq!(
        err,
        "serialwrap: service start failed: INTERNAL_ERROR: Object of type date is not JSON serializable\n"
    );
    assert!(t.calls().is_empty(), "不可序列化時不得呼叫系統指令");
}

#[test]
fn service_non_string_mode_mixed_keys_reports_internal_error_json() {
    // Python CLI 邊界會把 sort_keys 的 TypeError 轉為 INTERNAL_ERROR JSON。
    let t = TestEnv::posix().with_config("supervision_mode: {1: a, b: c}\n");
    let (rc, out, err) = t.run(&["service", "start"]);
    assert_eq!(rc, 1);
    assert_eq!(
        out,
        "{\"error_code\":\"INTERNAL_ERROR\",\"message\":\"'<' not supported between instances of 'str' and 'int'\",\"ok\":false}\n"
    );
    assert_eq!(
        err,
        "serialwrap: service start failed: INTERNAL_ERROR: '<' not supported between instances of 'str' and 'int'\n"
    );
}

#[test]
fn service_bad_constructor_tag_reports_line_and_column() {
    // DF-07：建構器錯誤（未知 tag）同樣帶 `(line l, column c)`；service 走 INTERNAL_ERROR 形狀。
    let t = TestEnv::posix().with_config("supervision_mode: !foo bar\n");
    let (rc, out, err) = t.run(&["service", "start"]);
    assert_eq!(rc, 1);
    assert!(
        out.contains("could not determine a constructor for the tag '!foo' (line 1, column "),
        "{out}"
    );
    assert!(err.starts_with("serialwrap: service start failed: INTERNAL_ERROR: config.yaml 解析失敗:"));
}

#[test]
fn supervision_mode_bad_yaml_summary_has_problem_line_and_column() {
    // DF-07 摘要：`problem (line l, column c)`，單行。
    let t = TestEnv::posix().with_config("supervision_mode: [unterminated\n");
    let (rc, out, err) = t.run(&["supervision-mode"]);
    assert_eq!((rc, out.as_str()), (1, ""));
    assert!(err.ends_with("(line 2, column 1)\n"), "{err}");
    assert_eq!(err.matches('\n').count(), 1);
}
