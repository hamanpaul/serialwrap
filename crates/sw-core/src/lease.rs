//! agent-only Interactive Lease 與 tombstone（Slice 2.1 Task 14；SC-13）。
//!
//! 對照 `session_manager.py` 的 `InteractiveLease`（`touch`／`expired`）與
//! `_record_interactive_terminal_locked`（每 session 有界 expired FIFO，`INTERACTIVE_TOMBSTONE_MAX` = 16）。

use std::collections::VecDeque;

use crate::Mono;

/// `INTERACTIVE_TOMBSTONE_MAX`（constants.py）。
pub const INTERACTIVE_TOMBSTONE_MAX: usize = 16;

/// agent 互動租約（`session.interactive_open` 建立；1 s tick 主動到期）。
#[derive(Clone, Debug)]
pub struct InteractiveLease {
    pub interactive_id: String,
    pub owner: String,
    pub created_at: String,
    pub timeout_s: f64,
    pub last_activity: Mono,
    pub status: &'static str,
    pub recovery_mode: bool,
}

impl InteractiveLease {
    /// 新租約（`status = "active"`；`last_activity = now`）。
    pub fn new(
        interactive_id: String,
        owner: String,
        created_at: String,
        timeout_s: f64,
        now: Mono,
        recovery_mode: bool,
    ) -> InteractiveLease {
        InteractiveLease {
            interactive_id,
            owner,
            created_at,
            timeout_s,
            last_activity: now,
            status: "active",
            recovery_mode,
        }
    }

    pub fn touch(&mut self, now: Mono) {
        self.last_activity = now;
    }

    /// `now - last > timeout`（嚴格大於）。
    pub fn expired(&self, now: Mono) -> bool {
        now.saturating_sub(self.last_activity) as f64 / 1_000_000_000.0 > self.timeout_s
    }

    /// 目前期限（`last_activity + timeout`；`LeaseExpiry` timer 只在與此值相同時生效）。
    pub fn deadline(&self) -> Mono {
        let ns = (self.timeout_s.max(0.0) * 1_000_000_000.0) as u64;
        self.last_activity.saturating_add(ns)
    }

    /// owner 是否為 human console（Slice 2.1 只承接 agent lease；human 不做 lazy 到期）。
    pub fn is_human(&self) -> bool {
        self.owner.starts_with("human:")
    }
}

/// 已到期租約的紀錄（cap 16，FIFO 淘汰；`interactive_tombstones`）。
///
/// Python 只把 `terminal_status="expired"`、`keep_tombstone=True` 的租約留在 `_interactive`；
/// 一般 close 直接移除（之後查詢回 `INTERACTIVE_NOT_FOUND`）。
pub struct Tombstones {
    entries: VecDeque<(String, &'static str)>,
}

impl Default for Tombstones {
    fn default() -> Self {
        Tombstones::new()
    }
}

impl Tombstones {
    pub fn new() -> Tombstones {
        Tombstones {
            entries: VecDeque::new(),
        }
    }

    /// 加入一筆終態（同 id 不重複）；超過 16 筆時淘汰最舊者。
    pub fn push(&mut self, id: String, status: &'static str) {
        if !self.entries.iter().any(|(existing, _)| *existing == id) {
            self.entries.push_back((id, status));
        }
        while self.entries.len() > INTERACTIVE_TOMBSTONE_MAX {
            self.entries.pop_front();
        }
    }

    /// 查詢 tombstone 中的終態（`"expired"`）；不存在回 `None`。
    pub fn status_of(&self, id: &str) -> Option<&'static str> {
        self.entries
            .iter()
            .find(|(existing, _)| existing == id)
            .map(|(_, status)| *status)
    }

    pub fn len(&self) -> usize {
        self.entries.len()
    }

    pub fn is_empty(&self) -> bool {
        self.entries.is_empty()
    }
}
