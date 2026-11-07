//! P8 Task 6：`SessionCore` 的 `file.push`／`file.pull` 整合（`session_core` 子模組，可直接操作私有欄位）。
//!
//! 對照 origin/main：`service.py::_resolve_session_id(file_transfer=True)`（resolve 層）與
//! `session_manager.py::file_push`／`file_pull`（manager 層：gate、recovering、lease、前景槽）、
//! `_file_tx_rejected_result`（傳輸中被拒的 OD-62 映射；quiet 分支在此，其餘在 OpRunner）。

use super::*;

impl SessionCore {
    /// `SessionRequest::FileTransfer`：resolve 層與 manager 層在同一次事件處理內完成；通過者取得前景槽並交 OpRunner。
    pub(super) fn file_transfer_request(
        &mut self,
        req: crate::file_transfer::FileTransferRequest,
        reply: Reply,
        now: Mono,
    ) -> Vec<Effect> {
        let _ = (req, reply, now);
        todo!("P8-T6")
    }
}
