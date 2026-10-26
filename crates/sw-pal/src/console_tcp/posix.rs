//! POSIX TCP console listener（P6 Task 8）：`127.0.0.1:0`、SO_REUSEADDR（bind 前）、`listen(fd, 8)`、nonblocking；
//! accept 後 TCP_NODELAY（失敗忽略）與 nonblocking。寫入一律 `send(MSG_NOSIGNAL)`。
use std::collections::HashSet;
use std::net::{Shutdown, TcpListener, TcpStream};
use std::os::fd::{AsRawFd, FromRawFd, RawFd};
use std::sync::Arc;
use std::time::Duration;

use crate::console_mux::posix::{errno, read_nonblocking, wait_fd_or_wake, write_nonblocking, FdWait, WakeFd};
use crate::console_mux::{MuxHandle, MuxHandleInner};
use crate::console_pty::{ConsoleIo, ConsoleListener};
use crate::console_tcp::CONSOLE_TCP_BACKLOG;
use crate::traits::{posix_io_error, IoWaker, PalError, PalResult, ReadOutcome};

pub fn bind() -> PalResult<Box<dyn ConsoleListener>> {
    let listener = bind_raw().map_err(PalError::from_errno)?;
    let addr = listener.local_addr().map_err(posix_io_error)?;
    Ok(Box::new(TcpConsoleListener {
        endpoint: format!("{}:{}", addr.ip(), addr.port()),
        inner: listener,
    }))
}

/// socket → SO_REUSEADDR → bind 127.0.0.1:0 → listen(8) → nonblocking（std 的 `TcpListener::bind` 固定 backlog 128）。
fn bind_raw() -> Result<TcpListener, i32> {
    // SAFETY: socket 不接收指標；回傳 fd 立即由 TcpListener 接管（失敗路徑亦由其 Drop 關閉）。
    let fd = unsafe { libc::socket(libc::AF_INET, libc::SOCK_STREAM, 0) };
    if fd < 0 {
        return Err(errno());
    }
    // SAFETY: fd 為剛建立且未被他處持有的 socket。
    let listener = unsafe { TcpListener::from_raw_fd(fd) };
    set_cloexec_nonblocking(fd)?;
    set_int_opt(fd, libc::SOL_SOCKET, libc::SO_REUSEADDR, 1)?;
    // SAFETY: sockaddr_in 以 0 初始化後逐欄填值。
    let mut sa: libc::sockaddr_in = unsafe { std::mem::zeroed() };
    sa.sin_family = libc::AF_INET as libc::sa_family_t;
    sa.sin_port = 0;
    sa.sin_addr = libc::in_addr {
        s_addr: u32::from_ne_bytes([127, 0, 0, 1]),
    };
    // SAFETY: sa 為有效 sockaddr_in，長度正確。
    let rc = unsafe {
        libc::bind(
            fd,
            (&sa as *const libc::sockaddr_in).cast(),
            std::mem::size_of::<libc::sockaddr_in>() as libc::socklen_t,
        )
    };
    if rc != 0 {
        return Err(errno());
    }
    // SAFETY: fd 為已 bind 的 socket。
    if unsafe { libc::listen(fd, CONSOLE_TCP_BACKLOG) } != 0 {
        return Err(errno());
    }
    Ok(listener)
}

struct TcpConsoleListener {
    inner: TcpListener,
    endpoint: String,
}

impl ConsoleListener for TcpConsoleListener {
    fn endpoint(&self) -> String {
        self.endpoint.clone()
    }

    fn accept(&mut self, timeout: Duration) -> PalResult<Option<(Box<dyn ConsoleIo>, String)>> {
        if !poll_readable(self.inner.as_raw_fd(), timeout)? {
            return Ok(None);
        }
        let (stream, peer) = match self.inner.accept() {
            Ok(v) => v,
            Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => return Ok(None),
            Err(e) if e.raw_os_error() == Some(libc::ECONNABORTED) => return Ok(None),
            Err(e) => return Err(posix_io_error(e)),
        };
        stream.set_nonblocking(true).map_err(posix_io_error)?;
        let _ = stream.set_nodelay(true);
        let io = TcpConsoleIo {
            stream,
            wake: WakeFd::new()?,
        };
        Ok(Some((Box::new(io), format!("{}:{}", peer.ip(), peer.port()))))
    }

    fn mux_handle(&self) -> MuxHandle {
        MuxHandle(MuxHandleInner::Posix(self.inner.as_raw_fd()))
    }
}

struct TcpConsoleIo {
    stream: TcpStream,
    wake: Arc<WakeFd>,
}

impl ConsoleIo for TcpConsoleIo {
    fn read(&mut self, buf: &mut [u8], timeout: Option<Duration>) -> PalResult<ReadOutcome> {
        let fd = self.stream.as_raw_fd();
        let deadline = timeout.map(|t| std::time::Instant::now() + t);
        loop {
            let remaining = deadline.map(|d| d.saturating_duration_since(std::time::Instant::now()));
            match wait_fd_or_wake(fd, &self.wake, remaining)? {
                FdWait::TimedOut => return Ok(ReadOutcome::TimedOut),
                FdWait::Woken => return Ok(ReadOutcome::Woken),
                FdWait::Ready => match read_nonblocking(fd, buf)? {
                    // 可讀但 recv 回 EAGAIN（spurious wakeup）：繼續等到期限。
                    ReadOutcome::TimedOut if remaining.is_none_or(|r| !r.is_zero()) => continue,
                    other => return Ok(other),
                },
            }
        }
    }

    fn try_write(&mut self, data: &[u8]) -> PalResult<usize> {
        write_nonblocking(self.stream.as_raw_fd(), data)
    }

    fn waker(&self) -> Arc<dyn IoWaker> {
        Arc::clone(&self.wake) as Arc<dyn IoWaker>
    }

    /// TCP 斷線由 EOF 直接偵測，不靠 `/proc` 掃描（uart_io.py `_client_has_external_peer_locked`）。
    fn has_external_peer(&self, _held: Option<&HashSet<String>>) -> bool {
        true
    }

    fn close(self: Box<Self>) {
        let _ = self.stream.shutdown(Shutdown::Both);
    }

    fn mux_handle(&self) -> MuxHandle {
        MuxHandle(MuxHandleInner::Posix(self.stream.as_raw_fd()))
    }
}

fn poll_readable(fd: RawFd, timeout: Duration) -> PalResult<bool> {
    let start = std::time::Instant::now();
    loop {
        let mut pfd = libc::pollfd {
            fd,
            events: libc::POLLIN,
            revents: 0,
        };
        let ms = crate::console_mux::posix::timeout_ms(start, Some(timeout));
        // SAFETY: pfd 為有效 pollfd。
        let rc = unsafe { libc::poll(&mut pfd, 1, ms) };
        if rc > 0 {
            return Ok(true);
        }
        if rc == 0 {
            return Ok(false);
        }
        let code = errno();
        if code != libc::EINTR {
            return Err(PalError::from_errno(code));
        }
    }
}

fn set_cloexec_nonblocking(fd: RawFd) -> Result<(), i32> {
    // SAFETY: fcntl 不保留指標，fd 由呼叫端持有。
    unsafe {
        let fd_flags = libc::fcntl(fd, libc::F_GETFD);
        if fd_flags < 0 || libc::fcntl(fd, libc::F_SETFD, fd_flags | libc::FD_CLOEXEC) < 0 {
            return Err(errno());
        }
        let fl = libc::fcntl(fd, libc::F_GETFL);
        if fl < 0 || libc::fcntl(fd, libc::F_SETFL, fl | libc::O_NONBLOCK) < 0 {
            return Err(errno());
        }
    }
    Ok(())
}

fn set_int_opt(fd: RawFd, level: libc::c_int, name: libc::c_int, v: libc::c_int) -> Result<(), i32> {
    // SAFETY: v 為有效 c_int，長度正確。
    let rc = unsafe {
        libc::setsockopt(
            fd,
            level,
            name,
            (&v as *const libc::c_int).cast(),
            std::mem::size_of::<libc::c_int>() as libc::socklen_t,
        )
    };
    if rc != 0 {
        return Err(errno());
    }
    Ok(())
}

/// `getsockopt(level, name)` 的整數值（testutil 用）。
#[cfg(feature = "test-fakes")]
pub(crate) fn get_int_opt(fd: RawFd, level: libc::c_int, name: libc::c_int) -> Option<libc::c_int> {
    let mut v: libc::c_int = 0;
    let mut len = std::mem::size_of::<libc::c_int>() as libc::socklen_t;
    // SAFETY: v／len 為有效輸出位置。
    let rc = unsafe { libc::getsockopt(fd, level, name, (&mut v as *mut libc::c_int).cast(), &mut len) };
    (rc == 0).then_some(v)
}
