//! 記憶體替身（feature `test-fakes`；P3-T9 骨架）。替身不得冒充原生 gate（arch §11.2）。
#![allow(unused_variables)]

use std::ffi::OsString;

pub mod clock;
pub mod env;
pub mod fs;
pub mod process;
pub mod rpc;

// 測試以 `sw_pal::fakes::<Type>` 直接引用（計畫 Task 9／13 的 import 路徑）。
pub use clock::FakeClock;
pub use env::FakeEnv;
pub use fs::MemFs;
pub use process::RecordingProcess;
pub use rpc::{FakeConn, ScriptedConnector, StreamStep};

/// 依 script 建立替身能力包（P3-T9 實作）。
pub fn fake_platform(
    script: Vec<rpc::FakeConn>,
) -> (
    crate::ClientPlatform,
    std::sync::Arc<rpc::ScriptedConnector>,
    std::sync::Arc<clock::FakeClock>,
) {
    todo!("P3-T9: fake_platform")
}

/// POSIX：`OsStr::from_bytes`；Windows：每個 byte 映射為未配對 surrogate（測試專用）。
pub fn non_utf8_os_string(raw: &[u8]) -> OsString {
    todo!("P3-T9: non_utf8_os_string")
}
