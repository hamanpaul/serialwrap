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
    let result = scan.uart_holders_same_uid(std::slice::from_ref(&tty));
    assert_eq!(result.holders, vec![700]);
    assert_eq!(result.status, HolderScanStatus::Ok);
    assert!(result.uninspectable.is_empty());
    // serialwrapd 專用掃描維持原語意（minicom 不是 serialwrapd）。
    assert!(scan.serialwrapd_tty_holders(std::slice::from_ref(&tty)).0.is_empty());
}

#[cfg(unix)]
fn chmod(path: &std::path::Path, mode: u32) {
    use std::os::unix::fs::PermissionsExt;
    std::fs::set_permissions(path, std::fs::Permissions::from_mode(mode)).unwrap();
}

/// root 可繞過 mode 000，測不到 PermissionDenied。
#[cfg(unix)]
fn running_as_root() -> bool {
    // SAFETY: geteuid 無參數、不會失敗。
    unsafe { libc::geteuid() == 0 }
}

/// 同 uid、non-dumpable 的行程（systemd user session 的 `(sd-pam)`）：`/proc/<pid>/fd` 屬 root，read_dir 回 PermissionDenied。
#[cfg(unix)]
fn fake_uninspectable_proc(root: &std::path::Path, pid: u32, uid: u32, comm: Option<&str>) {
    fake_proc(root, pid, 1, uid, &["(sd-pam)"], "/usr/lib/systemd/systemd");
    if let Some(comm) = comm {
        std::fs::write(root.join(pid.to_string()).join("comm"), format!("{comm}\n")).unwrap();
    }
    chmod(&root.join(pid.to_string()).join("fd"), 0o000);
}

/// 同 uid、fd 目錄列得出但條目全部 PermissionDenied 的行程（`systemd --user` 持有 CAP_WAKE_ALARM，
/// 核心的 fd 連結存取檢查以整個行程為單位失敗）。替身以指向 `locked`（呼叫端設 mode 000）底下的 symlink 模擬。
#[cfg(unix)]
fn fake_entries_denied_proc(root: &std::path::Path, pid: u32, uid: u32, locked: &std::path::Path, entries: u32) {
    fake_proc(root, pid, 1, uid, &["/usr/lib/systemd/systemd", "--user"], "/usr/lib/systemd/systemd");
    std::fs::write(root.join(pid.to_string()).join("comm"), "systemd\n").unwrap();
    std::fs::create_dir_all(locked).unwrap();
    for fd in 0..entries {
        let target = locked.join(format!("{pid}-{fd}"));
        std::fs::write(&target, b"").unwrap();
        std::os::unix::fs::symlink(&target, root.join(pid.to_string()).join(format!("fd/{fd}"))).unwrap();
    }
}

/// OD-48 D5.2（2026-11-06 實機）：讀得到的同 uid 行程都沒有持有 tty，只剩部分同 uid 行程的 fd 目錄整個讀不到 →
/// `Partial`，並列出這些行程（讀得到 comm 才附）；不得把整次掃描降成 `Permission`（legacy 停止會一路等到 330 s）。
#[cfg(unix)]
#[test]
fn posix_uart_holder_scan_reports_uninspectable_same_uid_processes_as_partial() {
    if running_as_root() {
        return;
    }
    let tmp = tempfile::tempdir().unwrap();
    let me = PosixProcScan::new().current_uid().expect("unix uid");
    let tty = tmp.path().join("ttyUSB0");
    let unrelated = tmp.path().join("unrelated.log");
    std::fs::write(&tty, b"").unwrap();
    std::fs::write(&unrelated, b"").unwrap();
    // 讀得到、但沒有持有 tty 的同 uid 行程。
    fake_proc(tmp.path(), 800, 1, me, &["/usr/bin/bash"], "/usr/bin/bash");
    std::os::unix::fs::symlink(&unrelated, tmp.path().join("800/fd/3")).unwrap();
    fake_uninspectable_proc(tmp.path(), 802, me, None);
    fake_uninspectable_proc(tmp.path(), 801, me, Some("(sd-pam)"));
    let result = PosixProcScan::with_root(tmp.path()).uart_holders_same_uid(std::slice::from_ref(&tty));
    chmod(&tmp.path().join("801/fd"), 0o755);
    chmod(&tmp.path().join("802/fd"), 0o755);
    assert!(result.holders.is_empty(), "{result:?}");
    assert_eq!(result.status, HolderScanStatus::Partial, "{result:?}");
    assert_eq!(
        result.uninspectable,
        vec![
            UninspectableProc { pid: 801, comm: Some("(sd-pam)".into()), reason: UninspectableReason::FdDirDenied },
            UninspectableProc { pid: 802, comm: None, reason: UninspectableReason::FdDirDenied },
        ]
    );
}

/// OD-48 D5.2（2026-11-06 實機：pid 407 `systemd --user` 的 fd 目錄屬使用者、列得出 38 個條目，`stat -L` 全部
/// PermissionDenied）：fd 目錄讀得到、但沒有任何條目檢查得了的同 uid 行程，與 fd 目錄讀不到同樣列為整個無法檢查。
/// 目錄可讀但條目數為 0 的行程不是無法檢查（沒開檔），照常視為已檢查。
#[cfg(unix)]
#[test]
fn posix_uart_holder_scan_treats_all_fd_entries_denied_as_uninspectable() {
    if running_as_root() {
        return;
    }
    let tmp = tempfile::tempdir().unwrap();
    let me = PosixProcScan::new().current_uid().expect("unix uid");
    let tty = tmp.path().join("ttyUSB0");
    std::fs::write(&tty, b"").unwrap();
    let locked = tmp.path().join("locked");
    fake_entries_denied_proc(tmp.path(), 407, me, &locked, 3);
    fake_uninspectable_proc(tmp.path(), 408, me, Some("(sd-pam)"));
    // 目錄可讀、沒有任何 fd 條目。
    fake_proc(tmp.path(), 409, 1, me, &["/usr/bin/sleep", "60"], "/usr/bin/sleep");
    chmod(&locked, 0o000);
    let result = PosixProcScan::with_root(tmp.path()).uart_holders_same_uid(std::slice::from_ref(&tty));
    chmod(&locked, 0o755);
    chmod(&tmp.path().join("408/fd"), 0o755);
    assert!(result.holders.is_empty(), "{result:?}");
    assert_eq!(result.status, HolderScanStatus::Partial, "{result:?}");
    assert_eq!(
        result.uninspectable,
        vec![
            UninspectableProc { pid: 407, comm: Some("systemd".into()), reason: UninspectableReason::FdEntriesDenied },
            UninspectableProc { pid: 408, comm: Some("(sd-pam)".into()), reason: UninspectableReason::FdDirDenied },
        ]
    );
}

/// D5.2 範圍：讀得到的行程仍持有 tty 時照常列為 holder（狀態 `Partial` 不代表放行）。
#[cfg(unix)]
#[test]
fn posix_uart_holder_scan_partial_still_lists_readable_holder() {
    if running_as_root() {
        return;
    }
    let tmp = tempfile::tempdir().unwrap();
    let me = PosixProcScan::new().current_uid().expect("unix uid");
    let tty = tmp.path().join("ttyUSB0");
    std::fs::write(&tty, b"").unwrap();
    fake_proc(tmp.path(), 700, 1, me, &["minicom", "-D", "/dev/ttyUSB0"], "/usr/bin/minicom");
    std::os::unix::fs::symlink(&tty, tmp.path().join("700/fd/3")).unwrap();
    fake_uninspectable_proc(tmp.path(), 801, me, Some("(sd-pam)"));
    let result = PosixProcScan::with_root(tmp.path()).uart_holders_same_uid(std::slice::from_ref(&tty));
    chmod(&tmp.path().join("801/fd"), 0o755);
    assert_eq!(result.holders, vec![700], "{result:?}");
    assert_eq!(result.status, HolderScanStatus::Partial, "{result:?}");
}

/// D5.2 範圍（反例）：目標 tty 本身 metadata 回 PermissionDenied（無法比對）→ 維持 `Permission`，
/// 即使另有只是 fd 目錄讀不到的同 uid 行程。
#[cfg(unix)]
#[test]
fn posix_uart_holder_scan_target_permission_denied_still_blocks() {
    if running_as_root() {
        return;
    }
    let tmp = tempfile::tempdir().unwrap();
    let me = PosixProcScan::new().current_uid().expect("unix uid");
    let locked = tmp.path().join("locked");
    std::fs::create_dir(&locked).unwrap();
    let tty = locked.join("ttyUSB0");
    std::fs::write(&tty, b"").unwrap();
    fake_uninspectable_proc(tmp.path(), 801, me, Some("(sd-pam)"));
    chmod(&locked, 0o000);
    let result = PosixProcScan::with_root(tmp.path()).uart_holders_same_uid(std::slice::from_ref(&tty));
    chmod(&locked, 0o755);
    chmod(&tmp.path().join("801/fd"), 0o755);
    assert!(result.holders.is_empty(), "{result:?}");
    assert_eq!(result.status, HolderScanStatus::Permission, "{result:?}");
}

/// D5.2 範圍（反例）：同一行程部分 fd 條目可讀、部分回 PermissionDenied（混合）→ 維持 `Permission`，
/// 即使另有整個無法檢查的同 uid 行程。
#[cfg(unix)]
#[test]
fn posix_uart_holder_scan_mixed_fd_entry_permission_denied_still_blocks() {
    if running_as_root() {
        return;
    }
    let tmp = tempfile::tempdir().unwrap();
    let me = PosixProcScan::new().current_uid().expect("unix uid");
    let tty = tmp.path().join("ttyUSB0");
    let unrelated = tmp.path().join("unrelated.log");
    std::fs::write(&tty, b"").unwrap();
    std::fs::write(&unrelated, b"").unwrap();
    let locked = tmp.path().join("locked");
    std::fs::create_dir(&locked).unwrap();
    std::fs::write(locked.join("target"), b"").unwrap();
    // fd/3 檢查得了；fd/4 跟隨到 mode 000 目錄底下 → metadata 回 PermissionDenied。
    fake_proc(tmp.path(), 800, 1, me, &["/usr/bin/bash"], "/usr/bin/bash");
    std::os::unix::fs::symlink(&unrelated, tmp.path().join("800/fd/3")).unwrap();
    std::os::unix::fs::symlink(locked.join("target"), tmp.path().join("800/fd/4")).unwrap();
    fake_uninspectable_proc(tmp.path(), 801, me, Some("(sd-pam)"));
    chmod(&locked, 0o000);
    let result = PosixProcScan::with_root(tmp.path()).uart_holders_same_uid(std::slice::from_ref(&tty));
    chmod(&locked, 0o755);
    chmod(&tmp.path().join("801/fd"), 0o755);
    assert!(result.holders.is_empty(), "{result:?}");
    assert_eq!(result.status, HolderScanStatus::Permission, "{result:?}");
}

/// D5.2 範圍（反例）：另有其他原因的讀取失敗（`Unknown` 類）時不降為 `Partial`，沿用原本判定
/// （任一 PermissionDenied 即 `Permission`）。
#[cfg(unix)]
#[test]
fn posix_uart_holder_scan_unknown_error_still_blocks() {
    if running_as_root() {
        return;
    }
    let tmp = tempfile::tempdir().unwrap();
    let me = PosixProcScan::new().current_uid().expect("unix uid");
    let tty = tmp.path().join("ttyUSB0");
    std::fs::write(&tty, b"").unwrap();
    fake_proc(tmp.path(), 800, 1, me, &["/usr/bin/bash"], "/usr/bin/bash");
    std::fs::remove_dir(tmp.path().join("800/fd")).unwrap();
    std::fs::write(tmp.path().join("800/fd"), b"not a directory").unwrap();
    let alone = PosixProcScan::with_root(tmp.path()).uart_holders_same_uid(std::slice::from_ref(&tty));
    assert_eq!(alone.status, HolderScanStatus::Unknown, "{alone:?}");
    fake_uninspectable_proc(tmp.path(), 801, me, Some("(sd-pam)"));
    let result = PosixProcScan::with_root(tmp.path()).uart_holders_same_uid(std::slice::from_ref(&tty));
    chmod(&tmp.path().join("801/fd"), 0o755);
    assert!(result.holders.is_empty(), "{result:?}");
    assert_eq!(result.status, HolderScanStatus::Permission, "{result:?}");
}

/// D5.2 範圍（反例）：uid 讀不到（保守納入掃描）的行程整個無法檢查（fd 目錄讀不到，或條目全部讀不到），
/// 無法確認是同 uid → 維持 `Permission`。
#[cfg(unix)]
#[test]
fn posix_uart_holder_scan_uninspectable_process_with_unknown_uid_still_blocks() {
    if running_as_root() {
        return;
    }
    for entries_denied in [false, true] {
        let tmp = tempfile::tempdir().unwrap();
        let me = PosixProcScan::new().current_uid().expect("unix uid");
        let tty = tmp.path().join("ttyUSB0");
        std::fs::write(&tty, b"").unwrap();
        let locked = tmp.path().join("locked");
        if entries_denied {
            fake_entries_denied_proc(tmp.path(), 801, me, &locked, 2);
            chmod(&locked, 0o000);
        } else {
            fake_uninspectable_proc(tmp.path(), 801, me, Some("(sd-pam)"));
        }
        std::fs::remove_file(tmp.path().join("801/status")).unwrap();
        let result = PosixProcScan::with_root(tmp.path()).uart_holders_same_uid(std::slice::from_ref(&tty));
        if entries_denied {
            chmod(&locked, 0o755);
        } else {
            chmod(&tmp.path().join("801/fd"), 0o755);
        }
        assert!(result.holders.is_empty(), "entries_denied={entries_denied}: {result:?}");
        assert_eq!(result.status, HolderScanStatus::Permission, "entries_denied={entries_denied}: {result:?}");
    }
}

/// 其他呼叫者不受影響：`serialwrapd_tty_holders` 對整個無法檢查的 serialwrapd 仍回 `Permission`（不回 `Partial`）。
#[cfg(unix)]
#[test]
fn posix_serialwrapd_holder_scan_keeps_permission_for_uninspectable_daemon() {
    if running_as_root() {
        return;
    }
    for entries_denied in [false, true] {
        let tmp = tempfile::tempdir().unwrap();
        let me = PosixProcScan::new().current_uid().expect("unix uid");
        let tty = tmp.path().join("ttyUSB0");
        std::fs::write(&tty, b"").unwrap();
        let locked = tmp.path().join("locked");
        fake_proc(tmp.path(), 900, 1, me, &["/usr/bin/python3", "/opt/a/.local/bin/serialwrapd"], "/usr/bin/python3.12");
        if entries_denied {
            std::fs::create_dir(&locked).unwrap();
            std::fs::write(locked.join("target"), b"").unwrap();
            std::os::unix::fs::symlink(locked.join("target"), tmp.path().join("900/fd/3")).unwrap();
            chmod(&locked, 0o000);
        } else {
            chmod(&tmp.path().join("900/fd"), 0o000);
        }
        let (holders, status) = PosixProcScan::with_root(tmp.path()).serialwrapd_tty_holders(std::slice::from_ref(&tty));
        if entries_denied {
            chmod(&locked, 0o755);
        } else {
            chmod(&tmp.path().join("900/fd"), 0o755);
        }
        assert!(holders.is_empty(), "entries_denied={entries_denied}");
        assert_eq!(status, HolderScanStatus::Permission, "entries_denied={entries_denied}");
    }
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
