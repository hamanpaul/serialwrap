//! `cmd submit --expect-reboot` 的 Python／Rust CLI 黑箱對照。

mod common;

use std::process::Command;

#[test]
fn interactive_rejection_matches_python_cli_byte_for_byte() {
    let root = common::data_root();
    let args = [
        "--socket",
        "/tmp/serialwrap-s1m242-never-connect.sock",
        "cmd",
        "submit",
        "--selector",
        "COM0",
        "--cmd",
        "firstboot -y && reboot",
        "--mode",
        "interactive",
        "--expect-reboot",
    ];
    let rust = Command::new(env!("CARGO_BIN_EXE_serialwrap"))
        .args(args)
        .current_dir(&root)
        .env("COLUMNS", "80")
        .output()
        .expect("Rust CLI 執行失敗");
    let python = Command::new("python3")
        .args(["-m", "sw_core.cli"])
        .args(args)
        .current_dir(&root)
        .env("COLUMNS", "80")
        .output()
        .expect("Python CLI 執行失敗");

    let expected = concat!(
        "{\"error_code\":\"EXPECT_REBOOT_MODE_UNSUPPORTED\",",
        "\"hint\":\"--expect-reboot 只適用 line／background 模式；",
        "interactive 是 lease 按鍵通道，不走 reboot 路徑。\",",
        "\"ok\":false}\n"
    );
    assert_eq!(python.status.code(), Some(1));
    assert_eq!(python.stdout, expected.as_bytes());
    assert!(python.stderr.is_empty());
    assert_eq!(rust.status.code(), python.status.code());
    assert_eq!(rust.stdout, python.stdout);
    assert_eq!(rust.stderr, python.stderr);
}
