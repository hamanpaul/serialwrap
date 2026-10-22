//! daemon 停止訊號等待的原生實作（arch §4.9，P5-T4）。`SignalWait` 介面定義於 `crate::traits`。
//!
//! Task 0 骨架：POSIX 以 `pthread_sigmask(SIG_BLOCK, {SIGINT,SIGTERM})` 加專屬 sigwait thread；
//! Windows 以 `SetConsoleCtrlHandler`。實作由 Task 4 交付。

pub use crate::traits::SignalWait;

/// 原生 `SignalWait`（P5-T4）。
pub struct NativeSignalWait;

impl SignalWait for NativeSignalWait {
    fn install(&self) -> crate::traits::PalResult<()> {
        crate::s21_todo!("T4", "NativeSignalWait::install")
    }

    fn wait_stop(&self, _extra: crossbeam_channel::Receiver<()>) {
        crate::s21_todo!("T4", "NativeSignalWait::wait_stop")
    }
}
