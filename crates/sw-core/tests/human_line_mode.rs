//! P6 Task 3：human line mode 判定（對照 `tests/test_service_human_console.py` 與 `service.py:89-142`；07 Task 3 Step 1）。
#![allow(dead_code)]

mod common;
use common::golden::{rows, unhex_utf8};
use sw_core::human_console::line_mode::{human_console_mode, HumanLineMode};

#[test] fn vim_is_interactive() { assert_eq!(human_console_mode("vim /etc/config"), HumanLineMode::Interactive); }
#[test] fn ls_is_line() { assert_eq!(human_console_mode("ls -la"), HumanLineMode::Line); }
#[test] fn sudo_vim_is_interactive() { assert_eq!(human_console_mode("sudo vim x"), HumanLineMode::Interactive); }
#[test] fn sudo_value_opt_skipped() { assert_eq!(human_console_mode("sudo -u root top"), HumanLineMode::Interactive); }
#[test] fn env_assignments_skipped() { assert_eq!(human_console_mode("FOO=1 BAR=2 less x"), HumanLineMode::Interactive); }
#[test] fn env_cmd_skipped() { assert_eq!(human_console_mode("env -i A=1 htop"), HumanLineMode::Interactive); }
#[test] fn exec_prefix_skipped() { assert_eq!(human_console_mode("exec watch date"), HumanLineMode::Interactive); }
#[test] fn double_dash_skipped() { assert_eq!(human_console_mode("-- top"), HumanLineMode::Interactive); }
#[test] fn basename_used() { assert_eq!(human_console_mode("/usr/bin/vi f"), HumanLineMode::Interactive); }
#[test] fn unclosed_quote_falls_back_to_split() { assert_eq!(human_console_mode("vim 'unterminated"), HumanLineMode::Interactive); }
#[test] fn empty_is_line() { assert_eq!(human_console_mode("   "), HumanLineMode::Line); }
#[test] fn as_str_matches_python() {
    assert_eq!(HumanLineMode::Line.as_str(), "line");
    assert_eq!(HumanLineMode::Interactive.as_str(), "interactive");
}
#[test] fn golden_matches_python() {
    // contracts/golden/human_console_mode.tsv：op "line_mode"；input＝命令 UTF-8 hex；expected＝"line"／"interactive" 的 hex。
    let mut n = 0;
    for r in rows(include_str!("../../../contracts/golden/human_console_mode.tsv")) {
        assert_eq!(r.op, "line_mode");
        let cmd = unhex_utf8(&r.input);
        assert_eq!(human_console_mode(&cmd).as_str(), unhex_utf8(&r.expected), "case {} cmd={cmd:?}", r.id);
        n += 1;
    }
    assert!(n >= 300, "golden 案例數 {n}");
}
