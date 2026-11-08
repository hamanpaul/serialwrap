//! `SysEnv` 基本語意（P3-T9）：依 `sysenv` 的 trait 文件註解（SC-09、`vars` 非 UTF-8、COLUMNS 不讀取）。
use sw_pal::native_client;

#[test]
fn var_reads_process_environment() {
    let key = "SW_PAL_T9_VAR_PROBE";
    std::env::set_var(key, "probe-value");
    let got = native_client().env.var(key);
    std::env::remove_var(key);
    assert_eq!(got.as_deref(), Some("probe-value"));
}

#[cfg(unix)]
#[test]
fn vars_survive_non_utf8_values() {
    use std::os::unix::ffi::OsStrExt;
    let key = "SW_PAL_T9_NON_UTF8";
    std::env::set_var(key, std::ffi::OsStr::from_bytes(b"ab\xffcd"));
    let vars = native_client().env.vars();
    std::env::remove_var(key);
    let got = vars
        .iter()
        .find(|(k, _)| k.as_str() == key)
        .map(|(_, v)| v.clone());
    // 與 to_string_lossy 相同：非法位元組轉為 U+FFFD，且不得 panic。
    assert_eq!(got.as_deref(), Some("ab\u{FFFD}cd"));
}

#[cfg(unix)]
fn expected_system_config_dir() -> String {
    "/etc/serialwrap".to_string()
}

#[cfg(windows)]
fn expected_system_config_dir() -> String {
    let pd = std::env::var("ProgramData").unwrap_or_else(|_| "C:\\ProgramData".to_string());
    format!("{pd}\\serialwrap")
}

#[test]
fn system_config_dir_is_platform_constant() {
    assert_eq!(native_client().env.system_config_dir(), expected_system_config_dir());
}

#[test]
fn errno_name_known_code() {
    assert_eq!(native_client().env.errno_name(2), Some("ENOENT"));
}

#[test]
fn pid_matches_current_process() {
    assert_eq!(native_client().env.pid(), std::process::id());
}

#[test]
fn current_exe_matches_std() {
    assert_eq!(
        native_client().env.current_exe().unwrap(),
        std::env::current_exe().unwrap()
    );
}

#[test]
fn stdout_is_terminal_matches_std_io() {
    use std::io::IsTerminal;
    assert_eq!(
        native_client().env.stdout_is_terminal(),
        std::io::stdout().is_terminal()
    );
}

#[test]
fn terminal_columns_ignores_columns_env() {
    use std::io::IsTerminal;
    std::env::set_var("COLUMNS", "17");
    let cols = native_client().env.terminal_columns();
    std::env::remove_var("COLUMNS");
    // 規格：查 stdout fd，不讀 COLUMNS。
    assert_ne!(cols, Some(17), "不得讀取 COLUMNS 環境變數");
    if !std::io::stdout().is_terminal() {
        assert_eq!(cols, None, "stdout 非 TTY 時應為 None");
    }
}

#[cfg(unix)]
#[test]
fn realpath_resolves_symlinks() {
    let d = tempfile::tempdir().unwrap();
    let real = d.path().join("real.txt");
    std::fs::write(&real, b"x").unwrap();
    let link = d.path().join("link.txt");
    std::os::unix::fs::symlink(&real, &link).unwrap();
    assert_eq!(
        native_client().env.realpath(&link).unwrap(),
        std::fs::canonicalize(&link).unwrap()
    );
}
