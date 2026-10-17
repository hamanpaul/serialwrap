//! 假 `SysEnv`。

use crate::traits::{CmdOutput, CmdOutputRaw, PalError, PalErrorKind, PalResult, SysEnv};
use std::collections::BTreeMap;
use std::ffi::OsString;
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::time::Duration;

#[derive(Debug)]
pub struct FakeEnv {
    pub vars: Mutex<BTreeMap<String, String>>,
    pub exe: PathBuf,
    pub columns: Option<u16>,
    pub tty: bool,
    pub system_config_dir: String,
}

impl Default for FakeEnv {
    fn default() -> Self {
        Self {
            vars: Mutex::new(BTreeMap::new()),
            exe: PathBuf::new(),
            columns: None,
            tty: false,
            system_config_dir: "/etc/serialwrap".into(),
        }
    }
}

impl SysEnv for FakeEnv {
    fn var(&self, key: &str) -> Option<String> {
        self.vars
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .get(key)
            .cloned()
    }

    fn vars(&self) -> Vec<(String, String)> {
        self.vars
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .iter()
            .map(|(key, value)| (key.clone(), value.clone()))
            .collect()
    }

    fn system_config_dir(&self) -> String {
        self.system_config_dir.clone()
    }

    fn current_user_home(&self) -> Option<String> {
        self.var("HOME").or_else(|| self.var("USERPROFILE"))
    }

    fn user_home(&self, user: &str) -> Option<String> {
        if user.is_empty()
            || self
                .var("USER")
                .or_else(|| self.var("USERNAME"))
                .is_some_and(|current| current == user)
        {
            self.current_user_home()
        } else {
            self.var(&format!("HOME_{user}"))
        }
    }

    fn current_exe(&self) -> crate::traits::PalResult<PathBuf> {
        if self.exe.as_os_str().is_empty() {
            std::env::current_exe()
                .map_err(|error| PalError::plain(PalErrorKind::Io, error.to_string()))
        } else {
            Ok(self.exe.clone())
        }
    }

    fn realpath(&self, path: &Path) -> crate::traits::PalResult<PathBuf> {
        std::fs::canonicalize(path)
            .map_err(|error| PalError::plain(PalErrorKind::Io, error.to_string()))
    }

    fn terminal_columns(&self) -> Option<u16> {
        self.columns
    }

    fn stdout_is_terminal(&self) -> bool {
        self.tty
    }

    fn errno_name(&self, code: i32) -> Option<&'static str> {
        #[cfg(unix)]
        {
            crate::sysenv::posix::errno_name(code)
        }
        #[cfg(windows)]
        {
            crate::sysenv::windows::errno_name(code)
        }
    }

    fn pid(&self) -> u32 {
        std::process::id()
    }

    fn run(&self, _argv: &[String], _timeout: Option<Duration>) -> PalResult<CmdOutput> {
        Ok(CmdOutput {
            rc: 0,
            stdout: String::new(),
            stderr: String::new(),
        })
    }

    fn run_raw(
        &self,
        _argv: &[String],
        _env: Option<&[(OsString, OsString)]>,
        _timeout: Option<Duration>,
    ) -> PalResult<CmdOutputRaw> {
        Ok(CmdOutputRaw {
            rc: 0,
            stdout: Vec::new(),
            stderr: Vec::new(),
        })
    }

    fn which(&self, _name: &str) -> Option<PathBuf> {
        None
    }

    fn which_all(&self, _name: &str) -> Vec<PathBuf> {
        Vec::new()
    }

    fn has_systemd(&self) -> bool {
        false
    }

    fn is_wsl(&self) -> bool {
        false
    }

    fn user_in_group(&self, _group: &str) -> bool {
        false
    }

    fn user_identity(&self) -> String {
        String::new()
    }

    fn local_appdata(&self) -> Option<PathBuf> {
        None
    }
}
