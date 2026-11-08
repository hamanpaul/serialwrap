//! 假 `ProcessTunnel`（feature `test-fakes`）：記錄 spawn／terminate，行程存活以表格控制。

use std::collections::{BTreeMap, BTreeSet};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use crate::process_tunnel::{ProcGroupRef, ProcessTunnel, TunnelProc};
use crate::traits::{PalError, PalErrorKind, PalResult};

#[derive(Debug)]
pub struct FakeTunnel {
    supports_cm: bool,
    spawned: Mutex<Vec<(Vec<String>, PathBuf)>>,
    alive: Mutex<BTreeMap<u32, bool>>,
    terminated: Mutex<Vec<u32>>,
    tail: Mutex<String>,
    terminate_fails: Mutex<BTreeSet<u32>>,
    die_on_spawn: Mutex<bool>,
    next_pid: Mutex<u32>,
}

impl FakeTunnel {
    pub fn new(supports_cm: bool) -> Arc<Self> {
        Arc::new(Self {
            supports_cm,
            spawned: Mutex::new(Vec::new()),
            alive: Mutex::new(BTreeMap::new()),
            terminated: Mutex::new(Vec::new()),
            tail: Mutex::new(String::new()),
            terminate_fails: Mutex::new(BTreeSet::new()),
            die_on_spawn: Mutex::new(false),
            next_pid: Mutex::new(4000),
        })
    }

    /// 依呼叫順序回傳 `(argv, log)`。
    pub fn spawned(&self) -> Vec<(Vec<String>, PathBuf)> {
        self.spawned
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .clone()
    }

    pub fn set_alive(&self, pid: u32, alive: bool) {
        self.alive
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .insert(pid, alive);
    }

    /// 依呼叫順序回傳 `terminate` 過的 pid。
    pub fn terminated(&self) -> Vec<u32> {
        self.terminated
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .clone()
    }

    pub fn set_tail(&self, s: &str) {
        *self.tail.lock().unwrap_or_else(|e| e.into_inner()) = s.to_string();
    }

    pub fn terminate_fails(&self, pid: u32) {
        self.terminate_fails
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .insert(pid);
    }

    /// 之後 spawn 的行程於第一次 alive 檢查即結束。
    pub fn die_on_spawn(&self) {
        *self.die_on_spawn.lock().unwrap_or_else(|e| e.into_inner()) = true;
    }
}

impl ProcessTunnel for FakeTunnel {
    fn spawn_detached(&self, argv: &[String], log: &Path) -> PalResult<TunnelProc> {
        self.spawned
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .push((argv.to_vec(), log.to_path_buf()));
        let pid = {
            let mut next = self.next_pid.lock().unwrap_or_else(|e| e.into_inner());
            let pid = *next;
            *next += 1;
            pid
        };
        let dies = *self.die_on_spawn.lock().unwrap_or_else(|e| e.into_inner());
        self.set_alive(pid, !dies);
        Ok(TunnelProc {
            pid,
            group: ProcGroupRef::Pgid(pid as i32),
            start_ticks: Some(u64::from(pid) * 10),
        })
    }

    fn alive(&self, p: &TunnelProc) -> bool {
        self.alive
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .get(&p.pid)
            .copied()
            .unwrap_or(false)
    }

    fn terminate(&self, p: &TunnelProc, _grace: Duration) -> PalResult<()> {
        if self
            .terminate_fails
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .contains(&p.pid)
        {
            return Err(PalError::plain(
                PalErrorKind::AccessDenied,
                "fake terminate failure",
            ));
        }
        self.terminated
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .push(p.pid);
        self.set_alive(p.pid, false);
        Ok(())
    }

    fn stderr_tail(&self, _log: &Path, max: usize) -> String {
        let tail = self.tail.lock().unwrap_or_else(|e| e.into_inner()).clone();
        tail.chars()
            .rev()
            .take(max)
            .collect::<Vec<_>>()
            .into_iter()
            .rev()
            .collect()
    }

    fn supports_control_master(&self) -> bool {
        self.supports_cm
    }
}
