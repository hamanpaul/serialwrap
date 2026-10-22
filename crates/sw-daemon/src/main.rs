//! `serialwrapd` 入口（P4-T17；P5-T0 起分派）。
//!
//! release manifest 存在時交 launcher（P4 既有路徑，含 `implementation` 的既有拒絕與錯誤碼）；
//! manifest 不存在（開發 build）時交 Rust daemon 本體 `daemon_main::run`（Task 19 實作前為 `s21_todo!` 紅燈）。
#![windows_subsystem = "windows"]

fn main() {
    let argv: Vec<std::ffi::OsString> = std::env::args_os().collect();
    let p = sw_pal::native_client();
    if !sw_daemon::launcher::manifest_absent(&p) {
        std::process::exit(sw_daemon::launcher::run(argv, &p));
    }
    let args: Vec<String> = argv
        .iter()
        .map(|arg| arg.to_string_lossy().into_owned())
        .collect();
    std::process::exit(sw_daemon::daemon_main::run(args, p));
}
