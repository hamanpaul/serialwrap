//! POSIX daemon 訊號等待：在主執行緒封鎖訊號，再由專屬執行緒呼叫 `sigwait`。

use std::{
    io,
    sync::{Mutex, OnceLock},
    thread,
};

use crossbeam_channel::{Receiver, Sender};

use crate::traits::{posix_errno, PalError, PalErrorKind, PalResult, SignalWait};

static STOP_RECEIVER: OnceLock<Mutex<Option<Receiver<()>>>> = OnceLock::new();

fn stop_receiver() -> &'static Mutex<Option<Receiver<()>>> {
    STOP_RECEIVER.get_or_init(|| Mutex::new(None))
}

#[derive(Clone, Copy, Debug, Default)]
pub struct PosixSignalWait;

impl SignalWait for PosixSignalWait {
    fn install(&self) -> PalResult<()> {
        let mut installed = stop_receiver()
            .lock()
            .expect("POSIX signal wait state mutex poisoned");
        if installed.is_some() {
            return Ok(());
        }

        let signals = stop_signal_set()?;
        let receiver = install_with_spawn(signals, |signals, sender| {
            thread::Builder::new()
                .name("serialwrap-sigwait".to_string())
                .spawn(move || wait_for_stop_signal(signals, sender))
                .map(|_| ())
        })?;
        *installed = Some(receiver);
        Ok(())
    }

    fn wait_stop(&self, extra: Receiver<()>) {
        let internal = stop_receiver()
            .lock()
            .expect("POSIX signal wait state mutex poisoned")
            .as_ref()
            .cloned()
            .unwrap_or_else(crossbeam_channel::never);
        crossbeam_channel::select! {
            recv(internal) -> _ => {},
            recv(extra) -> _ => {},
        }
    }
}

fn wait_for_stop_signal(signals: libc::sigset_t, sender: Sender<()>) {
    let mut received = 0;
    let rc = unsafe { libc::sigwait(&signals, &mut received) };
    if rc == 0 {
        let _ = sender.send(());
    }
}

fn stop_signal_set() -> PalResult<libc::sigset_t> {
    let mut signals: libc::sigset_t = unsafe { std::mem::zeroed() };
    let mut rc = unsafe { libc::sigemptyset(&mut signals) };
    if rc != 0 {
        return Err(posix_errno(unsafe { *libc::__errno_location() }));
    }
    for signal in [libc::SIGINT, libc::SIGTERM] {
        rc = unsafe { libc::sigaddset(&mut signals, signal) };
        if rc != 0 {
            return Err(posix_errno(unsafe { *libc::__errno_location() }));
        }
    }
    Ok(signals)
}

fn install_with_spawn<F>(signals: libc::sigset_t, spawn: F) -> PalResult<Receiver<()>>
where
    F: FnOnce(libc::sigset_t, Sender<()>) -> io::Result<()>,
{
    let mut previous_mask: libc::sigset_t = unsafe { std::mem::zeroed() };
    let rc = unsafe { libc::pthread_sigmask(libc::SIG_BLOCK, &signals, &mut previous_mask) };
    if rc != 0 {
        return Err(posix_errno(rc));
    }

    let (sender, receiver) = crossbeam_channel::unbounded();
    if let Err(error) = spawn(signals, sender) {
        let rc = unsafe {
            libc::pthread_sigmask(libc::SIG_SETMASK, &previous_mask, std::ptr::null_mut())
        };
        if rc != 0 {
            return Err(posix_errno(rc));
        }
        return Err(PalError::plain(PalErrorKind::Io, error.to_string()));
    }
    Ok(receiver)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn failed_signal_thread_spawn_restores_previous_mask() {
        let mut before: libc::sigset_t = unsafe { std::mem::zeroed() };
        let rc = unsafe { libc::pthread_sigmask(libc::SIG_SETMASK, std::ptr::null(), &mut before) };
        assert_eq!(rc, 0);

        let result = install_with_spawn(stop_signal_set().unwrap(), |_, _| {
            Err(io::Error::other("模擬 thread 建立失敗"))
        });
        assert!(result.is_err());

        let mut after: libc::sigset_t = unsafe { std::mem::zeroed() };
        let rc = unsafe { libc::pthread_sigmask(libc::SIG_SETMASK, std::ptr::null(), &mut after) };
        assert_eq!(rc, 0);
        for signal in [libc::SIGINT, libc::SIGTERM] {
            assert_eq!(
                unsafe { libc::sigismember(&before, signal) },
                unsafe { libc::sigismember(&after, signal) },
                "失敗時應還原原本的 SIGINT／SIGTERM 遮罩"
            );
        }
    }
}
