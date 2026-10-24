//! 每 console 出向 ring（P6 Task 5；SC-15、07 D-05）。
//!
//! `Control`（greeting、協商回覆）永不被 drop-oldest 丟棄；`Display`（RX fan-out、本地回顯）合計超過上限時丟最舊整個元素，
//! 已部分寫出的 front 不丟；單一元素超過上限時只留尾端，IAC 逸出過的資料起點不得落在 `FF FF` 對中間。
//! Task 1 骨架：型別與簽章凍結，函式本體為 `todo!("P6 Task 5")`。

use std::collections::VecDeque;

/// ring 元素。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum OutItem {
    Control(Vec<u8>),
    Display { bytes: Vec<u8>, iac_escaped: bool },
}

/// `push` 的結果。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PushOutcome {
    Queued,
    DroppedOldest { bytes: usize },
}

/// 每 console 一個的出向 ring。
#[derive(Debug)]
pub struct OutRing {
    #[allow(dead_code)]
    q: VecDeque<OutItem>,
    #[allow(dead_code)]
    front_off: usize,
    #[allow(dead_code)]
    display_bytes: usize,
    #[allow(dead_code)]
    cap: usize,
    #[allow(dead_code)]
    dropped_total: u64,
}

impl OutRing {
    pub fn new(_cap: usize) -> Self {
        todo!("P6 Task 5")
    }

    pub fn push(&mut self, _item: OutItem) -> PushOutcome {
        todo!("P6 Task 5")
    }

    /// front 元素尚未寫出的部分。
    pub fn front(&self) -> Option<&[u8]> {
        todo!("P6 Task 5")
    }

    /// `try_write` 回報寫出 `n` bytes。
    pub fn consume(&mut self, _n: usize) {
        todo!("P6 Task 5")
    }

    /// 07 D-05：無 peer 的 PTY；不清 Control、不清部分寫出的 front。回傳清除的 bytes 數。
    pub fn clear_display(&mut self) -> usize {
        todo!("P6 Task 5")
    }

    pub fn display_bytes(&self) -> usize {
        todo!("P6 Task 5")
    }

    pub fn is_empty(&self) -> bool {
        todo!("P6 Task 5")
    }

    pub fn dropped_total(&self) -> u64 {
        todo!("P6 Task 5")
    }

    /// 最新一個元素（測試與診斷用）。
    pub fn back(&self) -> Option<&OutItem> {
        todo!("P6 Task 5")
    }

    /// 尚未寫出的 Control bytes 總數（07 D-05 上限由 hub 判斷）。
    pub fn control_bytes(&self) -> usize {
        todo!("P6 Task 5")
    }
}
