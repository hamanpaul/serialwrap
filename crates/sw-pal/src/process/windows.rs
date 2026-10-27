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
    CloseHandle, FreeLibrary, GetHandleInformation, GetLastError, SetHandleInformation, HANDLE,
    HANDLE_FLAG_INHERIT, INVALID_HANDLE_VALUE, WAIT_OBJECT_0,
};
use windows_sys::Win32::System::Console::{
    GetStdHandle, SetConsoleCtrlHandler, STD_ERROR_HANDLE, STD_INPUT_HANDLE, STD_OUTPUT_HANDLE,
};
use windows_sys::Win32::System::JobObjects::{
    AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation,
    SetInformationJobObject, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
};
use windows_sys::Win32::System::LibraryLoader::{
    GetProcAddress, LoadLibraryExW, LOAD_LIBRARY_SEARCH_SYSTEM32,
};
use windows_sys::Win32::System::Threading::{
    CreateProcessW, GetExitCodeProcess, ResumeThread, TerminateProcess, WaitForSingleObject,
    CREATE_NEW_PROCESS_GROUP, CREATE_NO_WINDOW, CREATE_SUSPENDED, CREATE_UNICODE_ENVIRONMENT,
    DETACHED_PROCESS, INFINITE, PROCESS_INFORMATION, STARTF_USESTDHANDLES, STARTUPINFOW,
};
use windows_sys::Win32::UI::Shell::{SEE_MASK_NOCLOSEPROCESS, SHELLEXECUTEINFOW};

/// 串行化「暫時取消標準輸出 handle 繼承旗標」的區段，避免並行 spawn 互相還原彼此的旗標。
///
/// 本模組內直接以原始標準 handle 呼叫 `CreateProcessW` 的路徑（`exec_or_wait_opts`）也持有此鎖，
/// 不會在旗標被暫時取消的視窗內把不可繼承的 handle 交給子行程。std `Command` 的 `Stdio::inherit()`
/// 會另行 `DuplicateHandle` 出可繼承副本，不受此旗標影響。
///
/// 鎖只在同一行程多執行緒並行 spawn 時有作用：正式 `sw-cli` 無執行緒、`exec_or_wait_opts` 只在
/// `sw-daemon` launcher 另一 binary，實際只保護 cargo test 的並行測試執行緒；正確性以程式碼審查為證（OD-26 (c)）。
static STD_INHERIT_LOCK: Mutex<()> = Mutex::new(());

/// spawn 背景 daemon 期間暫時取消目前行程 stdout／stderr 的 `HANDLE_FLAG_INHERIT`，drop 時還原。
///
/// 子行程的 stdout／stderr 已明確指定為 NUL（std 會另建可繼承的 NUL handle），原始 handle 不需傳給子行程；
/// 與 stdin 相同的 handle 不動（stdin 依 Python 基線語意繼承）。
///
/// 已知限制：std 以 `bInheritHandles=TRUE` 建立行程，只處理標準輸出 handle；CLI 由父行程繼承而來、
/// 且仍標為可繼承的其他 handle（非標準 handle）仍會傳給 daemon。CLI 自身以 std 開啟的檔案與 pipe
/// 預設不可繼承，故實務上只剩父行程刻意傳入的額外 handle。完整解法為改以
/// `PROC_THREAD_ATTRIBUTE_HANDLE_LIST` 明確列出要繼承的 handle（需改寫成原始 `CreateProcessW`）。
///
/// OD-26（2026-10-27）：擁有者裁決維持本半修法（功能面已對齊 Python 基線），不改寫 `CreateProcessW`。
/// 追蹤項：std 的 `CommandExt::inherit_handles(false)`（feature `windows_process_extensions_inherit_handles`）
/// 與 `ProcThreadAttributeList`（feature `windows_process_extensions_raw_attribute`）在 1.96.1 stable
/// 仍為 unstable（E0658）；穩定後改用 `inherit_handles(false)`，並移除本 guard 與 `STD_INHERIT_LOCK`。
struct StdOutputInheritGuard {
    _lock: std::sync::MutexGuard<'static, ()>,
    cleared: Vec<HANDLE>,
}

impl StdOutputInheritGuard {
    fn suppress() -> Self {
        let lock = STD_INHERIT_LOCK
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        let mut cleared: Vec<HANDLE> = Vec::new();
        // SAFETY: GetStdHandle 只讀取目前行程的標準 handle 值，不解參考。
        let stdin = unsafe { GetStdHandle(STD_INPUT_HANDLE) };
        for id in [STD_OUTPUT_HANDLE, STD_ERROR_HANDLE] {
            // SAFETY: 同上。
            let handle = unsafe { GetStdHandle(id) };
            if handle.is_null()
                || handle == INVALID_HANDLE_VALUE
                || handle == stdin
                || cleared.contains(&handle)
            {
                continue;
            }
            let mut flags = 0u32;
            // SAFETY: handle 為目前行程的有效標準 handle；flags 指向可寫 u32。
            if unsafe { GetHandleInformation(handle, &mut flags) } == 0
                || flags & HANDLE_FLAG_INHERIT == 0
            {
                continue;
            }
            // SAFETY: 只修改繼承旗標，不關閉或轉移 handle 所有權。
            if unsafe { SetHandleInformation(handle, HANDLE_FLAG_INHERIT, 0) } != 0 {
                cleared.push(handle);
            }
        }
        Self {
            _lock: lock,
            cleared,
        }
    }
}

impl Drop for StdOutputInheritGuard {
    fn drop(&mut self) {
        for handle in &self.cleared {
            // SAFETY: 還原 suppress 時清除的繼承旗標；handle 仍為目前行程的標準 handle。
            unsafe {
                SetHandleInformation(*handle, HANDLE_FLAG_INHERIT, HANDLE_FLAG_INHERIT);
            }
        }
    }
}

#[derive(Clone, Copy, Debug, Default)]
pub struct NativeProcessCtl;

impl ProcessCtl for NativeProcessCtl {
    fn spawn_elevated(
        &self,
        exe: &std::path::Path,
        args: &[String],
    ) -> crate::traits::PalResult<crate::traits::ElevatedOutcome> {
        use crate::traits::ElevatedOutcome;
        use std::ffi::OsStr;

        if exe.as_os_str().encode_wide().any(|unit| unit == 0)
            || args
                .iter()
                .any(|arg| arg.encode_utf16().any(|unit| unit == 0))
        {
            return Err(crate::traits::PalError::plain(
                PalErrorKind::InvalidInput,
                "embedded null byte",
            ));
        }

        let mut shell_name: Vec<u16> = OsStr::new("shell32.dll").encode_wide().collect();
        shell_name.push(0);
        let module = unsafe {
            LoadLibraryExW(
                shell_name.as_ptr(),
                ptr::null_mut(),
                LOAD_LIBRARY_SEARCH_SYSTEM32,
            )
        };
        if module.is_null() {
            return Err(windows_win32_error(unsafe { GetLastError() }));
        }

        let procedure = unsafe { GetProcAddress(module, c"ShellExecuteExW".as_ptr().cast()) };
        let Some(procedure) = procedure else {
            let code = unsafe { GetLastError() };
            unsafe { FreeLibrary(module) };
            return Err(windows_win32_error(code));
        };
        // Shell32 只在明確選用提權時動態載入，避免產品匯入表新增 ShellExecuteExW。
        let shell_execute: unsafe extern "system" fn(*mut SHELLEXECUTEINFOW) -> i32 =
            unsafe { std::mem::transmute(procedure) };

        let mut verb: Vec<u16> = OsStr::new("runas").encode_wide().collect();
        verb.push(0);
        let mut file: Vec<u16> = exe.as_os_str().encode_wide().collect();
        file.push(0);
        let mut parameters =
            quote_command_line(&args.iter().map(OsString::from).collect::<Vec<_>>());
        parameters.push(0);
        let mut info = SHELLEXECUTEINFOW::default();
        info.cbSize = std::mem::size_of::<SHELLEXECUTEINFOW>() as u32;
        info.fMask = SEE_MASK_NOCLOSEPROCESS;
        info.lpVerb = verb.as_ptr();
        info.lpFile = file.as_ptr();
        info.lpParameters = if args.is_empty() {
            ptr::null()
        } else {
            parameters.as_ptr()
        };
        info.nShow = 0;

        let result = unsafe { shell_execute(&mut info) };
        if result == 0 {
            let code = unsafe { GetLastError() };
            unsafe { FreeLibrary(module) };
            if code == 1223 {
                return Ok(ElevatedOutcome::Declined);
            }
            return Err(windows_win32_error(code));
        }
        if info.hProcess.is_null() {
            unsafe { FreeLibrary(module) };
            return Err(crate::traits::PalError::plain(
                PalErrorKind::Io,
                "ShellExecuteExW 未回傳提權行程 handle",
            ));
        }

        let wait = unsafe { WaitForSingleObject(info.hProcess, INFINITE) };
        let mut exit_code = 0u32;
        let got_exit_code = wait == WAIT_OBJECT_0
            && unsafe { GetExitCodeProcess(info.hProcess, &mut exit_code) } != 0;
        let wait_error = if got_exit_code {
            None
        } else {
            Some(unsafe { GetLastError() })
        };
        unsafe {
            CloseHandle(info.hProcess);
            FreeLibrary(module);
        }
        if let Some(code) = wait_error {
            return Err(windows_win32_error(code));
        }
        Ok(ElevatedOutcome::Exited(exit_code as i32))
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

        // 傳入的是原始標準 handle：與 StdOutputInheritGuard 串行，避免在其暫時取消繼承旗標時建立行程。
        let inherit_lock = STD_INHERIT_LOCK
            .lock()
            .unwrap_or_else(|error| error.into_inner());
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
        drop(inherit_lock);
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
            // 只設 KILL_ON_JOB_CLOSE，不設 SILENT_BREAKAWAY_OK：實測（Windows 11 26200）該旗標會讓
            // 一般 CreateProcess 建立的孫行程靜默脫離 Job（即使未要求 breakaway），launcher 被終止後孫行程仍存活，
            // 違反委派的終止傳遞不變式。正常結束時的背景子行程保留由 release_kill_on_close 處理。
            limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
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
        // 子行程已正常結束：先解除 KILL_ON_JOB_CLOSE 再關閉 Job，使背景啟動的孫行程（例如 daemon start 建立的 daemon）
        // 照常存活，與 Python 基線一致。launcher 若被 TerminateProcess 終止，此步驟不會執行，Job 仍會連帶結束所有子孫。
        let release_error = if job.is_null() {
            None
        } else {
            release_kill_on_close(job)
        };
        // SAFETY: thread 與 process handle 各關閉一次；Job Object 若建立也只關閉一次。
        unsafe {
            CloseHandle(process_info.hThread);
            CloseHandle(process_info.hProcess);
            if !job.is_null() {
                CloseHandle(job);
            }
        }
        if let Some(error) = release_error {
            return error;
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

        // std 以 bInheritHandles=TRUE 建立行程：CLI 自身可繼承的 stdout／stderr（呼叫端擷取輸出的 pipe）
        // 會被背景 daemon 一併繼承，使呼叫端在 CLI 結束後仍等到 daemon 結束才 EOF（SC-18 autostart 卡死）。
        // 與 Python 基線（close_fds 只傳標準 handle）對齊：spawn 期間暫時取消這兩個 handle 的繼承旗標。
        let _inherit_guard = detached.then(StdOutputInheritGuard::suppress);
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

/// 解除 Job 的 KILL_ON_JOB_CLOSE（清空 LimitFlags）。失敗時回傳錯誤；呼叫端仍會關閉 Job，不留下狀態不明的 Job。
fn release_kill_on_close(job: HANDLE) -> Option<PalError> {
    let limits = JOBOBJECT_EXTENDED_LIMIT_INFORMATION::default();
    // SAFETY: limits 為正確大小且完整初始化（LimitFlags 為 0）的 Job Object 結構；job 由呼叫端持有且有效。
    let configured = unsafe {
        SetInformationJobObject(
            job,
            JobObjectExtendedLimitInformation,
            (&limits as *const JOBOBJECT_EXTENDED_LIMIT_INFORMATION).cast(),
            std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
        )
    };
    (configured == 0).then(last_error)
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
