//! 序列埠後端（arch §4.2）。POSIX 實作在 `posix`（P5 Task 2）；Windows 後端為 Task 3 的骨架。
//!
//! 跨平台 trait（`SerialBackend`、`SerialHandle`、`UartExclusive` 等）定義於 `crate::traits`。

#[cfg(unix)]
pub mod posix;

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
        unimplemented!("slice21 T3 WindowsSerialBackend::open_exclusive")
    }
}
