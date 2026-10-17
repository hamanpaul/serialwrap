//! 假 `SysEnv`（feature `test-fakes`）：記錄每次 `run`／`run_raw` 的 argv、環境與逾時，並依腳本回應。
//!
//! 未設定腳本的 argv 回 `rc=0`、空輸出（等同 Python FakeEffects）。`command_seq` 的同一 argv 依序回應，
//! 用盡後重複最後一筆。`SysEnv` 的其餘環境查詢委派給 `FakeEnv`。

use std::collections::{BTreeMap, VecDeque};
use std::ffi::OsString;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use crate::fakes::env::FakeEnv;
use crate::traits::{CmdOutput, CmdOutputRaw, PalError, PalErrorKind, PalResult, SysEnv};

#[derive(Debug, Clone)]
enum Scripted {
    Out { rc: i32, stdout: Vec<u8>, stderr: Vec<u8> },
    Fail { kind: PalErrorKind, message: String },
}

type Script = Vec<(Vec<String>, VecDeque<Scripted>)>;

#[derive(Debug)]
pub struct FakeSysEnv {
    inner: FakeEnv,
    script: Mutex<Script>,
    user: String,
    passwd_home: Option<String>,
    systemd: bool,
    wsl: bool,
    groups: Vec<String>,
    which: BTreeMap<String, Vec<PathBuf>>,
    local_appdata: Option<PathBuf>,
    calls: Mutex<Vec<(u64, Vec<String>)>>,
    call_envs: Mutex<Vec<Option<Vec<(OsString, OsString)>>>>,
    timeouts: Mutex<Vec<Option<Duration>>>,
}

#[derive(Debug, Default)]
pub struct FakeSysEnvBuilder {
    inner: FakeEnv,
    script: Script,
    user: String,
    passwd_home: Option<String>,
    systemd: bool,
    wsl: bool,
    groups: Vec<String>,
    which: BTreeMap<String, Vec<PathBuf>>,
    local_appdata: Option<PathBuf>,
}

fn owned_argv(argv: &[&str]) -> Vec<String> {
    argv.iter().map(|s| s.to_string()).collect()
}

fn push_script(script: &mut Script, argv: Vec<String>, step: Scripted) {
    if let Some((_, queue)) = script.iter_mut().find(|(key, _)| *key == argv) {
        queue.push_back(step);
    } else {
        script.push((argv, VecDeque::from(vec![step])));
    }
}

impl FakeSysEnv {
    pub fn builder() -> FakeSysEnvBuilder {
        FakeSysEnvBuilder::default()
    }

    /// 依呼叫順序回傳所有 `run`／`run_raw` 的 argv。
    pub fn calls(&self) -> Vec<Vec<String>> {
        self.calls
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .iter()
            .map(|(_, argv)| argv.clone())
            .collect()
    }

    /// 同 `calls`，附事件序號（`fakes::next_event_seq`）。
    pub fn call_events(&self) -> Vec<(u64, Vec<String>)> {
        self.calls.lock().unwrap_or_else(|e| e.into_inner()).clone()
    }

    /// 每次 `run_raw` 的環境覆寫（`run` 恆為 `None`）。
    pub fn call_envs(&self) -> Vec<Option<Vec<(OsString, OsString)>>> {
        self.call_envs.lock().unwrap_or_else(|e| e.into_inner()).clone()
    }

    /// 每次呼叫的逾時參數。
    pub fn timeouts(&self) -> Vec<Option<Duration>> {
        self.timeouts.lock().unwrap_or_else(|e| e.into_inner()).clone()
    }

    fn respond(&self, argv: &[String]) -> Option<Scripted> {
        let mut script = self.script.lock().unwrap_or_else(|e| e.into_inner());
        let (_, queue) = script.iter_mut().find(|(key, _)| key.as_slice() == argv)?;
        if queue.len() > 1 {
            queue.pop_front()
        } else {
            queue.front().cloned()
        }
    }

    fn record(&self, argv: &[String], env: Option<&[(OsString, OsString)]>, timeout: Option<Duration>) {
        self.calls
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .push((super::next_event_seq(), argv.to_vec()));
        self.call_envs
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .push(env.map(<[_]>::to_vec));
        self.timeouts
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .push(timeout);
    }
}

impl FakeSysEnvBuilder {
    pub fn var(mut self, k: &str, v: &str) -> Self {
        self.inner
            .vars
            .get_mut()
            .unwrap_or_else(|e| e.into_inner())
            .insert(k.to_string(), v.to_string());
        self
    }

    /// 設定 `HOME`（未指定 `passwd_home` 時也作為 `current_user_home`）。
    pub fn home(self, p: &Path) -> Self {
        self.var("HOME", &p.display().to_string())
    }

    /// 設定與 `HOME` 不同的 passwd 家目錄，用於驗證路徑優先序。
    pub fn passwd_home(mut self, p: &Path) -> Self {
        self.passwd_home = Some(p.display().to_string());
        self
    }

    pub fn systemd(mut self, b: bool) -> Self {
        self.systemd = b;
        self
    }

    pub fn wsl(mut self, b: bool) -> Self {
        self.wsl = b;
        self
    }

    pub fn in_group(mut self, g: &str) -> Self {
        self.groups.push(g.to_string());
        self
    }

    pub fn which(self, n: &str, p: &str) -> Self {
        self.which_all(n, &[p])
    }

    pub fn which_all(mut self, n: &str, ps: &[&str]) -> Self {
        self.which
            .insert(n.to_string(), ps.iter().map(PathBuf::from).collect());
        self
    }

    /// 覆寫 `system_config_dir`（預設 `/etc/serialwrap`；測試須指向隔離目錄，SC-09）。
    pub fn system_config_dir(mut self, p: &Path) -> Self {
        self.inner.system_config_dir = p.display().to_string();
        self
    }

    pub fn user_identity(mut self, s: &str) -> Self {
        self.user = s.to_string();
        self
    }

    pub fn local_appdata(mut self, p: &Path) -> Self {
        self.local_appdata = Some(p.to_path_buf());
        self
    }

    pub fn current_exe(mut self, p: &Path) -> Self {
        self.inner.exe = p.to_path_buf();
        self
    }

    /// 指定 argv（完整、逐字比對）的回應（stdout／stderr 以 UTF-8 文字給定）。
    pub fn command(self, argv: &[&str], rc: i32, out: &str, err: &str) -> Self {
        self.command_raw(argv, rc, out.as_bytes(), err.as_bytes())
    }

    /// 同 `command`，但 stdout／stderr 為原始 bytes。
    pub fn command_raw(mut self, argv: &[&str], rc: i32, out: &[u8], err: &[u8]) -> Self {
        push_script(
            &mut self.script,
            owned_argv(argv),
            Scripted::Out {
                rc,
                stdout: out.to_vec(),
                stderr: err.to_vec(),
            },
        );
        self
    }

    /// 同一 argv 依序回應；用盡後重複最後一筆。
    pub fn command_seq(mut self, argv: &[&str], seq: &[(i32, &str, &str)]) -> Self {
        for (rc, out, err) in seq {
            push_script(
                &mut self.script,
                owned_argv(argv),
                Scripted::Out {
                    rc: *rc,
                    stdout: out.as_bytes().to_vec(),
                    stderr: err.as_bytes().to_vec(),
                },
            );
        }
        self
    }

    /// 指定 argv 的 `run` 直接回 `PalError`（例如執行檔不存在）。
    pub fn command_err(mut self, argv: &[&str], kind: PalErrorKind, py_message: &str) -> Self {
        push_script(
            &mut self.script,
            owned_argv(argv),
            Scripted::Fail {
                kind,
                message: py_message.to_string(),
            },
        );
        self
    }

    pub fn build(self) -> Arc<FakeSysEnv> {
        Arc::new(FakeSysEnv {
            inner: self.inner,
            script: Mutex::new(self.script.into_iter().map(|(k, v)| (k, v.into())).collect()),
            user: self.user,
            passwd_home: self.passwd_home,
            systemd: self.systemd,
            wsl: self.wsl,
            groups: self.groups,
            which: self.which,
            local_appdata: self.local_appdata,
            calls: Mutex::new(Vec::new()),
            call_envs: Mutex::new(Vec::new()),
            timeouts: Mutex::new(Vec::new()),
        })
    }
}

impl SysEnv for FakeSysEnv {
    fn var(&self, k: &str) -> Option<String> {
        self.inner.var(k)
    }
    fn vars(&self) -> Vec<(String, String)> {
        self.inner.vars()
    }
    fn system_config_dir(&self) -> String {
        self.inner.system_config_dir()
    }
    fn current_user_home(&self) -> Option<String> {
        self.passwd_home
            .clone()
            .or_else(|| self.inner.current_user_home())
    }
    fn user_home(&self, user: &str) -> Option<String> {
        self.inner.user_home(user)
    }
    fn current_exe(&self) -> PalResult<PathBuf> {
        self.inner.current_exe()
    }
    fn realpath(&self, p: &Path) -> PalResult<PathBuf> {
        self.inner.realpath(p)
    }
    fn terminal_columns(&self) -> Option<u16> {
        self.inner.terminal_columns()
    }
    fn stdout_is_terminal(&self) -> bool {
        self.inner.stdout_is_terminal()
    }
    fn errno_name(&self, code: i32) -> Option<&'static str> {
        self.inner.errno_name(code)
    }
    fn pid(&self) -> u32 {
        self.inner.pid()
    }

    fn run(&self, argv: &[String], timeout: Option<Duration>) -> PalResult<CmdOutput> {
        self.record(argv, None, timeout);
        match self.respond(argv) {
            Some(Scripted::Out { rc, stdout, stderr }) => Ok(CmdOutput {
                rc,
                stdout: String::from_utf8_lossy(&stdout).into_owned(),
                stderr: String::from_utf8_lossy(&stderr).into_owned(),
            }),
            Some(Scripted::Fail { kind, message }) => Err(PalError::plain(kind, message)),
            None => Ok(CmdOutput {
                rc: 0,
                stdout: String::new(),
                stderr: String::new(),
            }),
        }
    }

    fn run_raw(
        &self,
        argv: &[String],
        env: Option<&[(OsString, OsString)]>,
        timeout: Option<Duration>,
    ) -> PalResult<CmdOutputRaw> {
        self.record(argv, env, timeout);
        match self.respond(argv) {
            Some(Scripted::Out { rc, stdout, stderr }) => Ok(CmdOutputRaw { rc, stdout, stderr }),
            Some(Scripted::Fail { kind, message }) => Err(PalError::plain(kind, message)),
            None => Ok(CmdOutputRaw {
                rc: 0,
                stdout: Vec::new(),
                stderr: Vec::new(),
            }),
        }
    }

    fn which(&self, name: &str) -> Option<PathBuf> {
        self.which.get(name).and_then(|v| v.first().cloned())
    }

    fn which_all(&self, name: &str) -> Vec<PathBuf> {
        self.which.get(name).cloned().unwrap_or_default()
    }

    fn has_systemd(&self) -> bool {
        self.systemd
    }

    fn is_wsl(&self) -> bool {
        self.wsl
    }

    fn user_in_group(&self, g: &str) -> bool {
        self.groups.iter().any(|x| x == g)
    }

    fn user_identity(&self) -> String {
        self.user.clone()
    }

    fn local_appdata(&self) -> Option<PathBuf> {
        self.local_appdata.clone()
    }
}
