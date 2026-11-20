//! Windows Winsock TCP RPC client。

use crate::traits::{
    timed_out, windows_gai_error, windows_wsa_error, EndpointSpec, PalError, PalErrorKind,
    PalResult, RpcConnector, RpcServerStream, RpcStream, SockTimeout,
};
use std::net::{SocketAddr, ToSocketAddrs};
use std::sync::OnceLock;
use std::time::Instant;

impl RpcServerStream for std::net::TcpStream {
    fn shutdown_read(&mut self) -> PalResult<()> {
        self.shutdown(std::net::Shutdown::Read)
            .map_err(map_std_io_error)
    }

    fn set_read_timeout(&mut self, duration: Option<std::time::Duration>) -> PalResult<()> {
        std::net::TcpStream::set_read_timeout(self, duration).map_err(map_std_io_error)
    }
}

fn map_std_io_error(error: std::io::Error) -> PalError {
    error
        .raw_os_error()
        .map(windows_wsa_error)
        .unwrap_or_else(|| PalError::plain(PalErrorKind::Io, error.to_string()))
}
use windows_sys::Win32::Networking::WinSock::{
    closesocket, connect, getsockopt, ioctlsocket, recv, send, socket, WSAGetLastError, WSAStartup,
    AF_INET, AF_INET6, FIONBIO, IN6_ADDR, IN6_ADDR_0, INVALID_SOCKET, IN_ADDR, IN_ADDR_0,
    IPPROTO_TCP, POLLERR, POLLHUP, POLLRDNORM, POLLWRNORM, SOCKADDR, SOCKADDR_IN, SOCKADDR_IN6,
    SOCKADDR_IN6_0, SOCKET, SOCKET_ERROR, SOCK_STREAM, SOL_SOCKET, SO_ERROR, WSADATA,
    WSAEINPROGRESS, WSAEINTR, WSAEWOULDBLOCK, WSAPOLLFD,
};

#[derive(Clone, Copy, Debug, Default)]
pub struct NativeRpcConnector;

struct SocketStream {
    socket: SOCKET,
    timeout: SockTimeout,
}

impl Drop for SocketStream {
    fn drop(&mut self) {
        // SAFETY: socket 由本模組建立，且此 Drop 是唯一關閉路徑。
        unsafe { closesocket(self.socket) };
    }
}

impl RpcConnector for NativeRpcConnector {
    fn af_unix_available(&self) -> bool {
        false
    }

    fn connect(
        &self,
        endpoint: &EndpointSpec,
        timeout: SockTimeout,
    ) -> PalResult<Box<dyn RpcStream>> {
        let EndpointSpec::Tcp { host, port } = endpoint else {
            return Err(PalError::plain(
                PalErrorKind::Unsupported,
                "AF_UNIX is unavailable",
            ));
        };
        let _ = winsock_startup()?;
        let addresses = resolve(host, *port)?;
        let mut last_error = None;
        for address in addresses {
            match connect_address(address, timeout) {
                Ok(socket) => return Ok(Box::new(SocketStream { socket, timeout })),
                Err(error) => last_error = Some(error),
            }
        }
        Err(last_error.unwrap_or_else(|| windows_gai_error(11001)))
    }

    fn resolve_tcp(&self, host: &str, port: u16) -> PalResult<()> {
        let _ = winsock_startup()?;
        resolve(host, port).map(|_| ())
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
            let count = (data.len() - offset).min(i32::MAX as usize) as i32;
            // SAFETY: socket 有效；送出的 slice 長度不超過 i32 上限且在呼叫期間有效。
            let sent = unsafe { send(self.socket, data[offset..].as_ptr().cast(), count, 0) };
            if sent > 0 {
                offset += sent as usize;
                continue;
            }
            if sent == 0 {
                return Err(windows_wsa_error(10054));
            }
            // SAFETY: 讀取目前執行緒最近一次 Winsock 錯誤。
            let code = unsafe { WSAGetLastError() };
            if code == WSAEINTR {
                continue;
            }
            if code == WSAEWOULDBLOCK {
                match self.timeout {
                    SockTimeout::NonBlocking => return Err(windows_wsa_error(code)),
                    SockTimeout::Blocking(_) => wait_socket(self.socket, POLLWRNORM, deadline)?,
                }
                continue;
            }
            return Err(windows_wsa_error(code));
        }
        Ok(())
    }

    fn recv(&mut self, buffer: &mut [u8]) -> PalResult<usize> {
        if buffer.is_empty() {
            return Ok(0);
        }
        if let SockTimeout::Blocking(duration) = self.timeout {
            wait_socket(
                self.socket,
                POLLRDNORM,
                Instant::now().checked_add(duration),
            )?;
        }
        let count = buffer.len().min(i32::MAX as usize) as i32;
        // SAFETY: socket 有效；可寫 buffer 長度不超過 i32 上限且在呼叫期間有效。
        let received = unsafe { recv(self.socket, buffer.as_mut_ptr().cast(), count, 0) };
        if received >= 0 {
            return Ok(received as usize);
        }
        // SAFETY: 讀取目前執行緒最近一次 Winsock 錯誤。
        let code = unsafe { WSAGetLastError() };
        if code == 10060 {
            Err(timed_out())
        } else {
            Err(windows_wsa_error(code))
        }
    }
}

fn winsock_startup() -> PalResult<()> {
    static STARTUP: OnceLock<Result<(), i32>> = OnceLock::new();
    match STARTUP.get_or_init(|| {
        let mut data = WSADATA::default();
        // SAFETY: data 指向可寫 WSADATA 結構，版本請求為 Winsock 2.2。
        let status = unsafe { WSAStartup(0x0202, &mut data) };
        if status == 0 {
            Ok(())
        } else {
            Err(status)
        }
    }) {
        Ok(()) => Ok(()),
        Err(code) => Err(windows_wsa_error(*code)),
    }
}

fn resolve(host: &str, port: u16) -> PalResult<Vec<SocketAddr>> {
    match (host, port).to_socket_addrs() {
        Ok(addresses) => {
            let result: Vec<_> = addresses.collect();
            if result.is_empty() {
                Err(windows_gai_error(11001))
            } else {
                Ok(result)
            }
        }
        Err(error) => {
            let code = error.raw_os_error().unwrap_or(11001);
            Err(windows_gai_error(code))
        }
    }
}

fn connect_address(address: SocketAddr, timeout: SockTimeout) -> PalResult<SOCKET> {
    let (family, sockaddr, length): (i32, SockAddr, i32) = match address {
        SocketAddr::V4(address) => {
            let mut raw = SOCKADDR_IN::default();
            raw.sin_family = AF_INET;
            raw.sin_port = address.port().to_be();
            raw.sin_addr = IN_ADDR {
                S_un: IN_ADDR_0 {
                    S_addr: u32::from_ne_bytes(address.ip().octets()),
                },
            };
            (
                i32::from(AF_INET),
                SockAddr::V4(raw),
                std::mem::size_of::<SOCKADDR_IN>() as i32,
            )
        }
        SocketAddr::V6(address) => {
            let mut raw = SOCKADDR_IN6::default();
            raw.sin6_family = AF_INET6;
            raw.sin6_port = address.port().to_be();
            raw.sin6_flowinfo = address.flowinfo().to_be();
            raw.sin6_addr = IN6_ADDR {
                u: IN6_ADDR_0 {
                    Byte: address.ip().octets(),
                },
            };
            raw.Anonymous = SOCKADDR_IN6_0 {
                sin6_scope_id: address.scope_id(),
            };
            (
                i32::from(AF_INET6),
                SockAddr::V6(raw),
                std::mem::size_of::<SOCKADDR_IN6>() as i32,
            )
        }
    };
    // SAFETY: family、SOCK_STREAM 與 IPPROTO_TCP 為有效 Winsock socket 參數。
    let socket_handle = unsafe { socket(family, SOCK_STREAM, IPPROTO_TCP) };
    if socket_handle == INVALID_SOCKET {
        // SAFETY: 讀取目前執行緒最近一次 Winsock 錯誤。
        return Err(windows_wsa_error(unsafe { WSAGetLastError() }));
    }
    let mut nonblocking = 1u32;
    // SAFETY: socket_handle 有效，nonblocking 為 FIONBIO 所需的可讀寫 DWORD。
    if unsafe { ioctlsocket(socket_handle, FIONBIO, &mut nonblocking) } == SOCKET_ERROR {
        // SAFETY: 先保存 Winsock 錯誤，再關閉剛建立的 socket。
        let error = windows_wsa_error(unsafe { WSAGetLastError() });
        unsafe { closesocket(socket_handle) };
        return Err(error);
    }
    let address_ptr = match &sockaddr {
        SockAddr::V4(value) => (value as *const SOCKADDR_IN).cast::<SOCKADDR>(),
        SockAddr::V6(value) => (value as *const SOCKADDR_IN6).cast::<SOCKADDR>(),
    };
    // SAFETY: sockaddr 指向依相應 C ABI 結構配置、仍存活且對齊的位址資料。
    let status = unsafe { connect(socket_handle, address_ptr, length) };
    if status == 0 {
        return Ok(socket_handle);
    }
    // SAFETY: 讀取 connect 最近一次 Winsock 錯誤。
    let code = unsafe { WSAGetLastError() };
    let result = if code == WSAEWOULDBLOCK || code == WSAEINPROGRESS {
        match timeout {
            SockTimeout::NonBlocking => Err(windows_wsa_error(code)),
            SockTimeout::Blocking(duration) => {
                wait_connect(socket_handle, Instant::now().checked_add(duration))
            }
        }
    } else {
        Err(windows_wsa_error(code))
    };
    match result {
        Ok(()) => Ok(socket_handle),
        Err(error) => {
            // SAFETY: 失敗連線不會交給 RpcStream，於此關閉一次。
            unsafe { closesocket(socket_handle) };
            Err(error)
        }
    }
}

enum SockAddr {
    V4(SOCKADDR_IN),
    V6(SOCKADDR_IN6),
}

fn wait_connect(socket_handle: SOCKET, deadline: Option<Instant>) -> PalResult<()> {
    wait_socket(socket_handle, POLLWRNORM, deadline)?;
    let mut error = 0i32;
    let mut length = std::mem::size_of::<i32>() as i32;
    // SAFETY: error 和 length 是可寫輸出欄位，socket_handle 仍開啟。
    let status = unsafe {
        getsockopt(
            socket_handle,
            SOL_SOCKET,
            SO_ERROR,
            (&mut error as *mut i32).cast(),
            &mut length,
        )
    };
    if status == SOCKET_ERROR {
        // SAFETY: 讀取 getsockopt 最近一次 Winsock 錯誤。
        return Err(windows_wsa_error(unsafe { WSAGetLastError() }));
    }
    if error == 0 {
        Ok(())
    } else if error == 10060 {
        Err(timed_out())
    } else {
        Err(windows_wsa_error(error))
    }
}

fn wait_socket(socket_handle: SOCKET, events: i16, deadline: Option<Instant>) -> PalResult<()> {
    loop {
        let timeout_ms = poll_timeout_ms(deadline, Instant::now());
        let mut descriptor = WSAPOLLFD {
            fd: socket_handle,
            events,
            revents: 0,
        };
        // SAFETY: descriptor 指向單一 WSAPOLLFD，socket_handle 為有效 Winsock socket。
        let status = unsafe {
            windows_sys::Win32::Networking::WinSock::WSAPoll(&mut descriptor, 1, timeout_ms)
        };
        if status > 0 {
            if descriptor.revents & (POLLERR | POLLHUP) != 0 {
                let mut error = 0i32;
                let mut length = std::mem::size_of::<i32>() as i32;
                // SAFETY: error 和 length 是可寫輸出欄位，socket_handle 仍開啟。
                let got = unsafe {
                    getsockopt(
                        socket_handle,
                        SOL_SOCKET,
                        SO_ERROR,
                        (&mut error as *mut i32).cast(),
                        &mut length,
                    )
                };
                if got != SOCKET_ERROR && error != 0 {
                    return Err(windows_wsa_error(error));
                }
            }
            return Ok(());
        }
        if status == 0 {
            if poll_deadline_expired(deadline, Instant::now()) {
                return Err(timed_out());
            }
            continue;
        }
        // SAFETY: 讀取 WSAPoll 最近一次 Winsock 錯誤。
        let code = unsafe { WSAGetLastError() };
        if code == WSAEINTR {
            continue;
        }
        return Err(windows_wsa_error(code));
    }
}

fn ensure_before_deadline(deadline: Option<Instant>) -> PalResult<()> {
    if deadline.is_some_and(|limit| Instant::now() >= limit) {
        Err(timed_out())
    } else {
        Ok(())
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
    use std::time::Duration;

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
