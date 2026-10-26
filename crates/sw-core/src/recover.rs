//! session recover／force recover 的計畫與回覆組裝（P7 Task 9；`recover_session`、`_force_recover`，05 §12）。
//! 常數取自 `sw_proto::constants::{FORCE_RECOVER_POLLS, FORCE_RECOVER_POLL_S}`。P7 Task 0 骨架：本體為 `todo!("P7 Task 9")`。

use sw_proto::pyjson::PyDict;

pub struct RecoverRequest {
    pub timeout_s: f64,
    pub force: bool,
}

#[derive(Debug, PartialEq)]
pub enum RecoverPlan {
    /// RELEASED／FLASHING_BUSY／SESSION_NOT_READY 等立即回覆。
    Reply(PyDict),
    /// 無 bridge 且裝置在、且無 drain 進行中：ATTACHING＋RequestAttach。
    Reattach,
    /// drain（RECOVERING）進行中：加入 06 StopOp waiters，回條到達後再重新 plan。
    AwaitDrain,
    /// ATTACHED 非 force：ExistingBridgeProbe{RecoverReprobe}。
    Reprobe,
    /// ATTACHED／非 READY 的 force：begin_stop(Clear) 後輪詢。
    Force,
    /// READY：CTRL_C／CTRL_D，兩鍵失敗後自動 Force。
    ReadyKeys,
}

/// 順序逐字照 `recover_session`（`session_manager.py:4582-4667`）。
pub fn plan_recover(
    _state: &str,
    _has_bridge: bool,
    _draining: bool,
    _device_present: bool,
    _released: bool,
    _flash_busy: bool,
    _req: &RecoverRequest,
) -> RecoverPlan {
    todo!("P7 Task 9")
}

/// RELEASED／FLASHING_BUSY 的 `Reply` 為 false，其餘 true。
pub fn plan_resets_progress(_plan: &RecoverPlan) -> bool {
    todo!("P7 Task 9")
}

/// SC-34：`last_error`／`last_error_detail` 取自同一 session 快照。
pub fn reprobe_reply(
    _recovered: bool,
    _last_error: Option<&str>,
    _last_error_detail: Option<&str>,
    _bootloader_match: Option<&str>,
    _session: PyDict,
) -> PyDict {
    todo!("P7 Task 9")
}

pub fn force_reply(_recovered: bool, _session: Option<PyDict>) -> PyDict {
    todo!("P7 Task 9")
}
