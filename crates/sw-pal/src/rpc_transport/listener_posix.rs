//! POSIX RPC listener（P5-T17）：AF_UNIX socket 與 TCP loopback。
//!
//! UDS bind 順序逐字照 Python 基線 `JsonRpcUnixServer.start`／`_secure_socket`（SC-08 撤回，OD-14 #3）：
//! 既有檔 unlink → bind（不切換 umask）→ `chmod(path, mode)`（失敗忽略）→ `group` 有值時 chgrp
//! （`OSError`／查無群組皆忽略）；`ensure_dir_mode` 為 `None` 時不建立、不 chmod 父目錄。

use crate::traits::{
    EndpointSpec, ListenOptions, PalError, PalErrorKind, PalResult, RpcListener, RpcServerStream,
    posix_io_error,
};
use std::ffi::CString;
use std::net::{TcpListener, ToSocketAddrs};
use std::os::fd::{AsRawFd, RawFd};
use std::os::unix::ffi::OsStrExt;
use std::os::unix::fs::{DirBuilderExt, PermissionsExt};
use std::os::unix::net::UnixListener;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

use super::{SERVER_WRITE_TIMEOUT, check_loopback_host, timeout_ms};

pub(super) fn bind(ep: &EndpointSpec, opts: &ListenOptions) -> PalResult<Box<dyn RpcListener>> {
    match ep {
        EndpointSpec::Unix(path) => bind_unix(Path::new(path), opts),
        EndpointSpec::Tcp { host, port } => bind_tcp(host, *port),
    }
}

fn bind_unix(path: &Path, opts: &ListenOptions) -> PalResult<Box<dyn RpcListener>> {
    if let Some(mode) = opts.ensure_dir_mode {
        if let Some(parent) = path.parent().filter(|p| !p.as_os_str().is_empty()) {
            if !parent.exists() {
                std::fs::DirBuilder::new()
                    .recursive(true)
                    .mode(mode)
                    .create(parent)
                    .map_err(posix_io_error)?;
            }
        }
    }
    // Python `os.path.exists`（跟隨 symlink）為真才 `os.unlink`。
    if std::fs::metadata(path).is_ok() {
        std::fs::remove_file(path).map_err(posix_io_error)?;
    }
    let listener = UnixListener::bind(path).map_err(posix_io_error)?;
    listener.set_nonblocking(true).map_err(posix_io_error)?;
    // `_secure_socket`：chmod 失敗忽略。
    let _ = std::fs::set_permissions(path, std::fs::Permissions::from_mode(opts.mode));
    if let Some(group) = opts
        .group
        .as_deref()
        .map(str::trim)
        .filter(|g| !g.is_empty())
    {
        chgrp_ignore_errors(path, group);
    }
    Ok(Box::new(UnixRpcListener {
        listener: Some(listener),
        path: path.to_path_buf(),
    }))
}

/// `shutil.chown(path, group=g)`：以群組名稱查 gid（查無 → LookupError），`chown(path, -1, gid)`；錯誤一律忽略。
fn chgrp_ignore_errors(path: &Path, group: &str) {
    let Some(gid) = lookup_gid(group) else {
        return;
    };
    let Ok(c_path) = CString::new(path.as_os_str().as_bytes()) else {
        return;
    };
    // SAFETY: c_path 為有效 C 字串；uid -1 表示不變更擁有者。
    unsafe {
        libc::chown(c_path.as_ptr(), libc::uid_t::MAX, gid);
    }
}

fn lookup_gid(name: &str) -> Option<libc::gid_t> {
    let c_name = CString::new(name).ok()?;
    let mut buf = vec![0 as libc::c_char; 16 * 1024];
    loop {
        // SAFETY: group 為輸出結構；buf 為此呼叫專屬緩衝區；result 指向 group 或 null。
        let mut grp: libc::group = unsafe { std::mem::zeroed() };
        let mut result: *mut libc::group = std::ptr::null_mut();
        let rc = unsafe {
            libc::getgrnam_r(
                c_name.as_ptr(),
                &mut grp,
                buf.as_mut_ptr(),
                buf.len(),
                &mut result,
            )
        };
        if rc == libc::ERANGE && buf.len() < 1024 * 1024 {
            buf.resize(buf.len() * 2, 0);
            continue;
        }
        if rc != 0 || result.is_null() {
            return None;
        }
        return Some(grp.gr_gid);
    }
}

fn bind_tcp(host: &str, port: u16) -> PalResult<Box<dyn RpcListener>> {
    check_loopback_host(host)?;
    let addresses: Vec<_> = (host, port)
        .to_socket_addrs()
        .map_err(posix_io_error)?
        .collect();
    if addresses.is_empty() {
        return Err(PalError::plain(
            PalErrorKind::NotFound,
            format!("no address for {host}"),
        ));
    }
    // asyncio `start_server` 對每個解析位址各綁一個 socket；std 在 POSIX 預設 SO_REUSEADDR。
    let mut listeners = Vec::with_capacity(addresses.len());
    for address in addresses {
        let listener = TcpListener::bind(address).map_err(posix_io_error)?;
        listener.set_nonblocking(true).map_err(posix_io_error)?;
        listeners.push(listener);
    }
    Ok(Box::new(TcpRpcListener { listeners }))
}

/// 以 poll 等待任一 fd 可讀；回傳可讀 fd 的索引。
fn poll_readable(fds: &[RawFd], timeout: Duration) -> PalResult<Option<usize>> {
    let deadline = Instant::now() + timeout;
    let mut pfds: Vec<libc::pollfd> = fds
        .iter()
        .map(|&fd| libc::pollfd {
            fd,
            events: libc::POLLIN,
            revents: 0,
        })
        .collect();
    loop {
        let remaining = deadline.saturating_duration_since(Instant::now());
        for p in &mut pfds {
            p.revents = 0;
        }
        // SAFETY: pfds 為有效的 pollfd 陣列，長度與傳入一致。
        let n = unsafe {
            libc::poll(
                pfds.as_mut_ptr(),
                pfds.len() as libc::nfds_t,
                timeout_ms(remaining),
            )
        };
        if n < 0 {
            let error = std::io::Error::last_os_error();
            if error.raw_os_error() == Some(libc::EINTR) {
                continue;
            }
            return Err(posix_io_error(error));
        }
        if n == 0 {
            return Ok(None);
        }
        return Ok(pfds.iter().position(|p| p.revents != 0));
    }
}

fn is_transient_accept_error(error: &std::io::Error) -> bool {
    matches!(
        error.raw_os_error(),
        Some(libc::EAGAIN) | Some(libc::EINTR) | Some(libc::ECONNABORTED) | Some(libc::EPROTO)
    )
}

struct UnixRpcListener {
    listener: Option<UnixListener>,
    path: PathBuf,
}

impl RpcListener for UnixRpcListener {
    fn accept(&mut self, timeout: Duration) -> PalResult<Option<Box<dyn RpcServerStream>>> {
        let Some(listener) = self.listener.as_ref() else {
            return Ok(None);
        };
        if poll_readable(&[listener.as_raw_fd()], timeout)?.is_none() {
            return Ok(None);
        }
        match listener.accept() {
            Ok((stream, _)) => {
                stream.set_nonblocking(false).map_err(posix_io_error)?;
                stream
                    .set_write_timeout(Some(SERVER_WRITE_TIMEOUT))
                    .map_err(posix_io_error)?;
                Ok(Some(Box::new(stream)))
            }
            Err(error) if is_transient_accept_error(&error) => Ok(None),
            Err(error) => Err(posix_io_error(error)),
        }
    }

    fn close(mut self: Box<Self>) {
        drop(self.listener.take());
        // Python `stop()`：`os.path.exists` 為真才 `os.unlink`。
        if std::fs::metadata(&self.path).is_ok() {
            let _ = std::fs::remove_file(&self.path);
        }
    }
}

struct TcpRpcListener {
    listeners: Vec<TcpListener>,
}

impl RpcListener for TcpRpcListener {
    fn accept(&mut self, timeout: Duration) -> PalResult<Option<Box<dyn RpcServerStream>>> {
        let fds: Vec<RawFd> = self.listeners.iter().map(|l| l.as_raw_fd()).collect();
        let Some(index) = poll_readable(&fds, timeout)? else {
            return Ok(None);
        };
        match self.listeners[index].accept() {
            Ok((stream, _)) => {
                stream.set_nonblocking(false).map_err(posix_io_error)?;
                stream
                    .set_write_timeout(Some(SERVER_WRITE_TIMEOUT))
                    .map_err(posix_io_error)?;
                Ok(Some(Box::new(stream)))
            }
            Err(error) if is_transient_accept_error(&error) => Ok(None),
            Err(error) => Err(posix_io_error(error)),
        }
    }

    fn close(self: Box<Self>) {}
}
