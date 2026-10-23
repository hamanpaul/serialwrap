//! 序列埠後端（arch §4.2）。POSIX 實作在 `posix`；Windows 後端由 Task 3 承接。
//!
//! 跨平台 trait（`SerialBackend`、`SerialHandle`、`UartExclusive` 等）定義於 `crate::traits`。

#[cfg(unix)]
pub mod posix;

#[cfg(windows)]
pub mod windows;

/// Windows 後端的純函式模型（DCB、COMMTIMEOUTS、錯誤映射；無平台依賴，P5 Task 3）。
pub mod windows_model;

/// Windows 的 holder 診斷（arch §4.2）：無法掃描 tty 持有者，回 `holder_unknown:true`、`status:Unsupported`。
#[cfg(windows)]
pub struct WindowsUartExclusive;

#[cfg(windows)]
impl crate::traits::UartExclusive for WindowsUartExclusive {
    fn probe_holders(&self, _real_path: &str) -> crate::traits::HolderReport {
        crate::traits::HolderReport {
            pids: Vec::new(),
            holder_unknown: true,
            status: crate::traits::HolderScanStatus::Unsupported,
        }
    }
}
