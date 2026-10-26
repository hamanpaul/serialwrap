//! Windows 序列埠後端（arch §4.2，P5-T3）。

#[cfg(any(test, feature = "test-fakes"))]
use std::cell::Cell;
use std::panic::{AssertUnwindSafe, catch_unwind, resume_unwind};
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex, Weak};
use std::time::{Duration, Instant};

use windows_sys::Win32::Devices::Communication::{
    COMMTIMEOUTS, DCB, GetCommState, SetCommState, SetCommTimeouts, SetupComm,
};
use windows_sys::Win32::Foundation::{
    CloseHandle, GetLastError, HANDLE, INVALID_HANDLE_VALUE, WAIT_OBJECT_0, WAIT_TIMEOUT,
};
use windows_sys::Win32::Storage::FileSystem::{
    CreateFileW, FILE_FLAG_OVERLAPPED, OPEN_EXISTING, ReadFile, WriteFile,
};
use windows_sys::Win32::System::IO::{CancelIoEx, GetOverlappedResult, OVERLAPPED};
use windows_sys::Win32::System::Threading::{
    CreateEventW, INFINITE, ResetEvent, SetEvent, WaitForMultipleObjects, WaitForSingleObject,
};

use crate::serial::windows_model::{
    OpenErrorKind, PYSERIAL_ERROR_CHAR, PYSERIAL_XOFF_CHAR, PYSERIAL_XON_CHAR, classify_read_error,
    classify_wait_error, comm_timeouts_model, dcb_flags, dcb_model, map_open_error,
    map_overlapped_write_completion, win_device_name,
};
use crate::traits::{
    CancelToken, CloseImpl, CloseReceipt, CloseToken, DevicePath, HolderReport, HolderScanStatus,
    IoWaker, OpenError, PalError, PalErrorKind, PalResult, ReadError, ReadOutcome, SerialBackend,
    SerialConfig, SerialHandle, SerialReader, SerialWriter, SubmitGate, SubmitRevoked, SubmitStep,
    TxOutcome, TxResult,
};

const ERROR_IO_INCOMPLETE: u32 = 996;
// 與 Python 基線每次交付 64 bytes 對齊；每個 chunk 獨立經 submit gate。
const MAX_OVERLAPPED_WRITE_BYTES: usize = 64;

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
#[derive(Clone, Copy)]
struct CreateFileArgs {
    access: u32,
    share_mode: u32,
    disposition: u32,
    flags: u32,
}

fn create_file_args() -> CreateFileArgs {
    CreateFileArgs {
        access: windows_sys::Win32::Foundation::GENERIC_READ
            | windows_sys::Win32::Foundation::GENERIC_WRITE,
        share_mode: 0,
        disposition: OPEN_EXISTING,
        flags: FILE_FLAG_OVERLAPPED,
    }
}

/// Windows 原生序列埠後端。
pub struct WindowsSerialBackend {
    pending_ops: Arc<AtomicUsize>,
    #[cfg(feature = "test-fakes")]
    cancel_reaps: Arc<AtomicUsize>,
}

impl WindowsSerialBackend {
    pub fn new() -> Self {
        Self {
            pending_ops: Arc::new(AtomicUsize::new(0)),
            #[cfg(feature = "test-fakes")]
            cancel_reaps: Arc::new(AtomicUsize::new(0)),
        }
    }

    /// 測試用計數器：確認取消後沒有仍在 OS 佇列中的 OVERLAPPED 操作。
    #[cfg(feature = "test-fakes")]
    pub fn pending_ops(&self) -> usize {
        self.pending_ops.load(Ordering::SeqCst)
    }

    /// 測試用計數器：確認取消路徑已取回 OVERLAPPED 最終結果。
    #[cfg(feature = "test-fakes")]
    pub fn cancel_reaps(&self) -> usize {
        self.cancel_reaps.load(Ordering::SeqCst)
    }
}

impl Default for WindowsSerialBackend {
    fn default() -> Self {
        Self::new()
    }
}

impl SerialBackend for WindowsSerialBackend {
    fn open_exclusive(
        &self,
        dev: &DevicePath,
        cfg: &SerialConfig,
    ) -> Result<Box<dyn SerialHandle>, OpenError> {
        let name = win_device_name(&dev.0);
        let wide_name: Vec<u16> = name.encode_utf16().chain(std::iter::once(0)).collect();
        let args = create_file_args();
        // SAFETY: `wide_name` 為有效 NUL 結尾 UTF-16；成功 handle 由 `WinPort` 接管。
        let raw = unsafe {
            CreateFileW(
                wide_name.as_ptr(),
                args.access,
                args.share_mode,
                std::ptr::null(),
                args.disposition,
                args.flags,
                std::ptr::null_mut(),
            )
        };
        if raw == INVALID_HANDLE_VALUE {
            let code = last_error();
            let error = win32_error(code);
            return Err(match map_open_error(code, error.py_message.clone()) {
                OpenErrorKind::NotFound => OpenError::NotFound(error),
                OpenErrorKind::AccessDenied => OpenError::AccessDenied {
                    os: error,
                    holder: HolderReport {
                        pids: Vec::new(),
                        holder_unknown: true,
                        status: HolderScanStatus::Unsupported,
                    },
                },
                OpenErrorKind::Other => OpenError::Other(error),
            });
        }

        let port = Arc::new(WinPort::new(raw));
        configure_port(&port, cfg).map_err(OpenError::Other)?;

        let read_io = Arc::new(OwnedEvent::new(true).map_err(OpenError::Other)?);
        let write_io = Arc::new(OwnedEvent::new(true).map_err(OpenError::Other)?);
        let read_wake = Arc::new(WakeEvent::new().map_err(OpenError::Other)?);
        let write_wake = Arc::new(WakeEvent::new().map_err(OpenError::Other)?);

        Ok(Box::new(WindowsSerialHandle {
            port,
            read_io,
            write_io,
            read_wake,
            write_wake,
            pending_ops: Arc::clone(&self.pending_ops),
            #[cfg(feature = "test-fakes")]
            cancel_reaps: Arc::clone(&self.cancel_reaps),
        }))
    }
}

fn configure_port(port: &WinPort, cfg: &SerialConfig) -> PalResult<()> {
    let mut dcb = DCB::default();
    dcb.DCBlength = std::mem::size_of::<DCB>() as u32;
    // SAFETY: `dcb` 為可寫 DCB，`port` 持有有效的通訊 handle。
    if unsafe { GetCommState(port.raw(), &mut dcb) } == 0 {
        return Err(last_error_pal());
    }

    let model = dcb_model(cfg);
    dcb.BaudRate = model.baud;
    dcb.ByteSize = model.byte_size;
    dcb.Parity = model.parity;
    dcb.StopBits = model.stop_bits;
    dcb._bitfield = dcb_flags(dcb._bitfield, &model);
    dcb.ErrorChar = PYSERIAL_ERROR_CHAR;
    dcb.XonChar = PYSERIAL_XON_CHAR;
    dcb.XoffChar = PYSERIAL_XOFF_CHAR;
    // SAFETY: `dcb` 由 GetCommState 初始化，僅更新本後端管理的序列線路欄位。
    if unsafe { SetCommState(port.raw(), &dcb) } == 0 {
        return Err(last_error_pal());
    }

    let timeouts = comm_timeouts_model();
    let timeouts = COMMTIMEOUTS {
        ReadIntervalTimeout: timeouts.read_interval,
        ReadTotalTimeoutMultiplier: timeouts.read_total_multiplier,
        ReadTotalTimeoutConstant: timeouts.read_total_constant,
        WriteTotalTimeoutMultiplier: timeouts.write_total_multiplier,
        WriteTotalTimeoutConstant: timeouts.write_total_constant,
    };
    // SAFETY: `timeouts` 為有效的 COMMTIMEOUTS 結構。
    if unsafe { SetCommTimeouts(port.raw(), &timeouts) } == 0 {
        return Err(last_error_pal());
    }
    // SAFETY: port 為有效的序列埠 handle；依計畫建立 4096-byte input/output queue。
    if unsafe { SetupComm(port.raw(), 4096, 4096) } == 0 {
        return Err(last_error_pal());
    }
    Ok(())
}

struct WinPort {
    handle: HANDLE,
    closed: AtomicBool,
}

// HANDLE 可供同一裝置的獨立 overlapped reader／writer 同時呼叫；實際關閉由 CloseToken 排序。
unsafe impl Send for WinPort {}
unsafe impl Sync for WinPort {}

impl WinPort {
    fn new(handle: HANDLE) -> Self {
        Self {
            handle,
            closed: AtomicBool::new(false),
        }
    }

    fn raw(&self) -> HANDLE {
        self.handle
    }

    fn close_once(&self) -> PalResult<()> {
        if self.closed.swap(true, Ordering::SeqCst) {
            return Ok(());
        }
        // SAFETY: reader／writer 已收斂後，只由 CloseToken 關閉此 handle 一次。
        if unsafe { CloseHandle(self.handle) } != 0 {
            Ok(())
        } else {
            Err(last_error_pal())
        }
    }

    fn is_open(&self) -> bool {
        !self.closed.load(Ordering::SeqCst)
    }
}

impl Drop for WinPort {
    fn drop(&mut self) {
        if !self.closed.swap(true, Ordering::SeqCst) {
            // SAFETY: Arc 最後一個 owner drop 時，沒有其他 PAL 物件能使用 handle。
            unsafe {
                CloseHandle(self.handle);
            }
        }
    }
}

struct OwnedEvent {
    handle: HANDLE,
}

unsafe impl Send for OwnedEvent {}
unsafe impl Sync for OwnedEvent {}

impl OwnedEvent {
    fn new(manual_reset: bool) -> PalResult<Self> {
        // SAFETY: 建立未命名的事件；回傳 handle 由此結構唯一持有。
        let handle = unsafe {
            CreateEventW(
                std::ptr::null(),
                i32::from(manual_reset),
                0,
                std::ptr::null(),
            )
        };
        if handle.is_null() {
            Err(last_error_pal())
        } else {
            Ok(Self { handle })
        }
    }
}

impl Drop for OwnedEvent {
    fn drop(&mut self) {
        if !self.handle.is_null() {
            // SAFETY: 此 event handle 由本結構唯一持有。
            unsafe {
                CloseHandle(self.handle);
            }
        }
    }
}

struct WakeEvent {
    event: OwnedEvent,
    signal_lock: Mutex<()>,
}

impl WakeEvent {
    fn new() -> PalResult<Self> {
        Ok(Self {
            event: OwnedEvent::new(true)?,
            signal_lock: Mutex::new(()),
        })
    }

    fn raw(&self) -> HANDLE {
        self.event.handle
    }

    fn signal(&self) {
        let _guard = self
            .signal_lock
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        // SAFETY: `event` 是仍存活的 manual-reset event handle。
        unsafe {
            SetEvent(self.raw());
        }
    }

    fn consume_if_signaled(&self) -> PalResult<bool> {
        let _guard = self
            .signal_lock
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        // SAFETY: 零逾時只檢查此仍存活的 event，不會阻塞。
        match unsafe { WaitForSingleObject(self.raw(), 0) } {
            WAIT_OBJECT_0 => {
                // SAFETY: event 已 signaled，重設供下一次 I/O 使用。
                if unsafe { ResetEvent(self.raw()) } == 0 {
                    Err(last_error_pal())
                } else {
                    Ok(true)
                }
            }
            WAIT_TIMEOUT => Ok(false),
            _ => Err(last_error_pal()),
        }
    }
}

struct WindowsIoWaker {
    read: Weak<WakeEvent>,
    write: Weak<WakeEvent>,
}

impl IoWaker for WindowsIoWaker {
    fn wake(&self) {
        if let Some(read) = self.read.upgrade() {
            read.signal();
        }
        if let Some(write) = self.write.upgrade() {
            write.signal();
        }
    }
}

struct WindowsSerialHandle {
    port: Arc<WinPort>,
    read_io: Arc<OwnedEvent>,
    write_io: Arc<OwnedEvent>,
    read_wake: Arc<WakeEvent>,
    write_wake: Arc<WakeEvent>,
    pending_ops: Arc<AtomicUsize>,
    #[cfg(feature = "test-fakes")]
    cancel_reaps: Arc<AtomicUsize>,
}

impl SerialHandle for WindowsSerialHandle {
    fn split(
        self: Box<Self>,
    ) -> (
        Box<dyn SerialReader>,
        Box<dyn SerialWriter>,
        Arc<dyn IoWaker>,
        CloseToken,
    ) {
        let Self {
            port,
            read_io,
            write_io,
            read_wake,
            write_wake,
            pending_ops,
            #[cfg(feature = "test-fakes")]
            cancel_reaps,
        } = *self;
        let waker: Arc<dyn IoWaker> = Arc::new(WindowsIoWaker {
            read: Arc::downgrade(&read_wake),
            write: Arc::downgrade(&write_wake),
        });
        let reader = WindowsSerialReader {
            port: Arc::clone(&port),
            io_event: read_io,
            wake_event: read_wake,
            overlapped: overlapped_for(None),
            buffer: Vec::new(),
            pending: false,
            pending_ops: Arc::clone(&pending_ops),
        };
        let writer = WindowsSerialWriter {
            port: Arc::clone(&port),
            io_event: write_io,
            wake_event: write_wake,
            overlapped: overlapped_for(None),
            buffer: Vec::new(),
            pending: false,
            pending_ops,
            #[cfg(feature = "test-fakes")]
            cancel_reaps,
        };
        let close = CloseToken::new(Box::new(WindowsClose { port }));
        (Box::new(reader), Box::new(writer), waker, close)
    }

    fn set_baud(&self, baud: u32) -> PalResult<()> {
        if !self.port.is_open() {
            return Err(PalError::plain(PalErrorKind::Io, "serial not open"));
        }
        let mut dcb = DCB::default();
        dcb.DCBlength = std::mem::size_of::<DCB>() as u32;
        // SAFETY: `dcb` 為可寫 DCB，`port` 持有有效 handle。
        if unsafe { GetCommState(self.port.raw(), &mut dcb) } == 0 {
            return Err(last_error_pal());
        }
        dcb.BaudRate = baud;
        // SAFETY: `dcb` 是剛由 GetCommState 取得，僅變更 BaudRate。
        if unsafe { SetCommState(self.port.raw(), &dcb) } == 0 {
            return Err(last_error_pal());
        }
        Ok(())
    }

    fn is_alive(&self) -> bool {
        self.port.is_open()
    }
}

struct WindowsClose {
    port: Arc<WinPort>,
}

impl CloseImpl for WindowsClose {
    fn close(self: Box<Self>) -> CloseReceipt {
        match self.port.close_once() {
            Ok(()) => CloseReceipt {
                closed: true,
                excl_cleared: true,
                error: None,
            },
            Err(error) => CloseReceipt {
                closed: false,
                excl_cleared: false,
                error: Some(error),
            },
        }
    }
}

struct WindowsSerialReader {
    port: Arc<WinPort>,
    io_event: Arc<OwnedEvent>,
    wake_event: Arc<WakeEvent>,
    overlapped: OVERLAPPED,
    buffer: Vec<u8>,
    pending: bool,
    pending_ops: Arc<AtomicUsize>,
}

// SAFETY: `&mut self` 序列化讀取；物件移動前不會有在途 I/O，方法會等待或取消並收斂後才返回。
unsafe impl Send for WindowsSerialReader {}

impl WindowsSerialReader {
    fn reset_operation(&mut self) -> PalResult<()> {
        // SAFETY: 前一個操作已由 GetOverlappedResult 收斂，event 可安全重設。
        if unsafe { ResetEvent(self.io_event.handle) } == 0 {
            return Err(last_error_pal());
        }
        self.overlapped = overlapped_for(Some(self.io_event.handle));
        Ok(())
    }

    fn mark_pending(&mut self) {
        if !self.pending {
            self.pending = true;
            self.pending_ops.fetch_add(1, Ordering::SeqCst);
        }
    }

    fn mark_complete(&mut self) {
        if self.pending {
            self.pending = false;
            self.pending_ops.fetch_sub(1, Ordering::SeqCst);
        }
    }

    fn collect(&mut self, wait: bool) -> Completion {
        let mut transferred = 0u32;
        // SAFETY: reader 持有 OVERLAPPED 與緩衝區，直到收集完成結果。
        let ok = unsafe {
            GetOverlappedResult(
                self.port.raw(),
                &self.overlapped,
                &mut transferred,
                i32::from(wait),
            )
        };
        let code = if ok == 0 { last_error() } else { 0 };
        if ok == 0 && code == ERROR_IO_INCOMPLETE {
            self.mark_pending();
        } else {
            self.mark_complete();
        }
        Completion {
            ok: ok != 0,
            transferred,
            win32: code,
            cancel_succeeded: false,
        }
    }

    fn cancel_and_collect(&mut self) -> Completion {
        // SAFETY: 只有此 reader 提交這個 OVERLAPPED，且會在重用或 drop 前收集完成結果。
        let cancel_succeeded = unsafe { CancelIoEx(self.port.raw(), &self.overlapped) != 0 };
        loop {
            let mut completion = self.collect(true);
            if completion.win32 != ERROR_IO_INCOMPLETE {
                completion.cancel_succeeded = cancel_succeeded;
                return completion;
            }
        }
    }

    fn wait(&self, timeout: u32) -> PalResult<WaitResult> {
        let handles = [self.io_event.handle, self.wake_event.raw()];
        // SAFETY: 等待期間，這兩個 handle 都由此 reader 持有。
        match unsafe { WaitForMultipleObjects(handles.len() as u32, handles.as_ptr(), 0, timeout) }
        {
            WAIT_OBJECT_0 => Ok(WaitResult::Io),
            value if value == WAIT_OBJECT_0 + 1 => Ok(WaitResult::Wake),
            WAIT_TIMEOUT => Ok(WaitResult::Timeout),
            _ => Err(last_error_pal()),
        }
    }

    fn read_result(
        &mut self,
        completion: Completion,
        wake: bool,
        timeout: bool,
    ) -> PalResult<ReadOutcome> {
        if completion.transferred > 0 {
            let count = completion.transferred as usize;
            if count > self.buffer.len() {
                return Err(PalError::plain(
                    PalErrorKind::Io,
                    "ReadFile 回報超過讀取緩衝區的 bytes",
                ));
            }
            self.buffer.truncate(count);
            return Ok(ReadOutcome::Data(count));
        }
        if completion.ok {
            return Ok(if timeout {
                ReadOutcome::TimedOut
            } else if wake {
                ReadOutcome::Woken
            } else {
                ReadOutcome::TimedOut
            });
        }
        if completion.win32 == windows_sys::Win32::Foundation::ERROR_OPERATION_ABORTED {
            return if completion.cancel_succeeded && timeout {
                Ok(ReadOutcome::TimedOut)
            } else if completion.cancel_succeeded && wake {
                Ok(ReadOutcome::Woken)
            } else {
                Err(win32_error_with_kind(completion.win32, PalErrorKind::Io))
            };
        }
        Err(win32_error(completion.win32))
    }
}

impl SerialReader for WindowsSerialReader {
    fn read(
        &mut self,
        buf: &mut [u8],
        timeout: Option<Duration>,
    ) -> Result<ReadOutcome, ReadError> {
        if buf.is_empty() {
            return Ok(ReadOutcome::Data(0));
        }
        let start = Instant::now();
        loop {
            if self
                .wake_event
                .consume_if_signaled()
                .map_err(classify_wait_error)?
            {
                return Ok(ReadOutcome::Woken);
            }
            self.reset_operation().map_err(classify_read_error)?;
            let read_len = buf.len().min(u32::MAX as usize);
            self.buffer.resize(read_len, 0);
            let buffer_ptr = self.buffer.as_mut_ptr();
            let overlapped_ptr = &mut self.overlapped as *mut OVERLAPPED;
            // SAFETY: I/O 收斂前，buffer 與 OVERLAPPED 都由此 reader 持有。
            let started = unsafe {
                ReadFile(
                    self.port.raw(),
                    buffer_ptr,
                    read_len as u32,
                    std::ptr::null_mut(),
                    overlapped_ptr,
                )
            };
            if started == 0 {
                let code = last_error();
                if code != windows_sys::Win32::Foundation::ERROR_IO_PENDING {
                    return Err(classify_read_error(win32_error(code)));
                }
                self.mark_pending();
            }

            loop {
                let wait_ms = remaining_timeout_ms(start, timeout);
                match self.wait(wait_ms) {
                    Ok(WaitResult::Io) => {
                        let completion = self.collect(false);
                        if completion.win32 == ERROR_IO_INCOMPLETE {
                            continue;
                        }
                        let outcome = self
                            .read_result(completion, false, false)
                            .map_err(classify_read_error)?;
                        if let ReadOutcome::Data(count) = &outcome {
                            buf[..*count].copy_from_slice(&self.buffer[..*count]);
                        }
                        if outcome == ReadOutcome::TimedOut
                            && retry_idle_read_timeout(timeout, start.elapsed())
                        {
                            break;
                        }
                        return Ok(outcome);
                    }
                    Ok(WaitResult::Wake) => {
                        if let Err(error) = self.wake_event.consume_if_signaled() {
                            self.cancel_and_collect();
                            return Err(classify_wait_error(error));
                        }
                        let completion = self.cancel_and_collect();
                        let outcome = self
                            .read_result(completion, true, false)
                            .map_err(classify_read_error)?;
                        if let ReadOutcome::Data(count) = &outcome {
                            buf[..*count].copy_from_slice(&self.buffer[..*count]);
                        }
                        return Ok(outcome);
                    }
                    Ok(WaitResult::Timeout) => {
                        let completion = self.cancel_and_collect();
                        let outcome = self
                            .read_result(completion, false, true)
                            .map_err(classify_read_error)?;
                        if let ReadOutcome::Data(count) = &outcome {
                            buf[..*count].copy_from_slice(&self.buffer[..*count]);
                        }
                        if outcome == ReadOutcome::TimedOut
                            && retry_idle_read_timeout(timeout, start.elapsed())
                        {
                            break;
                        }
                        return Ok(outcome);
                    }
                    Err(wait_error) => {
                        self.cancel_and_collect();
                        return Err(classify_wait_error(wait_error));
                    }
                }
            }
        }
    }
}

impl Drop for WindowsSerialReader {
    fn drop(&mut self) {
        if self.pending {
            self.cancel_and_collect();
        }
    }
}

struct WindowsSerialWriter {
    port: Arc<WinPort>,
    io_event: Arc<OwnedEvent>,
    wake_event: Arc<WakeEvent>,
    overlapped: OVERLAPPED,
    buffer: Vec<u8>,
    pending: bool,
    pending_ops: Arc<AtomicUsize>,
    #[cfg(feature = "test-fakes")]
    cancel_reaps: Arc<AtomicUsize>,
}

// SAFETY: `&mut self` 序列化寫入；物件移動前不會有在途 I/O，方法會等待或取消並收斂後才返回。
unsafe impl Send for WindowsSerialWriter {}

impl WindowsSerialWriter {
    fn reset_operation(&mut self) -> PalResult<()> {
        // SAFETY: 前一個操作已收斂，才可重設 event 與 OVERLAPPED。
        if unsafe { ResetEvent(self.io_event.handle) } == 0 {
            return Err(last_error_pal());
        }
        self.overlapped = overlapped_for(Some(self.io_event.handle));
        Ok(())
    }

    fn mark_pending(&mut self) {
        if !self.pending {
            self.pending = true;
            self.pending_ops.fetch_add(1, Ordering::SeqCst);
        }
    }

    fn mark_complete(&mut self) {
        if self.pending {
            self.pending = false;
            self.pending_ops.fetch_sub(1, Ordering::SeqCst);
        }
    }

    fn collect(&mut self, wait: bool) -> Completion {
        let mut transferred = 0u32;
        // SAFETY: writer 持有 OVERLAPPED 與緩衝區，直到收集完成結果。
        let ok = unsafe {
            GetOverlappedResult(
                self.port.raw(),
                &self.overlapped,
                &mut transferred,
                i32::from(wait),
            )
        };
        let code = if ok == 0 { last_error() } else { 0 };
        if ok == 0 && code == ERROR_IO_INCOMPLETE {
            self.mark_pending();
        } else {
            self.mark_complete();
        }
        Completion {
            ok: ok != 0,
            transferred,
            win32: code,
            cancel_succeeded: false,
        }
    }

    fn cancel_and_collect(&mut self) -> Completion {
        // SAFETY: 只有此 writer 提交這個 OVERLAPPED，且會在重用或 drop 前收集最終結果。
        let cancel_succeeded = unsafe { CancelIoEx(self.port.raw(), &self.overlapped) != 0 };
        loop {
            let mut completion = self.collect(true);
            if completion.win32 != ERROR_IO_INCOMPLETE {
                completion.cancel_succeeded = cancel_succeeded;
                #[cfg(feature = "test-fakes")]
                self.cancel_reaps.fetch_add(1, Ordering::SeqCst);
                return completion;
            }
        }
    }

    fn wait(&self) -> PalResult<WaitResult> {
        let handles = [self.io_event.handle, self.wake_event.raw()];
        // SAFETY: 等待期間，兩個事件 handle 都由此 writer 持有。
        match unsafe { WaitForMultipleObjects(handles.len() as u32, handles.as_ptr(), 0, INFINITE) }
        {
            WAIT_OBJECT_0 => Ok(WaitResult::Io),
            value if value == WAIT_OBJECT_0 + 1 => Ok(WaitResult::Wake),
            _ => Err(last_error_pal()),
        }
    }

    fn complete(
        &mut self,
        completion: Completion,
        sent: &mut usize,
        requested: usize,
        cancel_requested: bool,
    ) -> Option<TxResult> {
        let (outcome, error_kind) = map_overlapped_write_completion(
            completion.ok,
            completion.transferred,
            requested,
            completion.win32,
            cancellation_owns_aborted_completion(completion, cancel_requested),
        );
        match outcome {
            TxOutcome::Completed(count) => {
                if count > requested {
                    return Some(TxResult {
                        outcome: TxOutcome::Unknown { confirmed: *sent },
                        error: Some(PalError::plain(
                            PalErrorKind::Io,
                            "WriteFile 回報超過提交長度的 bytes",
                        )),
                    });
                }
                *sent += count;
                if let Some(kind) = error_kind {
                    return Some(TxResult {
                        outcome: TxOutcome::Completed(*sent),
                        error: Some(completion_error(completion.win32, kind)),
                    });
                }
                if count == 0 {
                    return Some(TxResult {
                        outcome: TxOutcome::Completed(*sent),
                        error: Some(PalError::plain(
                            PalErrorKind::Io,
                            "WriteFile completed without bytes",
                        )),
                    });
                }
                if *sent == self.buffer.len() {
                    Some(TxResult {
                        outcome: TxOutcome::Completed(*sent),
                        error: None,
                    })
                } else {
                    None
                }
            }
            TxOutcome::Unknown { confirmed } => Some(TxResult {
                outcome: TxOutcome::Unknown {
                    confirmed: sent.saturating_add(confirmed),
                },
                error: Some(completion_error(
                    completion.win32,
                    error_kind.unwrap_or(PalErrorKind::Io),
                )),
            }),
            TxOutcome::NotSubmitted => Some(TxResult {
                outcome: TxOutcome::Unknown { confirmed: *sent },
                error: Some(PalError::plain(
                    PalErrorKind::Io,
                    "overlapped write completion was not classified",
                )),
            }),
        }
    }

    fn complete_after_wait_error(
        &mut self,
        completion: Completion,
        sent: &mut usize,
        requested: usize,
        wait_error: PalError,
    ) -> TxResult {
        match self.complete(completion, sent, requested, false) {
            Some(result)
                if result.error.is_some()
                    || matches!(result.outcome, TxOutcome::Unknown { .. }) =>
            {
                result
            }
            Some(result) => result,
            None => TxResult {
                outcome: TxOutcome::Completed(*sent),
                error: Some(wait_error),
            },
        }
    }
}

impl SerialWriter for WindowsSerialWriter {
    fn write_gated(
        &mut self,
        data: &[u8],
        cancel: &CancelToken,
        gate: &dyn SubmitGate,
    ) -> TxResult {
        #[cfg(any(test, feature = "test-fakes"))]
        debug_begin_tx_write_count();
        self.buffer.clear();
        self.buffer.extend_from_slice(data);
        let mut sent = 0usize;
        loop {
            if cancel.is_cancelled() {
                return TxResult {
                    outcome: TxOutcome::Completed(sent),
                    error: Some(PalError::cancelled()),
                };
            }
            if sent == self.buffer.len() {
                return TxResult {
                    outcome: TxOutcome::Completed(sent),
                    error: None,
                };
            }
            if let Err(error) = self.wake_event.consume_if_signaled() {
                return TxResult {
                    outcome: TxOutcome::Completed(sent),
                    error: Some(error),
                };
            }
            if let Err(error) = self.reset_operation() {
                return TxResult {
                    outcome: TxOutcome::Completed(sent),
                    error: Some(error),
                };
            }

            let handle = self.port.raw();
            let overlapped = &mut self.overlapped as *mut OVERLAPPED;
            let remaining = self.buffer.len() - sent;
            let requested = remaining.min(MAX_OVERLAPPED_WRITE_BYTES);
            let write_len = requested as u32;
            // SAFETY: I/O 收斂前，writer 持有的 buffer 與 OVERLAPPED 都保持有效。
            let buffer = unsafe { self.buffer.as_ptr().add(sent) };
            let mut submitted_pending = false;
            let mut submitted_step = None;
            let submitted = catch_unwind(AssertUnwindSafe(|| {
                gate.submit(&mut || {
                    #[cfg(any(test, feature = "test-fakes"))]
                    debug_record_tx_write_syscall();
                    // SAFETY: handle 保持開啟，指標指向 writer 持有且穩定的配置，每次提交都使用新的 OVERLAPPED。
                    let ok = unsafe {
                        WriteFile(handle, buffer, write_len, std::ptr::null_mut(), overlapped)
                    };
                    let step = if ok != 0 {
                        let mut transferred = 0u32;
                        // SAFETY: overlapped WriteFile 已同步完成；此查詢不會等待。
                        let completed =
                            unsafe { GetOverlappedResult(handle, overlapped, &mut transferred, 0) };
                        if completed != 0 {
                            SubmitStep::Accepted(transferred as usize)
                        } else {
                            // WriteFile 已成功接受操作；無法查明完成長度時須走 Unknown 收斂路徑。
                            let _completion_error = last_error();
                            submitted_pending = true;
                            SubmitStep::Pending
                        }
                    } else {
                        let code = last_error();
                        if code == windows_sys::Win32::Foundation::ERROR_IO_PENDING {
                            submitted_pending = true;
                            SubmitStep::Pending
                        } else {
                            SubmitStep::Failed(win32_error(code))
                        }
                    };
                    submitted_step = Some(step.clone());
                    step
                })
            }));
            let step = match submitted {
                Ok(step) => step,
                Err(panic) => {
                    if submitted_pending {
                        self.mark_pending();
                        self.cancel_and_collect();
                    }
                    resume_unwind(panic);
                }
            };
            if submitted_pending {
                self.mark_pending();
            }

            match step {
                Err(SubmitRevoked) => match submitted_step {
                    Some(SubmitStep::Pending) => {
                        let completion = self.cancel_and_collect();
                        if let Some(result) = self.complete(completion, &mut sent, requested, true)
                        {
                            return result;
                        }
                        return TxResult {
                            outcome: TxOutcome::Completed(sent),
                            error: Some(PalError::cancelled()),
                        };
                    }
                    Some(SubmitStep::Accepted(count)) => {
                        if count > requested {
                            return TxResult {
                                outcome: TxOutcome::Unknown { confirmed: sent },
                                error: Some(PalError::plain(
                                    PalErrorKind::Io,
                                    "WriteFile 同步完成長度無效",
                                )),
                            };
                        }
                        sent += count;
                        return TxResult {
                            outcome: TxOutcome::Completed(sent),
                            error: Some(PalError::cancelled()),
                        };
                    }
                    Some(SubmitStep::Failed(error)) => {
                        return TxResult {
                            outcome: TxOutcome::Completed(sent),
                            error: Some(error),
                        };
                    }
                    Some(SubmitStep::WouldBlock) | None if sent == 0 => {
                        return TxResult {
                            outcome: TxOutcome::NotSubmitted,
                            error: None,
                        };
                    }
                    Some(SubmitStep::WouldBlock) | None => {
                        return TxResult {
                            outcome: TxOutcome::Completed(sent),
                            error: Some(PalError::cancelled()),
                        };
                    }
                },
                Ok(SubmitStep::Accepted(count)) => {
                    if count > requested {
                        return TxResult {
                            outcome: TxOutcome::Unknown { confirmed: sent },
                            error: Some(PalError::plain(
                                PalErrorKind::Io,
                                "WriteFile 同步完成長度無效",
                            )),
                        };
                    }
                    sent += count;
                    if count < requested {
                        return TxResult {
                            outcome: TxOutcome::Completed(sent),
                            error: Some(serial_write_timed_out()),
                        };
                    }
                }
                Ok(SubmitStep::Pending) => loop {
                    if cancel.is_cancelled() {
                        let completion = self.cancel_and_collect();
                        if let Some(result) = self.complete(completion, &mut sent, requested, true)
                        {
                            return result;
                        }
                        return TxResult {
                            outcome: TxOutcome::Completed(sent),
                            error: Some(PalError::cancelled()),
                        };
                    }
                    match self.wait() {
                        Ok(WaitResult::Io) => {
                            let completion = self.collect(false);
                            if completion.win32 == ERROR_IO_INCOMPLETE {
                                continue;
                            }
                            if let Some(result) =
                                self.complete(completion, &mut sent, requested, false)
                            {
                                return result;
                            }
                            break;
                        }
                        Ok(WaitResult::Wake) => {
                            if let Err(error) = self.wake_event.consume_if_signaled() {
                                let completion = self.cancel_and_collect();
                                return self.complete_after_wait_error(
                                    completion, &mut sent, requested, error,
                                );
                            }
                            if cancel.is_cancelled() {
                                let completion = self.cancel_and_collect();
                                if let Some(result) =
                                    self.complete(completion, &mut sent, requested, true)
                                {
                                    return result;
                                }
                                return TxResult {
                                    outcome: TxOutcome::Completed(sent),
                                    error: Some(PalError::cancelled()),
                                };
                            }
                        }
                        Ok(WaitResult::Timeout) => continue,
                        Err(error) => {
                            let completion = self.cancel_and_collect();
                            return self.complete_after_wait_error(
                                completion, &mut sent, requested, error,
                            );
                        }
                    }
                },
                Ok(SubmitStep::WouldBlock) => {
                    return TxResult {
                        outcome: TxOutcome::Completed(sent),
                        error: Some(PalError::plain(
                            PalErrorKind::WouldBlock,
                            "overlapped WriteFile returned WouldBlock",
                        )),
                    };
                }
                Ok(SubmitStep::Failed(error)) => {
                    return TxResult {
                        outcome: TxOutcome::Completed(sent),
                        error: Some(error),
                    };
                }
            }
        }
    }
}

impl Drop for WindowsSerialWriter {
    fn drop(&mut self) {
        if self.pending {
            self.cancel_and_collect();
        }
    }
}

#[derive(Clone, Copy)]
struct Completion {
    ok: bool,
    transferred: u32,
    win32: u32,
    cancel_succeeded: bool,
}

fn cancellation_owns_aborted_completion(completion: Completion, cancel_requested: bool) -> bool {
    // 依計畫優先採本地取消意圖分類；並發 USB 拔除的 995 來源仍待實體裝置驗證。
    cancel_requested && completion.cancel_succeeded
}

enum WaitResult {
    Io,
    Wake,
    Timeout,
}

fn overlapped_for(event: Option<HANDLE>) -> OVERLAPPED {
    let mut overlapped = OVERLAPPED::default();
    overlapped.hEvent = event.unwrap_or(std::ptr::null_mut());
    overlapped
}

fn remaining_timeout_ms(start: Instant, timeout: Option<Duration>) -> u32 {
    let Some(timeout) = timeout else {
        return INFINITE;
    };
    let remaining = timeout.saturating_sub(start.elapsed());
    let nanos = remaining.as_nanos();
    if nanos == 0 {
        return 0;
    }
    ((nanos + 999_999) / 1_000_000).min((INFINITE - 1) as u128) as u32
}

fn retry_idle_read_timeout(timeout: Option<Duration>, elapsed: Duration) -> bool {
    timeout.map_or(true, |deadline| elapsed < deadline)
}

fn completion_error(code: u32, kind: PalErrorKind) -> PalError {
    if kind == PalErrorKind::Cancelled && (code == 995 || code == 0) {
        return PalError::cancelled();
    }
    if kind == PalErrorKind::TimedOut && code == 0 {
        return serial_write_timed_out();
    }
    let base = win32_error(code);
    PalError::new(
        kind,
        base.os_code,
        base.winerror,
        base.strerror,
        base.py_message,
    )
}

fn serial_write_timed_out() -> PalError {
    PalError::new(
        PalErrorKind::TimedOut,
        Some(138),
        None,
        "Write timeout",
        "[Errno 138] Write timeout",
    )
}

fn last_error() -> u32 {
    // SAFETY: GetLastError 讀取目前執行緒的 Win32 錯誤碼。
    unsafe { GetLastError() }
}

fn win32_error(code: u32) -> PalError {
    crate::traits::windows_win32_error(code)
}

fn win32_error_with_kind(code: u32, kind: PalErrorKind) -> PalError {
    let error = win32_error(code);
    PalError::new(
        kind,
        error.os_code,
        error.winerror,
        error.strerror,
        error.py_message,
    )
}

fn last_error_pal() -> PalError {
    win32_error(last_error())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn share_mode_zero_requested() {
        let args = create_file_args();
        assert_eq!(args.share_mode, 0);
        assert_eq!(args.disposition, OPEN_EXISTING);
        assert_ne!(args.flags & FILE_FLAG_OVERLAPPED, 0);
        assert_eq!(
            args.access,
            windows_sys::Win32::Foundation::GENERIC_READ
                | windows_sys::Win32::Foundation::GENERIC_WRITE
        );
    }

    #[test]
    fn idle_read_timeout_retries_until_caller_deadline() {
        assert!(retry_idle_read_timeout(
            Some(Duration::from_millis(500)),
            Duration::from_millis(200)
        ));
        assert!(!retry_idle_read_timeout(
            Some(Duration::from_millis(500)),
            Duration::from_millis(500)
        ));
        assert!(retry_idle_read_timeout(None, Duration::from_secs(30)));
    }

    #[test]
    fn aborted_read_without_local_cancel_is_io_error() {
        let port = Arc::new(WinPort::new(std::ptr::null_mut()));
        port.closed.store(true, Ordering::SeqCst);
        let mut reader = WindowsSerialReader {
            port,
            io_event: Arc::new(OwnedEvent::new(true).expect("io event")),
            wake_event: Arc::new(WakeEvent::new().expect("wake event")),
            overlapped: overlapped_for(None),
            buffer: Vec::new(),
            pending: false,
            pending_ops: Arc::new(AtomicUsize::new(0)),
        };
        let completion = Completion {
            ok: false,
            transferred: 0,
            win32: windows_sys::Win32::Foundation::ERROR_OPERATION_ABORTED,
            cancel_succeeded: false,
        };

        let error = reader
            .read_result(completion, false, false)
            .expect_err("非本地取消的 995 必須回報 I/O 錯誤");
        assert_eq!(error.kind, PalErrorKind::Io);
        assert_eq!(error.winerror, Some(995));
        assert!(reader.read_result(completion, true, false).is_err());
        assert_eq!(
            reader
                .read_result(completion, false, true)
                .expect_err("CancelIoEx 未找到 request 時，995 不可當本地 timeout")
                .kind,
            PalErrorKind::Io
        );
        let local_cancel = Completion {
            cancel_succeeded: true,
            ..completion
        };
        assert_eq!(
            reader.read_result(local_cancel, true, false).expect("wake"),
            ReadOutcome::Woken
        );
        assert_eq!(
            reader
                .read_result(local_cancel, false, true)
                .expect("timeout"),
            ReadOutcome::TimedOut
        );
    }

    #[test]
    fn aborted_write_requires_successful_local_cancel_request() {
        let device_abort = Completion {
            ok: false,
            transferred: 4,
            win32: windows_sys::Win32::Foundation::ERROR_OPERATION_ABORTED,
            cancel_succeeded: false,
        };
        let local_cancel = Completion {
            cancel_succeeded: true,
            ..device_abort
        };
        let mapped_without_cancel = map_overlapped_write_completion(
            device_abort.ok,
            device_abort.transferred,
            10,
            device_abort.win32,
            cancellation_owns_aborted_completion(device_abort, true),
        );
        assert_eq!(
            mapped_without_cancel,
            (TxOutcome::Unknown { confirmed: 0 }, Some(PalErrorKind::Io))
        );
        let mapped_after_local_cancel = map_overlapped_write_completion(
            local_cancel.ok,
            local_cancel.transferred,
            10,
            local_cancel.win32,
            cancellation_owns_aborted_completion(local_cancel, true),
        );
        assert_eq!(
            mapped_after_local_cancel,
            (TxOutcome::Completed(4), Some(PalErrorKind::Cancelled))
        );
    }

    #[test]
    fn successful_short_write_timeout_has_no_win32_success_error() {
        let error = completion_error(0, PalErrorKind::TimedOut);
        assert_eq!(error.kind, PalErrorKind::TimedOut);
        assert_eq!(error.os_code, Some(138));
        assert_eq!(error.winerror, None);
        assert_eq!(error.strerror, "Write timeout");
        assert_eq!(error.py_message, "[Errno 138] Write timeout");
    }
}
