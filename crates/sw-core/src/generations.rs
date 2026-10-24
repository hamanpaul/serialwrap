//! P5-T11：世代計數（紅燈階段骨架）。
//!
//! `conn_epoch`（bridge 代次）、`fsm_gen`（登入世代）、`owner_gen`（lease／owner 世代）、`bridge_generation`（對外計數）。
//! 函式本體為 `s21_todo!`，由 Task 11 實作者補上。

use std::sync::atomic::AtomicU64;
use std::sync::Arc;

/// 全域單調的 epoch 配號器（`open_exclusive` 成功才 `next()`）。
#[allow(dead_code)]
pub struct EpochAllocator(AtomicU64);

impl EpochAllocator {
    pub fn new() -> Arc<EpochAllocator> {
        s21_todo!("T11", "EpochAllocator::new")
    }

    pub fn next(&self) -> u64 {
        s21_todo!("T11", "EpochAllocator::next")
    }
}

/// 世代快照（測試與 `public()` 共用）。
#[derive(Clone, Debug, Default)]
pub struct Generations {
    pub conn_epoch: Option<u64>,
    pub fsm_gen: u64,
    pub owner_gen: u64,
    pub bridge_generation: u64,
    /// P6 增補 #1（07 D-02）：BC-02 上下文世代（P1 Python `UARTBridge._owner_gen` 的 Rust 名稱），初值 0；
    /// 只在 `SessionCore::invalidate_human_input` 遞增。
    pub input_epoch: u64,
}
