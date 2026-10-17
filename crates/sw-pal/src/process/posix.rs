//! POSIX `execve` 委派。

use crate::traits::{posix_io_error, PalError, PalErrorKind, ProcessCtl};
use std::collections::BTreeMap;
use std::ffi::{CString, OsString};
use std::os::unix::ffi::OsStrExt;

#[derive(Clone, Copy, Debug, Default)]
pub struct NativeProcessCtl;

impl ProcessCtl for NativeProcessCtl {
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
}
