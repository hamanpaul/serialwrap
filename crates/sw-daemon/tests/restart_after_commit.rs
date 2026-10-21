//! P5-T19 Review Focus 1：release 的 `state.json` 提交前後被強制中止（`abort`）後的重啟語意（§6.2 交接）。
//!
//! 子行程模式：測試函式以 `S21_CHILD` 辨識，改以 `run_with_hooks` 執行 daemon 並於 commit hook 中 `abort`。
//! 紅燈階段：`run_with_hooks` 為 `unimplemented!`，子行程以 panic 結束（非 abort），故案例於執行期失敗。
#![cfg(feature = "test-fakes")]

mod support;

use std::fs;
use std::process::Command;
use std::time::Duration;

use support::*;

/// commit 後、回覆前被 kill：重啟後必須仍是 RELEASED、不自動 attach、不開 tty。
#[test]
fn kill_between_commit_and_reply_keeps_released_after_restart() {
    if std::env::var("S21_CHILD").as_deref() == Ok("1") {
        child_daemon_abort_after_commit();
        return;
    }
    let env = isolated_env_with_pty_target();
    let mut child = Command::new(std::env::current_exe().unwrap())
        .args(["--exact", "kill_between_commit_and_reply_keeps_released_after_restart", "--nocapture"])
        .env("S21_CHILD", "1")
        .envs(&env.vars)
        .spawn()
        .unwrap();
    wait_ready(&env, "COM0");
    let r = rpc_once(
        &env.socket,
        r#"{"id":1,"method":"device.release","params":{"selector":"COM0"}}"#,
    );
    assert!(r.is_empty(), "reply must not be sent: process aborts right after commit");
    assert!(!child.wait().unwrap().success());

    let mut d2 = DaemonProc::spawn(&env, &[]);
    wait_socket(&env.socket, Duration::from_secs(10));
    let s = get_state(&env.socket, "COM0");
    assert_eq!(s["state"], "RELEASED");
    assert!(
        pty_can_open_exclusively(&env.pty_slave),
        "daemon must not reopen the released tty"
    );
    rpc_once(&env.socket, STOP);
    assert_eq!(d2.wait(LONG).code(), Some(0));
}

/// commit 前被 kill：重啟後依最後有效的 state.json 恢復，該 session 不是 RELEASED，release 從未被確認。
#[test]
fn kill_before_commit_restores_previous_state() {
    if std::env::var("S21_CHILD").as_deref() == Ok("before") {
        child_daemon_abort_before_commit();
        return;
    }
    let env = isolated_env_with_pty_target();
    let mut child = Command::new(std::env::current_exe().unwrap())
        .args(["--exact", "kill_before_commit_restores_previous_state", "--nocapture"])
        .env("S21_CHILD", "before")
        .envs(&env.vars)
        .spawn()
        .unwrap();
    wait_ready(&env, "COM0");
    let state_path = env.state_dir.join("state.json");
    let state_before = fs::read(&state_path).ok();
    let r = rpc_once(
        &env.socket,
        r#"{"id":1,"method":"device.release","params":{"selector":"COM0"}}"#,
    );
    assert!(r.is_empty(), "release 未提交前中止：不得回覆成功");
    assert!(!child.wait().unwrap().success());

    // 重啟：state.json 應為 release 前的內容（該 session 不在 released 內）。
    let state_after = fs::read(&state_path).ok();
    assert_eq!(state_after, state_before, "提交前中止不得改寫 state.json");
    let mut d2 = DaemonProc::spawn(&env, &[]);
    wait_socket(&env.socket, Duration::from_secs(10));
    let s = get_state(&env.socket, "COM0");
    assert!(s["state"] != "RELEASED", "release 從未被確認，session 不得為 RELEASED：{s:?}");
    assert!(
        matches!(s["state"].as_str(), Some("ATTACHING") | Some("ATTACHED") | Some("READY")),
        "依裝置存在重新 attach：{s:?}"
    );
    rpc_once(&env.socket, STOP);
    assert_eq!(d2.wait(LONG).code(), Some(0));
}
