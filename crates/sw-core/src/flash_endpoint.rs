//! P8 Task 9：`/dev/ttyMCU` flash 端點（Python `sw_core/flash_endpoint.py` `FlashEndpoint`）。
//!
//! 常駐 PTY：slave 以穩定 symlink（`TTYMCU_PATH`）命名，daemon 同持 master 與 slave keepalive。端點 thread
//! （`serialwrap-ttyMCU`）只等 master 可讀（flasher 寫入），**從不主動寫 master**；可讀後讀出首段交給
//! [`FlashCoordinator::on_flash_open`]（同步執行：detection、FLASHING、pump 都在端點 thread 內，與 Python 相同），
//! 返回後回到等待，繼續服務下一次 flasher open。迴圈只看 [`EndpointShutdown`]（R2-F5）。
//!
//! 平台：`caps.flash_endpoint=false`（Windows）時 [`FlashEndpoint::start`] 回 `Ok(None)`、不起 thread——Windows
//! 韌體升級走 device release／attach 交接（#84、#54）。daemon 絕不對端點 slave 設 `TIOCEXCL`，也不以
//! `open_exclusive` 開它，flasher 每次關閉後可再開。

pub mod coordinator;
pub mod detect;
pub mod holder_watch;
pub mod pump;

use std::path::{Path, PathBuf};
use std::sync::atomic::AtomicBool;
use std::sync::Arc;
use std::thread::JoinHandle;
use std::time::Duration;

use sw_pal::console_pty::ConsoleTransportFactory;
use sw_pal::traits::PalResult;
use sw_pal::Platform;

pub use coordinator::{EndpointShutdown, FlashCoordinator, FlashPtyIo, FlashRunCancel, LastDetect};
pub use detect::{detect_mcu_line, DetectResult, DetectStatus, ProbeTransport};
pub use holder_watch::watch_flasher_disconnect;
pub use pump::pump_endpoint_to_sink;

/// 端點 thread 等可讀的上限（Python `FlashEndpoint(grace=0.3)`）。
pub const ENDPOINT_GRACE: Duration = Duration::from_millis(300);
/// `stop()` join 端點 thread 的上限（Python `self._thread.join(timeout=2.0)`）。
pub const ENDPOINT_JOIN: Duration = Duration::from_secs(2);
/// 可讀後首次讀取的上限（Python `os.read(master_fd, 4096)`）。
pub const FIRST_READ_MAX: usize = 4096;

/// `FlashEndpoint::stop` 的收尾回條。
///
/// `joined=false` 時不得宣稱清理完成：WARNING 列出 `stalled_threads`，daemon 停止序照常往下並列入 incomplete
/// （Task 12），但不關閉仍被 stalled thread 使用的 master。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EndpointStopReceipt {
    pub joined: bool,
    pub stalled_threads: Vec<&'static str>,
    pub unlinked: bool,
}

/// 常駐 `/dev/ttyMCU` 端點（見模組說明）。
pub struct FlashEndpoint {
    thread: Option<JoinHandle<()>>,
    coord: Arc<FlashCoordinator>,
    endpoint_shutdown: EndpointShutdown,
    active: Arc<AtomicBool>,
    console: Arc<dyn ConsoleTransportFactory>,
    link: PathBuf,
}

impl FlashEndpoint {
    /// `caps.flash_endpoint=false` → `Ok(None)`；否則 `create_pty` → `tty_setraw` → `link` → 起端點 thread。
    pub fn start(
        pal: &Platform,
        link_path: &Path,
        coord: Arc<FlashCoordinator>,
        grace: Duration,
    ) -> PalResult<Option<FlashEndpoint>> {
        let _ = (pal, link_path, coord, grace);
        todo!("P8-T9")
    }

    /// Python `is_flashing()`：`on_flash_open` 進行中（含 detection）。
    pub fn is_flashing(&self) -> bool {
        todo!("P8-T9")
    }

    /// 只供測試：端點 thread 尚未結束。
    #[cfg(feature = "test-fakes")]
    pub fn thread_alive_for_test(&self) -> bool {
        todo!("P8-T9")
    }

    /// `request_shutdown`（設 `EndpointShutdown`＋取消本次 run）→ join 端點 thread（2.0 s；thread 收尾時關 master 與
    /// slave keepalive）→ unlink。
    pub fn stop(self) -> EndpointStopReceipt {
        todo!("P8-T9")
    }
}

impl Drop for FlashEndpoint {
    /// 未呼叫 `stop` 的任何退出路徑（含 panic unwind）：設停止旗標並 unlink（不等 thread）。
    fn drop(&mut self) {}
}
