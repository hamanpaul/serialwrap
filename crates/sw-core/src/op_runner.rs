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
use std::thread::JoinHandle;
use std::time::Duration;

use crossbeam_channel::{Receiver, Sender};
use sw_pal::traits::{Clock, TxOutcome};
use sw_proto::config_types::{ProfileTemplate, SessionProfile};
use sw_proto::pycompat::{PyErr, a2b_base64_lenient, clean_text, py_strip, splitlines};
use sw_proto::pydict;
use sw_proto::pyjson::{PyDict, PyInt, PyValue};
use sw_proto::pyre::{PyFlags, PyRegex, compile, valid_completion_regex};

use crate::arbiter::QueuedCommand;
use crate::login_fsm::LoginOutcome;
use crate::mailbox::ControlEvent;
use crate::quiet_gate::is_reboot_command;
use crate::session_core::OpGrant;
use crate::tx_writer::{FenceReject, TxClass, TxJob, TxPermit, TxReport, TxWriterHandle};
use crate::uart_bridge::RxShared;
use crate::wal::{WalAppendReq, WalWriter};

/// 前景 op 的種類（`Effect::StartOp` 內容）。
pub enum OpSpec {
    Command(QueuedCommand),
    Recover { timeout_s: f64, force: bool },
    SelfTestProbe { timeout_s: f64, probe: String, nonce: String },
    InteractiveOpenCmd { command: String, owner: String },
    InteractiveSend { payload: Vec<u8>, owner: String },
    RebootRecovery { hard_timeout_s: f64 },
    DetectTemplate { templates: Vec<ProfileTemplate> },
}

/// op 完成結果（`ControlEvent::OpFinished` 內容）。
pub enum OpOutcome {
    /// 命令最終結果字典（Python `execute_command` 的回傳值；含 ok:false 的送出失敗映射）。
    CommandResult(PyDict),
    SendFailed,
    /// 救援鍵（`CTRL_C`／`CTRL_D`）後 prompt 回來（`PROMPT_TIMEOUT_RECOVERED`）。
    Recovered { key: &'static str, stdout: String },
    /// 未完成且救援未成功（或 passthrough 無完成條件）；`stdout` 為 `clean_text(pre_offset 起 RX)`。
    PromptTimeout { stdout: String },
    ToAttached { reason: &'static str },
    /// reboot 命令已送出、2 s 內 prompt 未回（`EXPECT_REBOOT`）。
    RebootExpected,
    RebootRecoveryDone(LoginOutcome),
    /// self-test nonce probe 結果：`{"probe_ok": bool, "rx_bytes_last_10s": int}`（actor 組完整回應）。
    SelfTest(PyDict),
    /// R5-F2：nonce probe 的 TxReport.reject == Some(Quiet)；不等 nonce，由 actor 組 AUTOBOOT_QUIET 回應。
    SelfTestRefused(FenceReject),
    Sent { bytes: usize },
    Detected(Option<String>),
    /// lease TX 在提交點被 fence 拒絕（#36 映射由 actor 套用）。
    SendRefused(FenceReject),
    /// Python 例外穿出 manager（framing `EXCEPTION`）。
    Error(PyErr),
}

/// `OpRunner` 的依賴（Task 14 Produces）。
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
}

/// 前景 op 執行器（thread pool size 1 per session，ForegroundSlot；Task 14 Produces）。
pub struct OpRunner {
    jobs: Sender<(OpGrant, OpSpec)>,
    _join: JoinHandle<()>,
}

impl OpRunner {
    pub fn spawn(ctx: OpCtx) -> OpRunner {
        let (jobs, rx): (Sender<(OpGrant, OpSpec)>, Receiver<(OpGrant, OpSpec)>) =
            crossbeam_channel::unbounded();
        let name = format!("serialwrap-op-{}", ctx.com);
        let join = std::thread::Builder::new()
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
        OpRunner { jobs, _join: join }
    }

    /// 以授權 `grant` 執行 `spec`；結果以 `ControlEvent::OpFinished` 回報給 actor。
    pub fn run(&self, grant: OpGrant, spec: OpSpec) {
        let _ = self.jobs.send((grant, spec));
    }
}

/// 由 raw 輸出抽出命令 stdout（05 §5.2；`_extract_command_stdout`）。
pub fn extract_command_stdout(
    raw: &str,
    command: &str,
    prompt_regex: &PyRegex,
    completion: Option<&PyRegex>,
) -> String {
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
    let trimmed = text.trim_start_matches('\n');
    let mut lines: Vec<&str> = splitlines(trimmed);
    if !command.is_empty() {
        let stripped = py_strip(command);
        while lines
            .first()
            .is_some_and(|line| py_strip(line).ends_with(stripped))
        {
            lines.remove(0);
        }
    }
    lines.join("\n").trim_matches('\n').to_string()
}

/// `interactive.send` 的 payload 編碼（Python `_encode_interactive_payload`；不合法 → `PyErr`）。
pub fn encode_interactive_payload(data: &str, encoding: &str) -> Result<Vec<u8>, PyErr> {
    match encoding {
        "plain" => return Ok(data.as_bytes().to_vec()),
        "base64" => {
            if !data.is_ascii() {
                return Err(PyErr::value_error(
                    "'ascii' codec can't encode characters: ordinal not in range(128)",
                ));
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
        // P6 增補 #4：human lane 的拒絕原因（P6 Task 6 交付映射；06 既有 class 不會產生）。
        FenceReject::HumanAdmissionClosed | FenceReject::InputStale | FenceReject::ReplayRevoked => {
            todo!("P6 Task 6")
        }
    }
}

/// Python `TxIncompleteError.__str__`：`tx incomplete: {outcome} {confirmed}/{requested} {error}`。
pub fn tx_incomplete_message(report: &TxReport, requested: usize) -> String {
    let (outcome, confirmed) = match &report.result.outcome {
        TxOutcome::NotSubmitted => ("not_submitted", 0),
        TxOutcome::Completed(n) => ("partial", *n),
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
            )
        }
        OpSpec::SelfTestProbe {
            timeout_s,
            probe,
            nonce,
        } => run_self_test_probe(ctx, grant, timeout_s, &probe, &nonce),
        OpSpec::InteractiveOpenCmd { command, owner } => {
            let payload = command_payload(&command);
            lease_send(ctx, grant, payload, TxClass::InteractiveOpenCmd, &owner)
        }
        OpSpec::InteractiveSend { payload, owner } => {
            lease_send(ctx, grant, payload, TxClass::LeaseOwner, &owner)
        }
        OpSpec::RebootRecovery { .. } => OpOutcome::Error(PyErr::value_error(
            "reboot recovery 由 session actor 以登入 run 驅動（OpRunner 不承接）",
        )),
        OpSpec::DetectTemplate { .. } => OpOutcome::Detected(None),
    }
}

fn command_payload(command: &str) -> Vec<u8> {
    let mut payload = command.as_bytes().to_vec();
    if !payload.ends_with(b"\n") {
        payload.push(b'\n');
    }
    payload
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
        input_epoch: None,
        replay_hold: None,
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
    Duration::from_secs_f64(value.max(0.0))
}

/// `wait_for_regex_from(pattern, offset, timeout)`（期限依注入 Clock）。
fn wait_regex_from(ctx: &OpCtx, re: &PyRegex, from: u64, timeout_s: f64) -> bool {
    ctx.rx
        .wait_for_regex_from(re, from, secs(timeout_s), &*ctx.clock)
        .is_some()
}

/// Python `_map_aborted_result`（非 drain 中止）：TX 未完整提交時的命令結果碼。
fn map_send_failure(
    ctx: &OpCtx,
    report: &TxReport,
    requested: usize,
    cmd_id: &str,
) -> PyDict {
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
        let _ = ctx.wal.append(WalAppendReq {
            com: &ctx.com,
            dir: "META",
            source: "system",
            payload: b"",
            cmd_id: Some(cmd_id),
            loss_flag: false,
            meta: Some(meta),
        });
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

fn run_command(ctx: &OpCtx, grant: &OpGrant, q: &QueuedCommand) -> OpOutcome {
    let t = &ctx.profile.template;
    let mode = normalized_mode(&q.req.mode).to_string();
    let command = q.req.command.as_str();
    let cmd_id = q.cmd_id.as_str();
    let timeout_s = q.req.timeout_s;
    let completion_regex = q.req.completion_regex.as_deref();
    let passthrough = t.platform == "passthrough";
    if !passthrough && is_reboot_command(command) {
        return run_reboot(ctx, grant, q, &mode);
    }
    let completion_pattern = if passthrough {
        passthrough_completion_pattern(t, completion_regex)
    } else {
        Some(completion_regex.unwrap_or(&t.prompt_regex).to_string())
    };
    let pre_offset = ctx.rx.snapshot_len();
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
        TxClass::Command,
        &q.req.source,
        Some(cmd_id),
        true,
    );
    if !fully_sent(&report, payload.len()) {
        return OpOutcome::CommandResult(map_send_failure(ctx, &report, payload.len(), cmd_id));
    }

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
    loop {
        let now = ctx.clock.mono_ns();
        if now >= deadline {
            break;
        }
        let remaining = (deadline - now) as f64 / 1_000_000_000.0;
        let wait_chunk = silence_limit.min(remaining);
        let pre_rx = ctx.rx.snapshot_len();
        if wait_regex_from(ctx, &wait_re, pre_offset, wait_chunk) {
            matched = true;
            break;
        }
        if ctx.rx.snapshot_len() == pre_rx {
            break;
        }
    }
    if !matched {
        return recover_after_failure(
            ctx,
            grant,
            timeout_s,
            command,
            &t.prompt_regex,
            pre_offset,
            passthrough,
        );
    }
    let raw = ctx.rx.text_from(pre_offset);
    let prompt_valid = valid_completion_regex(Some(&PyValue::from(t.prompt_regex.clone())));
    let stdout = match completion_regex {
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
/// `min(timeout, 2 s)` 內 prompt 回來 → ok／partial:false（actor 只清 TX 靜默維度，#162）；否則 `RebootExpected`。
fn run_reboot(ctx: &OpCtx, grant: &OpGrant, q: &QueuedCommand, mode: &str) -> OpOutcome {
    let t = &ctx.profile.template;
    let command = q.req.command.as_str();
    let pre_offset = ctx.rx.snapshot_len();
    let payload = command_payload(command);
    let report = send(
        ctx,
        grant,
        &payload,
        TxClass::Command,
        &q.req.source,
        Some(&q.cmd_id),
        true,
    );
    if !fully_sent(&report, payload.len()) {
        return OpOutcome::CommandResult(map_send_failure(
            ctx,
            &report,
            payload.len(),
            &q.cmd_id,
        ));
    }
    let prompt_re = compile_or_never(&t.prompt_regex);
    if wait_regex_from(ctx, &prompt_re, pre_offset, q.req.timeout_s.min(2.0)) {
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
fn recover_after_failure(
    ctx: &OpCtx,
    grant: &OpGrant,
    timeout_s: f64,
    command: &str,
    prompt_regex: &str,
    pre_offset: u64,
    passthrough: bool,
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
                    return OpOutcome::Error(PyErr::value_error(tx_incomplete_message(
                        &report, 1,
                    )));
                }
            }
        }
        if wait_regex_from(ctx, &prompt_re, offset, timeout_s.min(2.0)) {
            let raw = ctx.rx.text_from(pre_offset);
            let stdout = extract_command_stdout(&raw, command, &prompt_re, None);
            return OpOutcome::Recovered { key: name, stdout };
        }
    }
    OpOutcome::PromptTimeout {
        stdout: clean_text(&ctx.rx.text_from(pre_offset)),
    }
}

/// `_self_test_impl` 的 nonce probe 段（pre-check 由 actor 完成）。
fn run_self_test_probe(
    ctx: &OpCtx,
    grant: &OpGrant,
    timeout_s: f64,
    probe: &str,
    nonce: &str,
) -> OpOutcome {
    let offset = ctx.rx.snapshot_len();
    let payload = command_payload(probe);
    let report = send(
        ctx,
        grant,
        &payload,
        TxClass::SelfTestProbe,
        "system:self_test",
        None,
        true,
    );
    if !fully_sent(&report, payload.len()) {
        match report.reject {
            // RPC-H-07 KEEP：flash 拒絕照等 nonce（逾時 → TARGET_UNRESPONSIVE）。
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
    let nonce_re = compile_or_never(nonce);
    if !wait_regex_from(ctx, &nonce_re, offset, timeout_s) {
        let (flood, _) = ctx.rx.rx_stats(ctx.clock.mono_ns());
        return OpOutcome::SelfTest(pydict! {
            "probe_ok" => false,
            "rx_bytes_last_10s" => py_int(flood as i128),
        });
    }
    let prompt_re = compile_or_never(&ctx.profile.template.prompt_regex);
    let _ = wait_regex_from(ctx, &prompt_re, offset, timeout_s);
    OpOutcome::SelfTest(pydict! { "probe_ok" => true })
}

/// lease 擁有者 TX（`interactive_send`／`interactive_open` 的 command）。
fn lease_send(
    ctx: &OpCtx,
    grant: &OpGrant,
    payload: Vec<u8>,
    class: TxClass,
    owner: &str,
) -> OpOutcome {
    let report = send(ctx, grant, &payload, class, owner, None, true);
    if fully_sent(&report, payload.len()) {
        return OpOutcome::Sent {
            bytes: payload.len(),
        };
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
