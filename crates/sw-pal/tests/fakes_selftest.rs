//! sw-pal 記憶體替身自測（P4-T1）。
use sw_pal::fakes::*;
use sw_pal::SysEnv;

#[test]
fn fake_sysenv_records_calls_and_scripted_rc() {
    let env = FakeSysEnv::builder()
        .command(&["systemctl", "--user", "is-active", "serialwrap"], 3, "inactive\n", "")
        .build();
    let out = env
        .run(
            &[
                "systemctl".into(),
                "--user".into(),
                "is-active".into(),
                "serialwrap".into(),
            ],
            None,
        )
        .unwrap();
    assert_eq!((out.rc, out.stdout.as_str()), (3, "inactive\n"));
    let out2 = env
        .run(&["loginctl".into(), "enable-linger".into(), "alice".into()], None)
        .unwrap();
    assert_eq!(out2.rc, 0);
    assert_eq!(env.calls().len(), 2);
}

#[test]
fn fake_platform_unconfigured_component_panics_not_real_os() {
    let pf = platform().build();
    let r = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        pf.tunnel.supports_control_master()
    }));
    assert!(r.is_err());
}

#[test]
fn p3_alias_still_compiles() {
    let _: fn() -> sw_pal::ClientPlatform = sw_pal::native_client;
}
