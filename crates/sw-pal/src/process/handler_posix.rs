//! POSIX event handler 執行（P7 Task 14，SC-26；Python `dispatcher._run_fire` 的 `subprocess.Popen` 等價）：
//! `start_new_session=True`（setsid）、`close_fds=True`（fd≥3 標 CLOEXEC）、`env` 只放 spec、stdio 三條 pipe；
//! `communicate(stdin, timeout)`：子行程結束且 stdout／stderr 皆 EOF 才算完成；逾時 `killpg(SIGTERM)` → 等 1.0 s →
//! `killpg(SIGKILL)` → 讀完剩餘輸出。

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
    let mut command = Command::new(argv0);
    command
        .args(&spec.argv[1..])
        .env_clear()
        .envs(spec.env.iter().map(|(k, v)| (k, v)))
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    // SAFETY: closure 只呼叫 async-signal-safe 的 setsid／close_range／fcntl，於 fork 後、exec 前執行；
    // 只「標記」CLOEXEC（不直接關閉），保留 std 回報 exec 失敗用的 CLOEXEC pipe。
    unsafe {
        command.pre_exec(move || {
            if libc::setsid() == -1 {
                return Err(io::Error::last_os_error());
            }
            mark_inherited_fds_cloexec(max_fd)
        });
    }
    let start = Instant::now();
    let mut child = match command.spawn() {
        Ok(c) => c,
        Err(e) => return HandlerOutcome::SpawnFailed(spawn_error(e, argv0)),
    };
    let pgid = child.id();
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

    let deadline = start + spec.timeout;
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
