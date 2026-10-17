//! 記憶體替身（feature `test-fakes`）。替身不得冒充原生 gate（arch §11.2）。

use std::ffi::OsString;
use std::sync::Arc;

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
    let clock = Arc::new(clock::FakeClock::default());
    let connector = Arc::new(rpc::ScriptedConnector::new(
        script,
        Arc::clone(&clock),
        true,
    ));
    let platform = crate::ClientPlatform {
        env: Arc::new(env::FakeEnv::default()),
        fs: Arc::new(fs::MemFs::default()),
        clock: Arc::clone(&clock) as Arc<dyn crate::traits::Clock>,
        rpc: Arc::clone(&connector) as Arc<dyn crate::traits::RpcConnector>,
        process: Arc::new(process::RecordingProcess::default()),
        caps: crate::ClientCaps {
            path_flavor: crate::PalPathFlavor::Posix,
            af_unix: true,
            native_backend: crate::BackendKind::Posix,
            default_skill: crate::SkillPlatform::Linux,
            text_newline: "\n",
            exec_replace: true,
        },
    };
    (platform, connector, clock)
}

/// POSIX：`OsStr::from_bytes`；Windows：每個 byte 映射為未配對 surrogate（測試專用）。
pub fn non_utf8_os_string(raw: &[u8]) -> OsString {
    #[cfg(unix)]
    {
        use std::os::unix::ffi::OsStrExt;
        OsString::from(std::ffi::OsStr::from_bytes(raw))
    }
    #[cfg(windows)]
    {
        use std::os::windows::ffi::OsStringExt;
        let units: Vec<u16> = raw.iter().map(|byte| 0xdc00 | u16::from(*byte)).collect();
        OsString::from_wide(&units)
    }
}
