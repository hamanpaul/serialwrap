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
    // 以 output() 擷取子行程 stdout／stderr：子 libtest 的 `running 1 test`、`test result:` 等列若繼承父行程
    // stdout，會與父行程並行輸出的結果列交錯（L1 結果解析漏判）。輸出僅在斷言失敗時放入訊息。
    let out = std::process::Command::new(std::env::current_exe().unwrap())
        .args(["--exact", "exec_or_wait_propagates_exit_code_and_env", "--nocapture"])
        .env("SW_PAL_EXEC_CHILD", "1")
        .output()
        .unwrap();
    assert_eq!(
        out.status.code(),
        Some(37),
        "exec_or_wait 子行程結束碼不符\nstdout:\n{}\nstderr:\n{}",
        String::from_utf8_lossy(&out.stdout),
        String::from_utf8_lossy(&out.stderr),
    );
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
    use std::io::Write;
    use std::os::windows::process::CommandExt;
    use std::path::{Path, PathBuf};
    use std::process::{Child, Command, ExitStatus};
    use std::time::{Duration, Instant};

    pub const CTRL_C_EVENT: u32 = 0;
    pub const CTRL_BREAK_EVENT: u32 = 1;
    const CREATE_NEW_PROCESS_GROUP: u32 = 0x0000_0200;
    pub const CHILD_ENV: &str = "SW_PAL_CTRLC_CHILD";
    pub const READY_ENV: &str = "SW_PAL_CTRLC_READY";
    /// 擁有者 runbook 指定的診斷目錄；未設時用 `%TEMP%`。console 輸出未被 transcript 收錄時仍可由此取證。
    pub const DIAG_ENV: &str = "SW_PAL_CTRLC_DIAG_DIR";

    pub fn interactive_console_attached() -> bool {
        use std::io::IsTerminal;
        std::io::stdin().is_terminal() && std::io::stdout().is_terminal()
    }

    pub fn diag_dir() -> PathBuf {
        let dir = std::env::var_os(DIAG_ENV)
            .map(PathBuf::from)
            .unwrap_or_else(std::env::temp_dir);
        std::fs::create_dir_all(&dir).expect("建立診斷目錄");
        dir
    }

    /// 逐列附加並立即 flush；panic 前已寫入的列不會遺失。
    pub fn diag(path: &Path, line: &str) {
        let mut f = std::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(path)
            .expect("開啟診斷檔");
        writeln!(f, "{line}").expect("寫入診斷檔");
        f.flush().expect("flush 診斷檔");
    }

    // 必須帶 `--ignored`：本測試為 #[ignore]，缺此旗標時子 libtest 直接略過並以 0 結束（OD-29 W 空跑根因）。
    pub fn spawn_self_in_new_console_group(test: &str, ready: &Path) -> Child {
        Command::new(std::env::current_exe().unwrap())
            .args(["--ignored", "--exact", test, "--nocapture"])
            .env(CHILD_ENV, "1")
            .env(READY_ENV, ready)
            .creation_flags(CREATE_NEW_PROCESS_GROUP)
            .spawn()
            .unwrap()
    }

    /// CREATE_NEW_PROCESS_GROUP 會代子行程隱式呼叫 `SetConsoleCtrlHandler(NULL, TRUE)`（停用 Ctrl+C，且由其子孫繼承）；
    /// 子行程在委派前重新啟用，孫行程才會收到送往本 process group 的 CTRL_C_EVENT。
    pub fn reenable_ctrl_c() -> bool {
        // SAFETY: 僅以 NULL handler 與整數旗標呼叫 Win32 API；失敗以回傳值判定。
        unsafe { windows_sys::Win32::System::Console::SetConsoleCtrlHandler(None, 0) != 0 }
    }

    pub fn send_console_ctrl(event: u32, group_id: u32) -> bool {
        // SAFETY: 僅傳遞整數參數給 Win32 API；失敗以回傳值判定。
        unsafe { windows_sys::Win32::System::Console::GenerateConsoleCtrlEvent(event, group_id) != 0 }
    }

    pub fn wait_for_file(path: &Path, child: &mut Child, limit: Duration) -> Result<String, String> {
        let t0 = Instant::now();
        loop {
            if let Ok(text) = std::fs::read_to_string(path) {
                if !text.trim().is_empty() {
                    return Ok(text.trim().to_string());
                }
            }
            if let Some(st) = child.try_wait().unwrap() {
                return Err(format!("子行程在寫出 ready 標記前已結束：{st:?}"));
            }
            if t0.elapsed() >= limit {
                return Err(format!("{limit:?} 內未出現 ready 標記"));
            }
            std::thread::sleep(Duration::from_millis(50));
        }
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
    const TEST: &str = "cap_interactive_desktop__exec_or_wait_ctrl_c";
    let probe = sw_pal::native_client();
    let attached = win_ctrl::interactive_console_attached();
    if std::env::var_os(win_ctrl::CHILD_ENV).is_some() {
        // 子：先重新啟用 Ctrl+C，寫出 ready 標記（證明確實進入子分支），再委派到會等待 30 s 的程式；
        // Ctrl 事件應終止孫行程，並由 exec_or_wait 照傳 exit code。
        let ready = std::path::PathBuf::from(std::env::var_os(win_ctrl::READY_ENV).expect("缺少 READY 環境變數"));
        let reenabled = win_ctrl::reenable_ctrl_c();
        win_ctrl::diag(
            &ready,
            &format!("child pid={} interactive={attached} ctrl_c_reenabled={reenabled}", std::process::id()),
        );
        assert!(reenabled, "子行程重新啟用 Ctrl+C 失敗");
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
    let dir = win_ctrl::diag_dir();
    let parent_diag = dir.join("ctrl_c-parent.txt");
    let _ = std::fs::remove_file(&parent_diag);
    win_ctrl::diag(
        &parent_diag,
        &format!("parent pid={} interactive={attached} exe={}", std::process::id(), std::env::current_exe().unwrap().display()),
    );
    assert!(
        probe.caps.path_flavor == sw_pal::PalPathFlavor::Windows && attached,
        "cap:interactive_desktop 需要 Windows 互動 console（證據不足，非 PASS）"
    );
    for (event, name) in [(win_ctrl::CTRL_C_EVENT, "ctrl_c"), (win_ctrl::CTRL_BREAK_EVENT, "ctrl_break")] {
        let ready = dir.join(format!("ctrl_c-child-{name}.txt"));
        let _ = std::fs::remove_file(&ready);
        let mut child = win_ctrl::spawn_self_in_new_console_group(TEST, &ready);
        win_ctrl::diag(&parent_diag, &format!("{name}: spawned child pid={}", child.id()));
        // ready 標記未出現即 FAIL：防止子行程未進入子分支（例如漏 `--ignored` 被略過）時空跑。
        let marker = match win_ctrl::wait_for_file(&ready, &mut child, std::time::Duration::from_secs(10)) {
            Ok(marker) => marker,
            Err(reason) => {
                let _ = child.kill();
                win_ctrl::diag(&parent_diag, &format!("{name}: FAIL {reason}"));
                panic!("{name}: {reason}");
            }
        };
        win_ctrl::diag(&parent_diag, &format!("{name}: ready {marker}"));
        // 標記寫在委派之前：再等 2 s 讓孫行程建立。
        std::thread::sleep(std::time::Duration::from_secs(2));
        let sent = win_ctrl::send_console_ctrl(event, child.id());
        win_ctrl::diag(&parent_diag, &format!("{name}: GenerateConsoleCtrlEvent({event}, {}) ok={sent}", child.id()));
        assert!(sent, "{name}: GenerateConsoleCtrlEvent 失敗");
        let st = win_ctrl::wait_with_timeout(&mut child, std::time::Duration::from_secs(10));
        let code = st.and_then(|st| st.code()).map(|c| c as u32);
        win_ctrl::diag(&parent_diag, &format!("{name}: exit={st:?} code={code:x?}"));
        if st.is_none() {
            let _ = child.kill();
            panic!("{name}: 10 s 內未結束");
        }
        assert_eq!(code, Some(0xC000_013A), "event={event}");
    }
    win_ctrl::diag(&parent_diag, "PASS");
}

// 非 Windows：明確失敗（證據不足），不得空跑成 PASS。
#[cfg(not(windows))]
#[test]
#[ignore = "cap:interactive_desktop"]
#[allow(non_snake_case)] // 依 P3 規範：cap_<resource>__<name> 命名
fn cap_interactive_desktop__exec_or_wait_ctrl_c() {
    panic!("cap:interactive_desktop 需要 Windows 互動 console（證據不足，非 PASS）");
}

// Windows 委派的 Job 不變式（測試輔助）：
// - 委派父行程被 TerminateProcess 時，launcher 與其所有孫行程必須一併結束（終止傳遞）。
// - 委派子行程正常結束時，背景啟動的孫行程（daemon start 類）必須照常存活。
// - 孫行程不得以 CREATE_BREAKAWAY_FROM_JOB 脫離 Job。
// 各案例以本測試執行檔自我重入（ROLE 環境變數）實作 delegate → launcher → grandchild 三層。
#[cfg(windows)]
mod win_tree {
    use std::path::Path;
    use std::process::{Child, Command, Stdio};
    use std::time::{Duration, Instant};

    use windows_sys::Win32::Foundation::{CloseHandle, WAIT_TIMEOUT};
    use windows_sys::Win32::System::Threading::{
        OpenProcess, TerminateProcess, WaitForSingleObject, PROCESS_QUERY_LIMITED_INFORMATION,
        PROCESS_SYNCHRONIZE, PROCESS_TERMINATE,
    };

    pub const ROLE_ENV: &str = "SW_PAL_TREE_ROLE";
    pub const MODE_ENV: &str = "SW_PAL_TREE_MODE";
    pub const RESULT_ENV: &str = "SW_PAL_TREE_RESULT";
    pub const CREATE_BREAKAWAY_FROM_JOB: u32 = 0x0100_0000;
    // DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP，與 sw_core/cli.py `daemon start` 的 Popen 旗標相同。
    pub const DETACHED_NEW_GROUP: u32 = 0x0000_0008 | 0x0000_0200;

    pub fn role() -> Option<String> {
        std::env::var(ROLE_ENV).ok()
    }

    pub fn mode() -> String {
        std::env::var(MODE_ENV).expect("缺少 MODE 環境變數")
    }

    pub fn result_path() -> std::path::PathBuf {
        std::path::PathBuf::from(std::env::var_os(RESULT_ENV).expect("缺少 RESULT 環境變數"))
    }

    // 以暫存檔再 rename 發布結果，避免讀取端看到半寫入內容。
    pub fn publish(path: &Path, text: &str) {
        let tmp = path.with_extension("tmp");
        std::fs::write(&tmp, text).expect("寫入結果暫存檔");
        std::fs::rename(&tmp, path).expect("發布結果檔");
    }

    pub fn wait_for_result(path: &Path, delegate: &mut Child, limit: Duration) -> String {
        let deadline = Instant::now() + limit;
        loop {
            if let Ok(text) = std::fs::read_to_string(path) {
                if !text.trim().is_empty() {
                    return text.trim().to_string();
                }
            }
            if let Some(status) = delegate.try_wait().expect("檢查委派行程狀態") {
                panic!("委派行程在寫出結果前結束：{status}");
            }
            assert!(Instant::now() < deadline, "未在期限內寫出結果");
            std::thread::sleep(Duration::from_millis(100));
        }
    }

    // 以 pid 查詢行程是否仍存活；無法開啟視為已不存在（pid 由本測試新建，無重用疑慮）。
    pub fn is_alive(pid: u32) -> bool {
        // SAFETY: 只傳遞整數參數；NULL 以 `is_null` 判定，成功時於同一作用域關閉 handle。
        unsafe {
            let handle = OpenProcess(PROCESS_SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION, 0, pid);
            if handle.is_null() {
                return false;
            }
            let alive = WaitForSingleObject(handle, 0) == WAIT_TIMEOUT;
            CloseHandle(handle);
            alive
        }
    }

    pub fn alive_within(pid: u32, limit: Duration) -> bool {
        let deadline = Instant::now() + limit;
        while Instant::now() < deadline {
            if !is_alive(pid) {
                return false;
            }
            std::thread::sleep(Duration::from_millis(50));
        }
        is_alive(pid)
    }

    // 測試清理：失敗路徑不讓孫行程殘留。
    pub fn force_terminate(pid: u32) {
        // SAFETY: 只傳遞整數參數；handle 於呼叫後立即關閉。
        unsafe {
            let handle = OpenProcess(PROCESS_TERMINATE, 0, pid);
            if !handle.is_null() {
                TerminateProcess(handle, 1);
                CloseHandle(handle);
            }
        }
    }

    pub fn spawn_self(entry: &str, role: &str, mode: &str, result: &Path, breakaway_or_flags: u32) -> std::io::Result<Child> {
        use std::os::windows::process::CommandExt;

        let mut command = Command::new(std::env::current_exe()?);
        command
            .args(["--exact", entry, "--nocapture", "--ignored"])
            .env(ROLE_ENV, role)
            .env(MODE_ENV, mode)
            .env(RESULT_ENV, result)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null());
        if breakaway_or_flags != 0 {
            command.creation_flags(breakaway_or_flags);
        }
        command.spawn()
    }
}

// 委派 entry（父行程）：以 exec_or_wait 執行 launcher；launcher 依 MODE 啟動孫行程。
#[cfg(windows)]
#[test]
#[ignore = "委派 entry：僅由父測試以 --exact --ignored 重入，不得作為獨立案例（否則空跑即 PASS）"]
fn exec_or_wait_tree_delegate_entry() {
    if win_tree::role().as_deref() != Some("delegate") {
        return;
    }
    let launcher = vec![
        std::env::current_exe().expect("取得測試執行檔").into_os_string(),
        "--exact".into(),
        "exec_or_wait_tree_launcher_entry".into(),
        "--nocapture".into(),
        "--ignored".into(),
    ];
    let env_add = [
        (win_tree::ROLE_ENV.to_string(), "launcher".to_string()),
        (win_tree::MODE_ENV.to_string(), win_tree::mode()),
        (
            win_tree::RESULT_ENV.to_string(),
            win_tree::result_path().to_string_lossy().into_owned(),
        ),
    ];
    let e = sw_pal::native_client().process.exec_or_wait(&launcher, &env_add);
    panic!("exec_or_wait 返回錯誤：{e:?}");
}

// launcher：依 MODE 啟動孫行程並發布結果。
// - terminate／detached：一般或 DETACHED 旗標啟動孫行程，發布 `spawned <pid>`；terminate 等待孫行程結束。
// - breakaway：請求 CREATE_BREAKAWAY_FROM_JOB，發布 `spawned <pid>` 或 `denied`（不得成功脫離）。
#[cfg(windows)]
#[test]
#[ignore = "委派 entry：僅由父測試以 --exact --ignored 重入，不得作為獨立案例（否則空跑即 PASS）"]
fn exec_or_wait_tree_launcher_entry() {
    if win_tree::role().as_deref() != Some("launcher") {
        return;
    }
    let result = win_tree::result_path();
    let flags = match win_tree::mode().as_str() {
        "detached" => win_tree::DETACHED_NEW_GROUP,
        "breakaway" => win_tree::CREATE_BREAKAWAY_FROM_JOB,
        _ => 0,
    };
    let spawned = win_tree::spawn_self(
        "exec_or_wait_tree_grandchild_entry",
        "grandchild",
        "",
        &result,
        flags,
    );
    match spawned {
        Ok(mut grandchild) => {
            win_tree::publish(&result, &format!("spawned {}", grandchild.id()));
            if win_tree::mode() == "terminate" {
                let _ = grandchild.wait();
            }
        }
        Err(_) => win_tree::publish(&result, "denied"),
    }
}

#[cfg(windows)]
#[test]
#[ignore = "委派 entry：僅由父測試以 --exact --ignored 重入，不得作為獨立案例（否則空跑即 PASS）"]
fn exec_or_wait_tree_grandchild_entry() {
    if win_tree::role().as_deref() != Some("grandchild") {
        return;
    }
    // 長時間等待；父測試於結果驗證後以 pid 清理，失敗路徑不殘留。
    std::thread::sleep(std::time::Duration::from_secs(120));
}

#[cfg(windows)]
fn parse_spawned(result: &str) -> u32 {
    result
        .strip_prefix("spawned ")
        .and_then(|pid| pid.trim().parse().ok())
        .unwrap_or_else(|| panic!("launcher 結果非預期：{result:?}"))
}

// 終止傳遞：委派父行程被終止後，預設旗標的孫行程必須在期限內結束。
#[cfg(windows)]
#[test]
fn exec_or_wait_terminate_kills_default_grandchild() {
    if win_tree::role().is_some() {
        return;
    }
    let dir = tempfile::tempdir().expect("建立暫存目錄");
    let result = dir.path().join("result.txt");
    let mut delegate = win_tree::spawn_self(
        "exec_or_wait_tree_delegate_entry",
        "delegate",
        "terminate",
        &result,
        0,
    )
    .expect("啟動委派父行程");
    let grandchild = parse_spawned(&win_tree::wait_for_result(
        &result,
        &mut delegate,
        std::time::Duration::from_secs(30),
    ));
    delegate.kill().expect("終止委派父行程");
    let _ = delegate.wait();
    let alive = win_tree::alive_within(grandchild, std::time::Duration::from_secs(5));
    if alive {
        win_tree::force_terminate(grandchild);
    }
    assert!(
        !alive,
        "委派父行程終止後孫行程 {grandchild} 仍存活（孫行程未受 kill-on-close Job 約束）"
    );
}

// 正常結束保留：背景啟動的孫行程（daemon start 類，DETACHED|NEW_PGRP）在委派正常結束後仍存活。
#[cfg(windows)]
#[test]
fn exec_or_wait_normal_exit_keeps_detached_grandchild() {
    if win_tree::role().is_some() {
        return;
    }
    let dir = tempfile::tempdir().expect("建立暫存目錄");
    let result = dir.path().join("result.txt");
    let mut delegate = win_tree::spawn_self(
        "exec_or_wait_tree_delegate_entry",
        "delegate",
        "detached",
        &result,
        0,
    )
    .expect("啟動委派父行程");
    let grandchild = parse_spawned(&win_tree::wait_for_result(
        &result,
        &mut delegate,
        std::time::Duration::from_secs(30),
    ));
    let status = delegate.wait().expect("等待委派父行程正常結束");
    assert_eq!(status.code(), Some(0), "委派父行程應以 launcher 結束碼 0 正常結束");
    let alive = win_tree::alive_within(grandchild, std::time::Duration::from_secs(2));
    if alive {
        win_tree::force_terminate(grandchild);
    }
    assert!(
        alive,
        "委派正常結束後背景孫行程 {grandchild} 已不存在（daemon start 類行程必須保留）"
    );
}

// 不得脫離 Job：孫行程請求 CREATE_BREAKAWAY_FROM_JOB 必須被拒絕，不得成功啟動於 Job 之外。
#[cfg(windows)]
#[test]
fn exec_or_wait_refuses_breakaway_grandchild() {
    if win_tree::role().is_some() {
        return;
    }
    let dir = tempfile::tempdir().expect("建立暫存目錄");
    let result = dir.path().join("result.txt");
    let mut delegate = win_tree::spawn_self(
        "exec_or_wait_tree_delegate_entry",
        "delegate",
        "breakaway",
        &result,
        0,
    )
    .expect("啟動委派父行程");
    let outcome = win_tree::wait_for_result(&result, &mut delegate, std::time::Duration::from_secs(30));
    let _ = delegate.wait();
    if let Some(pid) = outcome.strip_prefix("spawned ").and_then(|p| p.trim().parse::<u32>().ok()) {
        win_tree::force_terminate(pid);
        panic!("breakaway 孫行程 {pid} 成功啟動（應被 Job 拒絕）");
    }
    assert_eq!(outcome, "denied", "breakaway 結果非預期");
}
