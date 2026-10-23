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

pub use serial_suite::serial_suite;

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

/// `openpty` 建立一對 PTY：回傳 slave 路徑（`DevicePath`）與 master 對端。
/// 建立時先把 slave 設為 raw 並關閉本程序持有的 slave fd（路徑保留，master 持有 tty 物件）；
/// 否則父程序的 slave fd 會被子程序的鎖後重掃識別為外部 holder。
#[cfg(unix)]
pub fn posix_pty_pair() -> (DevicePath, PtyPeer) {
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
