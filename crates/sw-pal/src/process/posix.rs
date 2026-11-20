//! POSIX `execve` 委派。

use crate::traits::{posix_io_error, PalError, PalErrorKind, ProcessCtl};
use std::collections::BTreeMap;
use std::ffi::{CString, OsString};
use std::io;
use std::os::unix::ffi::OsStrExt;
use std::os::unix::process::{CommandExt, ExitStatusExt};
use std::process::{Command, Stdio};

#[derive(Clone, Copy, Debug, Default)]
pub struct NativeProcessCtl;

impl ProcessCtl for NativeProcessCtl {
    fn spawn_elevated(
        &self,
        _exe: &std::path::Path,
        _args: &[String],
    ) -> crate::traits::PalResult<crate::traits::ElevatedOutcome> {
        Err(crate::traits::PalError::plain(
            PalErrorKind::Unsupported,
            "POSIX 不支援提權啟動",
        ))
    }

    fn exec_or_wait(&self, argv: &[OsString], env_add: &[(String, String)]) -> PalError {
        if argv.is_empty() {
            return crate::traits::posix_errno(libc::EINVAL);
        }
        let c_argv: Result<Vec<CString>, _> = argv
            .iter()
            .map(|value| CString::new(value.as_os_str().as_bytes()))
            .collect();
        let c_argv = match c_argv {
            Ok(values) => values,
            Err(_) => return PalError::plain(PalErrorKind::InvalidInput, "embedded null byte"),
        };

        let mut environment: BTreeMap<OsString, OsString> = std::env::vars_os().collect();
        for (key, value) in env_add {
            environment.insert(OsString::from(key), OsString::from(value));
        }
        let c_env: Result<Vec<CString>, _> = environment
            .into_iter()
            .map(|(key, value)| {
                let mut bytes = key.as_os_str().as_bytes().to_vec();
                bytes.push(b'=');
                bytes.extend_from_slice(value.as_os_str().as_bytes());
                CString::new(bytes)
            })
            .collect();
        let c_env = match c_env {
            Ok(values) => values,
            Err(_) => return PalError::plain(PalErrorKind::InvalidInput, "embedded null byte"),
        };

        let mut argv_ptrs: Vec<*const libc::c_char> =
            c_argv.iter().map(|value| value.as_ptr()).collect();
        argv_ptrs.push(std::ptr::null());
        let mut env_ptrs: Vec<*const libc::c_char> =
            c_env.iter().map(|value| value.as_ptr()).collect();
        env_ptrs.push(std::ptr::null());
        // SAFETY: 所有 CString 與指標陣列在 execve 呼叫期間仍存活且以 NUL 結尾；成功時不返回。
        unsafe {
            libc::execve(c_argv[0].as_ptr(), argv_ptrs.as_ptr(), env_ptrs.as_ptr());
        }
        posix_io_error(std::io::Error::last_os_error())
    }

    fn spawn_daemon(
        &self,
        argv: &[String],
        env: &[(OsString, OsString)],
        detached: bool,
    ) -> crate::traits::PalResult<crate::traits::ChildHandle> {
        if argv.is_empty() {
            return Err(crate::traits::posix_errno(libc::EINVAL));
        }

        let mut command = Command::new(&argv[0]);
        command
            .args(&argv[1..])
            .env_clear()
            .envs(env.iter().map(|(key, value)| (key, value)));
        let max_fd = max_open_fd().map_err(process_io_error)?;
        if detached {
            command.stdout(Stdio::null()).stderr(Stdio::null());
        }
        // SAFETY: 此 closure 僅呼叫 async-signal-safe 的系統呼叫，並於 fork 後、exec 前執行。
        unsafe {
            command.pre_exec(move || {
                if detached && libc::setsid() == -1 {
                    return Err(io::Error::last_os_error());
                }
                mark_inherited_fds_cloexec(max_fd)
            });
        }

        let child = command.spawn().map_err(process_io_error)?;
        Ok(crate::traits::ChildHandle::new(child.id()))
    }

    fn poll(&self, child: &crate::traits::ChildHandle) -> Option<i64> {
        let mut cached = child
            .exit_code
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        if let Some(code) = *cached {
            return Some(code);
        }
        if child.pid == 0 || child.pid > libc::pid_t::MAX as u32 {
            return None;
        }
        let mut status = 0;
        loop {
            // SAFETY: status 指向可寫整數，WNOHANG 讓此呼叫不阻塞。
            let result =
                unsafe { libc::waitpid(child.pid as libc::pid_t, &mut status, libc::WNOHANG) };
            if result == 0 {
                return None;
            }
            if result == -1 {
                let error = std::io::Error::last_os_error();
                if error.raw_os_error() == Some(libc::EINTR) {
                    continue;
                }
                if error.raw_os_error() == Some(libc::ECHILD) {
                    *cached = Some(0);
                    return Some(0);
                }
                return None;
            }
            let code = if libc::WIFEXITED(status) {
                i64::from(libc::WEXITSTATUS(status))
            } else if libc::WIFSIGNALED(status) {
                -i64::from(libc::WTERMSIG(status))
            } else {
                return None;
            };
            *cached = Some(code);
            return Some(code);
        }
    }

    fn call_foreground(
        &self,
        argv: &[String],
        env: &[(OsString, OsString)],
    ) -> crate::traits::PalResult<i64> {
        if argv.is_empty() {
            return Err(crate::traits::posix_errno(libc::EINVAL));
        }
        let status = Command::new(&argv[0])
            .args(&argv[1..])
            .env_clear()
            .envs(env.iter().map(|(key, value)| (key, value)))
            .status()
            .map_err(process_io_error)?;
        Ok(status
            .code()
            .map(i64::from)
            .or_else(|| status.signal().map(|signal| -i64::from(signal)))
            .unwrap_or(-1))
    }
}

pub(super) fn max_open_fd() -> io::Result<libc::c_int> {
    let mut limit = libc::rlimit {
        rlim_cur: 0,
        rlim_max: 0,
    };
    // SAFETY: limit 指向可寫且大小正確的 rlimit 結構。
    if unsafe { libc::getrlimit(libc::RLIMIT_NOFILE, &mut limit) } == -1 {
        return Err(io::Error::last_os_error());
    }
    Ok(limit.rlim_cur.min(libc::c_int::MAX as libc::rlim_t) as libc::c_int)
}

pub(super) fn mark_inherited_fds_cloexec(max_fd: libc::c_int) -> io::Result<()> {
    #[cfg(target_os = "linux")]
    {
        const CLOSE_RANGE_CLOEXEC: libc::c_uint = 1 << 2;
        // SAFETY: close_range 僅替 fd 3 以上設定 close-on-exec，不關閉標準串流。
        let result =
            unsafe { libc::syscall(libc::SYS_close_range, 3u32, u32::MAX, CLOSE_RANGE_CLOEXEC) };
        if result == 0 {
            return Ok(());
        }
    }

    for fd in 3..max_fd {
        // SAFETY: fcntl 只查詢目前行程中指定 fd 的 descriptor flags。
        let flags = unsafe { libc::fcntl(fd, libc::F_GETFD) };
        if flags == -1 {
            let error = io::Error::last_os_error();
            if error.raw_os_error() == Some(libc::EBADF) {
                continue;
            }
            return Err(error);
        }
        // SAFETY: 保留原有 flags，僅增加 FD_CLOEXEC。
        if unsafe { libc::fcntl(fd, libc::F_SETFD, flags | libc::FD_CLOEXEC) } == -1 {
            return Err(io::Error::last_os_error());
        }
    }
    Ok(())
}

fn process_io_error(error: std::io::Error) -> PalError {
    if error.kind() == std::io::ErrorKind::InvalidInput {
        PalError::plain(PalErrorKind::InvalidInput, error.to_string())
    } else {
        posix_io_error(error)
    }
}
