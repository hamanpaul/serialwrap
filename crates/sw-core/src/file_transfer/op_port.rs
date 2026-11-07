//! P8 Task 5：`OpTransferPort`——以前景 op 的 `OpCtx` 實作 [`TransferPort`]。
//!
//! 對照 `uart_io.py` 的 `send_command`、`send_command_echo_paced`、`_await_echo_progress`、`cancel_input_line`、
//! `rx_snapshot_len`／`rx_text_from`／`wait_for_regex_from`。TX 一律經該 epoch 的 `TxWriterHandle`
//! （`TxClass::FileTransfer`、permit 的 `op_id` 為授權 grant 的 `op_id`；OD-62 整個傳輸共用一個 TX operation）；
//! RX 讀 `RxShared`。等待一律走 `RxShared::wait_for_regex_cancellable`（注入的 `Clock` 與同一個 `RunCancel`）。
//!
//! 中止（brief 不符 #2、#3、#5）：被拒那筆不重送，回 [`TransferAbort::TxRejected`]（只填 `rej`，`chunks`／
//! `transfer_cmd_id` 由引擎補齊）；等待被取消時以一次零 TX 的 `Fence::admit` 判定「此刻送出會被拒的原因」。
//! 錯誤碼映射（`AUTOBOOT_QUIET`／`TRANSFER_TIMEOUT` 等）由 session 層（T6）依 reason 決定，本模組不映射。

use std::time::Duration;

use sw_pal::traits::TxOutcome;
use sw_proto::pycompat::strip_ansi;
use sw_proto::pyre::{compile, PyFlags, PyRegex};

use super::{EchoPaced, RejectReason, TransferAbort, TransferPort, TxRejection};
use crate::op_runner::OpCtx;
use crate::session_core::OpGrant;
use crate::tx_writer::{AggregateKey, AggregateOp, FenceReject, TxClass, TxJob, TxPermit, TxReport};
use crate::uart_bridge::{RunCancel, WaitOutcome};

/// 前景檔案傳輸的 UART 介面。
pub struct OpTransferPort<'a> {
    ctx: &'a OpCtx,
    grant: OpGrant,
    /// 呼叫端的取消旗標（同一 flag／login 世代），另監看 drain Phase 1 的檔案傳輸取消世代（brief 不符 #1）。
    cancel: RunCancel,
    /// 「有任何新 RX」的等待條件（echo 進度以新 RX 喚醒後重算）。
    any_rx: PyRegex,
}

/// fence 拒絕原因 → 傳輸中止分類（brief 不符 #2；Flash／WAL／owner 等一律 `Other`，由 T6 映射）。
fn classify(reject: FenceReject) -> RejectReason {
    match reject {
        FenceReject::Quiet | FenceReject::ReconfirmPending => RejectReason::Quiet,
        FenceReject::Cancelled => RejectReason::Cancelled,
        FenceReject::StaleEpoch => RejectReason::StaleGeneration,
        FenceReject::Frozen => RejectReason::Draining,
        FenceReject::WalUnhealthy
        | FenceReject::Flash
        | FenceReject::OwnerMismatch
        | FenceReject::FsmMismatch
        | FenceReject::HumanAdmissionClosed
        | FenceReject::InputStale
        | FenceReject::ReplayRevoked => RejectReason::Other,
    }
}

fn confirmed_of(report: &TxReport) -> usize {
    match report.result.outcome {
        TxOutcome::NotSubmitted => 0,
        TxOutcome::Completed(n) => n,
        TxOutcome::Unknown { confirmed } => confirmed,
    }
}

fn fully_sent(report: &TxReport, requested: usize) -> bool {
    matches!(report.result.outcome, TxOutcome::Completed(n) if n >= requested)
}

/// 未完整提交那一筆的中止證據：reason 取 fence 拒絕原因（無原因＝部分寫入錯誤／結果不明 → `Other`）。
fn rejection(report: &TxReport) -> TransferAbort {
    TransferAbort::rejected(TxRejection {
        reason: report.reject.map_or(RejectReason::Other, classify),
        rejected_confirmed: confirmed_of(report),
        unknown: matches!(report.result.outcome, TxOutcome::Unknown { .. }),
    })
}

/// `_await_echo_progress` 的比對：以移動起點依序 `find` 每段（段間容許任意雜訊）；回（已確認字元數, 是否全部確認）。
fn echo_progress(text: &str, expected: &[&str]) -> (usize, bool) {
    let mut pos = 0usize;
    let mut acked = 0usize;
    for piece in expected {
        match text[pos..].find(piece) {
            Some(found) => {
                pos += found + piece.len();
                acked += piece.chars().count();
            }
            None => return (acked, false),
        }
    }
    (acked, true)
}

fn nanos(d: Duration) -> u64 {
    d.as_nanos().min(u64::MAX as u128) as u64
}

impl<'a> OpTransferPort<'a> {
    pub fn new(ctx: &'a OpCtx, grant: &OpGrant, cancel: &'a RunCancel) -> Self {
        OpTransferPort {
            ctx,
            grant: grant.clone(),
            cancel: cancel.watching_transfers(),
            any_rx: compile(r"[\s\S]", PyFlags(0)).expect("固定 pattern 必可編譯"),
        }
    }

    fn permit(&self, source: &str, cmd_id: Option<&str>, log: bool) -> TxPermit {
        TxPermit {
            op_id: self.grant.op_id,
            epoch: self.grant.epoch,
            owner_gen: self.grant.owner_gen,
            fsm_gen: self.grant.fsm_gen,
            class: TxClass::FileTransfer,
            source: source.to_string(),
            cmd_id: cmd_id.map(str::to_string),
            log,
            // brief 不符 #4：只有 human／flash probe 的 permit 帶這三欄。
            input_epoch: None,
            replay_hold: None,
            probe_id: None,
        }
    }

    /// 經 TxWriter 送出一筆並等 `TxReport`；writer 已停止時視為 `Frozen` 拒絕（同 `op_runner::send`）。
    fn submit(
        &self,
        payload: &[u8],
        source: &str,
        cmd_id: Option<&str>,
        log: bool,
        aggregate: Option<AggregateOp>,
    ) -> TxReport {
        let (reply, report) = crossbeam_channel::bounded(1);
        let frozen = TxReport {
            op_id: self.grant.op_id,
            epoch: self.grant.epoch,
            result: sw_pal::TxResult { outcome: TxOutcome::NotSubmitted, error: None },
            wal: None,
            reject: Some(FenceReject::Frozen),
        };
        let job = TxJob { permit: self.permit(source, cmd_id, log), payload: payload.to_vec(), reply, aggregate };
        if self.ctx.tx.jobs.send(job).is_err() {
            return frozen;
        }
        self.ctx.tx.wait_report(&report).unwrap_or(frozen)
    }

    /// 結束該行命令的聚合（緩衝以一筆 TX 落 WAL）；writer 已停止時由其收尾 flush 處理過，不重複寫。
    fn finish(&self, key: &AggregateKey) {
        let _ = self.submit(b"", &key.source, key.cmd_id.as_deref(), false, Some(AggregateOp::Finish(key.clone())));
    }

    /// 等待被取消：以一次零 TX 的 `Fence::admit` 判定此刻送出會被拒的原因（drain → Draining、quiet → Quiet）。
    fn cancelled(&self) -> TransferAbort {
        let reason = match self.ctx.tx.fence_verdict(&self.permit("", None, false)) {
            Err(reject) => classify(reject),
            Ok(()) => RejectReason::Other,
        };
        TransferAbort::rejected(TxRejection { reason, rejected_confirmed: 0, unknown: false })
    }

    /// `_await_echo_progress`：先比對再看期限；未完成時等到「有新 RX」、期限或取消後重算。逾時回部分進度。
    fn await_echo_progress(&self, expected: &[&str], from: u64, timeout: Duration) -> Result<usize, TransferAbort> {
        let clock = &*self.ctx.clock;
        let deadline = clock.mono_ns().saturating_add(nanos(timeout));
        loop {
            // 先取長度再讀文字：之後抵達的 RX 一定在 `seen` 之後，喚醒不會遺失。
            let seen = self.ctx.rx.snapshot_len();
            let text: String = strip_ansi(&self.ctx.rx.text_from(from))
                .chars()
                .filter(|c| *c != '\r' && *c != '\n')
                .collect();
            let (acked, complete) = echo_progress(&text, expected);
            let now = clock.mono_ns();
            if complete || now >= deadline {
                return Ok(acked);
            }
            let remaining = Duration::from_nanos(deadline - now);
            if self.ctx.rx.wait_for_regex_cancellable(&self.any_rx, Some(seen), remaining, clock, &self.cancel)
                == WaitOutcome::Cancelled
            {
                return Err(self.cancelled());
            }
        }
    }
}

impl TransferPort for OpTransferPort<'_> {
    fn send_command(&mut self, cmd: &str, source: &str, cmd_id: Option<&str>) -> Result<(), TransferAbort> {
        let mut payload = cmd.as_bytes().to_vec();
        if !payload.ends_with(b"\n") {
            payload.push(b'\n');
        }
        let report = self.submit(&payload, source, cmd_id, true, None);
        if fully_sent(&report, payload.len()) { Ok(()) } else { Err(rejection(&report)) }
    }

    fn supports_echo_paced(&self) -> bool {
        true
    }

    fn send_command_echo_paced(
        &mut self,
        cmd: &str,
        source: &str,
        cmd_id: Option<&str>,
        slice_size: usize,
        echo_timeout: Duration,
    ) -> Result<EchoPaced, TransferAbort> {
        // 同時去 CR：留在本文的 "\r" 會被板端當換行提早執行，且永遠 ack 不到（Python 註解）。
        let body = cmd.trim_end_matches(['\r', '\n']);
        let chars: Vec<char> = body.chars().collect();
        let slices: Vec<String> = chars.chunks(slice_size.max(1)).map(|c| c.iter().collect()).collect();
        let pieces: Vec<&str> = slices.iter().map(String::as_str).collect();
        let key = AggregateKey {
            op_id: self.grant.op_id,
            source: source.to_string(),
            cmd_id: cmd_id.map(str::to_string),
            max_bytes: body.len() + 1,
        };
        let append = || Some(AggregateOp::Append(key.clone()));
        let pre = self.ctx.rx.snapshot_len();
        let mut sent_chars = 0usize;
        let mut acked_chars = 0usize;
        for (idx, piece) in pieces.iter().enumerate() {
            let report = self.submit(piece.as_bytes(), source, cmd_id, false, append());
            if !fully_sent(&report, piece.len()) {
                self.finish(&key);
                return Err(rejection(&report));
            }
            sent_chars += piece.chars().count();
            acked_chars = match self.await_echo_progress(&pieces[..=idx], pre, echo_timeout) {
                Ok(acked) => acked,
                Err(abort) => {
                    self.finish(&key);
                    return Err(abort);
                }
            };
            if acked_chars < sent_chars {
                // echo 停滯：換行未送出、命令未執行；只記實際確認的 bytes。
                self.finish(&key);
                return Ok(EchoPaced { ok: false, acked_chars, sent_chars });
            }
        }
        let report = self.submit(b"\n", source, cmd_id, false, append());
        self.finish(&key);
        if !fully_sent(&report, 1) {
            return Err(rejection(&report));
        }
        Ok(EchoPaced { ok: true, acked_chars, sent_chars })
    }

    fn cancel_input_line(&mut self, source: &str) -> Result<(), TransferAbort> {
        let report = self.submit(b"\x15\n", source, None, true, None);
        if fully_sent(&report, 2) { Ok(()) } else { Err(rejection(&report)) }
    }

    fn rx_snapshot_len(&self) -> u64 {
        self.ctx.rx.snapshot_len()
    }

    fn rx_text_from(&self, from_offset: u64) -> String {
        self.ctx.rx.text_from(from_offset)
    }

    fn wait_for_regex_from(&mut self, re: &PyRegex, from_offset: u64, timeout: Duration) -> Result<bool, TransferAbort> {
        // EVT-D-F1：Python `while monotonic() < deadline` 在 timeout <= 0 時不做任何檢查。
        if timeout.is_zero() {
            return Ok(false);
        }
        match self.ctx.rx.wait_for_regex_cancellable(re, Some(from_offset), timeout, &*self.ctx.clock, &self.cancel) {
            WaitOutcome::Matched => Ok(true),
            WaitOutcome::TimedOut => Ok(false),
            WaitOutcome::Cancelled => Err(self.cancelled()),
        }
    }
}
