//! daemon 停止訊號等待的原生實作（arch §4.9，P5-T4）。`SignalWait` 介面定義於 `crate::traits`。
//!
//! POSIX 以 `pthread_sigmask(SIG_BLOCK, {SIGINT,SIGTERM})` 加專屬 `sigwait` thread；
//! Windows 以 `SetConsoleCtrlHandler` 接收停止訊號。

pub use crate::traits::SignalWait;

#[cfg(unix)]
pub mod posix;
#[cfg(windows)]
pub mod windows;

/// 原生 `SignalWait`（P5-T4）。
pub struct NativeSignalWait;

impl SignalWait for NativeSignalWait {
    fn install(&self) -> crate::traits::PalResult<()> {
        #[cfg(unix)]
        {
            posix::PosixSignalWait.install()
        }
        #[cfg(windows)]
        {
            windows::WindowsSignalWait.install()
        }
        #[cfg(not(any(unix, windows)))]
        {
            Err(crate::traits::PalError::plain(
                crate::traits::PalErrorKind::Unsupported,
                "signal wait is unsupported on this platform",
            ))
        }
    }

    fn wait_stop(&self, extra: crossbeam_channel::Receiver<()>) {
        #[cfg(unix)]
        posix::PosixSignalWait.wait_stop(extra);
        #[cfg(windows)]
        windows::WindowsSignalWait.wait_stop(extra);
        #[cfg(not(any(unix, windows)))]
        {
            let _ = extra.recv();
        }
    }
}
