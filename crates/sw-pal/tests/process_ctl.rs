//! `ProcessCtl` 測試（P4-T4）：daemon spawn、poll、call_foreground。
use sw_pal::native_client;

#[cfg(unix)]
#[test]
fn spawn_daemon_detached_new_session() {
    let p = native_client();
    let child = p
        .process
        .spawn_daemon(
            &[
                "sh".to_string(),
                "-c".to_string(),
                "echo stdout_should_be_nulled; sleep 2".to_string(),
            ],
            &[],
            true,
        )
        .unwrap();

    let sid = unsafe { libc::getsid(child.pid as libc::pid_t) };
    assert_eq!(sid, child.pid as libc::pid_t);

    unsafe {
        libc::kill(child.pid as libc::pid_t, libc::SIGKILL);
        libc::waitpid(child.pid as libc::pid_t, std::ptr::null_mut(), 0);
    }
}

#[test]
fn call_foreground_returns_exit_code() {
    let p = native_client();
    #[cfg(unix)]
    let argv = vec!["sh".to_string(), "-c".to_string(), "exit 3".to_string()];
    #[cfg(windows)]
    let argv = vec!["cmd.exe".to_string(), "/c".to_string(), "exit 3".to_string()];

    let rc = p.process.call_foreground(&argv, &[]).unwrap();
    assert_eq!(rc, 3);
}
