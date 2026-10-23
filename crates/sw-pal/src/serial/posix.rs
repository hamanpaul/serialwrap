//! POSIX 序列埠後端（P5 Task 2，arch §4.2）。

use std::ffi::CString;
use std::os::fd::RawFd;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

#[cfg(any(test, feature = "test-fakes"))]
use std::cell::Cell;

pub use crate::proc_holders::posix::{probe_holders_in, PosixUartExclusive};
use crate::traits::{
    CloseImpl, CloseReceipt, CloseToken, DevicePath, HolderReport, HolderScanStatus, IoWaker,
    OpenError, PalError, PalResult, ReadOutcome, SerialBackend, SerialConfig, SerialHandle,
    SerialReader, SerialWriter, SubmitGate, SubmitRevoked, SubmitStep, TxOutcome, TxResult,
    UartExclusive,
};

/// POSIX 序列埠後端。`post_excl_fault` 只供 test-fakes 注入「TIOCEXCL 成功後」的失敗。
pub struct PosixSerialBackend {
    excl: Arc<dyn UartExclusive>,
    post_excl_fault: Option<PalError>,
}

impl PosixSerialBackend {
    /// 正式組裝（`native()`）使用的後端；建構本身不得失敗。
    pub fn new() -> Self {
        Self {
            excl: Arc::new(PosixUartExclusive),
            post_excl_fault: None,
        }
    }

    /// test-fakes only：TIOCEXCL 成功後立即回傳注入的錯誤（模擬 termios／eventfd 失敗）。
    #[cfg(feature = "test-fakes")]
    pub fn with_post_excl_fault(err: PalError) -> Self {
        Self {
            excl: Arc::new(PosixUartExclusive),
            post_excl_fault: Some(err),
        }
    }

    /// test-fakes only：替換鎖後重掃所用的 holder 掃描（僅供需預持 fd 的測試情境，正式路徑不得使用）。
    #[cfg(feature = "test-fakes")]
    pub fn with_uart_exclusive(excl: Arc<dyn UartExclusive>) -> Self {
        Self {
            excl,
            post_excl_fault: None,
        }
    }
}

impl SerialBackend for PosixSerialBackend {
    fn open_exclusive(
        &self,
        dev: &DevicePath,
        cfg: &SerialConfig,
    ) -> Result<Box<dyn SerialHandle>, OpenError> {
        if dev.0.as_bytes().contains(&0) {
            return Err(OpenError::Other(PalError::invalid("path contains NUL")));
        }
        let c_path = CString::new(dev.0.as_bytes())
            .map_err(|_| OpenError::Other(PalError::invalid("path contains NUL")))?;
        let mut ops = PosixOpenOps::new(
            c_path,
            dev.0.clone(),
            cfg,
            &*self.excl,
            self.post_excl_fault.clone(),
        );
        if let Err(error) = open_sequence(&mut ops, &dev.0) {
            return Err(match ops.injected_error.take() {
                Some(injected) => OpenError::Other(injected),
                None => error,
            });
        }
        let fd = ops.take_fd().expect("open_sequence 成功時必須保留 tty fd");
        let eio_is_pty_hangup = is_pty_slave_fd(fd);

        let wake_r = match WakeChannel::new() {
            Ok(channel) => channel,
            Err(code) => {
                close_partial_open(fd, dev.0.clone(), None, None);
                return Err(OpenError::Other(PalError::from_errno(code)));
            }
        };
        let wake_w = match WakeChannel::new() {
            Ok(channel) => channel,
            Err(code) => {
                close_partial_open(fd, dev.0.clone(), Some(wake_r), None);
                return Err(OpenError::Other(PalError::from_errno(code)));
            }
        };
        let waker = Arc::new(PosixIoWaker::new(Some(&wake_r), Some(&wake_w)));
        let close = Arc::new(Mutex::new(PosixClose {
            fd: Some(fd),
            path: dev.0.clone(),
            wake_r: Some(wake_r),
            wake_w: Some(wake_w),
            waker: Arc::clone(&waker),
        }));
        let (wake_r_fd, wake_w_fd, wake_r_eventfd, wake_w_eventfd) = {
            let state = close
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner);
            let wake_r = state.wake_r.as_ref().expect("wake_r");
            let wake_w = state.wake_w.as_ref().expect("wake_w");
            (
                wake_r.read_fd,
                wake_w.read_fd,
                wake_r.eventfd,
                wake_w.eventfd,
            )
        };
        Ok(Box::new(PosixSerialHandle {
            close,
            wake_r_fd,
            wake_w_fd,
            wake_r_eventfd,
            wake_w_eventfd,
            eio_is_pty_hangup,
            waker,
        }))
    }
}

/// 鎖後重掃的判定（規格 §6.2；P1 T10 凍結規則）。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PostLockVerdict {
    Reject,
    Accept,
    AcceptUnverified,
}

/// 依 `pids` 與 `status` 判定鎖後重掃結果：`pids` 非空即 Reject（不論 status）。
pub fn post_lock_holder_verdict(rep: &HolderReport) -> PostLockVerdict {
    if !rep.pids.is_empty() {
        PostLockVerdict::Reject
    } else if rep.status == HolderScanStatus::Ok {
        PostLockVerdict::Accept
    } else {
        PostLockVerdict::AcceptUnverified
    }
}

/// P1 T10 凍結規則：`holder_unknown` 只由 `pids` 是否為空決定，`status` 只作診斷。
pub fn finalize_holder_report(rep: HolderReport) -> HolderReport {
    HolderReport {
        holder_unknown: rep.pids.is_empty(),
        ..rep
    }
}

/// 由 `SerialConfig` 建出 termios：`c_iflag=c_oflag=c_lflag=0`、`c_cflag` 覆寫為
/// `CREAD|CLOCAL|CSx[|PARENB[|PARODD]][|CSTOPB][|CRTSCTS]`，`c_cc` 不動（arch §4.2）。
pub fn build_termios(input: libc::termios, cfg: &SerialConfig) -> libc::termios {
    let mut attrs = input;
    attrs.c_iflag = 0;
    attrs.c_oflag = 0;
    attrs.c_lflag = 0;
    let mut cflag = libc::CREAD | libc::CLOCAL;
    cflag |= if cfg.data_bits == 7 {
        libc::CS7
    } else {
        libc::CS8
    };
    match cfg.parity {
        crate::traits::Parity::None => {}
        crate::traits::Parity::Even => cflag |= libc::PARENB,
        crate::traits::Parity::Odd => cflag |= libc::PARENB | libc::PARODD,
    }
    if cfg.stop_bits == 2 {
        cflag |= libc::CSTOPB;
    }
    if cfg.flow == crate::traits::FlowControl::RtsCts {
        cflag |= libc::CRTSCTS;
    }
    attrs.c_cflag = cflag;
    attrs
}

/// 未知 baud 回退 `B115200`（UIO-12.1 KEEP）。
pub fn speed_for_baud(baud: u32) -> libc::speed_t {
    mapped_speed(baud).unwrap_or(libc::B115200)
}

/// `set_baud` 的 termios 存取抽象（測試以錄製替身取代 libc）。
pub trait TtyTermiosOps {
    fn tcgetattr(&mut self) -> Result<libc::termios, i32>;
    fn tcsetattr(&mut self, t: &libc::termios) -> Result<(), i32>;
}

/// 依 baud 設定速率；未知 baud 規則見 `unknown_baud_falls_back_115200` 單元測試。
pub fn set_baud_with(ops: &mut dyn TtyTermiosOps, baud: u32) -> PalResult<()> {
    let Some(speed) = mapped_speed(baud) else {
        return Ok(());
    };
    let mut attrs = ops.tcgetattr().map_err(PalError::from_errno)?;
    // SAFETY: `attrs` 是 tcgetattr 產生的有效 termios 結構。
    if unsafe { libc::cfsetispeed(&mut attrs, speed) } != 0 {
        return Err(PalError::from_errno(errno()));
    }
    // SAFETY: `attrs` 是 tcgetattr 產生的有效 termios 結構。
    if unsafe { libc::cfsetospeed(&mut attrs, speed) } != 0 {
        return Err(PalError::from_errno(errno()));
    }
    ops.tcsetattr(&attrs).map_err(PalError::from_errno)
}

/// `CloseToken::close` 的系統呼叫序列（記錄順序即為 `CloseOp` 順序）。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CloseOp {
    Nxcl,
    FlockUnlock,
    CloseFd,
    CloseWakeR,
    CloseWakeW,
}

/// 關閉序的 OS 操作抽象（arch §4.2，R0-F3）。
pub trait TtyCloseOps {
    /// `ioctl(fd, TIOCNXCL)`；失敗回 errno。
    fn nxcl(&mut self) -> Result<(), i32>;
    fn flock_unlock(&mut self);
    /// `close(fd)`；失敗回 errno。
    fn close_fd(&mut self) -> Result<(), i32>;
    fn close_wake_r(&mut self);
    fn close_wake_w(&mut self);
    /// daemon stderr WARNING（整行，不含換行）。
    fn warn(&mut self, line: &str);
}

/// 關閉序純邏輯核心：`Nxcl` 回 `EIO`／`ENXIO`／`ENODEV` 視為已解除（`excl_cleared=true`、不警告）；
/// 其他 errno 照序繼續關閉、`excl_cleared=false`、`error` 為該 errno、警告一筆；`has_wake=false` 時略過 wake fd 兩步。
pub fn close_sequence(ops: &mut dyn TtyCloseOps, dev: &str, has_wake: bool) -> CloseReceipt {
    let mut excl_cleared = true;
    let mut error = None;
    if let Err(code) = ops.nxcl() {
        if !is_hangup_errno(code) {
            excl_cleared = false;
            let os = PalError::from_errno(code);
            ops.warn(&format!(
                "serialwrapd: TIOCNXCL failed on {dev}: {}",
                os.py_message
            ));
            error = Some(os);
        }
    }
    ops.flock_unlock();
    let closed = match ops.close_fd() {
        Ok(()) => true,
        Err(code) => {
            if error.is_none() {
                error = Some(PalError::from_errno(code));
            }
            false
        }
    };
    if has_wake {
        ops.close_wake_r();
        ops.close_wake_w();
    }
    CloseReceipt {
        closed,
        excl_cleared,
        error,
    }
}

/// 開啟序的 OS 操作抽象（arch §4.2；`open_sequence` 的純邏輯核心）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum OpenOp {
    Open,
    Flock,
    Excl,
    ProbeHolders,
    Termios(Result<(), i32>),
    Nxcl,
    CloseFd,
}

pub trait TtyOpenOps {
    /// `open(O_RDWR|O_NOCTTY|O_NONBLOCK|O_CLOEXEC)`；失敗回 errno。
    fn open(&mut self) -> Result<(), i32>;
    /// `flock(LOCK_EX|LOCK_NB)`；`EWOULDBLOCK` → `Held`。
    fn flock(&mut self) -> Result<(), i32>;
    /// `ioctl(fd, TIOCEXCL)`。
    fn excl(&mut self) -> Result<(), i32>;
    /// 僅供測試在 `TIOCEXCL` 成功後立即注入失敗；正式實作固定回成功。
    fn after_excl(&mut self) -> Result<(), i32> {
        Ok(())
    }
    /// 鎖後重掃（排除自身 pid）。
    fn probe_holders(&mut self) -> HolderReport;
    /// `tcgetattr` → `build_termios` → `tcsetattr(TCSANOW)`。
    fn termios(&mut self) -> Result<(), i32>;
    fn nxcl(&mut self) -> Result<(), i32>;
    fn close_fd(&mut self) -> Result<(), i32>;
    fn warn(&mut self, line: &str);
}

/// 開啟序純邏輯核心：步驟順序為 open → flock → TIOCEXCL → 鎖後重掃 → termios；
/// TIOCEXCL 之後的任何失敗必先 TIOCNXCL 再 close(fd)，TIOCEXCL 之前的失敗不得出現 TIOCNXCL。
pub fn open_sequence(ops: &mut dyn TtyOpenOps, dev: &str) -> Result<(), OpenError> {
    if let Err(code) = ops.open() {
        return Err(open_error(code, || ops.probe_holders()));
    }
    if let Err(code) = ops.flock() {
        let error = if code == libc::EWOULDBLOCK || code == libc::EAGAIN {
            OpenError::Held(finalize_holder_report(ops.probe_holders()))
        } else {
            OpenError::Other(PalError::from_errno(code))
        };
        let _ = ops.close_fd();
        return Err(error);
    }
    if let Err(code) = ops.excl() {
        let _ = ops.close_fd();
        return Err(OpenError::Other(PalError::from_errno(code)));
    }
    if let Err(code) = ops.after_excl() {
        cleanup_open_failure(ops, dev);
        return Err(OpenError::Other(PalError::from_errno(code)));
    }

    let holders = finalize_holder_report(ops.probe_holders());
    match post_lock_holder_verdict(&holders) {
        PostLockVerdict::Reject => {
            cleanup_open_failure(ops, dev);
            return Err(OpenError::Held(holders));
        }
        PostLockVerdict::AcceptUnverified => ops.warn(&format!(
            "serialwrap: 無法確認 {dev} 的外部 holder（status={:?}），BC-04 僅保證 serialwrap 實例互斥",
            holders.status
        )),
        PostLockVerdict::Accept => {}
    }

    if let Err(code) = ops.termios() {
        cleanup_open_failure(ops, dev);
        return Err(OpenError::Other(PalError::from_errno(code)));
    }
    Ok(())
}

fn open_error(code: i32, mut holders: impl FnMut() -> HolderReport) -> OpenError {
    match code {
        libc::ENOENT | libc::ENODEV | libc::ENXIO => {
            OpenError::NotFound(PalError::from_errno(code))
        }
        libc::EBUSY => OpenError::Held(finalize_holder_report(holders())),
        libc::EACCES | libc::EPERM => OpenError::AccessDenied {
            os: PalError::from_errno(code),
            holder: finalize_holder_report(holders()),
        },
        _ => OpenError::Other(PalError::from_errno(code)),
    }
}

fn cleanup_open_failure(ops: &mut dyn TtyOpenOps, dev: &str) {
    if let Err(code) = ops.nxcl() {
        if !is_hangup_errno(code) {
            let error = PalError::from_errno(code);
            ops.warn(&format!(
                "serialwrapd: TIOCNXCL failed on {dev}: {}",
                error.py_message
            ));
        }
    }
    let _ = ops.close_fd();
}

fn is_hangup_errno(code: i32) -> bool {
    code == libc::EIO || code == libc::ENXIO || code == libc::ENODEV
}

fn errno() -> i32 {
    std::io::Error::last_os_error()
        .raw_os_error()
        .unwrap_or(libc::EIO)
}

fn mapped_speed(baud: u32) -> Option<libc::speed_t> {
    Some(match baud {
        9600 => libc::B9600,
        19200 => libc::B19200,
        38400 => libc::B38400,
        57600 => libc::B57600,
        115200 => libc::B115200,
        #[cfg(target_os = "linux")]
        230400 => libc::B230400,
        #[cfg(target_os = "linux")]
        460800 => libc::B460800,
        #[cfg(target_os = "linux")]
        921600 => libc::B921600,
        _ => return None,
    })
}

struct PosixOpenOps<'a> {
    c_path: CString,
    path: String,
    cfg: &'a SerialConfig,
    excl: &'a dyn UartExclusive,
    post_excl_fault: Option<PalError>,
    injected_error: Option<PalError>,
    fd: Option<RawFd>,
    excl_armed: bool,
}

impl<'a> PosixOpenOps<'a> {
    fn new(
        c_path: CString,
        path: String,
        cfg: &'a SerialConfig,
        excl: &'a dyn UartExclusive,
        post_excl_fault: Option<PalError>,
    ) -> Self {
        Self {
            c_path,
            path,
            cfg,
            excl,
            post_excl_fault,
            injected_error: None,
            fd: None,
            excl_armed: false,
        }
    }

    fn fd(&self) -> Result<RawFd, i32> {
        self.fd.ok_or(libc::EBADF)
    }

    fn take_fd(&mut self) -> Option<RawFd> {
        self.excl_armed = false;
        self.fd.take()
    }
}

impl TtyOpenOps for PosixOpenOps<'_> {
    fn open(&mut self) -> Result<(), i32> {
        // SAFETY: `c_path` 為有效 NUL 結尾字串；回傳 fd 由本結構持有。
        let fd = unsafe {
            libc::open(
                self.c_path.as_ptr(),
                libc::O_RDWR | libc::O_NOCTTY | libc::O_NONBLOCK | libc::O_CLOEXEC,
            )
        };
        if fd < 0 {
            return Err(errno());
        }
        self.fd = Some(fd);
        Ok(())
    }

    fn flock(&mut self) -> Result<(), i32> {
        let fd = self.fd()?;
        // SAFETY: fd 由本結構持有，flock 不保留使用者指標。
        if unsafe { libc::flock(fd, libc::LOCK_EX | libc::LOCK_NB) } == 0 {
            Ok(())
        } else {
            Err(errno())
        }
    }

    fn excl(&mut self) -> Result<(), i32> {
        let fd = self.fd()?;
        // SAFETY: fd 為本結構持有的 tty fd。
        if unsafe { libc::ioctl(fd, libc::TIOCEXCL) } == 0 {
            self.excl_armed = true;
            Ok(())
        } else {
            Err(errno())
        }
    }

    fn probe_holders(&mut self) -> HolderReport {
        finalize_holder_report(self.excl.probe_holders(&self.path))
    }

    fn after_excl(&mut self) -> Result<(), i32> {
        if let Some(error) = self.post_excl_fault.take() {
            self.injected_error = Some(error.clone());
            return Err(error.os_code.unwrap_or(libc::EIO));
        }
        Ok(())
    }

    fn termios(&mut self) -> Result<(), i32> {
        apply_termios(self.fd()?, self.cfg)
    }

    fn nxcl(&mut self) -> Result<(), i32> {
        let fd = self.fd()?;
        // SAFETY: fd 為本結構持有的 tty fd。
        if unsafe { libc::ioctl(fd, libc::TIOCNXCL) } == 0 {
            self.excl_armed = false;
            Ok(())
        } else {
            let code = errno();
            if is_hangup_errno(code) {
                self.excl_armed = false;
            }
            Err(code)
        }
    }

    fn close_fd(&mut self) -> Result<(), i32> {
        let Some(fd) = self.fd.take() else {
            return Ok(());
        };
        self.excl_armed = false;
        close_raw(fd)
    }

    fn warn(&mut self, line: &str) {
        eprintln!("{line}");
    }
}

impl Drop for PosixOpenOps<'_> {
    fn drop(&mut self) {
        let Some(fd) = self.fd.take() else {
            return;
        };
        if self.excl_armed {
            // SAFETY: fd 仍由本結構持有，drop 時先解除 tty 獨占旗標。
            if unsafe { libc::ioctl(fd, libc::TIOCNXCL) } != 0 {
                let code = errno();
                if !is_hangup_errno(code) {
                    eprintln!(
                        "serialwrapd: TIOCNXCL failed on {}: {}",
                        self.path,
                        PalError::from_errno(code).py_message
                    );
                }
            }
        }
        // SAFETY: fd 仍由本結構持有；關閉也會釋放 flock。
        unsafe {
            libc::flock(fd, libc::LOCK_UN);
            libc::close(fd);
        }
    }
}

fn close_raw(fd: RawFd) -> Result<(), i32> {
    // SAFETY: 呼叫端保證 fd 尚未關閉且僅呼叫一次。
    if unsafe { libc::close(fd) } == 0 {
        Ok(())
    } else {
        Err(errno())
    }
}

fn apply_termios(fd: RawFd, cfg: &SerialConfig) -> Result<(), i32> {
    // SAFETY: `attrs` 是 tcgetattr 的輸出位置，fd 由呼叫端持有。
    let mut attrs: libc::termios = unsafe { std::mem::zeroed() };
    if unsafe { libc::tcgetattr(fd, &mut attrs) } != 0 {
        return Err(errno());
    }
    attrs = build_termios(attrs, cfg);
    let speed = speed_for_baud(cfg.baud);
    // SAFETY: `attrs` 是剛由 tcgetattr 取得並更新的有效 termios 結構。
    if unsafe { libc::cfsetispeed(&mut attrs, speed) } != 0 {
        return Err(errno());
    }
    // SAFETY: `attrs` 是剛由 tcgetattr 取得並更新的有效 termios 結構。
    if unsafe { libc::cfsetospeed(&mut attrs, speed) } != 0 {
        return Err(errno());
    }
    // SAFETY: fd 仍開啟，attrs 為有效的 termios 結構。
    if unsafe { libc::tcsetattr(fd, libc::TCSANOW, &attrs) } != 0 {
        return Err(errno());
    }
    Ok(())
}

struct LibcTermiosOps {
    fd: RawFd,
}

impl TtyTermiosOps for LibcTermiosOps {
    fn tcgetattr(&mut self) -> Result<libc::termios, i32> {
        // SAFETY: `attrs` 是 tcgetattr 的輸出位置，fd 由 serial handle 持有。
        let mut attrs: libc::termios = unsafe { std::mem::zeroed() };
        if unsafe { libc::tcgetattr(self.fd, &mut attrs) } == 0 {
            Ok(attrs)
        } else {
            Err(errno())
        }
    }

    fn tcsetattr(&mut self, attrs: &libc::termios) -> Result<(), i32> {
        // SAFETY: fd 由 serial handle 持有，attrs 是有效 termios 結構。
        if unsafe { libc::tcsetattr(self.fd, libc::TCSANOW, attrs) } == 0 {
            Ok(())
        } else {
            Err(errno())
        }
    }
}

#[derive(Debug)]
struct WakeChannel {
    read_fd: RawFd,
    write_fd: RawFd,
    eventfd: bool,
}

impl WakeChannel {
    fn new() -> Result<Self, i32> {
        #[cfg(target_os = "linux")]
        {
            // SAFETY: eventfd 不接收指標，回傳 fd 由本結構持有。
            let fd = unsafe { libc::eventfd(0, libc::EFD_NONBLOCK | libc::EFD_CLOEXEC) };
            if fd < 0 {
                return Err(errno());
            }
            return Ok(Self {
                read_fd: fd,
                write_fd: fd,
                eventfd: true,
            });
        }
        #[cfg(not(target_os = "linux"))]
        {
            let mut fds = [-1; 2];
            // SAFETY: fds 指向兩個有效整數欄位。
            if unsafe { libc::pipe(fds.as_mut_ptr()) } != 0 {
                return Err(errno());
            }
            for fd in fds {
                if let Err(code) = set_fd_flags(fd) {
                    // SAFETY: pipe 回傳的兩個 fd 尚未移交且各關閉一次。
                    unsafe {
                        libc::close(fds[0]);
                        libc::close(fds[1]);
                    }
                    return Err(code);
                }
            }
            Ok(Self {
                read_fd: fds[0],
                write_fd: fds[1],
                eventfd: false,
            })
        }
    }
}

impl Drop for WakeChannel {
    fn drop(&mut self) {
        // SAFETY: 此 channel 獨占持有 wake fd；eventfd 的讀寫 fd 是同一個。
        unsafe {
            libc::close(self.read_fd);
            if self.write_fd != self.read_fd {
                libc::close(self.write_fd);
            }
        }
    }
}

#[cfg(not(target_os = "linux"))]
fn set_fd_flags(fd: RawFd) -> Result<(), i32> {
    // SAFETY: fcntl 不保留指標，fd 由呼叫端持有。
    let status = unsafe { libc::fcntl(fd, libc::F_GETFL) };
    if status < 0 || unsafe { libc::fcntl(fd, libc::F_SETFL, status | libc::O_NONBLOCK) } < 0 {
        return Err(errno());
    }
    let descriptor = unsafe { libc::fcntl(fd, libc::F_GETFD) };
    if descriptor < 0
        || unsafe { libc::fcntl(fd, libc::F_SETFD, descriptor | libc::FD_CLOEXEC) } < 0
    {
        return Err(errno());
    }
    Ok(())
}

fn signal_fd(fd: RawFd, eventfd: bool) {
    if fd < 0 {
        return;
    }
    loop {
        let event = 1u64;
        let byte = [1u8];
        let (ptr, len) = if eventfd {
            ((&event as *const u64).cast(), std::mem::size_of::<u64>())
        } else {
            (byte.as_ptr().cast(), byte.len())
        };
        // SAFETY: ptr 指向 len 位元組的有效緩衝區，fd 是非阻塞 wake fd。
        let rc = unsafe { libc::write(fd, ptr, len) };
        if rc >= 0 {
            return;
        }
        let code = errno();
        if code == libc::EINTR {
            continue;
        }
        // EAGAIN 表示已有未消耗的喚醒資料，等待者已可讀。
        return;
    }
}

fn drain_fd(fd: RawFd, eventfd: bool) {
    let mut buf = [0u8; 128];
    loop {
        let len = if eventfd {
            std::mem::size_of::<u64>()
        } else {
            buf.len()
        };
        // SAFETY: buf 至少有 len 位元組，fd 是非阻塞 wake fd。
        let rc = unsafe { libc::read(fd, buf.as_mut_ptr().cast(), len) };
        if rc > 0 {
            continue;
        }
        if rc < 0 && errno() == libc::EINTR {
            continue;
        }
        return;
    }
}

struct PosixWakeFds {
    wake_r_write: RawFd,
    wake_w_write: RawFd,
}

struct PosixIoWaker {
    wake_fds: Mutex<PosixWakeFds>,
    wake_r_eventfd: bool,
    wake_w_eventfd: bool,
}

impl PosixIoWaker {
    fn new(wake_r: Option<&WakeChannel>, wake_w: Option<&WakeChannel>) -> Self {
        Self {
            wake_fds: Mutex::new(PosixWakeFds {
                wake_r_write: wake_r.map_or(-1, |channel| channel.write_fd),
                wake_w_write: wake_w.map_or(-1, |channel| channel.write_fd),
            }),
            wake_r_eventfd: wake_r.is_some_and(|channel| channel.eventfd),
            wake_w_eventfd: wake_w.is_some_and(|channel| channel.eventfd),
        }
    }

    fn disable_r(&self) {
        self.wake_fds
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .wake_r_write = -1;
    }

    fn disable_w(&self) {
        self.wake_fds
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .wake_w_write = -1;
    }
}

impl IoWaker for PosixIoWaker {
    fn wake(&self) {
        let fds = self
            .wake_fds
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        signal_fd(fds.wake_r_write, self.wake_r_eventfd);
        signal_fd(fds.wake_w_write, self.wake_w_eventfd);
    }
}

struct PosixClose {
    fd: Option<RawFd>,
    path: String,
    wake_r: Option<WakeChannel>,
    wake_w: Option<WakeChannel>,
    waker: Arc<PosixIoWaker>,
}

impl TtyCloseOps for PosixClose {
    fn nxcl(&mut self) -> Result<(), i32> {
        let Some(fd) = self.fd else {
            return Err(libc::EBADF);
        };
        // SAFETY: fd 是尚未關閉的 tty descriptor。
        if unsafe { libc::ioctl(fd, libc::TIOCNXCL) } == 0 {
            Ok(())
        } else {
            Err(errno())
        }
    }

    fn flock_unlock(&mut self) {
        if let Some(fd) = self.fd {
            // SAFETY: fd 是尚未關閉的 tty descriptor。
            unsafe {
                libc::flock(fd, libc::LOCK_UN);
            }
        }
    }

    fn close_fd(&mut self) -> Result<(), i32> {
        match self.fd.take() {
            Some(fd) => close_raw(fd),
            None => Ok(()),
        }
    }

    fn close_wake_r(&mut self) {
        self.waker.disable_r();
        drop(self.wake_r.take());
    }

    fn close_wake_w(&mut self) {
        self.waker.disable_w();
        drop(self.wake_w.take());
    }

    fn warn(&mut self, line: &str) {
        eprintln!("{line}");
    }
}

struct PosixCloseToken {
    close: Arc<Mutex<PosixClose>>,
}

impl CloseImpl for PosixCloseToken {
    fn close(self: Box<Self>) -> CloseReceipt {
        let mut close = self
            .close
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        let path = close.path.clone();
        close_sequence(&mut *close, &path, true)
    }
}

impl Drop for PosixClose {
    fn drop(&mut self) {
        if self.fd.is_some() || self.wake_r.is_some() || self.wake_w.is_some() {
            let path = self.path.clone();
            let _ = close_sequence(self, &path, true);
        }
    }
}

fn close_partial_open(
    fd: RawFd,
    path: String,
    wake_r: Option<WakeChannel>,
    wake_w: Option<WakeChannel>,
) {
    let waker = Arc::new(PosixIoWaker::new(wake_r.as_ref(), wake_w.as_ref()));
    let mut close = PosixClose {
        fd: Some(fd),
        path,
        wake_r,
        wake_w,
        waker,
    };
    let path = close.path.clone();
    let _ = close_sequence(&mut close, &path, true);
}

struct PosixSerialHandle {
    close: Arc<Mutex<PosixClose>>,
    wake_r_fd: RawFd,
    wake_w_fd: RawFd,
    wake_r_eventfd: bool,
    wake_w_eventfd: bool,
    eio_is_pty_hangup: bool,
    waker: Arc<PosixIoWaker>,
}

impl SerialHandle for PosixSerialHandle {
    fn split(
        self: Box<Self>,
    ) -> (
        Box<dyn SerialReader>,
        Box<dyn SerialWriter>,
        Arc<dyn IoWaker>,
        CloseToken,
    ) {
        let waker: Arc<dyn IoWaker> = Arc::clone(&self.waker) as Arc<dyn IoWaker>;
        let fd = self
            .close
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .fd
            .expect("open serial fd");
        let reader = Box::new(PosixSerialReader {
            fd,
            wake_fd: self.wake_r_fd,
            wake_eventfd: self.wake_r_eventfd,
            eio_is_pty_hangup: self.eio_is_pty_hangup,
            _close: Arc::clone(&self.close),
        });
        let writer = Box::new(PosixSerialWriter {
            fd,
            wake_fd: self.wake_w_fd,
            wake_eventfd: self.wake_w_eventfd,
            _close: Arc::clone(&self.close),
        });
        let close = Box::new(PosixCloseToken {
            close: Arc::clone(&self.close),
        });
        (reader, writer, waker, CloseToken::new(close))
    }

    fn set_baud(&self, baud: u32) -> PalResult<()> {
        let fd = self
            .close
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .fd
            .ok_or_else(|| PalError::from_errno(libc::EBADF))?;
        let mut ops = LibcTermiosOps { fd };
        set_baud_with(&mut ops, baud)
    }

    fn is_alive(&self) -> bool {
        let close = self
            .close
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        let Some(fd) = close.fd else {
            return false;
        };
        // SAFETY：共用的關閉狀態持有 fd，直到 reader/writer 收斂或 close token 明確關閉。
        let mut st: libc::stat = unsafe { std::mem::zeroed() };
        unsafe { libc::fstat(fd, &mut st) == 0 }
    }
}

struct PosixSerialReader {
    fd: RawFd,
    wake_fd: RawFd,
    wake_eventfd: bool,
    eio_is_pty_hangup: bool,
    _close: Arc<Mutex<PosixClose>>,
}

impl SerialReader for PosixSerialReader {
    fn read(&mut self, buf: &mut [u8], timeout: Option<Duration>) -> PalResult<ReadOutcome> {
        if buf.is_empty() {
            return Ok(ReadOutcome::Data(0));
        }
        let start = Instant::now();
        loop {
            let timeout_ms = remaining_timeout_ms(start, timeout);
            let mut fds = [
                libc::pollfd {
                    fd: self.fd,
                    events: libc::POLLIN,
                    revents: 0,
                },
                libc::pollfd {
                    fd: self.wake_fd,
                    events: libc::POLLIN,
                    revents: 0,
                },
            ];
            // SAFETY: fds 是有效的兩個 pollfd 結構，timeout 為有限毫秒數或 -1。
            let rc = unsafe {
                libc::poll(
                    fds.as_mut_ptr(),
                    fds.len() as libc::nfds_t,
                    timeout_ms.unwrap_or(-1),
                )
            };
            if rc == 0 {
                return Ok(ReadOutcome::TimedOut);
            }
            if rc < 0 {
                let code = errno();
                if code == libc::EINTR {
                    continue;
                }
                return Err(PalError::from_errno(code));
            }
            if fds[1].revents & (libc::POLLIN | libc::POLLHUP | libc::POLLERR) != 0 {
                drain_fd(self.wake_fd, self.wake_eventfd);
                return Ok(ReadOutcome::Woken);
            }
            if fds[0].revents & (libc::POLLIN | libc::POLLHUP | libc::POLLERR) != 0 {
                // SAFETY: buf 是有效輸出緩衝區，fd 由共用的關閉狀態持有。
                let n = unsafe { libc::read(self.fd, buf.as_mut_ptr().cast(), buf.len()) };
                match classify_read_result(n, (n < 0).then(errno), self.eio_is_pty_hangup)? {
                    Some(outcome) => return Ok(outcome),
                    None => continue,
                }
            }
            if fds[0].revents & libc::POLLNVAL != 0 {
                return Err(PalError::from_errno(libc::EBADF));
            }
        }
    }
}

struct PosixSerialWriter {
    fd: RawFd,
    wake_fd: RawFd,
    wake_eventfd: bool,
    _close: Arc<Mutex<PosixClose>>,
}

impl SerialWriter for PosixSerialWriter {
    fn write_gated(
        &mut self,
        data: &[u8],
        cancel: &crate::traits::CancelToken,
        gate: &dyn SubmitGate,
    ) -> TxResult {
        #[cfg(any(test, feature = "test-fakes"))]
        debug_begin_tx_write_count();
        let mut sent = 0usize;
        loop {
            if cancel.is_cancelled() {
                return TxResult {
                    outcome: TxOutcome::Completed(sent),
                    error: Some(PalError::cancelled()),
                };
            }
            if sent == data.len() {
                return TxResult {
                    outcome: TxOutcome::Completed(sent),
                    error: None,
                };
            }
            let fd = self.fd;
            let step = gate.submit(&mut || match retry_eintr(|| {
                let n = raw_tx_write(fd, &data[sent..]);
                if n < 0 {
                    Err(errno())
                } else {
                    Ok(n)
                }
            }) {
                Ok(n) if n > 0 => SubmitStep::Accepted(n as usize),
                Ok(0) => SubmitStep::Failed(PalError::io(0, "write returned 0")),
                Err(code) if code == libc::EAGAIN || code == libc::EWOULDBLOCK => {
                    SubmitStep::WouldBlock
                }
                Err(code) => SubmitStep::Failed(PalError::from_errno(code)),
                Ok(_) => unreachable!("write 回傳值不得小於零"),
            });
            match step {
                Err(SubmitRevoked) if sent == 0 => {
                    return TxResult {
                        outcome: TxOutcome::NotSubmitted,
                        error: None,
                    };
                }
                Err(SubmitRevoked) => {
                    return TxResult {
                        outcome: TxOutcome::Completed(sent),
                        error: Some(PalError::cancelled()),
                    };
                }
                Ok(SubmitStep::Accepted(n)) if n > 0 => sent += n,
                Ok(SubmitStep::Accepted(_)) => {
                    return TxResult {
                        outcome: TxOutcome::Completed(sent),
                        error: Some(PalError::io(0, "write returned 0")),
                    };
                }
                Ok(SubmitStep::WouldBlock) => {
                    if let Err(error) = poll_out_or_wake(self.fd, self.wake_fd, self.wake_eventfd) {
                        return TxResult {
                            outcome: TxOutcome::Completed(sent),
                            error: Some(error),
                        };
                    }
                }
                Ok(SubmitStep::Failed(error)) => {
                    return TxResult {
                        outcome: TxOutcome::Completed(sent),
                        error: Some(error),
                    };
                }
                Ok(SubmitStep::Pending) => unreachable!("POSIX write 不會產生 Pending"),
            }
        }
    }
}

fn poll_out_or_wake(fd: RawFd, wake_fd: RawFd, wake_eventfd: bool) -> PalResult<()> {
    loop {
        let mut fds = [
            libc::pollfd {
                fd,
                events: libc::POLLOUT,
                revents: 0,
            },
            libc::pollfd {
                fd: wake_fd,
                events: libc::POLLIN,
                revents: 0,
            },
        ];
        // SAFETY: fds 是有效的兩個 pollfd 結構，-1 表示無逾時。
        let rc = unsafe { libc::poll(fds.as_mut_ptr(), fds.len() as libc::nfds_t, -1) };
        if rc < 0 {
            let code = errno();
            if code == libc::EINTR {
                continue;
            }
            return Err(PalError::from_errno(code));
        }
        if fds[1].revents & (libc::POLLIN | libc::POLLHUP | libc::POLLERR) != 0 {
            drain_fd(wake_fd, wake_eventfd);
            return Ok(());
        }
        if fds[0].revents & libc::POLLOUT != 0 {
            return Ok(());
        }
        if fds[0].revents & (libc::POLLERR | libc::POLLHUP | libc::POLLNVAL) != 0 {
            return Err(PalError::from_errno(libc::EIO));
        }
    }
}

fn remaining_timeout_ms(start: Instant, timeout: Option<Duration>) -> Option<i32> {
    let timeout = timeout?;
    let elapsed = start.elapsed();
    if elapsed >= timeout {
        return Some(0);
    }
    let remaining = timeout - elapsed;
    let millis = remaining.as_millis();
    let rounded = millis + u128::from(remaining.subsec_nanos() % 1_000_000 != 0);
    Some(rounded.clamp(1, i32::MAX as u128) as i32)
}

fn classify_read_result(
    n: isize,
    error: Option<i32>,
    eio_is_pty_hangup: bool,
) -> Result<Option<ReadOutcome>, PalError> {
    if n > 0 {
        return Ok(Some(ReadOutcome::Data(n as usize)));
    }
    if n == 0 {
        return Ok(Some(ReadOutcome::Eof));
    }
    let code = error.unwrap_or(libc::EIO);
    if code == libc::EIO && eio_is_pty_hangup {
        return Ok(Some(ReadOutcome::Eof));
    }
    if code == libc::EAGAIN || code == libc::EWOULDBLOCK || code == libc::EINTR {
        return Ok(None);
    }
    Err(PalError::from_errno(code))
}

fn is_pty_slave_fd(fd: RawFd) -> bool {
    #[cfg(target_os = "linux")]
    {
        let mut stat: libc::stat = unsafe { std::mem::zeroed() };
        // SAFETY: stat 是有效輸出位置，fd 仍由共用的關閉狀態持有。
        if unsafe { libc::fstat(fd, &mut stat) } != 0
            || stat.st_mode & libc::S_IFMT != libc::S_IFCHR
        {
            return false;
        }
        return (136..=143).contains(&libc::major(stat.st_rdev));
    }
    #[cfg(not(target_os = "linux"))]
    {
        let _ = fd;
        false
    }
}

fn retry_eintr<T>(mut operation: impl FnMut() -> Result<T, i32>) -> Result<T, i32> {
    loop {
        match operation() {
            Err(code) if code == libc::EINTR => continue,
            result => return result,
        }
    }
}

fn raw_tx_write(fd: RawFd, data: &[u8]) -> isize {
    #[cfg(any(test, feature = "test-fakes"))]
    debug_record_tx_write_syscall();
    // SAFETY: fd 由共用的關閉狀態持有，切片在本次同步系統呼叫期間有效。
    unsafe { libc::write(fd, data.as_ptr().cast(), data.len()) }
}

#[cfg(any(test, feature = "test-fakes"))]
thread_local! {
    static DEBUG_TX_WRITE_COUNT: Cell<Option<usize>> = const { Cell::new(None) };
}

#[cfg(any(test, feature = "test-fakes"))]
fn debug_begin_tx_write_count() {
    DEBUG_TX_WRITE_COUNT.with(|count| count.set(Some(0)));
}

#[cfg(any(test, feature = "test-fakes"))]
fn debug_record_tx_write_syscall() {
    DEBUG_TX_WRITE_COUNT.with(|count| {
        if let Some(value) = count.get() {
            count.set(Some(value + 1));
        }
    });
}

#[cfg(any(test, feature = "test-fakes"))]
pub(crate) fn take_debug_tx_write_count() -> Option<usize> {
    DEBUG_TX_WRITE_COUNT.with(|count| count.replace(None))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::traits::{FlowControl, HolderScanStatus, PalErrorKind, Parity};
    use std::ffi::CStr;
    use std::os::fd::{AsRawFd, FromRawFd, OwnedFd};
    use std::os::unix::fs::PermissionsExt;
    use std::path::Path;

    fn raw_cfg(
        data_bits: u8,
        parity: Parity,
        stop_bits: u8,
        flow: FlowControl,
        xonxoff: bool,
    ) -> SerialConfig {
        SerialConfig {
            baud: 115200,
            data_bits,
            parity,
            stop_bits,
            flow,
            xonxoff,
        }
    }

    /// 輸入 termios：預置 `HUPCL|CS7|PARENB` 與 `c_iflag=ICRNL`，供驗證 build_termios 會完全覆寫 c_cflag。
    fn input_termios() -> libc::termios {
        let mut t: libc::termios = unsafe { std::mem::zeroed() };
        t.c_cflag = libc::HUPCL | libc::CS7 | libc::PARENB;
        t.c_iflag = libc::ICRNL;
        t.c_cc[libc::VMIN] = 1;
        t.c_cc[libc::VTIME] = 0;
        t.c_cc[libc::VINTR] = 3;
        t
    }

    #[test]
    fn termios_bits_8n1_rtscts() {
        let cfg = raw_cfg(8, Parity::None, 1, FlowControl::RtsCts, false);
        let out = build_termios(input_termios(), &cfg);
        assert_eq!(out.c_iflag, 0);
        assert_eq!(out.c_oflag, 0);
        assert_eq!(out.c_lflag, 0);
        // 完全相等：HUPCL 必須被清除（避免每次 close 掉 DTR 重置板子）
        assert_eq!(
            out.c_cflag & !libc::CBAUD,
            libc::CREAD | libc::CLOCAL | libc::CS8 | libc::CRTSCTS
        );
        assert_eq!(out.c_cc, input_termios().c_cc);
    }

    #[test]
    fn termios_bits_7e2() {
        let cfg = raw_cfg(7, Parity::Even, 2, FlowControl::None, false);
        let out = build_termios(input_termios(), &cfg);
        assert_eq!(
            out.c_cflag & !libc::CBAUD,
            libc::CREAD | libc::CLOCAL | libc::CS7 | libc::PARENB | libc::CSTOPB
        );
    }

    #[test]
    fn termios_odd() {
        let cfg = raw_cfg(8, Parity::Odd, 1, FlowControl::None, false);
        let out = build_termios(input_termios(), &cfg);
        assert_eq!(
            out.c_cflag & !libc::CBAUD,
            libc::CREAD | libc::CLOCAL | libc::CS8 | libc::PARENB | libc::PARODD
        );
    }

    #[test]
    fn xonxoff_ignored_on_posix() {
        let cfg = raw_cfg(8, Parity::None, 1, FlowControl::None, true);
        let out = build_termios(input_termios(), &cfg);
        assert_eq!(out.c_iflag, 0);
    }

    #[derive(Default)]
    struct RecTermios {
        tcsetattr_calls: usize,
    }

    impl TtyTermiosOps for RecTermios {
        fn tcgetattr(&mut self) -> Result<libc::termios, i32> {
            Ok(input_termios())
        }
        fn tcsetattr(&mut self, _t: &libc::termios) -> Result<(), i32> {
            self.tcsetattr_calls += 1;
            Ok(())
        }
    }

    #[test]
    fn unknown_baud_falls_back_115200() {
        assert_eq!(speed_for_baud(1234), libc::B115200);
        let mut ops = RecTermios::default();
        assert!(set_baud_with(&mut ops, 1234).is_ok());
        assert_eq!(ops.tcsetattr_calls, 0, "未知 baud 不得呼叫 tcsetattr");
    }

    #[test]
    fn vmin_vtime_untouched() {
        let mut input = input_termios();
        input.c_cc[libc::VMIN] = 7;
        input.c_cc[libc::VTIME] = 9;
        let out = build_termios(
            input,
            &raw_cfg(8, Parity::None, 1, FlowControl::None, false),
        );
        assert_eq!(out.c_cc[libc::VMIN], 7);
        assert_eq!(out.c_cc[libc::VTIME], 9);
    }

    #[test]
    fn serial_read_eio_is_an_error_not_eof() {
        let error = classify_read_result(-1, Some(libc::EIO), false).unwrap_err();
        assert_eq!(error.kind, PalErrorKind::Io);
        assert_eq!(error.os_code, Some(libc::EIO));
        assert_eq!(
            classify_read_result(0, None, false).unwrap(),
            Some(ReadOutcome::Eof)
        );
        assert_eq!(
            classify_read_result(-1, Some(libc::EIO), true).unwrap(),
            Some(ReadOutcome::Eof),
            "PTY 對端關閉時的 EIO 代表 EOF"
        );
    }

    #[test]
    fn write_retries_eintr_without_losing_result() {
        let mut attempts = 0;
        let mut results = [Err(libc::EINTR), Ok(4isize)].into_iter();
        let result = retry_eintr(|| {
            attempts += 1;
            results.next().expect("two scripted attempts")
        });
        assert_eq!(result, Ok(4));
        assert_eq!(attempts, 2);
    }

    #[test]
    fn debug_tx_write_count_tracks_raw_syscall() {
        debug_begin_tx_write_count();
        assert_eq!(raw_tx_write(-1, b"x"), -1);
        assert_eq!(take_debug_tx_write_count(), Some(1));
    }

    #[cfg(feature = "test-fakes")]
    #[test]
    #[ignore = "cap:posix_pty"]
    #[allow(non_snake_case)]
    fn cap_posix_pty__dropping_close_token_keeps_reader_fd_open() {
        use crate::conformance::PeerEnd;

        let (dev, mut peer) = crate::conformance::posix_pty_pair();
        let handle = PosixSerialBackend::new()
            .open_exclusive(&dev, &raw_cfg(8, Parity::None, 1, FlowControl::None, false))
            .expect("開啟隔離 PTY");
        let (mut reader, mut writer, waker, close) = handle.split();

        drop(close);
        let mut buf = [0u8; 8];
        assert_eq!(
            reader.read(&mut buf, Some(Duration::ZERO)).unwrap(),
            ReadOutcome::TimedOut,
            "CloseToken drop 不得在 reader 存活時關閉 tty fd"
        );
        assert!(
            matches!(
                PosixSerialBackend::new()
                    .open_exclusive(&dev, &raw_cfg(8, Parity::None, 1, FlowControl::None, false)),
                Err(OpenError::Held(_))
            ),
            "I/O handles 尚存活時仍須維持 UART 獨占"
        );

        let cancel = crate::traits::CancelToken::new(waker);
        let result = writer.write(b"still-owned", &cancel);
        assert_eq!(result.outcome, TxOutcome::Completed(11));
        assert!(result.error.is_none());
        assert_eq!(peer.read_available(Duration::from_secs(1)), b"still-owned");

        drop(reader);
        drop(writer);
        let external = std::fs::OpenOptions::new()
            .read(true)
            .write(true)
            .open(&dev.0)
            .expect("最後一個 I/O handle drop 後解除 TIOCEXCL");
        drop(external);
    }

    #[test]
    #[ignore = "cap:posix_pty"]
    #[allow(non_snake_case)]
    fn cap_posix_pty__zero_timeout_reads_ready_data_and_wake() {
        let mut master = -1;
        let mut slave = -1;
        // SAFETY: 兩個輸出欄位有效；其餘 openpty 參數可為 null。
        assert_eq!(
            unsafe {
                libc::openpty(
                    &mut master,
                    &mut slave,
                    std::ptr::null_mut(),
                    std::ptr::null(),
                    std::ptr::null(),
                )
            },
            0
        );
        // SAFETY: openpty 回傳兩個唯一且有效的 fd，交由 OwnedFd 關閉。
        let master = unsafe { OwnedFd::from_raw_fd(master) };
        // SAFETY: openpty 回傳兩個唯一且有效的 fd，交由 OwnedFd 關閉。
        let slave = unsafe { OwnedFd::from_raw_fd(slave) };
        let mut name = vec![0 as libc::c_char; 4096];
        // SAFETY: slave 有效，name 是足長的可寫緩衝區。
        let rc = unsafe { libc::ttyname_r(slave.as_raw_fd(), name.as_mut_ptr(), name.len()) };
        assert_eq!(
            rc,
            0,
            "ttyname_r: {}",
            std::io::Error::from_raw_os_error(rc)
        );
        // SAFETY: ttyname_r 成功時 name 含 NUL 結尾路徑。
        let path = unsafe { CStr::from_ptr(name.as_ptr()) }
            .to_string_lossy()
            .into_owned();
        drop(slave);

        let handle = PosixSerialBackend::new()
            .open_exclusive(
                &DevicePath(path),
                &raw_cfg(8, Parity::None, 1, FlowControl::None, false),
            )
            .expect("open PTY");
        let (mut reader, writer, waker, close) = handle.split();
        let byte = [b'x'];
        // SAFETY: byte 為有效來源緩衝區，master 仍由 OwnedFd 持有。
        assert_eq!(
            unsafe { libc::write(master.as_raw_fd(), byte.as_ptr().cast(), byte.len()) },
            1
        );
        let mut buf = [0; 8];
        assert!(matches!(
            reader.read(&mut buf, Some(Duration::ZERO)),
            Ok(ReadOutcome::Data(1))
        ));
        assert_eq!(buf[0], b'x');
        waker.wake();
        assert!(matches!(
            reader.read(&mut buf, Some(Duration::ZERO)),
            Ok(ReadOutcome::Woken)
        ));
        assert!(close.close(reader, writer).closed);
    }

    fn make_fd_link(root: &Path, pid: u32, target: &str) {
        let fd_dir = root.join(pid.to_string()).join("fd");
        std::fs::create_dir_all(&fd_dir).expect("mkdir fd");
        std::os::unix::fs::symlink(target, fd_dir.join("3")).expect("symlink fd");
    }

    #[test]
    fn holder_scan_excludes_self_and_matches_rdev() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let root = tmp.path();
        make_fd_link(root, 123, "/dev/pts/9");
        make_fd_link(root, std::process::id(), "/dev/pts/9"); // 自身 pid 必須被排除
        let rep = probe_holders_in(root, "/dev/pts/9");
        assert_eq!(rep.pids, vec![123]);
        assert_eq!(rep.status, HolderScanStatus::Ok);
    }

    #[test]
    fn holder_scan_unreadable_proc_is_unknown() {
        let rep = probe_holders_in(Path::new("/nonexistent/serialwrap-s21-proc"), "/dev/pts/9");
        assert_eq!(
            rep,
            HolderReport {
                pids: vec![],
                holder_unknown: true,
                status: HolderScanStatus::Unknown,
            }
        );
    }

    #[test]
    fn holder_scan_permission() {
        if unsafe { libc::geteuid() } == 0 {
            eprintln!("EVIDENCE_MISSING: root 下 chmod 000 無效，本案例證據不足");
            return;
        }
        let tmp = tempfile::tempdir().expect("tempdir");
        let root = tmp.path();
        make_fd_link(root, 123, "/dev/pts/9");
        let fd_dir = root.join("123").join("fd");
        std::fs::set_permissions(&fd_dir, std::fs::Permissions::from_mode(0o000))
            .expect("chmod 000");
        let rep = probe_holders_in(root, "/dev/pts/9");
        // 還原權限，讓 tempdir 可被清除
        let _ = std::fs::set_permissions(&fd_dir, std::fs::Permissions::from_mode(0o755));
        assert_eq!(rep.status, HolderScanStatus::Permission);
        assert!(rep.holder_unknown);
    }

    #[test]
    fn holder_unknown_only_when_no_pids() {
        // probe 回 pids:[123]、status:Permission（部分目錄不可讀）→ holder_unknown 必為 false
        let rep = finalize_holder_report(HolderReport {
            pids: vec![123],
            holder_unknown: true,
            status: HolderScanStatus::Permission,
        });
        assert!(!rep.holder_unknown);
        // pids 為空 → holder_unknown 必為 true（與 status 無關）
        let rep = finalize_holder_report(HolderReport {
            pids: vec![],
            holder_unknown: false,
            status: HolderScanStatus::Ok,
        });
        assert!(rep.holder_unknown);
    }

    #[derive(Default)]
    struct RecClose {
        ops: Vec<CloseOp>,
        warns: Vec<String>,
        nxcl_err: Option<i32>,
        close_fd_err: Option<i32>,
    }

    impl TtyCloseOps for RecClose {
        fn nxcl(&mut self) -> Result<(), i32> {
            self.ops.push(CloseOp::Nxcl);
            match self.nxcl_err {
                Some(e) => Err(e),
                None => Ok(()),
            }
        }
        fn flock_unlock(&mut self) {
            self.ops.push(CloseOp::FlockUnlock);
        }
        fn close_fd(&mut self) -> Result<(), i32> {
            self.ops.push(CloseOp::CloseFd);
            match self.close_fd_err {
                Some(e) => Err(e),
                None => Ok(()),
            }
        }
        fn close_wake_r(&mut self) {
            self.ops.push(CloseOp::CloseWakeR);
        }
        fn close_wake_w(&mut self) {
            self.ops.push(CloseOp::CloseWakeW);
        }
        fn warn(&mut self, line: &str) {
            self.warns.push(line.to_string());
        }
    }

    const FULL_CLOSE: [CloseOp; 5] = [
        CloseOp::Nxcl,
        CloseOp::FlockUnlock,
        CloseOp::CloseFd,
        CloseOp::CloseWakeR,
        CloseOp::CloseWakeW,
    ];

    #[test]
    fn close_sequence_order_nxcl_before_close() {
        let mut ops = RecClose::default();
        let rc = close_sequence(&mut ops, "/dev/pts/9", true);
        assert_eq!(ops.ops, FULL_CLOSE.to_vec());
        assert!(rc.closed && rc.excl_cleared && rc.error.is_none(), "{rc:?}");
    }

    #[test]
    fn close_sequence_nxcl_hangup_errno_is_cleared() {
        for code in [libc::EIO, libc::ENXIO, libc::ENODEV] {
            let mut ops = RecClose {
                nxcl_err: Some(code),
                ..Default::default()
            };
            let rc = close_sequence(&mut ops, "/dev/pts/9", true);
            assert_eq!(ops.ops, FULL_CLOSE.to_vec(), "errno {code}");
            assert!(rc.closed, "errno {code}: {rc:?}");
            assert!(rc.excl_cleared, "errno {code}: {rc:?}");
            assert!(rc.error.is_none(), "errno {code}: {rc:?}");
            assert!(ops.warns.is_empty(), "errno {code}: 不得有 WARNING");
        }
    }

    #[test]
    fn close_sequence_nxcl_other_errno_still_closes() {
        let mut ops = RecClose {
            nxcl_err: Some(libc::ENOTTY),
            ..Default::default()
        };
        let rc = close_sequence(&mut ops, "/dev/pts/9", true);
        assert_eq!(
            ops.ops,
            vec![
                CloseOp::Nxcl,
                CloseOp::FlockUnlock,
                CloseOp::CloseFd,
                CloseOp::CloseWakeR,
                CloseOp::CloseWakeW
            ]
        );
        assert!(rc.closed);
        assert!(!rc.excl_cleared);
        assert_eq!(
            rc.error.as_ref().map(|e| e.py_message.as_str()),
            Some("[Errno 25] Inappropriate ioctl for device")
        );
        assert_eq!(ops.warns.len(), 1, "WARNING 恰一筆：{:?}", ops.warns);
        assert!(
            ops.warns[0].starts_with("serialwrapd: TIOCNXCL failed on "),
            "{:?}",
            ops.warns
        );
    }

    #[test]
    fn close_sequence_close_fd_failure() {
        let mut ops = RecClose {
            close_fd_err: Some(libc::EIO),
            ..Default::default()
        };
        let rc = close_sequence(&mut ops, "/dev/pts/9", true);
        assert!(!rc.closed, "{rc:?}");
        assert!(rc.excl_cleared, "TIOCNXCL 已成功：{rc:?}");
        assert_eq!(rc.error.as_ref().map(|e| e.kind), Some(PalErrorKind::Io));
        assert!(ops.ops.contains(&CloseOp::CloseWakeR), "wake fd 仍須關閉");
        assert!(ops.ops.contains(&CloseOp::CloseWakeW), "wake fd 仍須關閉");
    }

    #[derive(Default)]
    struct RecOpen {
        ops: Vec<OpenOp>,
        open_err: Option<i32>,
        flock_err: Option<i32>,
        probe: Option<HolderReport>,
        termios_err: Option<i32>,
        warns: Vec<String>,
    }

    impl TtyOpenOps for RecOpen {
        fn open(&mut self) -> Result<(), i32> {
            self.ops.push(OpenOp::Open);
            match self.open_err {
                Some(e) => Err(e),
                None => Ok(()),
            }
        }
        fn flock(&mut self) -> Result<(), i32> {
            self.ops.push(OpenOp::Flock);
            match self.flock_err {
                Some(e) => Err(e),
                None => Ok(()),
            }
        }
        fn excl(&mut self) -> Result<(), i32> {
            self.ops.push(OpenOp::Excl);
            Ok(())
        }
        fn probe_holders(&mut self) -> HolderReport {
            self.ops.push(OpenOp::ProbeHolders);
            self.probe.clone().unwrap_or(HolderReport {
                pids: vec![],
                holder_unknown: true,
                status: HolderScanStatus::Ok,
            })
        }
        fn termios(&mut self) -> Result<(), i32> {
            let r = match self.termios_err {
                Some(e) => Err(e),
                None => Ok(()),
            };
            self.ops.push(OpenOp::Termios(r));
            r
        }
        fn nxcl(&mut self) -> Result<(), i32> {
            self.ops.push(OpenOp::Nxcl);
            Ok(())
        }
        fn close_fd(&mut self) -> Result<(), i32> {
            self.ops.push(OpenOp::CloseFd);
            Ok(())
        }
        fn warn(&mut self, line: &str) {
            self.warns.push(line.to_string());
        }
    }

    #[test]
    fn open_failure_after_excl_runs_nxcl_before_close() {
        // (a) termios 失敗：TIOCEXCL 之後必須 Nxcl 再 CloseFd。
        // 注意：計畫文字寫「尾段恰為 [Excl, Termios(Err), Nxcl, CloseFd]」，但計畫 Step 3 的程式順序是
        // 「TIOCEXCL → 鎖後重掃 → termios」，故完整序列含 ProbeHolders；此處以 Step 3 的順序為準。
        let mut ops = RecOpen {
            termios_err: Some(libc::EIO),
            ..Default::default()
        };
        match open_sequence(&mut ops, "/dev/pts/9") {
            Err(OpenError::Other(_)) => {}
            other => panic!("termios 失敗應回 Other，實際 {other:?}"),
        }
        assert_eq!(
            ops.ops,
            vec![
                OpenOp::Open,
                OpenOp::Flock,
                OpenOp::Excl,
                OpenOp::ProbeHolders,
                OpenOp::Termios(Err(libc::EIO)),
                OpenOp::Nxcl,
                OpenOp::CloseFd,
            ]
        );
        // (b) 鎖後重掃 Reject：尾段恰為 [Excl, ProbeHolders, Nxcl, CloseFd]，不得出現 Termios
        let mut ops = RecOpen {
            probe: Some(HolderReport {
                pids: vec![123],
                holder_unknown: false,
                status: HolderScanStatus::Ok,
            }),
            ..Default::default()
        };
        match open_sequence(&mut ops, "/dev/pts/9") {
            Err(OpenError::Held(rep)) => assert_eq!(rep.pids, vec![123]),
            other => panic!("鎖後重掃 Reject 應回 Held，實際 {other:?}"),
        }
        assert_eq!(
            ops.ops,
            vec![
                OpenOp::Open,
                OpenOp::Flock,
                OpenOp::Excl,
                OpenOp::ProbeHolders,
                OpenOp::Nxcl,
                OpenOp::CloseFd,
            ]
        );
        assert!(
            !ops.ops.iter().any(|o| matches!(o, OpenOp::Termios(_))),
            "Reject 時不得改寫 termios"
        );
        // (c) TIOCEXCL 之前失敗（open／flock）：序列中不得出現 Nxcl
        let mut ops = RecOpen {
            open_err: Some(libc::EACCES),
            ..Default::default()
        };
        let _ = open_sequence(&mut ops, "/dev/pts/9");
        assert!(
            !ops.ops.contains(&OpenOp::Nxcl),
            "open 失敗時不得 TIOCNXCL：{:?}",
            ops.ops
        );
        let mut ops = RecOpen {
            flock_err: Some(libc::EWOULDBLOCK),
            ..Default::default()
        };
        let _ = open_sequence(&mut ops, "/dev/pts/9");
        assert!(
            !ops.ops.contains(&OpenOp::Nxcl),
            "flock 失敗時不得 TIOCNXCL：{:?}",
            ops.ops
        );
    }

    #[test]
    fn post_lock_holder_verdict_matrix() {
        let rep = |pids: Vec<u32>, status: HolderScanStatus| HolderReport {
            pids,
            holder_unknown: false,
            status,
        };
        assert_eq!(
            post_lock_holder_verdict(&rep(vec![123], HolderScanStatus::Ok)),
            PostLockVerdict::Reject
        );
        assert_eq!(
            post_lock_holder_verdict(&rep(vec![123], HolderScanStatus::Permission)),
            PostLockVerdict::Reject,
            "已識別即拒絕，不論 status"
        );
        assert_eq!(
            post_lock_holder_verdict(&rep(vec![], HolderScanStatus::Ok)),
            PostLockVerdict::Accept
        );
        assert_eq!(
            post_lock_holder_verdict(&rep(vec![], HolderScanStatus::Unknown)),
            PostLockVerdict::AcceptUnverified
        );
        assert_eq!(
            post_lock_holder_verdict(&rep(vec![], HolderScanStatus::Permission)),
            PostLockVerdict::AcceptUnverified
        );
    }
}
