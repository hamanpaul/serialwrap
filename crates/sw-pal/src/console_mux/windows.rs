//! Windows ConsoleMux：以 `WSAPoll` 等待 TCP socket 與 loopback socket waker。

use crate::console_mux::{ConsoleMux, MuxEntry, MuxEvent, MuxHandle, MuxHandleInner, MuxId};
use crate::console_pty::{ConsoleIo, ConsoleListener};
use crate::console_tcp::windows::{read_socket, write_socket};
use crate::traits::{IoWaker, PalError, PalErrorKind, PalResult, ReadOutcome};
use std::collections::BTreeMap;
use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::os::windows::io::AsRawSocket;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};
use windows_sys::Win32::Networking::WinSock::{
    WSAGetLastError, WSAPoll, POLLERR, POLLHUP, POLLRDNORM, POLLWRNORM, SOCKET, WSAEINTR, WSAPOLLFD,
};

enum Slot {
    Io(Box<dyn ConsoleIo>),
    Listener(Box<dyn ConsoleListener>),
}

struct Entry {
    socket: SOCKET,
    write_interest: bool,
    slot: Slot,
}

/// Winsock socket pair。pending 閘門讓連續 `wake()` 最多送出一個 byte。
pub(crate) struct WinWakePair {
    reader: TcpStream,
    writer: TcpStream,
    pending: Mutex<bool>,
}

impl WinWakePair {
    pub(crate) fn new() -> PalResult<Arc<WinWakePair>> {
        crate::console_tcp::windows::ensure_winsock()?;
        let listener =
            TcpListener::bind((std::net::Ipv4Addr::LOCALHOST, 0)).map_err(map_std_io_error)?;
        let address = listener.local_addr().map_err(map_std_io_error)?;
        let writer = TcpStream::connect(address).map_err(map_std_io_error)?;
        let (reader, _) = listener.accept().map_err(map_std_io_error)?;
        reader.set_nonblocking(true).map_err(map_std_io_error)?;
        writer.set_nonblocking(true).map_err(map_std_io_error)?;
        Ok(Arc::new(WinWakePair {
            reader,
            writer,
            pending: Mutex::new(false),
        }))
    }

    pub(crate) fn read_socket(&self) -> SOCKET {
        self.reader.as_raw_socket() as SOCKET
    }

    pub(crate) fn drain(&self) {
        let mut pending = self
            .pending
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        let mut buffer = [0u8; 128];
        loop {
            match (&self.reader).read(&mut buffer) {
                Ok(count) if count > 0 => continue,
                Err(error) if error.kind() == std::io::ErrorKind::Interrupted => continue,
                _ => break,
            }
        }
        *pending = false;
    }
}

impl IoWaker for WinWakePair {
    fn wake(&self) {
        let mut pending = self
            .pending
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        if *pending {
            return;
        }
        match (&self.writer).write(&[1]) {
            Ok(count) if count > 0 => *pending = true,
            Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => *pending = true,
            _ => {}
        }
    }
}

/// 在 `WSAPoll` 上等待 socket 集合；逾時採毫秒向上取整並以單調時鐘維持期限。
pub(crate) fn poll_sockets(
    descriptors: &mut [WSAPOLLFD],
    timeout: Option<Duration>,
) -> PalResult<bool> {
    let start = Instant::now();
    let deadline = timeout.map(|duration| start.checked_add(duration));
    loop {
        for descriptor in descriptors.iter_mut() {
            descriptor.revents = 0;
        }
        let timeout_ms = match deadline {
            None => -1,
            Some(None) => i32::MAX,
            Some(Some(limit)) => duration_ms(limit.saturating_duration_since(Instant::now())),
        };
        // SAFETY: descriptors 是有效的 WSAPOLLFD 陣列，socket 由對應 listener／stream 持有。
        let status = unsafe {
            WSAPoll(
                descriptors.as_mut_ptr(),
                descriptors.len() as u32,
                timeout_ms,
            )
        };
        if status > 0 {
            if descriptors.iter().any(|descriptor| descriptor.revents != 0) {
                return Ok(true);
            }
            continue;
        }
        if status == 0 {
            match deadline {
                Some(Some(limit)) if Instant::now() >= limit => return Ok(false),
                _ => continue,
            }
        }
        // SAFETY: 讀取目前執行緒最近一次 Winsock 錯誤。
        let code = unsafe { WSAGetLastError() };
        if code == WSAEINTR {
            continue;
        }
        return Err(crate::traits::windows_wsa_error(code));
    }
}

fn duration_ms(remaining: Duration) -> i32 {
    if remaining.is_zero() {
        return 0;
    }
    remaining
        .as_nanos()
        .div_ceil(1_000_000)
        .min(i32::MAX as u128) as i32
}

pub struct WinConsoleMux {
    entries: BTreeMap<MuxId, Entry>,
    wake: Arc<WinWakePair>,
}

impl WinConsoleMux {
    pub fn create() -> PalResult<Box<dyn ConsoleMux>> {
        Ok(Box::new(WinConsoleMux {
            entries: BTreeMap::new(),
            wake: WinWakePair::new()?,
        }))
    }

    fn insert(&mut self, id: MuxId, handle: MuxHandle, slot: Slot) -> PalResult<()> {
        let MuxHandleInner::WinSocket(socket) = handle.0 else {
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
                socket: socket as SOCKET,
                write_interest: false,
                slot,
            },
        );
        Ok(())
    }

    fn io_socket(&self, id: MuxId) -> PalResult<SOCKET> {
        match self.entries.get(&id) {
            Some(Entry {
                socket,
                slot: Slot::Io(_),
                ..
            }) => Ok(*socket),
            _ => Err(unknown_id(id)),
        }
    }
}

fn unknown_id(id: MuxId) -> PalError {
    PalError::plain(
        PalErrorKind::NotFound,
        format!("ConsoleMux：未登記的 io id {id}"),
    )
}

impl ConsoleMux for WinConsoleMux {
    fn add_io(&mut self, id: MuxId, io: Box<dyn ConsoleIo>) -> PalResult<()> {
        let handle = io.mux_handle();
        self.insert(id, handle, Slot::Io(io))
    }

    fn add_listener(&mut self, id: MuxId, listener: Box<dyn ConsoleListener>) -> PalResult<()> {
        let handle = listener.mux_handle();
        self.insert(id, handle, Slot::Listener(listener))
    }

    fn remove(&mut self, id: MuxId) -> Option<MuxEntry> {
        self.entries.remove(&id).map(|entry| match entry.slot {
            Slot::Io(io) => MuxEntry::Io(io),
            Slot::Listener(listener) => MuxEntry::Listener(listener),
        })
    }

    fn set_write_interest(&mut self, id: MuxId, on: bool) {
        if let Some(entry) = self.entries.get_mut(&id) {
            entry.write_interest = on;
        }
    }

    fn wait(&mut self, timeout: Duration) -> PalResult<Vec<MuxEvent>> {
        let mut descriptors = Vec::with_capacity(self.entries.len() + 1);
        descriptors.push(WSAPOLLFD {
            fd: self.wake.read_socket(),
            events: POLLRDNORM,
            revents: 0,
        });
        let ids: Vec<MuxId> = self.entries.keys().copied().collect();
        for entry in self.entries.values() {
            let mut events = POLLRDNORM;
            if entry.write_interest && matches!(entry.slot, Slot::Io(_)) {
                events |= POLLWRNORM;
            }
            descriptors.push(WSAPOLLFD {
                fd: entry.socket,
                events,
                revents: 0,
            });
        }
        if !poll_sockets(&mut descriptors, Some(timeout))? {
            return Ok(Vec::new());
        }
        let mut events = Vec::new();
        if descriptors[0].revents != 0 {
            self.wake.drain();
            events.push(MuxEvent::Woken);
        }
        for (descriptor, id) in descriptors[1..].iter().zip(ids) {
            let ready = descriptor.revents;
            if ready == 0 {
                continue;
            }
            let is_listener = matches!(
                self.entries.get(&id).map(|entry| &entry.slot),
                Some(Slot::Listener(_))
            );
            if is_listener {
                if ready & POLLRDNORM != 0 {
                    events.push(MuxEvent::Acceptable(id));
                }
                continue;
            }
            if ready & POLLRDNORM != 0 {
                events.push(MuxEvent::Readable(id));
            } else if ready & (POLLHUP | POLLERR) != 0 {
                events.push(MuxEvent::Hangup(id));
            }
            if ready & POLLWRNORM != 0 {
                events.push(MuxEvent::Writable(id));
            }
        }
        Ok(events)
    }

    fn read(&mut self, id: MuxId, buffer: &mut [u8]) -> PalResult<ReadOutcome> {
        read_socket(self.io_socket(id)?, buffer)
    }

    fn try_write(&mut self, id: MuxId, data: &[u8]) -> PalResult<usize> {
        write_socket(self.io_socket(id)?, data)
    }

    fn accept(&mut self, id: MuxId) -> PalResult<Option<(Box<dyn ConsoleIo>, String)>> {
        match self.entries.get_mut(&id) {
            Some(Entry {
                slot: Slot::Listener(listener),
                ..
            }) => listener.accept(Duration::ZERO),
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

fn map_std_io_error(error: std::io::Error) -> PalError {
    error
        .raw_os_error()
        .map(crate::traits::windows_wsa_error)
        .unwrap_or_else(|| PalError::plain(PalErrorKind::Io, error.to_string()))
}
