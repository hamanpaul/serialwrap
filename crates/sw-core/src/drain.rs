//! P5-T12：兩階段 drain 的型別（紅燈階段骨架；T11 的 `Effect::BeginDrain`／`ControlEvent::WorkerStopped` 使用）。
//!
//! 函式本體（drain 狀態機）由 Task 12 實作者補上；本檔只宣告型別。

use sw_pal::traits::PalError;

use crate::mailbox::Reply;
use crate::tx_writer::{EvidenceGap, OpId, TxLedger};
use crate::Mono;

/// 一次 drain 的識別碼（`WorkerStopped`／`DrainTimeout` 以此比對）。
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub struct StopId(pub u64);

/// drain 的起因。
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum StopKind {
    Release,
    Recover,
    IoError,
    Removed,
    Rebound,
    Clear,
    Bind,
    Shutdown,
    LoginFailed,
}

/// 進行中的 drain（同一 session 同時只有一個）。
pub struct StopOp {
    pub id: StopId,
    pub kind: StopKind,
    pub epoch: u64,
    pub waiters: Vec<Reply>,
    pub started: Mono,
    pub stalled: bool,
    /// Phase 1 當下的前景命令。
    pub aborted_op: Option<(OpId, String)>,
    /// drain 中到達的 Removed／Rebound／IoError。
    pub post_kind: Option<StopKind>,
}

/// IoWorker 收尾回條（BC-03 映射的唯一來源；只含未結算 op）。
#[derive(Debug)]
pub struct WorkerStoppedReceipt {
    pub epoch: u64,
    pub stop_id: StopId,
    pub ledger: TxLedger,
    pub unrecorded: Vec<EvidenceGap>,
    pub wal_error: Option<PalError>,
    pub handle_closed: bool,
    pub close_error: Option<PalError>,
}
