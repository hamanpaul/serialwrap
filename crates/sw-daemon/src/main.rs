//! `serialwrapd` 入口（P4-T17；P5-T0 起分派；P5-T19 承接 daemon 本體）。
//!
//! release manifest 存在時交 launcher（P4 既有路徑，含 `implementation` 的既有拒絕與錯誤碼）；
//! manifest 不存在（開發 build）時交 Rust daemon 本體 `daemon_main::run`：
//! 1. 建立任何 thread 之前先 `signals.install()`（POSIX 封鎖 SIGINT／SIGTERM 交專屬 sigwait thread；Task 4）。
//!    launcher 路徑不安裝：訊號遮罩會經 exec 繼承給 Python daemon。
//! 2. panic hook 只把訊息與位置寫到 stderr（不 exit），讓 framing／actor／reader 的 `catch_unwind` 能各自隔離。
//! 3. `daemon_main::run` 內以 `catch_unwind` 包住主流程：未攔截的 panic → exit 1（arch §6.1）。
#![windows_subsystem = "windows"]

fn main() {
    let argv: Vec<std::ffi::OsString> = std::env::args_os().collect();
    let p = sw_pal::native_client();
    if !sw_daemon::launcher::manifest_absent(&p) {
        std::process::exit(sw_daemon::launcher::run(argv, &p));
    }
    if let Err(e) = p.signals.install() {
        eprintln!("{}", e.py_message);
        std::process::exit(1);
    }
    std::panic::set_hook(Box::new(|info| {
        let msg = if let Some(s) = info.payload().downcast_ref::<&str>() {
            (*s).to_string()
        } else if let Some(s) = info.payload().downcast_ref::<String>() {
            s.clone()
        } else {
            "Box<dyn Any>".to_string()
        };
        let location = info
            .location()
            .map(|l| format!("{}:{}", l.file(), l.line()))
            .unwrap_or_default();
        let thread = std::thread::current();
        let name = thread.name().unwrap_or("<unnamed>");
        eprintln!("serialwrapd: panic in thread '{name}' at {location}: {msg}");
    }));
    let args: Vec<String> = argv
        .iter()
        .map(|arg| arg.to_string_lossy().into_owned())
        .collect();
    std::process::exit(sw_daemon::daemon_main::run(args, p));
}
