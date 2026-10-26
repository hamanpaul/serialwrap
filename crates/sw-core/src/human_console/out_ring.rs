//! 每 console 出向 ring（P6 Task 5；SC-15、07 D-05）。
//!
//! `Control`（greeting、協商回覆）永不被 drop-oldest 丟棄；`Display`（RX fan-out、本地回顯）合計超過上限時丟最舊整個元素，
//! 已部分寫出的 front 不丟；單一元素超過上限時只留尾端 `cap` bytes，IAC 逸出過的資料起點不得落在 `FF FF` 對中間。
//! Control 的總量上限（`CONSOLE_CONTROL_MAX_BYTES`）由 hub 以 `control_bytes()` 判斷（超過即關閉該 console，SC-31(c)）。

use std::collections::VecDeque;

/// ring 元素。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum OutItem {
    Control(Vec<u8>),
    Display { bytes: Vec<u8>, iac_escaped: bool },
}

impl OutItem {
    fn bytes(&self) -> &[u8] {
        match self {
            OutItem::Control(b) => b,
            OutItem::Display { bytes, .. } => bytes,
        }
    }

    fn is_display(&self) -> bool {
        matches!(self, OutItem::Display { .. })
    }
}

/// `push` 的結果。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PushOutcome {
    Queued,
    /// 丟掉的全是較舊的 Display 元素（或單一超大元素的頭端）。
    DroppedOldest { bytes: usize },
    /// 已部分寫出的 front 受保護、為守住上限而丟掉了剛推入的新項（parity-amendments-2026-10-26 #178，P6 審查 F7）；
    /// `bytes` 為本次 push 丟棄的合計（含先丟的較舊元素）。hub 的 WARNING 文字須註明丟的是最新輸出。
    DroppedNewest { bytes: usize },
}

/// 每 console 一個的出向 ring。
#[derive(Debug)]
pub struct OutRing {
    q: VecDeque<OutItem>,
    /// front 元素已寫出的 bytes 數。
    front_off: usize,
    /// 尚未寫出的 Display bytes 總數。
    display_bytes: usize,
    /// 尚未寫出的 Control bytes 總數。
    control_bytes: usize,
    cap: usize,
    /// 累計被 drop-oldest／截頭丟掉的 Display bytes。
    dropped_total: u64,
}

impl OutRing {
    pub fn new(cap: usize) -> Self {
        OutRing { q: VecDeque::new(), front_off: 0, display_bytes: 0, control_bytes: 0, cap, dropped_total: 0 }
    }

    pub fn push(&mut self, item: OutItem) -> PushOutcome {
        match item {
            OutItem::Control(b) => {
                if !b.is_empty() {
                    self.control_bytes += b.len();
                    self.q.push_back(OutItem::Control(b));
                }
                PushOutcome::Queued
            }
            OutItem::Display { bytes, iac_escaped } => {
                if bytes.is_empty() {
                    return PushOutcome::Queued;
                }
                let mut dropped = 0usize;
                let bytes = if bytes.len() > self.cap {
                    let mut start = bytes.len() - self.cap;
                    if iac_escaped {
                        // 起點前連續 0xFF 的個數為奇數 → 起點落在 `FF FF` 對中間，再後移 1。
                        let run = bytes[..start].iter().rev().take_while(|&&b| b == 0xFF).count();
                        if run % 2 == 1 {
                            start += 1;
                        }
                    }
                    dropped += start;
                    bytes[start..].to_vec()
                } else {
                    bytes
                };
                self.display_bytes += bytes.len();
                self.q.push_back(OutItem::Display { bytes, iac_escaped });
                let mut dropped_newest = false;
                while self.display_bytes > self.cap {
                    // 已部分寫出的 front 受保護：從 index 1 起找。
                    let from = usize::from(self.front_off > 0);
                    let Some(idx) = (from..self.q.len()).find(|&i| self.q[i].is_display()) else { break };
                    dropped_newest |= idx == self.q.len() - 1;
                    let removed = self.q.remove(idx).expect("index 在範圍內");
                    let n = removed.bytes().len();
                    self.display_bytes -= n;
                    dropped += n;
                }
                self.dropped_total += dropped as u64;
                if dropped_newest {
                    PushOutcome::DroppedNewest { bytes: dropped }
                } else if dropped > 0 {
                    PushOutcome::DroppedOldest { bytes: dropped }
                } else {
                    PushOutcome::Queued
                }
            }
        }
    }

    /// front 元素尚未寫出的部分。
    pub fn front(&self) -> Option<&[u8]> {
        self.q.front().map(|it| &it.bytes()[self.front_off..])
    }

    /// `try_write` 回報寫出 `n` bytes（可跨越多個元素）。
    pub fn consume(&mut self, mut n: usize) {
        while n > 0 {
            let Some(front) = self.q.front() else { break };
            let remaining = front.bytes().len() - self.front_off;
            let k = n.min(remaining);
            if front.is_display() {
                self.display_bytes -= k;
            } else {
                self.control_bytes -= k;
            }
            self.front_off += k;
            n -= k;
            if self.front_off == front.bytes().len() {
                self.q.pop_front();
                self.front_off = 0;
            }
        }
    }

    /// 07 D-05：無 peer 的 PTY；不清 Control、不清部分寫出的 front。回傳清除的 bytes 數。
    pub fn clear_display(&mut self) -> usize {
        let protect_front = self.front_off > 0;
        let mut cleared = 0usize;
        let mut kept = VecDeque::with_capacity(self.q.len());
        for (i, it) in self.q.drain(..).enumerate() {
            if it.is_display() && !(i == 0 && protect_front) {
                cleared += it.bytes().len();
            } else {
                kept.push_back(it);
            }
        }
        self.q = kept;
        self.display_bytes -= cleared;
        cleared
    }

    pub fn display_bytes(&self) -> usize {
        self.display_bytes
    }

    pub fn is_empty(&self) -> bool {
        self.q.is_empty()
    }

    pub fn dropped_total(&self) -> u64 {
        self.dropped_total
    }

    /// 最新一個元素（測試與診斷用）。
    pub fn back(&self) -> Option<&OutItem> {
        self.q.back()
    }

    /// 尚未寫出的 Control bytes 總數（07 D-05 上限由 hub 判斷）。
    pub fn control_bytes(&self) -> usize {
        self.control_bytes
    }
}
