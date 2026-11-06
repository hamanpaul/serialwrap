//! Windows `ProcessTunnel` 實作（BC-05）。

use super::{read_stderr_tail, ProcGroupRef, ProcessTunnel, TunnelProc};
use crate::traits::{windows_win32_error, PalError, PalErrorKind, PalResult};
use std::collections::{HashMap, HashSet, VecDeque};
use std::fs::{self, OpenOptions};
use std::io;
use std::os::windows::io::AsRawHandle;
use std::path::Path;
use std::ptr;
use std::time::{Duration, Instant};
use windows_sys::Win32::Foundation::{
    CloseHandle, GetLastError, SetHandleInformation, FILETIME, HANDLE, HANDLE_FLAG_INHERIT,
    INVALID_HANDLE_VALUE, STILL_ACTIVE, WAIT_OBJECT_0, WAIT_TIMEOUT,
};
use windows_sys::Win32::System::Diagnostics::ToolHelp::{
    CreateToolhelp32Snapshot, Process32FirstW, Process32NextW, PROCESSENTRY32W, TH32CS_SNAPPROCESS,
};
use windows_sys::Win32::System::Threading::{
    CreateProcessW, DeleteProcThreadAttributeList, GetExitCodeProcess, GetProcessTimes,
    InitializeProcThreadAttributeList, OpenProcess, TerminateProcess, UpdateProcThreadAttribute,
    WaitForSingleObject, CREATE_NEW_PROCESS_GROUP, CREATE_NO_WINDOW, EXTENDED_STARTUPINFO_PRESENT,
    PROCESS_INFORMATION, PROCESS_QUERY_LIMITED_INFORMATION, PROCESS_TERMINATE,
    PROC_THREAD_ATTRIBUTE_HANDLE_LIST, STARTF_USESTDHANDLES, STARTUPINFOEXW,
};

const SYNCHRONIZE_ACCESS: u32 = 0x0010_0000;

#[derive(Clone, Copy, Debug, Default)]
pub struct NativeProcessTunnel;

impl ProcessTunnel for NativeProcessTunnel {
    fn spawn_detached(&self, argv: &[String], log: &Path) -> PalResult<TunnelProc> {
        if argv.is_empty() {
            return Err(PalError::plain(
                PalErrorKind::InvalidInput,
                "無法以空 argv 啟動分離行程",
            ));
        }
        let parent = log
            .parent()
            .filter(|path| !path.as_os_str().is_empty())
            .unwrap_or_else(|| Path::new("."));
        fs::create_dir_all(parent).map_err(file_error)?;
        let output = OpenOptions::new()
            .create(true)
            .append(true)
            .open(log)
            .map_err(file_error)?;
        let input = OpenOptions::new()
            .read(true)
            .open(r"\\.\NUL")
            .map_err(file_error)?;
        let mut handles = [
            input.as_raw_handle() as HANDLE,
            output.as_raw_handle() as HANDLE,
        ];
        let mut command_line = make_command_line(argv);
        let mut startup = unsafe { std::mem::zeroed::<STARTUPINFOEXW>() };
        startup.StartupInfo.cb = std::mem::size_of::<STARTUPINFOEXW>() as u32;
        startup.StartupInfo.dwFlags = STARTF_USESTDHANDLES;
        startup.StartupInfo.hStdInput = handles[0];
        startup.StartupInfo.hStdOutput = handles[1];
        startup.StartupInfo.hStdError = handles[1];
        let attributes = AttributeList::new(1)?;
        startup.lpAttributeList = attributes.as_ptr();
        // SAFETY: handles 在建立子行程期間有效；HANDLE_LIST 限制子行程只繼承 NUL 與 log，
        // 不會繼承呼叫端的其他 handle。兩個標準輸出都直接指向 log，未建立 pipe。
        unsafe {
            set_inheritable(handles[0], true)?;
            if let Err(error) = set_inheritable(handles[1], true) {
                let _ = set_inheritable(handles[0], false);
                return Err(error);
            }
            let handle_list_updated = UpdateProcThreadAttribute(
                startup.lpAttributeList,
                0,
                PROC_THREAD_ATTRIBUTE_HANDLE_LIST as usize,
                handles.as_mut_ptr().cast(),
                std::mem::size_of_val(&handles),
                ptr::null_mut(),
                ptr::null_mut(),
            );
            if handle_list_updated == 0 {
                let error = last_win32_error();
                let _ = set_inheritable(handles[0], false);
                let _ = set_inheritable(handles[1], false);
                return Err(error);
            }
        }

        let mut information = unsafe { std::mem::zeroed::<PROCESS_INFORMATION>() };
        // SAFETY: command line 是可寫 NUL 結尾 UTF-16；環境與目錄沿用目前行程；
        // startup attribute list 與 std handles 在呼叫期間有效。
        let created = unsafe {
            CreateProcessW(
                ptr::null(),
                command_line.as_mut_ptr(),
                ptr::null(),
                ptr::null(),
                1,
                CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW | EXTENDED_STARTUPINFO_PRESENT,
                ptr::null(),
                ptr::null(),
                (&startup.StartupInfo) as *const _,
                &mut information,
            )
        };
        let create_error = if created == 0 {
            Some(unsafe { GetLastError() })
        } else {
            None
        };
        unsafe {
            let _ = set_inheritable(handles[0], false);
            let _ = set_inheritable(handles[1], false);
        }
        if let Some(code) = create_error {
            return Err(windows_win32_error(code));
        }

        let process = OwnedHandle(information.hProcess);
        let _thread = OwnedHandle(information.hThread);
        let start_ticks = process_creation_time(process.0).ok_or_else(last_win32_error)?;
        Ok(TunnelProc {
            pid: information.dwProcessId,
            group: ProcGroupRef::WinTree,
            start_ticks: Some(start_ticks),
        })
    }

    fn alive(&self, process: &TunnelProc) -> bool {
        if process.start_ticks.is_none() || process.group != ProcGroupRef::WinTree {
            return false;
        }
        let Some(handle) = open_process(process.pid, PROCESS_QUERY_LIMITED_INFORMATION) else {
            return false;
        };
        let mut exit_code = 0;
        // SAFETY: handle 為本函式開啟且於呼叫期間有效；兩個輸出欄位可寫。
        unsafe {
            GetExitCodeProcess(handle.0, &mut exit_code) != 0
                && exit_code == STILL_ACTIVE as u32
                && process_creation_time(handle.0) == process.start_ticks
        }
    }

    fn terminate(&self, process: &TunnelProc, grace: Duration) -> PalResult<()> {
        let Some(expected_root_time) = process.start_ticks else {
            return Ok(());
        };
        if process.group != ProcGroupRef::WinTree {
            return Err(PalError::plain(
                PalErrorKind::InvalidInput,
                "Windows 行程通道需要 WinTree 群組識別",
            ));
        }
        let Some(root) = open_process_checked(process.pid, PROCESS_QUERY_LIMITED_INFORMATION)?
        else {
            return Ok(());
        };
        let Some(actual_root_time) = process_creation_time(root.0) else {
            return Err(last_win32_error());
        };
        if actual_root_time != expected_root_time {
            return Ok(());
        }

        let rows = snapshot_processes()?;
        let mut children: HashMap<u32, Vec<u32>> = HashMap::new();
        for row in rows {
            children.entry(row.parent_pid).or_default().push(row.pid);
        }

        let mut visited = HashSet::from([process.pid]);
        let mut queue = VecDeque::from([(process.pid, expected_root_time)]);
        let mut tree = vec![(process.pid, expected_root_time)];
        while let Some((parent_pid, parent_time)) = queue.pop_front() {
            for child_pid in children.get(&parent_pid).into_iter().flatten().copied() {
                if !visited.insert(child_pid) {
                    continue;
                }
                let Some(child) =
                    open_process_checked(child_pid, PROCESS_QUERY_LIMITED_INFORMATION)?
                else {
                    continue;
                };
                let Some(child_time) = process_creation_time(child.0) else {
                    return Err(last_win32_error());
                };
                if child_time >= parent_time {
                    tree.push((child_pid, child_time));
                    queue.push_back((child_pid, child_time));
                }
            }
        }

        let mut handles = Vec::new();
        for (pid, expected_time) in tree.into_iter().rev() {
            let access = PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_TERMINATE | SYNCHRONIZE_ACCESS;
            let Some(handle) = open_process_checked(pid, access)? else {
                continue;
            };
            let Some(actual_time) = process_creation_time(handle.0) else {
                return Err(last_win32_error());
            };
            if actual_time != expected_time {
                continue;
            }
            // SAFETY: handle 已以 PROCESS_TERMINATE 開啟，creation time 與快照記錄相符。
            if unsafe { TerminateProcess(handle.0, 1) } == 0 {
                // 若行程在呼叫前已退出，等待句柄已 signalled，視同成功收斂。
                if unsafe { WaitForSingleObject(handle.0, 0) } != WAIT_OBJECT_0 {
                    return Err(last_win32_error());
                }
            }
            handles.push(handle);
        }

        let deadline = Instant::now() + grace;
        loop {
            let mut all_exited = true;
            for handle in &handles {
                // SAFETY: 所有句柄均保留在 `handles` 直到檢查結束。
                match unsafe { WaitForSingleObject(handle.0, 0) } {
                    WAIT_OBJECT_0 => {}
                    WAIT_TIMEOUT => all_exited = false,
                    _ => return Err(last_win32_error()),
                }
            }
            if all_exited {
                return Ok(());
            }
            if Instant::now() >= deadline {
                return Err(PalError::plain(
                    PalErrorKind::TimedOut,
                    "Windows 行程樹未在 grace 時間內結束",
                ));
            }
            std::thread::sleep(Duration::from_millis(100));
        }
    }

    fn stderr_tail(&self, log: &Path, max: usize) -> String {
        read_stderr_tail(log, max)
    }

    fn supports_control_master(&self) -> bool {
        false
    }
}

#[derive(Clone, Copy)]
struct ProcessRow {
    pid: u32,
    parent_pid: u32,
}

fn snapshot_processes() -> PalResult<Vec<ProcessRow>> {
    // SAFETY: 此快照唯讀列舉目前程序；句柄由下方 RAII 包裝關閉。
    let snapshot = unsafe { CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0) };
    if snapshot == INVALID_HANDLE_VALUE {
        return Err(last_win32_error());
    }
    let _snapshot = OwnedHandle(snapshot);
    let mut entry = unsafe { std::mem::zeroed::<PROCESSENTRY32W>() };
    entry.dwSize = std::mem::size_of::<PROCESSENTRY32W>() as u32;
    let mut rows = Vec::new();
    // SAFETY: dwSize 已初始化，entry 為可寫的 PROCESSENTRY32W。
    if unsafe { Process32FirstW(snapshot, &mut entry) } != 0 {
        loop {
            rows.push(ProcessRow {
                pid: entry.th32ProcessID,
                parent_pid: entry.th32ParentProcessID,
            });
            // SAFETY: 與 Process32FirstW 相同，entry 可由系統更新。
            if unsafe { Process32NextW(snapshot, &mut entry) } == 0 {
                break;
            }
        }
    }
    Ok(rows)
}

fn process_creation_time(handle: HANDLE) -> Option<u64> {
    let mut creation = unsafe { std::mem::zeroed::<FILETIME>() };
    let mut exit = unsafe { std::mem::zeroed::<FILETIME>() };
    let mut kernel = unsafe { std::mem::zeroed::<FILETIME>() };
    let mut user = unsafe { std::mem::zeroed::<FILETIME>() };
    // SAFETY: handle 有效；四個 FILETIME 均為可寫輸出欄位。
    if unsafe { GetProcessTimes(handle, &mut creation, &mut exit, &mut kernel, &mut user) } == 0 {
        None
    } else {
        Some(((creation.dwHighDateTime as u64) << 32) | creation.dwLowDateTime as u64)
    }
}

fn open_process(pid: u32, access: u32) -> Option<OwnedHandle> {
    // SAFETY: OpenProcess 僅回傳本行程擁有的新 handle，無繼承權限。
    let handle = unsafe { OpenProcess(access, 0, pid) };
    if handle.is_null() {
        None
    } else {
        Some(OwnedHandle(handle))
    }
}

fn open_process_checked(pid: u32, access: u32) -> PalResult<Option<OwnedHandle>> {
    open_process_checked_with(pid, access, |pid, access| {
        // SAFETY: OpenProcess 僅回傳本行程擁有的新 handle，無繼承權限。
        let handle = unsafe { OpenProcess(access, 0, pid) };
        if handle.is_null() {
            Err(unsafe { GetLastError() })
        } else {
            Ok(OwnedHandle(handle))
        }
    })
}

fn open_process_checked_with(
    pid: u32,
    access: u32,
    open: impl FnOnce(u32, u32) -> Result<OwnedHandle, u32>,
) -> PalResult<Option<OwnedHandle>> {
    match open(pid, access) {
        Ok(handle) => Ok(Some(handle)),
        Err(87) => Ok(None), // ERROR_INVALID_PARAMETER：PID 已不存在。
        Err(code) => Err(windows_win32_error(code)),
    }
}

struct OwnedHandle(HANDLE);

impl Drop for OwnedHandle {
    fn drop(&mut self) {
        if !self.0.is_null() && self.0 != INVALID_HANDLE_VALUE {
            // SAFETY: 此 RAII 值唯一擁有由 Windows API 回傳的 handle。
            unsafe {
                CloseHandle(self.0);
            }
        }
    }
}

struct AttributeList {
    storage: Vec<usize>,
    list: windows_sys::Win32::System::Threading::LPPROC_THREAD_ATTRIBUTE_LIST,
}

impl AttributeList {
    fn new(count: u32) -> PalResult<Self> {
        let mut size = 0usize;
        // SAFETY: 第一次呼叫只查詢所需緩衝區大小，預期回傳 FALSE 並設定 size。
        unsafe {
            InitializeProcThreadAttributeList(ptr::null_mut(), count, 0, &mut size);
        }
        if size == 0 {
            return Err(last_win32_error());
        }
        let mut storage = vec![0usize; size.div_ceil(std::mem::size_of::<usize>())];
        let list = storage.as_mut_ptr().cast();
        // SAFETY: storage 有足夠容量且以 usize 對齊；API 於其中初始化 attribute list。
        if unsafe { InitializeProcThreadAttributeList(list, count, 0, &mut size) } == 0 {
            return Err(last_win32_error());
        }
        Ok(Self { storage, list })
    }

    fn as_ptr(&self) -> windows_sys::Win32::System::Threading::LPPROC_THREAD_ATTRIBUTE_LIST {
        let _keep_storage_alive = &self.storage;
        self.list
    }
}

impl Drop for AttributeList {
    fn drop(&mut self) {
        // SAFETY: list 已成功初始化，且 storage 尚未釋放。
        unsafe {
            DeleteProcThreadAttributeList(self.list);
        }
    }
}

unsafe fn set_inheritable(handle: HANDLE, inheritable: bool) -> PalResult<()> {
    // SAFETY: 呼叫端保證 handle 在目前範圍有效。
    if unsafe {
        SetHandleInformation(
            handle,
            HANDLE_FLAG_INHERIT,
            if inheritable { HANDLE_FLAG_INHERIT } else { 0 },
        )
    } == 0
    {
        Err(last_win32_error())
    } else {
        Ok(())
    }
}

fn make_command_line(argv: &[String]) -> Vec<u16> {
    let mut command = Vec::new();
    for (index, argument) in argv.iter().enumerate() {
        if index > 0 {
            command.push(b' ' as u16);
        }
        append_quoted_argument(&mut command, argument);
    }
    command.push(0);
    command
}

fn append_quoted_argument(output: &mut Vec<u16>, argument: &str) {
    let wide: Vec<u16> = argument.encode_utf16().collect();
    if !wide.is_empty()
        && !wide
            .iter()
            .any(|unit| *unit == b' ' as u16 || *unit == b'\t' as u16 || *unit == b'"' as u16)
    {
        output.extend(wide);
        return;
    }
    output.push(b'"' as u16);
    let mut backslashes = 0usize;
    for unit in wide {
        if unit == b'\\' as u16 {
            backslashes += 1;
        } else if unit == b'"' as u16 {
            output.extend(std::iter::repeat_n(b'\\' as u16, backslashes * 2 + 1));
            output.push(unit);
            backslashes = 0;
        } else {
            output.extend(std::iter::repeat_n(b'\\' as u16, backslashes));
            output.push(unit);
            backslashes = 0;
        }
    }
    output.extend(std::iter::repeat_n(b'\\' as u16, backslashes * 2));
    output.push(b'"' as u16);
}

fn file_error(error: io::Error) -> PalError {
    windows_win32_error(error.raw_os_error().unwrap_or(5) as u32)
}

fn last_win32_error() -> PalError {
    windows_win32_error(unsafe { GetLastError() })
}

#[cfg(test)]
mod tests {
    use super::open_process_checked_with;
    use crate::traits::PalErrorKind;

    #[test]
    fn terminate_open_access_denied_is_not_reported_as_success() {
        let result = open_process_checked_with(42, 1, |_, _| Err(5));
        match result {
            Err(error) => {
                assert_eq!(error.kind, PalErrorKind::PermissionDenied);
                assert_eq!(error.winerror, Some(5));
            }
            Ok(_) => panic!("PROCESS_TERMINATE 權限不足不得回報成功"),
        }
    }

    #[test]
    fn terminate_open_invalid_pid_confirms_process_exit() {
        let result = open_process_checked_with(42, 1, |_, _| Err(87));
        assert!(matches!(result, Ok(None)));
    }
}
