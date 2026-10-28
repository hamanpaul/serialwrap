//! POSIX event handler 執行（P7 Task 14，SC-26；Python `dispatcher._run_fire` 的 `subprocess.Popen` 等價）：
//! `start_new_session=True`（setsid）、`close_fds=True`（fd≥3 標 CLOEXEC）、`env` 只放 spec、stdio 三條 pipe；
//! `communicate(stdin, timeout)`：子行程結束且 stdout／stderr 皆 EOF 才算完成；逾時 `killpg(SIGTERM)` → 等 1.0 s →
//! `killpg(SIGKILL)` → 讀完剩餘輸出。

use std::ffi::CString;
use std::io;
use std::os::unix::process::{CommandExt, ExitStatusExt};
use std::process::{Command, Stdio};
use std::sync::mpsc;
use std::time::{Duration, Instant};

use super::posix::{mark_inherited_fds_cloexec, max_open_fd};
use super::{spawn_stdin_writer, spawn_tail_reader, HandlerAbort, HandlerOutcome, HandlerSpec};
use crate::traits::{posix_errno, posix_io_error, PalError, PalErrorKind};

/// dispatcher `_INHERITED_ENV_KEYS`。
pub(super) const INHERITED_ENV_KEYS: &[&str] = &["PATH", "HOME", "USER", "LANG", "LC_ALL"];

/// 主迴圈的輪詢粒度（同時是 abort 觸發的最大反應延遲）。
const POLL: Duration = Duration::from_millis(10);
/// Python `proc.wait(timeout=1.0)`：SIGTERM 後等子行程結束的寬限。
const TERM_GRACE: Duration = Duration::from_secs(1);

/// CPython `os.defpath`（spec.env 無 `PATH` 時的查找路徑）。
const DEFPATH: &str = "/bin:/usr/bin";

/// fork 前備妥的 `execve` 參數（CPython `_posixsubprocess.child_exec`）：argv0 含 `/` 時只試其本身，否則依
/// spec.env 的 `PATH`（無則 `os.defpath`）逐目錄組候選；envp 依 spec.env 順序。不經 glibc `execvp`，故無
/// ENOEXEC→`/bin/sh` 後備（審查 96b28dd_06c61d0 M1、L2）。
struct ExecPlan {
    _argv: Vec<CString>,
    _envp: Vec<CString>,
    candidates: Vec<CString>,
    argv_ptrs: Vec<*const libc::c_char>,
    envp_ptrs: Vec<*const libc::c_char>,
}

// SAFETY: 指標只指向本結構自有、建立後不再變動的 CString 緩衝；僅在 fork 後的子行程中讀取。
unsafe impl Send for ExecPlan {}
unsafe impl Sync for ExecPlan {}

fn cstring(s: &str) -> io::Result<CString> {
    CString::new(s).map_err(|_| io::Error::new(io::ErrorKind::InvalidInput, "embedded null byte"))
}

impl ExecPlan {
    fn new(spec: &HandlerSpec) -> io::Result<Self> {
        let argv: Vec<CString> = spec
            .argv
            .iter()
            .map(|a| cstring(a))
            .collect::<io::Result<_>>()?;
        let envp: Vec<CString> = spec
            .env
            .iter()
            .map(|(k, v)| cstring(&format!("{k}={v}")))
            .collect::<io::Result<_>>()?;
        let exe = &spec.argv[0];
        let candidates: Vec<CString> = if exe.contains('/') {
            vec![cstring(exe)?]
        } else {
            let path = spec
                .env
                .iter()
                .rev()
                .find(|(k, _)| k == "PATH")
                .map_or(DEFPATH, |(_, v)| v.as_str());
            path.split(':')
                .map(|dir| {
                    if dir.is_empty() {
                        cstring(exe)
                    } else if dir.ends_with('/') {
                        cstring(&format!("{dir}{exe}"))
                    } else {
                        cstring(&format!("{dir}/{exe}"))
                    }
                })
                .collect::<io::Result<_>>()?
        };
        let mut argv_ptrs: Vec<*const libc::c_char> = argv.iter().map(|c| c.as_ptr()).collect();
        argv_ptrs.push(std::ptr::null());
        let mut envp_ptrs: Vec<*const libc::c_char> = envp.iter().map(|c| c.as_ptr()).collect();
        envp_ptrs.push(std::ptr::null());
        Ok(Self {
            _argv: argv,
            _envp: envp,
            candidates,
            argv_ptrs,
            envp_ptrs,
        })
    }

    /// 逐一 `execve`；成功不返回。回報第一個非 ENOENT／ENOTDIR 的 errno，否則最後一個（不配置記憶體）。
    fn exec(&self) -> io::Error {
        let mut saved = 0;
        let mut last = libc::ENOENT;
        for c in &self.candidates {
            // SAFETY: c／argv／envp 皆為 NUL 結尾 C 字串與 NULL 結尾指標陣列，於子行程記憶體中有效。
            unsafe {
                libc::execve(c.as_ptr(), self.argv_ptrs.as_ptr(), self.envp_ptrs.as_ptr());
            }
            last = io::Error::last_os_error()
                .raw_os_error()
                .unwrap_or(libc::ENOENT);
            if last != libc::ENOENT && last != libc::ENOTDIR && saved == 0 {
                saved = last;
            }
        }
        io::Error::from_raw_os_error(if saved != 0 { saved } else { last })
    }
}

pub(super) fn shell_argv(shell: &str) -> Vec<String> {
    vec!["/bin/sh".into(), "-c".into(), shell.into()]
}

/// Python `str(OSError)`：`[Errno N] <strerror>: '<argv[0]>'`；嵌入 NUL 為 `ValueError("embedded null byte")`。
fn spawn_error(e: io::Error, argv0: &str) -> PalError {
    if e.raw_os_error().is_none() && e.kind() == io::ErrorKind::InvalidInput {
        return PalError::plain(PalErrorKind::InvalidInput, "embedded null byte");
    }
    crate::fs_ops::with_filename(posix_io_error(e), std::path::Path::new(argv0))
}

/// `killpg(pgid, sig)`；ESRCH／EPERM 忽略（Python `except (ProcessLookupError, PermissionError)`）。
fn killpg(pgid: u32, sig: libc::c_int) {
    if pgid == 0 || pgid > libc::pid_t::MAX as u32 {
        return;
    }
    // SAFETY: killpg 只對指定 process group 送訊號，不觸及本行程記憶體。
    unsafe {
        libc::killpg(pgid as libc::pid_t, sig);
    }
}

fn exit_code(status: std::process::ExitStatus) -> i32 {
    status
        .code()
        .or_else(|| status.signal().map(|s| -s))
        .unwrap_or(-1)
}

pub(super) fn run(spec: &HandlerSpec, abort: &HandlerAbort) -> HandlerOutcome {
    let Some(argv0) = spec.argv.first() else {
        return HandlerOutcome::SpawnFailed(posix_errno(libc::EINVAL));
    };
    let max_fd = match max_open_fd() {
        Ok(n) => n,
        Err(e) => return HandlerOutcome::SpawnFailed(posix_io_error(e)),
    };
    let plan = match ExecPlan::new(spec) {
        Ok(p) => p,
        Err(e) => return HandlerOutcome::SpawnFailed(spawn_error(e, argv0)),
    };
    let mut command = Command::new(argv0);
    command
        .env_clear()
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    // SAFETY: closure 只呼叫 async-signal-safe 的 setsid／close_range／fcntl／execve，於 fork 後、exec 前執行，
    // 不配置記憶體（argv／envp／候選路徑皆於 fork 前備妥）；只「標記」CLOEXEC（不直接關閉），保留 std 回報
    // exec 失敗用的 CLOEXEC pipe。execve 全數失敗時回 Err，由 std 經該 pipe 回報 errno。
    unsafe {
        command.pre_exec(move || {
            if libc::setsid() == -1 {
                return Err(io::Error::last_os_error());
            }
            mark_inherited_fds_cloexec(max_fd)?;
            Err(plan.exec())
        });
    }
    let start = Instant::now();
    let mut child = match command.spawn() {
        Ok(c) => c,
        Err(e) => return HandlerOutcome::SpawnFailed(spawn_error(e, argv0)),
    };
    let pgid = child.id();
    if super::py_communicate_timeout_overflows(spec.timeout) {
        // Python：Popen 成功後 `communicate` 在第一次 select 前溢位（stdin 尚未寫入）；Popen 物件隨即被回收、
        // 管線關閉，子行程不被終止。此處同樣關閉三條管線、不送訊號，另起執行緒 wait 以免殭屍。
        drop(child.stdin.take());
        drop(child.stdout.take());
        drop(child.stderr.take());
        std::thread::spawn(move || {
            let _ = child.wait();
        });
        return super::communicate_overflow();
    }
    let (done_tx, done_rx) = mpsc::channel();
    let out = spawn_tail_reader(
        child.stdout.take().expect("piped stdout"),
        spec.tail_bytes,
        done_tx.clone(),
    );
    let err = spawn_tail_reader(
        child.stderr.take().expect("piped stderr"),
        spec.tail_bytes,
        done_tx,
    );
    spawn_stdin_writer(child.stdin.take().expect("piped stdin"), spec.stdin.clone());

    // 未溢位者 timeout ≤ INT_MAX ms，`start + timeout` 不會溢位；仍以 checked_add 防禦。
    let Some(deadline) = start.checked_add(spec.timeout) else {
        return super::communicate_overflow();
    };
    let mut status = None;
    let mut eof = 0usize;
    loop {
        if status.is_none() {
            status = child.try_wait().ok().flatten();
        }
        eof += done_rx.try_iter().count();
        if status.is_some() && eof >= 2 {
            break;
        }
        let now = Instant::now();
        if abort.is_triggered() || now >= deadline {
            return timed_out(child, status, pgid, out, err);
        }
        abort.wait_timeout(POLL.min(deadline - now));
    }
    let status = status.expect("loop exits only after child exit");
    HandlerOutcome::Completed {
        exit_code: exit_code(status),
        stdout_tail: out.join().unwrap_or_default(),
        stderr_tail: err.join().unwrap_or_default(),
    }
}

fn timed_out(
    mut child: std::process::Child,
    status: Option<std::process::ExitStatus>,
    pgid: u32,
    out: std::thread::JoinHandle<Vec<u8>>,
    err: std::thread::JoinHandle<Vec<u8>>,
) -> HandlerOutcome {
    killpg(pgid, libc::SIGTERM);
    let mut exited = status.is_some();
    let grace_end = Instant::now() + TERM_GRACE;
    while !exited && Instant::now() < grace_end {
        exited = matches!(child.try_wait(), Ok(Some(_)));
        if !exited {
            std::thread::sleep(POLL);
        }
    }
    if !exited {
        killpg(pgid, libc::SIGKILL);
    }
    // Python 收尾 `proc.communicate()`：讀到 EOF 並回收子行程。
    let _ = out.join();
    let _ = err.join();
    let _ = child.wait();
    HandlerOutcome::TimedOut
}
