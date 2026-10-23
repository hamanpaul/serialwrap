//! 裝置來源（arch §4.3，P5-T5）。
//!
//! `posix_scan`（POSIX by-id／by-path 掃描）與 `WindowsDeviceSource`（SERIALCOMM＋藍牙排除＋OD-11 allowlist）為 P5-T5 的實作面；
//! `windows_model` 為 P4 05 已實作的平台無關模型，`windows_registry` 只在 Windows 編譯。
pub mod posix;
pub mod windows;
pub mod windows_model;
#[cfg(windows)]
pub mod windows_registry;

pub use posix::posix_scan;
pub(crate) use posix::PosixDeviceSource;
pub use windows::WindowsDeviceSource;
