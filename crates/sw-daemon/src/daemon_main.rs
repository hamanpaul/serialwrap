//! `serialwrapd` daemon 本體組裝（arch §6.1、計畫 P5-T19）。
//!
//! 本檔目前只含 P5-T19 紅燈階段的介面骨架：函式本體為 `unimplemented!`，
//! 讓整合測試可編譯並於執行期以「功能未實作」失敗。啟動順序、有序關機與 exit code 由實作補上。

use sw_pal::Platform;

/// 產品入口：解析 argv、組裝 daemon、回傳 exit code（0／1／2／3）。
pub fn run(argv: Vec<String>, platform: Platform) -> i32 {
    let _ = (argv, platform);
    unimplemented!("slice21 T19 run")
}

/// 啟動／停機順序的觀測事件（僅 `test-fakes` 編譯；產品不產生副作用）。
///
/// 計畫原列的變體表缺 `ShutdownGate`，但 `sigterm_graceful_exit_0_and_flushes_queue` 的停機序列斷言需要它
/// （stopping gate 先於 watcher 停止，R1-F2），故於此補上。
#[cfg(feature = "test-fakes")]
#[derive(Debug, Clone, PartialEq)]
pub enum TraceEvent {
    InitialScan,
    Rank,
    UpdateDevices,
    BootstrapAttach,
    WatcherStart,
    Bound,
    EndpointWritten,
    ServerStopped,
    ShutdownGate,
    WatcherStopped,
    SessionsDrained {
        flushed: Vec<(String, String)>,
        stalled: Vec<String>,
    },
    WalShutdown {
        ok: bool,
    },
    LockReleased,
}

/// 測試掛鉤（僅 `test-fakes` 編譯）。兩個 commit hook 經 `StateStore::spawn_with_hooks` 交給 store thread。
#[cfg(feature = "test-fakes")]
pub struct DaemonHooks {
    pub before_state_commit: Option<Box<dyn Fn() + Send + Sync>>,
    pub after_state_commit: Option<Box<dyn Fn() + Send + Sync>>,
    pub trace: Option<Box<dyn Fn(TraceEvent) + Send + Sync>>,
}

/// in-process 執行 daemon（僅 `test-fakes` 編譯）；語意與 `run` 相同，另接受測試掛鉤。
#[cfg(feature = "test-fakes")]
pub fn run_with_hooks(argv: Vec<String>, platform: Platform, hooks: DaemonHooks) -> i32 {
    let _ = (argv, platform, hooks);
    unimplemented!("slice21 T19 run_with_hooks")
}
