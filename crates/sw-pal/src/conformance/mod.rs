//! PAL conformance（arch §9.1）：同一套斷言分別跑在 fakes、POSIX PTY、Windows COM pair 上。
//! 僅供測試（feature `test-fakes`）；不得在產品路徑使用。在 fakes 上通過不得計為原生 gate（OD-08）。
//!
//! T2 測試作者交付：PTY 測試夾具（`posix_pty_pair`、`PtyPeer`、子程序輔助）為可執行的測試基礎設施，
//! 讓測試能走到被測行為；被測的 PAL 行為（`PosixSerialBackend` 等）仍為 `unimplemented!` 骨架。

pub mod lock_suite;
pub mod rpc_suite;
pub mod serial_suite;
pub mod wal_fs_suite;

pub use lock_suite::lock_suite;
#[cfg(unix)]
pub use lock_suite::bind_stale_unix_socket;
pub use rpc_suite::rpc_suite;
pub use wal_fs_suite::{wal_fs_suite, ExternalFsOps, FakeExternalFs, NativeExternalFs};

use std::ffi::CStr;
use std::io::{BufRead, BufReader};
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::Arc;
use std::thread;
use std::time::{Duration, Instant};

use crate::fakes::serial::FakeSerialBackend;
use crate::traits::{DevicePath, HolderReport, HolderScanStatus, UartExclusive};

pub use serial_suite::{serial_suite, SerialPairCaps};

/// 在指定執行緒的下一次 sysenv spawn 前暫停子行程，供 POSIX fork→exec 競態回歸重用。
///
/// 預設不接到產品路徑；只有 `test-fakes` 測試建置會編譯本夾具與其 thread-local 註冊。
#[cfg(all(unix, feature = "test-fakes"))]
pub struct SysenvSpawnPauseCall<T> {
    gate: SysenvSpawnPauseGate,
    result: std::sync::mpsc::Receiver<T>,
    worker: Option<thread::JoinHandle<()>>,
}

#[cfg(all(unix, feature = "test-fakes"))]
impl<T: Send + 'static> SysenvSpawnPauseCall<T> {
    /// 在呼叫執行緒註冊一次性暫停點，然後執行 `call`。
    /// `call` 應自行設定執行期限；join 與 Drop 都會先放行子行程再回收執行緒。
    pub fn spawn<F>(child_timeout: Duration, call: F) -> std::io::Result<Self>
    where
        F: FnOnce() -> T + Send + 'static,
    {
        let (pause, gate) = SysenvSpawnPause::new(child_timeout)?;
        let (sender, result) = std::sync::mpsc::sync_channel(1);
        let worker = thread::Builder::new()
            .name("sw-pal-sysenv-spawn-pause".to_string())
            .spawn(move || {
                let _registration = pause.install();
                let result = call();
                let _ = sender.send(result);
            })?;
        Ok(Self {
            gate,
            result,
            worker: Some(worker),
        })
    }

    /// 等待子行程在 pre-exec 回報 PID；本等待由呼叫端提供期限。
    pub fn wait_for_pre_exec(&mut self, timeout: Duration) -> std::io::Result<u32> {
        self.gate.wait_for_pre_exec(timeout)
    }

    /// 檢查呼叫執行緒是否已完成。
    pub fn try_result(&self) -> std::result::Result<T, std::sync::mpsc::TryRecvError> {
        self.result.try_recv()
    }

    /// 等待呼叫完成。
    pub fn recv_timeout(
        &self,
        timeout: Duration,
    ) -> std::result::Result<T, std::sync::mpsc::RecvTimeoutError> {
        self.result.recv_timeout(timeout)
    }

    /// 放行 pre-exec 子行程；重複呼叫不會重複寫入。
    pub fn release(&mut self) -> std::io::Result<()> {
        self.gate.release()
    }

    /// 回收呼叫執行緒。
    pub fn join(&mut self) -> thread::Result<()> {
        let _ = self.gate.release();
        match self.worker.take() {
            Some(worker) => worker.join(),
            None => Ok(()),
        }
    }
}

#[cfg(all(unix, feature = "test-fakes"))]
impl<T> Drop for SysenvSpawnPauseCall<T> {
    fn drop(&mut self) {
        let _ = self.gate.release();
        if let Some(worker) = self.worker.take() {
            let _ = worker.join();
        }
    }
}

#[cfg(all(unix, feature = "test-fakes"))]
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) struct SysenvSpawnPauseConfig {
    pub(crate) notify_read_fd: libc::c_int,
    pub(crate) notify_write_fd: libc::c_int,
    pub(crate) release_read_fd: libc::c_int,
    pub(crate) release_write_fd: libc::c_int,
    pub(crate) release_keepalive_fd: libc::c_int,
    pub(crate) child_timeout_ms: libc::c_int,
}

#[cfg(all(unix, feature = "test-fakes"))]
thread_local! {
    static SYSENV_SPAWN_PAUSE: std::cell::RefCell<Option<SysenvSpawnPauseConfig>> = const {
        std::cell::RefCell::new(None)
    };
}

#[cfg(all(unix, feature = "test-fakes"))]
pub(crate) fn take_sysenv_spawn_pause() -> Option<SysenvSpawnPauseConfig> {
    SYSENV_SPAWN_PAUSE.with(|slot| slot.borrow_mut().take())
}

#[cfg(all(unix, feature = "test-fakes"))]
struct SysenvSpawnPause {
    config: SysenvSpawnPauseConfig,
    _notify_write: std::os::fd::OwnedFd,
    _release_read: std::os::fd::OwnedFd,
}

#[cfg(all(unix, feature = "test-fakes"))]
impl SysenvSpawnPause {
    fn new(child_timeout: Duration) -> std::io::Result<(Self, SysenvSpawnPauseGate)> {
        use std::os::fd::AsRawFd;

        const MAX_CHILD_PAUSE_MS: u128 = 30_000;
        if child_timeout.is_zero() {
            return Err(std::io::Error::new(
                std::io::ErrorKind::InvalidInput,
                "pre-exec 暫停期限必須大於零",
            ));
        }
        let child_timeout_ms =
            child_timeout.as_millis().clamp(1, MAX_CHILD_PAUSE_MS) as libc::c_int;
        let (notify_read, notify_write) = pipe_cloexec()?;
        let (release_read, release_write) = pipe_cloexec()?;
        // 保留一份讀端直到放行，避免呼叫執行緒先結束時寫 pipe 觸發 SIGPIPE。
        let release_keepalive = duplicate_cloexec(release_read.as_raw_fd())?;
        let config = SysenvSpawnPauseConfig {
            notify_read_fd: notify_read.as_raw_fd(),
            notify_write_fd: notify_write.as_raw_fd(),
            release_read_fd: release_read.as_raw_fd(),
            release_write_fd: release_write.as_raw_fd(),
            release_keepalive_fd: release_keepalive.as_raw_fd(),
            child_timeout_ms,
        };
        Ok((
            Self {
                config,
                _notify_write: notify_write,
                _release_read: release_read,
            },
            SysenvSpawnPauseGate {
                notify_read,
                release_write,
                _release_keepalive: release_keepalive,
                reached_pid: None,
                released: false,
            },
        ))
    }

    fn install(self) -> SysenvSpawnPauseRegistration {
        SYSENV_SPAWN_PAUSE.with(|slot| {
            let mut slot = slot.borrow_mut();
            assert!(slot.is_none(), "同一執行緒不可巢狀註冊 sysenv spawn 暫停點");
            *slot = Some(self.config);
        });
        SysenvSpawnPauseRegistration {
            config: self.config,
            _notify_write: self._notify_write,
            _release_read: self._release_read,
        }
    }
}

#[cfg(all(unix, feature = "test-fakes"))]
struct SysenvSpawnPauseRegistration {
    config: SysenvSpawnPauseConfig,
    _notify_write: std::os::fd::OwnedFd,
    _release_read: std::os::fd::OwnedFd,
}

#[cfg(all(unix, feature = "test-fakes"))]
impl Drop for SysenvSpawnPauseRegistration {
    fn drop(&mut self) {
        SYSENV_SPAWN_PAUSE.with(|slot| {
            let mut slot = slot.borrow_mut();
            if *slot == Some(self.config) {
                *slot = None;
            }
        });
    }
}

#[cfg(all(unix, feature = "test-fakes"))]
struct SysenvSpawnPauseGate {
    notify_read: std::os::fd::OwnedFd,
    release_write: std::os::fd::OwnedFd,
    _release_keepalive: std::os::fd::OwnedFd,
    reached_pid: Option<u32>,
    released: bool,
}

#[cfg(all(unix, feature = "test-fakes"))]
impl SysenvSpawnPauseGate {
    fn wait_for_pre_exec(&mut self, timeout: Duration) -> std::io::Result<u32> {
        use std::os::fd::AsRawFd;
        use std::time::Instant;

        if let Some(pid) = self.reached_pid {
            return Ok(pid);
        }
        let deadline = Instant::now().checked_add(timeout).ok_or_else(|| {
            std::io::Error::new(std::io::ErrorKind::InvalidInput, "通知期限超出範圍")
        })?;
        loop {
            let remaining = deadline.saturating_duration_since(Instant::now());
            if remaining.is_zero() {
                return Err(std::io::Error::new(
                    std::io::ErrorKind::TimedOut,
                    "等待 sysenv pre-exec PID 通知逾時",
                ));
            }
            let timeout_ms = remaining
                .as_millis()
                .saturating_add(u128::from(remaining.subsec_nanos() % 1_000_000 != 0))
                .clamp(1, libc::c_int::MAX as u128) as libc::c_int;
            let mut descriptor = libc::pollfd {
                fd: self.notify_read.as_raw_fd(),
                events: libc::POLLIN,
                revents: 0,
            };
            // SAFETY: descriptor 指向單一有效 pollfd，等待期限有限。
            let ready = unsafe { libc::poll(&mut descriptor, 1, timeout_ms) };
            if ready < 0 {
                let error = std::io::Error::last_os_error();
                if error.kind() == std::io::ErrorKind::Interrupted {
                    continue;
                }
                return Err(error);
            }
            if ready == 0 {
                return Err(std::io::Error::new(
                    std::io::ErrorKind::TimedOut,
                    "等待 sysenv pre-exec PID 通知逾時",
                ));
            }
            if descriptor.revents & libc::POLLIN == 0 {
                return Err(std::io::Error::new(
                    std::io::ErrorKind::UnexpectedEof,
                    "sysenv spawn 在 pre-exec 通知前結束",
                ));
            }

            let mut pid: libc::pid_t = 0;
            let mut read_bytes = 0usize;
            while read_bytes < std::mem::size_of::<libc::pid_t>() {
                // SAFETY: pid 為有效、足夠大的輸出緩衝區；fd 由此 gate 持有。
                let read = unsafe {
                    libc::read(
                        self.notify_read.as_raw_fd(),
                        std::ptr::addr_of_mut!(pid)
                            .cast::<u8>()
                            .add(read_bytes)
                            .cast(),
                        std::mem::size_of::<libc::pid_t>() - read_bytes,
                    )
                };
                if read < 0 {
                    let error = std::io::Error::last_os_error();
                    if error.kind() == std::io::ErrorKind::Interrupted {
                        continue;
                    }
                    return Err(error);
                }
                if read == 0 {
                    return Err(std::io::Error::new(
                        std::io::ErrorKind::UnexpectedEof,
                        "sysenv spawn 未提供完整 PID 通知",
                    ));
                }
                read_bytes += read as usize;
            }
            if pid <= 0 {
                return Err(std::io::Error::new(
                    std::io::ErrorKind::InvalidData,
                    "sysenv pre-exec 回報無效 PID",
                ));
            }
            let pid = pid as u32;
            self.reached_pid = Some(pid);
            return Ok(pid);
        }
    }

    fn release(&mut self) -> std::io::Result<()> {
        use std::os::fd::AsRawFd;

        if self.released {
            return Ok(());
        }
        let byte = 1u8;
        // SAFETY: byte 為有效的一位元組輸入，fd 由此 gate 持有。
        let written = unsafe {
            libc::write(
                self.release_write.as_raw_fd(),
                std::ptr::addr_of!(byte).cast(),
                std::mem::size_of_val(&byte),
            )
        };
        if written == 1 {
            self.released = true;
            Ok(())
        } else if written < 0 {
            Err(std::io::Error::last_os_error())
        } else {
            Err(std::io::Error::new(
                std::io::ErrorKind::WriteZero,
                "未能寫入 sysenv pre-exec 放行標記",
            ))
        }
    }
}

#[cfg(all(unix, feature = "test-fakes"))]
fn pipe_cloexec() -> std::io::Result<(std::os::fd::OwnedFd, std::os::fd::OwnedFd)> {
    use std::os::fd::{AsRawFd, FromRawFd, OwnedFd};

    let mut fds = [-1; 2];
    // SAFETY: fds 提供兩格 c_int 輸出空間。
    if unsafe { libc::pipe(fds.as_mut_ptr()) } != 0 {
        return Err(std::io::Error::last_os_error());
    }
    // SAFETY: pipe 成功後兩個 fd 各由一個 OwnedFd 接管。
    let read = unsafe { OwnedFd::from_raw_fd(fds[0]) };
    // SAFETY: 同上，接管另一端。
    let write = unsafe { OwnedFd::from_raw_fd(fds[1]) };
    set_cloexec(read.as_raw_fd())?;
    set_cloexec(write.as_raw_fd())?;
    Ok((read, write))
}

#[cfg(all(unix, feature = "test-fakes"))]
fn duplicate_cloexec(fd: std::os::fd::RawFd) -> std::io::Result<std::os::fd::OwnedFd> {
    use std::os::fd::{AsRawFd, FromRawFd};

    // SAFETY: dup 僅複製有效 fd，回傳值由 OwnedFd 接管。
    let duplicate = unsafe { libc::dup(fd) };
    if duplicate < 0 {
        return Err(std::io::Error::last_os_error());
    }
    // SAFETY: duplicate 是 dup 新建且尚未被接管的 fd。
    let duplicate = unsafe { std::os::fd::OwnedFd::from_raw_fd(duplicate) };
    set_cloexec(duplicate.as_raw_fd())?;
    Ok(duplicate)
}

#[cfg(all(unix, feature = "test-fakes"))]
fn set_cloexec(fd: std::os::fd::RawFd) -> std::io::Result<()> {
    // SAFETY: fcntl 只查詢／設定 fd descriptor flag。
    let flags = unsafe { libc::fcntl(fd, libc::F_GETFD) };
    if flags < 0 || unsafe { libc::fcntl(fd, libc::F_SETFD, flags | libc::FD_CLOEXEC) } < 0 {
        return Err(std::io::Error::last_os_error());
    }
    Ok(())
}

/// 對端（PTY master／COM pair 對端）：測試用來讀寫 UART 的另一端。
pub trait PeerEnd: Send {
    /// 在 `timeout` 內讀到的 bytes（可能為空）。
    fn read_available(&mut self, timeout: Duration) -> Vec<u8>;
    fn write(&mut self, b: &[u8]);
    /// 停止讀取 master（讓 PTY buffer 可被塞滿）。
    fn stop_reading(&mut self);
    fn resume_reading(&mut self);
}

/// 鎖後重掃永遠回報「無 pid、status Ok」的 `UartExclusive`（test-fakes only）。
/// 只供需要繞過鎖後重掃、驗證 flock／TIOCEXCL 釋放語意的子程序情境（R0-F2）。
pub struct NoHolders;

impl UartExclusive for NoHolders {
    fn probe_holders(&self, _real_path: &str) -> HolderReport {
        HolderReport {
            pids: Vec::new(),
            holder_unknown: true,
            status: HolderScanStatus::Ok,
        }
    }
}

/// `CAP_SYS_ADMIN`／root 判定（§6.2 例外）：root 下 TIOCEXCL 不擋普通 open，相關測試須 skip 並記「證據不足」。
pub fn has_cap_sys_admin() -> bool {
    #[cfg(unix)]
    {
        // SAFETY: geteuid 無參數、不可能失敗。
        unsafe { libc::geteuid() == 0 }
    }
    #[cfg(not(unix))]
    {
        false
    }
}

/// 子程序（`spawn_plain_holder`）重入的測試名稱；必須與 `serial_posix.rs` 內的 helper 測試同名。
#[cfg(unix)]
const PLAIN_HOLDER_CHILD_TEST: &str = "plain_holder_child";

/// POSIX PTY 對端：持有 master（O_NONBLOCK）。`stop_reading` 後不再從 master 取資料。
#[cfg(unix)]
pub struct PtyPeer {
    master: libc::c_int,
    reading: bool,
}

#[cfg(unix)]
impl Drop for PtyPeer {
    fn drop(&mut self) {
        // SAFETY: master 由本結構獨占持有，只關閉一次。
        unsafe {
            libc::close(self.master);
        }
    }
}

#[cfg(unix)]
impl PeerEnd for PtyPeer {
    fn read_available(&mut self, timeout: Duration) -> Vec<u8> {
        let deadline = Instant::now() + timeout;
        let mut out = Vec::new();
        loop {
            let now = Instant::now();
            if now >= deadline {
                break;
            }
            let remaining = deadline - now;
            if !self.reading {
                thread::sleep(remaining);
                break;
            }
            let ms = remaining.as_millis().clamp(1, i32::MAX as u128) as i32;
            let mut pfd = libc::pollfd {
                fd: self.master,
                events: libc::POLLIN,
                revents: 0,
            };
            // SAFETY: pfd 為合法的單元素陣列。
            let n = unsafe { libc::poll(&mut pfd, 1, ms) };
            if n <= 0 {
                continue;
            }
            if pfd.revents & libc::POLLIN != 0 {
                let mut buf = [0u8; 4096];
                // SAFETY: buf 為有效的可寫記憶體。
                let r = unsafe { libc::read(self.master, buf.as_mut_ptr().cast(), buf.len()) };
                if r > 0 {
                    out.extend_from_slice(&buf[..r as usize]);
                    continue;
                }
                // slave 端已全部關閉（EIO）或 EOF：不再有資料
                break;
            }
            if pfd.revents & (libc::POLLHUP | libc::POLLERR) != 0 {
                break;
            }
        }
        out
    }

    fn write(&mut self, b: &[u8]) {
        let mut off = 0usize;
        while off < b.len() {
            // SAFETY: 傳入的指標與長度來自同一切片。
            let n = unsafe { libc::write(self.master, b[off..].as_ptr().cast(), b.len() - off) };
            if n > 0 {
                off += n as usize;
                continue;
            }
            let e = std::io::Error::last_os_error();
            if e.raw_os_error() == Some(libc::EAGAIN) {
                let mut pfd = libc::pollfd {
                    fd: self.master,
                    events: libc::POLLOUT,
                    revents: 0,
                };
                // SAFETY: pfd 為合法的單元素陣列。
                unsafe {
                    libc::poll(&mut pfd, 1, 100);
                }
                continue;
            }
            panic!("pty master write 失敗: {e}");
        }
    }

    fn stop_reading(&mut self) {
        self.reading = false;
    }

    fn resume_reading(&mut self) {
        self.reading = true;
    }
}

/// 建立一對 PTY：回傳 slave 路徑（`DevicePath`）與 master 對端。
/// 建立時先把 slave 設為 raw 並關閉本程序持有的 slave fd（路徑保留，master 持有 tty 物件）；
/// 否則父程序的 slave fd 會被子程序的鎖後重掃識別為外部 holder。
/// P5 第 7 輪：master／slave 皆以 CLOEXEC 開啟（Linux 以 `posix_openpt(O_CLOEXEC)`＋`open(O_CLOEXEC)` 原子設定）——
/// `openpty` 的 fd 不帶 CLOEXEC，同執行檔平行測試 spawn 的子行程（plain holder 等）會繼承而被當成外部 holder。
#[cfg(unix)]
pub fn posix_pty_pair() -> (DevicePath, PtyPeer) {
    let (master, slave) = open_pty_cloexec();
    // SAFETY: slave 為 openpty 回傳的有效 fd；ttyname_r 將路徑寫入此呼叫專屬緩衝區。
    let path = unsafe {
        let mut t: libc::termios = std::mem::zeroed();
        if libc::tcgetattr(slave, &mut t) == 0 {
            libc::cfmakeraw(&mut t);
            libc::tcsetattr(slave, libc::TCSANOW, &t);
        }
        let mut name = vec![0 as libc::c_char; 4096];
        let rc = libc::ttyname_r(slave, name.as_mut_ptr(), name.len());
        assert_eq!(
            rc,
            0,
            "ttyname_r 失敗: {}",
            std::io::Error::from_raw_os_error(rc)
        );
        let s = CStr::from_ptr(name.as_ptr()).to_string_lossy().into_owned();
        libc::close(slave);
        // master 設 O_NONBLOCK（讀寫皆不阻塞）
        let flags = libc::fcntl(master, libc::F_GETFL);
        libc::fcntl(master, libc::F_SETFL, flags | libc::O_NONBLOCK);
        s
    };
    (
        DevicePath(path),
        PtyPeer {
            master,
            reading: true,
        },
    )
}

/// 開一對帶 CLOEXEC 的 PTY fd（master, slave）。
#[cfg(target_os = "linux")]
fn open_pty_cloexec() -> (libc::c_int, libc::c_int) {
    // SAFETY: posix_openpt／grantpt／unlockpt／ptsname_r／open 皆以有效參數呼叫；名稱寫入本呼叫專屬緩衝區。
    unsafe {
        let master = libc::posix_openpt(libc::O_RDWR | libc::O_NOCTTY | libc::O_CLOEXEC);
        assert!(master >= 0, "posix_openpt 失敗: {}", std::io::Error::last_os_error());
        assert_eq!(libc::grantpt(master), 0, "grantpt 失敗: {}", std::io::Error::last_os_error());
        assert_eq!(libc::unlockpt(master), 0, "unlockpt 失敗: {}", std::io::Error::last_os_error());
        let mut name = vec![0 as libc::c_char; 4096];
        let rc = libc::ptsname_r(master, name.as_mut_ptr(), name.len());
        assert_eq!(rc, 0, "ptsname_r 失敗: {}", std::io::Error::from_raw_os_error(rc));
        let slave = libc::open(name.as_ptr(), libc::O_RDWR | libc::O_NOCTTY | libc::O_CLOEXEC);
        assert!(slave >= 0, "開啟 PTY slave 失敗: {}", std::io::Error::last_os_error());
        (master, slave)
    }
}

/// 非 Linux 的 POSIX：`openpty` 後立即設 FD_CLOEXEC（無原子介面；本專案 POSIX 正式平台為 Linux／WSL）。
#[cfg(all(unix, not(target_os = "linux")))]
fn open_pty_cloexec() -> (libc::c_int, libc::c_int) {
    let mut master: libc::c_int = -1;
    let mut slave: libc::c_int = -1;
    // SAFETY: 兩個輸出指標皆指向有效的 c_int；其餘參數允許為 null。
    let rc = unsafe {
        libc::openpty(
            &mut master,
            &mut slave,
            std::ptr::null_mut(),
            std::ptr::null(),
            std::ptr::null(),
        )
    };
    assert_eq!(rc, 0, "openpty 失敗: {}", std::io::Error::last_os_error());
    for fd in [master, slave] {
        // SAFETY: fd 為 openpty 回傳的有效 fd。
        unsafe { libc::fcntl(fd, libc::F_SETFD, libc::FD_CLOEXEC) };
    }
    (master, slave)
}

/// 子程序（以 `current_exe()` 重入 `plain_holder_child` 測試）：普通 open（不 flock、不 TIOCEXCL）持有 slave，回報 READY 後等待。
/// `Drop` 時 kill＋wait。
#[cfg(unix)]
pub struct PlainHolder {
    child: Child,
}

#[cfg(unix)]
impl PlainHolder {
    pub fn id(&self) -> u32 {
        self.child.id()
    }
}

#[cfg(unix)]
impl Drop for PlainHolder {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

#[cfg(unix)]
pub fn spawn_plain_holder(dev: &DevicePath) -> PlainHolder {
    let exe = std::env::current_exe().expect("current_exe");
    let mut child = Command::new(exe)
        .args([
            "--exact",
            PLAIN_HOLDER_CHILD_TEST,
            "--include-ignored",
            "--nocapture",
            "--test-threads=1",
        ])
        .env("S21_CHILD", "plain_holder")
        .env("S21_DEV", &dev.0)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::inherit())
        .spawn()
        .expect("spawn plain holder");
    // 測試框架會先印 `test <name> ... ` 而不換行，READY 行形如 `test <name> ... S21_READY`，以子字串比對。
    // 10 秒未就緒即由看門狗 kill，避免掛住。
    let done = Arc::new(AtomicBool::new(false));
    let watchdog_done = Arc::clone(&done);
    let pid = child.id();
    thread::spawn(move || {
        thread::sleep(Duration::from_secs(10));
        if !watchdog_done.load(Ordering::SeqCst) {
            // SAFETY: pid 為剛 spawn 的子程序；未就緒即強制結束，讓父程序讀取回 EOF。
            unsafe {
                libc::kill(pid as libc::pid_t, libc::SIGKILL);
            }
        }
    });
    {
        let stdout = child.stdout.as_mut().expect("plain holder stdout");
        let mut reader = BufReader::new(stdout);
        let mut line = String::new();
        loop {
            line.clear();
            let n = reader
                .read_line(&mut line)
                .expect("read plain holder stdout");
            assert!(n > 0, "plain holder 在 READY 前結束（見上方子程序 stderr）");
            if line.contains("S21_READY") {
                break;
            }
        }
    }
    done.store(true, Ordering::SeqCst);
    PlainHolder { child }
}

/// 只讀取 slave 的 termios 位元組（另開 `O_RDONLY|O_NOCTTY|O_NONBLOCK`，取完即關閉），供「未被改寫」比對。
#[cfg(unix)]
pub fn tcgetattr_snapshot(dev: &DevicePath) -> Vec<u8> {
    let c = std::ffi::CString::new(dev.0.as_bytes()).expect("device path without NUL");
    // SAFETY: 路徑為有效 C 字串；回傳的 fd 只在本函式內使用並關閉。
    unsafe {
        let fd = libc::open(
            c.as_ptr(),
            libc::O_RDONLY | libc::O_NOCTTY | libc::O_NONBLOCK,
        );
        assert!(
            fd >= 0,
            "open {} 失敗: {}",
            dev.0,
            std::io::Error::last_os_error()
        );
        let mut t: libc::termios = std::mem::zeroed();
        let rc = libc::tcgetattr(fd, &mut t);
        libc::close(fd);
        assert_eq!(rc, 0, "tcgetattr 失敗");
        std::slice::from_raw_parts(
            (&t as *const libc::termios).cast::<u8>(),
            std::mem::size_of::<libc::termios>(),
        )
        .to_vec()
    }
}

/// fakes 上的成對裝置工廠（每次呼叫新增一個裝置並回傳其 peer）。
pub fn fake_pair_factory(
    be: Arc<FakeSerialBackend>,
) -> impl Fn() -> (DevicePath, Box<dyn PeerEnd>) {
    let counter = Arc::new(AtomicUsize::new(0));
    move || {
        let id = counter.fetch_add(1, Ordering::SeqCst);
        let path = format!("/dev/ttyFAKEPAIR{id}");
        let dev = be.add_device(&path);
        (DevicePath(path), Box::new(FakePeer { dev }))
    }
}

struct FakePeer {
    dev: crate::fakes::serial::FakeDevice,
}

impl PeerEnd for FakePeer {
    fn read_available(&mut self, timeout: Duration) -> Vec<u8> {
        let deadline = Instant::now() + timeout;
        loop {
            let data = self.dev.take_tx();
            if !data.is_empty() {
                return data;
            }
            let now = Instant::now();
            if now >= deadline {
                return Vec::new();
            }
            let remaining = (deadline - now).min(Duration::from_millis(5));
            thread::sleep(remaining);
        }
    }

    fn write(&mut self, b: &[u8]) {
        self.dev.push_rx(b);
    }

    fn stop_reading(&mut self) {}

    fn resume_reading(&mut self) {}
}

impl Drop for FakePeer {
    fn drop(&mut self) {
        self.dev.close_peer();
    }
}

#[cfg(all(test, target_os = "linux"))]
mod pty_fd_tests {
    use super::*;

    /// P5 第 7 輪（cap_posix_pty__* 並行負載下間歇 `Held(pids:[同測試執行檔的子行程])`，本機重現 11/180）：夾具建立的
    /// PTY fd 須帶 CLOEXEC——否則同執行檔平行測試 spawn 的子行程（plain holder 等）會繼承 master／slave，被測端的鎖後
    /// holder 掃描把它當成外部 holder。以「建立後才 spawn 的子行程不持有 master」驗證。
    #[test]
    fn pty_pair_fds_are_not_inherited_by_spawned_children() {
        let (_dev, peer) = posix_pty_pair();
        // SAFETY: master 為 posix_pty_pair 回傳、仍由 peer 持有的有效 fd。
        let fd_flags = unsafe { libc::fcntl(peer.master, libc::F_GETFD) };
        assert!(fd_flags >= 0 && fd_flags & libc::FD_CLOEXEC != 0, "master 須帶 FD_CLOEXEC（flags={fd_flags}）");
        let mut child = Command::new("sleep")
            .arg("5")
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .expect("spawn sleep");
        // 以連結目標比對（PTY master 連到 ptmx）：子行程自己開的檔（例如 sleep 啟動時讀 locale 用到 fd 3）不得誤判。
        let master_link = std::fs::read_link(format!("/proc/self/fd/{}", peer.master)).expect("讀 master fd 連結");
        let inherited: Vec<String> = std::fs::read_dir(format!("/proc/{}/fd", child.id()))
            .map(|entries| {
                entries
                    .filter_map(|e| e.ok())
                    .filter(|e| std::fs::read_link(e.path()).ok().as_ref() == Some(&master_link))
                    .map(|e| e.file_name().to_string_lossy().into_owned())
                    .collect()
            })
            .unwrap_or_default();
        let _ = child.kill();
        let _ = child.wait();
        assert!(inherited.is_empty(), "子行程繼承了 PTY master（{}）：fd {inherited:?}", master_link.display());
    }
}
