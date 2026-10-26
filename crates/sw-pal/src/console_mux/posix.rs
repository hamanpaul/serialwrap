//! POSIX ConsoleMux（P6 Task 8）：`poll(2)`＋eventfd（非 Linux 用 self-pipe）。
//!
//! 另提供 TCP console 共用的 wake fd 與「等待 fd 或 wake」helper（`console_tcp/posix.rs`）。
use std::collections::BTreeMap;
use std::os::fd::RawFd;
use std::sync::Arc;
use std::time::{Duration, Instant};

use crate::console_mux::{ConsoleMux, MuxEntry, MuxEvent, MuxHandle, MuxHandleInner, MuxId};
use crate::console_pty::{ConsoleIo, ConsoleListener};
use crate::traits::{IoWaker, PalError, PalErrorKind, PalResult, ReadOutcome};

/// 非阻塞 wake fd（Linux eventfd；其他 POSIX 為 self-pipe）。fd 隨最後一個 `Arc` 釋放而關閉。
pub(crate) struct WakeFd {
    read_fd: RawFd,
    write_fd: RawFd,
    eventfd: bool,
}

impl WakeFd {
    pub(crate) fn new() -> PalResult<Arc<WakeFd>> {
        #[cfg(target_os = "linux")]
        {
            // SAFETY: eventfd 不接收指標，回傳 fd 由此結構持有。
            let fd = unsafe { libc::eventfd(0, libc::EFD_NONBLOCK | libc::EFD_CLOEXEC) };
            if fd < 0 {
                return Err(PalError::from_errno(errno()));
            }
            Ok(Arc::new(WakeFd {
                read_fd: fd,
                write_fd: fd,
                eventfd: true,
            }))
        }
        #[cfg(not(target_os = "linux"))]
        {
            let mut fds = [-1; 2];
            // SAFETY: fds 指向兩個有效整數欄位。
            if unsafe { libc::pipe(fds.as_mut_ptr()) } != 0 {
                return Err(PalError::from_errno(errno()));
            }
            let wake = WakeFd {
                read_fd: fds[0],
                write_fd: fds[1],
                eventfd: false,
            };
            for fd in fds {
                set_nonblocking_cloexec(fd).map_err(PalError::from_errno)?;
            }
            Ok(Arc::new(wake))
        }
    }

    pub(crate) fn read_fd(&self) -> RawFd {
        self.read_fd
    }

    /// 讀乾 wake fd（`wake()` 連呼叫只算一次喚醒）。
    pub(crate) fn drain(&self) {
        let mut buf = [0u8; 128];
        let len = if self.eventfd {
            std::mem::size_of::<u64>()
        } else {
            buf.len()
        };
        loop {
            // SAFETY: buf 至少有 len 位元組，fd 是非阻塞 wake fd。
            let rc = unsafe { libc::read(self.read_fd, buf.as_mut_ptr().cast(), len) };
            if rc > 0 && !self.eventfd {
                continue;
            }
            if rc < 0 && errno() == libc::EINTR {
                continue;
            }
            return;
        }
    }
}

impl IoWaker for WakeFd {
    fn wake(&self) {
        let event = 1u64;
        let byte = [1u8];
        let (ptr, len) = if self.eventfd {
            ((&event as *const u64).cast(), std::mem::size_of::<u64>())
        } else {
            (byte.as_ptr().cast(), byte.len())
        };
        loop {
            // SAFETY: ptr 指向 len 位元組的有效緩衝區，fd 是非阻塞 wake fd。
            let rc = unsafe { libc::write(self.write_fd, ptr, len) };
            if rc < 0 && errno() == libc::EINTR {
                continue;
            }
            // EAGAIN 表示 wake fd 已可讀，等同已喚醒。
            return;
        }
    }
}

impl Drop for WakeFd {
    fn drop(&mut self) {
        // SAFETY: 此結構獨占持有 wake fd；eventfd 的讀寫 fd 是同一個。
        unsafe {
            libc::close(self.read_fd);
            if self.write_fd != self.read_fd {
                libc::close(self.write_fd);
            }
        }
    }
}

/// `wait_fd_or_wake` 的結果。
pub(crate) enum FdWait {
    Ready,
    Woken,
    TimedOut,
}

/// 等待 `fd` 可讀（含 HUP／ERR）或 wake；`timeout=None` 無限等待。
pub(crate) fn wait_fd_or_wake(fd: RawFd, wake: &WakeFd, timeout: Option<Duration>) -> PalResult<FdWait> {
    let start = Instant::now();
    loop {
        let mut fds = [
            libc::pollfd {
                fd,
                events: libc::POLLIN,
                revents: 0,
            },
            libc::pollfd {
                fd: wake.read_fd,
                events: libc::POLLIN,
                revents: 0,
            },
        ];
        // SAFETY: fds 是有效的兩個 pollfd 結構。
        let rc = unsafe { libc::poll(fds.as_mut_ptr(), 2, timeout_ms(start, timeout)) };
        if rc == 0 {
            return Ok(FdWait::TimedOut);
        }
        if rc < 0 {
            let code = errno();
            if code == libc::EINTR {
                continue;
            }
            return Err(PalError::from_errno(code));
        }
        if fds[1].revents != 0 {
            wake.drain();
            return Ok(FdWait::Woken);
        }
        if fds[0].revents != 0 {
            return Ok(FdWait::Ready);
        }
    }
}

/// 非阻塞讀取：資料 → `Data`、0 → `Eof`、EAGAIN → `TimedOut`；PTY master 的 EIO（slave 全關）視為 `Eof`。
pub(crate) fn read_nonblocking(fd: RawFd, buf: &mut [u8]) -> PalResult<ReadOutcome> {
    if buf.is_empty() {
        return Ok(ReadOutcome::Data(0));
    }
    loop {
        // SAFETY: buf 為有效輸出緩衝區，fd 由呼叫端持有的物件保持開啟。
        let n = unsafe { libc::read(fd, buf.as_mut_ptr().cast(), buf.len()) };
        if n > 0 {
            return Ok(ReadOutcome::Data(n as usize));
        }
        if n == 0 {
            return Ok(ReadOutcome::Eof);
        }
        match errno() {
            libc::EINTR => continue,
            code if code == libc::EAGAIN || code == libc::EWOULDBLOCK => return Ok(ReadOutcome::TimedOut),
            libc::EIO => return Ok(ReadOutcome::Eof),
            code => return Err(PalError::from_errno(code)),
        }
    }
}

/// 非阻塞寫入：socket 以 `send(MSG_NOSIGNAL)`（對端已關不觸發 SIGPIPE），非 socket 退回 `write`；EAGAIN → `Ok(0)`。
pub(crate) fn write_nonblocking(fd: RawFd, data: &[u8]) -> PalResult<usize> {
    if data.is_empty() {
        return Ok(0);
    }
    let mut use_send = true;
    loop {
        // SAFETY: data 在同步呼叫期間有效，fd 由呼叫端持有的物件保持開啟。
        let n = unsafe {
            if use_send {
                libc::send(fd, data.as_ptr().cast(), data.len(), send_flags())
            } else {
                libc::write(fd, data.as_ptr().cast(), data.len())
            }
        };
        if n >= 0 {
            return Ok(n as usize);
        }
        match errno() {
            libc::EINTR => continue,
            libc::ENOTSOCK if use_send => use_send = false,
            code if code == libc::EAGAIN || code == libc::EWOULDBLOCK => return Ok(0),
            code => return Err(PalError::from_errno(code)),
        }
    }
}

#[cfg(any(target_os = "linux", target_os = "android"))]
fn send_flags() -> libc::c_int {
    libc::MSG_NOSIGNAL
}

#[cfg(not(any(target_os = "linux", target_os = "android")))]
fn send_flags() -> libc::c_int {
    0
}

enum Slot {
    Io(Box<dyn ConsoleIo>),
    Listener(Box<dyn ConsoleListener>),
}

struct Entry {
    fd: RawFd,
    write_interest: bool,
    slot: Slot,
}

/// POSIX `poll(2)` 多工器。
pub struct PosixConsoleMux {
    entries: BTreeMap<MuxId, Entry>,
    wake: Arc<WakeFd>,
}

impl PosixConsoleMux {
    pub fn create() -> PalResult<Box<dyn ConsoleMux>> {
        Ok(Box::new(PosixConsoleMux {
            entries: BTreeMap::new(),
            wake: WakeFd::new()?,
        }))
    }

    fn insert(&mut self, id: MuxId, handle: MuxHandle, slot: Slot) -> PalResult<()> {
        let MuxHandleInner::Posix(fd) = handle.0 else {
            return Err(PalError::plain(
                PalErrorKind::Unsupported,
                "ConsoleMux：handle 不屬於此 PAL",
            ));
        };
        if self.entries.contains_key(&id) {
            return Err(PalError::plain(
                PalErrorKind::InvalidInput,
                format!("ConsoleMux：id {id} 已登記"),
            ));
        }
        self.entries.insert(
            id,
            Entry {
                fd,
                write_interest: false,
                slot,
            },
        );
        Ok(())
    }

    fn io_fd(&self, id: MuxId) -> PalResult<RawFd> {
        match self.entries.get(&id) {
            Some(Entry {
                fd,
                slot: Slot::Io(_),
                ..
            }) => Ok(*fd),
            _ => Err(unknown_id(id)),
        }
    }
}

fn unknown_id(id: MuxId) -> PalError {
    PalError::plain(PalErrorKind::NotFound, format!("ConsoleMux：未登記的 io id {id}"))
}

impl ConsoleMux for PosixConsoleMux {
    fn add_io(&mut self, id: MuxId, io: Box<dyn ConsoleIo>) -> PalResult<()> {
        let handle = io.mux_handle();
        self.insert(id, handle, Slot::Io(io))
    }

    fn add_listener(&mut self, id: MuxId, l: Box<dyn ConsoleListener>) -> PalResult<()> {
        let handle = l.mux_handle();
        self.insert(id, handle, Slot::Listener(l))
    }

    fn remove(&mut self, id: MuxId) -> Option<MuxEntry> {
        self.entries.remove(&id).map(|e| match e.slot {
            Slot::Io(io) => MuxEntry::Io(io),
            Slot::Listener(l) => MuxEntry::Listener(l),
        })
    }

    fn set_write_interest(&mut self, id: MuxId, on: bool) {
        if let Some(e) = self.entries.get_mut(&id) {
            e.write_interest = on;
        }
    }

    fn wait(&mut self, timeout: Duration) -> PalResult<Vec<MuxEvent>> {
        let mut fds = Vec::with_capacity(self.entries.len() + 1);
        fds.push(libc::pollfd {
            fd: self.wake.read_fd(),
            events: libc::POLLIN,
            revents: 0,
        });
        let ids: Vec<MuxId> = self.entries.keys().copied().collect();
        for e in self.entries.values() {
            let mut events = libc::POLLIN;
            if e.write_interest && matches!(e.slot, Slot::Io(_)) {
                events |= libc::POLLOUT;
            }
            fds.push(libc::pollfd {
                fd: e.fd,
                events,
                revents: 0,
            });
        }
        let start = Instant::now();
        loop {
            // SAFETY: fds 為有效 pollfd 陣列，長度即 fds.len()。
            let rc = unsafe {
                libc::poll(
                    fds.as_mut_ptr(),
                    fds.len() as libc::nfds_t,
                    timeout_ms(start, Some(timeout)),
                )
            };
            if rc >= 0 {
                break;
            }
            let code = errno();
            if code != libc::EINTR {
                return Err(PalError::from_errno(code));
            }
        }
        let mut out = Vec::new();
        if fds[0].revents != 0 {
            self.wake.drain();
            out.push(MuxEvent::Woken);
        }
        for (pfd, id) in fds[1..].iter().zip(ids) {
            let r = pfd.revents;
            if r == 0 {
                continue;
            }
            let is_listener = matches!(self.entries.get(&id).map(|e| &e.slot), Some(Slot::Listener(_)));
            if is_listener {
                out.push(MuxEvent::Acceptable(id));
                continue;
            }
            if r & libc::POLLIN != 0 {
                out.push(MuxEvent::Readable(id));
            } else if r & (libc::POLLHUP | libc::POLLERR | libc::POLLNVAL) != 0 {
                out.push(MuxEvent::Hangup(id));
            }
            if r & libc::POLLOUT != 0 {
                out.push(MuxEvent::Writable(id));
            }
        }
        Ok(out)
    }

    fn read(&mut self, id: MuxId, buf: &mut [u8]) -> PalResult<ReadOutcome> {
        read_nonblocking(self.io_fd(id)?, buf)
    }

    fn try_write(&mut self, id: MuxId, data: &[u8]) -> PalResult<usize> {
        write_nonblocking(self.io_fd(id)?, data)
    }

    fn accept(&mut self, id: MuxId) -> PalResult<Option<(Box<dyn ConsoleIo>, String)>> {
        match self.entries.get_mut(&id) {
            Some(Entry {
                slot: Slot::Listener(l),
                ..
            }) => l.accept(Duration::ZERO),
            _ => Err(PalError::plain(
                PalErrorKind::NotFound,
                format!("ConsoleMux：未登記的 listener id {id}"),
            )),
        }
    }

    fn waker(&self) -> Arc<dyn IoWaker> {
        Arc::clone(&self.wake) as Arc<dyn IoWaker>
    }
}

/// poll 逾時（ms，向上取整）；`None` → -1。
pub(crate) fn timeout_ms(start: Instant, timeout: Option<Duration>) -> libc::c_int {
    let Some(timeout) = timeout else {
        return -1;
    };
    let remaining = timeout.saturating_sub(start.elapsed());
    if remaining.is_zero() {
        return 0;
    }
    let ms = remaining.as_nanos().div_ceil(1_000_000);
    ms.clamp(1, libc::c_int::MAX as u128) as libc::c_int
}

#[cfg(not(target_os = "linux"))]
fn set_nonblocking_cloexec(fd: RawFd) -> Result<(), i32> {
    // SAFETY: fcntl 不保留指標，fd 由呼叫端持有。
    unsafe {
        let fl = libc::fcntl(fd, libc::F_GETFL);
        if fl < 0 || libc::fcntl(fd, libc::F_SETFL, fl | libc::O_NONBLOCK) < 0 {
            return Err(errno());
        }
        let fd_flags = libc::fcntl(fd, libc::F_GETFD);
        if fd_flags < 0 || libc::fcntl(fd, libc::F_SETFD, fd_flags | libc::FD_CLOEXEC) < 0 {
            return Err(errno());
        }
    }
    Ok(())
}

pub(crate) fn errno() -> i32 {
    std::io::Error::last_os_error()
        .raw_os_error()
        .unwrap_or(libc::EIO)
}
