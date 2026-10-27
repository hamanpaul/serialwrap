//! `ProcessCtl` 的平台實作放在 `posix`／`windows` 子模組，依目標平台擇一編譯。
//!
//! 背景啟動保留 stdin，並依平台建立獨立 session／process group；stdout 與 stderr
//! 導向 null device。同步前景呼叫則繼承標準串流並回傳子行程結束碼。
#[cfg(unix)]
mod handler_posix;
#[cfg(windows)]
mod handler_windows;
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

/// Python `Popen.returncode` 的呈現（審查 b65df2d_01c474c 9c4a80e-1）：Windows 為 `GetExitCodeProcess` 的不帶號 DWORD
/// （`HandlerOutcome.exit_code: i32` 凍結，呈現時還原）；POSIX 原值（訊號終止為 `-signum`）。
pub fn py_returncode(exit_code: i32) -> i64 {
    #[cfg(windows)]
    {
        i64::from(exit_code as u32)
    }
    #[cfg(not(windows))]
    {
        i64::from(exit_code)
    }
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
pub struct HandlerAbort(std::sync::Arc<(std::sync::Mutex<bool>, std::sync::Condvar)>);

impl HandlerAbort {
    pub fn new() -> Self {
        Self::default()
    }
    pub fn trigger(&self) {
        let (flag, cv) = &*self.0;
        *flag.lock().unwrap_or_else(|e| e.into_inner()) = true;
        cv.notify_all();
    }
    pub fn is_triggered(&self) -> bool {
        *self.0 .0.lock().unwrap_or_else(|e| e.into_inner())
    }
    /// 最多等 `d`；回 `true` 表示已觸發。
    pub(crate) fn wait_timeout(&self, d: std::time::Duration) -> bool {
        let (flag, cv) = &*self.0;
        let g = flag.lock().unwrap_or_else(|e| e.into_inner());
        if *g {
            return true;
        }
        let (g, _) = cv.wait_timeout(g, d).unwrap_or_else(|e| e.into_inner());
        *g
    }
}

/// Windows：可繼承 handle 的建立到 CreateProcessW 之間的互斥（handler pipe 子端與 ProcessCtl 的 spawn 不交錯；
/// 審查 96b28dd_06c61d0 L3）。POSIX 不使用（CLOEXEC 標記即足夠）。
#[cfg_attr(not(windows), allow(dead_code))]
pub(crate) static WIN_SPAWN_LOCK: std::sync::Mutex<()> = std::sync::Mutex::new(());

/// stdout／stderr 讀取執行緒：讀到 EOF，只保留最後 `tail` bytes（Python `stdout[-N:]` 等價）；結束時通知 `done`。
pub(crate) fn spawn_tail_reader<R: std::io::Read + Send + 'static>(
    mut r: R,
    tail: usize,
    done: std::sync::mpsc::Sender<()>,
) -> std::thread::JoinHandle<Vec<u8>> {
    std::thread::spawn(move || {
        let mut keep: Vec<u8> = Vec::new();
        let mut buf = [0u8; 8192];
        loop {
            match r.read(&mut buf) {
                Ok(0) => break,
                Ok(n) => {
                    keep.extend_from_slice(&buf[..n]);
                    if keep.len() > tail.saturating_mul(2).max(8192) {
                        let cut = keep.len() - tail;
                        keep.drain(..cut);
                    }
                }
                Err(e) if e.kind() == std::io::ErrorKind::Interrupted => continue,
                Err(_) => break,
            }
        }
        if keep.len() > tail {
            let cut = keep.len() - tail;
            keep.drain(..cut);
        }
        let _ = done.send(());
        keep
    })
}

/// stdin 寫入執行緒：寫完即關閉；子行程不讀或已結束（EPIPE）時略過（Python `communicate` 同樣忽略 BrokenPipe）。
/// 不 join：孫行程持有 stdin 而不讀時不得卡住收尾。
pub(crate) fn spawn_stdin_writer<W: std::io::Write + Send + 'static>(mut w: W, data: Vec<u8>) {
    std::thread::spawn(move || {
        let _ = w.write_all(&data);
        let _ = w.flush();
    });
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

/// 原生 `HandlerSpawner`（平台本體在 `handler_posix`／`handler_windows`；Task 14）。
pub struct NativeHandlerSpawner;

impl HandlerSpawner for NativeHandlerSpawner {
    fn shell_argv(&self, shell: &str) -> Vec<String> {
        #[cfg(unix)]
        {
            handler_posix::shell_argv(shell)
        }
        #[cfg(windows)]
        {
            handler_windows::shell_argv(shell)
        }
    }
    fn inherited_env_keys(&self) -> &'static [&'static str] {
        #[cfg(unix)]
        {
            handler_posix::INHERITED_ENV_KEYS
        }
        #[cfg(windows)]
        {
            handler_windows::INHERITED_ENV_KEYS
        }
    }
    fn run(&self, spec: &HandlerSpec, abort: &HandlerAbort) -> HandlerOutcome {
        #[cfg(unix)]
        {
            handler_posix::run(spec, abort)
        }
        #[cfg(windows)]
        {
            handler_windows::run(spec, abort)
        }
    }
}
