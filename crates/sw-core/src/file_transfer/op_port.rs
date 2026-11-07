//! P8 Task 5：`OpTransferPort`——以前景 op 的 `OpCtx` 實作 [`TransferPort`]。
//!
//! 對照 `uart_io.py` 的 `send_command`、`send_command_echo_paced`、`_await_echo_progress`、`cancel_input_line`、
//! `rx_snapshot_len`／`rx_text_from`／`wait_for_regex_from`。TX 一律經該 epoch 的 `TxWriterHandle`
//! （`TxClass::FileTransfer`、permit 的 `op_id` 為授權 grant 的 `op_id`）；RX 讀 `RxShared`。

use std::time::Duration;

use sw_proto::pyre::PyRegex;

use super::{EchoPaced, TransferAbort, TransferPort};
use crate::op_runner::OpCtx;
use crate::session_core::OpGrant;
use crate::uart_bridge::RunCancel;

/// 前景檔案傳輸的 UART 介面（OD-62：整個傳輸共用一個 TX operation）。
pub struct OpTransferPort<'a> {
    #[allow(dead_code)]
    ctx: &'a OpCtx,
    #[allow(dead_code)]
    grant: OpGrant,
    #[allow(dead_code)]
    cancel: RunCancel,
}

impl<'a> OpTransferPort<'a> {
    pub fn new(ctx: &'a OpCtx, grant: &OpGrant, cancel: &'a RunCancel) -> Self {
        OpTransferPort { ctx, grant: grant.clone(), cancel: cancel.clone() }
    }
}

impl TransferPort for OpTransferPort<'_> {
    fn send_command(&mut self, _cmd: &str, _source: &str, _cmd_id: Option<&str>) -> Result<(), TransferAbort> {
        todo!("P8-T5")
    }

    fn supports_echo_paced(&self) -> bool {
        todo!("P8-T5")
    }

    fn send_command_echo_paced(
        &mut self,
        _cmd: &str,
        _source: &str,
        _cmd_id: Option<&str>,
        _slice_size: usize,
        _echo_timeout: Duration,
    ) -> Result<EchoPaced, TransferAbort> {
        todo!("P8-T5")
    }

    fn cancel_input_line(&mut self, _source: &str) -> Result<(), TransferAbort> {
        todo!("P8-T5")
    }

    fn rx_snapshot_len(&self) -> u64 {
        todo!("P8-T5")
    }

    fn rx_text_from(&self, _from_offset: u64) -> String {
        todo!("P8-T5")
    }

    fn wait_for_regex_from(&mut self, _re: &PyRegex, _from_offset: u64, _timeout: Duration) -> Result<bool, TransferAbort> {
        todo!("P8-T5")
    }
}
