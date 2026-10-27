//! Human console（P6 Slice 2.2；07-slice22-console.md）。
//!
//! 狀態決策全部在 `SessionCore`：ownership、line editor、DeferredBuffer、human TX lane 都是 `HumanConsoleState` 的欄位；
//! I/O 由每 session 一條 `ConsoleHub` thread 負責（`hub.rs`）。Task 1 骨架：型別與簽章凍結，行為由 Task 2～10 實作。

use std::collections::{HashMap, HashSet};

use indexmap::IndexMap;

use sw_proto::constants::DEFERRED_INPUT_MAX_BYTES;

use crate::deferred::DeferredBuffer;
use crate::Mono;

pub mod hub;
pub mod human_tx;
pub mod line_editor;
pub mod line_mode;
pub mod out_ring;
pub mod ownership;

use human_tx::HumanLane;
use line_editor::LineEditor;
use ownership::OwnershipState;

/// console 的承接方式。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ConsoleTransportKind {
    Pty,
    TcpTelnet,
    Com0com,
}

/// 一個 console 的 core 側紀錄。
#[derive(Debug, Clone)]
pub struct ConsoleRecord {
    pub client_id: String,
    pub client_lifetime: u64,
    pub label: String,
    pub vtty: String,
    pub kind: ConsoleTransportKind,
    pub internal: bool,
    pub attached_at: Mono,
    pub line: LineEditor,
}

/// 07 D-22：回放保留；`remaining`＝本 hold 尚未結算的 HumanReplay 數。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ReplayHold {
    pub hold_id: u64,
    pub remaining: usize,
    pub revoked: bool,
}

/// `ConsoleScan` 的來源（07 D-09）。
#[derive(Debug, Clone)]
pub enum ScanSource {
    Rpc,
    Hub,
}

/// `/proc` 掃描快照（在 actor thread 之外取得，07 D-09）。
#[derive(Debug, Clone)]
pub struct ConsoleScanSnapshot {
    pub held: Option<HashSet<String>>,
    pub taken_at: Mono,
}

/// `SessionCore` 的 human console 欄位集合。
#[derive(Debug)]
pub struct HumanConsoleState {
    pub consoles: IndexMap<String, ConsoleRecord>,
    pub primary_client_id: Option<String>,
    pub ownership: OwnershipState,
    /// 私有（07 D-02）：其他 Phase 只能經 `SessionCore::invalidate_human_input` 影響。
    deferred: DeferredBuffer,
    pub lane: HumanLane,
    pub peer_present: HashMap<String, bool>,
    pub next_lifetime: u64,
    /// 07 D-20。
    pub vtty_cache: Option<String>,
    /// 07 D-22。
    pub replay_hold: Option<ReplayHold>,
    pub next_hold_id: u64,
    /// 07 D-22(4)。
    pub replay_after_revoked: bool,
}

impl HumanConsoleState {
    pub fn new() -> HumanConsoleState {
        HumanConsoleState {
            consoles: IndexMap::new(),
            primary_client_id: None,
            ownership: OwnershipState::default(),
            deferred: DeferredBuffer::new(DEFERRED_INPUT_MAX_BYTES),
            lane: HumanLane::default(),
            peer_present: HashMap::new(),
            next_lifetime: 0,
            vtty_cache: None,
            replay_hold: None,
            next_hold_id: 0,
            replay_after_revoked: false,
        }
    }

    /// 07 D-02：只供 `SessionCore` 使用（crate 內）。
    pub(crate) fn deferred_mut(&mut self) -> &mut DeferredBuffer {
        &mut self.deferred
    }

    /// 取出全部可回放項目（client 存活且 lifetime 相符者；07 D-07 規則 4）。
    pub(crate) fn take_replay(&mut self, current_epoch: u64) -> Vec<crate::deferred::Replay> {
        let consoles = &self.consoles;
        let live = |cid: &str, life: u64| consoles.get(cid).is_some_and(|c| c.client_lifetime == life);
        self.deferred.take_replay(current_epoch, &live)
    }

    /// 07 D-15(4)：只取出某 client 的可回放項目。
    pub(crate) fn take_replay_for(&mut self, client_id: &str, current_epoch: u64) -> Option<crate::deferred::Replay> {
        let consoles = &self.consoles;
        let live = |cid: &str, life: u64| consoles.get(cid).is_some_and(|c| c.client_lifetime == life);
        self.deferred.take_replay_for(client_id, current_epoch, &live)
    }

    /// 唯讀查詢（SessionCore 與測試的 `deferred_len`）。
    pub fn deferred_len_for(&self, client_id: &str) -> usize {
        self.deferred.len_for(client_id)
    }

    /// 該 client 是否仍存在且 lifetime 相符。
    pub fn is_live(&self, client_id: &str, lifetime: u64) -> bool {
        self.consoles
            .get(client_id)
            .is_some_and(|c| c.client_lifetime == lifetime)
    }

    /// 配下一個 `client_lifetime`（同 id 重新連入亦不同）。
    pub fn alloc_lifetime(&mut self) -> u64 {
        self.next_lifetime = self.next_lifetime.saturating_add(1);
        self.next_lifetime
    }

    /// 移除 console；primary 轉移給剩下的第一個（Python `_drop_console_client`／`detach_console`）。
    pub fn remove_console(&mut self, client_id: &str) -> Option<ConsoleRecord> {
        let rec = self.consoles.shift_remove(client_id)?;
        if self.primary_client_id.as_deref() == Some(client_id) {
            self.primary_client_id = self.consoles.keys().next().cloned();
        }
        self.peer_present.remove(client_id);
        Some(rec)
    }
}

impl Default for HumanConsoleState {
    fn default() -> Self {
        HumanConsoleState::new()
    }
}
