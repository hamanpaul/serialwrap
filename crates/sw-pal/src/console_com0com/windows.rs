//! Windows com0com console-side 埠。

use crate::console_mux::windows::{poll_sockets, WinWakePair};
use crate::console_mux::MuxHandle;
use crate::console_mux::MuxHandleInner;
use crate::console_pty::ConsoleIo;
use crate::serial::windows_model::win_device_name;
use crate::traits::{IoWaker, PalError, PalErrorKind, PalResult, ReadOutcome};
use crossbeam_channel::{bounded, Receiver, SendTimeoutError, Sender, TryRecvError, TrySendError};
use std::collections::{HashMap, VecDeque};
use std::fs::File;
use std::os::windows::io::{AsRawHandle, FromRawHandle, RawHandle};
use std::sync::atomic::{AtomicBool, AtomicU32, AtomicU64, Ordering};
use std::sync::{Arc, Mutex, OnceLock, Weak};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};
use windows_sys::Win32::Devices::Communication::{
    SetCommState, SetCommTimeouts, COMMTIMEOUTS, DCB, NOPARITY, ONESTOPBIT,
};
use windows_sys::Win32::Foundation::{
    CloseHandle, GetLastError, ERROR_IO_PENDING, ERROR_OPERATION_ABORTED, GENERIC_READ,
    GENERIC_WRITE, HANDLE, INVALID_HANDLE_VALUE, WAIT_OBJECT_0, WAIT_TIMEOUT,
};
use windows_sys::Win32::Storage::FileSystem::{
    CreateFileW, ReadFile, WriteFile, FILE_FLAG_OVERLAPPED, OPEN_EXISTING,
};
use windows_sys::Win32::System::Threading::{
    CreateEventW, ResetEvent, WaitForSingleObject, INFINITE,
};
use windows_sys::Win32::System::IO::{CancelIoEx, GetOverlappedResult, OVERLAPPED};

const READER_CHANNEL_CAPACITY: usize = 64;
const READER_BUFFER_BYTES: usize = 4096;
const READER_SEND_RETRY: Duration = Duration::from_millis(100);
const WRITER_CHANNEL_CAPACITY: usize = 64;
const WRITER_BUFFER_BYTES: usize = 4096;

static NEXT_ID: AtomicU64 = AtomicU64::new(1);
static IO_REGISTRY: OnceLock<Mutex<HashMap<u64, Weak<Com0comState>>>> = OnceLock::new();

#[derive(Default)]
struct CommApiCounters {
    set_comm_state: AtomicU32,
    escape_comm_function: AtomicU32,
}

enum ReaderEvent {
    Data(Vec<u8>),
    Failed(PalError),
}

struct ReadQueue {
    receiver: Receiver<ReaderEvent>,
    buffered: VecDeque<u8>,
    error: Option<PalError>,
    closed: bool,
}

struct Com0comState {
    reads: Mutex<ReadQueue>,
    write_error: Mutex<Option<PalError>>,
    write_start: Mutex<()>,
    local_wake: Arc<WinWakePair>,
    mux_wake: Mutex<Option<Weak<WinWakePair>>>,
    stopping: AtomicBool,
    counters: CommApiCounters,
}

impl Com0comState {
    fn collect(&self) -> bool {
        let mut reads = self.reads.lock().unwrap_or_else(|error| error.into_inner());
        // 只把一個 chunk 移入讀取緩衝；未消費的資料仍留在容量 64 的 channel，避免緩衝無界增長。
        if reads.buffered.is_empty() && reads.error.is_none() && !reads.closed {
            match reads.receiver.try_recv() {
                Ok(ReaderEvent::Data(bytes)) => reads.buffered.extend(bytes),
                Ok(ReaderEvent::Failed(error)) => {
                    reads.error = Some(error);
                    reads.closed = true;
                }
                Err(TryRecvError::Empty | TryRecvError::Disconnected) => {}
            }
        }
        !reads.buffered.is_empty() || reads.error.is_some() || reads.closed
    }

    fn read_ready(&self, buffer: &mut [u8]) -> PalResult<Option<ReadOutcome>> {
        let mut reads = self.reads.lock().unwrap_or_else(|error| error.into_inner());
        if !reads.buffered.is_empty() {
            let count = buffer.len().min(reads.buffered.len());
            for (slot, byte) in buffer.iter_mut().zip(reads.buffered.drain(..count)) {
                *slot = byte;
            }
            return Ok(Some(ReadOutcome::Data(count)));
        }
        if let Some(error) = reads.error.take() {
            return Err(error);
        }
        if reads.closed {
            return Ok(Some(ReadOutcome::Eof));
        }
        Ok(None)
    }

    fn read(&self, buffer: &mut [u8], timeout: Option<Duration>) -> PalResult<ReadOutcome> {
        if buffer.is_empty() {
            return Ok(ReadOutcome::Data(0));
        }
        let start = Instant::now();
        let deadline = timeout.map(|duration| start.checked_add(duration));
        loop {
            self.collect();
            if let Some(outcome) = self.read_ready(buffer)? {
                return Ok(outcome);
            }
            let remaining = match deadline {
                None => None,
                Some(Some(limit)) => Some(limit.saturating_duration_since(Instant::now())),
                Some(None) => Some(Duration::MAX),
            };
            if remaining.is_some_and(|duration| duration.is_zero()) {
                return Ok(ReadOutcome::TimedOut);
            }
            let mut descriptor = windows_sys::Win32::Networking::WinSock::WSAPOLLFD {
                fd: self.local_wake.read_socket(),
                events: windows_sys::Win32::Networking::WinSock::POLLRDNORM,
                revents: 0,
            };
            if !poll_sockets(std::slice::from_mut(&mut descriptor), remaining)? {
                return Ok(ReadOutcome::TimedOut);
            }
            if descriptor.revents != 0 {
                self.local_wake.drain();
                self.collect();
                if let Some(outcome) = self.read_ready(buffer)? {
                    return Ok(outcome);
                }
                return Ok(ReadOutcome::Woken);
            }
        }
    }

    fn wait_readable(&self, timeout: Option<Duration>) -> PalResult<crate::console_pty::Readiness> {
        if self.collect() {
            return Ok(crate::console_pty::Readiness::Ready);
        }
        let mut descriptor = windows_sys::Win32::Networking::WinSock::WSAPOLLFD {
            fd: self.local_wake.read_socket(),
            events: windows_sys::Win32::Networking::WinSock::POLLRDNORM,
            revents: 0,
        };
        if !poll_sockets(std::slice::from_mut(&mut descriptor), timeout)? {
            return Ok(crate::console_pty::Readiness::TimedOut);
        }
        self.local_wake.drain();
        if self.collect() {
            Ok(crate::console_pty::Readiness::Ready)
        } else {
            Ok(crate::console_pty::Readiness::Woken)
        }
    }

    fn notify(&self) {
        self.local_wake.wake();
        let mux_wake = self
            .mux_wake
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .as_ref()
            .and_then(Weak::upgrade);
        if let Some(wake) = mux_wake {
            wake.wake();
        }
    }

    fn writer_failed(&self) -> bool {
        self.write_error
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .is_some()
    }

    fn fail_writer(&self, error: PalError) {
        if self.stopping.load(Ordering::SeqCst) {
            return;
        }
        let mut write_error = self
            .write_error
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        if write_error.is_none() {
            *write_error = Some(error);
        }
        drop(write_error);
        self.notify();
    }
}

struct Com0comConsoleIo {
    id: u64,
    file: File,
    state: Arc<Com0comState>,
    writer_tx: Option<Sender<Vec<u8>>>,
    writer: Option<JoinHandle<()>>,
    reader: Option<JoinHandle<()>>,
}

impl Drop for Com0comConsoleIo {
    fn drop(&mut self) {
        let handle = self.file.as_raw_handle() as HANDLE;
        // 與 writer 的 WriteFile 啟動序列化：取得鎖後設 stopping 並取消所有在途 I/O，
        // 避免 writer 在取消之後才開始新的 overlapped write。
        let _write_start = self
            .state
            .write_start
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        self.state.stopping.store(true, Ordering::SeqCst);
        // SAFETY: handle 由 `file` 持有；null OVERLAPPED 表示取消此 handle 上所有在途 I/O。
        unsafe { CancelIoEx(handle, std::ptr::null()) };
        self.writer_tx.take();
        self.state.local_wake.wake();
        self.state
            .mux_wake
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .take();
        registry()
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .remove(&self.id);
        drop(_write_start);
        if let Some(writer) = self.writer.take() {
            let _ = writer.join();
        }
        if let Some(reader) = self.reader.take() {
            let _ = reader.join();
        }
    }
}

impl crate::console_pty::ConsoleIo for Com0comConsoleIo {
    fn read(&mut self, buffer: &mut [u8], timeout: Option<Duration>) -> PalResult<ReadOutcome> {
        self.state.read(buffer, timeout)
    }

    fn try_write(&mut self, data: &[u8]) -> PalResult<usize> {
        if data.is_empty() {
            return Ok(0);
        }
        if let Some(error) = self
            .state
            .write_error
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .clone()
        {
            return Err(error);
        }
        if self.state.stopping.load(Ordering::SeqCst) {
            return Err(PalError::plain(
                PalErrorKind::Cancelled,
                "com0com console 正在關閉",
            ));
        }
        let count = data.len().min(WRITER_BUFFER_BYTES);
        let Some(sender) = self.writer_tx.as_ref() else {
            return Err(PalError::plain(
                PalErrorKind::Cancelled,
                "com0com console writer 已關閉",
            ));
        };
        match sender.try_send(data[..count].to_vec()) {
            Ok(()) => Ok(count),
            Err(TrySendError::Full(_)) => Ok(0),
            Err(TrySendError::Disconnected(_)) => Err(PalError::plain(
                PalErrorKind::Io,
                "com0com console writer 已停止",
            )),
        }
    }

    fn waker(&self) -> Arc<dyn IoWaker> {
        Arc::clone(&self.state.local_wake) as Arc<dyn IoWaker>
    }

    fn has_external_peer(&self, _held: Option<&std::collections::HashSet<String>>) -> bool {
        true
    }

    fn close(self: Box<Self>) {}

    fn mux_handle(&self) -> MuxHandle {
        MuxHandle(MuxHandleInner::Com0com(self.id))
    }

    fn wait_readable(
        &mut self,
        timeout: Option<Duration>,
    ) -> PalResult<crate::console_pty::Readiness> {
        self.state.wait_readable(timeout)
    }
}

pub fn open(port: &str) -> PalResult<Box<dyn ConsoleIo>> {
    let path = console_port_path(port)?;
    let wide_path: Vec<u16> = path.encode_utf16().chain(std::iter::once(0)).collect();
    // SAFETY: `wide_path` 為有效 NUL 結尾 UTF-16；成功 handle 立即交由 `File` 唯一持有。
    let raw = unsafe {
        CreateFileW(
            wide_path.as_ptr(),
            GENERIC_READ | GENERIC_WRITE,
            0,
            std::ptr::null(),
            OPEN_EXISTING,
            FILE_FLAG_OVERLAPPED,
            std::ptr::null_mut(),
        )
    };
    if raw == INVALID_HANDLE_VALUE {
        return Err(last_error());
    }
    // SAFETY: `raw` 是剛建立的通訊埠 handle，交由 File 唯一關閉。
    let file = unsafe { File::from_raw_handle(raw as RawHandle) };
    let handle = file.as_raw_handle() as HANDLE;
    let counters = CommApiCounters::default();
    let dcb = DCB {
        DCBlength: std::mem::size_of::<DCB>() as u32,
        BaudRate: 115_200,
        ByteSize: 8,
        Parity: NOPARITY,
        StopBits: ONESTOPBIT,
        // fBinary=1；不啟用流控，也不主動操作 DTR／RTS。
        _bitfield: 1,
        ..DCB::default()
    };
    counters.set_comm_state.fetch_add(1, Ordering::SeqCst);
    // SAFETY: DCB 欄位已完整初始化，通訊 handle 在 `file` 存活期間有效。
    if unsafe { SetCommState(handle, &dcb) } == 0 {
        return Err(last_error());
    }

    let timeouts = crate::serial::windows_model::comm_timeouts_model();
    let timeouts = COMMTIMEOUTS {
        ReadIntervalTimeout: timeouts.read_interval,
        ReadTotalTimeoutMultiplier: timeouts.read_total_multiplier,
        ReadTotalTimeoutConstant: timeouts.read_total_constant,
        WriteTotalTimeoutMultiplier: timeouts.write_total_multiplier,
        WriteTotalTimeoutConstant: timeouts.write_total_constant,
    };
    // SAFETY: `timeouts` 是完整的 COMMTIMEOUTS 結構，handle 由 `file` 持有。
    if unsafe { SetCommTimeouts(handle, &timeouts) } == 0 {
        return Err(last_error());
    }

    let local_wake = WinWakePair::new()?;
    let (sender, receiver) = bounded(READER_CHANNEL_CAPACITY);
    let (writer_tx, writer_rx) = bounded(WRITER_CHANNEL_CAPACITY);
    let id = NEXT_ID.fetch_add(1, Ordering::Relaxed);
    let state = Arc::new(Com0comState {
        reads: Mutex::new(ReadQueue {
            receiver,
            buffered: VecDeque::new(),
            error: None,
            closed: false,
        }),
        write_error: Mutex::new(None),
        write_start: Mutex::new(()),
        local_wake,
        mux_wake: Mutex::new(None),
        stopping: AtomicBool::new(false),
        counters,
    });
    let reader_handle = file.as_raw_handle() as usize;
    let reader_state = Arc::clone(&state);
    let reader = thread::Builder::new()
        .name(format!("sw-com0com-{id}"))
        .spawn(move || read_loop(reader_handle as HANDLE, sender, reader_state))
        .map_err(|error| PalError::plain(PalErrorKind::Io, error.to_string()))?;
    let writer_handle = file.as_raw_handle() as usize;
    let writer_state = Arc::clone(&state);
    let writer = match thread::Builder::new()
        .name(format!("sw-com0com-write-{id}"))
        .spawn(move || write_loop(writer_handle as HANDLE, writer_rx, writer_state))
    {
        Ok(writer) => writer,
        Err(error) => {
            state.stopping.store(true, Ordering::SeqCst);
            // SAFETY: handle 仍由 `file` 持有；取消 reader 目前的 overlapped request。
            unsafe { CancelIoEx(handle, std::ptr::null()) };
            let _ = reader.join();
            return Err(PalError::plain(PalErrorKind::Io, error.to_string()));
        }
    };
    registry()
        .lock()
        .unwrap_or_else(|error| error.into_inner())
        .insert(id, Arc::downgrade(&state));
    Ok(Box::new(Com0comConsoleIo {
        id,
        file,
        state,
        writer_tx: Some(writer_tx),
        writer: Some(writer),
        reader: Some(reader),
    }))
}

pub(crate) fn bind_mux_waker(id: u64, wake: &Arc<WinWakePair>) -> PalResult<()> {
    let state = state_for(id).ok_or_else(|| {
        PalError::plain(
            PalErrorKind::NotFound,
            format!("com0com reader {id} 不存在"),
        )
    })?;
    *state
        .mux_wake
        .lock()
        .unwrap_or_else(|error| error.into_inner()) = Some(Arc::downgrade(wake));
    Ok(())
}

pub(crate) fn unbind_mux_waker(id: u64) {
    if let Some(state) = state_for(id) {
        state
            .mux_wake
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .take();
    }
}

pub(crate) fn collect_readable(id: u64) -> bool {
    state_for(id).is_some_and(|state| state.collect())
}

pub(crate) fn writer_failed(id: u64) -> bool {
    state_for(id).is_some_and(|state| state.writer_failed())
}

pub(crate) fn api_call_counts(id: u64) -> (u32, u32) {
    state_for(id).map_or((0, 0), |state| {
        (
            state.counters.set_comm_state.load(Ordering::SeqCst),
            state.counters.escape_comm_function.load(Ordering::SeqCst),
        )
    })
}

fn read_loop(handle: HANDLE, sender: Sender<ReaderEvent>, state: Arc<Com0comState>) {
    let event = match create_event() {
        Ok(event) => event,
        Err(error) => {
            let _ = send_event(&sender, &state, ReaderEvent::Failed(error));
            return;
        }
    };
    let mut buffer = [0u8; READER_BUFFER_BYTES];
    while !state.stopping.load(Ordering::SeqCst) {
        match read_overlapped(handle, event.0, &mut buffer, &state.stopping) {
            Ok(0) if state.stopping.load(Ordering::SeqCst) => return,
            Ok(0) => continue,
            Ok(count) => {
                if !send_event(&sender, &state, ReaderEvent::Data(buffer[..count].to_vec())) {
                    return;
                }
            }
            Err(error) => {
                let _ = send_event(&sender, &state, ReaderEvent::Failed(error));
                return;
            }
        }
    }
}

fn write_loop(handle: HANDLE, receiver: Receiver<Vec<u8>>, state: Arc<Com0comState>) {
    while let Ok(bytes) = receiver.recv() {
        if state.stopping.load(Ordering::SeqCst) {
            return;
        }
        let mut offset = 0;
        while offset < bytes.len() {
            let started = {
                let _write_start = state
                    .write_start
                    .lock()
                    .unwrap_or_else(|error| error.into_inner());
                if state.stopping.load(Ordering::SeqCst) {
                    return;
                }
                start_write_overlapped(handle, &bytes[offset..])
            };
            let result = match started {
                Ok(WriteStart::Complete(count)) => Ok(count),
                Ok(WriteStart::Pending(pending)) => finish_write_overlapped(handle, pending),
                Err(error) => Err(error),
            };
            match result {
                Ok(count) if count > 0 => offset += count,
                Ok(_) => {
                    state.fail_writer(PalError::plain(
                        PalErrorKind::Io,
                        "com0com console overlapped write 未前進",
                    ));
                    return;
                }
                Err(_) if state.stopping.load(Ordering::SeqCst) => return,
                Err(error) => {
                    state.fail_writer(error);
                    return;
                }
            }
        }
        // Hub 收到這次 completion 後再從有界 queue 取 ring 中尚未接受的資料。
        state.notify();
    }
}

fn send_event(sender: &Sender<ReaderEvent>, state: &Com0comState, mut event: ReaderEvent) -> bool {
    loop {
        match sender.send_timeout(event, READER_SEND_RETRY) {
            Ok(()) => {
                state.notify();
                return true;
            }
            Err(SendTimeoutError::Timeout(returned)) => {
                if state.stopping.load(Ordering::SeqCst) {
                    return false;
                }
                event = returned;
            }
            Err(SendTimeoutError::Disconnected(_)) => return false,
        }
    }
}

fn console_port_path(port: &str) -> PalResult<String> {
    let device = win_device_name(port);
    let name = device.strip_prefix(r"\\.\").unwrap_or(&device);
    let Some(number) = name.get(3..) else {
        return Err(PalError::plain(
            PalErrorKind::InvalidInput,
            format!("com0com 埠須為 COMn：{port}"),
        ));
    };
    if !name
        .get(..3)
        .is_some_and(|prefix| prefix.eq_ignore_ascii_case("COM"))
        || number.is_empty()
        || !number.chars().all(|ch| ch.is_ascii_digit())
    {
        return Err(PalError::plain(
            PalErrorKind::InvalidInput,
            format!("com0com 埠須為 COMn：{port}"),
        ));
    }
    Ok(format!(r"\\.\{}", name.to_ascii_uppercase()))
}

fn registry() -> &'static Mutex<HashMap<u64, Weak<Com0comState>>> {
    IO_REGISTRY.get_or_init(|| Mutex::new(HashMap::new()))
}

fn state_for(id: u64) -> Option<Arc<Com0comState>> {
    registry()
        .lock()
        .unwrap_or_else(|error| error.into_inner())
        .get(&id)
        .and_then(Weak::upgrade)
}

fn last_error() -> PalError {
    // SAFETY: `GetLastError` 讀取目前執行緒最近一次 Win32 錯誤碼。
    crate::traits::windows_win32_error(unsafe { GetLastError() })
}

struct OwnedEvent(HANDLE);

impl Drop for OwnedEvent {
    fn drop(&mut self) {
        // SAFETY: event handle 由 `create_event` 建立，僅由此值關閉一次。
        unsafe { CloseHandle(self.0) };
    }
}

fn create_event() -> PalResult<OwnedEvent> {
    // SAFETY: 建立未命名、手動重設、初始未觸發的 I/O completion event。
    let event = unsafe { CreateEventW(std::ptr::null(), 1, 0, std::ptr::null()) };
    if event.is_null() {
        Err(last_error())
    } else {
        Ok(OwnedEvent(event))
    }
}

fn read_overlapped(
    handle: HANDLE,
    event: HANDLE,
    buffer: &mut [u8],
    stopping: &AtomicBool,
) -> PalResult<usize> {
    // SAFETY: event 是本 reader 專用的有效事件 handle。
    if unsafe { ResetEvent(event) } == 0 {
        return Err(last_error());
    }
    let mut overlapped = OVERLAPPED {
        hEvent: event,
        ..OVERLAPPED::default()
    };
    let mut transferred = 0u32;
    let length = buffer.len().min(u32::MAX as usize) as u32;
    // SAFETY: handle 在 reader join 前保持開啟；buffer 與 OVERLAPPED 在操作收斂前有效。
    if unsafe {
        ReadFile(
            handle,
            buffer.as_mut_ptr(),
            length,
            std::ptr::null_mut(),
            &mut overlapped,
        )
    } != 0
    {
        // SAFETY: 同步完成的 overlapped ReadFile 不需等待。
        if unsafe { GetOverlappedResult(handle, &overlapped, &mut transferred, 0) } == 0 {
            return Err(last_error());
        }
        return Ok(transferred as usize);
    }
    let code = unsafe { GetLastError() };
    if code != ERROR_IO_PENDING {
        return if stopping.load(Ordering::SeqCst) && code == ERROR_OPERATION_ABORTED {
            Ok(0)
        } else {
            Err(crate::traits::windows_win32_error(code))
        };
    }
    loop {
        // SAFETY: event 在等待期間有效；100 ms 輪詢讓 Drop 可取消閒置中的讀取。
        match unsafe { WaitForSingleObject(event, 100) } {
            WAIT_OBJECT_0 => {
                // SAFETY: event 已觸發，讀取已完成，不需再次等待。
                if unsafe { GetOverlappedResult(handle, &overlapped, &mut transferred, 0) } == 0 {
                    let error = last_error();
                    if stopping.load(Ordering::SeqCst)
                        && error.winerror == Some(ERROR_OPERATION_ABORTED as i32)
                    {
                        return Ok(0);
                    }
                    return Err(error);
                }
                return Ok(transferred as usize);
            }
            WAIT_TIMEOUT if stopping.load(Ordering::SeqCst) => {
                // SAFETY: 只取消此 reader 的在途讀取，並收斂後才重用 OVERLAPPED／event。
                unsafe { CancelIoEx(handle, &overlapped) };
                // SAFETY: CancelIoEx 後等待此操作的 completion event，避免遺留 I/O。
                let wait = unsafe { WaitForSingleObject(event, INFINITE) };
                if wait != WAIT_OBJECT_0 {
                    return Err(last_error());
                }
                // SAFETY: 已收到 completion；取消的操作回 ERROR_OPERATION_ABORTED。
                if unsafe { GetOverlappedResult(handle, &overlapped, &mut transferred, 0) } == 0 {
                    let error = last_error();
                    if error.winerror == Some(ERROR_OPERATION_ABORTED as i32) {
                        return Ok(0);
                    }
                    return Err(error);
                }
                return Ok(transferred as usize);
            }
            WAIT_TIMEOUT => continue,
            _ => return Err(last_error()),
        }
    }
}

enum WriteStart {
    Complete(usize),
    Pending(PendingWrite),
}

struct PendingWrite {
    event: OwnedEvent,
    overlapped: Box<OVERLAPPED>,
}

fn start_write_overlapped(handle: HANDLE, data: &[u8]) -> PalResult<WriteStart> {
    if data.is_empty() {
        return Ok(WriteStart::Complete(0));
    }
    let event = create_event()?;
    let mut overlapped = Box::new(OVERLAPPED {
        hEvent: event.0,
        ..OVERLAPPED::default()
    });
    let mut transferred = 0u32;
    let length = data.len().min(u32::MAX as usize) as u32;
    // SAFETY: handle 與 event 有效；data 與 OVERLAPPED 在 completion 收斂前保持有效。
    let status = unsafe {
        WriteFile(
            handle,
            data.as_ptr(),
            length,
            std::ptr::null_mut(),
            &mut *overlapped,
        )
    };
    if status != 0 {
        // SAFETY: 同步完成的 overlapped WriteFile 不需等待。
        if unsafe { GetOverlappedResult(handle, &*overlapped, &mut transferred, 0) } == 0 {
            return Err(last_error());
        }
        return Ok(WriteStart::Complete(transferred as usize));
    }
    let code = unsafe { GetLastError() };
    if code != ERROR_IO_PENDING {
        return Err(crate::traits::windows_win32_error(code));
    }
    Ok(WriteStart::Pending(PendingWrite { event, overlapped }))
}

fn finish_write_overlapped(handle: HANDLE, pending: PendingWrite) -> PalResult<usize> {
    let PendingWrite { event, overlapped } = pending;
    let mut transferred = 0u32;
    // SAFETY: COM write timeout 已設為 5 秒；多留 500 ms 收斂逾時與取消。
    let wait = unsafe { WaitForSingleObject(event.0, 5_500) };
    if wait == WAIT_TIMEOUT {
        // SAFETY: 只取消本次寫入，並等待其完成狀態收斂。
        unsafe { CancelIoEx(handle, &*overlapped) };
        // SAFETY: CancelIoEx 後 completion event 會喚醒此等待。
        if unsafe { WaitForSingleObject(event.0, INFINITE) } != WAIT_OBJECT_0 {
            return Err(last_error());
        }
    } else if wait != WAIT_OBJECT_0 {
        return Err(last_error());
    }
    // SAFETY: WriteFile 已完成或 completion event 已觸發。
    if unsafe { GetOverlappedResult(handle, &*overlapped, &mut transferred, 0) } == 0 {
        let error = last_error();
        if error.winerror == Some(ERROR_OPERATION_ABORTED as i32) {
            return Err(PalError::plain(PalErrorKind::TimedOut, "com0com 寫入逾時"));
        }
        return Err(error);
    }
    Ok(transferred as usize)
}
