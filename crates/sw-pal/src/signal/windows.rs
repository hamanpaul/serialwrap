//! Windows daemon 停止訊號：接收 Ctrl+C、Ctrl+Break 與 console 關閉事件。

use std::sync::{Mutex, OnceLock};

use crossbeam_channel::{Receiver, Sender};
use windows_sys::core::BOOL;
use windows_sys::Win32::{
    Foundation::GetLastError,
    System::Console::{SetConsoleCtrlHandler, CTRL_BREAK_EVENT, CTRL_CLOSE_EVENT, CTRL_C_EVENT},
};

use crate::traits::{windows_win32_error, PalResult, SignalWait};

static STOP_CHANNEL: OnceLock<(Sender<()>, Receiver<()>)> = OnceLock::new();
static HANDLER_REGISTERED: OnceLock<Mutex<bool>> = OnceLock::new();

#[derive(Clone, Copy, Debug, Default)]
pub struct WindowsSignalWait;

impl SignalWait for WindowsSignalWait {
    fn install(&self) -> PalResult<()> {
        let _ = STOP_CHANNEL.get_or_init(crossbeam_channel::unbounded);
        let mut registered = HANDLER_REGISTERED
            .get_or_init(|| Mutex::new(false))
            .lock()
            .expect("Windows signal handler state mutex poisoned");
        if *registered {
            return Ok(());
        }

        // 安全性：callback 只會將停止事件送進行程共用的 channel。
        if unsafe { SetConsoleCtrlHandler(Some(handle_console_control), 1) } == 0 {
            return Err(windows_win32_error(unsafe { GetLastError() }));
        }
        *registered = true;
        Ok(())
    }

    fn wait_stop(&self, extra: Receiver<()>) {
        let internal = STOP_CHANNEL
            .get()
            .map(|(_, receiver)| receiver.clone())
            .unwrap_or_else(crossbeam_channel::never);
        crossbeam_channel::select! {
            recv(internal) -> _ => {},
            recv(extra) -> _ => {},
        }
    }
}

unsafe extern "system" fn handle_console_control(control_type: u32) -> BOOL {
    match control_type {
        CTRL_C_EVENT | CTRL_BREAK_EVENT | CTRL_CLOSE_EVENT => {
            if let Some((sender, _)) = STOP_CHANNEL.get() {
                let _ = sender.try_send(());
            }
            1
        }
        _ => 0,
    }
}
