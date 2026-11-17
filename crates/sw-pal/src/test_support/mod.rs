//! 僅供平台整合測試使用的原生 console control 輔助。

use std::io;
use std::process::{Child, Command};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ConsoleControlEvent {
    /// Windows CTRL_C_EVENT。
    CtrlC,
    /// Windows CTRL_BREAK_EVENT。
    CtrlBreak,
}

/// 回報目前行程是否連接互動 console。
pub fn has_interactive_console() -> bool {
    platform::has_interactive_console()
}

/// 為新 process group 重新啟用 Ctrl+C 接收。
pub fn enable_ctrl_c() -> io::Result<()> {
    platform::enable_ctrl_c()
}

/// 以獨立 console process group 啟動子行程。
pub fn spawn_console_process_group(command: Command) -> io::Result<Child> {
    platform::spawn_console_process_group(command)
}

/// 將 console control event 定向送至指定 process group。
pub fn send_console_control_event(group_id: u32, event: ConsoleControlEvent) -> io::Result<()> {
    platform::send_console_control_event(group_id, event)
}

#[cfg(windows)]
mod platform {
    use std::io;
    use std::os::windows::process::CommandExt;
    use std::process::{Child, Command};

    use windows_sys::Win32::System::Console::{
        GenerateConsoleCtrlEvent, GetConsoleProcessList, SetConsoleCtrlHandler, CTRL_BREAK_EVENT,
        CTRL_C_EVENT,
    };

    const CREATE_NEW_PROCESS_GROUP: u32 = 0x0000_0200;

    pub fn has_interactive_console() -> bool {
        let mut process_ids = [0u32; 64];
        unsafe { GetConsoleProcessList(process_ids.as_mut_ptr(), process_ids.len() as u32) > 0 }
    }

    pub fn enable_ctrl_c() -> io::Result<()> {
        if unsafe { SetConsoleCtrlHandler(None, 0) } == 0 {
            Err(io::Error::last_os_error())
        } else {
            Ok(())
        }
    }

    pub fn spawn_console_process_group(mut command: Command) -> io::Result<Child> {
        command.creation_flags(CREATE_NEW_PROCESS_GROUP);
        command.spawn()
    }

    pub fn send_console_control_event(
        group_id: u32,
        event: super::ConsoleControlEvent,
    ) -> io::Result<()> {
        let control_type = match event {
            super::ConsoleControlEvent::CtrlC => CTRL_C_EVENT,
            super::ConsoleControlEvent::CtrlBreak => CTRL_BREAK_EVENT,
        };
        if unsafe { GenerateConsoleCtrlEvent(control_type, group_id) } == 0 {
            Err(io::Error::last_os_error())
        } else {
            Ok(())
        }
    }
}

#[cfg(not(windows))]
mod platform {
    use std::io;
    use std::process::{Child, Command};

    pub fn has_interactive_console() -> bool {
        false
    }

    pub fn enable_ctrl_c() -> io::Result<()> {
        Err(unsupported())
    }

    pub fn spawn_console_process_group(_command: Command) -> io::Result<Child> {
        Err(unsupported())
    }

    pub fn send_console_control_event(
        _group_id: u32,
        _event: super::ConsoleControlEvent,
    ) -> io::Result<()> {
        Err(unsupported())
    }

    fn unsupported() -> io::Error {
        io::Error::new(
            io::ErrorKind::Unsupported,
            "Windows console control event 僅支援 Windows",
        )
    }
}
