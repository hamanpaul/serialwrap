//! P6 Task 3：human line mode 判定（對照 `tests/test_service_human_console.py` 與 `service.py:89-142`；07 Task 3 Step 1）。
#![allow(dead_code)]

mod common;
use common::golden::{rows, unhex_utf8};
use sw_core::human_console::line_mode::{human_console_mode, HumanLineMode};
use sw_pal::PalPathFlavor::{Posix, Windows};

#[test] fn vim_is_interactive() { assert_eq!(human_console_mode("vim /etc/config", Posix), HumanLineMode::Interactive); }
#[test] fn ls_is_line() { assert_eq!(human_console_mode("ls -la", Posix), HumanLineMode::Line); }
#[test] fn sudo_vim_is_interactive() { assert_eq!(human_console_mode("sudo vim x", Posix), HumanLineMode::Interactive); }
#[test] fn sudo_value_opt_skipped() { assert_eq!(human_console_mode("sudo -u root top", Posix), HumanLineMode::Interactive); }
#[test] fn env_assignments_skipped() { assert_eq!(human_console_mode("FOO=1 BAR=2 less x", Posix), HumanLineMode::Interactive); }
#[test] fn env_cmd_skipped() { assert_eq!(human_console_mode("env -i A=1 htop", Posix), HumanLineMode::Interactive); }
#[test] fn exec_prefix_skipped() { assert_eq!(human_console_mode("exec watch date", Posix), HumanLineMode::Interactive); }
#[test] fn double_dash_skipped() { assert_eq!(human_console_mode("-- top", Posix), HumanLineMode::Interactive); }
#[test] fn basename_used() { assert_eq!(human_console_mode("/usr/bin/vi f", Posix), HumanLineMode::Interactive); }
#[test] fn unclosed_quote_falls_back_to_split() { assert_eq!(human_console_mode("vim 'unterminated", Posix), HumanLineMode::Interactive); }
#[test] fn empty_is_line() { assert_eq!(human_console_mode("   ", Posix), HumanLineMode::Line); }
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
        assert_eq!(human_console_mode(&cmd, Posix).as_str(), unhex_utf8(&r.expected), "case {} cmd={cmd:?}", r.id);
        n += 1;
    }
    assert!(n >= 300, "golden 案例數 {n}");
}

// sol 補審 P6 #2：basename 依 PAL 路徑風格（Windows Python 的 `os.path` 為 `ntpath`：`\\`、`/` 與 drive prefix 皆分隔）。
// 期望值以 Python oracle 實算（`_human_console_mode` 於 `os.path` 換成 `ntpath`／`posixpath`）。
#[test] fn windows_backslash_path_is_interactive() {
    assert_eq!(human_console_mode(r"'C:\bin\vim' file", Windows), HumanLineMode::Interactive);
    assert_eq!(human_console_mode(r"'C:\bin\vim' file", Posix), HumanLineMode::Line);
}
#[test] fn windows_drive_relative_is_interactive() {
    assert_eq!(human_console_mode("'C:vim' file", Windows), HumanLineMode::Interactive);
    assert_eq!(human_console_mode("'C:vim' file", Posix), HumanLineMode::Line);
}
#[test] fn windows_prefix_basename_skips_sudo_and_env() {
    assert_eq!(human_console_mode(r"'C:\Windows\sudo' vim", Windows), HumanLineMode::Interactive);
    assert_eq!(human_console_mode("'D:env' htop", Windows), HumanLineMode::Interactive);
    assert_eq!(human_console_mode(r"'C:\Windows\sudo' vim", Posix), HumanLineMode::Line);
}
#[test] fn windows_forward_slash_still_splits() {
    assert_eq!(human_console_mode("/usr/bin/vi f", Windows), HumanLineMode::Interactive);
}
#[test] fn golden_windows_matches_python_ntpath() {
    // contracts/golden/human_console_mode.windows.tsv：同格式，Python oracle 以 ntpath 取代 os.path。
    let mut n = 0;
    for r in rows(include_str!("../../../contracts/golden/human_console_mode.windows.tsv")) {
        assert_eq!(r.op, "line_mode");
        let cmd = unhex_utf8(&r.input);
        assert_eq!(human_console_mode(&cmd, Windows).as_str(), unhex_utf8(&r.expected), "case {} cmd={cmd:?}", r.id);
        n += 1;
    }
    assert!(n >= 600, "golden 案例數 {n}");
}
