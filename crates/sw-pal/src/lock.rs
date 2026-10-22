//! Tier 1 實例鎖（arch §4.4，P5-T4）：同一 `(lock_path, socket)` 只允許一個 daemon。
//!
//! 本檔為 Task 0 骨架：型別與簽章已凍結，`acquire` 的 POSIX／Windows 實作由 Task 4 交付。

use std::path::Path;
use std::sync::Arc;

use crate::traits::{EndpointSpec, PalError};

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
    /// POSIX：makedirs → open(0600) → flock(EX|NB)（失敗 → AlreadyRunning）→ socket 檔存在時 0.2s 試連：
    ///   連得上 → AlreadyRunning；ENOENT/ECONNREFUSED → unlink stale；其他錯誤 → SocketUnavailable → 寫 "pid\n" + fsync
    /// Windows：先試連 endpoint（0.2s）→ open → LockFileEx(EXCLUSIVE|FAIL_IMMEDIATELY, offset 0, len 1)
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
        _lock_path: &Path,
        _endpoint: &EndpointSpec,
    ) -> Result<Box<dyn InstanceLockGuard>, InstanceLockError> {
        // 計畫 06 Task 4 Step 2 的預期訊息為 `PosixInstanceLock::acquire`（POSIX 實作）；Windows 實作於同一 Task 交付。
        crate::s21_todo!("T4", "PosixInstanceLock::acquire")
    }
}

/// 原生工廠的共享實例（組裝點 `native().instance_lock` 由 Task 4 接線）。
pub fn native_instance_lock() -> Arc<dyn InstanceLockFactory> {
    Arc::new(NativeInstanceLockFactory)
}
