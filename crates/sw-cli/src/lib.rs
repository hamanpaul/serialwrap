//! `sw-cli`：Stage 1A 資料面 CLI（P3-T1 骨架；P3-T10～P3-T16 承接實作）。
//!
//! 只依賴 `sw-proto`＋`sw-pal`；不得連結 `sw-core`（arch §1.3）。
//! 行為尚未實作的函式本體一律 `todo!("P3-T<n>: <name>")`。
#![allow(unused_variables)]

pub mod argparse;
pub mod assets;
pub mod bench_state;
pub mod cli_spec;
pub mod cli_trace;
pub mod client;
pub mod commands;
pub mod ctx;
pub mod delegate;
pub mod endpoint_resolve;
pub mod output;

use std::ffi::OsString;

/// 主流程（P3-T14）：回傳 exit code；stdout／stderr 由呼叫端提供。
pub fn run(
    argv: Vec<OsString>,
    p: &sw_pal::ClientPlatform,
    out: &mut dyn std::io::Write,
    err: &mut dyn std::io::Write,
) -> i32 {
    todo!("P3-T14: run")
}

/// 組裝點（P3-T14）：建立 stdout_writer(caps)／stderr，以 `catch_unwind` 包住 `run`，最後 flush。
/// - run 期間 stdout 寫入失敗（EPIPE 等）→ 1；run 正常回傳後最終 flush 失敗 → 120（Q-20）。
/// - panic → stderr 一行 `serialwrap: internal error: <panic 訊息>`、回傳 1（不得以 Rust 預設 101 外洩）。
pub fn run_process(argv: Vec<OsString>, p: &sw_pal::ClientPlatform) -> i32 {
    todo!("P3-T14: run_process")
}

/// `cli.py:2457-2473` 逐字（Q-08）：回傳 JSON 邊界指令名稱（若 argv 命中）。
pub fn json_boundary_command(argv: &[String]) -> Option<&'static str> {
    todo!("P3-T14: json_boundary_command")
}

/// 測試入口：以閉包取代 `run`，驗 panic → 1、寫入失敗 → 1、最終 flush 失敗 → 120。
#[doc(hidden)]
pub fn run_process_with(
    f: &dyn Fn(&mut dyn std::io::Write, &mut dyn std::io::Write) -> i32,
    out: Box<dyn std::io::Write>,
    err: Box<dyn std::io::Write>,
) -> i32 {
    todo!("P3-T14: run_process_with")
}
