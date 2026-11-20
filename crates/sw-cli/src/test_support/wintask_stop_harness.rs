//! P8 Task 13：`windows-task` 實跑測試的正式停止路徑 harness（只在 `--features test-fakes` 建置）。
//!
//! 用法：`wintask_stop_harness --task sw-p8-test-<pid> --port <測試埠> --var SERIALWRAP_CONFIG=<path> --var …`
//!
//! 以 `sw_pal::native_client()` 為基礎、經 `sw_cli::wintask_harness::scoped_service_stop` 執行正式的
//! `serialwrap service stop`，stdout 印一行 JSON（`ScopedStopReport::to_json`），exit 0。要求不合法或 harness
//! 自身失敗時 stderr 一行說明、exit 2，不執行任何監管命令。由 sw-pal `tests/wintask_runtime.rs` 呼叫。

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let req = match sw_cli::wintask_harness::parse_harness_args(&args) {
        Ok(req) => req,
        Err(why) => {
            eprintln!("wintask_stop_harness: {why}");
            std::process::exit(2);
        }
    };
    match sw_cli::wintask_harness::scoped_service_stop(sw_pal::native_client(), &req) {
        Ok(report) => {
            println!("{}", report.to_json());
        }
        Err(why) => {
            eprintln!("wintask_stop_harness: {why}");
            std::process::exit(2);
        }
    }
}
