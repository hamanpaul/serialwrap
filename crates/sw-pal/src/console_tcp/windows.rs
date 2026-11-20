//! Windows（Winsock）TCP console listener 與 socket I/O。

use crate::console_mux::windows::{poll_sockets, WinWakePair};
use crate::console_mux::{MuxHandle, MuxHandleInner};
use crate::console_pty::{ConsoleIo, ConsoleListener};
use crate::traits::{IoWaker, PalError, PalErrorKind, PalResult, ReadOutcome};
use std::collections::HashSet;
use std::net::{Shutdown, TcpListener, TcpStream};
use std::os::windows::io::{AsRawSocket, FromRawSocket, RawSocket};
use std::sync::OnceLock;
use std::time::{Duration, Instant};
use windows_sys::Win32::Networking::WinSock::{
    bind as wsa_bind, getsockopt, listen as wsa_listen, recv, send, setsockopt,
    socket as wsa_socket, WSAGetLastError, WSAStartup, AF_INET, INVALID_SOCKET, IN_ADDR, IN_ADDR_0,
    IPPROTO_TCP, POLLRDNORM, SOCKADDR, SOCKADDR_IN, SOCKET, SOCKET_ERROR, SOCK_STREAM, SOL_SOCKET,
    SO_REUSEADDR, WSADATA, WSAEINTR, WSAEWOULDBLOCK, WSAPOLLFD,
};

/// 啟動 Winsock 一次；與 process lifetime 相同，呼叫後不配對 `WSACleanup`。
pub(crate) fn ensure_winsock() -> PalResult<()> {
    static STARTUP: OnceLock<Result<(), i32>> = OnceLock::new();
    match STARTUP.get_or_init(|| {
        let mut data = WSADATA::default();
        // SAFETY: data 指向可寫 WSADATA，要求 Winsock 2.2。
        let status = unsafe { WSAStartup(0x0202, &mut data) };
        if status == 0 {
            Ok(())
        } else {
            Err(status)
        }
    }) {
        Ok(()) => Ok(()),
        Err(code) => Err(crate::traits::windows_wsa_error(*code)),
    }
}

pub fn bind() -> PalResult<Box<dyn ConsoleListener>> {
    ensure_winsock()?;
    // SAFETY: 參數是有效的 IPv4 TCP socket 組合。
    let raw = unsafe { wsa_socket(i32::from(AF_INET), SOCK_STREAM, IPPROTO_TCP) };
    if raw == INVALID_SOCKET {
        return Err(last_wsa_error());
    }
    // SAFETY: raw 是剛建立且有效的 socket，交由 TcpListener 唯一持有並關閉。
    let listener = unsafe { TcpListener::from_raw_socket(raw as RawSocket) };

    let mut reuseaddr = 1i32;
    // SAFETY: reuseaddr 指向有效整數，長度符合 Winsock `int` 選項。
    if unsafe {
        setsockopt(
            raw,
            SOL_SOCKET,
            SO_REUSEADDR,
            (&mut reuseaddr as *mut i32).cast(),
            std::mem::size_of::<i32>() as i32,
        )
    } == SOCKET_ERROR
    {
        return Err(last_wsa_error());
    }

    let address = SOCKADDR_IN {
        sin_family: AF_INET,
        sin_port: 0,
        sin_addr: IN_ADDR {
            S_un: IN_ADDR_0 {
                S_addr: u32::from_ne_bytes([127, 0, 0, 1]),
            },
        },
        ..SOCKADDR_IN::default()
    };
    // SAFETY: address 是完整 SOCKADDR_IN，長度與結構相符。
    if unsafe {
        wsa_bind(
            raw,
            (&address as *const SOCKADDR_IN).cast::<SOCKADDR>(),
            std::mem::size_of::<SOCKADDR_IN>() as i32,
        )
    } == SOCKET_ERROR
    {
        return Err(last_wsa_error());
    }
    // SAFETY: raw 已成功綁定，backlog 8 與 Python console 契約一致。
    if unsafe { wsa_listen(raw, 8) } == SOCKET_ERROR {
        return Err(last_wsa_error());
    }
    listener.set_nonblocking(true).map_err(map_std_io_error)?;
    let endpoint = listener.local_addr().map_err(map_std_io_error)?.to_string();
    Ok(Box::new(TcpConsoleListener {
        inner: listener,
        endpoint,
    }))
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
        let socket = self.inner.as_raw_socket() as SOCKET;
        let mut descriptor = WSAPOLLFD {
            fd: socket,
            events: POLLRDNORM,
            revents: 0,
        };
        if !poll_sockets(std::slice::from_mut(&mut descriptor), Some(timeout))? {
            return Ok(None);
        }
        let (stream, peer) = match self.inner.accept() {
            Ok(value) => value,
            Err(error)
                if is_would_block(&error) || error.kind() == std::io::ErrorKind::Interrupted =>
            {
                return Ok(None)
            }
            Err(error) => return Err(map_std_io_error(error)),
        };
        stream.set_nonblocking(true).map_err(map_std_io_error)?;
        // Python console 採 best-effort TCP_NODELAY；設定失敗不拒絕已接受的 client。
        let _ = stream.set_nodelay(true);
        let io = TcpConsoleIo {
            stream,
            wake: WinWakePair::new()?,
        };
        Ok(Some((
            Box::new(io),
            format!("{}:{}", peer.ip(), peer.port()),
        )))
    }

    fn mux_handle(&self) -> MuxHandle {
        MuxHandle(MuxHandleInner::WinSocket(
            self.inner.as_raw_socket() as usize
        ))
    }
}

struct TcpConsoleIo {
    stream: TcpStream,
    wake: std::sync::Arc<WinWakePair>,
}

impl ConsoleIo for TcpConsoleIo {
    fn read(&mut self, buffer: &mut [u8], timeout: Option<Duration>) -> PalResult<ReadOutcome> {
        if buffer.is_empty() {
            return Ok(ReadOutcome::Data(0));
        }
        let start = Instant::now();
        let deadline = timeout.and_then(|duration| start.checked_add(duration));
        let finite = timeout.is_some();
        let mut descriptors = [
            WSAPOLLFD {
                fd: self.stream.as_raw_socket() as SOCKET,
                events: POLLRDNORM,
                revents: 0,
            },
            WSAPOLLFD {
                fd: self.wake.read_socket(),
                events: POLLRDNORM,
                revents: 0,
            },
        ];
        loop {
            let remaining = timeout.map(|duration| {
                deadline.map_or_else(
                    || duration.saturating_sub(start.elapsed()),
                    |limit| limit.saturating_duration_since(Instant::now()),
                )
            });
            if !poll_sockets(&mut descriptors, remaining)? {
                return Ok(ReadOutcome::TimedOut);
            }
            if descriptors[1].revents != 0 {
                self.wake.drain();
                return Ok(ReadOutcome::Woken);
            }
            if descriptors[0].revents != 0 {
                let outcome = read_socket(descriptors[0].fd, buffer)?;
                if outcome == ReadOutcome::TimedOut
                    && finite
                    && remaining.is_some_and(|left| !left.is_zero())
                {
                    continue;
                }
                return Ok(outcome);
            }
        }
    }

    fn try_write(&mut self, data: &[u8]) -> PalResult<usize> {
        write_socket(self.stream.as_raw_socket() as SOCKET, data)
    }

    fn waker(&self) -> std::sync::Arc<dyn IoWaker> {
        std::sync::Arc::clone(&self.wake) as std::sync::Arc<dyn IoWaker>
    }

    fn has_external_peer(&self, _held: Option<&HashSet<String>>) -> bool {
        true
    }

    fn close(self: Box<Self>) {
        let _ = self.stream.shutdown(Shutdown::Both);
    }

    fn mux_handle(&self) -> MuxHandle {
        MuxHandle(MuxHandleInner::WinSocket(
            self.stream.as_raw_socket() as usize
        ))
    }
}

pub(crate) fn read_socket(socket: SOCKET, buffer: &mut [u8]) -> PalResult<ReadOutcome> {
    if buffer.is_empty() {
        return Ok(ReadOutcome::Data(0));
    }
    loop {
        let length = buffer.len().min(i32::MAX as usize) as i32;
        // SAFETY: socket 由呼叫端持有；buffer 在同步呼叫期間可寫且長度不超過 i32 上限。
        let received = unsafe { recv(socket, buffer.as_mut_ptr().cast(), length, 0) };
        if received > 0 {
            return Ok(ReadOutcome::Data(received as usize));
        }
        if received == 0 {
            return Ok(ReadOutcome::Eof);
        }
        let code = unsafe { WSAGetLastError() };
        if code == WSAEINTR {
            continue;
        }
        if code == WSAEWOULDBLOCK {
            return Ok(ReadOutcome::TimedOut);
        }
        return Err(crate::traits::windows_wsa_error(code));
    }
}

pub(crate) fn write_socket(socket: SOCKET, data: &[u8]) -> PalResult<usize> {
    if data.is_empty() {
        return Ok(0);
    }
    loop {
        let length = data.len().min(i32::MAX as usize) as i32;
        // SAFETY: socket 由呼叫端持有；data 在同步呼叫期間有效且長度不超過 i32 上限。
        let sent = unsafe { send(socket, data.as_ptr().cast(), length, 0) };
        if sent >= 0 {
            return Ok(sent as usize);
        }
        let code = unsafe { WSAGetLastError() };
        if code == WSAEINTR {
            continue;
        }
        if code == WSAEWOULDBLOCK {
            return Ok(0);
        }
        return Err(crate::traits::windows_wsa_error(code));
    }
}

/// `getsockopt(level, name)` 的整數值（testutil 用）。
#[cfg(feature = "test-fakes")]
pub(crate) fn get_int_opt(socket: SOCKET, level: i32, name: i32) -> Option<i32> {
    let mut value = 0i32;
    let mut length = std::mem::size_of::<i32>() as i32;
    // SAFETY: value 與 length 是有效輸出位置，socket 由 ConsoleIo／ConsoleListener 保持開啟。
    let status = unsafe {
        getsockopt(
            socket,
            level,
            name,
            (&mut value as *mut i32).cast(),
            &mut length,
        )
    };
    (status != SOCKET_ERROR).then_some(value)
}

fn last_wsa_error() -> PalError {
    // SAFETY: 緊接著失敗的 Winsock 呼叫，讀取目前執行緒的最後錯誤。
    crate::traits::windows_wsa_error(unsafe { WSAGetLastError() })
}

fn is_would_block(error: &std::io::Error) -> bool {
    error.raw_os_error() == Some(WSAEWOULDBLOCK) || error.kind() == std::io::ErrorKind::WouldBlock
}

fn map_std_io_error(error: std::io::Error) -> PalError {
    error
        .raw_os_error()
        .map(crate::traits::windows_wsa_error)
        .unwrap_or_else(|| PalError::plain(PalErrorKind::Io, error.to_string()))
}
