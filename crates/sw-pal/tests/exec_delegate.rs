//! `ProcessCtl::exec_or_wait` 委派行為（P3-T9）：exit code 與 env 原樣傳遞。
use std::ffi::OsString;

use sw_pal::traits::PalErrorKind;

#[cfg(unix)]
fn exit_with_env_argv() -> Vec<OsString> {
    vec![
        "/bin/sh".into(),
        "-c".into(),
        "[ \"$SW_X\" = ok ] && exit 37 || exit 1".into(),
    ]
}

#[cfg(windows)]
fn exit_with_env_argv() -> Vec<OsString> {
    vec![
        "cmd.exe".into(),
        "/d".into(),
        "/c".into(),
        "if \"%SW_X%\"==\"ok\" (exit 37) else (exit 1)".into(),
    ]
}

// 父測試以自身測試執行檔重新啟動子測試，子測試呼叫 exec_or_wait，父測試斷言 exit code 與 env。
#[test]
fn exec_or_wait_propagates_exit_code_and_env() {
    if std::env::var_os("SW_PAL_EXEC_CHILD").is_some() {
        let argv = exit_with_env_argv();
        let e = sw_pal::native_client()
            .process
            .exec_or_wait(&argv, &[("SW_X".into(), "ok".into())]);
        panic!("exec_or_wait 返回錯誤：{e:?}");
    }
    let st = std::process::Command::new(std::env::current_exe().unwrap())
        .args(["--exact", "exec_or_wait_propagates_exit_code_and_env", "--nocapture"])
        .env("SW_PAL_EXEC_CHILD", "1")
        .status()
        .unwrap();
    assert_eq!(st.code(), Some(37));
}

#[test]
fn exec_or_wait_reports_missing_program() {
    let e = sw_pal::native_client()
        .process
        .exec_or_wait(&["/definitely/not/here".into()], &[]);
    assert_eq!(e.kind, PalErrorKind::NotFound);
}

// Windows 互動 console 的 Ctrl 事件轉送（僅 Windows 原生執行；WSL interop 不能充數）。
#[cfg(windows)]
mod win_ctrl {
    use std::os::windows::process::CommandExt;
    use std::process::{Child, Command, ExitStatus};
    use std::time::{Duration, Instant};

    pub const CTRL_C_EVENT: u32 = 0;
    pub const CTRL_BREAK_EVENT: u32 = 1;
    const CREATE_NEW_PROCESS_GROUP: u32 = 0x0000_0200;

    pub fn interactive_console_attached() -> bool {
        use std::io::IsTerminal;
        std::io::stdin().is_terminal() && std::io::stdout().is_terminal()
    }

    pub fn spawn_self_in_new_console_group(test: &str, env_key: &str) -> Child {
        Command::new(std::env::current_exe().unwrap())
            .args(["--exact", test, "--nocapture"])
            .env(env_key, "1")
            .creation_flags(CREATE_NEW_PROCESS_GROUP)
            .spawn()
            .unwrap()
    }

    pub fn send_console_ctrl(event: u32, group_id: u32) {
        // SAFETY: 僅傳遞整數參數給 Win32 API；失敗以回傳值判定。
        let ok = unsafe {
            windows_sys::Win32::System::Console::GenerateConsoleCtrlEvent(event, group_id)
        };
        assert!(ok != 0, "GenerateConsoleCtrlEvent 失敗");
    }

    pub fn wait_with_timeout(child: &mut Child, limit: Duration) -> Option<ExitStatus> {
        let t0 = Instant::now();
        loop {
            if let Some(st) = child.try_wait().unwrap() {
                return Some(st);
            }
            if t0.elapsed() >= limit {
                return None;
            }
            std::thread::sleep(Duration::from_millis(100));
        }
    }
}

#[cfg(windows)]
#[test]
#[ignore = "cap:interactive_desktop"]
#[allow(non_snake_case)] // 依 P3 規範：cap_<resource>__<name> 命名
fn cap_interactive_desktop__exec_or_wait_ctrl_c() {
    let probe = sw_pal::native_client();
    assert!(
        probe.caps.path_flavor == sw_pal::PalPathFlavor::Windows
            && win_ctrl::interactive_console_attached(),
        "cap:interactive_desktop 需要 Windows 互動 console（證據不足，非 PASS）"
    );
    if std::env::var_os("SW_PAL_CTRLC_CHILD").is_some() {
        // 子：委派到一個會等待 30 s 的程式；Ctrl 事件應終止孫行程，並由 exec_or_wait 照傳 exit code。
        let e = probe.process.exec_or_wait(
            &[
                "cmd.exe".into(),
                "/d".into(),
                "/c".into(),
                "ping -n 30 127.0.0.1 >NUL".into(),
            ],
            &[],
        );
        panic!("exec_or_wait 返回錯誤：{e:?}");
    }
    for event in [win_ctrl::CTRL_C_EVENT, win_ctrl::CTRL_BREAK_EVENT] {
        let mut child = win_ctrl::spawn_self_in_new_console_group(
            "cap_interactive_desktop__exec_or_wait_ctrl_c",
            "SW_PAL_CTRLC_CHILD",
        );
        std::thread::sleep(std::time::Duration::from_secs(2));
        win_ctrl::send_console_ctrl(event, child.id());
        let st = win_ctrl::wait_with_timeout(&mut child, std::time::Duration::from_secs(10))
            .expect("10 s 內未結束");
        assert_eq!(st.code().map(|c| c as u32), Some(0xC000_013A), "event={event}");
    }
}

// 非 Windows：明確失敗（證據不足），不得空跑成 PASS。
#[cfg(not(windows))]
#[test]
#[ignore = "cap:interactive_desktop"]
#[allow(non_snake_case)] // 依 P3 規範：cap_<resource>__<name> 命名
fn cap_interactive_desktop__exec_or_wait_ctrl_c() {
    panic!("cap:interactive_desktop 需要 Windows 互動 console（證據不足，非 PASS）");
}
