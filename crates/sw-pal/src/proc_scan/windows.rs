//! Windows Toolhelp32 行程掃描（P4-T4）。
use std::mem::size_of;
use std::path::PathBuf;
use std::slice;

use crate::proc_scan::{
    dedup_launcher_children, is_launcher_image, is_python_image, DaemonProc, HolderScanStatus,
    ProcIdentity, ProcScan, ScanFailure, WinProcInfo,
};
use windows_sys::Wdk::System::Threading::{
    NtQueryInformationProcess, ProcessCommandLineInformation,
};
use windows_sys::Win32::Foundation::{
    CloseHandle, GetLastError, ERROR_INVALID_PARAMETER, ERROR_NO_MORE_FILES, FILETIME, HANDLE,
    INVALID_HANDLE_VALUE, STILL_ACTIVE,
};
use windows_sys::Win32::System::Diagnostics::ToolHelp::{
    CreateToolhelp32Snapshot, Process32FirstW, Process32NextW, PROCESSENTRY32W, TH32CS_SNAPPROCESS,
};
use windows_sys::Win32::System::Threading::{
    GetExitCodeProcess, GetProcessTimes, OpenProcess, PROCESS_QUERY_LIMITED_INFORMATION,
};

/// Windows 行程快照掃描器。
#[derive(Debug, Clone, Default)]
pub struct WindowsProcScan;

impl WindowsProcScan {
    pub fn new() -> Self {
        Self
    }

    pub fn serialwrapd_instances(&self) -> Result<Vec<DaemonProc>, ScanFailure> {
        <Self as ProcScan>::serialwrapd_instances(self)
    }

    pub fn serialwrapd_tty_holders(&self, paths: &[PathBuf]) -> (Vec<u32>, HolderScanStatus) {
        <Self as ProcScan>::serialwrapd_tty_holders(self, paths)
    }

    pub fn pid_alive(&self, pid: u32) -> bool {
        <Self as ProcScan>::pid_alive(self, pid)
    }

    pub fn identity_alive(&self, id: &ProcIdentity) -> bool {
        <Self as ProcScan>::identity_alive(self, id)
    }
}

impl ProcScan for WindowsProcScan {
    fn serialwrapd_instances(&self) -> Result<Vec<DaemonProc>, ScanFailure> {
        // SAFETY: 快照使用 process-list flag 與 system-wide pid 0。
        let snapshot = unsafe { CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0) };
        if snapshot.is_null() || snapshot == INVALID_HANDLE_VALUE {
            return Err(ScanFailure);
        }

        let mut entry = PROCESSENTRY32W::default();
        entry.dwSize = size_of::<PROCESSENTRY32W>() as u32;
        // SAFETY: entry.dwSize 已設定，snapshot 是有效快照 handle。
        if unsafe { Process32FirstW(snapshot, &mut entry) } == 0 {
            // SAFETY: 讀取目前執行緒最近一次 Win32 錯誤碼。
            let error = unsafe { GetLastError() };
            // SAFETY: snapshot 由本函式建立，且尚未關閉。
            unsafe { CloseHandle(snapshot) };
            return if error == ERROR_NO_MORE_FILES {
                Ok(Vec::new())
            } else {
                Err(ScanFailure)
            };
        }

        let mut processes = Vec::new();
        loop {
            let image = decode_wide_nul(&entry.szExeFile);
            if is_launcher_image(&image) || is_python_image(&image) {
                let creation_time = process_creation_time(entry.th32ProcessID).unwrap_or(0);
                let argv = read_process_command_line(entry.th32ProcessID).unwrap_or_default();
                if is_launcher_image(&image) || !argv.is_empty() {
                    processes.push(WinProcInfo {
                        pid: entry.th32ProcessID,
                        parent_pid: entry.th32ParentProcessID,
                        image,
                        argv,
                        creation_time,
                    });
                }
            }

            entry.dwSize = size_of::<PROCESSENTRY32W>() as u32;
            // SAFETY: entry.dwSize 已設定，snapshot 是有效快照 handle。
            if unsafe { Process32NextW(snapshot, &mut entry) } == 0 {
                // SAFETY: 讀取目前執行緒最近一次 Win32 錯誤碼。
                let error = unsafe { GetLastError() };
                // SAFETY: snapshot 由本函式建立，且尚未關閉。
                unsafe { CloseHandle(snapshot) };
                if error != ERROR_NO_MORE_FILES {
                    return Err(ScanFailure);
                }
                break;
            }
        }
        Ok(dedup_launcher_children(&processes))
    }

    fn serialwrapd_tty_holders(&self, _paths: &[PathBuf]) -> (Vec<u32>, HolderScanStatus) {
        (Vec::new(), HolderScanStatus::Unsupported)
    }

    fn pid_alive(&self, pid: u32) -> bool {
        let handle = match open_query_handle(pid) {
            Ok(handle) => handle,
            Err(error) => return open_error_means_alive(error),
        };
        let mut exit_code = 0u32;
        // SAFETY: exit_code 指向可寫 u32，handle 是僅供查詢的 process handle。
        let query_succeeded = unsafe { GetExitCodeProcess(handle, &mut exit_code) } != 0;
        // SAFETY: handle 由本函式取得，且尚未關閉。
        unsafe { CloseHandle(handle) };
        exit_query_means_alive(query_succeeded, exit_code)
    }

    fn identity_alive(&self, id: &ProcIdentity) -> bool {
        let handle = match open_query_handle(id.pid) {
            Ok(handle) => handle,
            Err(error) => return open_error_means_alive(error),
        };
        let mut exit_code = 0u32;
        let mut creation = FILETIME::default();
        let mut exit = FILETIME::default();
        let mut kernel = FILETIME::default();
        let mut user = FILETIME::default();
        // SAFETY: exit_code 指向可寫 u32，handle 是有效的 process query handle。
        let query_succeeded = unsafe { GetExitCodeProcess(handle, &mut exit_code) } != 0;
        // SAFETY: 所有 FILETIME 輸出參數可寫，handle 是有效的查詢 handle。
        let creation_time = if query_succeeded && exit_code == STILL_ACTIVE as u32 {
            (unsafe { GetProcessTimes(handle, &mut creation, &mut exit, &mut kernel, &mut user) }
                != 0)
                .then(|| filetime_value(creation))
        } else {
            None
        };
        // SAFETY: handle 由本函式取得，且只在此處關閉一次。
        unsafe { CloseHandle(handle) };
        identity_query_means_alive(query_succeeded, exit_code, creation_time, id.start_ticks)
    }
}

#[repr(C)]
struct UnicodeString {
    length: u16,
    maximum_length: u16,
    buffer: *mut u16,
}

fn decode_wide_nul(units: &[u16]) -> String {
    let length = units
        .iter()
        .position(|unit| *unit == 0)
        .unwrap_or(units.len());
    String::from_utf16_lossy(&units[..length])
}

fn open_query_handle(pid: u32) -> Result<HANDLE, u32> {
    if pid == 0 {
        return Err(ERROR_INVALID_PARAMETER);
    }
    // SAFETY: 只對指定 pid 要求查詢權限。
    let handle = unsafe { OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid) };
    if handle.is_null() {
        // SAFETY: 讀取 OpenProcess 失敗後目前執行緒的 Win32 錯誤碼。
        Err(unsafe { GetLastError() })
    } else {
        Ok(handle)
    }
}

fn open_error_means_alive(error: u32) -> bool {
    error != ERROR_INVALID_PARAMETER
}

fn exit_query_means_alive(query_succeeded: bool, exit_code: u32) -> bool {
    !query_succeeded || exit_code == STILL_ACTIVE as u32
}

fn identity_query_means_alive(
    exit_query_succeeded: bool,
    exit_code: u32,
    creation_time: Option<u64>,
    expected: Option<u64>,
) -> bool {
    if !exit_query_succeeded {
        return true;
    }
    if exit_code != STILL_ACTIVE as u32 {
        return false;
    }
    identity_matches_or_unknown(expected, creation_time)
}

fn identity_matches_or_unknown(expected: Option<u64>, actual: Option<u64>) -> bool {
    match (expected, actual) {
        (Some(expected), Some(actual)) => expected == actual,
        _ => true,
    }
}

fn process_creation_time(pid: u32) -> Option<u64> {
    let handle = open_query_handle(pid).ok()?;
    let mut creation = FILETIME::default();
    let mut exit = FILETIME::default();
    let mut kernel = FILETIME::default();
    let mut user = FILETIME::default();
    // SAFETY: FILETIME 輸出參數可寫，handle 是有效的 process query handle。
    let ok = unsafe { GetProcessTimes(handle, &mut creation, &mut exit, &mut kernel, &mut user) };
    // SAFETY: handle 由本函式取得，且尚未關閉。
    unsafe { CloseHandle(handle) };
    (ok != 0).then(|| filetime_value(creation))
}

fn filetime_value(value: FILETIME) -> u64 {
    (u64::from(value.dwHighDateTime) << 32) | u64::from(value.dwLowDateTime)
}

fn read_process_command_line(pid: u32) -> Option<Vec<String>> {
    let handle = open_query_handle(pid).ok()?;
    let result = query_process_command_line(handle);
    // SAFETY: handle 由本函式取得，且尚未關閉。
    unsafe { CloseHandle(handle) };
    result
}

fn query_process_command_line(handle: HANDLE) -> Option<Vec<String>> {
    let mut required = 0u32;
    // SAFETY: 空指標與零長度用來向系統查詢所需輸出緩衝區大小。
    let _ = unsafe {
        NtQueryInformationProcess(
            handle,
            ProcessCommandLineInformation,
            std::ptr::null_mut(),
            0,
            &mut required,
        )
    };
    if usize::try_from(required).ok()? < size_of::<UnicodeString>() {
        return None;
    }

    let mut buffer = aligned_buffer(required)?;
    let mut buffer_bytes = buffer.len().checked_mul(size_of::<u64>())?;
    let mut returned = 0u32;
    let mut status = query_into_buffer(handle, &mut buffer, &mut returned)?;
    if status == 0xC000_0004u32 as i32 && returned as usize > buffer_bytes {
        buffer = aligned_buffer(returned)?;
        buffer_bytes = buffer.len().checked_mul(size_of::<u64>())?;
        returned = 0;
        status = query_into_buffer(handle, &mut buffer, &mut returned)?;
    }
    if status < 0 {
        return None;
    }

    let base = buffer.as_ptr().cast::<u8>();
    // SAFETY: 成功時核心至少填入對齊且足以容納 UNICODE_STRING 的緩衝區。
    let command = unsafe { &*base.cast::<UnicodeString>() };
    let length = usize::from(command.length);
    let offset = (command.buffer as usize).checked_sub(base as usize)?;
    if command.buffer.is_null()
        || length % size_of::<u16>() != 0
        || offset > buffer_bytes
        || length > buffer_bytes.saturating_sub(offset)
        || (command.buffer as usize) % std::mem::align_of::<u16>() != 0
    {
        return None;
    }
    // SAFETY: 已依擁有的查詢緩衝區檢查指標範圍與對齊。
    let units = unsafe { slice::from_raw_parts(command.buffer, length / size_of::<u16>()) };
    let command_line = String::from_utf16_lossy(units);
    Some(crate::traits::split_windows_cmdline(&command_line))
}

fn aligned_buffer(bytes: u32) -> Option<Vec<u64>> {
    let words = usize::try_from(bytes)
        .ok()?
        .checked_add(size_of::<u64>() - 1)?
        / size_of::<u64>();
    Some(vec![0u64; words])
}

fn query_into_buffer(handle: HANDLE, buffer: &mut [u64], returned: &mut u32) -> Option<i32> {
    let byte_len = buffer.len().checked_mul(size_of::<u64>())?;
    let byte_len = u32::try_from(byte_len).ok()?;
    // SAFETY: 緩衝區可寫且對齊，byte_len 與配置大小相符。
    Some(unsafe {
        NtQueryInformationProcess(
            handle,
            ProcessCommandLineInformation,
            buffer.as_mut_ptr().cast(),
            byte_len,
            returned,
        )
    })
}

#[cfg(test)]
mod tests {
    use super::{
        exit_query_means_alive, identity_matches_or_unknown, identity_query_means_alive,
        open_error_means_alive,
    };
    use windows_sys::Win32::Foundation::{
        ERROR_ACCESS_DENIED, ERROR_INVALID_PARAMETER, STILL_ACTIVE,
    };

    #[test]
    fn access_denied_is_inconclusive_but_invalid_pid_is_dead() {
        assert!(open_error_means_alive(ERROR_ACCESS_DENIED));
        assert!(!open_error_means_alive(ERROR_INVALID_PARAMETER));
    }

    #[test]
    fn unknown_creation_time_is_conservatively_alive() {
        assert!(identity_matches_or_unknown(Some(10), None));
        assert!(identity_matches_or_unknown(None, Some(10)));
        assert!(identity_matches_or_unknown(Some(10), Some(10)));
        assert!(!identity_matches_or_unknown(Some(10), Some(11)));
    }

    #[test]
    fn failed_process_queries_never_confirm_exit() {
        assert!(exit_query_means_alive(false, 0));
        assert!(identity_query_means_alive(false, 0, None, Some(10)));
        assert!(identity_query_means_alive(
            true,
            STILL_ACTIVE as u32,
            None,
            Some(10)
        ));
        assert!(!identity_query_means_alive(true, 0, Some(10), Some(10)));
    }
}
