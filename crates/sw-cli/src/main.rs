//! `serialwrap` 執行檔入口（P3-T1 骨架；P3-T14 起接 `run_process`）：平台選擇只在此組裝點呼叫 `sw_pal::native_client()`。

fn main() {
    // 內部 panic 由 `run_process` 統一改寫為 `serialwrap: internal error: <訊息>`（exit 1），不讓 Rust 預設訊息外洩。
    std::panic::set_hook(Box::new(|_| {}));
    let p = sw_pal::native_client();
    let code = sw_cli::run_process(std::env::args_os().collect(), &p);
    std::process::exit(code);
}
