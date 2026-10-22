//! 序列埠後端（arch §4.2）。POSIX 實作在 `posix`（P5 Task 2）；Windows 後端為 Task 3 的骨架。
//!
//! 跨平台 trait（`SerialBackend`、`SerialHandle`、`UartExclusive` 等）定義於 `crate::traits`。

#[cfg(unix)]
pub mod posix;

/// Windows 後端的純函式模型（DCB、COMMTIMEOUTS、錯誤映射；無平台依賴，Task 3）。
pub mod windows_model;

/// Windows 序列埠後端骨架（P5 Task 3 以 overlapped I/O 取代；`open_exclusive` 尚未實作）。
#[cfg(windows)]
pub struct WindowsSerialStub;

#[cfg(windows)]
impl crate::traits::SerialBackend for WindowsSerialStub {
    fn open_exclusive(
        &self,
        _dev: &crate::traits::DevicePath,
        _cfg: &crate::traits::SerialConfig,
    ) -> Result<Box<dyn crate::traits::SerialHandle>, crate::traits::OpenError> {
        s21_todo!("T3", "WindowsSerialBackend::open_exclusive")
    }
}

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
