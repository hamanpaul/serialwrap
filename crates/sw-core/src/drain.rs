//! P5-T12：兩階段 drain 的狀態與 worker 收尾回條型別。

use sw_pal::traits::PalError;

use crate::Mono;
use crate::mailbox::Reply;
pub use crate::tx_writer::{EvidenceGap, OpId, TxLedger};

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
    /// 本次 Release 是對已 RELEASED session 的在途 attach 做取消，不得改寫 release metadata。
    pub already_released: bool,
    /// Phase 1 當下的前景命令。
    pub aborted_op: Option<(OpId, String)>,
    /// drain 中到達的 Removed／Rebound／IoError。
    pub post_kind: Option<StopKind>,
}

/// IoWorker 收尾回條（BC-03 映射的唯一來源；只含未結算 op）。
///
/// P8 Task 5（OD-69「送出前排空」、§5.2.4／SC-07）：TxWriter 在交回 writer 之前先把 echo-ACK 節流未結的聚合緩衝
/// 各寫成一筆 TX（`tx_writer` 的 `flush_open_aggregates`），本回條在 join writer 之後才組裝，故必晚於這些 TX 紀錄；
/// 寫入失敗記在 `ledger` 的 op `wal_gaps`（已結算者進 `unresolved_gaps`），併入 `unrecorded`。shutdown 時
/// `SessionManager::shutdown` 等回條後 daemon 才關 WAL，同理先於 WAL 關閉。
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
