//! `fs_ops` 的平台實作（P3-T9 骨架）：實作放在 `posix`／`windows` 子模組，依目標平台擇一編譯。
#[cfg(unix)]
pub mod posix;
#[cfg(windows)]
pub mod windows;
