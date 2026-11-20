//! 裝置來源（arch §4.3，P5-T5）。
//!
//! `posix_scan`（POSIX by-id／by-path 掃描）與 `WindowsDeviceSource`（SERIALCOMM＋藍牙排除＋OD-11 allowlist）為 P5-T5 的實作面；
//! `windows_model` 為 P4 05 已實作的平台無關模型，`windows_registry` 只在 Windows 編譯。
pub mod posix;
#[cfg(unix)]
pub mod usb_posix;
pub mod windows;
pub mod windows_model;
#[cfg(windows)]
pub mod windows_registry;

pub use posix::posix_scan;
pub(crate) use posix::PosixDeviceSource;
pub use windows::WindowsDeviceSource;

/// `/dev/serial/by-id` 實體路徑 → USB busid（`resolve_usb_busid`；P7 Task 0 凍結、Task 6 實作）。
pub trait UsbTopology: Send + Sync {
    fn usb_busid(&self, real_path: &str) -> Option<String>;
}

/// 原生 `UsbTopology`（POSIX 讀 sysfs；Windows 回 `None`；Task 6 實作）。
pub struct NativeUsbTopology;

impl UsbTopology for NativeUsbTopology {
    #[cfg(unix)]
    fn usb_busid(&self, real_path: &str) -> Option<String> {
        usb_posix::SysfsUsbTopology::default().usb_busid(real_path)
    }

    /// Windows：`resolve_usb_busid` 對非 POSIX 恆回 None。
    #[cfg(not(unix))]
    fn usb_busid(&self, _real_path: &str) -> Option<String> {
        None
    }
}
