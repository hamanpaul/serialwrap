//! POSIX Unix-domain 與 TCP RPC client。

use crate::traits::{
    posix_errno, posix_io_error, timed_out, EndpointSpec, PalError, PalErrorKind, PalResult,
    RpcConnector, RpcServerStream, RpcStream, SockTimeout,
};
use std::ffi::{CStr, CString, OsStr};
use std::io;
use std::mem::{offset_of, size_of};
use std::os::fd::RawFd;
use std::os::unix::ffi::OsStrExt;
use std::ptr;
use std::time::{Duration, Instant};

impl RpcServerStream for std::net::TcpStream {
    fn shutdown_read(&mut self) -> PalResult<()> {
        self.shutdown(std::net::Shutdown::Read)
            .map_err(posix_io_error)
    }

    fn set_read_timeout(&mut self, duration: Option<Duration>) -> PalResult<()> {
        std::net::TcpStream::set_read_timeout(self, duration).map_err(posix_io_error)
    }
}

impl RpcServerStream for std::os::unix::net::UnixStream {
    fn shutdown_read(&mut self) -> PalResult<()> {
        self.shutdown(std::net::Shutdown::Read)
            .map_err(posix_io_error)
    }

    fn set_read_timeout(&mut self, duration: Option<Duration>) -> PalResult<()> {
        std::os::unix::net::UnixStream::set_read_timeout(self, duration).map_err(posix_io_error)
    }
}

#[derive(Clone, Copy, Debug, Default)]
pub struct NativeRpcConnector;

struct SocketStream {
    fd: RawFd,
    timeout: SockTimeout,
}

impl Drop for SocketStream {
    fn drop(&mut self) {
        // SAFETY: fd 由本模組 socket() 建立，且此 Drop 是唯一關閉路徑。
        unsafe { libc::close(self.fd) };
    }
}

impl RpcConnector for NativeRpcConnector {
    fn af_unix_available(&self) -> bool {
        true
    }

    fn connect(
        &self,
        endpoint: &EndpointSpec,
        timeout: SockTimeout,
    ) -> PalResult<Box<dyn RpcStream>> {
        let fd = match endpoint {
            EndpointSpec::Unix(path) => connect_unix(path, timeout)?,
            EndpointSpec::Tcp { host, port } => connect_tcp(host, *port, timeout)?,
        };
        Ok(Box::new(SocketStream { fd, timeout }))
    }

    fn resolve_tcp(&self, host: &str, port: u16) -> PalResult<()> {
        let _addresses = resolve(host, port)?;
        Ok(())
    }
}

impl RpcStream for SocketStream {
    fn send_all(&mut self, data: &[u8]) -> PalResult<()> {
        let deadline = match self.timeout {
            SockTimeout::Blocking(duration) => Instant::now().checked_add(duration),
            SockTimeout::NonBlocking => None,
        };
        let mut offset = 0;
        while offset < data.len() {
            ensure_before_deadline(deadline)?;
            // SAFETY: fd 為有效 socket；slice 對整個呼叫保持有效。
            let sent = unsafe {
                libc::send(
                    self.fd,
                    data[offset..].as_ptr().cast(),
                    data.len() - offset,
                    libc::MSG_NOSIGNAL,
                )
            };
            if sent > 0 {
                offset += sent as usize;
                continue;
            }
            if sent == 0 {
                return Err(posix_errno(libc::EPIPE));
            }
            let error = io::Error::last_os_error();
            let code = error.raw_os_error().unwrap_or(libc::EIO);
            if code == libc::EINTR {
                continue;
            }
            if code == libc::EAGAIN || code == libc::EWOULDBLOCK {
                match self.timeout {
                    SockTimeout::NonBlocking => return Err(posix_errno(code)),
                    SockTimeout::Blocking(_) => wait_until(self.fd, libc::POLLOUT, deadline)?,
                }
                continue;
            }
            return Err(posix_io_error(error));
        }
        Ok(())
    }

    fn recv(&mut self, buffer: &mut [u8]) -> PalResult<usize> {
        if buffer.is_empty() {
            return Ok(0);
        }
        if let SockTimeout::Blocking(duration) = self.timeout {
            wait_until(self.fd, libc::POLLIN, Instant::now().checked_add(duration))?;
        }
        // SAFETY: fd 為有效 socket；可寫 buffer 在整個呼叫期間有效。
        let received = unsafe { libc::recv(self.fd, buffer.as_mut_ptr().cast(), buffer.len(), 0) };
        if received >= 0 {
            return Ok(received as usize);
        }
        let error = io::Error::last_os_error();
        let code = error.raw_os_error().unwrap_or(libc::EIO);
        if code == libc::ETIMEDOUT {
            Err(timed_out())
        } else {
            Err(posix_io_error(error))
        }
    }
}

fn new_socket(
    family: libc::c_int,
    socket_type: libc::c_int,
    protocol: libc::c_int,
) -> PalResult<RawFd> {
    // SAFETY: 參數是核心支援的 socket family/type/protocol，沒有指標參數。
    let fd = unsafe {
        libc::socket(
            family,
            socket_type | libc::SOCK_CLOEXEC | libc::SOCK_NONBLOCK,
            protocol,
        )
    };
    if fd < 0 {
        Err(posix_io_error(io::Error::last_os_error()))
    } else {
        Ok(fd)
    }
}

fn connect_unix(path: &OsStr, timeout: SockTimeout) -> PalResult<RawFd> {
    let bytes = path.as_bytes();
    if bytes.is_empty() {
        return Err(posix_errno(libc::EINVAL));
    }
    if bytes
        .iter()
        .any(|byte| *byte == 0 && bytes.first() != Some(&0))
    {
        return Err(PalError::plain(
            PalErrorKind::InvalidInput,
            "embedded null byte",
        ));
    }

    let address = std::mem::MaybeUninit::<libc::sockaddr_un>::zeroed();
    // SAFETY: sockaddr_un 為 POD；全零初始化後再填入 family 與路徑。
    let mut address = unsafe { address.assume_init() };
    address.sun_family = libc::AF_UNIX as libc::sa_family_t;
    let path_capacity = address.sun_path.len();
    let is_abstract = bytes.first() == Some(&0);
    let max_path = if is_abstract {
        path_capacity
    } else {
        path_capacity - 1
    };
    if bytes.len() > max_path {
        return Err(PalError::plain(
            PalErrorKind::InvalidInput,
            "AF_UNIX path too long",
        ));
    }
    for (slot, byte) in address.sun_path.iter_mut().zip(bytes.iter().copied()) {
        *slot = byte as libc::c_char;
    }
    let address_len = (offset_of!(libc::sockaddr_un, sun_path)
        + bytes.len()
        + usize::from(!is_abstract)) as libc::socklen_t;
    let fd = new_socket(libc::AF_UNIX, libc::SOCK_STREAM, 0)?;
    // SAFETY: address 是已初始化的 sockaddr_un，長度依 sun_path 的實際格式計算。
    let status = unsafe {
        libc::connect(
            fd,
            (&address as *const libc::sockaddr_un).cast(),
            address_len,
        )
    };
    if status == 0 {
        return Ok(fd);
    }
    let error = io::Error::last_os_error();
    let code = error.raw_os_error().unwrap_or(libc::EIO);
    if is_connect_pending(code) {
        match timeout {
            SockTimeout::NonBlocking => {
                // SAFETY: 非阻塞 connect 未完成，fd 尚未交給 SocketStream。
                unsafe { libc::close(fd) };
                Err(posix_errno(code))
            }
            SockTimeout::Blocking(duration) => match wait_connect(fd, duration) {
                Ok(()) => Ok(fd),
                Err(error) => {
                    // SAFETY: fd 尚未交給 SocketStream，錯誤路徑由此關閉。
                    unsafe { libc::close(fd) };
                    Err(error)
                }
            },
        }
    } else {
        // SAFETY: fd 尚未交給 SocketStream，錯誤路徑由此關閉。
        unsafe { libc::close(fd) };
        Err(posix_io_error(error))
    }
}

fn connect_tcp(host: &str, port: u16, timeout: SockTimeout) -> PalResult<RawFd> {
    let addresses = resolve(host, port)?;
    let mut last_error = None;
    for address in &addresses.entries {
        let fd = match new_socket(address.ai_family, address.ai_socktype, address.ai_protocol) {
            Ok(fd) => fd,
            Err(error) => {
                last_error = Some(error);
                continue;
            }
        };
        // SAFETY: addrinfo 節點由 getaddrinfo 提供且在 freeaddrinfo 前有效。
        let status = unsafe { libc::connect(fd, address.ai_addr, address.ai_addrlen) };
        if status == 0 {
            return Ok(fd);
        }
        let error = io::Error::last_os_error();
        let code = error.raw_os_error().unwrap_or(libc::EIO);
        let result = if is_connect_pending(code) {
            match timeout {
                SockTimeout::NonBlocking => Err(posix_errno(code)),
                SockTimeout::Blocking(duration) => wait_connect(fd, duration),
            }
        } else {
            Err(posix_io_error(error))
        };
        match result {
            Ok(()) => return Ok(fd),
            Err(error) => {
                // SAFETY: fd 尚未交給 SocketStream，錯誤路徑由此關閉。
                unsafe { libc::close(fd) };
                last_error = Some(error);
            }
        }
    }
    Err(last_error.unwrap_or_else(|| posix_errno(libc::EHOSTUNREACH)))
}

struct AddrInfo(*mut libc::addrinfo);

impl Drop for AddrInfo {
    fn drop(&mut self) {
        // SAFETY: 此指標只由成功的 getaddrinfo 建立，並且只在此處釋放一次。
        if !self.0.is_null() {
            unsafe { libc::freeaddrinfo(self.0) };
        }
    }
}

struct ResolvedAddresses {
    _owner: AddrInfo,
    entries: Vec<SocketAddress>,
}

struct SocketAddress {
    ai_family: libc::c_int,
    ai_socktype: libc::c_int,
    ai_protocol: libc::c_int,
    ai_addr: *mut libc::sockaddr,
    ai_addrlen: libc::socklen_t,
}

impl SocketAddress {
    fn copy_from(info: &libc::addrinfo) -> Self {
        Self {
            ai_family: info.ai_family,
            ai_socktype: info.ai_socktype,
            ai_protocol: info.ai_protocol,
            ai_addr: info.ai_addr,
            ai_addrlen: info.ai_addrlen,
        }
    }
}

fn resolve(host: &str, port: u16) -> PalResult<ResolvedAddresses> {
    let node = CString::new(host)
        .map_err(|_| PalError::plain(PalErrorKind::InvalidInput, "embedded null byte"))?;
    let service = CString::new(port.to_string()).expect("十進位 port 不含 NUL");
    let hints = libc::addrinfo {
        ai_flags: 0,
        ai_family: libc::AF_UNSPEC,
        ai_socktype: libc::SOCK_STREAM,
        ai_protocol: 0,
        ai_addrlen: 0,
        ai_addr: ptr::null_mut(),
        ai_canonname: ptr::null_mut(),
        ai_next: ptr::null_mut(),
    };
    let mut result = ptr::null_mut();
    // SAFETY: C string 與 hints 在呼叫期間有效，result 指向可寫指標槽。
    let status = unsafe { libc::getaddrinfo(node.as_ptr(), service.as_ptr(), &hints, &mut result) };
    if status != 0 {
        // SAFETY: gai_strerror 接受 getaddrinfo 回傳的錯誤碼；結果字串立即複製。
        let message = unsafe {
            let ptr = libc::gai_strerror(status);
            if ptr.is_null() {
                "getaddrinfo failed".to_string()
            } else {
                CStr::from_ptr(ptr).to_string_lossy().into_owned()
            }
        };
        return Err(PalError::new(
            PalErrorKind::Io,
            Some(status),
            None,
            message.clone(),
            format!("[Errno {status}] {message}"),
        ));
    }
    let owner = AddrInfo(result);
    let mut addresses = Vec::new();
    let mut current = result;
    while !current.is_null() {
        // SAFETY: current 由 getaddrinfo 鏈結，owner 尚未釋放。
        let info = unsafe { &*current };
        if !info.ai_addr.is_null() {
            addresses.push(SocketAddress::copy_from(info));
        }
        current = info.ai_next;
    }
    Ok(ResolvedAddresses {
        _owner: owner,
        entries: addresses,
    })
}

fn is_connect_pending(code: i32) -> bool {
    code == libc::EINPROGRESS
}

fn ensure_before_deadline(deadline: Option<Instant>) -> PalResult<()> {
    if deadline.is_some_and(|limit| Instant::now() >= limit) {
        Err(timed_out())
    } else {
        Ok(())
    }
}

fn wait_connect(fd: RawFd, timeout: Duration) -> PalResult<()> {
    wait_until(fd, libc::POLLOUT, Instant::now().checked_add(timeout))?;
    let mut socket_error: libc::c_int = 0;
    let mut length = size_of::<libc::c_int>() as libc::socklen_t;
    // SAFETY: socket_error 和 length 是可寫輸出欄位，fd 為尚開啟的 socket。
    let status = unsafe {
        libc::getsockopt(
            fd,
            libc::SOL_SOCKET,
            libc::SO_ERROR,
            (&mut socket_error as *mut libc::c_int).cast(),
            &mut length,
        )
    };
    if status != 0 {
        return Err(posix_io_error(io::Error::last_os_error()));
    }
    if socket_error == 0 {
        Ok(())
    } else if socket_error == libc::ETIMEDOUT {
        Err(timed_out())
    } else {
        Err(posix_errno(socket_error))
    }
}

fn wait_until(fd: RawFd, events: libc::c_short, deadline: Option<Instant>) -> PalResult<()> {
    loop {
        let timeout_ms = poll_timeout_ms(deadline, Instant::now());
        let mut descriptor = libc::pollfd {
            fd,
            events,
            revents: 0,
        };
        // SAFETY: descriptor 指向單一可寫 pollfd，nfds 與其陣列長度一致。
        let status = unsafe { libc::poll(&mut descriptor, 1, timeout_ms) };
        if status > 0 {
            if descriptor.revents & libc::POLLNVAL != 0 {
                return Err(posix_errno(libc::EBADF));
            }
            return Ok(());
        }
        if status == 0 {
            if poll_deadline_expired(deadline, Instant::now()) {
                return Err(timed_out());
            }
            continue;
        }
        let error = io::Error::last_os_error();
        if error.raw_os_error() == Some(libc::EINTR) {
            continue;
        }
        return Err(posix_io_error(error));
    }
}

fn poll_timeout_ms(deadline: Option<Instant>, now: Instant) -> i32 {
    deadline.map_or(-1, |limit| {
        let remaining = limit.saturating_duration_since(now);
        if remaining.is_zero() {
            0
        } else {
            remaining
                .as_millis()
                .saturating_add(u128::from(remaining.subsec_nanos() % 1_000_000 != 0))
                .min(i32::MAX as u128) as i32
        }
    })
}

fn poll_deadline_expired(deadline: Option<Instant>, now: Instant) -> bool {
    deadline.map_or(true, |limit| now >= limit)
}

#[cfg(test)]
mod review_regression_tests {
    use super::*;

    #[test]
    fn only_einprogress_is_treated_as_pending_connect() {
        assert!(is_connect_pending(libc::EINPROGRESS));
        assert!(!is_connect_pending(libc::EAGAIN));
        assert!(!is_connect_pending(libc::EALREADY));
    }

    #[test]
    fn clamped_poll_timeout_does_not_finish_a_later_deadline() {
        let now = Instant::now();
        let deadline = now + Duration::from_secs((i32::MAX as u64 / 1000) + 60);

        assert_eq!(poll_timeout_ms(Some(deadline), now), i32::MAX);
        assert!(!poll_deadline_expired(Some(deadline), now));
    }

    #[test]
    fn expired_send_deadline_returns_timeout() {
        let deadline = Instant::now() - Duration::from_millis(1);

        assert_eq!(
            ensure_before_deadline(Some(deadline)).unwrap_err().kind,
            PalErrorKind::TimedOut
        );
    }
}
