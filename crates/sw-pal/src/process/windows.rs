//! Windows CreateProcess 與 Job Object 委派。

use crate::traits::{windows_win32_error, PalError, PalErrorKind, ProcessCtl};
use std::collections::BTreeMap;
use std::ffi::OsString;
use std::os::windows::ffi::OsStrExt;
use std::path::Path;
use std::ptr;
use windows_sys::Win32::Foundation::{CloseHandle, GetLastError, HANDLE, WAIT_OBJECT_0};
use windows_sys::Win32::System::Console::SetConsoleCtrlHandler;
use windows_sys::Win32::System::JobObjects::{
    AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation,
    SetInformationJobObject, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE, JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK,
};
use windows_sys::Win32::System::Threading::{
    CreateProcessW, GetExitCodeProcess, ResumeThread, TerminateProcess, WaitForSingleObject,
    CREATE_SUSPENDED, CREATE_UNICODE_ENVIRONMENT, INFINITE, PROCESS_INFORMATION, STARTUPINFOW,
};

#[derive(Clone, Copy, Debug, Default)]
pub struct NativeProcessCtl;

impl ProcessCtl for NativeProcessCtl {
    fn exec_or_wait(&self, argv: &[OsString], env_add: &[(String, String)]) -> PalError {
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
        let mut process_info = PROCESS_INFORMATION::default();

        // SAFETY: all UTF-16 buffers are NUL terminated and remain writable/alive through the call.
        let created = unsafe {
            CreateProcessW(
                application_name,
                command_line.as_mut_ptr(),
                ptr::null(),
                ptr::null(),
                1,
                CREATE_SUSPENDED | CREATE_UNICODE_ENVIRONMENT,
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
        // SAFETY: 這三個 handle 均由此函式建立，且各關閉一次。
        unsafe {
            CloseHandle(process_info.hThread);
            CloseHandle(process_info.hProcess);
            CloseHandle(job);
        }
        if let Some(error) = exit_error {
            return error;
        }
        std::process::exit(exit_code as i32)
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
