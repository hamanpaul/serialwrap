//! agent-only Interactive Lease 與 tombstone（Slice 2.1 Task 14；SC-13）。
//!
//! Task 0 骨架：型別與簽章凍結，行為由 Task 14 交付。

use std::collections::VecDeque;

use crate::Mono;

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
    pub fn touch(&mut self, _now: Mono) {
        crate::s21_todo!("T14", "InteractiveLease::touch")
    }
    /// `now - last > timeout`（嚴格大於）。
    pub fn expired(&self, _now: Mono) -> bool {
        crate::s21_todo!("T14", "InteractiveLease::expired")
    }
}

/// 已關閉租約的紀錄（cap 16，FIFO 淘汰；`interactive_tombstones`）。
pub struct Tombstones {
    _entries: VecDeque<(String, &'static str)>,
}

impl Tombstones {
    pub fn new() -> Tombstones {
        crate::s21_todo!("T14", "Tombstones::new")
    }
    pub fn push(&mut self, _id: String, _status: &'static str) {
        crate::s21_todo!("T14", "Tombstones::push")
    }
}
