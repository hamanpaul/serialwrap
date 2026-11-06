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

// ───────── 行程辨識（D1.3／D3.2／D5.2） ─────────

use sw_pal::proc_scan::*;

fn stat_line(pid: u32, ppid: u32, start_ticks: u64) -> String {
    // 欄位：pid (comm) state ppid … 第 22 欄 starttime（`)` 之後第 20 個欄位）。
    let mut fields = vec!["S".to_string(), ppid.to_string()];
    while fields.len() < 19 {
        fields.push("0".into());
    }
    fields.push(start_ticks.to_string());
    format!("{pid} (python3) {}\n", fields.join(" "))
}

#[cfg(unix)]
fn fake_proc(root: &std::path::Path, pid: u32, ppid: u32, uid: u32, argv: &[&str], exe: &str) {
    let dir = root.join(pid.to_string());
    std::fs::create_dir_all(dir.join("fd")).unwrap();
    std::fs::write(dir.join("stat"), stat_line(pid, ppid, 1000 + u64::from(pid))).unwrap();
    std::fs::write(dir.join("status"), format!("Name:\tpython3\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\n")).unwrap();
    let mut cmdline = Vec::new();
    for part in argv {
        cmdline.extend_from_slice(part.as_bytes());
        cmdline.push(0);
    }
    std::fs::write(dir.join("cmdline"), cmdline).unwrap();
    std::os::unix::fs::symlink(exe, dir.join("exe")).unwrap();
}

#[cfg(unix)]
#[test]
fn posix_proc_details_reads_ppid_uid_exe_argv_and_start_ticks() {
    let tmp = tempfile::tempdir().unwrap();
    fake_proc(tmp.path(), 4242, 1, 1000, &["/usr/bin/python3", "/opt/a/.local/bin/serialwrapd", "--socket", "/run/u/s.sock"], "/usr/bin/python3.12");
    let details = PosixProcScan::with_root(tmp.path()).proc_details(4242).expect("details");
    assert_eq!(details.ppid, Some(1));
    assert_eq!(details.uid, Some(1000));
    assert_eq!(details.exe.as_deref(), Some("/usr/bin/python3.12"));
    assert_eq!(details.argv[1], "/opt/a/.local/bin/serialwrapd");
    assert_eq!(details.start_ticks, Some(5242));
    assert!(PosixProcScan::with_root(tmp.path()).proc_details(9999).is_none());
}

#[cfg(unix)]
#[test]
fn posix_uart_holder_scan_covers_any_same_uid_process_and_skips_other_uids() {
    let tmp = tempfile::tempdir().unwrap();
    let me = PosixProcScan::new().current_uid().expect("unix uid");
    let tty = tmp.path().join("ttyUSB0");
    std::fs::write(&tty, b"").unwrap();
    // 同 uid 的非 serialwrapd 行程（例如 minicom）持有 tty → 必須列出。
    fake_proc(tmp.path(), 700, 1, me, &["minicom", "-D", "/dev/ttyUSB0"], "/usr/bin/minicom");
    std::os::unix::fs::symlink(&tty, tmp.path().join("700/fd/3")).unwrap();
    // 他 uid 的行程不在掃描範圍。
    fake_proc(tmp.path(), 701, 1, me.wrapping_add(1), &["minicom"], "/usr/bin/minicom");
    std::os::unix::fs::symlink(&tty, tmp.path().join("701/fd/3")).unwrap();
    let scan = PosixProcScan::with_root(tmp.path());
    let (holders, status) = scan.uart_holders_same_uid(std::slice::from_ref(&tty));
    assert_eq!(holders, vec![700]);
    assert_eq!(status, HolderScanStatus::Ok);
    // serialwrapd 專用掃描維持原語意（minicom 不是 serialwrapd）。
    assert!(scan.serialwrapd_tty_holders(std::slice::from_ref(&tty)).0.is_empty());
}

#[test]
fn python_interpreter_detection() {
    for yes in ["/usr/bin/python3", "/usr/bin/python3.12", "/opt/py/bin/python", "C:\\Python312\\python.exe", "pythonw3"] {
        assert_eq!(is_python_interpreter(yes), !yes.ends_with("pythonw3"), "{yes}");
    }
    for no in ["/usr/bin/serialwrapd", "/opt/a/.local/share/serialwrap/versions/0.4.0/bin/serialwrapd", "/usr/bin/pythonista", ""] {
        assert!(!is_python_interpreter(no), "{no}");
    }
}

#[test]
fn python_daemon_script_detection() {
    let argv = |v: &[&str]| v.iter().map(|s| s.to_string()).collect::<Vec<_>>();
    assert_eq!(
        python_daemon_script(&argv(&["/usr/bin/python3", "/opt/a/.local/bin/serialwrapd", "--socket", "x"]), ArgvFlavor::Posix),
        Some(Some("/opt/a/.local/bin/serialwrapd".into()))
    );
    assert_eq!(python_daemon_script(&argv(&["python3", "-m", "sw_core.daemon"]), ArgvFlavor::Posix), Some(None));
    assert_eq!(
        python_daemon_script(&argv(&["python.exe", "C:\\Users\\a\\pipx\\venvs\\serialwrap\\Scripts\\serialwrapd-script.py"]), ArgvFlavor::Windows),
        Some(Some("C:\\Users\\a\\pipx\\venvs\\serialwrap\\Scripts\\serialwrapd-script.py".into()))
    );
    assert_eq!(python_daemon_script(&argv(&["/opt/serialwrap/bin/serialwrapd", "--socket", "x"]), ArgvFlavor::Posix), None);
    assert_eq!(python_daemon_script(&argv(&["python3", "serialwrapd"]), ArgvFlavor::Posix), None, "無路徑的 argv 不視為 script");
}

#[test]
fn fake_proc_details_follow_liveness() {
    let rpc = FakeRpcConnector::new();
    let procs = FakeProcScan::new(vec![DaemonProc { pid: 9, socket: None, tree: vec![ProcIdentity { pid: 9, start_ticks: Some(1) }] }]);
    procs.set_details(ProcDetails { pid: 9, ppid: Some(1), uid: Some(5), exe: Some("/usr/bin/python3".into()), argv: vec![], start_ticks: Some(1) });
    procs.set_current_uid(Some(5));
    assert_eq!(procs.proc_details(9).and_then(|d| d.uid), Some(5));
    assert_eq!(procs.current_uid(), Some(5));
    procs.alive_until_stopped(9, rpc.clone(), "unix:///x");
    rpc.stop_endpoint("unix:///x");
    assert!(procs.proc_details(9).is_none(), "已結束的行程不得回傳辨識資訊");
}

/// OD-48 OWNER-BATCH #3（rulings/s1.md）：Linux 以 `/proc/<pid>/cgroup` 判定外部監管者。
#[test]
fn cgroup_classification_distinguishes_foreign_service_units() {
    use sw_pal::proc_scan::{classify_cgroup, CgroupOwner};
    // cgroup v2 unified 與 v1 name=systemd 兩種格式。
    let foreign_system = "0::/system.slice/my-wrapper.service\n";
    assert_eq!(classify_cgroup(foreign_system), CgroupOwner::ForeignUnit("my-wrapper.service".into()));
    let foreign_user = "0::/user.slice/user-1000.slice/user@1000.service/app.slice/sw-custom.service\n";
    assert_eq!(classify_cgroup(foreign_user), CgroupOwner::ForeignUnit("sw-custom.service".into()));
    let v1 = "12:cpu,cpuacct:/\n1:name=systemd:/system.slice/supervisord.service\n";
    assert_eq!(classify_cgroup(v1), CgroupOwner::ForeignUnit("supervisord.service".into()));
    // scope 結尾不論位於 system.slice 或 user.slice 都是一般 session／終端機 scope，不算外部監管者。
    assert_eq!(classify_cgroup("0::/system.slice/docker-0123abcd.scope\n"), CgroupOwner::Session);
    assert_eq!(classify_cgroup("0::/system.slice/init.scope\n"), CgroupOwner::Session);
    assert_eq!(classify_cgroup("0::/system.slice/docker-0123abcd.scope/init.scope\n"), CgroupOwner::Session);
    let v1_scope = "12:cpu,cpuacct:/\n1:name=systemd:/system.slice/docker-0123abcd.scope\n";
    assert_eq!(classify_cgroup(v1_scope), CgroupOwner::Session);
    // serialwrap 自己的 unit 與一般使用者 session／終端機 scope 不算外部監管者。
    assert_eq!(classify_cgroup("0::/system.slice/serialwrap.service\n"), CgroupOwner::SerialwrapUnit);
    assert_eq!(classify_cgroup("0::/user.slice/user-1000.slice/user@1000.service/app.slice/serialwrap.service\n"), CgroupOwner::SerialwrapUnit);
    assert_eq!(classify_cgroup("0::/user.slice/user-1000.slice/session-3.scope\n"), CgroupOwner::Session);
    assert_eq!(classify_cgroup("0::/user.slice/user-1000.slice/user@1000.service/app.slice/app-org.gnome.Terminal.slice/vte-spawn-1.scope\n"), CgroupOwner::Session);
    assert_eq!(classify_cgroup("0::/user.slice/user-1000.slice/user@1000.service/app.slice/app-tmux.slice/tmux-spawn-9.scope\n"), CgroupOwner::Session);
    assert_eq!(classify_cgroup("0::/user.slice/user-1000.slice/user@1000.service/tmux-spawn-9.scope\n"), CgroupOwner::Session);
    // 容器內的根 cgroup 或讀不到 → 無法判定（呼叫端退回父行程判定）。
    assert_eq!(classify_cgroup("0::/\n"), CgroupOwner::Unknown);
    assert_eq!(classify_cgroup(""), CgroupOwner::Unknown);
}

#[cfg(unix)]
#[test]
fn posix_proc_cgroup_reads_proc_pid_cgroup() {
    let tmp = tempfile::tempdir().unwrap();
    fake_proc(tmp.path(), 4242, 1, 1000, &["/usr/bin/python3", "/opt/a/.local/bin/serialwrapd"], "/usr/bin/python3.12");
    std::fs::write(tmp.path().join("4242/cgroup"), "0::/system.slice/my-wrapper.service\n").unwrap();
    let scan = PosixProcScan::with_root(tmp.path());
    assert_eq!(scan.proc_cgroup(4242).as_deref(), Some("0::/system.slice/my-wrapper.service\n"));
    assert!(scan.proc_cgroup(9999).is_none());
}

#[test]
fn fake_proc_cgroup_is_scriptable() {
    let procs = FakeProcScan::new(Vec::new());
    assert!(procs.proc_cgroup(9).is_none());
    procs.set_cgroup(9, "0::/system.slice/x.service\n");
    assert_eq!(procs.proc_cgroup(9).as_deref(), Some("0::/system.slice/x.service\n"));
}
