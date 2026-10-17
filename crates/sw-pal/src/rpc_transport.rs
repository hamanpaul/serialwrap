//! RPC transport 實作放在 `posix`／`windows` 子模組，依目標平台擇一編譯。
#[cfg(unix)]
pub mod posix;
#[cfg(windows)]
pub mod windows;
