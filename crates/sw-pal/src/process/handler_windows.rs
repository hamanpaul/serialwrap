//! Windows event handler 執行（P7 Task 14，SC-26）：`CreateProcessW`（`CREATE_NO_WINDOW|CREATE_SUSPENDED|
//! CREATE_UNICODE_ENVIRONMENT|EXTENDED_STARTUPINFO_PRESENT`；`PROC_THREAD_ATTRIBUTE_HANDLE_LIST` 只列三條 stdio pipe；
//! 環境區塊只含 spec.env）→ Job Object（`KILL_ON_JOB_CLOSE`）→ `ResumeThread`；逾時或 abort → `TerminateJobObject`。
//! `shell` 路徑的 command line 逐字組為 `cmd.exe /d /s /c "<shell>"`（不對 `<shell>` 套 MSVC 跳脫）。

use std::ffi::OsString;
use std::fs::File;
use std::os::windows::io::FromRawHandle;
use std::ptr;
use std::sync::mpsc;
use std::time::{Duration, Instant};

use windows_sys::Win32::Foundation::{
    CloseHandle, GetLastError, SetHandleInformation, HANDLE, HANDLE_FLAG_INHERIT, WAIT_OBJECT_0,
};
use windows_sys::Win32::Security::SECURITY_ATTRIBUTES;
use windows_sys::Win32::System::JobObjects::{
    AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation,
    SetInformationJobObject, TerminateJobObject, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
};
use windows_sys::Win32::System::Pipes::CreatePipe;
use windows_sys::Win32::System::Threading::{
    CreateProcessW, DeleteProcThreadAttributeList, GetExitCodeProcess,
    InitializeProcThreadAttributeList, ResumeThread, UpdateProcThreadAttribute,
    WaitForSingleObject, CREATE_NO_WINDOW, CREATE_SUSPENDED, CREATE_UNICODE_ENVIRONMENT,
    EXTENDED_STARTUPINFO_PRESENT, INFINITE, LPPROC_THREAD_ATTRIBUTE_LIST, PROCESS_INFORMATION,
    PROC_THREAD_ATTRIBUTE_HANDLE_LIST, STARTF_USESTDHANDLES, STARTUPINFOEXW, STARTUPINFOW,
};

use super::windows::quote_command_line;
use super::{spawn_stdin_writer, spawn_tail_reader, HandlerAbort, HandlerOutcome, HandlerSpec};
use crate::traits::{windows_win32_error, PalError, PalErrorKind};

/// dispatcher `_INHERITED_ENV_KEYS` 再加 SC-26 的 Windows 六鍵。
pub(super) const INHERITED_ENV_KEYS: &[&str] = &[
    "PATH",
    "HOME",
    "USER",
    "LANG",
    "LC_ALL",
    "SYSTEMROOT",
    "COMSPEC",
    "PATHEXT",
    "TEMP",
    "TMP",
    "USERPROFILE",
];

const POLL: Duration = Duration::from_millis(10);
const SHELL_PREFIX: [&str; 4] = ["cmd.exe", "/d", "/s", "/c"];

pub(super) fn shell_argv(shell: &str) -> Vec<String> {
    let mut v: Vec<String> = SHELL_PREFIX.iter().map(|s| s.to_string()).collect();
    v.push(shell.into());
    v
}

fn last_error() -> PalError {
    // SAFETY: GetLastError 讀取目前執行緒最近一次 Win32 錯誤。
    windows_win32_error(unsafe { GetLastError() })
}

/// `cmd.exe /d /s /c "<shell>"`（shell 形）或 MSVC 引號規則（exec 形）。
fn command_line(argv: &[String]) -> Vec<u16> {
    let mut units: Vec<u16> = if argv.len() == 5 && argv[..4] == SHELL_PREFIX {
        format!("cmd.exe /d /s /c \"{}\"", argv[4])
            .encode_utf16()
            .collect()
    } else {
        let os: Vec<OsString> = argv.iter().map(OsString::from).collect();
        quote_command_line(&os)
    };
    units.push(0);
    units
}

/// `CREATE_UNICODE_ENVIRONMENT` 區塊：只含 spec.env，依不分大小寫鍵排序（Windows 要求）。
fn env_block(env: &[(String, String)]) -> Vec<u16> {
    let mut entries: Vec<(String, &String, &String)> =
        env.iter().map(|(k, v)| (k.to_uppercase(), k, v)).collect();
    entries.sort_by(|a, b| a.0.cmp(&b.0));
    entries.dedup_by(|a, b| a.0 == b.0);
    let mut block = Vec::new();
    for (_, k, v) in entries {
        block.extend(format!("{k}={v}").encode_utf16());
        block.push(0);
    }
    block.push(0);
    if block.len() == 1 {
        block.push(0);
    }
    block
}

struct Owned(HANDLE);
impl Drop for Owned {
    fn drop(&mut self) {
        if !self.0.is_null() {
            // SAFETY: handle 由本模組建立且只關閉一次。
            unsafe { CloseHandle(self.0) };
        }
    }
}

/// 建立 pipe：`child_reads` 為真時子端是讀端（stdin），否則子端是寫端；父端設為不可繼承。
fn pipe(child_reads: bool) -> Result<(Owned, Owned), PalError> {
    let sa = SECURITY_ATTRIBUTES {
        nLength: std::mem::size_of::<SECURITY_ATTRIBUTES>() as u32,
        lpSecurityDescriptor: ptr::null_mut(),
        bInheritHandle: 1,
    };
    let (mut r, mut w): (HANDLE, HANDLE) = (ptr::null_mut(), ptr::null_mut());
    // SAFETY: 兩個輸出指標指向可寫的 HANDLE，sa 在呼叫期間有效。
    if unsafe { CreatePipe(&mut r, &mut w, &sa, 0) } == 0 {
        return Err(last_error());
    }
    let (r, w) = (Owned(r), Owned(w));
    let (child, parent) = if child_reads { (r, w) } else { (w, r) };
    // SAFETY: parent 為剛建立的有效 handle；只清除可繼承旗標。
    if unsafe { SetHandleInformation(parent.0, HANDLE_FLAG_INHERIT, 0) } == 0 {
        return Err(last_error());
    }
    Ok((child, parent))
}

fn take_file(h: &mut Owned) -> File {
    let raw = std::mem::replace(&mut h.0, ptr::null_mut());
    // SAFETY: raw 為有效且所有權移轉給 File 的 pipe handle。
    unsafe { File::from_raw_handle(raw as _) }
}

pub(super) fn run(spec: &HandlerSpec, abort: &HandlerAbort) -> HandlerOutcome {
    if spec.argv.is_empty() {
        return HandlerOutcome::SpawnFailed(PalError::plain(
            PalErrorKind::InvalidInput,
            "argv must not be empty",
        ));
    }
    if spec
        .argv
        .iter()
        .chain(spec.env.iter().flat_map(|(k, v)| [k, v]))
        .any(|s| s.contains('\0'))
    {
        return HandlerOutcome::SpawnFailed(PalError::plain(
            PalErrorKind::InvalidInput,
            "embedded null character",
        ));
    }
    match spawn(spec) {
        Ok(running) => running.wait(spec, abort),
        Err(e) => HandlerOutcome::SpawnFailed(e),
    }
}

struct Running {
    process: Owned,
    job: Owned,
    out: std::thread::JoinHandle<Vec<u8>>,
    err: std::thread::JoinHandle<Vec<u8>>,
    done: mpsc::Receiver<()>,
    start: Instant,
}

fn spawn(spec: &HandlerSpec) -> Result<Running, PalError> {
    // 從建立可繼承子端 pipe 到 CreateProcessW 後關閉子端為止，與其他 CreateProcessW 互斥（審查 96b28dd_06c61d0 L3）。
    let spawn_guard = super::WIN_SPAWN_LOCK
        .lock()
        .unwrap_or_else(|e| e.into_inner());
    let (child_in, mut parent_in) = pipe(true)?;
    let (child_out, mut parent_out) = pipe(false)?;
    let (child_err, mut parent_err) = pipe(false)?;
    let handles: [HANDLE; 3] = [child_in.0, child_out.0, child_err.0];

    let mut size = 0usize;
    // SAFETY: 第一次呼叫只查詢所需大小（預期回 FALSE＋ERROR_INSUFFICIENT_BUFFER）。
    unsafe { InitializeProcThreadAttributeList(ptr::null_mut(), 1, 0, &mut size) };
    let mut attr_buf = vec![0u8; size.max(1)];
    let attrs = attr_buf.as_mut_ptr() as LPPROC_THREAD_ATTRIBUTE_LIST;
    // SAFETY: attr_buf 具 size bytes，屬性數 1。
    if unsafe { InitializeProcThreadAttributeList(attrs, 1, 0, &mut size) } == 0 {
        return Err(last_error());
    }
    struct AttrList(LPPROC_THREAD_ATTRIBUTE_LIST);
    impl Drop for AttrList {
        fn drop(&mut self) {
            // SAFETY: 由成功的 InitializeProcThreadAttributeList 初始化。
            unsafe { DeleteProcThreadAttributeList(self.0) };
        }
    }
    let _attrs_guard = AttrList(attrs);
    // SAFETY: handles 陣列在 CreateProcessW 完成前保持有效。
    if unsafe {
        UpdateProcThreadAttribute(
            attrs,
            0,
            PROC_THREAD_ATTRIBUTE_HANDLE_LIST as usize,
            handles.as_ptr() as *const _,
            std::mem::size_of_val(&handles),
            ptr::null_mut(),
            ptr::null(),
        )
    } == 0
    {
        return Err(last_error());
    }

    // SAFETY: STARTUPINFOEXW 全零為合法初值。
    let mut si: STARTUPINFOEXW = unsafe { std::mem::zeroed() };
    si.StartupInfo.cb = std::mem::size_of::<STARTUPINFOEXW>() as u32;
    si.StartupInfo.dwFlags = STARTF_USESTDHANDLES;
    si.StartupInfo.hStdInput = child_in.0;
    si.StartupInfo.hStdOutput = child_out.0;
    si.StartupInfo.hStdError = child_err.0;
    si.lpAttributeList = attrs;
    let mut cmdline = command_line(&spec.argv);
    let env = env_block(&spec.env);
    // SAFETY: PROCESS_INFORMATION 全零為合法初值。
    let mut info: PROCESS_INFORMATION = unsafe { std::mem::zeroed() };
    // SAFETY: cmdline 為可寫、NUL 結尾的 UTF-16；env 為雙 NUL 結尾區塊；si／info 在呼叫期間有效。
    let ok = unsafe {
        CreateProcessW(
            ptr::null(),
            cmdline.as_mut_ptr(),
            ptr::null(),
            ptr::null(),
            1,
            CREATE_NO_WINDOW
                | CREATE_SUSPENDED
                | CREATE_UNICODE_ENVIRONMENT
                | EXTENDED_STARTUPINFO_PRESENT,
            env.as_ptr() as *const _,
            ptr::null(),
            &si as *const STARTUPINFOEXW as *const STARTUPINFOW,
            &mut info,
        )
    };
    if ok == 0 {
        return Err(last_error());
    }
    let process = Owned(info.hProcess);
    let thread = Owned(info.hThread);
    // 子端 handle 已複製進子行程，父行程立即關閉（否則讀端永遠等不到 EOF）。
    drop((child_in, child_out, child_err));
    drop(spawn_guard);

    // SAFETY: 建立匿名 Job。
    let job = Owned(unsafe { CreateJobObjectW(ptr::null(), ptr::null()) });
    let kill = |e: PalError| {
        // SAFETY: process 為有效且尚未恢復執行的行程。
        unsafe { windows_sys::Win32::System::Threading::TerminateProcess(process.0, 1) };
        e
    };
    if job.0.is_null() {
        return Err(kill(last_error()));
    }
    // SAFETY: 全零為合法初值。
    let mut limits: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = unsafe { std::mem::zeroed() };
    limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
    // SAFETY: limits 指向大小正確的結構。
    if unsafe {
        SetInformationJobObject(
            job.0,
            JobObjectExtendedLimitInformation,
            &limits as *const _ as *const _,
            std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
        )
    } == 0
    {
        return Err(kill(last_error()));
    }
    // SAFETY: job／process 皆為有效 handle。
    if unsafe { AssignProcessToJobObject(job.0, process.0) } == 0 {
        return Err(kill(last_error()));
    }

    let (done_tx, done) = mpsc::channel();
    let out = spawn_tail_reader(take_file(&mut parent_out), spec.tail_bytes, done_tx.clone());
    let err = spawn_tail_reader(take_file(&mut parent_err), spec.tail_bytes, done_tx);
    spawn_stdin_writer(take_file(&mut parent_in), spec.stdin.clone());
    // SAFETY: thread 為 CREATE_SUSPENDED 建立的主執行緒。
    if unsafe { ResumeThread(thread.0) } == u32::MAX {
        let e = last_error();
        // SAFETY: job 有效；終止整個 Job。
        unsafe { TerminateJobObject(job.0, 1) };
        return Err(e);
    }
    Ok(Running {
        process,
        job,
        out,
        err,
        done,
        start: Instant::now(),
    })
}

impl Running {
    fn wait(self, spec: &HandlerSpec, abort: &HandlerAbort) -> HandlerOutcome {
        let deadline = self.start + spec.timeout;
        let mut exited = false;
        let mut eof = 0usize;
        loop {
            if !exited {
                // SAFETY: process 為有效 handle；0 ms 等待即查詢。
                exited = unsafe { WaitForSingleObject(self.process.0, 0) } == WAIT_OBJECT_0;
            }
            eof += self.done.try_iter().count();
            if exited && eof >= 2 {
                break;
            }
            let now = Instant::now();
            if abort.is_triggered() || now >= deadline {
                // SAFETY: job 有效；終止 Job 內所有行程（含孫行程）。
                unsafe {
                    TerminateJobObject(self.job.0, 1);
                    WaitForSingleObject(self.process.0, INFINITE);
                }
                let _ = self.out.join();
                let _ = self.err.join();
                return HandlerOutcome::TimedOut;
            }
            abort.wait_timeout(POLL.min(deadline - now));
        }
        let mut code = 0u32;
        // SAFETY: process 已結束；code 為可寫輸出。
        unsafe { GetExitCodeProcess(self.process.0, &mut code) };
        HandlerOutcome::Completed {
            exit_code: code as i32,
            stdout_tail: self.out.join().unwrap_or_default(),
            stderr_tail: self.err.join().unwrap_or_default(),
        }
    }
}
