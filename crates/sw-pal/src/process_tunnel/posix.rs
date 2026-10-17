//! POSIX `ProcessTunnel` 實作（BC-05）。

use std::fs::{self, OpenOptions};
use std::io;
use std::os::unix::process::CommandExt;
use std::path::Path;
use std::process::{Command, Stdio};
use std::time::Duration;

use super::{read_stderr_tail, ProcGroupRef, ProcessTunnel, TunnelProc};
use crate::traits::{posix_io_error, PalError, PalErrorKind, PalResult};

#[derive(Clone, Copy, Debug, Default)]
pub struct NativeProcessTunnel;

impl ProcessTunnel for NativeProcessTunnel {
    fn spawn_detached(&self, argv: &[String], log: &Path) -> PalResult<TunnelProc> {
        let Some(program) = argv.first() else {
            return Err(PalError::plain(
                PalErrorKind::InvalidInput,
                "無法以空 argv 啟動分離行程",
            ));
        };
        let parent = log
            .parent()
            .filter(|path| !path.as_os_str().is_empty())
            .unwrap_or_else(|| Path::new("."));
        fs::create_dir_all(parent).map_err(posix_io_error)?;
        let output = OpenOptions::new()
            .create(true)
            .append(true)
            .open(log)
            .map_err(posix_io_error)?;
        let error = output.try_clone().map_err(posix_io_error)?;
        let fd_limit = max_open_fd().map_err(posix_io_error)?;

        let mut command = Command::new(program);
        command
            .args(&argv[1..])
            .stdin(Stdio::null())
            .stdout(Stdio::from(output))
            .stderr(Stdio::from(error));
        // SAFETY: 在 exec 前只建立新 session 並關閉額外 fd；不讀寫 Rust heap 或取得任何鎖。
        unsafe {
            command.pre_exec(move || {
                if libc::setsid() == -1 {
                    return Err(io::Error::last_os_error());
                }
                close_extra_fds(fd_limit)
            });
        }
        let child = command.spawn().map_err(posix_io_error)?;
        let pid = child.id();
        drop(child);

        // SAFETY: `pid` 是剛建立的子行程，getpgid 只查詢其 process group。
        let pgid = unsafe { libc::getpgid(pid as libc::pid_t) };
        if pgid == -1 {
            return Err(posix_io_error(io::Error::last_os_error()));
        }
        Ok(TunnelProc {
            pid,
            group: ProcGroupRef::Pgid(pgid),
            start_ticks: read_start_ticks(pid),
        })
    }

    fn alive(&self, p: &TunnelProc) -> bool {
        if p.pid == 0 || p.pid > libc::pid_t::MAX as u32 {
            return false;
        }
        if reap_pid(p.pid) {
            return false;
        }
        // SAFETY: kill(pid, 0) 僅查詢權限與存活狀態，不送訊號。
        if unsafe { libc::kill(p.pid as libc::pid_t, 0) } != 0 {
            return false;
        }
        if matches!(read_process_state(p.pid), Some(b'Z' | b'X' | b'x')) {
            return false;
        }
        match (p.start_ticks, read_start_ticks(p.pid)) {
            (Some(expected), Some(actual)) => expected == actual,
            _ => true,
        }
    }

    fn terminate(&self, p: &TunnelProc, grace: Duration) -> PalResult<()> {
        let ProcGroupRef::Pgid(pgid) = p.group else {
            return Err(PalError::plain(
                PalErrorKind::InvalidInput,
                "POSIX 行程通道需要 Pgid 群組識別",
            ));
        };
        if p.pid == 0 || p.pid > libc::pid_t::MAX as u32 || pgid <= 1 {
            return Err(PalError::plain(
                PalErrorKind::InvalidInput,
                "POSIX 行程通道的 pid／pgid 超出安全範圍",
            ));
        }
        // Python 基線會吞掉群組已消失或無權限的錯誤，之後仍執行有界存活輪詢。
        send_group_signal(pgid, libc::SIGTERM).map_err(posix_io_error)?;
        let started = std::time::Instant::now();
        while started.elapsed() < grace {
            reap_group(pgid);
            match group_exists(pgid) {
                Ok(false) => return Ok(()),
                Ok(true) => std::thread::sleep(Duration::from_millis(100)),
                Err(error) if error.raw_os_error() == Some(libc::EPERM) => {
                    std::thread::sleep(Duration::from_millis(100));
                }
                Err(error) => return Err(posix_io_error(error)),
            }
        }
        send_group_signal(pgid, libc::SIGKILL).map_err(posix_io_error)?;
        let reap_deadline = std::time::Instant::now() + Duration::from_secs(1);
        loop {
            reap_group(pgid);
            match group_exists(pgid) {
                Ok(false) => return Ok(()),
                Ok(true) => {}
                Err(error) if error.raw_os_error() == Some(libc::EPERM) => {}
                Err(error) => return Err(posix_io_error(error)),
            }
            if std::time::Instant::now() >= reap_deadline {
                return Err(PalError::plain(
                    PalErrorKind::TimedOut,
                    "SIGKILL 後行程群組仍未收斂",
                ));
            }
            std::thread::sleep(Duration::from_millis(10));
        }
    }

    fn stderr_tail(&self, log: &Path, max: usize) -> String {
        read_stderr_tail(log, max)
    }

    fn supports_control_master(&self) -> bool {
        true
    }
}

fn max_open_fd() -> io::Result<libc::rlim_t> {
    let mut limit = std::mem::MaybeUninit::<libc::rlimit>::uninit();
    // SAFETY: `getrlimit` 會完整寫入有效的輸出結構。
    if unsafe { libc::getrlimit(libc::RLIMIT_NOFILE, limit.as_mut_ptr()) } != 0 {
        return Err(io::Error::last_os_error());
    }
    // SAFETY: `getrlimit` 成功後已初始化 `limit`。
    let limit = unsafe { limit.assume_init() }.rlim_cur;
    if limit != libc::RLIM_INFINITY {
        return Ok(limit);
    }
    // `RLIM_INFINITY` 時在 parent 先取有限上限，避免 pre-exec 內呼叫非 async-signal-safe API。
    // SAFETY: `sysconf` 只查詢目前行程的開啟檔案上限。
    let open_max = unsafe { libc::sysconf(libc::_SC_OPEN_MAX) };
    if open_max > 0 {
        Ok(open_max as libc::rlim_t)
    } else {
        Ok(1_048_576)
    }
}

fn close_extra_fds(limit: libc::rlim_t) -> io::Result<()> {
    #[cfg(target_os = "linux")]
    {
        // CLOSE_RANGE_CLOEXEC 標記 fd 3 以上於 exec 時關閉，保留 Command::spawn 的錯誤回報 fd。
        // SAFETY: syscall 只更新子行程 fd 3 以上的 close-on-exec 旗標。
        if unsafe { libc::syscall(libc::SYS_close_range, 3u32, u32::MAX, 4u32) } == 0 {
            return Ok(());
        }
        let error = io::Error::last_os_error();
        if !matches!(
            error.raw_os_error(),
            Some(libc::ENOSYS) | Some(libc::EINVAL) | Some(libc::EPERM)
        ) {
            return Err(error);
        }
    }

    // fcntl 標記 close-on-exec，避免關閉 Rust spawn 用來回報 exec 錯誤的管線。
    let limit = limit.min(libc::c_int::MAX as libc::rlim_t);
    for fd in 3..limit {
        loop {
            // SAFETY: fork 後只查詢 fd 3 以上，不讀寫 stdio 或 Rust 狀態。
            let flags = unsafe { libc::fcntl(fd as libc::c_int, libc::F_GETFD) };
            if flags == -1 {
                let error = io::Error::last_os_error();
                if error.raw_os_error() == Some(libc::EBADF) {
                    break;
                }
                if error.kind() == io::ErrorKind::Interrupted {
                    continue;
                }
                return Err(error);
            }
            // SAFETY: 只為已存在的額外 fd 加上 FD_CLOEXEC。
            if unsafe {
                libc::fcntl(
                    fd as libc::c_int,
                    libc::F_SETFD,
                    flags | libc::FD_CLOEXEC,
                )
            } == 0
            {
                break;
            }
            let error = io::Error::last_os_error();
            if error.kind() == io::ErrorKind::Interrupted {
                continue;
            }
            return Err(error);
        }
    }
    Ok(())
}

fn reap_pid(pid: u32) -> bool {
    loop {
        let mut status = 0;
        // SAFETY: WNOHANG 僅檢查並回收指定子行程，不等待其他行程。
        let result = unsafe {
            libc::waitpid(
                pid as libc::pid_t,
                &mut status,
                libc::WNOHANG,
            )
        };
        if result == pid as libc::pid_t {
            return true;
        }
        if result == 0 {
            return false;
        }
        let error = io::Error::last_os_error();
        if error.kind() == io::ErrorKind::Interrupted {
            continue;
        }
        return false;
    }
}

fn read_process_state(pid: u32) -> Option<u8> {
    let stat = fs::read(format!("/proc/{pid}/stat")).ok()?;
    let close = stat.iter().rposition(|byte| *byte == b')')?;
    stat.get(close + 2).copied()
}

fn read_start_ticks(pid: u32) -> Option<u64> {
    let stat = fs::read(format!("/proc/{pid}/stat")).ok()?;
    let close = stat.iter().rposition(|byte| *byte == b')')?;
    let fields = stat
        .get(close + 2..)?
        .split(|byte| byte.is_ascii_whitespace());
    fields
        .filter(|field| !field.is_empty())
        .nth(19)
        .and_then(|field| std::str::from_utf8(field).ok())
        .and_then(|field| field.parse().ok())
}

fn send_group_signal(pgid: i32, signal: i32) -> io::Result<()> {
    // SAFETY: killpg 只影響本 ProcessTunnel 建立的獨立 session 群組。
    if unsafe { libc::killpg(pgid, signal) } == 0 {
        return Ok(());
    }
    let error = io::Error::last_os_error();
    if matches!(error.raw_os_error(), Some(libc::ESRCH | libc::EPERM)) {
        Ok(())
    } else {
        Err(error)
    }
}

fn group_exists(pgid: i32) -> io::Result<bool> {
    // SAFETY: signal 0 僅查詢群組，不傳送訊號。
    if unsafe { libc::killpg(pgid, 0) } == 0 {
        return Ok(true);
    }
    let error = io::Error::last_os_error();
    if error.raw_os_error() == Some(libc::ESRCH) {
        Ok(false)
    } else {
        Err(error)
    }
}

fn reap_group(pgid: i32) {
    loop {
        // SAFETY: 僅回收本 process group 中已結束的子行程，不阻塞。
        let result = unsafe { libc::waitpid(-pgid, std::ptr::null_mut(), libc::WNOHANG) };
        if result <= 0 {
            return;
        }
    }
}
