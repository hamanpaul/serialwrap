//! P5-T11：連線、FSM 與 owner 世代計數。
//!
//! `conn_epoch`（bridge 代次）、`fsm_gen`（登入世代）、`owner_gen`（lease／owner 世代）、`bridge_generation`（對外計數）。
//! `EpochAllocator` 在成功開啟 bridge 後配發全域遞增的連線世代。

use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;

/// 全域單調的 epoch 配號器（`open_exclusive` 成功才 `next()`）。
#[allow(dead_code)]
pub struct EpochAllocator(AtomicU64);

impl EpochAllocator {
    pub fn new() -> Arc<EpochAllocator> {
        Arc::new(EpochAllocator(AtomicU64::new(0)))
    }

    pub fn next(&self) -> u64 {
        self.0.fetch_add(1, Ordering::SeqCst).saturating_add(1)
    }
}

/// 世代快照（測試與 `public()` 共用）。
#[derive(Clone, Debug, Default)]
pub struct Generations {
    pub conn_epoch: Option<u64>,
    pub fsm_gen: u64,
    pub owner_gen: u64,
    pub bridge_generation: u64,
}
