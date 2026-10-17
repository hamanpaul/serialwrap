//! `ProcessTunnel`（BC-05）承接測試（P4-T5）：無 pipe 分離行程、PID 重用身分核對、整棵樹終止。
//!
//! POSIX 測試逐項標 `#[cfg(unix)]`，Windows 測試逐項標 `#[cfg(windows)]`（不使用檔案級 `#![cfg(unix)]`，
//! 以免排除表內的 Windows 測試）。Windows 測試由 `scripts/rust/win-test.sh --package sw-pal -- process_tunnel` 於原生 Windows 執行（arch §9.4）。
use std::time::{Duration, Instant};

use sw_pal::process_tunnel::{ProcGroupRef, TunnelProc};

/// 每 20 ms 輪詢條件，直到成立或逾時；回傳最後一次判定結果。
fn wait_until(limit: Duration, mut cond: impl FnMut() -> bool) -> bool {
    let deadline = Instant::now() + limit;
    loop {
        if cond() {
            return true;
        }
        if Instant::now() >= deadline {
            return false;
        }
        std::thread::sleep(Duration::from_millis(20));
    }
}

/// 測試用「自身行程」的群組參考：POSIX 為目前 process group。
#[cfg(unix)]
fn self_group() -> ProcGroupRef {
    // SAFETY: getpgid(0) 只讀取目前行程的 process group id。
    ProcGroupRef::Pgid(unsafe { libc::getpgid(0) })
}

/// 測試用「自身行程」的群組參考：Windows 為整棵樹模式。
#[cfg(windows)]
fn self_group() -> ProcGroupRef {
    ProcGroupRef::WinTree
}

#[cfg(unix)]
#[test]
fn posix_terminate_kills_whole_group_including_grandchild() {
    let tmp = tempfile::tempdir().unwrap();
    let t = sw_pal::native().tunnel;
    let p = t
        .spawn_detached(
            &["sh".into(), "-c".into(), "sleep 300 & sleep 300; wait".into()],
            &tmp.path().join("t.log"),
        )
        .unwrap();
    std::thread::sleep(Duration::from_millis(200));
    let start = Instant::now();
    t.terminate(&p, Duration::from_secs(5)).unwrap();
    assert!(start.elapsed() < Duration::from_secs(6));
    assert!(!t.alive(&p));
    let ProcGroupRef::Pgid(g) = p.group else {
        panic!("posix 必為 Pgid");
    };
    // SAFETY: killpg(pgid, 0) 只探測群組是否存在，不送任何訊號。
    assert_eq!(unsafe { libc::killpg(g, 0) }, -1, "群組內不得殘留行程");
}

#[test]
fn stderr_tail_returns_last_500_bytes_decoded() {
    let tmp = tempfile::tempdir().unwrap();
    let log = tmp.path().join("x.log");
    std::fs::write(&log, [b"a".repeat(600), "錯誤".as_bytes().to_vec()].concat()).unwrap();
    let tail = sw_pal::native().tunnel.stderr_tail(&log, 500);
    assert!(tail.ends_with("錯誤") && tail.len() <= 500);
}

#[cfg(unix)]
#[test]
fn spawn_detached_no_pipes_stdout_to_log() {
    let tmp = tempfile::tempdir().unwrap();
    let log = tmp.path().join("hi.log");
    let t = sw_pal::native().tunnel;
    let p = t
        .spawn_detached(&["sh".into(), "-c".into(), "echo hi; sleep 30".into()], &log)
        .unwrap();
    let written = wait_until(Duration::from_secs(5), || {
        std::fs::read(&log).map(|b| b == b"hi\n").unwrap_or(false)
    });
    assert!(written, "stdout 應寫入 log，實際內容：{:?}", std::fs::read(&log).ok());
    // stdin 為 NUL：子行程 fd 0 必須指向 /dev/null，且不得是 pipe。
    let fd0 = std::fs::read_link(format!("/proc/{}/fd/0", p.pid)).unwrap();
    assert_eq!(fd0, std::path::PathBuf::from("/dev/null"), "stdin 應為 NUL（/dev/null）");
    t.terminate(&p, Duration::from_secs(5)).unwrap();
}

#[test]
fn alive_false_after_pid_reuse_ticks_mismatch() {
    let p = TunnelProc {
        pid: std::process::id(),
        group: self_group(),
        start_ticks: Some(1),
    };
    assert!(
        !sw_pal::native().tunnel.alive(&p),
        "start_ticks 不符時必須視為 PID 已重用（原行程已結束）"
    );
}

#[test]
fn supports_control_master_by_platform() {
    assert_eq!(
        sw_pal::native().tunnel.supports_control_master(),
        cfg!(unix),
        "POSIX 為 true；原生 Windows 為 false"
    );
}

/// Toolhelp32 快照輔助（僅 Windows 測試使用；屬測試程式，不是受測實作）。
#[cfg(windows)]
mod win32_tree {
    use windows_sys::Win32::Foundation::{CloseHandle, INVALID_HANDLE_VALUE};
    use windows_sys::Win32::System::Diagnostics::ToolHelp::{
        CreateToolhelp32Snapshot, Process32FirstW, Process32NextW, PROCESSENTRY32W,
        TH32CS_SNAPPROCESS,
    };

    /// 以 Toolhelp32 列出目前所有行程的 `(pid, parent_pid)`。
    pub fn snapshot() -> Vec<(u32, u32)> {
        // SAFETY: 快照句柄於回傳前關閉；PROCESSENTRY32W 以 dwSize 初始化後才交給系統填寫。
        unsafe {
            let snap = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
            assert_ne!(snap, INVALID_HANDLE_VALUE, "CreateToolhelp32Snapshot 失敗");
            let mut entry = PROCESSENTRY32W {
                dwSize: std::mem::size_of::<PROCESSENTRY32W>() as u32,
                ..Default::default()
            };
            let mut rows = Vec::new();
            if Process32FirstW(snap, &mut entry) != 0 {
                loop {
                    rows.push((entry.th32ProcessID, entry.th32ParentProcessID));
                    if Process32NextW(snap, &mut entry) == 0 {
                        break;
                    }
                }
            }
            CloseHandle(snap);
            rows
        }
    }

    /// `root` 的所有子孫 pid（沿父子關係 BFS）。
    pub fn descendants(root: u32) -> Vec<u32> {
        let rows = snapshot();
        let mut found = Vec::new();
        let mut frontier = vec![root];
        while let Some(parent) = frontier.pop() {
            for &(pid, ppid) in &rows {
                if ppid == parent && pid != parent && !found.contains(&pid) {
                    found.push(pid);
                    frontier.push(pid);
                }
            }
        }
        found
    }
}

#[cfg(windows)]
#[test]
fn windows_terminate_tree_kills_child_and_grandchild() {
    use win32_tree::{descendants, snapshot};

    let tmp = tempfile::tempdir().unwrap();
    let t = sw_pal::native().tunnel;
    let p = t
        .spawn_detached(
            &[
                "cmd".into(),
                "/c".into(),
                "start /b ping -n 300 127.0.0.1 & ping -n 300 127.0.0.1".into(),
            ],
            &tmp.path().join("tree.log"),
        )
        .unwrap();
    // 前置條件：cmd 下方已形成兩個 ping 子孫（start /b 與前景 ping）。
    let formed = wait_until(Duration::from_secs(5), || descendants(p.pid).len() >= 2);
    assert!(formed, "前置條件不成立：cmd 未產生兩個子孫行程");
    let kids = descendants(p.pid);
    t.terminate(&p, Duration::from_secs(5)).unwrap();
    let alive_pids: Vec<u32> = snapshot().iter().map(|(pid, _)| *pid).collect();
    assert!(!alive_pids.contains(&p.pid), "根行程不得殘留");
    for kid in kids {
        assert!(!alive_pids.contains(&kid), "子孫行程 {kid} 不得殘留");
    }
}

#[cfg(windows)]
#[test]
fn windows_alive_never_terminates() {
    let tmp = tempfile::tempdir().unwrap();
    let t = sw_pal::native().tunnel;
    let p = t
        .spawn_detached(
            &["ping".into(), "-n".into(), "300".into(), "127.0.0.1".into()],
            &tmp.path().join("alive.log"),
        )
        .unwrap();
    for i in 0..50 {
        assert!(t.alive(&p), "第 {i} 次 alive 呼叫不得終止或誤判目標行程");
    }
    assert!(t.alive(&p), "50 次 alive 之後目標行程仍必須存活");
    t.terminate(&p, Duration::from_secs(5)).unwrap();
}

#[cfg(windows)]
#[test]
fn windows_terminate_root_pid_reused_not_signalled() {
    let mut helper = std::process::Command::new("ping")
        .args(["-n", "300", "127.0.0.1"])
        .stdout(std::process::Stdio::null())
        .spawn()
        .unwrap();
    // 以真實 helper 的 pid 搭配錯誤的 start_ticks，模擬 PID 已被其他行程重用。
    let stale = TunnelProc {
        pid: helper.id(),
        group: ProcGroupRef::WinTree,
        start_ticks: Some(1),
    };
    let result = sw_pal::native().tunnel.terminate(&stale, Duration::from_secs(5));
    let still_running = helper.try_wait().unwrap().is_none();
    // 清理：無論結果如何都結束 helper，避免測試失敗時殘留行程。
    let _ = helper.kill();
    let _ = helper.wait();
    assert!(result.is_ok(), "身分不符時 terminate 應回 Ok：{result:?}");
    assert!(still_running, "PID 重用情境下 helper 不得被終止");
}

#[cfg(windows)]
#[test]
fn windows_terminate_without_ctrl_break_finishes_fast() {
    let tmp = tempfile::tempdir().unwrap();
    let t = sw_pal::native().tunnel;
    let p = t
        .spawn_detached(
            &["ping".into(), "-n".into(), "300".into(), "127.0.0.1".into()],
            &tmp.path().join("fast.log"),
        )
        .unwrap();
    std::thread::sleep(Duration::from_millis(200));
    let start = Instant::now();
    t.terminate(&p, Duration::from_secs(5)).unwrap();
    let elapsed = start.elapsed();
    assert!(
        elapsed < Duration::from_secs(1),
        "不得白等 grace（Ctrl-Break 送不到獨立 console），實際 {elapsed:?}"
    );
    assert!(!t.alive(&p), "terminate 返回後目標行程必須已結束");
}
