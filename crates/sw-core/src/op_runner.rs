//! P5-T14：前景 op 的執行（命令 line／background、逾時救援、reboot、self-test probe、lease TX）。
//!
//! 對照 `session_manager.py` 的 `_execute_command_inner`、`_recover_after_failure`、`_handle_reboot_command`、
//! `_self_test_impl`（probe 段）、`interactive_send`／`interactive_open`（TX 段）與 `_extract_command_stdout`。
//!
//! `OpRunner` 每個 bridge epoch 一條 thread（ForegroundSlot，pool size 1）；op 的狀態副作用（ATTACHED 轉移、
//! 佇列 flush、capture 終態、quiet 清除）一律由 actor 在 `OpFinished` 時依 `OpOutcome` 套用，本模組只做 I/O。
//! TX 一律經該 epoch 的 `TxWriterHandle`，permit 的 `op_id` 為授權 grant 的 `op_id`（帳本以 op 為單位，
//! 由 actor 在終態處理點 `SettleTx`）。

use std::sync::Arc;
use std::time::Duration;

use crossbeam_channel::{Receiver, Sender};
use sw_pal::traits::{Clock, TxOutcome};
use sw_proto::config_types::{ProfileTemplate, SessionProfile};
use sw_proto::pycompat::{a2b_base64_lenient, clean_text, py_strip, splitlines, PyErr};
use sw_proto::pydict;
use sw_proto::pyjson::{PyDict, PyInt, PyValue};
use sw_proto::pyre::{compile, valid_completion_regex, PyFlags, PyRegex};

use crate::arbiter::QueuedCommand;
use crate::command_completion::{CompletionEvaluator, Evaluation as CompletionEvaluation};
use crate::file_transfer::{
    pull_file, push_file, OpTransferPort, PullParams, PushParams, RejectReason, StdLocalFiles,
    TransferAbort, TransferIds, TxRejection,
};
use crate::login_fsm::LoginOutcome;
use crate::mailbox::ControlEvent;
use crate::quiet_gate::is_reboot_command;
use crate::session_core::OpGrant;
use crate::tx_writer::{FenceReject, TxClass, TxJob, TxPermit, TxReport, TxWriterHandle};
use crate::uart_bridge::{RunCancel, RxShared, WaitMatchOutcome, WaitOutcome};
use crate::wal::{WalAppendReq, WalWriter};

/// 前景 op 的種類（`Effect::StartOp` 內容）。
pub enum OpSpec {
    Command(QueuedCommand),
    Recover {
        timeout_s: f64,
        force: bool,
    },
    SelfTestProbe {
        timeout_s: f64,
        probe: String,
        marker_nonce: String,
    },
    InteractiveOpenCmd {
        command: String,
        owner: String,
    },
    InteractiveSend {
        payload: Vec<u8>,
        owner: String,
    },
    RebootRecovery {
        hard_timeout_s: f64,
    },
    DetectTemplate {
        templates: Vec<ProfileTemplate>,
    },
    /// P8 Task 6：前景檔案傳輸（`file.push`／`file.pull`）；走前景 lane（不是 [`OpSpec::runs_beside_foreground`]）。
    FileTransfer(FileTransferPlan),
}

/// P8 Task 6：檔案傳輸 op 的參數。由 actor 依 RPC 請求與 session profile 組成（timeout 推導、`prompt_regex`、
/// `max_console_line_chars`、`path_flavor` 皆已代入），OpRunner 原樣交給 `push_file`／`pull_file`。
#[derive(Debug, Clone)]
pub enum FileTransferPlan {
    Push(PushParams),
    Pull(PullParams),
}

/// op 完成結果（`ControlEvent::OpFinished` 內容）。
pub enum OpOutcome {
    /// 命令最終結果字典（Python `execute_command` 的回傳值；含 ok:false 的送出失敗映射）。
    CommandResult(PyDict),
    SendFailed,
    /// 救援鍵（`CTRL_C`／`CTRL_D`）後 prompt 回來（`PROMPT_TIMEOUT_RECOVERED`）。
    Recovered {
        key: &'static str,
        stdout: String,
    },
    /// 未完成且救援未成功（或 passthrough 無完成條件）；`stdout` 為 `clean_text(pre_offset 起 RX)`。
    PromptTimeout {
        stdout: String,
    },
    /// echo 無法確認且同窗口出現可信 human TX；保留輸出並回 PROMPT_TIMEOUT，不降級 session 或送救援鍵。
    CompletionUncertain {
        stdout: String,
    },
    ToAttached {
        reason: &'static str,
    },
    /// reboot 命令已送出、2 s 內 prompt 未回（`EXPECT_REBOOT`）。
    RebootExpected,
    RebootRecoveryDone(LoginOutcome),
    /// self-test nonce probe 結果：`{"probe_ok": bool, "rx_bytes_last_10s": int}`（actor 組完整回應）。
    SelfTest(PyDict),
    /// R5-F2：nonce probe 的 TxReport.reject == Some(Quiet)；不等 nonce，由 actor 組 AUTOBOOT_QUIET 回應。
    SelfTestRefused(FenceReject),
    Sent {
        bytes: usize,
    },
    Detected(Option<String>),
    /// lease TX 在提交點被 fence 拒絕（#36 映射由 actor 套用）。
    SendRefused(FenceReject),
    /// Python 例外穿出 manager（framing `EXCEPTION`）。
    Error(PyErr),
    /// P8 Task 6：檔案傳輸結果。`Ok` 為引擎回應或已依 OD-62 映射的錯誤回應（`COMMAND_ABORTED_IN_FLIGHT`／
    /// `FLUSHED_BY_RECOVERY`／`TRANSFER_TIMEOUT`，META 已由 OpRunner 寫出）；`Err(TxRejected)` 只留給「quiet 且被拒
    /// 那筆零寫出」（`retry_after_s` 需 actor 的 quiet 狀態），`Err(LocalIo|BadPromptRegex)` 由 actor 轉 framing `EXCEPTION`。
    FileTransfer(Result<PyDict, TransferAbort>),
}

/// `OpRunner` 的依賴（Task 14 Produces）。
#[derive(Clone)]
pub struct OpCtx {
    pub com: String,
    pub sid: String,
    pub profile: SessionProfile,
    pub rx: Arc<RxShared>,
    /// 可被 `OpCtx` clone 持有的 TX 入口（`TxWriterHandle` 的 Sender 不因此阻止 writer 停止，見 Task 9）。
    pub tx: TxWriterHandle,
    pub wal: Arc<WalWriter>,
    pub clock: Arc<dyn Clock>,
    pub control: crossbeam_channel::Sender<ControlEvent>,
    pub rng: Arc<dyn Fn(&mut [u8]) + Send + Sync>,
    /// `command_aborted_in_flight` META 寫入權（與 drain 回條互斥；`CommandRegistry::claim_abort_meta`）。
    pub registry: Arc<crate::arbiter::CommandRegistry>,
}

/// 前景 op 執行器（thread pool size 1 per session，ForegroundSlot；Task 14 Produces）。
pub struct OpRunner {
    jobs: Sender<(OpGrant, OpSpec)>,
    /// 並行 lane（READY recover）。
    side: Sender<(OpGrant, OpSpec)>,
}

impl OpSpec {
    /// 不是 Python 前景 operation、須與前景命令並行的 op（審查 F25）：READY recover（`recover_session` →
    /// `_recover_after_failure`）照舊可與前景操作並行；self-test 由 actor 保留前景槽。
    pub fn runs_beside_foreground(&self) -> bool {
        matches!(self, OpSpec::Recover { .. })
    }
}

fn spawn_lane(ctx: OpCtx, lane: &str) -> Sender<(OpGrant, OpSpec)> {
    let (jobs, rx): (Sender<(OpGrant, OpSpec)>, Receiver<(OpGrant, OpSpec)>) =
        crossbeam_channel::unbounded();
    let name = format!("serialwrap-op{lane}-{}", ctx.com);
    std::thread::Builder::new()
        .name(name)
        .spawn(move || {
            for (grant, spec) in rx.iter() {
                let outcome = execute(&ctx, &grant, spec);
                let (wm_epoch, wm_seq) = ctx.rx.watermark();
                let _ = ctx.control.send(ControlEvent::OpFinished {
                    op_id: grant.op_id,
                    epoch: grant.epoch,
                    outcome,
                    after_rx_seq: (wm_epoch == grant.epoch).then_some(wm_seq),
                });
            }
        })
        .expect("建立 OpRunner thread");
    jobs
}

impl OpRunner {
    /// 前景 lane（命令、interactive、self-test）與並行 lane（[`OpSpec::runs_beside_foreground`]）各一條 thread；
    /// 兩者的 TX 一律經同一 epoch 的 TxWriter（單一寫入者不變）。
    pub fn spawn(ctx: OpCtx) -> OpRunner {
        let side = spawn_lane(ctx.clone(), "-side");
        let jobs = spawn_lane(ctx, "");
        OpRunner { jobs, side }
    }

    /// 以授權 `grant` 執行 `spec`；結果以 `ControlEvent::OpFinished` 回報給 actor。
    pub fn run(&self, grant: OpGrant, spec: OpSpec) {
        let lane = if spec.runs_beside_foreground() {
            &self.side
        } else {
            &self.jobs
        };
        let _ = lane.send((grant, spec));
    }
}

/// 由 raw 輸出抽出命令 stdout（05 §5.2；`_extract_command_stdout`）。
pub fn extract_command_stdout(
    raw: &str,
    command: &str,
    prompt_regex: &PyRegex,
    completion: Option<&PyRegex>,
) -> String {
    let mut lines = prompt_cut_lines(raw, prompt_regex, completion);
    strip_command_echo_lines(raw, &mut lines, command);
    lines.join("\n").trim_matches('\n').to_string()
}

fn strip_command_echo_lines(raw: &str, lines: &mut Vec<String>, command: &str) {
    if !command.is_empty() {
        let stripped = py_strip(command);
        let mut removed = false;
        while lines
            .first()
            .is_some_and(|line| py_strip(line).ends_with(stripped))
        {
            lines.remove(0);
            removed = true;
        }
        if !removed {
            // 修正 PR（fix/wrapped-echo-false-positive 34c3a84）語意：只接受「命令原文＋終端折行插入字元」。
            // 插入字元須有原始串流證據（rulings/p5.md P5-70f6ce7-1）：換行只在 raw 含 BusyBox `\r\r\n`、
            // 多一個空白只在 raw 含 readline `" \r"` 時可跳過；一般 CRLF stdout 不得拼接（sol P5-70f6ce7-1）。
            let wraps = WrapEvidence {
                newline: raw.contains("\r\r\n"),
                space: raw.contains(" \r"),
            };
            if wraps.newline || wraps.space {
                if let Some(n) = strip_wrapped_command_echo(&lines, stripped, wraps) {
                    lines.drain(..n);
                }
            }
        }
    }
}

/// `clean_text` 後依最後一個 prompt（completion 模式下只裁完成標記之後的尾 prompt）截斷並切行。
fn prompt_cut_lines(
    raw: &str,
    prompt_regex: &PyRegex,
    completion: Option<&PyRegex>,
) -> Vec<String> {
    let cleaned = clean_text(raw);
    let prompt_start = last_match_start(prompt_regex, &cleaned);
    let completion_end = completion
        .and_then(|re| re.search(raw))
        .map(|m| clean_text(&char_prefix(raw, m.end)).chars().count());
    let mut text = cleaned.clone();
    if let Some(start) = prompt_start {
        let cut = match completion {
            None => true,
            Some(_) => completion_end.is_some_and(|end| start >= end),
        };
        if cut {
            text = char_prefix(&cleaned, start);
        }
    }
    splitlines(text.trim_start_matches('\n'))
        .into_iter()
        .map(str::to_string)
        .collect()
}

/// 終端折行後每段回顯至少約 20 字元（80 欄扣掉 prompt；修正 PR `_WRAP_MIN_SEGMENT`），也以此保守估計回顯最多佔幾行。
const WRAP_MIN_SEGMENT: usize = 20;

/// 原始串流中的終端折行證據：`newline` 為 BusyBox `\r\r\n`、`space` 為 bash readline `" \r"`。
#[derive(Clone, Copy)]
pub(crate) struct WrapEvidence {
    pub(crate) newline: bool,
    pub(crate) space: bool,
}

/// 修正 PR `_match_wrapped_echo`：從 `text[start]` 比對一段可能被折行的命令回顯，成功時回傳回顯結束位置。
/// 命令的每個字元（含空白）都必須依序出現；只有換行或多出的空白（且原始串流有對應證據）可以跳過，
/// 第一個命令字元前不得插入，插入點彼此至少相隔 [`WRAP_MIN_SEGMENT`] 個字元（第一個插入點以第一行欄位計，含殘留 prompt）。
pub(crate) fn match_wrapped_echo(
    text: &[char],
    start: usize,
    command: &[char],
    wraps: WrapEvidence,
) -> Option<usize> {
    let (mut i, mut j) = (start, 0usize);
    let mut last_wrap: Option<usize> = None;
    while j < command.len() {
        let ch = *text.get(i)?;
        if ch == command[j] {
            i += 1;
            j += 1;
            continue;
        }
        let allowed = (ch == '\n' && wraps.newline) || (ch == ' ' && wraps.space);
        if j == 0 || !allowed {
            return None;
        }
        let column = last_wrap.map_or(start + j, |w| j - w);
        if column < WRAP_MIN_SEGMENT {
            return None;
        }
        last_wrap = Some(j);
        i += 1;
    }
    Some(i)
}

/// 修正 PR `_strip_wrapped_command_echo`：`lines` 已 `clean_text` 並切行；命令須從第一行開始（容許殘留 prompt），
/// 完整結束在某一行的行尾，才把這幾行視為回顯。回傳要移除的行數。
fn strip_wrapped_command_echo(
    lines: &[String],
    command: &str,
    wraps: WrapEvidence,
) -> Option<usize> {
    if command.is_empty() || lines.is_empty() {
        return None;
    }
    let cmd: Vec<char> = command.chars().collect();
    let window = &lines[..lines.len().min(cmd.len() / WRAP_MIN_SEGMENT + 2)];
    let text: Vec<char> = window.join("\n").chars().collect();
    for start in 0..lines[0].chars().count() {
        if let Some(end) = match_wrapped_echo(&text, start, &cmd, wraps) {
            if end == text.len() || text[end] == '\n' {
                return Some(text[..end].iter().filter(|c| **c == '\n').count() + 1);
            }
        }
    }
    None
}

/// Python `str.encode("ascii")` 的 `UnicodeEncodeError` 訊息：回報第一段連續非 ASCII 字元（以 code point 計位置），
/// 單一字元時附 repr 跳脫（`\xNN`／`\uNNNN`／`\UNNNNNNNN`，小寫 hex；審查 F38）。
fn ascii_encode_error_message(data: &str) -> String {
    let chars: Vec<char> = data.chars().collect();
    let start = chars.iter().position(|c| !c.is_ascii()).unwrap_or(0);
    let end = chars[start..]
        .iter()
        .position(char::is_ascii)
        .map_or(chars.len(), |n| start + n);
    if end - start == 1 {
        let code = chars[start] as u32;
        let escaped = if code < 0x100 {
            format!("\\x{code:02x}")
        } else if code < 0x10000 {
            format!("\\u{code:04x}")
        } else {
            format!("\\U{code:08x}")
        };
        format!(
            "'ascii' codec can't encode character '{escaped}' in position {start}: ordinal not in range(128)"
        )
    } else {
        format!(
            "'ascii' codec can't encode characters in position {start}-{}: ordinal not in range(128)",
            end - 1
        )
    }
}

/// `interactive.send` 的 payload 編碼（Python `_encode_interactive_payload`；不合法 → `PyErr`）。
pub fn encode_interactive_payload(data: &str, encoding: &str) -> Result<Vec<u8>, PyErr> {
    match encoding {
        "plain" => return Ok(data.as_bytes().to_vec()),
        "base64" => {
            if !data.is_ascii() {
                return Err(PyErr::value_error(ascii_encode_error_message(data)));
            }
            return a2b_base64_lenient(data.as_bytes())
                .map_err(|error| PyErr::value_error(error.to_string()));
        }
        _ => {}
    }
    let key = py_strip(data).to_lowercase();
    let mapped: Option<&[u8]> = match key.as_str() {
        "enter" => Some(b"\n"),
        "tab" => Some(b"\t"),
        "escape" => Some(b"\x1b"),
        "ctrl-c" => Some(b"\x03"),
        "ctrl-d" => Some(b"\x04"),
        "up" => Some(b"\x1b[A"),
        "down" => Some(b"\x1b[B"),
        "right" => Some(b"\x1b[C"),
        "left" => Some(b"\x1b[D"),
        _ => None,
    };
    match mapped {
        Some(bytes) if encoding == "key" => Ok(bytes.to_vec()),
        _ => Err(PyErr::value_error("INVALID_INTERACTIVE_ENCODING")),
    }
}

/// fence 拒絕原因 → Python `TxIncompleteError` 的 reason 字串（uart_io `_tx_reject_reason`）。
pub fn reject_reason(reject: FenceReject) -> &'static str {
    match reject {
        FenceReject::Frozen => "draining",
        FenceReject::Flash => "flashing",
        FenceReject::StaleEpoch => "stale_generation",
        FenceReject::Cancelled => "cancelled",
        FenceReject::Quiet | FenceReject::ReconfirmPending => "quiet",
        FenceReject::OwnerMismatch => "lease",
        FenceReject::FsmMismatch => "stale_login_gen",
        FenceReject::WalUnhealthy => "wal_unhealthy",
        // 07 D-03：只對 HumanRaw／HumanReplay 產生，op TX 不會遇到；取 Python human_raw 拒絕字串。
        FenceReject::HumanAdmissionClosed
        | FenceReject::InputStale
        | FenceReject::ReplayRevoked => "stale_owner_gen",
    }
}

/// Python `TxIncompleteError.__str__`：`tx incomplete: {outcome} {confirmed}/{requested} {error}`。
/// `outcome` 只有 Python `TxResult.outcome` 的三個字面值（`not_submitted`／`completed`／`unknown`）；部分寫出是
/// `completed` 加 `confirmed < requested`（審查 F16 順帶修正：舊版寫成 Python 不存在的 `partial`）。
pub fn tx_incomplete_message(report: &TxReport, requested: usize) -> String {
    let (outcome, confirmed) = match &report.result.outcome {
        TxOutcome::NotSubmitted => ("not_submitted", 0),
        TxOutcome::Completed(n) => ("completed", *n),
        TxOutcome::Unknown { confirmed } => ("unknown", *confirmed),
    };
    let error = match (report.reject, &report.result.error) {
        (Some(reject), _) => reject_reason(reject).to_string(),
        (None, Some(error)) => error.py_message.clone(),
        (None, None) => "None".to_string(),
    };
    format!("tx incomplete: {outcome} {confirmed}/{requested} {error}")
}

/// 送出結果是否完整提交。
fn fully_sent(report: &TxReport, requested: usize) -> bool {
    matches!(report.result.outcome, TxOutcome::Completed(n) if n >= requested)
}

fn confirmed_of(report: &TxReport) -> usize {
    match report.result.outcome {
        TxOutcome::NotSubmitted => 0,
        TxOutcome::Completed(n) => n,
        TxOutcome::Unknown { confirmed } => confirmed,
    }
}

fn execute(ctx: &OpCtx, grant: &OpGrant, spec: OpSpec) -> OpOutcome {
    match spec {
        OpSpec::Command(command) => run_command(ctx, grant, &command),
        OpSpec::Recover { timeout_s, .. } => {
            let t = &ctx.profile.template;
            let pre_offset = ctx.rx.snapshot_len();
            recover_after_failure(
                ctx,
                grant,
                timeout_s,
                "",
                &t.prompt_regex,
                pre_offset,
                t.platform == "passthrough",
                None,
                true,
            )
        }
        OpSpec::SelfTestProbe {
            timeout_s,
            probe,
            marker_nonce,
        } => run_self_test_probe(ctx, grant, timeout_s, &probe, &marker_nonce),
        OpSpec::InteractiveOpenCmd { command, owner } => {
            let payload = command_payload(&command);
            // `command.submit {mode:"interactive"}` 的命令：TX 帶原 cmd_id，未完整送出依命令帳結算（sol-backfill p5 #1）。
            lease_send(
                ctx,
                grant,
                payload,
                TxClass::InteractiveOpenCmd,
                &owner,
                grant.cmd_id.as_deref(),
            )
        }
        OpSpec::InteractiveSend { payload, owner } => {
            lease_send(ctx, grant, payload, TxClass::LeaseOwner, &owner, None)
        }
        OpSpec::RebootRecovery { .. } => OpOutcome::Error(PyErr::value_error(
            "reboot recovery 由 session actor 以登入 run 驅動（OpRunner 不承接）",
        )),
        OpSpec::DetectTemplate { .. } => OpOutcome::Detected(None),
        OpSpec::FileTransfer(plan) => run_file_transfer(ctx, grant, plan),
    }
}

/// P8 Task 6：`TransferIds`（Python `uuid.uuid4().hex[:n]`）以 `OpCtx.rng` 產生。
struct RngIds<'a> {
    rng: &'a (dyn Fn(&mut [u8]) + Send + Sync),
}

impl TransferIds for RngIds<'_> {
    fn hex(&mut self, n: usize) -> String {
        let rng = self.rng;
        let mut fill = |bytes: &mut [u8]| rng(bytes);
        sw_proto::pycompat::uuid4_hex(&mut fill)
            .chars()
            .take(n)
            .collect()
    }
}

/// P8 Task 6：檔案傳輸執行體（`session_manager.py::file_push`／`file_pull` 的 I/O 段＋`_file_tx_rejected_result`）。
///
/// 引擎經 [`OpTransferPort`] 送出（同一 grant 的 TX operation、`TxClass::FileTransfer`）；等待監看 drain Phase 1 的
/// 傳輸取消世代（T5），release／recover／shutdown 時盡快返回、不自動重送。傳輸中被拒依 OD-62 映射：
/// - quiet 且被拒那筆零寫出 → 原樣交回 actor（`AUTOBOOT_QUIET` 的 `retry_after_s` 取 actor 的 quiet 狀態）；
/// - quiet／cancelled／stale_generation／draining：整段已確認寫出 ≥1 byte → 寫 `command_aborted_in_flight` META
///   （與 drain 回條以 `claim_abort_meta` 互斥）並回 `COMMAND_ABORTED_IN_FLIGHT`；否則 `FLUSHED_BY_RECOVERY`；
/// - 其餘（flashing、WAL、owner、結果不明等）→ 傳輸自身的 `TRANSFER_TIMEOUT`。
///
/// push 在 chunk 迴圈內被拒時附 `chunks_sent`／`chunks_total`。
fn run_file_transfer(ctx: &OpCtx, grant: &OpGrant, plan: FileTransferPlan) -> OpOutcome {
    let cancel = RunCancel::new(&ctx.rx);
    let mut port = OpTransferPort::new(ctx, grant, &cancel);
    let mut ids = RngIds { rng: &*ctx.rng };
    let result = match &plan {
        FileTransferPlan::Push(p) => push_file(&mut port, &StdLocalFiles, &mut ids, p),
        FileTransferPlan::Pull(p) => pull_file(&mut port, &StdLocalFiles, &mut ids, p),
    };
    let confirmed = port.confirmed_bytes();
    OpOutcome::FileTransfer(match result {
        Err(TransferAbort::TxRejected {
            rej,
            chunks,
            transfer_cmd_id,
        }) => {
            let cmd_id = if transfer_cmd_id.is_empty() {
                format!("file:{}", grant.op_id.0)
            } else {
                transfer_cmd_id
            };
            map_file_tx_rejected(ctx, rej, chunks, cmd_id, confirmed)
        }
        other => other,
    })
}

/// `_file_tx_rejected_result` 中不需 session 狀態的部分（見 [`run_file_transfer`]）。
fn map_file_tx_rejected(
    ctx: &OpCtx,
    rej: TxRejection,
    chunks: Option<(usize, usize)>,
    transfer_cmd_id: String,
    confirmed: usize,
) -> Result<PyDict, TransferAbort> {
    let code = match rej.reason {
        RejectReason::Quiet if rej.rejected_confirmed == 0 => {
            return Err(TransferAbort::TxRejected {
                rej,
                chunks,
                transfer_cmd_id,
            });
        }
        RejectReason::Quiet
        | RejectReason::Cancelled
        | RejectReason::StaleGeneration
        | RejectReason::Draining => {
            if confirmed > 0 {
                append_transfer_aborted_meta(ctx, &transfer_cmd_id, confirmed, rej.unknown);
                "COMMAND_ABORTED_IN_FLIGHT"
            } else {
                "FLUSHED_BY_RECOVERY"
            }
        }
        RejectReason::Other => "TRANSFER_TIMEOUT",
    };
    Ok(file_error_with_progress(code, chunks))
}

/// `{"ok": False, "error_code": code, **progress}`（push chunk 迴圈內被拒時附 `chunks_sent`／`chunks_total`）。
pub fn file_error_with_progress(code: &str, chunks: Option<(usize, usize)>) -> PyDict {
    let mut d = pydict! { "ok" => false, "error_code" => code };
    if let Some((sent, total)) = chunks {
        d.insert("chunks_sent", py_int(sent as i128));
        d.insert("chunks_total", py_int(total as i128));
    }
    d
}

/// Python `_append_aborted_in_flight_meta`（file.* 與一般命令共用欄位）：`cmd_id` 為傳輸前綴、`confirmed_tx_bytes`
/// 為整段累計；被拒那筆結果不明 → `unknown`，否則該筆必未完整提交 → `partial`。只在取得 META 寫入權時寫一次。
fn append_transfer_aborted_meta(ctx: &OpCtx, cmd_id: &str, confirmed: usize, unknown: bool) {
    if !ctx.registry.claim_abort_meta(cmd_id) {
        return;
    }
    let mut meta = PyDict::new();
    meta.insert("event", PyValue::from("command_aborted_in_flight"));
    meta.insert("confirmed_tx_bytes", py_int(confirmed as i128));
    meta.insert(
        "tx_outcome",
        PyValue::from(if unknown { "unknown" } else { "partial" }),
    );
    let _ = ctx.wal.append(WalAppendReq {
        com: &ctx.com,
        dir: "META",
        source: "system",
        payload: b"",
        cmd_id: Some(cmd_id),
        loss_flag: false,
        meta: Some(meta),
    });
}

fn command_payload(command: &str) -> Vec<u8> {
    let mut payload = command.as_bytes().to_vec();
    if !payload.ends_with(b"\n") {
        payload.push(b'\n');
    }
    payload
}

/// self-test 專用 shell 框架；printf 的格式字串與隨機 marker nonce 分開，TX 回顯不含完整預期標記。
pub(crate) fn self_test_probe_payload(probe: &str, marker_nonce: &str) -> Vec<u8> {
    command_payload(&format!(
        "( {probe} ) && printf '\\n__SW_ST_%s_OK__\\n' '{marker_nonce}'"
    ))
}

/// 經 TxWriter 送出一筆 TX 並等待 `TxReport`（writer 已停止時視為 `Frozen` 拒絕）。
fn send(
    ctx: &OpCtx,
    grant: &OpGrant,
    payload: &[u8],
    class: TxClass,
    source: &str,
    cmd_id: Option<&str>,
    log: bool,
) -> TxReport {
    let (reply, report) = crossbeam_channel::bounded(1);
    let permit = TxPermit {
        op_id: grant.op_id,
        epoch: grant.epoch,
        owner_gen: grant.owner_gen,
        fsm_gen: grant.fsm_gen,
        class,
        source: source.to_string(),
        cmd_id: cmd_id.map(str::to_string),
        log,
        // 07 D-03：06 既有 permit 建構點一律 `None`。
        input_epoch: None,
        replay_hold: None,
        probe_id: None,
    };
    let frozen = TxReport {
        op_id: grant.op_id,
        epoch: grant.epoch,
        result: sw_pal::TxResult {
            outcome: TxOutcome::NotSubmitted,
            error: None,
        },
        wal: None,
        reject: Some(FenceReject::Frozen),
    };
    let job = TxJob {
        permit,
        payload: payload.to_vec(),
        reply,
        aggregate: None,
    };
    if ctx.tx.jobs.send(job).is_err() {
        return frozen;
    }
    ctx.tx.wait_report(&report).unwrap_or(frozen)
}

fn compile_or_never(pattern: &str) -> PyRegex {
    compile(pattern, PyFlags(0))
        .or_else(|_| compile("(?!)", PyFlags(0)))
        .expect("(?!) 必可編譯")
}

fn secs(value: f64) -> Duration {
    Duration::try_from_secs_f64(value.max(0.0)).unwrap_or(Duration::MAX)
}

/// `wait_for_regex_from(pattern, offset, timeout)`（期限依注入 Clock）。
fn wait_regex_from(ctx: &OpCtx, re: &PyRegex, from: u64, timeout_s: f64) -> bool {
    ctx.rx
        .wait_for_regex_from(re, from, secs(timeout_s), &*ctx.clock)
        .is_some()
}

/// Python `_map_aborted_result`（非 drain 中止）：TX 未完整提交時的命令結果碼。
fn map_send_failure(ctx: &OpCtx, report: &TxReport, requested: usize, cmd_id: &str) -> PyDict {
    let confirmed = confirmed_of(report);
    let unknown = matches!(report.result.outcome, TxOutcome::Unknown { .. });
    if confirmed > 0 {
        let tx_outcome = if unknown {
            "unknown"
        } else if confirmed < requested {
            "partial"
        } else {
            "complete"
        };
        let mut meta = PyDict::new();
        meta.insert("event", PyValue::from("command_aborted_in_flight"));
        meta.insert("confirmed_tx_bytes", py_int(confirmed as i128));
        meta.insert("tx_outcome", PyValue::from(tx_outcome));
        if ctx.registry.claim_abort_meta(cmd_id) {
            let _ = ctx.wal.append(WalAppendReq {
                com: &ctx.com,
                dir: "META",
                source: "system",
                payload: b"",
                cmd_id: Some(cmd_id),
                loss_flag: false,
                meta: Some(meta),
            });
        }
        return pydict! { "ok" => false, "error_code" => "COMMAND_ABORTED_IN_FLIGHT" };
    }
    if unknown {
        return pydict! { "ok" => false, "error_code" => "SEND_FAILED" };
    }
    let code = match report.reject {
        Some(FenceReject::Quiet | FenceReject::ReconfirmPending) => "AUTOBOOT_QUIET",
        Some(FenceReject::Cancelled | FenceReject::StaleEpoch | FenceReject::Frozen) => {
            "FLUSHED_BY_RECOVERY"
        }
        _ => "SEND_FAILED",
    };
    pydict! { "ok" => false, "error_code" => code }
}

fn normalized_mode(mode: &str) -> &str {
    match mode {
        "fg" => "line",
        "bg" => "background",
        other => other,
    }
}

/// passthrough 完成條件（`_passthrough_completion_pattern`）：completion_regex → 可用 prompt → None。
fn passthrough_completion_pattern(t: &ProfileTemplate, completion: Option<&str>) -> Option<String> {
    if let Some(pattern) = completion {
        return Some(pattern.to_string());
    }
    valid_completion_regex(Some(&PyValue::from(t.prompt_regex.clone())))
        .then(|| t.prompt_regex.clone())
}

/// 命令 TX 類別（Python `session_manager.py:6603-6607`、`:6414-6419` 的 `"human_line" if human else "agent"`）：
/// 只看 `SubmitReq::is_trusted_human()`（RPC 依原始 source 判定的 `trusted_human` OR `HumanConsole`），永不看 source 字首。
fn command_tx_class(q: &QueuedCommand) -> TxClass {
    if q.req.is_trusted_human() {
        TxClass::HumanLine
    } else {
        TxClass::Command
    }
}

fn run_command(ctx: &OpCtx, grant: &OpGrant, q: &QueuedCommand) -> OpOutcome {
    let t = &ctx.profile.template;
    let mode = normalized_mode(&q.req.mode).to_string();
    let command = q.req.command.as_str();
    let cmd_id = q.cmd_id.as_str();
    let timeout_s = q.req.timeout_s;
    let completion_regex = q.req.completion_regex.as_deref();
    let passthrough = t.platform == "passthrough";
    // G3（Python `session_manager.py:6840-6868`）：`expect_reboot` 不看命令文字、也不排除 passthrough，
    // 一律走 reboot 分支；命令文字猜法只對非 passthrough 生效。分流先於 completion pattern 與
    // `command_completion` META，故旗標優先於 `completion_regex`。
    let guessed_reboot = !passthrough && is_reboot_command(command);
    if q.req.expect_reboot {
        // Python `session_manager.py:6842-6851`：旗標 true 即於分流與命令 TX 之前寫稽核 META（不看是否猜中、
        // 不排除 passthrough）；契約 `contracts/wal_meta_events.schema.json` 的 `command_expect_reboot`。
        let mut meta = PyDict::new();
        meta.insert("event", PyValue::from("command_expect_reboot"));
        meta.insert("expect_reboot", PyValue::Bool(true));
        let _ = ctx.wal.append(WalAppendReq {
            com: &ctx.com,
            dir: "META",
            source: "system",
            payload: b"",
            cmd_id: Some(cmd_id),
            loss_flag: false,
            meta: Some(meta),
        });
    }
    if q.req.expect_reboot || guessed_reboot {
        // Python `session_manager.py:6860-6864`：旗標且非猜中（串接命令、passthrough）→ prompt 何時回來由
        // `--cmd-timeout`／`--expected-duration` 界定；猜中的單獨 reboot 傳 `None`，維持 `min(timeout, 2 s)`。
        // `max(timeout_s, expected_duration_s or 0.0)` 依 Python `max` 語意：後者嚴格較大才取後者（NaN 不勝出）。
        let prompt_wait_s = (q.req.expect_reboot && !guessed_reboot).then(|| {
            let expected = q.req.expected_duration_s.unwrap_or(0.0);
            if expected > timeout_s {
                expected
            } else {
                timeout_s
            }
        });
        return run_reboot(ctx, grant, q, &mode, prompt_wait_s);
    }
    let completion_pattern = if passthrough {
        passthrough_completion_pattern(t, completion_regex)
    } else {
        Some(completion_regex.unwrap_or(&t.prompt_regex).to_string())
    };
    let pre_tx_offset = ctx.rx.snapshot_len();
    let human_tx_before_send = ctx.rx.human_tx_state();
    let human_snapshot_before_send = if human_tx_before_send.generation > 0 {
        ctx.rx.snapshot_from(human_tx_before_send.rx_offset).text
    } else {
        String::new()
    };
    let echo_prompt_re = (!passthrough
        && completion_regex.is_none()
        && !q.req.is_trusted_human()
        && matches!(mode.as_str(), "line" | "background"))
    .then(|| compile_or_never(&t.prompt_regex));
    let echo_evaluator = echo_prompt_re
        .as_ref()
        .map(|prompt_re| CompletionEvaluator::new(command, prompt_re));
    if let Some(pattern) = completion_regex {
        let mut meta = PyDict::new();
        meta.insert("event", PyValue::from("command_completion"));
        meta.insert("completion_regex", PyValue::from(pattern));
        let _ = ctx.wal.append(WalAppendReq {
            com: &ctx.com,
            dir: "META",
            source: "system",
            payload: b"",
            cmd_id: Some(cmd_id),
            loss_flag: false,
            meta: Some(meta),
        });
    }
    let payload = command_payload(command);
    let report = send(
        ctx,
        grant,
        &payload,
        command_tx_class(q),
        &q.req.source,
        Some(cmd_id),
        true,
    );
    if !fully_sent(&report, payload.len()) {
        return OpOutcome::CommandResult(map_send_failure(ctx, &report, payload.len(), cmd_id));
    }
    let command_boundary = echo_evaluator
        .as_ref()
        .and_then(|_| ctx.rx.command_tx_boundary(grant.op_id.0));
    let pre_offset = command_boundary
        .as_ref()
        .map_or(pre_tx_offset, |boundary| boundary.rx_offset);
    let human_tx_before = command_boundary
        .as_ref()
        .map_or(human_tx_before_send, |boundary| boundary.human_tx_state);
    let human_snapshot_at_boundary = command_boundary
        .map(|boundary| boundary.human_snapshot.text)
        .unwrap_or(human_snapshot_before_send);
    let pending_human_before = echo_evaluator.as_ref().is_some_and(|evaluator| {
        human_tx_before.generation > 0
            && (human_tx_before.raw_line_open
                || !evaluator.has_tail_prompt(&human_snapshot_at_boundary))
    });

    // heartbeat／keepalive 迴圈（有 RX 就續等；整段 wait_chunk 無新 RX 即視為靜默結束）。
    let effective_timeout = match q.req.expected_duration_s {
        Some(expected) => timeout_s.max(expected),
        None => timeout_s,
    };
    let silence_limit = timeout_s.min(30.0);
    let deadline = ctx
        .clock
        .mono_ns()
        .saturating_add((effective_timeout.max(0.0) * 1_000_000_000.0) as u64);
    let wait_re = compile_or_never(completion_pattern.as_deref().unwrap_or("(?!)"));
    let mut matched = false;
    let mut completion_stdout = None;
    let mut uncertain_stdout = None;
    loop {
        let now = ctx.clock.mono_ns();
        if now >= deadline {
            break;
        }
        let remaining = (deadline - now) as f64 / 1_000_000_000.0;
        let wait_chunk = silence_limit.min(remaining);
        let pre_rx = ctx.rx.snapshot_len();
        if let Some(evaluator) = echo_evaluator.as_ref() {
            let hit = ctx.rx.wait_for_snapshot_from(
                pre_offset,
                secs(wait_chunk),
                &*ctx.clock,
                |snapshot| {
                    match evaluator.evaluate(
                        &snapshot.text,
                        pending_human_before
                            || ctx.rx.human_tx_state().generation > human_tx_before.generation,
                        human_tx_before.raw_line_open
                            || ctx.rx.human_tx_state().raw_line_open,
                    ) {
                        CompletionEvaluation::Complete { stdout } => Some(stdout),
                        CompletionEvaluation::Pending | CompletionEvaluation::Uncertain { .. } => {
                            None
                        }
                    }
                },
            );
            if let Some((_snapshot, stdout)) = hit {
                completion_stdout = Some(stdout);
                matched = true;
                break;
            }
            let snapshot = ctx.rx.snapshot_from(pre_offset);
            match evaluator.evaluate(
                &snapshot.text,
                pending_human_before
                    || ctx.rx.human_tx_state().generation > human_tx_before.generation,
                human_tx_before.raw_line_open || ctx.rx.human_tx_state().raw_line_open,
            ) {
                CompletionEvaluation::Complete { stdout } => {
                    completion_stdout = Some(stdout);
                    matched = true;
                    break;
                }
                CompletionEvaluation::Uncertain { stdout } => uncertain_stdout = Some(stdout),
                CompletionEvaluation::Pending => uncertain_stdout = None,
            }
        } else if wait_regex_from(ctx, &wait_re, pre_offset, wait_chunk) {
            matched = true;
            break;
        }
        if ctx.rx.snapshot_len() == pre_rx {
            break;
        }
    }
    if !matched && uncertain_stdout.is_none() {
        if let Some(evaluator) = echo_evaluator.as_ref() {
            let snapshot = ctx.rx.snapshot_from(pre_offset);
            if let CompletionEvaluation::Uncertain { stdout } = evaluator.evaluate(
                &snapshot.text,
                pending_human_before
                    || ctx.rx.human_tx_state().generation > human_tx_before.generation,
                human_tx_before.raw_line_open || ctx.rx.human_tx_state().raw_line_open,
            ) {
                uncertain_stdout = Some(stdout);
            }
        }
    }
    if !matched {
        if let Some(stdout) = uncertain_stdout {
            return OpOutcome::CompletionUncertain { stdout };
        }
        if q.req.is_trusted_human() {
            // `_recover_after_failure` human 分支（DF-02／OD-74，Python `session_manager.py:6988-7012`）：可信 human
            // ＝RPC 依原始 source 判定的 `trusted_human` OR line editor 的 `HumanConsole`（rulings s2 D5），永不看
            // source 字首；不送救援鍵（CTRL_C／CTRL_D），actor 依前景快照改開 human lease。明寫 `human:<x>` 的
            // RPC（`trusted_human=false`）照常進下方 agent 救援。
            return OpOutcome::PromptTimeout {
                stdout: clean_text(&ctx.rx.text_from(pre_offset)),
            };
        }
        return recover_after_failure(
            ctx,
            grant,
            timeout_s,
            command,
            &t.prompt_regex,
            pre_offset,
            passthrough,
            Some((cmd_id, confirmed_of(&report))),
            false,
        );
    }
    let stdout = if let Some(stdout) = completion_stdout {
        stdout
    } else {
        let raw = ctx.rx.text_from(pre_offset);
        let prompt_valid = valid_completion_regex(Some(&PyValue::from(t.prompt_regex.clone())));
        match completion_regex {
            Some(pattern) if passthrough && !prompt_valid => {
                let end = compile(pattern, PyFlags(0))
                    .ok()
                    .and_then(|re| re.search(&raw))
                    .map(|m| m.end);
                match end {
                    Some(end) => clean_text(&char_prefix(&raw, end)),
                    None => clean_text(&raw),
                }
            }
            _ => {
                let prompt_re = compile_or_never(&t.prompt_regex);
                let completion_re = completion_regex.map(compile_or_never);
                extract_command_stdout(&raw, command, &prompt_re, completion_re.as_ref())
            }
        }
    };
    let mut result = pydict! {
        "ok" => true,
        "execution_mode" => mode.clone(),
        "stdout" => stdout,
        "partial" => false,
    };
    if mode == "background" {
        result.insert("background_capture_id", PyValue::from(cmd_id));
    }
    OpOutcome::CommandResult(result)
}

/// `_handle_reboot_command`（agent 分支）：送出後 quiet 由 actor 於 TX 結算時 arm（#55）；
/// prompt 等待期限內 prompt 回來 → ok／partial:false（actor 只清 TX 靜默維度，#162）；否則 `RebootExpected`。
/// 期限（Python `:6394`、`:6448`）：`prompt_wait_s` 為 `None`（猜中的單獨 reboot）時取 `min(timeout, 2 s)`，
/// 否則取呼叫端給的值（旗標且非猜中：`max(timeout_s, expected_duration_s or 0)`）。
fn run_reboot(
    ctx: &OpCtx,
    grant: &OpGrant,
    q: &QueuedCommand,
    mode: &str,
    prompt_wait_s: Option<f64>,
) -> OpOutcome {
    let t = &ctx.profile.template;
    let command = q.req.command.as_str();
    if t.console_unlock_env
        .as_deref()
        .is_some_and(|s| !s.is_empty())
    {
        // G10（Python `session_manager.py:6421-6423`）：已重開的舊 root prompt 不可在 recovery 中被當成解鎖證據。
        ctx.rx.clear();
    }
    let pre_offset = ctx.rx.snapshot_len();
    let payload = command_payload(command);
    let report = send(
        ctx,
        grant,
        &payload,
        command_tx_class(q),
        &q.req.source,
        Some(&q.cmd_id),
        true,
    );
    if !fully_sent(&report, payload.len()) {
        return OpOutcome::CommandResult(map_send_failure(ctx, &report, payload.len(), &q.cmd_id));
    }
    let prompt_re = compile_or_never(&t.prompt_regex);
    let prompt_wait = prompt_wait_s.unwrap_or(q.req.timeout_s.min(2.0));
    // Python `deadline = monotonic() + prompt_wait`（`uart_io.py:2635-2648`）不會溢位：超出 `Duration` 範圍的期限
    // （`expected_duration_s` 為 1e300／inf）飽和為無限等待，不得 panic；NaN／負值 → 0（Python 立即逾時）。
    let prompt_wait = Duration::try_from_secs_f64(prompt_wait.max(0.0)).unwrap_or(Duration::MAX);
    if ctx
        .rx
        .wait_for_regex_from(&prompt_re, pre_offset, prompt_wait, &*ctx.clock)
        .is_some()
    {
        let raw = ctx.rx.text_from(pre_offset);
        let stdout = extract_command_stdout(&raw, command, &prompt_re, None);
        return OpOutcome::CommandResult(pydict! {
            "ok" => true,
            "execution_mode" => mode,
            "stdout" => stdout,
            "partial" => false,
        });
    }
    OpOutcome::RebootExpected
}

/// `_recover_after_failure`（agent／system:recover 分支；人類來源不適用，DF-02）。
/// passthrough 不送救援鍵（SC-35）；quiet 由 fence 在每次寫入前重驗（`Quiet` 拒絕即停止）。
/// `aborted`：命令路徑的 `(cmd_id, 命令本身已確認的 TX bytes)`，救援鍵遇其他拒絕時依 Python `_map_aborted_result`
/// 結算（審查 F26）；READY recover 為 `None`（維持例外）。
#[allow(clippy::too_many_arguments)]
fn recover_after_failure(
    ctx: &OpCtx,
    grant: &OpGrant,
    timeout_s: f64,
    command: &str,
    prompt_regex: &str,
    pre_offset: u64,
    passthrough: bool,
    aborted: Option<(&str, usize)>,
    shutdown_cancellable: bool,
) -> OpOutcome {
    if passthrough {
        return OpOutcome::PromptTimeout {
            stdout: clean_text(&ctx.rx.text_from(pre_offset)),
        };
    }
    let prompt_re = compile_or_never(prompt_regex);
    for (name, key) in [("CTRL_C", b"\x03"), ("CTRL_D", b"\x04")] {
        let offset = ctx.rx.snapshot_len();
        let report = send(
            ctx,
            grant,
            key,
            TxClass::SystemRecoverKey,
            "system:recover",
            None,
            true,
        );
        if !fully_sent(&report, 1) {
            match report.reject {
                Some(FenceReject::Quiet | FenceReject::ReconfirmPending) => break,
                Some(FenceReject::Frozen | FenceReject::Cancelled | FenceReject::StaleEpoch) => {
                    return OpOutcome::CommandResult(
                        pydict! { "ok" => false, "error_code" => "FLUSHED_BY_RECOVERY" },
                    );
                }
                _ => {
                    if let Some((cmd_id, confirmed)) = aborted.filter(|(_, n)| *n > 0) {
                        // Python：TxIncompleteError → `execute_command` → `_map_aborted_result(cmd_id, exc)`；
                        // `tx_account(cmd_id)` 含命令本身的 bytes，救援鍵結果非完整 → `partial`（Unknown → `unknown`）。
                        let tx_outcome =
                            if matches!(report.result.outcome, TxOutcome::Unknown { .. }) {
                                "unknown"
                            } else {
                                "partial"
                            };
                        let mut meta = PyDict::new();
                        meta.insert("event", PyValue::from("command_aborted_in_flight"));
                        meta.insert("confirmed_tx_bytes", py_int(confirmed as i128));
                        meta.insert("tx_outcome", PyValue::from(tx_outcome));
                        if ctx.registry.claim_abort_meta(cmd_id) {
                            let _ = ctx.wal.append(WalAppendReq {
                                com: &ctx.com,
                                dir: "META",
                                source: "system",
                                payload: b"",
                                cmd_id: Some(cmd_id),
                                loss_flag: false,
                                meta: Some(meta),
                            });
                        }
                        return OpOutcome::CommandResult(
                            pydict! { "ok" => false, "error_code" => "COMMAND_ABORTED_IN_FLIGHT" },
                        );
                    }
                    return OpOutcome::Error(PyErr::value_error(tx_incomplete_message(&report, 1)));
                }
            }
        }
        let matched = if shutdown_cancellable {
            match ctx.rx.wait_for_regex_from_shutdown_cancellable(
                &prompt_re,
                offset,
                secs(timeout_s.min(2.0)),
                &*ctx.clock,
            ) {
                WaitOutcome::Matched => true,
                WaitOutcome::TimedOut => false,
                WaitOutcome::Cancelled => {
                    return OpOutcome::CommandResult(
                        pydict! { "ok" => false, "error_code" => "FLUSHED_BY_SHUTDOWN" },
                    );
                }
            }
        } else {
            wait_regex_from(ctx, &prompt_re, offset, timeout_s.min(2.0))
        };
        if matched {
            let raw = ctx.rx.text_from(pre_offset);
            let stdout = extract_command_stdout(&raw, command, &prompt_re, None);
            return OpOutcome::Recovered { key: name, stdout };
        }
    }
    OpOutcome::PromptTimeout {
        stdout: clean_text(&ctx.rx.text_from(pre_offset)),
    }
}

/// `_self_test_impl` 的 execution-marker probe 段（pre-check 與 reservation 由 actor 完成）。
fn run_self_test_probe(
    ctx: &OpCtx,
    grant: &OpGrant,
    timeout_s: f64,
    probe: &str,
    marker_nonce: &str,
) -> OpOutcome {
    let offset = ctx.rx.snapshot_len();
    let payload = self_test_probe_payload(probe, marker_nonce);
    let expected_marker = format!("__SW_ST_{marker_nonce}_OK__");
    if String::from_utf8_lossy(&payload).contains(&expected_marker) {
        return OpOutcome::SelfTest(self_test_evidence_outcome(false, false, false, false, 0));
    }
    let marker_re = compile_or_never(&format!(r"(?m)^__SW_ST_{marker_nonce}_OK__\r?\n"));
    let report = send(
        ctx,
        grant,
        &payload,
        TxClass::SelfTestProbe,
        "system:self_test",
        None,
        true,
    );
    let tx_complete = fully_sent(&report, payload.len());
    if !tx_complete {
        match report.reject {
            // RPC-H-07 KEEP：flash 拒絕照等 probe 證據（沒有本次 TX 不可能確認成功）。
            Some(FenceReject::Flash) => {}
            Some(reject) => return OpOutcome::SelfTestRefused(reject),
            None => {
                return OpOutcome::Error(PyErr::value_error(tx_incomplete_message(
                    &report,
                    payload.len(),
                )));
            }
        }
    }
    let (execution_marker, probe_rc_zero, prompt_after_marker) =
        match ctx.rx.wait_for_regex_end_from_shutdown_cancellable(
            &marker_re,
            offset,
            secs(timeout_s),
            &*ctx.clock,
        ) {
            WaitMatchOutcome::Matched { end } => {
                let prompt_re = compile_or_never(&ctx.profile.template.prompt_regex);
                // marker regex 已消耗結尾 LF；回退到該行終止符起點，保留自訂 prompt regex 所需的 CRLF／LF 行界。
                // prompt match 必須延伸到 marker 結尾之後，避免只匹配 marker 的行終止符就算確認成功。
                let marker_tail = ctx.rx.text_from(end.saturating_sub(2));
                let prompt_from = if marker_tail.starts_with("\r\n") {
                    end.saturating_sub(2)
                } else {
                    end.saturating_sub(1)
                };
                let prompt = match ctx.rx.wait_for_regex_end_after_from_shutdown_cancellable(
                    &prompt_re,
                    prompt_from,
                    end,
                    secs(timeout_s),
                    &*ctx.clock,
                ) {
                    WaitMatchOutcome::Matched { end: prompt_end } => prompt_end > end,
                    WaitMatchOutcome::TimedOut => false,
                    WaitMatchOutcome::Cancelled => {
                        return OpOutcome::SelfTestRefused(FenceReject::Cancelled);
                    }
                };
                (true, true, prompt)
            }
            WaitMatchOutcome::TimedOut => (false, false, false),
            WaitMatchOutcome::Cancelled => {
                return OpOutcome::SelfTestRefused(FenceReject::Cancelled);
            }
        };
    OpOutcome::SelfTest(self_test_evidence_outcome(
        tx_complete,
        execution_marker,
        probe_rc_zero,
        prompt_after_marker,
        ctx.rx.rx_stats(ctx.clock.mono_ns()).0,
    ))
}

fn self_test_evidence_outcome(
    tx_complete: bool,
    execution_marker: bool,
    probe_rc_zero: bool,
    prompt_after_marker: bool,
    rx_bytes_last_10s: u64,
) -> PyDict {
    let probe_ok = tx_complete && execution_marker && probe_rc_zero && prompt_after_marker;
    let mut result = pydict! {
        "probe_ok" => probe_ok,
        "probe_evidence" => pydict! {
            "tx_complete" => tx_complete,
            "execution_marker" => execution_marker,
            "probe_rc_zero" => probe_rc_zero,
            "prompt_after_marker" => prompt_after_marker,
        },
    };
    result.insert("rx_bytes_last_10s", py_int(rx_bytes_last_10s as i128));
    result
}

/// lease 擁有者 TX（`interactive_send`／`interactive_open` 的 command）。
///
/// `cmd_id` 為 `Some` 時（interactive 模式命令）：TX WAL 帶該 cmd_id；已確認 bytes >0 或結果 Unknown 時以
/// [`map_send_failure`] 結算（Python `_map_aborted_result`：`COMMAND_ABORTED_IN_FLIGHT`＋META）。
fn lease_send(
    ctx: &OpCtx,
    grant: &OpGrant,
    payload: Vec<u8>,
    class: TxClass,
    owner: &str,
    cmd_id: Option<&str>,
) -> OpOutcome {
    let report = send(ctx, grant, &payload, class, owner, cmd_id, true);
    if fully_sent(&report, payload.len()) {
        return OpOutcome::Sent {
            bytes: payload.len(),
        };
    }
    if let Some(cmd_id) = cmd_id {
        let unknown = matches!(report.result.outcome, TxOutcome::Unknown { .. });
        if confirmed_of(&report) > 0 || unknown || report.reject.is_none() {
            return OpOutcome::CommandResult(map_send_failure(ctx, &report, payload.len(), cmd_id));
        }
    }
    match report.reject {
        Some(reject) => OpOutcome::SendRefused(reject),
        None => OpOutcome::Error(PyErr::value_error(tx_incomplete_message(
            &report,
            payload.len(),
        ))),
    }
}

fn last_match_start(re: &PyRegex, text: &str) -> Option<usize> {
    let total = text.chars().count();
    let mut pos = 0usize;
    let mut last = None;
    while pos <= total {
        let Some(m) = re.search_from(text, pos) else {
            break;
        };
        last = Some(m.start);
        pos = if m.end > m.start { m.end } else { m.end + 1 };
    }
    last
}

fn char_prefix(text: &str, chars: usize) -> String {
    text.chars().take(chars).collect()
}

fn py_int(value: i128) -> PyValue {
    PyValue::Int(PyInt::Small(value))
}
