//! WAL 檔案層的原生實作（arch §4.5，P5-T6）。`WalFs`／`WalFile`／`FileIdentity`／`WalWriteError` 介面定義於 `crate::traits`。

pub use crate::traits::{FileIdentity, WalFile, WalFs, WalWriteError};

#[cfg(unix)]
#[path = "wal_fs/posix.rs"]
mod posix;
#[cfg(windows)]
#[path = "wal_fs/windows.rs"]
mod windows;

#[cfg(unix)]
pub use posix::PosixWalFs as NativeWalFs;
#[cfg(unix)]
pub use posix::PosixWalFs;
#[cfg(windows)]
pub use windows::WindowsWalFs as NativeWalFs;
#[cfg(windows)]
pub use windows::WindowsWalFs;
