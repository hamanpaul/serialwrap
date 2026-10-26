//! `ProcessCtl` 的平台實作放在 `posix`／`windows` 子模組，依目標平台擇一編譯。
//!
//! 背景啟動保留 stdin，並依平台建立獨立 session／process group；stdout 與 stderr
//! 導向 null device。同步前景呼叫則繼承標準串流並回傳子行程結束碼。
#[cfg(unix)]
pub mod posix;
#[cfg(windows)]
pub mod windows;

/// 前景執行程序的啟動選項。
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct ExecOpts {
    /// Windows 下不建立 console 視窗。
    pub no_window: bool,
    /// Windows 下以 Job Object 確保 launcher 結束時清除子行程。
    pub kill_on_close: bool,
}

/// `ProcessCtl::spawn_elevated` 的結果（P4-T19，SC-36，OD-15）。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ElevatedOutcome {
    /// 提權行程已結束，附結束碼。
    Exited(i32),
    /// 使用者拒絕 UAC（`ERROR_CANCELLED`）。
    Declined,
}

/// event handler 的執行規格（P7 Task 0 凍結；Task 14 實作，SC-26）。
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct HandlerSpec {
    pub argv: Vec<String>,
    pub env: Vec<(String, String)>,
    pub stdin: Vec<u8>,
    pub timeout: std::time::Duration,
    pub tail_bytes: usize,
}

/// handler 執行結果。
#[derive(Debug)]
pub enum HandlerOutcome {
    Completed { exit_code: i32, stdout_tail: Vec<u8>, stderr_tail: Vec<u8> },
    TimedOut,
    SpawnFailed(crate::traits::PalError),
}

/// 關機時由 dispatcher 觸發的中止旗標（視同逾時終止整個 process group／Job）。
#[derive(Clone, Debug, Default)]
pub struct HandlerAbort(#[allow(dead_code)] std::sync::Arc<(std::sync::Mutex<bool>, std::sync::Condvar)>);

impl HandlerAbort {
    pub fn new() -> Self {
        todo!("P7 Task 14")
    }
    pub fn trigger(&self) {
        todo!("P7 Task 14")
    }
}

/// event handler 子行程產生器（POSIX `fork/exec`＋process group；Windows `CreateProcessW`＋Job，SC-26）。
pub trait HandlerSpawner: Send + Sync {
    /// POSIX `["/bin/sh","-c",shell]`；Windows `["cmd.exe","/d","/s","/c",shell]`。
    fn shell_argv(&self, shell: &str) -> Vec<String>;
    /// POSIX `PATH,HOME,USER,LANG,LC_ALL`；Windows 再加 SC-26 六鍵。
    fn inherited_env_keys(&self) -> &'static [&'static str];
    /// `abort`：關機時由 dispatcher 觸發，視同逾時終止。
    fn run(&self, spec: &HandlerSpec, abort: &HandlerAbort) -> HandlerOutcome;
}

/// 原生 `HandlerSpawner`（平台本體在 `posix`／`windows` 子模組；Task 14 實作）。
pub struct NativeHandlerSpawner;

impl HandlerSpawner for NativeHandlerSpawner {
    fn shell_argv(&self, _shell: &str) -> Vec<String> {
        todo!("P7 Task 14")
    }
    fn inherited_env_keys(&self) -> &'static [&'static str] {
        todo!("P7 Task 14")
    }
    fn run(&self, _spec: &HandlerSpec, _abort: &HandlerAbort) -> HandlerOutcome {
        todo!("P7 Task 14")
    }
}
