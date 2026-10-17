//! 記憶體替身（feature `test-fakes`）。替身不得冒充原生 gate（arch §11.2）。

use std::ffi::OsString;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;

pub mod clock;
pub mod env;
pub mod fs;
pub mod platform;
pub mod proc;
pub mod process;
pub mod realfs;
pub mod registry;
pub mod rpc;
pub mod service;
pub mod sysenv;
pub mod tunnel;

// 測試以 `sw_pal::fakes::<Type>` 直接引用（計畫 Task 1／9／13 的 import 路徑）。
pub use clock::FakeClock;
pub use env::FakeEnv;
pub use fs::{FaultFs, MemFs};
pub use platform::{platform, posix_caps, windows_caps, FakePlatformBuilder};
pub use proc::FakeProcScan;
pub use process::{FakeProcessCtl, RecordingProcess};
pub use realfs::real_fs;
pub use registry::FakeRegistry;
pub use rpc::{FakeConn, FakeRpcConnector, ScriptedConnector, StreamStep};
pub use service::FakeServiceCtl;
pub use sysenv::{FakeSysEnv, FakeSysEnvBuilder};
pub use tunnel::FakeTunnel;

static EVENT_SEQ: AtomicU64 = AtomicU64::new(1);

/// 全程單調的事件序號（P4-T1）：各替身記錄呼叫時取號，供 `sw-cli` 測試替身的事件合併（clock 後序號）。
pub fn next_event_seq() -> u64 {
    EVENT_SEQ.fetch_add(1, Ordering::SeqCst)
}

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
        procs: proc::FakeProcScan::new(Vec::new()),
        tunnel: tunnel::FakeTunnel::new(false),
        service: service::FakeServiceCtl::new(Vec::new()),
        registry: registry::FakeRegistry::new(Vec::new(), &[]),
        caps: platform::posix_caps(),
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
