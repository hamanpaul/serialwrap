//! Human TX lane（P6 Task 6；07 D-15、D-22）。
//!
//! 每 session 同時最多一筆 human TX（HumanRaw 或 HumanReplay）未結算；後續依序排在 backlog（上限
//! `HUMAN_TX_BACKLOG_MAX_BYTES`）。actor 對 pump 只 `try_send`（容量 1），pump thread 可阻塞送 TxWriter。
//! Task 1 骨架：型別與簽章凍結，函式本體為 `todo!("P6 Task 6")`。

use std::collections::VecDeque;

use crossbeam_channel::Sender;

use crate::mailbox::ControlEvent;
use crate::tx_writer::{TxClass, TxPermit, TxWriterHandle};

/// 一筆 human TX job 的識別（結算時以 `HumanTxCompleted { tag, .. }` 帶回）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HumanTxTag {
    pub seq: u64,
    pub client_id: String,
    pub client_lifetime: u64,
    pub input_epoch: u64,
    pub class: TxClass,
    pub len: usize,
}

/// backlog 中尚未送出的一筆（07 D-15(2)）。
#[derive(Debug, Clone)]
#[allow(dead_code)]
pub(crate) struct BacklogItem {
    pub(crate) tag: HumanTxTag,
    pub(crate) payload: Vec<u8>,
}

/// 每 session 一條 human TX lane 的簿記。
#[derive(Debug, Default)]
pub struct HumanLane {
    #[allow(dead_code)]
    in_flight: Option<(HumanTxTag, Vec<u8>)>,
    #[allow(dead_code)]
    backlog: VecDeque<BacklogItem>,
    #[allow(dead_code)]
    backlog_bytes: usize,
    #[allow(dead_code)]
    next_seq: u64,
}

impl HumanLane {
    /// in_flight 或 backlog 中有該 client。
    pub fn is_busy_for(&self, _client_id: &str) -> bool {
        todo!("P6 Task 6")
    }

    pub fn in_flight(&self) -> Option<&HumanTxTag> {
        todo!("P6 Task 6")
    }
}

/// pump 容量 1 的 channel 已有一筆未取走。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PumpFull;

/// per-session human TX pump thread（`serialwrap-human-tx-<COM>`）。
pub struct HumanTxPump {
    _stub: (),
}

impl HumanTxPump {
    pub fn spawn(_com: &str, _tx: TxWriterHandle, _control: Sender<ControlEvent>) -> HumanTxPump {
        todo!("P6 Task 6")
    }

    /// bounded(1)；actor 永不阻塞。
    pub fn try_submit(&self, _tag: HumanTxTag, _permit: TxPermit, _payload: Vec<u8>) -> Result<(), PumpFull> {
        todo!("P6 Task 6")
    }

    pub fn stop(self) {
        todo!("P6 Task 6")
    }
}
