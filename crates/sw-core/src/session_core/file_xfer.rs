//! P8 Task 6：`SessionCore` 的 `file.push`／`file.pull` 整合（`session_core` 子模組，可直接操作私有欄位）。
//!
//! 對照 origin/main：`service.py::_resolve_session_id(file_transfer=True)`（resolve 層）與
//! `session_manager.py::file_push`／`file_pull`（manager 層：gate、recovering、lease、前景槽）、
//! `_file_tx_rejected_result`（傳輸中被拒的 OD-62 映射；quiet 分支在此，其餘在 OpRunner）。
//!
//! Python 的 resolve 層與 manager 層分兩次取鎖；Rust 在同一次 actor 事件內依序判定（外部回應形狀不變，兩層之間的
//! 競態窗口不存在）。通過者以 `command_fg_begin`（`_begin_foreground_operation_locked`：admission 關，agent 遇 human
//! lease 時 suspend）取得前景槽，交 OpRunner 前景 lane；結束時 `command_fg_finish`（resume、admission 開、回放）。

use sw_proto::pypath::PathFlavor;

use super::*;
use crate::file_transfer::templates::DEFAULT_ECHO_SLICE_SIZE;
use crate::file_transfer::{FileTransferKind, PullParams, PushParams, RejectReason, TransferAbort};
use crate::op_runner::{file_error_with_progress, FileTransferPlan};

/// `passthrough_file_transfer_error` 的 hint（`session_manager.py`）。
const PASSTHROUGH_FILE_HINT: &str = "passthrough profile 不支援 file.push／file.pull；請使用互動 console。";
/// `_resolve_session_id` 對 ATTACHED 且非 command-capable profile 的 hint（與 `command.submit` 相同字面量）。
const NOT_COMMAND_CAPABLE_HINT: &str = "此 profile 僅支援 console。command_capable 純粹由 profile 的 ready_probe 決定，與 session state 或 console 是否被佔用無關。要下命令：先 `serialwrap session self-test --selector <COM>` 看有無 suggested_profile，再 `serialwrap session pin --selector <COM> --profile <template>` 釘定、`serialwrap session clear --selector <COM>` 套用；或為該 profile 設定 ready_probe。";
/// `_MIN_FILE_CHUNK_TIMEOUT_S`／`_MIN_FILE_ECHO_TIMEOUT_S`／`_MIN_FILE_PULL_TIMEOUT_S`。
const MIN_FILE_CHUNK_TIMEOUT_S: f64 = 5.0;
const MIN_FILE_ECHO_TIMEOUT_S: f64 = 5.0;
const MIN_FILE_PULL_TIMEOUT_S: f64 = 30.0;
/// `READY_RECONFIRM_RETRY_S`：只剩 reconfirm pending 時的 `retry_after_s`。
const READY_RECONFIRM_RETRY_S: f64 = 5.0;

/// Python 等待逾時秒數 → `Duration`：`<= 0`／NaN 為零（EVT-D-F1：不做任何檢查即逾時），過大或無限飽和。
fn timeout_duration(seconds: f64) -> std::time::Duration {
    if seconds.is_nan() || seconds <= 0.0 {
        return std::time::Duration::ZERO;
    }
    std::time::Duration::try_from_secs_f64(seconds).unwrap_or(std::time::Duration::MAX)
}

impl SessionCore {
    /// `SessionRequest::FileTransfer`：resolve 層與 manager 層在同一次事件處理內完成；通過者取得前景槽並交 OpRunner。
    pub(super) fn file_transfer_request(
        &mut self,
        req: FileTransferRequest,
        reply: Reply,
        now: Mono,
    ) -> Vec<Effect> {
        if let Err(refused) = self.file_transfer_admit(now) {
            return vec![Effect::Reply(reply, Ok(refused))];
        }
        let plan = self.file_transfer_plan(&req);
        // `_begin_foreground_operation_locked(kind="file")`＋（agent 遇 human lease）`bridge.suspend_interactive()`；
        // RPC 路徑 `trusted_human=False`，一律比照 agent（DF-02、OD-74）。
        self.command_fg_begin(&SubmitOrigin::Rpc, false);
        let grant = self.new_grant(None);
        self.pending_ops
            .insert(grant.op_id, PendingOp::FileTransfer { reply });
        self.foreground = Some(grant.clone());
        vec![Effect::StartOp {
            grant,
            spec: OpSpec::FileTransfer(plan),
        }]
    }

    /// resolve 層（`_resolve_session_id(file_transfer=True)`）＋manager 層（`file_push`／`file_pull` 的鎖內判定）。
    /// 任一拒絕皆在第一個 TX 之前、零 TX、不占前景槽。
    fn file_transfer_admit(&mut self, now: Mono) -> Result<(), PyDict> {
        // SC-35：passthrough 任何 state（含安靜轉入的 READY）皆無 shell 傳輸契約。
        if self.is_passthrough() {
            return Err(pydict! {
                "ok" => false,
                "error_code" => "PROFILE_NOT_COMMAND_CAPABLE",
                "hint" => PASSTHROUGH_FILE_HINT,
                "session" => self.public(now, &self.public_ctx()),
            });
        }
        if self.state != SessionState::Ready {
            return Err(match self.state {
                SessionState::Recovering => self.error_session("SESSION_RECOVERING", now),
                SessionState::Flashing => self.error_session("FLASHING_BUSY", now),
                SessionState::Attached if !self.profile.template.command_capable() => pydict! {
                    "ok" => false,
                    "error_code" => "PROFILE_NOT_COMMAND_CAPABLE",
                    "hint" => NOT_COMMAND_CAPABLE_HINT,
                    "session" => self.public(now, &self.public_ctx()),
                },
                _ => self.error_session("SESSION_NOT_READY", now),
            });
        }
        // submit-time gate（#139／#162；RPC 的 source 不授權豁免）。
        self.expire_reconfirm(now);
        if self.quiet.failed || self.quiet.quiet_active(now) || self.quiet.pending {
            let public = self.public(now, &self.public_ctx());
            if let Some(refused) = crate::quiet_gate::submit_gate_reject(&public, "") {
                return Err(refused);
            }
        }
        // manager 層（Python 第二次取鎖；Rust 同一事件內不會與上層不同，仍依序保留以對齊回應形狀）。
        if self.generations.conn_epoch.is_none() {
            return Err(pydict! { "ok" => false, "error_code" => "SESSION_NOT_READY" });
        }
        if let Some(refused) = crate::quiet_gate::execute_gate_reject(&mut self.quiet, &self.profile.com, now) {
            return Err(refused);
        }
        if self.recovering {
            return Err(pydict! { "ok" => false, "error_code" => "SESSION_RECOVERING" });
        }
        // `_refresh_interactive_locked`：agent 遇 human lease → suspend（由 fg_begin 執行）；其他 lease → BUSY。
        self.refresh_lease(now);
        if self.lease.as_ref().is_some_and(|lease| !lease.is_human()) {
            return Err(pydict! { "ok" => false, "error_code" => "SESSION_INTERACTIVE_BUSY" });
        }
        // `foreground_busy or _foreground_operation is not None`（含 D-22 回放保留）：不排隊（EVT-D-F6）。
        if self.fg_slot_busy() {
            return Err(pydict! {
                "ok" => false,
                "error_code" => "SESSION_BUSY",
                "selector" => self.profile.com.clone(),
            });
        }
        Ok(())
    }

    /// 依 profile 推導 timeout 等參數（`file_push`：chunk＝`chunk_timeout_s` 或 `max(timeout_s, 5.0)`、echo＝
    /// `max(timeout_s, 5.0)`；`file_pull`：`chunk_timeout_s` 或 `max(timeout_s, 30.0)`）。
    fn file_transfer_plan(&self, req: &FileTransferRequest) -> FileTransferPlan {
        let t = &self.profile.template;
        let budget = t.max_console_line_chars.as_ref().map(|v| match v {
            PyInt::Small(n) => *n,
            PyInt::Big(digits) => digits.parse::<i128>().unwrap_or(i128::MAX),
        });
        match req.kind {
            FileTransferKind::Push => FileTransferPlan::Push(PushParams {
                local_path: req.local_path.clone().unwrap_or_default(),
                remote_path: req.remote_path.clone(),
                chunk_size: req.chunk_size,
                timeout: timeout_duration(
                    req.chunk_timeout_s
                        .unwrap_or_else(|| t.timeout_s.max(MIN_FILE_CHUNK_TIMEOUT_S)),
                ),
                prompt_regex: t.prompt_regex.clone(),
                source: req.source.clone(),
                ack_mode: req.ack_mode.clone(),
                echo_slice_size: DEFAULT_ECHO_SLICE_SIZE,
                echo_timeout: timeout_duration(t.timeout_s.max(MIN_FILE_ECHO_TIMEOUT_S)),
                max_console_line_chars: budget,
            }),
            FileTransferKind::Pull => FileTransferPlan::Pull(PullParams {
                remote_path: req.remote_path.clone(),
                local_path: req.local_path.clone(),
                timeout: timeout_duration(
                    req.chunk_timeout_s
                        .unwrap_or_else(|| t.timeout_s.max(MIN_FILE_PULL_TIMEOUT_S)),
                ),
                prompt_regex: t.prompt_regex.clone(),
                source: req.source.clone(),
                max_console_line_chars: budget,
                path_flavor: match self.path_flavor {
                    sw_pal::PalPathFlavor::Posix => PathFlavor::Posix,
                    sw_pal::PalPathFlavor::Windows => PathFlavor::Windows,
                },
            }),
        }
    }

    /// `OpOutcome::FileTransfer` 的收斂：OpRunner 已映射的錯誤原樣回；quiet 且被拒那筆零寫出 → `AUTOBOOT_QUIET`
    /// （`selector`、`retry_after_s`＝quiet 剩餘秒 round 1 位，只剩 reconfirm pending 時 5.0，再附進度）；
    /// 本地 I/O 與 prompt regex 錯誤 → framing `EXCEPTION`（Python 例外穿出 manager）。
    pub(super) fn file_transfer_result(&mut self, outcome: OpOutcome, now: Mono) -> Result<PyDict, PyErr> {
        match outcome {
            OpOutcome::FileTransfer(Ok(result)) => Ok(result),
            OpOutcome::FileTransfer(Err(TransferAbort::TxRejected { rej, chunks, .. })) => {
                if rej.reason != RejectReason::Quiet || rej.rejected_confirmed != 0 {
                    // OpRunner 只把 quiet 零寫出交回；其餘若出現視同結果不明（傳輸自身的結束碼）。
                    return Ok(file_error_with_progress("TRANSFER_TIMEOUT", chunks));
                }
                let retry_after_s = if self.quiet.quiet_active(now) {
                    py_round(
                        self.quiet.boot_quiet_until.saturating_sub(now) as f64 / 1_000_000_000.0,
                        1,
                    )
                } else {
                    READY_RECONFIRM_RETRY_S
                };
                let mut result = pydict! {
                    "ok" => false,
                    "error_code" => "AUTOBOOT_QUIET",
                    "selector" => self.profile.com.clone(),
                    "retry_after_s" => retry_after_s,
                };
                if let Some((sent, total)) = chunks {
                    result.insert("chunks_sent", py_int(sent as i128));
                    result.insert("chunks_total", py_int(total as i128));
                }
                Ok(result)
            }
            OpOutcome::FileTransfer(Err(TransferAbort::LocalIo(message)))
            | OpOutcome::FileTransfer(Err(TransferAbort::BadPromptRegex(message))) => {
                Err(PyErr::value_error(message))
            }
            // executor 無法啟動 op（該 epoch 的 worker 已結束）：零 TX，比照 drain 零寫出（OD-62）。
            OpOutcome::SendFailed => Ok(pydict! { "ok" => false, "error_code" => "FLUSHED_BY_RECOVERY" }),
            OpOutcome::Error(error) => Err(error),
            _ => Err(PyErr::value_error("file transfer 未完成")),
        }
    }
}
