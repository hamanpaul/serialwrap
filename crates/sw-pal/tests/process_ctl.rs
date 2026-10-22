//! `ProcessCtl` 測試（P4-T4）：daemon spawn、poll、call_foreground。
use std::ffi::OsString;
use std::sync::Arc;
use std::time::{Duration, Instant};
use sw_pal::native_client;
use sw_pal::traits::{ChildHandle, ProcessCtl};

#[cfg(unix)]
#[test]
fn spawn_daemon_detached_new_session() {
    let p = native_client();
    let child = ChildGuard::spawn(
        Arc::clone(&p.process),
        p.process
            .spawn_daemon(
                &["/bin/sleep".to_string(), "5".to_string()],
                &[
                    (OsString::from("PATH"), OsString::from("/usr/bin:/bin")),
                    (
                        OsString::from("P4_DETACHED_ENV"),
                        OsString::from("supplied"),
                    ),
                ],
                true,
            )
            .unwrap(),
    );

    let sid = unsafe { libc::getsid(child.handle.pid as libc::pid_t) };
    assert_eq!(sid, child.handle.pid as libc::pid_t);

    #[cfg(target_os = "linux")]
    {
        let stdin_target = std::fs::read_link("/proc/self/fd/0").unwrap();
        assert_eq!(
            std::fs::read_link(format!("/proc/{}/fd/0", child.handle.pid)).unwrap(),
            stdin_target
        );
        for fd in [1, 2] {
            assert_eq!(
                std::fs::read_link(format!("/proc/{}/fd/{fd}", child.handle.pid)).unwrap(),
                std::path::PathBuf::from("/dev/null")
            );
        }
        let environment = std::fs::read(format!("/proc/{}/environ", child.handle.pid)).unwrap();
        assert!(environment
            .split(|byte| *byte == 0)
            .any(|entry| entry == b"P4_DETACHED_ENV=supplied"));
        assert!(!environment
            .split(|byte| *byte == 0)
            .any(|entry| entry.starts_with(b"HOME=")));
    }
}

#[cfg(target_os = "linux")]
#[test]
fn spawn_daemon_closes_inherited_non_cloexec_fds_in_both_modes() {
    let p = native_client();
    let pipe = PipePair::new();

    for detached in [false, true] {
        let child = ChildGuard::spawn(
            Arc::clone(&p.process),
            p.process
                .spawn_daemon(
                    &["/bin/sleep".to_string(), "5".to_string()],
                    &[(OsString::from("PATH"), OsString::from("/usr/bin:/bin"))],
                    detached,
                )
                .unwrap(),
        );
        // spawn 回傳時子行程已 exec，但動態載入器可能仍短暫開著共享程式庫的 fd（readlink 前即消失）；
        // 在期限內重讀直到穩定。繼承而未關閉的 fd 會存活整個 `sleep 5`，期限到仍會判定失敗。
        let read_fds = || {
            let mut fds = std::fs::read_dir(format!("/proc/{}/fd", child.handle.pid))
                .unwrap()
                .map(|entry| entry.unwrap().file_name().to_string_lossy().into_owned())
                .collect::<Vec<_>>();
            fds.sort();
            fds
        };
        let deadline = std::time::Instant::now() + std::time::Duration::from_secs(2);
        let mut fds = read_fds();
        while fds != ["0", "1", "2"] && std::time::Instant::now() < deadline {
            std::thread::sleep(std::time::Duration::from_millis(10));
            fds = read_fds();
        }
        assert_eq!(fds, vec!["0", "1", "2"]);
        drop(child);
    }
    drop(pipe);
}

#[test]
fn call_foreground_returns_exit_code() {
    let p = native_client();
    #[cfg(unix)]
    let argv = vec!["sh".to_string(), "-c".to_string(), "exit 3".to_string()];
    #[cfg(windows)]
    let argv = vec![
        "cmd.exe".to_string(),
        "/c".to_string(),
        "exit 3".to_string(),
    ];

    let rc = p.process.call_foreground(&argv, &[]).unwrap();
    assert_eq!(rc, 3);
}

#[cfg(unix)]
#[test]
fn call_foreground_returns_negative_signal_status() {
    let p = native_client();
    let rc = p
        .process
        .call_foreground(
            &[
                "/bin/sh".to_string(),
                "-c".to_string(),
                "kill -9 $$".to_string(),
            ],
            &[],
        )
        .unwrap();
    assert_eq!(rc, -9);
}

#[test]
fn spawn_daemon_poll_is_nonblocking_and_returns_exit_code() {
    let p = native_client();
    #[cfg(unix)]
    let argv = vec![
        "/bin/sh".to_string(),
        "-c".to_string(),
        "sleep 0.5; test \"$P4_POLL_ENV\" = supplied && test -z \"${HOME+x}\"".to_string(),
    ];
    #[cfg(windows)]
    let argv = vec![
        "cmd.exe".to_string(),
        "/C".to_string(),
        "ping -n 2 127.0.0.1 >NUL & if defined P4_POLL_ENV (if defined HOME (exit /b 9) else (exit /b 0)) else (exit /b 8)".to_string(),
    ];

    #[cfg(unix)]
    let env = vec![
        (
            OsString::from("PATH"),
            std::env::var_os("PATH").unwrap_or_default(),
        ),
        (OsString::from("P4_POLL_ENV"), OsString::from("supplied")),
    ];
    #[cfg(windows)]
    let env = vec![
        (
            OsString::from("PATH"),
            std::env::var_os("PATH").unwrap_or_default(),
        ),
        (
            OsString::from("SystemRoot"),
            std::env::var_os("SystemRoot").unwrap_or_default(),
        ),
        (OsString::from("P4_POLL_ENV"), OsString::from("supplied")),
    ];

    let child = p.process.spawn_daemon(&argv, &env, false).unwrap();
    let started = Instant::now();
    assert_eq!(p.process.poll(&child), None);
    assert!(started.elapsed() < Duration::from_millis(250));

    let deadline = Instant::now() + Duration::from_secs(3);
    loop {
        if let Some(code) = p.process.poll(&child) {
            assert_eq!(code, 0);
            assert_eq!(p.process.poll(&child), Some(0));
            break;
        }
        assert!(Instant::now() < deadline, "child process did not exit");
        std::thread::sleep(Duration::from_millis(10));
    }
}

#[cfg(unix)]
#[test]
fn spawn_daemon_poll_preserves_signal_exit_status() {
    let p = native_client();
    let mut child = ChildGuard::spawn(
        Arc::clone(&p.process),
        p.process
            .spawn_daemon(
                &[
                    "/bin/sh".to_string(),
                    "-c".to_string(),
                    "kill -9 $$".to_string(),
                ],
                &[],
                false,
            )
            .unwrap(),
    );
    let deadline = Instant::now() + Duration::from_secs(3);
    loop {
        if let Some(code) = child.poll() {
            assert_eq!(code, -9);
            assert_eq!(child.poll(), Some(-9));
            break;
        }
        assert!(
            Instant::now() < deadline,
            "signalled child process did not exit"
        );
        std::thread::sleep(Duration::from_millis(10));
    }
}

#[cfg(unix)]
#[test]
fn poll_treats_echild_as_exited_when_sigchld_is_ignored() {
    const HELPER_ENV: &str = "SW_PAL_SIGCHLD_IGNORED_HELPER";
    if std::env::var_os(HELPER_ENV).is_some() {
        // SAFETY: 此 helper 在獨立測試子行程中執行，SIGCHLD 設定不會影響測試 runner。
        unsafe { libc::signal(libc::SIGCHLD, libc::SIG_IGN) };
        let p = native_client();
        let mut child = ChildGuard::spawn(
            Arc::clone(&p.process),
            p.process
                .spawn_daemon(
                    &[
                        "/bin/sh".to_string(),
                        "-c".to_string(),
                        "exit 7".to_string(),
                    ],
                    &[],
                    false,
                )
                .unwrap(),
        );
        let deadline = Instant::now() + Duration::from_secs(3);
        loop {
            if let Some(code) = child.poll() {
                assert_eq!(code, 0);
                assert_eq!(child.poll(), Some(0));
                break;
            }
            assert!(
                Instant::now() < deadline,
                "ECHILD child was not reported exited"
            );
            std::thread::sleep(Duration::from_millis(10));
        }
        return;
    }

    // 以 output() 擷取子行程的 stdout／stderr：子 libtest 會印出 `running 1 test`、`.`、
    // `test result:` 等列，若繼承父行程 stdout，會與父行程並行的 `test … ok` 結果列交錯，
    // 造成 L1 結果解析漏判。輸出僅在斷言失敗時放入訊息，不改變判定條件。
    let output = std::process::Command::new(std::env::current_exe().unwrap())
        .args([
            "--exact",
            "poll_treats_echild_as_exited_when_sigchld_is_ignored",
            "--quiet",
        ])
        .env(HELPER_ENV, "1")
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "ECHILD helper 子行程失敗（status={:?}）\nstdout:\n{}\nstderr:\n{}",
        output.status,
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr),
    );
}

#[cfg(windows)]
#[test]
fn windows_exit_code_child_helper() {
    if std::env::var_os("SW_PAL_WINDOWS_EXIT_CODE_HELPER").is_some() {
        std::process::exit(0xC000_013Au32 as i32);
    }
}

#[cfg(windows)]
#[test]
fn windows_process_ctl_preserves_unsigned_exit_code() {
    let p = native_client();
    let exe = std::env::current_exe().unwrap();
    let child_argv = vec![
        exe.to_string_lossy().into_owned(),
        "--exact".to_string(),
        "windows_exit_code_child_helper".to_string(),
        "--quiet".to_string(),
    ];
    let env = windows_test_environment("SW_PAL_WINDOWS_EXIT_CODE_HELPER");
    let expected = i64::from(0xC000_013Au32);
    let child = p.process.spawn_daemon(&child_argv, &env, false).unwrap();
    let deadline = Instant::now() + Duration::from_secs(5);
    loop {
        if let Some(code) = p.process.poll(&child) {
            assert_eq!(code, expected);
            assert_eq!(p.process.poll(&child), Some(expected));
            break;
        }
        assert!(
            Instant::now() < deadline,
            "Windows child process did not exit"
        );
        std::thread::sleep(Duration::from_millis(10));
    }

    let mut foreground_argv = child_argv;
    foreground_argv[1] = "--exact".to_string();
    let foreground_env = windows_test_environment("SW_PAL_WINDOWS_EXIT_CODE_HELPER");
    assert_eq!(
        p.process
            .call_foreground(&foreground_argv, &foreground_env)
            .unwrap(),
        expected
    );
}

#[cfg(windows)]
fn windows_test_environment(helper_name: &str) -> Vec<(OsString, OsString)> {
    let mut env = vec![(OsString::from(helper_name), OsString::from("1"))];
    for key in ["PATH", "SystemRoot"] {
        if let Some(value) = std::env::var_os(key) {
            env.push((OsString::from(key), value));
        }
    }
    env
}

#[cfg(unix)]
struct ChildGuard {
    process: Arc<dyn ProcessCtl>,
    handle: ChildHandle,
    finished: bool,
}

#[cfg(unix)]
impl ChildGuard {
    fn spawn(process: Arc<dyn ProcessCtl>, handle: ChildHandle) -> Self {
        Self {
            process,
            handle,
            finished: false,
        }
    }

    fn poll(&mut self) -> Option<i64> {
        let result = self.process.poll(&self.handle);
        if result.is_some() {
            self.finished = true;
        }
        result
    }
}

#[cfg(unix)]
impl Drop for ChildGuard {
    fn drop(&mut self) {
        if self.finished {
            return;
        }
        // SAFETY: 此句柄記錄的是本測試剛建立的子行程 pid。
        unsafe { libc::kill(self.handle.pid as libc::pid_t, libc::SIGKILL) };
        let deadline = Instant::now() + Duration::from_secs(3);
        while self.poll().is_none() && Instant::now() < deadline {
            std::thread::sleep(Duration::from_millis(10));
        }
    }
}

#[cfg(target_os = "linux")]
struct PipePair([libc::c_int; 2]);

#[cfg(target_os = "linux")]
impl PipePair {
    fn new() -> Self {
        let mut fds = [-1; 2];
        // SAFETY: fds 指向兩個可寫整數，pipe 成功後分別持有 read/write descriptor。
        assert_eq!(unsafe { libc::pipe(fds.as_mut_ptr()) }, 0);
        Self(fds)
    }
}

#[cfg(target_os = "linux")]
impl Drop for PipePair {
    fn drop(&mut self) {
        for fd in self.0 {
            // SAFETY: 兩個 descriptor 由此測試建立，且各關閉一次。
            unsafe { libc::close(fd) };
        }
    }
}
