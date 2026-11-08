//! Stage 2 承接：daemon 核心狀態機、I/O 協調與 Python 相容行為。

/// `Clock::mono_ns()` 的單位（納秒）。
pub type Mono = u64;

/// 目前交付的切片識別字串（P6 Task 10：Slice 2.2 承接 console RPC，增補 #24）。
pub const SLICE: &str = "2.2";

pub mod actor;
pub mod admission;
pub mod alias_registry;
pub mod arbiter;
pub mod capture;
pub mod drain;
pub mod event_engine;
pub mod deferred;
pub mod file_transfer;
pub mod flash_endpoint;
pub mod generations;
pub mod human_console;
pub mod lease;
pub mod login_fsm;
pub mod login_run;
pub mod mailbox;
pub mod mcu_patterns;
pub mod mono;
pub mod op_runner;
pub mod quiet_gate;
pub mod recover;
pub mod reprobe;
pub mod rescue;
pub mod resources;
pub mod self_test;
pub mod session_auth;
pub mod session_core;
pub mod session_manager;
pub mod state_store;
pub mod telnet_console;
pub mod transport_stall;
pub mod tx_writer;
pub mod uart_bridge;
pub mod wal;
