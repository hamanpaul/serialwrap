//! Stage 2 承接；Slice 2.1 逐 Task 建立中（各模組目前為紅燈階段骨架，函式本體為 `s21_todo!`，行為由各 Task 實作）。

/// Slice 2.1 骨架未實作函式的統一 panic（紅燈測試的失敗訊息：`not implemented: slice21 <Task> <symbol>`）。
#[macro_export]
macro_rules! s21_todo {
    ($t:literal, $sym:literal) => {
        unimplemented!(concat!("slice21 ", $t, " ", $sym))
    };
}

/// `Clock::mono_ns()` 的單位（納秒）。
pub type Mono = u64;

/// Slice 2.1 識別字串。
pub const SLICE: &str = "2.1";

pub mod actor;
pub mod alias_registry;
pub mod arbiter;
pub mod capture;
pub mod drain;
pub mod deferred;
pub mod generations;
pub mod human_console;
pub mod lease;
pub mod login_fsm;
pub mod mailbox;
pub mod mcu_patterns;
pub mod op_runner;
pub mod quiet_gate;
pub mod rescue;
pub mod resources;
pub mod self_test;
pub mod session_auth;
pub mod session_core;
pub mod session_manager;
pub mod state_store;
pub mod telnet_console;
pub mod tx_writer;
pub mod uart_bridge;
pub mod wal;
