//! OD-48（含修訂）、S1X-c-1 與 D8 的行為測試：0.4.0 `setup` 遇到 0.3.x 舊 daemon 時的停止與還原。
//!
//! 規格：`docs/rust-refactor/reviews/od48-design-v2.md`（docs/parity-amendments 分支）D1～D10；
//! 裁決：`reviews/rulings/s1.md` 的 OD-48、OD-48 修訂、S1X-c-1。legacy 專用的中斷注入點另立清單
//! （`LEGACY_CRASH_POINTS`），不加進 `support::CRASH_POINTS` 的凍結迴圈。

mod support;

use support::*;
use sw_pal::service_ctl::{EnabledQuery, TaskQuery};

fn acts(t: &TestEnv) -> Vec<String> {
    t.service().actions().into_iter().map(|a| a.1).collect()
}

fn methods(t: &TestEnv) -> Vec<String> {
    t.rpc().requests().iter().map(|r| r.1.clone()).collect()
}

/// 已在跑、unit 卻是 disabled 的 systemd-user 監管（真實 systemd：`is-active` 為 active、`is-enabled` 為 disabled）。
fn disabled_but_running_user_unit(t: &TestEnv) {
    t.service().query_script(vec![TaskQuery::Running]);
    t.service().set_enabled_query(Some(EnabledQuery::Known(Some(false))));
}

#[test]
fn d8_disabled_running_unit_stays_disabled_after_update() {
    let t = TestEnv::posix()
        .with_install_root()
        .with_installed("0.4.0")
        .with_current("0.4.0", None)
        .with_bundle("0.4.1")
        .with_systemd(true)
        .with_var("USER", "alice")
        .with_supervisor_state("systemd-user", false, true)
        .with_idle_daemon_default(4242)
        .with_new_daemon_reporting_version("0.4.1");
    disabled_but_running_user_unit(&t);
    let (rc, out, _) = t.run_from_bundle(&["setup", "--user"]);
    assert_eq!(rc, 0, "{out}");
    let actions = acts(&t);
    assert!(
        !actions.iter().any(|a| a == "release_autostart:true" || a == "set_enabled:true"),
        "原本 disabled 的 unit 升級後不得被改成 enabled：{actions:?}"
    );
    assert!(actions.iter().any(|a| a == "release_autostart:false"), "{actions:?}");
}

#[test]
fn d8_disabled_running_unit_stays_disabled_after_refusal() {
    let t = TestEnv::posix()
        .with_install_root()
        .with_installed("0.4.0")
        .with_current("0.4.0", None)
        .with_bundle("0.4.1")
        .with_systemd(true)
        .with_var("USER", "alice")
        .with_supervisor_state("systemd-user", false, true)
        .with_idle_daemon_default(4242)
        .with_rpc_ok_default(
            "daemon.handoff_stop",
            "{\"ok\":false,\"error_code\":\"SESSION_BUSY\",\"busy\":[{\"com\":\"COM0\",\"state\":\"READY\",\"foreground_busy\":true,\"outstanding_commands\":0,\"interactive_session_id\":null,\"recovering\":false,\"ops\":[\"file.push\"]}]}",
        );
    disabled_but_running_user_unit(&t);
    let (rc, out, _) = t.run_from_bundle(&["setup", "--user"]);
    assert_eq!(rc, 2, "{out}");
    assert_eq!(s(&json(&out), "error_code"), "SESSION_BUSY");
    let actions = acts(&t);
    assert!(
        !actions.iter().any(|a| a == "release_autostart:true" || a == "set_enabled:true"),
        "拒絕後撤銷 hold 不得把 disabled unit 改成 enabled：{actions:?}"
    );
    assert!(!methods(&t).iter().any(|m| m == "daemon.stop"));
}

const SYS_SOCKET: &str = "/run/serialwrap/serialwrapd.sock";
const FLASHING_LIST: &str = "{\"ok\":true,\"sessions\":[{\"com\":\"COM0\",\"state\":\"FLASHING\",\"foreground_busy\":false,\"outstanding_commands\":0,\"interactive_session_id\":null,\"recovering\":false}]}";

fn system_daemon_flashing(t: TestEnv) -> TestEnv {
    t.with_supervisor_state("systemd-system", true, true)
        .with_rpc_ok(SYS_SOCKET, "health.ping", "{\"ok\":true,\"pid\":4242}")
        .with_rpc_ok(SYS_SOCKET, "health.status", "{\"ok\":true,\"pid\":4242,\"version\":\"0.4.0\"}")
        .with_rpc_ok(SYS_SOCKET, "mcu.status", "{\"flashing\":true,\"ok\":true}")
        .with_rpc_ok(SYS_SOCKET, "session.list", FLASHING_LIST)
        .with_procs(vec![DaemonProc {
            pid: 4242,
            socket: Some(SYS_SOCKET.into()),
            tree: vec![ProcIdentity { pid: 4242, start_ticks: Some(9) }],
        }])
}

/// S1X-c-1／D9(i)：systemd-system 的 daemon 在固定 socket 上 flash 中，update 必須偵測到並拒絕。
#[test]
fn s1x_c1_update_detects_systemd_system_daemon_on_fixed_socket() {
    let t = system_daemon_flashing(
        TestEnv::posix().with_install_root().with_installed("0.4.0").with_current("0.4.0", None).with_bundle("0.4.1"),
    );
    let (rc, out, _) = t.run_from_bundle(&["setup", "--system", "--with-sudo"]);
    assert_eq!(rc, 2, "{out}");
    let j = json(&out);
    assert_eq!((s(&j, "error_code"), s(&j, "phase")), ("FLASHING_BUSY", "update_safety_check"), "{out}");
    let current = sw_cli::install_layout::read_current(&t.root()).unwrap().unwrap();
    assert_eq!(current.bundle_version, "0.4.0");
    assert!(methods(&t).iter().all(|m| m != "daemon.handoff_stop" && m != "daemon.stop"));
    assert!(t.journal_json().is_none());
}

/// S1X-c-1／D9(i)：首次安裝（無 current-release.json）同樣以 systemd-system 固定 socket 偵測舊 daemon。
#[test]
fn s1x_c1_first_install_detects_systemd_system_daemon_on_fixed_socket() {
    let t = system_daemon_flashing(TestEnv::posix().with_install_root().with_bundle("0.4.1"));
    let (rc, out, _) = t.run_from_bundle(&["setup", "--system", "--with-sudo"]);
    assert_eq!(rc, 2, "{out}");
    let j = json(&out);
    assert_eq!((s(&j, "error_code"), s(&j, "phase")), ("FLASHING_BUSY", "update_safety_check"), "{out}");
    assert!(sw_cli::install_layout::read_current(&t.root()).unwrap().is_none());
    assert!(methods(&t).iter().all(|m| m != "daemon.handoff_stop" && m != "daemon.stop"));
}

/// D3.2：ping 不通改掃 /proc 時，找到 socket 不符的同 uid serialwrapd 必須 fail-closed（DEVICE_STILL_HELD），
/// 不得當成 daemon 不存在繼續。
#[test]
fn d3_2_foreign_socket_daemon_without_rpc_fails_closed() {
    let other = "/tmp/elsewhere/serialwrapd.sock";
    let t = TestEnv::posix()
        .with_install_root()
        .with_installed("0.4.0")
        .with_current("0.4.0", None)
        .with_bundle("0.4.1")
        .with_supervisor_state("on-demand", false, false)
        .with_procs(vec![DaemonProc {
            pid: 5151,
            socket: Some(other.into()),
            tree: vec![ProcIdentity { pid: 5151, start_ticks: Some(3) }],
        }]);
    let (rc, out, _) = t.run_from_bundle(&["setup", "--on-demand"]);
    assert_eq!(rc, 2, "{out}");
    let j = json(&out);
    assert_eq!((s(&j, "error_code"), s(&j, "phase")), ("DEVICE_STILL_HELD", "update_safety_check"), "{out}");
    assert!(out.contains("5151") && out.contains(other), "需附 pids 與 socket：{out}");
    assert_eq!(sw_cli::install_layout::read_current(&t.root()).unwrap().unwrap().bundle_version, "0.4.0");
}

/// D3.2：監管查詢顯示 running，卻沒有可 RPC 的 daemon → fail-closed。
#[test]
fn d3_2_supervisor_running_without_rpc_daemon_fails_closed() {
    let t = TestEnv::posix()
        .with_install_root()
        .with_installed("0.4.0")
        .with_current("0.4.0", None)
        .with_bundle("0.4.1")
        .with_systemd(true)
        .with_var("USER", "alice")
        .with_supervisor_state("systemd-user", true, true);
    let (rc, out, _) = t.run_from_bundle(&["setup", "--user"]);
    assert_eq!(rc, 2, "{out}");
    let j = json(&out);
    assert_eq!((s(&j, "error_code"), s(&j, "phase")), ("DEVICE_STILL_HELD", "update_safety_check"), "{out}");
    assert_eq!(sw_cli::install_layout::read_current(&t.root()).unwrap().unwrap().bundle_version, "0.4.0");
}

/// S1X-c-1：supervision-mode set 也以舊模式的 instance endpoint 做安全檢查（systemd-system 固定 socket）。
#[test]
fn s1x_c1_supervision_switch_detects_systemd_system_daemon_on_fixed_socket() {
    let t = system_daemon_flashing(TestEnv::posix().with_systemd(true));
    let cfg = t.config_bytes();
    let (rc, out, _) = t.run(&["supervision-mode", "set", "on-demand", "--with-sudo"]);
    assert_eq!(rc, 2, "{out}");
    let j = json(&out);
    assert_eq!((s(&j, "error_code"), s(&j, "phase")), ("FLASHING_BUSY", "supervision_set_safety_check"), "{out}");
    assert_eq!(t.config_bytes(), cfg);
    assert!(methods(&t).iter().all(|m| m != "daemon.handoff_stop" && m != "daemon.stop"));
}
