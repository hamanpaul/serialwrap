//! OD-48 D4／D8：systemd enabled 狀態與 MainPID 查詢，以及 legacy daemon 辨識所需的行程資訊。
#![allow(dead_code)]

use sw_pal::fakes::*;
use sw_pal::service_ctl::*;

fn sm(s: &str) -> SupervisionMode {
    SupervisionMode::parse(s)
}

#[test]
fn is_enabled_mapping_distinguishes_disabled_masked_and_static() {
    assert_eq!(parse_is_enabled("enabled\n"), Some(true));
    assert_eq!(parse_is_enabled("disabled\n"), Some(false));
    assert_eq!(parse_is_enabled("masked\n"), Some(false));
    assert_eq!(parse_is_enabled("masked-runtime\n"), Some(false));
    for unknown in ["static", "indirect", "enabled-runtime", "linked", "generated", "transient", "alias", "not-found", ""] {
        assert_eq!(parse_is_enabled(unknown), None, "{unknown} 不得映射成布林");
    }
}

#[test]
fn systemd_query_enabled_uses_is_enabled_not_is_active() {
    let env = FakeSysEnv::builder()
        .command(&["systemctl", "--user", "is-active", "serialwrap"], 0, "active\n", "")
        .command(&["systemctl", "--user", "is-enabled", "serialwrap"], 1, "disabled\n", "")
        .build();
    let ctl = SystemdCtl::new(env.clone(), real_fs(), FakeClock::new());
    assert_eq!(ctl.query(&sm("systemd-user")), TaskQuery::Running);
    assert_eq!(ctl.query_enabled(&sm("systemd-user")), EnabledQuery::Known(Some(false)));
    assert!(env.calls().iter().any(|argv| argv == &["systemctl", "--user", "is-enabled", "serialwrap"]));
}

#[test]
fn systemd_system_query_enabled_and_main_pid_never_use_sudo() {
    let env = FakeSysEnv::builder()
        .command(&["systemctl", "is-enabled", "serialwrap"], 0, "static\n", "")
        .command(&["systemctl", "show", "serialwrap", "-p", "MainPID", "--value"], 0, "4242\n", "")
        .build();
    let ctl = SystemdCtl::new(env.clone(), real_fs(), FakeClock::new());
    assert_eq!(ctl.query_enabled(&sm("systemd-system")), EnabledQuery::Known(None));
    assert_eq!(ctl.main_pid(&sm("systemd-system")), Ok(Some(4242)));
    assert!(env.calls().iter().all(|argv| argv[0] != "sudo"));
}

#[test]
fn systemd_main_pid_zero_means_no_main_process() {
    let env = FakeSysEnv::builder()
        .command(&["systemctl", "--user", "show", "serialwrap", "-p", "MainPID", "--value"], 0, "0\n", "")
        .build();
    let ctl = SystemdCtl::new(env, real_fs(), FakeClock::new());
    assert_eq!(ctl.main_pid(&sm("systemd-user")), Ok(None));
    assert_eq!(parse_main_pid("garbage"), None);
}

#[test]
fn non_systemd_backends_do_not_claim_enabled_or_main_pid() {
    let env = FakeSysEnv::builder().build();
    let ctl = WindowsTaskCtl::new(env.clone(), real_fs(), FakeClock::new());
    assert_eq!(ctl.query_enabled(&sm("windows-task")), EnabledQuery::NotSupported);
    assert_eq!(ctl.main_pid(&sm("windows-task")), Ok(None));
    assert!(env.calls().is_empty());
}

#[test]
fn fake_service_main_pid_and_enabled_are_scriptable() {
    let fake = FakeServiceCtl::new(vec![sm("systemd-user")]);
    assert_eq!(fake.query_enabled(&sm("systemd-user")), EnabledQuery::NotSupported);
    fake.set_enabled_query(Some(EnabledQuery::Known(Some(false))));
    fake.set_main_pid(&sm("systemd-user"), Ok(Some(77)));
    assert_eq!(fake.query_enabled(&sm("systemd-user")), EnabledQuery::Known(Some(false)));
    assert_eq!(fake.main_pid(&sm("systemd-user")), Ok(Some(77)));
    assert_eq!(fake.main_pid(&sm("systemd-system")), Ok(None));
}
