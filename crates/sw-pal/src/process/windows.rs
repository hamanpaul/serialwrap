//! Windows CreateProcess 與 Job Object 委派。

use crate::process::ExecOpts;
use crate::traits::{windows_win32_error, PalError, PalErrorKind, ProcessCtl};
use std::collections::BTreeMap;
use std::ffi::OsString;
use std::os::windows::ffi::OsStrExt;
use std::os::windows::io::AsRawHandle;
use std::os::windows::process::CommandExt;
use std::path::Path;
use std::process::{Command, Stdio};
use std::ptr;
use std::sync::{Arc, Mutex};
use windows_sys::Win32::Foundation::{
    CloseHandle, GetLastError, HANDLE, INVALID_HANDLE_VALUE, WAIT_OBJECT_0,
};
use windows_sys::Win32::System::Console::{
    GetStdHandle, SetConsoleCtrlHandler, STD_ERROR_HANDLE, STD_INPUT_HANDLE, STD_OUTPUT_HANDLE,
};
use windows_sys::Win32::System::JobObjects::{
    AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation,
    SetInformationJobObject, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE, JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK,
};
use windows_sys::Win32::System::Threading::{
    CreateProcessW, GetExitCodeProcess, ResumeThread, TerminateProcess, WaitForSingleObject,
    CREATE_NEW_PROCESS_GROUP, CREATE_NO_WINDOW, CREATE_SUSPENDED, CREATE_UNICODE_ENVIRONMENT,
    DETACHED_PROCESS, INFINITE, PROCESS_INFORMATION, STARTF_USESTDHANDLES, STARTUPINFOW,
};

#[derive(Clone, Copy, Debug, Default)]
pub struct NativeProcessCtl;

impl ProcessCtl for NativeProcessCtl {
    fn spawn_elevated(
        &self,
        _exe: &std::path::Path,
        _args: &[String],
    ) -> crate::traits::PalResult<crate::traits::ElevatedOutcome> {
        todo!("P4-T19: NativeProcessCtl::spawn_elevated 尚未實作")
    }

    fn exec_or_wait(&self, argv: &[OsString], env_add: &[(String, String)]) -> PalError {
        self.exec_or_wait_opts(
            argv,
            env_add,
            ExecOpts {
                no_window: false,
                kill_on_close: true,
            },
        )
    }

    fn exec_or_wait_opts(
        &self,
        argv: &[OsString],
        env_add: &[(String, String)],
        opts: ExecOpts,
    ) -> PalError {
        if argv.is_empty() {
            return windows_win32_error(87);
        }
        let mut application: Vec<u16> = argv[0].encode_wide().collect();
        if application.contains(&0)
            || argv
                .iter()
                .any(|arg| arg.encode_wide().any(|unit| unit == 0))
        {
            return PalError::plain(PalErrorKind::InvalidInput, "embedded null byte");
        }
        application.push(0);
        let application_name = if Path::new(&argv[0]).is_absolute() {
            application.as_ptr()
        } else {
            ptr::null()
        };
        let mut command_line = quote_command_line(argv);
        command_line.push(0);
        let mut environment = environment_block(env_add);
        let mut startup = STARTUPINFOW::default();
        startup.cb = std::mem::size_of::<STARTUPINFOW>() as u32;
        // GUI launcher 無 console 時仍須把由父行程繼承的 pipe／檔案 handle 傳給 helper。
        // 沒有三個有效 handle（例如 windows-task 無 console 啟動）時保留系統預設。
        // SAFETY: GetStdHandle 只讀取目前行程的預定標準 handle；後續只在三者皆非空且有效時轉交，未解參考 handle。
        let std_handles = unsafe {
            [
                GetStdHandle(STD_INPUT_HANDLE),
                GetStdHandle(STD_OUTPUT_HANDLE),
                GetStdHandle(STD_ERROR_HANDLE),
            ]
        };
        if std_handles
            .iter()
            .all(|handle| !handle.is_null() && *handle != INVALID_HANDLE_VALUE)
        {
            startup.dwFlags |= STARTF_USESTDHANDLES;
            startup.hStdInput = std_handles[0];
            startup.hStdOutput = std_handles[1];
            startup.hStdError = std_handles[2];
        }
        let mut process_info = PROCESS_INFORMATION::default();

        // SAFETY: 所有 UTF-16 緩衝區皆以 NUL 結尾，且呼叫期間保持可寫並有效。
        let created = unsafe {
            CreateProcessW(
                application_name,
                command_line.as_mut_ptr(),
                ptr::null(),
                ptr::null(),
                1,
                CREATE_SUSPENDED
                    | CREATE_UNICODE_ENVIRONMENT
                    | if opts.no_window { CREATE_NO_WINDOW } else { 0 },
                environment.as_mut_ptr().cast(),
                ptr::null(),
                &startup,
                &mut process_info,
            )
        };
        if created == 0 {
            // SAFETY: GetLastError 讀取目前執行緒最近一次 Win32 錯誤。
            return unsafe { windows_win32_error(GetLastError()) };
        }

        let job = if opts.kill_on_close {
            // SAFETY: NULL attributes 與名稱建立匿名 Job Object。
            let job = unsafe { CreateJobObjectW(ptr::null(), ptr::null()) };
            if job.is_null() {
                let error = last_error();
                terminate_and_close(&process_info, ptr::null_mut());
                return error;
            }
            let mut limits = JOBOBJECT_EXTENDED_LIMIT_INFORMATION::default();
            limits.BasicLimitInformation.LimitFlags =
                JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK;
            // SAFETY: limits 為正確大小且完整初始化的 Job Object 結構。
            let configured = unsafe {
                SetInformationJobObject(
                    job,
                    JobObjectExtendedLimitInformation,
                    (&limits as *const JOBOBJECT_EXTENDED_LIMIT_INFORMATION).cast(),
                    std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
                )
            };
            if configured == 0 {
                let error = last_error();
                terminate_and_close(&process_info, job);
                return error;
            }
            // SAFETY: process_info.hProcess 與 job 均由本次 Win32 建立呼叫取得。
            let assigned = unsafe { AssignProcessToJobObject(job, process_info.hProcess) };
            if assigned == 0 {
                let error = last_error();
                terminate_and_close(&process_info, job);
                return error;
            }
            job
        } else {
            ptr::null_mut()
        };
        // SAFETY: 將忽略 Ctrl-C 的旗標設在子行程建立後，避免旗標被子行程繼承。
        if unsafe { SetConsoleCtrlHandler(None, 1) } == 0 {
            let error = last_error();
            terminate_and_close(&process_info, job);
            return error;
        }
        // SAFETY: hThread 是 CreateProcessW 回傳的有效暫停執行緒控制代碼。
        if unsafe { ResumeThread(process_info.hThread) } == u32::MAX {
            let error = last_error();
            restore_ctrl_handler();
            terminate_and_close(&process_info, job);
            return error;
        }
        // SAFETY: WaitForSingleObject 等待有效 process handle，不設逾時。
        let wait_status = unsafe { WaitForSingleObject(process_info.hProcess, INFINITE) };
        if wait_status != WAIT_OBJECT_0 {
            let error = last_error();
            restore_ctrl_handler();
            terminate_and_close(&process_info, job);
            return error;
        }
        let mut exit_code = 0u32;
        // SAFETY: exit_code 是可寫 u32，handle 指向已等待完成的子行程。
        let got_code = unsafe { GetExitCodeProcess(process_info.hProcess, &mut exit_code) };
        let exit_error = (got_code == 0).then(last_error);
        restore_ctrl_handler();
        // SAFETY: thread 與 process handle 各關閉一次；Job Object 若建立也只關閉一次。
        unsafe {
            CloseHandle(process_info.hThread);
            CloseHandle(process_info.hProcess);
            if !job.is_null() {
                CloseHandle(job);
            }
        }
        if let Some(error) = exit_error {
            return error;
        }
        std::process::exit(exit_code as i32)
    }

    fn spawn_daemon(
        &self,
        argv: &[String],
        env: &[(OsString, OsString)],
        detached: bool,
    ) -> crate::traits::PalResult<crate::traits::ChildHandle> {
        if argv.is_empty() {
            return Err(windows_win32_error(87));
        }

        let mut command = Command::new(&argv[0]);
        command
            .args(&argv[1..])
            .env_clear()
            .envs(env.iter().map(|(key, value)| (key, value)));
        if detached {
            command
                .creation_flags(DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP)
                .stdin(Stdio::inherit())
                .stdout(Stdio::null())
                .stderr(Stdio::null());
        }

        let child = command.spawn().map_err(process_io_error)?;
        let pid = child.id();
        let mut handle = crate::traits::ChildHandle::new(pid);
        handle.native = Some(Arc::new(Mutex::new(child)));
        Ok(handle)
    }

    fn poll(&self, child: &crate::traits::ChildHandle) -> Option<i64> {
        let mut cached = child
            .exit_code
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        if let Some(code) = *cached {
            return Some(code);
        }
        let native = child.native.as_ref()?;
        let mut native = native.lock().ok()?;
        let status = native.try_wait().ok()??;
        let mut exit_code = 0u32;
        // SAFETY: hProcess 由此 Child 持有，exit_code 指向可寫 u32。
        let code =
            if unsafe { GetExitCodeProcess(native.as_raw_handle() as HANDLE, &mut exit_code) } != 0
            {
                i64::from(exit_code)
            } else {
                i64::from(status.code()? as u32)
            };
        *cached = Some(code);
        Some(code)
    }

    fn call_foreground(
        &self,
        argv: &[String],
        env: &[(OsString, OsString)],
    ) -> crate::traits::PalResult<i64> {
        if argv.is_empty() {
            return Err(windows_win32_error(87));
        }
        let mut child = Command::new(&argv[0])
            .args(&argv[1..])
            .env_clear()
            .envs(env.iter().map(|(key, value)| (key, value)))
            .spawn()
            .map_err(process_io_error)?;
        let _status = child.wait().map_err(process_io_error)?;
        let mut exit_code = 0u32;
        // SAFETY: hProcess 由此 Child 持有，exit_code 指向可寫 u32。
        if unsafe { GetExitCodeProcess(child.as_raw_handle() as HANDLE, &mut exit_code) } == 0 {
            return Err(last_error());
        }
        Ok(i64::from(exit_code))
    }
}

fn process_io_error(error: std::io::Error) -> PalError {
    match error.raw_os_error() {
        Some(code) if code >= 0 => windows_win32_error(code as u32),
        _ if error.kind() == std::io::ErrorKind::InvalidInput => {
            PalError::plain(PalErrorKind::InvalidInput, error.to_string())
        }
        _ => PalError::plain(PalErrorKind::Io, error.to_string()),
    }
}

fn last_error() -> PalError {
    // SAFETY: GetLastError 讀取目前執行緒最近一次 Win32 錯誤。
    unsafe { windows_win32_error(GetLastError()) }
}

fn restore_ctrl_handler() {
    // SAFETY: 移除前一個 NULL handler 登記的忽略 Ctrl-C 旗標。
    unsafe { SetConsoleCtrlHandler(None, 0) };
}

fn terminate_and_close(info: &PROCESS_INFORMATION, job: HANDLE) {
    // SAFETY: process/thread handles 由成功的 CreateProcessW 建立；job 可能為 NULL。
    unsafe {
        let _ = TerminateProcess(info.hProcess, 1);
        CloseHandle(info.hThread);
        CloseHandle(info.hProcess);
        if !job.is_null() {
            CloseHandle(job);
        }
    }
}

fn quote_command_line(argv: &[OsString]) -> Vec<u16> {
    let mut result = Vec::new();
    for (index, arg) in argv.iter().enumerate() {
        if index > 0 {
            result.push(b' ' as u16);
        }
        let units: Vec<u16> = arg.encode_wide().collect();
        let needs_quotes = units.is_empty()
            || units
                .iter()
                .any(|unit| *unit == b' ' as u16 || *unit == b'\t' as u16 || *unit == b'"' as u16);
        if !needs_quotes {
            result.extend(units);
            continue;
        }
        result.push(b'"' as u16);
        let mut backslashes = 0usize;
        for unit in units {
            if unit == b'\\' as u16 {
                backslashes += 1;
            } else if unit == b'"' as u16 {
                result.extend(std::iter::repeat_n(b'\\' as u16, backslashes * 2 + 1));
                result.push(unit);
                backslashes = 0;
            } else {
                result.extend(std::iter::repeat_n(b'\\' as u16, backslashes));
                result.push(unit);
                backslashes = 0;
            }
        }
        result.extend(std::iter::repeat_n(b'\\' as u16, backslashes * 2));
        result.push(b'"' as u16);
    }
    result
}

fn environment_block(env_add: &[(String, String)]) -> Vec<u16> {
    let mut entries = BTreeMap::<String, (OsString, OsString)>::new();
    for (key, value) in std::env::vars_os() {
        let folded = key.to_string_lossy().to_lowercase();
        entries.insert(folded, (key, value));
    }
    for (key, value) in env_add {
        entries.insert(key.to_lowercase(), (key.into(), value.into()));
    }
    let mut block = Vec::new();
    for (_, (key, value)) in entries {
        block.extend(key.encode_wide());
        block.push(b'=' as u16);
        block.extend(value.encode_wide());
        block.push(0);
    }
    block.push(0);
    if block.len() == 1 {
        block.push(0);
    }
    block
}
