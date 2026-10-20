//! P4-T10 `setup`（資產物化、WSL systemd、reconcile）與 `supervision-mode set` 參數驗證、reconcile OldStop 的回歸測試。
//!
//! 本檔驗證 P4-T10 的資產物化、WSL systemd、reconcile 與監管模式參數驗證。
//! 替身與 `TestEnv` 由 P4-T1（`tests/support/mod.rs`）提供。
//! 規格來源：`docs/rust-refactor/plans/05-stage1b-rust-cli-mgmt.md` Task 10；盤點 `inventory/07-platform-mgmt.md` §5、§6。

mod common;
mod support;

use std::collections::BTreeSet;
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
