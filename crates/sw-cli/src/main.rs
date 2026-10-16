//! `serialwrap` 執行檔入口（P3-T1 骨架）：平台選擇只在此組裝點呼叫 `sw_pal::native_client()`。

fn main() {
    let p = sw_pal::native_client();
    let code = sw_cli::run_process(std::env::args_os().collect(), &p);
    std::process::exit(code);
}
