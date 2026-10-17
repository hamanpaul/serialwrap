//! 假 `ProcScan`（feature `test-fakes`）：serialwrapd 實例清單、tty 持有者與 pid 存活皆可腳本化。
//!
//! pid 存活可綁定假 RPC 連線計數（`alive_until_connect`）：pid 在 `connect_count(ep) < n` 期間存活，
//! 與 `FakeRpcConnector::refuse_from_connect(ep, n)` 同一計數，故「ping 翻為 refused」與「pid 不存活」在同一輪詢翻轉。

use std::collections::BTreeMap;
use std::path::PathBuf;
use std::sync::{Arc, Mutex};

use super::rpc::FakeRpcConnector;
use crate::proc_scan::{DaemonProc, HolderScanStatus, ProcIdentity, ProcScan, ScanFailure};

#[derive(Debug, Clone)]
enum GateKind {
    /// 連線次數 `< n` 時存活（`refuse_from_connect(ep, n)` 同輪翻轉）。
    BelowConnects(usize),
    /// `stop_after_send` 觸發後才不存活。
    UntilStopped,
}

#[derive(Debug, Clone)]
struct ConnectGate {
    conn: Arc<FakeRpcConnector>,
    ep: String,
    kind: GateKind,
}

#[derive(Debug, Default)]
pub struct FakeProcScan {
    base: Mutex<Vec<DaemonProc>>,
    script: Mutex<Vec<Vec<DaemonProc>>>,
    failing: Mutex<bool>,
    alive: Mutex<BTreeMap<u32, Vec<bool>>>,
    gates: Mutex<BTreeMap<u32, ConnectGate>>,
    holders: Mutex<(Vec<u32>, Option<HolderScanStatus>)>,
}

impl FakeProcScan {
    pub fn new(v: Vec<DaemonProc>) -> Arc<Self> {
        Arc::new(Self {
            base: Mutex::new(v),
            ..Self::default()
        })
    }

    /// 之後的 `serialwrapd_instances` 依序回傳這些清單；用盡後回到基底清單。
    pub fn script(&self, seq: Vec<Vec<DaemonProc>>) {
        *self.script.lock().unwrap_or_else(|e| e.into_inner()) = seq;
    }

    /// 之後的 `serialwrapd_instances` 一律失敗。
    pub fn fail(&self) {
        *self.failing.lock().unwrap_or_else(|e| e.into_inner()) = true;
    }

    /// 指定 pid 的存活序列（依序；用盡後保持最後一筆）。
    pub fn alive_script(&self, pid: u32, seq: Vec<bool>) {
        self.alive
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .insert(pid, seq);
    }

    /// `pid` 在 `conn` 對 `ep` 的連線次數達 `n` 之前存活，達到後不存活（與 `refuse_from_connect(ep, n)` 同輪翻轉）。
    pub fn alive_until_connect(&self, pid: u32, conn: Arc<FakeRpcConnector>, ep: &str, n: usize) {
        self.gates
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .insert(pid, ConnectGate { conn, ep: ep.to_string(), kind: GateKind::BelowConnects(n) });
    }

    /// `pid` 在 `conn` 對 `ep` 進入 `stop_after_send` 停止狀態之前存活（之後不存活）。
    pub fn alive_until_stopped(&self, pid: u32, conn: Arc<FakeRpcConnector>, ep: &str) {
        self.gates
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .insert(pid, ConnectGate { conn, ep: ep.to_string(), kind: GateKind::UntilStopped });
    }

    fn gate_alive(gate: &ConnectGate) -> bool {
        match gate.kind {
            GateKind::BelowConnects(n) => gate.conn.connect_count(&gate.ep) < n,
            GateKind::UntilStopped => !gate.conn.is_stopped(&gate.ep),
        }
    }

    /// 非消耗性查詢：`pid` 是否已由連線閘門判定為不存活（不推進任何腳本序列）。
    fn gate_says_gone(&self, pid: u32) -> bool {
        match self.gates.lock().unwrap_or_else(|e| e.into_inner()).get(&pid) {
            Some(g) => !Self::gate_alive(g),
            None => false,
        }
    }

    pub fn holders(&self, v: Vec<u32>, st: HolderScanStatus) {
        *self.holders.lock().unwrap_or_else(|e| e.into_inner()) = (v, Some(st));
    }
}

impl ProcScan for FakeProcScan {
    fn serialwrapd_instances(&self) -> Result<Vec<DaemonProc>, ScanFailure> {
        if *self.failing.lock().unwrap_or_else(|e| e.into_inner()) {
            return Err(ScanFailure);
        }
        let mut script = self.script.lock().unwrap_or_else(|e| e.into_inner());
        if !script.is_empty() {
            return Ok(script.remove(0));
        }
        let base = self.base.lock().unwrap_or_else(|e| e.into_inner()).clone();
        Ok(base.into_iter().filter(|d| !self.gate_says_gone(d.pid)).collect())
    }

    fn serialwrapd_tty_holders(&self, _paths: &[PathBuf]) -> (Vec<u32>, HolderScanStatus) {
        let guard = self.holders.lock().unwrap_or_else(|e| e.into_inner());
        (guard.0.clone(), guard.1.unwrap_or(HolderScanStatus::Ok))
    }

    fn pid_alive(&self, pid: u32) -> bool {
        if let Some(gate) = self.gates.lock().unwrap_or_else(|e| e.into_inner()).get(&pid).cloned() {
            return Self::gate_alive(&gate);
        }
        {
            let mut alive = self.alive.lock().unwrap_or_else(|e| e.into_inner());
            if let Some(seq) = alive.get_mut(&pid) {
                return if seq.len() > 1 { seq.remove(0) } else { seq.first().copied().unwrap_or(false) };
            }
        }
        self.base
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .iter()
            .any(|d| d.pid == pid)
    }

    fn identity_alive(&self, id: &ProcIdentity) -> bool {
        if !self.pid_alive(id.pid) {
            return false;
        }
        // PID 重用：記錄於清單的 start_ticks 與呼叫端持有者不符即視為不相符（同 pid 不同 identity）。
        let recorded = self
            .base
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .iter()
            .flat_map(|d| d.tree.iter())
            .find(|p| p.pid == id.pid)
            .and_then(|p| p.start_ticks);
        match (recorded, id.start_ticks) {
            (Some(r), Some(s)) => r == s,
            _ => true,
        }
    }
}
