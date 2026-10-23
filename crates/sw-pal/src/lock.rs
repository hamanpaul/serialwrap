//! Tier 1 實例鎖（arch §4.4，P5-T4）：同一 `(lock_path, socket)` 只允許一個 daemon。
//!
//! 平台細節分別位於 `lock/posix.rs` 與 `lock/windows.rs`。

use std::path::Path;
use std::sync::Arc;

use crate::traits::{EndpointSpec, PalError};

#[cfg(unix)]
pub mod posix;
#[cfg(windows)]
pub mod windows;

/// 實例鎖取得失敗的分類（daemon：stderr `serialwrapd: <CODE>`，exit 2）。
#[derive(Debug)]
pub enum InstanceLockError {
    AlreadyRunning,
    SocketUnavailable,
    Io(PalError),
}

/// 已取得的實例鎖；`release` 不刪除 lock 檔。
pub trait InstanceLockGuard: Send {
    fn release(self: Box<Self>);
}

/// 實例鎖工廠（arch §4.4）。
pub trait InstanceLockFactory: Send + Sync {
    /// POSIX：建立目錄 → 以 0600 開檔 → 非阻塞 flock；鎖失敗回 `AlreadyRunning`。
    /// socket 檔存在時試連 0.2 秒：連得上回 `AlreadyRunning`；ENOENT／ECONNREFUSED 視為失效 socket 並略過 unlink 錯誤；其他錯誤回 `SocketUnavailable`；最後寫入 PID 並 fsync。
    /// Windows：先試連 endpoint 0.2 秒；再以 `LockFileEx(EXCLUSIVE|FAIL_IMMEDIATELY)` 鎖住 offset 0 的一個位元組。
    fn acquire(
        &self,
        lock_path: &Path,
        endpoint: &EndpointSpec,
    ) -> Result<Box<dyn InstanceLockGuard>, InstanceLockError>;
}

/// 原生實例鎖工廠（P5-T4 實作）。
pub struct NativeInstanceLockFactory;

impl InstanceLockFactory for NativeInstanceLockFactory {
    fn acquire(
        &self,
        lock_path: &Path,
        endpoint: &EndpointSpec,
    ) -> Result<Box<dyn InstanceLockGuard>, InstanceLockError> {
        #[cfg(unix)]
        {
            posix::PosixInstanceLock.acquire(lock_path, endpoint)
        }
        #[cfg(windows)]
        {
            windows::WindowsInstanceLock.acquire(lock_path, endpoint)
        }
        #[cfg(not(any(unix, windows)))]
        {
            let _ = (lock_path, endpoint);
            Err(InstanceLockError::Io(PalError::plain(
                crate::traits::PalErrorKind::Unsupported,
                "instance lock is unsupported on this platform",
            )))
        }
    }
}

/// 原生工廠的共享實例（組裝點 `native().instance_lock` 由 Task 4 接線）。
pub fn native_instance_lock() -> Arc<dyn InstanceLockFactory> {
    Arc::new(NativeInstanceLockFactory)
}
