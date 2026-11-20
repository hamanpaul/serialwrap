//! Windows `LockFileEx` 實例鎖與 TCP endpoint 存活探測。

use std::{
    fs,
    fs::File,
    io::{self, Seek, SeekFrom, Write},
    net::{TcpStream, ToSocketAddrs},
    os::windows::{
        ffi::OsStrExt,
        io::{AsRawHandle, FromRawHandle, RawHandle},
    },
    path::Path,
    time::Duration,
};

use windows_sys::Win32::{
    Foundation::{GetLastError, GENERIC_READ, GENERIC_WRITE, HANDLE, INVALID_HANDLE_VALUE},
    Storage::FileSystem::{
        CreateFileW, LockFileEx, UnlockFileEx, FILE_ATTRIBUTE_NORMAL, FILE_SHARE_READ,
        FILE_SHARE_WRITE, LOCKFILE_EXCLUSIVE_LOCK, LOCKFILE_FAIL_IMMEDIATELY, OPEN_ALWAYS,
    },
    System::IO::OVERLAPPED,
};

use crate::{
    lock::{InstanceLockError, InstanceLockFactory, InstanceLockGuard},
    path_norm::endpoint_path_for_io,
    traits::{windows_win32_error, EndpointSpec, PalError},
};

const ENDPOINT_PROBE_TIMEOUT: Duration = Duration::from_millis(200);

/// Windows Tier 1 實例鎖。
pub struct WindowsInstanceLock;

impl InstanceLockFactory for WindowsInstanceLock {
    fn acquire(
        &self,
        lock_path: &Path,
        endpoint: &EndpointSpec,
    ) -> Result<Box<dyn InstanceLockGuard>, InstanceLockError> {
        if let EndpointSpec::Tcp { host, port } = endpoint {
            if endpoint_alive(host, *port) {
                return Err(InstanceLockError::AlreadyRunning);
            }
        }

        let lock_path = endpoint_path_for_io(lock_path).map_err(file_error)?;
        if let Some(parent) = lock_path
            .parent()
            .filter(|parent| !parent.as_os_str().is_empty())
        {
            fs::create_dir_all(parent).map_err(file_error)?;
        }

        let wide_path: Vec<u16> = lock_path.as_os_str().encode_wide().chain(Some(0)).collect();
        // 安全性：`wide_path` 以 NUL 結尾，且在呼叫期間維持有效。
        let handle = unsafe {
            CreateFileW(
                wide_path.as_ptr(),
                GENERIC_READ | GENERIC_WRITE,
                FILE_SHARE_READ | FILE_SHARE_WRITE,
                std::ptr::null(),
                OPEN_ALWAYS,
                FILE_ATTRIBUTE_NORMAL,
                std::ptr::null_mut(),
            )
        };
        if handle == INVALID_HANDLE_VALUE {
            return Err(InstanceLockError::Io(windows_win32_error(unsafe {
                GetLastError()
            })));
        }
        // 安全性：`CreateFileW` 回傳有效的自有 handle，所有權移交給 `File`。
        let mut file = unsafe { File::from_raw_handle(handle as RawHandle) };

        let mut overlapped = OVERLAPPED::default();
        // 安全性：同步鎖定期間，檔案 handle 與 `OVERLAPPED` 都保持有效。
        let locked = unsafe {
            LockFileEx(
                file.as_raw_handle() as HANDLE,
                LOCKFILE_EXCLUSIVE_LOCK | LOCKFILE_FAIL_IMMEDIATELY,
                0,
                1,
                0,
                &mut overlapped,
            )
        };
        if locked == 0 {
            return Err(InstanceLockError::AlreadyRunning);
        }

        file.seek(SeekFrom::Start(0)).map_err(file_error)?;
        file.set_len(0).map_err(file_error)?;
        file.write_all(format!("{}\n", std::process::id()).as_bytes())
            .map_err(file_error)?;
        file.sync_all().map_err(file_error)?;

        Ok(Box::new(WindowsInstanceLockGuard { file: Some(file) }))
    }
}

struct WindowsInstanceLockGuard {
    file: Option<File>,
}

impl InstanceLockGuard for WindowsInstanceLockGuard {
    fn release(mut self: Box<Self>) {
        if let Some(file) = self.file.take() {
            let mut overlapped = OVERLAPPED::default();
            // 安全性：此 guard 持有的 handle 目前鎖住 offset 0 的一個位元組。
            unsafe {
                UnlockFileEx(file.as_raw_handle() as HANDLE, 0, 1, 0, &mut overlapped);
            }
            drop(file);
        }
    }
}

fn endpoint_alive(host: &str, port: u16) -> bool {
    (host, port)
        .to_socket_addrs()
        .map(|addresses| {
            addresses
                .into_iter()
                .any(|address| TcpStream::connect_timeout(&address, ENDPOINT_PROBE_TIMEOUT).is_ok())
        })
        .unwrap_or(false)
}

fn file_error(error: io::Error) -> InstanceLockError {
    match error.raw_os_error() {
        Some(code) => InstanceLockError::Io(windows_win32_error(code as u32)),
        None => InstanceLockError::Io(PalError::plain(
            crate::traits::PalErrorKind::Io,
            error.to_string(),
        )),
    }
}
