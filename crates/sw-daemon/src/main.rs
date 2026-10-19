//! `serialwrapd` 入口（P4-T17）：Stage 1 由 launcher 承接（`sw_daemon::launcher::run`）。

fn main() {
    let argv: Vec<std::ffi::OsString> = std::env::args_os().collect();
    let p = sw_pal::native_client();
    std::process::exit(sw_daemon::launcher::run(argv, &p));
}
