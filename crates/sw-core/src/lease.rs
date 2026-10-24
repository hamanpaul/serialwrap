//! agent-only Interactive Lease 與 tombstone（Slice 2.1 Task 14；SC-13）。
//!
//! Task 0 骨架：型別與簽章凍結，行為由 Task 14 交付。

use std::collections::VecDeque;

use sw_proto::pyjson::PyDict;

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
    /// P6 增補 #14（07 Task 7）：開 lease 時是否 suspend 了 human raw。
    pub suspended_human: bool,
    /// P6 增補 #14（07 Task 7）：human lease 的 peer 消失時點（peer-loss grace 起點）。
    pub peer_lost_at: Option<Mono>,
}

impl InteractiveLease {
    pub fn touch(&mut self, _now: Mono) {
        crate::s21_todo!("T14", "InteractiveLease::touch")
    }
    /// `now - last > timeout`（嚴格大於）。
    pub fn expired(&self, _now: Mono) -> bool {
        crate::s21_todo!("T14", "InteractiveLease::expired")
    }
    /// P6 Task 7：owner 以 `human:` 開頭。
    pub fn is_human(&self) -> bool {
        todo!("P6 Task 7")
    }
    /// P6 Task 7：human lease 的 client_id（owner 去掉 `human:`）。
    pub fn human_client_id(&self) -> Option<&str> {
        todo!("P6 Task 7")
    }
}

/// 已關閉租約的紀錄（cap 16，FIFO 淘汰；`interactive_tombstones`）。
/// P6 Task 1：加 `Debug`／`Default`（`LeaseState` 需要；`Default` 即空佇列）。
#[derive(Debug, Default)]
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

/// P6 增補 #14（07 Task 7）：每 session 的 lease 簿記（active、stash 的 human lease、tombstone、retained human owner）。
#[derive(Debug, Default)]
pub struct LeaseState {
    pub active: Option<InteractiveLease>,
    pub stashed_human: Option<InteractiveLease>,
    pub tombstones: Tombstones,
    pub retained_human_owner: Option<String>,
    pub retained_human_timeout_s: Option<f64>,
}

/// P6 增補 #14：`_PostCloseAction`（05 §1.5）＋07 D-16。
#[derive(Debug, Default)]
pub struct PostClose {
    pub needs_resume: bool,
    pub clear_owner_after_resume: bool,
    pub replay_allowed: bool,
    pub dropped_stash_client: Option<String>,
}

/// P6 增補 #14：`{"interactive_owner","human_attached","human_active","recovery_mode"}`（02 §6.2）。
pub fn lease_context(_lease: Option<&InteractiveLease>, _last_human_input_at: Option<Mono>, _now: Mono) -> PyDict {
    todo!("P6 Task 7")
}
