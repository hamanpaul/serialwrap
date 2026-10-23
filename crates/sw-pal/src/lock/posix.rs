//! POSIX `flock` 實例鎖與 Unix socket 存活探測。

use std::{
    fs::{self, File, OpenOptions},
    io::{self, Write},
    os::fd::{AsRawFd, FromRawFd, OwnedFd},
    os::unix::ffi::OsStrExt,
    os::unix::fs::OpenOptionsExt,
    path::Path,
    time::{Duration, Instant},
};

use crate::{
    lock::{InstanceLockError, InstanceLockFactory, InstanceLockGuard},
    path_norm::endpoint_path_for_io,
    traits::{posix_io_error, EndpointSpec},
};

const SOCKET_PROBE_TIMEOUT: Duration = Duration::from_millis(200);

/// POSIX Tier 1 實例鎖。
pub struct PosixInstanceLock;

impl InstanceLockFactory for PosixInstanceLock {
    fn acquire(
        &self,
        lock_path: &Path,
        endpoint: &EndpointSpec,
    ) -> Result<Box<dyn InstanceLockGuard>, InstanceLockError> {
        let lock_path = endpoint_path_for_io(lock_path)
            .map_err(|error| InstanceLockError::Io(posix_io_error(error)))?;
        if let Some(parent) = lock_path.parent() {
            fs::create_dir_all(parent)
                .map_err(|error| InstanceLockError::Io(posix_io_error(error)))?;
        }

        let mut file = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .mode(0o600)
            .custom_flags(libc::O_CLOEXEC)
            .open(&lock_path)
            .map_err(|error| InstanceLockError::Io(posix_io_error(error)))?;

        let rc = unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) };
        if rc != 0 {
            let error = io::Error::last_os_error();
            if error
                .raw_os_error()
                .is_some_and(|code| code == libc::EWOULDBLOCK || code == libc::EAGAIN)
            {
                return Err(InstanceLockError::AlreadyRunning);
            }
            return Err(InstanceLockError::Io(posix_io_error(error)));
        }

        if let EndpointSpec::Unix(socket_path) = endpoint {
            let socket_path = endpoint_path_for_io(Path::new(socket_path))
                .map_err(|error| InstanceLockError::Io(posix_io_error(error)))?;
            match fs::metadata(&socket_path) {
                Ok(_) => match probe_unix_socket(&socket_path) {
                    Ok(true) => return Err(InstanceLockError::AlreadyRunning),
                    Ok(false) => {}
                    Err(error)
                        if error.raw_os_error().is_some_and(|code| {
                            code == libc::ENOENT || code == libc::ECONNREFUSED
                        }) =>
                    {
                        let _ = fs::remove_file(&socket_path);
                    }
                    Err(_) => return Err(InstanceLockError::SocketUnavailable),
                },
                Err(error)
                    if error
                        .raw_os_error()
                        .is_some_and(|code| code == libc::EACCES) =>
                {
                    return Err(InstanceLockError::SocketUnavailable);
                }
                Err(_) => {}
            }
        }

        file.set_len(0)
            .map_err(|error| InstanceLockError::Io(posix_io_error(error)))?;
        file.write_all(format!("{}\n", std::process::id()).as_bytes())
            .map_err(|error| InstanceLockError::Io(posix_io_error(error)))?;
        file.sync_all()
            .map_err(|error| InstanceLockError::Io(posix_io_error(error)))?;

        Ok(Box::new(PosixInstanceLockGuard { file: Some(file) }))
    }
}

struct PosixInstanceLockGuard {
    file: Option<File>,
}

impl InstanceLockGuard for PosixInstanceLockGuard {
    fn release(mut self: Box<Self>) {
        if let Some(file) = self.file.take() {
            unsafe {
                libc::flock(file.as_raw_fd(), libc::LOCK_UN);
            }
            drop(file);
        }
    }
}

fn probe_unix_socket(path: &Path) -> io::Result<bool> {
    let path_bytes = path.as_os_str().as_bytes();
    let mut address: libc::sockaddr_un = unsafe { std::mem::zeroed() };
    if path_bytes.len() >= address.sun_path.len() {
        return Err(io::Error::from_raw_os_error(libc::ENAMETOOLONG));
    }
    if path_bytes.contains(&0) {
        return Err(io::Error::from_raw_os_error(libc::EINVAL));
    }

    address.sun_family = libc::AF_UNIX as libc::sa_family_t;
    for (destination, source) in address.sun_path.iter_mut().zip(path_bytes.iter().copied()) {
        *destination = source as libc::c_char;
    }
    let address_len = (std::mem::offset_of!(libc::sockaddr_un, sun_path) + path_bytes.len() + 1)
        as libc::socklen_t;

    let raw_fd = unsafe {
        libc::socket(
            libc::AF_UNIX,
            libc::SOCK_STREAM | libc::SOCK_CLOEXEC | libc::SOCK_NONBLOCK,
            0,
        )
    };
    if raw_fd < 0 {
        return Err(io::Error::last_os_error());
    }
    let socket = unsafe { OwnedFd::from_raw_fd(raw_fd) };
    let rc = unsafe {
        libc::connect(
            socket.as_raw_fd(),
            (&address as *const libc::sockaddr_un).cast::<libc::sockaddr>(),
            address_len,
        )
    };
    if rc == 0 {
        return Ok(true);
    }

    let error = io::Error::last_os_error();
    match error.raw_os_error() {
        Some(code)
            if code == libc::EINPROGRESS
                || code == libc::EALREADY
                || code == libc::EAGAIN
                || code == libc::EWOULDBLOCK =>
        {
            wait_for_connect(socket.as_raw_fd(), SOCKET_PROBE_TIMEOUT)
        }
        _ => Err(error),
    }
}

fn wait_for_connect(fd: libc::c_int, timeout: Duration) -> io::Result<bool> {
    let deadline = Instant::now() + timeout;
    loop {
        let remaining = deadline.saturating_duration_since(Instant::now());
        let millis = remaining.as_millis().min(i32::MAX as u128) as i32;
        let mut descriptor = libc::pollfd {
            fd,
            events: libc::POLLOUT | libc::POLLERR | libc::POLLHUP,
            revents: 0,
        };
        let rc = unsafe { libc::poll(&mut descriptor, 1, millis) };
        if rc == 0 {
            return Err(io::Error::from_raw_os_error(libc::ETIMEDOUT));
        }
        if rc < 0 {
            let error = io::Error::last_os_error();
            if error.kind() == io::ErrorKind::Interrupted {
                if Instant::now() < deadline {
                    continue;
                }
                return Err(io::Error::from_raw_os_error(libc::ETIMEDOUT));
            }
            return Err(error);
        }

        let mut socket_error: libc::c_int = 0;
        let mut error_len = std::mem::size_of_val(&socket_error) as libc::socklen_t;
        let getsockopt_rc = unsafe {
            libc::getsockopt(
                fd,
                libc::SOL_SOCKET,
                libc::SO_ERROR,
                (&mut socket_error as *mut libc::c_int).cast(),
                &mut error_len,
            )
        };
        if getsockopt_rc != 0 {
            return Err(io::Error::last_os_error());
        }
        return if socket_error == 0 {
            Ok(true)
        } else {
            Err(io::Error::from_raw_os_error(socket_error))
        };
    }
}
