//! `ProcScan` 行程掃描測試（P4-T4）：
//! - Windows launcher 與 Python 子行程去重（SC-17）
//! - POSIX `/proc` 檔案系統掃描與 PID 排序
//! - `is_serialwrapd_argv` 規則 1～4 與 Windows 大小寫比對
//! - `--socket` 引數解析
//! - `pid_alive` 與 `identity_alive` 存活判定

use sw_pal::proc_scan::*;

fn w(pid: u32, ppid: u32, image: &str, argv: &[&str], t: u64) -> WinProcInfo {
    WinProcInfo {
        pid,
        parent_pid: ppid,
        image: image.into(),
        argv: argv.iter().map(|s| s.to_string()).collect(),
        creation_time: t,
    }
}

#[test]
fn windows_launcher_and_python_child_count_as_one_daemon() {
    let procs = vec![
        w(
            100,
            4,
            "serialwrapd.exe",
            &[
                "C:\\p\\bin\\serialwrapd.exe",
                "--socket",
                "tcp://127.0.0.1:48700",
            ],
            10,
        ),
        w(
            101,
            100,
            "python.exe",
            &[
                "C:\\p\\versions\\0.4.0\\py\\python.exe",
                "-I",
                "C:\\p\\versions\\0.4.0\\py\\serialwrapd.py",
                "--socket",
                "tcp://127.0.0.1:48700",
            ],
            11,
        ),
        w(200, 4, "python.exe", &["python.exe", "-c", "print(1)"], 12),
    ];
    let d = dedup_launcher_children(&procs);
    assert_eq!(d.len(), 1);
    assert_eq!(
        (d[0].pid, d[0].socket.as_deref()),
        (100, Some("tcp://127.0.0.1:48700"))
    );
}

#[test]
fn reused_parent_pid_does_not_swallow_child() {
    // 子行程比「同 pid 的父」更早建立 → 父 pid 已被重用，不得去重
    let procs = vec![
        w(
            100,
            4,
            "serialwrapd.exe",
            &["serialwrapd.exe", "--socket", "tcp://127.0.0.1:48701"],
            50,
        ),
        w(
            101,
            100,
            "python.exe",
            &["python.exe", "-I", "serialwrapd.py"],
            20,
        ),
    ];
    assert_eq!(dedup_launcher_children(&procs).len(), 2);
}

#[test]
fn windows_pyinstaller_onefile_pair_counts_as_one_daemon() {
    let procs = vec![
        w(
            100,
            4,
            "serialwrapd.exe",
            &[
                "C:\\p\\bin\\serialwrapd.exe",
                "--socket",
                "tcp://127.0.0.1:48700",
            ],
            10,
        ),
        w(
            101,
            100,
            "serialwrapd.exe",
            &[
                "C:\\p\\bin\\serialwrapd.exe",
                "--internal-child",
                "--socket",
                "tcp://127.0.0.1:48700",
            ],
            11,
        ),
    ];

    let daemons = dedup_launcher_children(&procs);
    assert_eq!(daemons.len(), 1);
    assert_eq!(daemons[0].pid, 100);
    assert_eq!(daemons[0].socket.as_deref(), Some("tcp://127.0.0.1:48700"));
    assert_eq!(
        daemons[0].tree,
        vec![
            ProcIdentity {
                pid: 100,
                start_ticks: Some(10),
            },
            ProcIdentity {
                pid: 101,
                start_ticks: Some(11),
            },
        ]
    );
}

#[test]
fn windows_launcher_with_unknown_creation_time_is_kept_without_ticks() {
    let procs = vec![w(100, 4, "serialwrapd.exe", &[], 0)];

    let daemons = dedup_launcher_children(&procs);
    assert_eq!(daemons.len(), 1);
    assert_eq!(daemons[0].pid, 100);
    assert_eq!(daemons[0].socket, None);
    assert_eq!(
        daemons[0].tree,
        vec![ProcIdentity {
            pid: 100,
            start_ticks: None,
        }]
    );
}

#[test]
fn reused_launcher_pid_does_not_swallow_launcher_child() {
    let procs = vec![
        w(100, 4, "serialwrapd.exe", &["serialwrapd.exe"], 50),
        w(101, 100, "serialwrapd.exe", &["serialwrapd.exe"], 20),
    ];

    assert_eq!(dedup_launcher_children(&procs).len(), 2);
}

#[test]
fn launcher_parent_cycle_does_not_merge_processes() {
    let procs = vec![
        w(100, 101, "serialwrapd.exe", &["serialwrapd.exe"], 10),
        w(101, 100, "serialwrapd.exe", &["serialwrapd.exe"], 10),
    ];

    assert_eq!(dedup_launcher_children(&procs).len(), 2);
}

#[test]
fn posix_scan_reads_fake_proc_root_sorted_by_pid() {
    let tmp = tempfile::tempdir().unwrap();
    for (pid, cmd) in [
        (
            "300",
            "/usr/bin/python3\0/opt/sw/serialwrapd.py\0--socket=/run/a.sock\0",
        ),
        ("20", "serialwrapd\0--socket\0/run/b.sock\0"),
        ("21", "bash\0-c\0echo\0"),
        ("self", "x\0"),
    ] {
        std::fs::create_dir_all(tmp.path().join(pid)).unwrap();
        std::fs::write(tmp.path().join(pid).join("cmdline"), cmd).unwrap();
    }
    let got = PosixProcScan::with_root(tmp.path())
        .serialwrapd_instances()
        .unwrap();
    assert_eq!(
        got.iter()
            .map(|d| (d.pid, d.socket.clone()))
            .collect::<Vec<_>>(),
        vec![
            (20, Some("/run/b.sock".into())),
            (300, Some("/run/a.sock".into()))
        ]
    );
}

#[test]
fn is_serialwrapd_rule1_basename() {
    assert!(is_serialwrapd_argv(
        &["/x/serialwrapd".into()],
        ArgvFlavor::Posix
    ));
    assert!(!is_serialwrapd_argv(
        &["/x/serialwrapd2".into()],
        ArgvFlavor::Posix
    ));
}

#[test]
fn is_serialwrapd_rule2_py_suffix_anywhere() {
    assert!(is_serialwrapd_argv(
        &["python3".into(), "a/serialwrapd.py".into()],
        ArgvFlavor::Posix
    ));
}

#[test]
fn is_serialwrapd_rule3_requires_slash() {
    assert!(!is_serialwrapd_argv(
        &["env".into(), "serialwrapd".into()],
        ArgvFlavor::Posix
    ));
    assert!(is_serialwrapd_argv(
        &["env".into(), "/opt/serialwrapd".into()],
        ArgvFlavor::Posix
    ));
}

#[test]
fn is_serialwrapd_rule4_module_form() {
    assert!(is_serialwrapd_argv(
        &[
            "/v/py/bin/python3".into(),
            "-I".into(),
            "-m".into(),
            "sw_core.daemon".into(),
            "--socket".into(),
            "/s".into()
        ],
        ArgvFlavor::Posix
    ));
    assert!(!is_serialwrapd_argv(
        &["python3".into(), "-m".into(), "sw_core.cli".into()],
        ArgvFlavor::Posix
    ));
    assert!(!is_serialwrapd_argv(
        &["python3".into(), "-c".into(), "-m sw_core.daemon".into()],
        ArgvFlavor::Posix
    ));
}

#[test]
fn is_serialwrapd_windows_exe_case_insensitive() {
    assert!(is_serialwrapd_argv(
        &[
            "C:\\p\\bin\\serialwrapd.exe".into(),
            "--socket".into(),
            "tcp://127.0.0.1:48700".into()
        ],
        ArgvFlavor::Windows
    ));
    assert!(is_serialwrapd_argv(
        &["C:\\P\\BIN\\SerialwrapD.EXE".into()],
        ArgvFlavor::Windows
    ));
    assert!(!is_serialwrapd_argv(
        &["C:\\x\\serialwrapd2.exe".into()],
        ArgvFlavor::Windows
    ));
    assert!(!is_serialwrapd_argv(
        &["python.exe".into(), "-c".into(), "print(1)".into()],
        ArgvFlavor::Windows
    ));
    assert!(!is_serialwrapd_argv(
        &["serialwrapd.exe".into()],
        ArgvFlavor::Posix
    ));
}

#[test]
fn windows_launcher_only_counts_one() {
    let procs = vec![w(
        100,
        4,
        "serialwrapd.exe",
        &[
            "C:\\p\\bin\\serialwrapd.exe",
            "--socket",
            "tcp://127.0.0.1:48700",
        ],
        10,
    )];
    let d = dedup_launcher_children(&procs);
    assert_eq!(d.len(), 1);
    assert_eq!(
        (d[0].pid, d[0].socket.as_deref()),
        (100, Some("tcp://127.0.0.1:48700"))
    );
}

#[test]
fn windows_unreadable_launcher_cmdline_kept_as_unknown_socket() {
    let procs = vec![w(100, 4, "serialwrapd.exe", &[], 10)];
    let d = dedup_launcher_children(&procs);
    assert_eq!(d.len(), 1);
    assert_eq!((d[0].pid, d[0].socket.as_deref()), (100, None));
}

#[test]
fn extract_socket_equals_form_and_trailing_flag() {
    assert_eq!(
        extract_socket_arg(&["serialwrapd".into(), "--socket".into()]),
        None
    );
    assert_eq!(
        extract_socket_arg(&["serialwrapd".into(), "--socket=x".into()]),
        Some("x".into())
    );
}

#[test]
fn posix_scan_missing_root_is_scan_failure() {
    let tmp = tempfile::tempdir().unwrap();
    let missing = tmp.path().join("does_not_exist");
    let got = PosixProcScan::with_root(&missing).serialwrapd_instances();
    assert_eq!(got, Err(ScanFailure));
}

#[cfg(unix)]
#[test]
fn pid_alive_self_true_and_reaped_false() {
    let scan = PosixProcScan::new();
    let self_pid = std::process::id();
    assert!(scan.pid_alive(self_pid));

    let mut child = std::process::Command::new("true").spawn().unwrap();
    let child_pid = child.id();
    child.wait().unwrap();
    assert!(!scan.pid_alive(child_pid));
}

#[test]
fn pid_alive_false_for_zombie() {
    let tmp = tempfile::tempdir().unwrap();
    std::fs::create_dir_all(tmp.path().join("101")).unwrap();
    std::fs::write(
        tmp.path().join("101").join("stat"),
        "101 (zombie) Z 1 0 0\n",
    )
    .unwrap();
    std::fs::create_dir_all(tmp.path().join("102")).unwrap();
    std::fs::write(tmp.path().join("102").join("stat"), "102 (dead) X 1 0 0\n").unwrap();
    std::fs::create_dir_all(tmp.path().join("103")).unwrap();
    std::fs::write(
        tmp.path().join("103").join("stat"),
        "103 (sleeping) S 1 0 0\n",
    )
    .unwrap();

    let scan = PosixProcScan::with_root(tmp.path());
    assert!(!scan.pid_alive(101));
    assert!(!scan.pid_alive(102));
    assert!(scan.pid_alive(103));
}

#[cfg(unix)]
#[test]
fn pid_and_identity_alive_handle_non_utf8_comm_and_matching_ticks() {
    let tmp = tempfile::tempdir().unwrap();
    let pid = 201;
    std::fs::create_dir_all(tmp.path().join(pid.to_string())).unwrap();
    std::fs::write(
        tmp.path().join(pid.to_string()).join("stat"),
        fake_proc_stat(pid, &[b'n', 0xff, b'm', b'e'], b'S', 778),
    )
    .unwrap();

    let scan = PosixProcScan::with_root(tmp.path());
    assert!(scan.pid_alive(pid));
    assert!(scan.identity_alive(&ProcIdentity {
        pid,
        start_ticks: Some(778),
    }));
}

#[cfg(unix)]
#[test]
fn incomplete_proc_stat_is_conservatively_alive() {
    let tmp = tempfile::tempdir().unwrap();
    let pid = 202;
    std::fs::create_dir_all(tmp.path().join(pid.to_string())).unwrap();
    std::fs::write(
        tmp.path().join(pid.to_string()).join("stat"),
        format!("{pid} (truncated) S 1 2\n"),
    )
    .unwrap();

    let scan = PosixProcScan::with_root(tmp.path());
    assert!(scan.pid_alive(pid));
    assert!(scan.identity_alive(&ProcIdentity {
        pid,
        start_ticks: Some(778),
    }));
}

#[cfg(unix)]
#[test]
fn missing_proc_stat_is_not_alive() {
    let tmp = tempfile::tempdir().unwrap();
    let pid = 203;
    std::fs::create_dir_all(tmp.path().join(pid.to_string())).unwrap();

    let scan = PosixProcScan::with_root(tmp.path());
    assert!(!scan.pid_alive(pid));
    assert!(!scan.identity_alive(&ProcIdentity {
        pid,
        start_ticks: Some(778),
    }));
}

#[cfg(unix)]
#[test]
fn unreadable_proc_stat_is_conservatively_alive() {
    use std::os::unix::fs::PermissionsExt;

    // root 可繞過 mode 000，因此只在此處不會測到 PermissionDenied。
    if unsafe { libc::geteuid() } == 0 {
        return;
    }
    let tmp = tempfile::tempdir().unwrap();
    let pid = 204;
    let process_dir = tmp.path().join(pid.to_string());
    std::fs::create_dir_all(&process_dir).unwrap();
    let stat = process_dir.join("stat");
    std::fs::write(&stat, fake_proc_stat(pid, b"blocked", b'S', 778)).unwrap();
    std::fs::set_permissions(&stat, std::fs::Permissions::from_mode(0)).unwrap();

    let scan = PosixProcScan::with_root(tmp.path());
    assert!(scan.pid_alive(pid));
    assert!(scan.identity_alive(&ProcIdentity {
        pid,
        start_ticks: Some(778),
    }));
}

#[cfg(unix)]
#[test]
fn posix_tty_holder_scan_reports_matching_pid_and_ok_status() {
    let tmp = tempfile::tempdir().unwrap();
    let pid = std::process::id();
    let process_dir = tmp.path().join(pid.to_string());
    std::fs::create_dir_all(process_dir.join("fd")).unwrap();
    std::fs::write(process_dir.join("cmdline"), b"serialwrapd\0").unwrap();
    let tty = tmp.path().join("tty-fake");
    std::fs::write(&tty, b"").unwrap();
    std::os::unix::fs::symlink(&tty, process_dir.join("fd/7")).unwrap();

    let (holders, status) =
        PosixProcScan::with_root(tmp.path()).serialwrapd_tty_holders(std::slice::from_ref(&tty));
    assert_eq!(holders, vec![pid]);
    assert_eq!(status, HolderScanStatus::Ok);
}

#[cfg(unix)]
#[test]
fn posix_tty_holder_scan_ignores_unrelated_tty_file() {
    // #R2：不同 inode（非同一 tty）即使路徑形態相近，也不得判為 holder。
    let tmp = tempfile::tempdir().unwrap();
    let pid = std::process::id();
    let process_dir = tmp.path().join(pid.to_string());
    std::fs::create_dir_all(process_dir.join("fd")).unwrap();
    std::fs::write(process_dir.join("cmdline"), b"serialwrapd\0").unwrap();
    let held = tmp.path().join("tty-held");
    let wanted = tmp.path().join("tty-wanted");
    std::fs::write(&held, b"").unwrap();
    std::fs::write(&wanted, b"").unwrap();
    std::os::unix::fs::symlink(&held, process_dir.join("fd/7")).unwrap();

    let (holders, status) =
        PosixProcScan::with_root(tmp.path()).serialwrapd_tty_holders(std::slice::from_ref(&wanted));
    assert!(holders.is_empty());
    assert_eq!(status, HolderScanStatus::Ok);
}

#[cfg(unix)]
#[test]
fn posix_tty_holder_scan_reports_unknown_when_target_unstatable() {
    // #R2／Codex P2：目標 tty 無法取得身分時，不得回「無 holder、Ok」（那會被當成已確認可安全開啟）。
    let tmp = tempfile::tempdir().unwrap();
    let (holders, status) = PosixProcScan::with_root(tmp.path())
        .serialwrapd_tty_holders(&[tmp.path().join("tty-missing")]);
    assert!(holders.is_empty());
    assert_eq!(status, HolderScanStatus::Unknown);
}

#[cfg(unix)]
#[test]
fn posix_multi_open_holder_scan_skips_unstatable_target_like_detect_multi_open() {
    // daemon status／doctor 走 Python `detect_multi_open`：`tty_stat` 回 None 即略過該 tty，status 維持 ok；
    // 與 fail-closed 的 `serialwrapd_tty_holders`（上一案）刻意不同。
    let tmp = tempfile::tempdir().unwrap();
    let (holders, status) = PosixProcScan::with_root(tmp.path())
        .multi_open_tty_holders(&[tmp.path().join("tty-missing")]);
    assert!(holders.is_empty());
    assert_eq!(status, HolderScanStatus::Ok);
}

#[cfg(unix)]
#[test]
fn posix_tty_holder_scan_reports_unknown_on_unreadable_fd_tree() {
    let tmp = tempfile::tempdir().unwrap();
    let process_dir = tmp.path().join("205");
    std::fs::create_dir_all(&process_dir).unwrap();
    std::fs::write(process_dir.join("cmdline"), b"serialwrapd\0").unwrap();
    std::fs::write(process_dir.join("fd"), b"not a directory").unwrap();

    let (holders, status) = PosixProcScan::with_root(tmp.path())
        .serialwrapd_tty_holders(&[tmp.path().join("tty-fake")]);
    assert!(holders.is_empty());
    assert_eq!(status, HolderScanStatus::Unknown);
}

#[cfg(unix)]
#[test]
fn posix_tty_holder_scan_reports_permission_status() {
    use std::os::unix::fs::PermissionsExt;

    // root 可繞過目錄權限，因此只在此處不會測到 PermissionDenied。
    if unsafe { libc::geteuid() } == 0 {
        return;
    }
    let tmp = tempfile::tempdir().unwrap();
    let process_dir = tmp.path().join("206");
    let fd_dir = process_dir.join("fd");
    std::fs::create_dir_all(&fd_dir).unwrap();
    std::fs::write(process_dir.join("cmdline"), b"serialwrapd\0").unwrap();
    std::fs::set_permissions(&fd_dir, std::fs::Permissions::from_mode(0)).unwrap();

    let (holders, status) = PosixProcScan::with_root(tmp.path())
        .serialwrapd_tty_holders(&[tmp.path().join("tty-fake")]);
    assert!(holders.is_empty());
    assert_eq!(status, HolderScanStatus::Permission);
}

#[cfg(unix)]
#[test]
fn identity_alive_detects_pid_reuse() {
    let scan = PosixProcScan::new();
    let id = ProcIdentity {
        pid: std::process::id(),
        start_ticks: Some(1),
    };
    assert!(!scan.identity_alive(&id));
}

#[cfg(windows)]
#[test]
fn windows_tty_holder_scan_is_unsupported_and_empty() {
    use sw_pal::proc_scan::WindowsProcScan;

    let (holders, status) = WindowsProcScan::new().serialwrapd_tty_holders(&[]);
    assert!(holders.is_empty());
    assert_eq!(status, HolderScanStatus::Unsupported);
}

#[cfg(windows)]
#[test]
fn windows_scan_helper_hold() {
    if std::env::var_os("SW_PAL_HELPER_HOLD").is_some() {
        std::thread::sleep(std::time::Duration::from_secs(10));
    }
}

#[cfg(windows)]
#[test]
fn windows_scan_finds_self_spawned_fake_daemon() {
    use sw_pal::proc_scan::WindowsProcScan;
    let tmp = tempfile::tempdir().unwrap();
    let fake_exe = tmp.path().join("serialwrapd.exe");
    std::fs::copy(std::env::current_exe().unwrap(), &fake_exe).unwrap();

    let mut child = std::process::Command::new(&fake_exe)
        .args([
            "--exact",
            "windows_scan_helper_hold",
            "--quiet",
            "--",
            "--socket",
            "tcp://127.0.0.1:1",
        ])
        .env("SW_PAL_HELPER_HOLD", "1")
        .spawn()
        .unwrap();
    let child_pid = child.id();

    let scan = WindowsProcScan::new();
    let instances = scan.serialwrapd_instances();
    let _ = child.kill();
    let _ = child.wait();
    let instances = instances.unwrap();
    let found = instances.iter().find(|d| d.pid == child_pid);
    assert!(found.is_some());
    assert_eq!(found.unwrap().socket.as_deref(), Some("tcp://127.0.0.1:1"));

    let instances_after = scan.serialwrapd_instances().unwrap();
    assert!(!instances_after.iter().any(|d| d.pid == child_pid));
}

#[cfg(windows)]
#[test]
fn windows_pid_alive_never_terminates_target() {
    use sw_pal::proc_scan::WindowsProcScan;
    let mut child = std::process::Command::new("cmd.exe")
        .args(["/c", "pause"])
        .spawn()
        .unwrap();
    let child_pid = child.id();

    let scan = WindowsProcScan::new();
    for _ in 0..100 {
        assert!(scan.pid_alive(child_pid));
    }
    assert!(scan.pid_alive(child_pid));

    let _ = child.kill();
    let _ = child.wait();
}

#[cfg(unix)]
fn fake_proc_stat(pid: u32, comm: &[u8], state: u8, start_ticks: u64) -> Vec<u8> {
    let mut stat = format!("{pid} (").into_bytes();
    stat.extend_from_slice(comm);
    stat.extend_from_slice(b") ");
    stat.push(state);
    for _ in 0..18 {
        stat.extend_from_slice(b" 0");
    }
    stat.extend_from_slice(format!(" {start_ticks}\n").as_bytes());
    stat
}
