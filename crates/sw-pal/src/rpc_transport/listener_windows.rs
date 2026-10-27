//! Windows RPC listener（P5-T17）：只綁 loopback TCP（對應 `sw_core/rpc_win.py::TcpRpcServer`）。
//!
//! AF_UNIX endpoint 一律回 `Unsupported`（Windows daemon RPC plane 只走 TCP loopback，#84 PORT-4）。
//! 原生行為驗證列為 LOCAL-WINDOWS-TODO（雲端只能 cross-check 編譯）。

use crate::traits::{
    EndpointSpec, ListenOptions, PalError, PalErrorKind, PalResult, RpcListener, RpcServerStream,
    windows_wsa_error,
};
use std::net::{TcpListener, ToSocketAddrs};
use std::os::windows::io::AsRawSocket;
use std::time::{Duration, Instant};
use windows_sys::Win32::Networking::WinSock::{
    POLLRDNORM, SOCKET, SOCKET_ERROR, WSAEINTR, WSAGetLastError, WSAPOLLFD, WSAPoll,
};

use super::{SERVER_WRITE_TIMEOUT, check_loopback_host, timeout_ms};

pub(super) fn bind(ep: &EndpointSpec, _opts: &ListenOptions) -> PalResult<Box<dyn RpcListener>> {
    let EndpointSpec::Tcp { host, port } = ep else {
        return Err(PalError::plain(
            PalErrorKind::Unsupported,
            "AF_UNIX is unavailable",
        ));
    };
    check_loopback_host(host)?;
    let addresses: Vec<_> = (host.as_str(), *port)
        .to_socket_addrs()
        .map_err(map_std_io_error)?
        .collect();
    if addresses.is_empty() {
        return Err(PalError::plain(
            PalErrorKind::NotFound,
            format!("no address for {host}"),
        ));
    }
    // asyncio `start_server` 對每個解析位址各綁一個 socket；Windows 不設 SO_REUSEADDR（與 asyncio 預設相同）。
    let mut listeners = Vec::with_capacity(addresses.len());
    for address in addresses {
        let listener = TcpListener::bind(address).map_err(map_std_io_error)?;
        listener.set_nonblocking(true).map_err(map_std_io_error)?;
        listeners.push(listener);
    }
    Ok(Box::new(WinTcpRpcListener { listeners }))
}

fn map_std_io_error(error: std::io::Error) -> PalError {
    error
        .raw_os_error()
        .map(windows_wsa_error)
        .unwrap_or_else(|| PalError::plain(PalErrorKind::Io, error.to_string()))
}

struct WinTcpRpcListener {
    listeners: Vec<TcpListener>,
}

fn poll_readable(sockets: &[SOCKET], timeout: Duration) -> PalResult<Option<usize>> {
    let deadline = Instant::now() + timeout;
    let mut fds: Vec<WSAPOLLFD> = sockets
        .iter()
        .map(|&fd| WSAPOLLFD {
            fd,
            events: POLLRDNORM,
            revents: 0,
        })
        .collect();
    loop {
        let remaining = deadline.saturating_duration_since(Instant::now());
        for f in &mut fds {
            f.revents = 0;
        }
        // SAFETY: fds 為有效的 WSAPOLLFD 陣列，長度與傳入一致。
        let n = unsafe { WSAPoll(fds.as_mut_ptr(), fds.len() as u32, timeout_ms(remaining)) };
        if n == SOCKET_ERROR {
            // SAFETY: 讀取目前執行緒最近一次 Winsock 錯誤。
            let code = unsafe { WSAGetLastError() };
            if code == WSAEINTR {
                continue;
            }
            return Err(windows_wsa_error(code));
        }
        if n == 0 {
            return Ok(None);
        }
        return Ok(fds.iter().position(|f| f.revents != 0));
    }
}

impl RpcListener for WinTcpRpcListener {
    fn accept(&mut self, timeout: Duration) -> PalResult<Option<Box<dyn RpcServerStream>>> {
        let sockets: Vec<SOCKET> = self
            .listeners
            .iter()
            .map(|l| l.as_raw_socket() as SOCKET)
            .collect();
        let Some(index) = poll_readable(&sockets, timeout)? else {
            return Ok(None);
        };
        match self.listeners[index].accept() {
            Ok((stream, _)) => {
                // Windows 的 accepted socket 會繼承 listener 的非阻塞模式，需明確切回阻塞。
                stream.set_nonblocking(false).map_err(map_std_io_error)?;
                stream
                    .set_write_timeout(Some(SERVER_WRITE_TIMEOUT))
                    .map_err(map_std_io_error)?;
                Ok(Some(Box::new(stream)))
            }
            Err(error)
                if matches!(
                    error.kind(),
                    std::io::ErrorKind::WouldBlock
                        | std::io::ErrorKind::Interrupted
                        | std::io::ErrorKind::ConnectionAborted
                        | std::io::ErrorKind::ConnectionReset
                ) =>
            {
                Ok(None)
            }
            Err(error) => Err(map_std_io_error(error)),
        }
    }

    fn close(self: Box<Self>) {}
}
