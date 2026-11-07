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

use std::panic::{catch_unwind, AssertUnwindSafe};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::thread::JoinHandle;
use std::time::{Duration, Instant};

use sw_pal::console_pty::{ConsoleTransportFactory, KeepAlive, Readiness};
use sw_pal::traits::{PalError, PalErrorKind, PalResult, ReadOutcome};
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
        if !pal.caps.flash_endpoint {
            // Windows 無 PTY：flash 端點不適用（#84 PORT-4：Windows 走 device release／handoff）。
            return Ok(None);
        }
        let console = pal.console.clone();
        let pair = console.create_pty()?;
        // 關鍵：slave 設 raw，否則 line discipline 的 CR/LF 轉換會汙染 flasher 的 SBL 二進位協定。
        console.tty_setraw(&pair.slave_ref)?;
        console.link(&pair.slave_path, link_path)?;
        let endpoint_shutdown = coord.endpoint_shutdown().clone();
        endpoint_shutdown.add_waker(pair.master.waker());
        let active = Arc::new(AtomicBool::new(false));
        let io = FlashPtyIo {
            master: pair.master,
            slave_path: pair.slave_path,
            slave_ref: pair.slave_ref,
        };
        let keepalive = pair.slave_keepalive;
        let spawned = {
            let coord = coord.clone();
            let shutdown = endpoint_shutdown.clone();
            let active = active.clone();
            std::thread::Builder::new()
                .name("serialwrap-ttyMCU".into())
                .spawn(move || endpoint_loop(io, keepalive, coord, shutdown, active, grace))
        };
        let thread = match spawned {
            Ok(t) => t,
            Err(e) => {
                let _ = console.unlink(link_path);
                return Err(PalError::plain(PalErrorKind::Io, format!("/dev/ttyMCU 端點 thread 啟動失敗：{e}")));
            }
        };
        Ok(Some(FlashEndpoint {
            thread: Some(thread),
            coord,
            active,
            console,
            link: link_path.to_path_buf(),
        }))
    }

    /// Python `is_flashing()`：`on_flash_open` 進行中（含 detection）。
    pub fn is_flashing(&self) -> bool {
        self.active.load(Ordering::SeqCst)
    }

    /// 只供測試：端點 thread 尚未結束。
    #[cfg(feature = "test-fakes")]
    pub fn thread_alive_for_test(&self) -> bool {
        self.thread.as_ref().is_some_and(|t| !t.is_finished())
    }

    /// `request_shutdown`（設 `EndpointShutdown`＋取消本次 run）→ join 端點 thread（2.0 s；thread 收尾時關 master 與
    /// slave keepalive）→ unlink。
    pub fn stop(mut self) -> EndpointStopReceipt {
        self.coord.request_shutdown();
        let mut stalled: Vec<&'static str> = Vec::new();
        if let Some(thread) = self.thread.take() {
            let deadline = Instant::now() + ENDPOINT_JOIN;
            while !thread.is_finished() && Instant::now() < deadline {
                std::thread::sleep(Duration::from_millis(2));
            }
            if thread.is_finished() {
                let _ = thread.join();
            } else {
                // 仍在使用 master 的 thread 不 join、不關 master（thread 收尾時自行關閉）。
                stalled.push(if self.coord.phase() == coordinator::PHASE_PUMP { "pump" } else { "endpoint" });
            }
        }
        stalled.extend(self.coord.take_stalled());
        let unlinked = self.console.unlink(&self.link).is_ok();
        if !stalled.is_empty() {
            self.coord.warn(format!(
                "serialwrap: /dev/ttyMCU 端點停止未完成：{} 未在 {:.1}s 內結束",
                stalled.join(","),
                ENDPOINT_JOIN.as_secs_f64()
            ));
        }
        EndpointStopReceipt {
            joined: stalled.is_empty(),
            stalled_threads: stalled,
            unlinked,
        }
    }
}

impl Drop for FlashEndpoint {
    /// 未呼叫 `stop` 的任何退出路徑（含 panic unwind）：設停止旗標並 unlink（不等 thread）。
    fn drop(&mut self) {
        if self.thread.take().is_some() {
            self.coord.request_shutdown();
            let _ = self.console.unlink(&self.link);
        }
    }
}

/// 端點 thread（Python `FlashEndpoint._loop`）：只偵測 flasher 寫入；其餘時間端點完全沉默。
fn endpoint_loop(
    mut io: FlashPtyIo,
    keepalive: KeepAlive,
    coord: Arc<FlashCoordinator>,
    shutdown: EndpointShutdown,
    active: Arc<AtomicBool>,
    grace: Duration,
) {
    let mut buf = vec![0u8; FIRST_READ_MAX];
    // 迴圈只看端點停止旗標：on_flash_open 返回（不論結果）後回到等待，服務下一次 flasher open（R2-F5）。
    while !shutdown.is_set() {
        match io.master.wait_readable(Some(grace)) {
            Ok(Readiness::Ready) => {}
            // 無 client 寫入 → 不主動寫任何 bytes（保持沉默）。
            Ok(Readiness::TimedOut | Readiness::Woken) => continue,
            Err(e) => {
                coord.warn(format!("serialwrap: /dev/ttyMCU 端點等待失敗，端點 thread 結束：{}", e.py_message));
                break;
            }
        }
        if shutdown.is_set() {
            break;
        }
        // client 寫入＝flasher：讀出首段交給 flash 路徑；讀取錯誤或 EOF 視同空（Python `OSError`／`BlockingIOError`）。
        let first = match io.master.read(&mut buf, Some(Duration::ZERO)) {
            Ok(ReadOutcome::Data(n)) => buf[..n].to_vec(),
            _ => Vec::new(),
        };
        if !active.load(Ordering::SeqCst) {
            active.store(true, Ordering::SeqCst);
            // on_flash_open（偵測／pump）的任何 panic 都不得殺死端點 thread；收斂後繼續服務後續 open（C1）。
            if catch_unwind(AssertUnwindSafe(|| coord.on_flash_open(&mut io, &first))).is_err() {
                coord.warn("serialwrap: /dev/ttyMCU 端點 on_flash_open 發生 panic，已收斂並繼續服務".to_string());
            }
            active.store(false, Ordering::SeqCst);
        }
    }
    io.master.close();
    drop(keepalive);
}
