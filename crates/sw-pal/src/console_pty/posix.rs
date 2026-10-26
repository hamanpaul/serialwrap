//! POSIX 哨兵 PTY 建立與 master 端 I/O。

use std::collections::HashSet;
use std::ffi::CStr;
use std::os::fd::{FromRawFd, OwnedFd, RawFd};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use crate::console_pty::{ConsoleIo, KeepAlive, PtyPair, PtySlaveRef};
use crate::traits::{IoWaker, PalError, PalResult, ReadOutcome};

struct OpenPtyGuard {
    master: Option<RawFd>,
    slave: Option<RawFd>,
}

impl Drop for OpenPtyGuard {
    fn drop(&mut self) {
        // SAFETY: 這兩個 fd 只在本 guard 持有時關閉。
        unsafe {
            if let Some(fd) = self.master.take() {
                libc::close(fd);
            }
            if let Some(fd) = self.slave.take() {
                libc::close(fd);
            }
        }
    }
}

struct PtyWake {
    read_fd: RawFd,
    write_fd: RawFd,
    eventfd: bool,
}

impl PtyWake {
    fn new() -> Result<Self, i32> {
        #[cfg(target_os = "linux")]
        {
            // SAFETY: eventfd 不接收指標，回傳 fd 由此結構持有。
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

impl Drop for PtyWake {
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

struct PtyWaker {
    write_fd: Mutex<RawFd>,
    eventfd: bool,
}

impl IoWaker for PtyWaker {
    fn wake(&self) {
        let write_fd = self
            .write_fd
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        let fd = *write_fd;
        if fd < 0 {
            return;
        }
        let event = 1u64;
        let byte = [1u8];
        let (ptr, len) = if self.eventfd {
            ((&event as *const u64).cast(), std::mem::size_of::<u64>())
        } else {
            (byte.as_ptr().cast(), byte.len())
        };
        loop {
            // SAFETY: ptr 指向 len 位元組的有效緩衝區，fd 是非阻塞 wake fd。
            let rc = unsafe { libc::write(fd, ptr, len) };
            if rc >= 0 {
                return;
            }
            let code = errno();
            if code == libc::EINTR {
                continue;
            }
            // EAGAIN 表示 wake fd 已可讀。
            return;
        }
    }
}

struct PtyMaster {
    fd: RawFd,
    wake: PtyWake,
    waker: Arc<PtyWaker>,
    slave_path: String,
}

impl Drop for PtyMaster {
    fn drop(&mut self) {
        *self
            .waker
            .write_fd
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner) = -1;
        // SAFETY: master fd 由本結構獨占持有。
        unsafe {
            libc::close(self.fd);
        }
    }
}

impl ConsoleIo for PtyMaster {
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
                    fd: self.wake.read_fd,
                    events: libc::POLLIN,
                    revents: 0,
                },
            ];
            // SAFETY: fds 是有效的兩個 pollfd 結構。
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
                drain_wake(self.wake.read_fd, self.wake.eventfd);
                return Ok(ReadOutcome::Woken);
            }
            if fds[0].revents & (libc::POLLIN | libc::POLLHUP | libc::POLLERR) != 0 {
                // SAFETY: buf 為有效輸出緩衝區，master fd 由本結構持有。
                let n = unsafe { libc::read(self.fd, buf.as_mut_ptr().cast(), buf.len()) };
                if n > 0 {
                    return Ok(ReadOutcome::Data(n as usize));
                }
                if n == 0 {
                    return Ok(ReadOutcome::Eof);
                }
                let code = errno();
                if code == libc::EINTR || code == libc::EAGAIN || code == libc::EWOULDBLOCK {
                    continue;
                }
                if code == libc::EIO {
                    return Ok(ReadOutcome::Eof);
                }
                return Err(PalError::from_errno(code));
            }
            if fds[0].revents & libc::POLLNVAL != 0 {
                return Err(PalError::from_errno(libc::EBADF));
            }
        }
    }

    fn try_write(&mut self, data: &[u8]) -> PalResult<usize> {
        loop {
            // SAFETY: data 在同步 write 期間有效，master fd 由本結構持有。
            let n = unsafe { libc::write(self.fd, data.as_ptr().cast(), data.len()) };
            if n >= 0 {
                return Ok(n as usize);
            }
            let code = errno();
            if code == libc::EINTR {
                continue;
            }
            if code == libc::EAGAIN || code == libc::EWOULDBLOCK {
                return Ok(0);
            }
            return Err(PalError::from_errno(code));
        }
    }

    fn waker(&self) -> Arc<dyn IoWaker> {
        Arc::clone(&self.waker) as Arc<dyn IoWaker>
    }

    fn has_external_peer(&self, held: Option<&HashSet<String>>) -> bool {
        external_peer_from_held(held, &self.slave_path)
    }

    fn close(self: Box<Self>) {}

    fn mux_handle(&self) -> crate::console_mux::MuxHandle {
        crate::console_mux::MuxHandle(crate::console_mux::MuxHandleInner::Posix(self.fd))
    }
}

/// 建立 byte-transparent PTY；master 非阻塞，daemon 保留 slave fd 避免閒置 EOF。
pub(crate) fn create_pty() -> PalResult<PtyPair> {
    let mut master = -1;
    let mut slave = -1;
    // SAFETY: 輸出指標均有效，其餘 openpty 參數可為 null。
    if unsafe {
        libc::openpty(
            &mut master,
            &mut slave,
            std::ptr::null_mut(),
            std::ptr::null(),
            std::ptr::null(),
        )
    } != 0
    {
        return Err(PalError::from_errno(errno()));
    }
    let mut guard = OpenPtyGuard {
        master: Some(master),
        slave: Some(slave),
    };
    set_cloexec(master).map_err(PalError::from_errno)?;
    set_cloexec(slave).map_err(PalError::from_errno)?;
    set_nonblocking(master).map_err(PalError::from_errno)?;
    configure_slave(slave).map_err(PalError::from_errno)?;

    let mut name = vec![0 as libc::c_char; 4096];
    // SAFETY: name 是可寫緩衝區，長度已提供；slave 仍開啟。
    let rc = unsafe { libc::ttyname_r(slave, name.as_mut_ptr(), name.len()) };
    if rc != 0 {
        return Err(PalError::from_errno(rc));
    }
    // SAFETY: ttyname_r 成功時 name 含 NUL 結尾路徑。
    let slave_path = unsafe { CStr::from_ptr(name.as_ptr()) }
        .to_string_lossy()
        .into_owned();
    let wake = PtyWake::new().map_err(PalError::from_errno)?;
    let waker = Arc::new(PtyWaker {
        write_fd: Mutex::new(wake.write_fd),
        eventfd: wake.eventfd,
    });

    let master = guard.master.take().expect("master fd");
    let slave = guard.slave.take().expect("slave fd");
    // SAFETY: 兩個 fd 均由 guard 唯一持有，現在移交給結果物件。
    let keepalive = unsafe { OwnedFd::from_raw_fd(slave) };
    Ok(PtyPair {
        master: Box::new(PtyMaster {
            fd: master,
            wake,
            waker,
            slave_path: slave_path.clone(),
        }),
        slave_keepalive: KeepAlive { _fd: Some(keepalive) },
        slave_ref: PtySlaveRef {
            path: slave_path.clone(),
        },
        slave_path,
    })
}

fn configure_slave(fd: RawFd) -> Result<(), i32> {
    // SAFETY: attrs 是 tcgetattr 輸出位置，fd 是 openpty 回傳的 slave。
    let mut attrs: libc::termios = unsafe { std::mem::zeroed() };
    if unsafe { libc::tcgetattr(fd, &mut attrs) } != 0 {
        return Err(errno());
    }
    // 保存 openpty 提供的 kernel default，覆寫 c_cflag 時一併維持原速率，避免變成 B0。
    let input_speed = unsafe { libc::cfgetispeed(&attrs) };
    let output_speed = unsafe { libc::cfgetospeed(&attrs) };
    attrs.c_iflag = 0;
    attrs.c_oflag = 0;
    attrs.c_lflag = 0;
    attrs.c_cflag = libc::CREAD | libc::CLOCAL | libc::CS8;
    attrs.c_cc[libc::VMIN] = 1;
    attrs.c_cc[libc::VTIME] = 0;
    // SAFETY: attrs 來自 tcgetattr，且速率值由同一 termios 讀取。
    if unsafe { libc::cfsetispeed(&mut attrs, input_speed) } != 0 {
        return Err(errno());
    }
    // SAFETY: attrs 來自 tcgetattr，且速率值由同一 termios 讀取。
    if unsafe { libc::cfsetospeed(&mut attrs, output_speed) } != 0 {
        return Err(errno());
    }
    // SAFETY: fd 尚開啟，attrs 為有效 termios 結構。
    if unsafe { libc::tcsetattr(fd, libc::TCSANOW, &attrs) } != 0 {
        return Err(errno());
    }
    Ok(())
}

fn external_peer_from_held(held: Option<&HashSet<String>>, slave_path: &str) -> bool {
    held.map_or(true, |paths| paths.contains(slave_path))
}

fn set_nonblocking(fd: RawFd) -> Result<(), i32> {
    // SAFETY: fcntl 不保留指標，fd 由 openpty guard 持有。
    let flags = unsafe { libc::fcntl(fd, libc::F_GETFL) };
    if flags < 0 || unsafe { libc::fcntl(fd, libc::F_SETFL, flags | libc::O_NONBLOCK) } < 0 {
        return Err(errno());
    }
    Ok(())
}

fn set_cloexec(fd: RawFd) -> Result<(), i32> {
    // SAFETY: fcntl 不保留指標，fd 由 openpty guard 持有。
    let flags = unsafe { libc::fcntl(fd, libc::F_GETFD) };
    if flags < 0 || unsafe { libc::fcntl(fd, libc::F_SETFD, flags | libc::FD_CLOEXEC) } < 0 {
        return Err(errno());
    }
    Ok(())
}

#[cfg(not(target_os = "linux"))]
fn set_fd_flags(fd: RawFd) -> Result<(), i32> {
    set_nonblocking(fd)?;
    set_cloexec(fd)
}

fn drain_wake(fd: RawFd, eventfd: bool) {
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

fn remaining_timeout_ms(start: Instant, timeout: Option<Duration>) -> Option<i32> {
    let timeout = timeout?;
    let elapsed = start.elapsed();
    if elapsed >= timeout {
        return Some(0);
    }
    let remaining = timeout - elapsed;
    let millis = remaining.as_millis();
    let rounded = millis
        + if remaining.subsec_nanos() % 1_000_000 != 0 {
            1
        } else {
            0
        };
    Some(rounded.clamp(1, i32::MAX as u128) as i32)
}

fn errno() -> i32 {
    std::io::Error::last_os_error()
        .raw_os_error()
        .unwrap_or(libc::EIO)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;
    use std::os::unix::fs::OpenOptionsExt;

    #[test]
    #[ignore = "cap:posix_pty"]
    #[allow(non_snake_case)]
    fn cap_posix_pty__zero_timeout_reads_ready_data_and_wake() {
        let mut pair = create_pty().expect("create PTY");
        let mut slave = std::fs::OpenOptions::new()
            .write(true)
            .custom_flags(libc::O_NOCTTY | libc::O_NONBLOCK)
            .open(&pair.slave_path)
            .expect("open PTY slave");
        slave.write_all(b"x").expect("write PTY slave");

        let mut buf = [0; 8];
        assert!(matches!(
            pair.master.read(&mut buf, Some(Duration::ZERO)),
            Ok(ReadOutcome::Data(1))
        ));
        assert_eq!(buf[0], b'x');
        pair.master.waker().wake();
        assert!(matches!(
            pair.master.read(&mut buf, Some(Duration::ZERO)),
            Ok(ReadOutcome::Woken)
        ));
    }

    #[test]
    fn external_peer_held_paths_fail_closed_when_unknown() {
        let held = HashSet::from(["/dev/pts/4".to_string()]);
        assert!(external_peer_from_held(None, "/dev/pts/4"));
        assert!(external_peer_from_held(Some(&held), "/dev/pts/4"));
        assert!(!external_peer_from_held(Some(&held), "/dev/pts/5"));
    }

    #[test]
    #[ignore = "cap:posix_pty"]
    #[allow(non_snake_case)]
    fn cap_posix_pty__new_slave_speed_not_b0() {
        let mut master = -1;
        let mut slave = -1;
        // SAFETY: openpty 將兩個有效 fd 寫入輸出位置。
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
        let _guard = OpenPtyGuard {
            master: Some(master),
            slave: Some(slave),
        };
        let mut before: libc::termios = unsafe { std::mem::zeroed() };
        // SAFETY: slave 仍由 guard 持有。
        assert_eq!(unsafe { libc::tcgetattr(slave, &mut before) }, 0);
        let input_speed = unsafe { libc::cfgetispeed(&before) };
        let output_speed = unsafe { libc::cfgetospeed(&before) };
        assert_ne!(input_speed, libc::B0, "PTY kernel default input speed");
        assert_ne!(output_speed, libc::B0, "PTY kernel default output speed");

        configure_slave(slave).expect("設定哨兵 slave");
        let mut after: libc::termios = unsafe { std::mem::zeroed() };
        // SAFETY: slave 仍由 guard 持有。
        assert_eq!(unsafe { libc::tcgetattr(slave, &mut after) }, 0);
        assert_eq!(unsafe { libc::cfgetispeed(&after) }, input_speed);
        assert_eq!(unsafe { libc::cfgetospeed(&after) }, output_speed);
    }
}
