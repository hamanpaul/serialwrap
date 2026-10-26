//! 假 `ProcessCtl`，記錄委派呼叫。
#![allow(unused_variables)]

use crate::traits::{ChildHandle, PalError, PalErrorKind, PalResult, ProcessCtl};
use std::collections::VecDeque;
use std::ffi::OsString;
use std::sync::{Arc, Mutex};

#[derive(Debug, Default)]
pub struct RecordingProcess {
    pub calls: Mutex<Vec<(Vec<OsString>, Vec<(String, String)>)>>,
}

impl ProcessCtl for RecordingProcess {
    fn exec_or_wait(&self, argv: &[OsString], env_add: &[(String, String)]) -> PalError {
        self.calls
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .push((argv.to_vec(), env_add.to_vec()));
        PalError::plain(
            PalErrorKind::Unsupported,
            "recording process does not execute",
        )
    }
}

/// 假 `ProcessCtl`（P4-T4 承接）：記錄 spawn／前景呼叫，`poll`／`call_foreground` 依腳本回應。
#[derive(Debug, Default)]
pub struct FakeProcessCtl {
    spawns: Mutex<Vec<(Vec<String>, Vec<(OsString, OsString)>, bool)>>,
    poll_script: Mutex<VecDeque<Option<i64>>>,
    foreground_rc: Mutex<i64>,
    spawn_failure: Mutex<Option<PalErrorKind>>,
    next_pid: Mutex<u32>,
    spawn_events: Mutex<Vec<(u64, String)>>,
    spawn_lock_probe: Mutex<Option<std::path::PathBuf>>,
    spawn_lock_held: Mutex<Vec<bool>>,
    spawned: Arc<std::sync::atomic::AtomicUsize>,
    elevated: Mutex<Vec<(std::path::PathBuf, Vec<String>)>>,
    elevation: Mutex<Option<crate::traits::ElevatedOutcome>>,
}

impl FakeProcessCtl {
    /// P4-T19：`spawn_elevated` 的呼叫紀錄 `(exe, args)`，依呼叫順序。
    pub fn elevated_spawns(&self) -> Vec<(std::path::PathBuf, Vec<String>)> {
        self.elevated
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .clone()
    }

    /// P4-T19：之後的 `spawn_elevated` 一律回傳 `outcome`（未設定時視為 `Exited(0)`）。
    pub fn elevation_result(&self, outcome: crate::traits::ElevatedOutcome) {
        *self.elevation.lock().unwrap_or_else(|e| e.into_inner()) = Some(outcome);
    }

    pub fn new() -> Arc<Self> {
        Arc::new(Self {
            next_pid: Mutex::new(9000),
            ..Self::default()
        })
    }

    /// 依呼叫順序回傳 `spawn_daemon` 的 `(argv, env, detached)`。
    pub fn spawns(&self) -> Vec<(Vec<String>, Vec<(OsString, OsString)>, bool)> {
        self.spawns
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .clone()
    }

    /// 同 `spawns`，回傳 `(事件序號, argv0 檔名)`（`TestEnv::event_order` 的 `spawn:<argv0>` 事件）。
    pub fn spawn_events(&self) -> Vec<(u64, String)> {
        self.spawn_events
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .clone()
    }

    /// 與 `spawn_daemon` 同步遞增的計數器（供 `FakeRpcConnector::gate_on_spawn` 使用；成功的 spawn 才計）。
    pub fn spawn_counter(&self) -> Arc<std::sync::atomic::AtomicUsize> {
        Arc::clone(&self.spawned)
    }

    /// 目前為止 `spawn_daemon` 的呼叫次數（失敗的呼叫亦計）。
    pub fn spawn_count(&self) -> usize {
        self.spawns.lock().unwrap_or_else(|e| e.into_inner()).len()
    }

    /// 設定一個鎖路徑，記錄每次 daemon spawn 當下該鎖是否仍由其他持有者鎖住。
    pub fn observe_lock_at_spawn(&self, path: std::path::PathBuf) {
        *self
            .spawn_lock_probe
            .lock()
            .unwrap_or_else(|e| e.into_inner()) = Some(path);
    }

    /// 回傳每次 daemon spawn 當下觀察到的鎖持有狀態。
    pub fn spawn_lock_held(&self) -> Vec<bool> {
        self.spawn_lock_held
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .clone()
    }

    /// 之後的 `poll` 依序回傳這些結果；用盡後回 `None`（仍在執行）。
    pub fn poll_script(&self, seq: Vec<Option<i64>>) {
        *self.poll_script.lock().unwrap_or_else(|e| e.into_inner()) = seq.into();
    }

    /// `call_foreground` 的回傳 rc。
    pub fn foreground_rc(&self, rc: i64) {
        *self.foreground_rc.lock().unwrap_or_else(|e| e.into_inner()) = rc;
    }

    /// 之後的 `spawn_daemon` 一律失敗。
    pub fn spawn_fail(&self, kind: PalErrorKind) {
        *self.spawn_failure.lock().unwrap_or_else(|e| e.into_inner()) = Some(kind);
    }
}

impl ProcessCtl for FakeProcessCtl {
    fn spawn_elevated(
        &self,
        exe: &std::path::Path,
        args: &[String],
    ) -> PalResult<crate::traits::ElevatedOutcome> {
        self.elevated
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .push((exe.to_path_buf(), args.to_vec()));
        Ok(self
            .elevation
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .unwrap_or(crate::traits::ElevatedOutcome::Exited(0)))
    }

    fn exec_or_wait(&self, argv: &[OsString], env_add: &[(String, String)]) -> PalError {
        PalError::plain(PalErrorKind::Unsupported, "fake process does not exec")
    }

    fn spawn_daemon(
        &self,
        argv: &[String],
        env: &[(OsString, OsString)],
        detached: bool,
    ) -> PalResult<ChildHandle> {
        if let Some(path) = self
            .spawn_lock_probe
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .clone()
        {
            let held = crate::fakes::real_fs()
                .file_lock_exclusive(&path, false)
                .is_err();
            self.spawn_lock_held
                .lock()
                .unwrap_or_else(|e| e.into_inner())
                .push(held);
        }
        self.spawns.lock().unwrap_or_else(|e| e.into_inner()).push((
            argv.to_vec(),
            env.to_vec(),
            detached,
        ));
        let label = argv
            .first()
            .map(|a| {
                std::path::Path::new(a)
                    .file_name()
                    .map(|f| f.to_string_lossy().into_owned())
                    .unwrap_or_else(|| a.clone())
            })
            .unwrap_or_default();
        self.spawn_events
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .push((super::next_event_seq(), label));
        if let Some(kind) = *self.spawn_failure.lock().unwrap_or_else(|e| e.into_inner()) {
            return Err(PalError::plain(kind, "fake spawn failure"));
        }
        let mut next = self.next_pid.lock().unwrap_or_else(|e| e.into_inner());
        let pid = *next;
        *next += 1;
        self.spawned
            .fetch_add(1, std::sync::atomic::Ordering::SeqCst);
        Ok(ChildHandle::new(pid))
    }

    fn poll(&self, _c: &ChildHandle) -> Option<i64> {
        self.poll_script
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .pop_front()
            .flatten()
    }

    fn call_foreground(&self, _argv: &[String], _env: &[(OsString, OsString)]) -> PalResult<i64> {
        Ok(*self.foreground_rc.lock().unwrap_or_else(|e| e.into_inner()))
    }
}

/// 假 `HandlerSpawner`（P7 Task 0 凍結；Task 14／15 使用）：依序回放腳本結果並記錄每次 `run` 的規格。
#[derive(Debug, Default)]
pub struct FakeHandlerSpawner {
    pub script: Mutex<VecDeque<crate::traits::HandlerOutcome>>,
    pub calls: Mutex<Vec<crate::traits::HandlerSpec>>,
}

impl crate::traits::HandlerSpawner for FakeHandlerSpawner {
    fn shell_argv(&self, _shell: &str) -> Vec<String> {
        todo!("P7 Task 14")
    }
    fn inherited_env_keys(&self) -> &'static [&'static str] {
        todo!("P7 Task 14")
    }
    fn run(&self, _spec: &crate::traits::HandlerSpec, _abort: &crate::traits::HandlerAbort) -> crate::traits::HandlerOutcome {
        todo!("P7 Task 14")
    }
}
