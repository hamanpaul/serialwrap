//! P7 Task 14（08-slice23-login-events.md）：原生 `HandlerSpawner`（SC-26）。斷言在 `conformance::handler_suite`。
//! Windows 原生執行列 LOCAL-WINDOWS-TODO（`scripts/rust/win-test.sh --package sw-pal --test handler_spawner`，
//! 報告 `passed` 須等於本檔 `#[test]` 數 9、`ignored==0`）。
#![cfg(feature = "test-fakes")]

use sw_pal::conformance::handler_suite as suite;

fn native() -> std::sync::Arc<dyn sw_pal::traits::HandlerSpawner> {
    sw_pal::native().handlers
}

#[test]
fn native_completes() {
    suite::completes_with_stdin_echo_and_tail(&*native());
}

#[test]
fn native_timeout_kills_tree() {
    suite::timeout_kills_whole_tree(&*native());
}

#[test]
fn native_tail_bounded() {
    suite::stdout_tail_is_bounded(&*native());
}

#[test]
fn native_spawn_failure() {
    suite::spawn_failure_message_is_python_like(&*native());
}

#[test]
fn native_env_cleared() {
    suite::env_is_cleared_except_spec(&*native());
}

#[test]
fn native_abort_terminates_tree() {
    suite::abort_terminates_tree(&*native());
}

#[test]
fn native_inherits_only_stdio() {
    suite::handler_inherits_only_stdio(&*native());
}

#[test]
fn native_cmd_shell_quotes() {
    let h = native();
    if h.shell_argv("x")[0] == "cmd.exe" {
        suite::cmd_shell_preserves_inner_quotes(&*h);
    }
}

#[test]
fn shell_argv_and_env_keys_by_platform() {
    let h = native();
    let argv = h.shell_argv("x");
    assert!(argv == ["/bin/sh", "-c", "x"] || argv == ["cmd.exe", "/d", "/s", "/c", "x"]);
    let keys = h.inherited_env_keys();
    assert!(keys.starts_with(&["PATH", "HOME", "USER", "LANG", "LC_ALL"]));
    if argv[0] == "cmd.exe" {
        assert!(keys.ends_with(&[
            "SYSTEMROOT",
            "COMSPEC",
            "PATHEXT",
            "TEMP",
            "TMP",
            "USERPROFILE"
        ]));
    } else {
        assert_eq!(keys.len(), 5);
    }
}
