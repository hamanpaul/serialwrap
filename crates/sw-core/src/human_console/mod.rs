//! Human console（P6 Slice 2.2；07-slice22-console.md）。
//!
//! 狀態決策全部在 `SessionCore`：ownership、line editor、DeferredBuffer、human TX lane 都是 `HumanConsoleState` 的欄位；
//! I/O 由每 session 一條 `ConsoleHub` thread 負責（`hub.rs`）。Task 1 骨架：型別與簽章凍結，行為由 Task 2～10 實作。

use std::collections::{HashMap, HashSet};

use indexmap::IndexMap;

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
    #[allow(dead_code)]
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
