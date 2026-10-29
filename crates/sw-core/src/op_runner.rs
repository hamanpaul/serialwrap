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
use sw_proto::pycompat::{PyErr, a2b_base64_lenient, clean_text, py_strip, splitlines};
use sw_proto::pydict;
use sw_proto::pyjson::{PyDict, PyInt, PyValue};
use sw_proto::pyre::{PyFlags, PyRegex, compile, valid_completion_regex};

use crate::arbiter::{QueuedCommand, SubmitOrigin};
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
    /// 並行 lane（READY recover、self-test probe）。
    side: Sender<(OpGrant, OpSpec)>,
}

impl OpSpec {
    /// 不是 Python 前景 operation、須與前景命令並行的 op（審查 F25）：READY recover（`recover_session` →
    /// `_recover_after_failure`）與 self-test nonce probe 都不看 `foreground_busy`，照送救援鍵／probe。
    pub fn runs_beside_foreground(&self) -> bool {
        matches!(self, OpSpec::Recover { .. } | OpSpec::SelfTestProbe { .. })
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
    /// 前景 lane（命令、interactive）與並行 lane（[`OpSpec::runs_beside_foreground`]）各一條 thread；
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
            // 只把原始串流中 BusyBox 的雙 CR＋LF 視為折行證據；一般 stdout 換行不可拼接（sol P5-70f6ce7-1）。
            let mut wrapped = false;
            if raw.contains("\r\r\n") {
                if let Some(n) = strip_wrapped_command_echo(&lines, stripped) {
                    lines.drain(..n);
                    wrapped = true;
                }
            }
            // bash readline 會在長回顯折行處寫入「空白＋CR」再接續同一行：以原始控制字元辨識後重新抽取。
            if !wrapped && raw.contains(" \r") {
                let chars: Vec<char> = raw.chars().collect();
                let cmd: Vec<char> = stripped.chars().collect();
                if let Some((start, end)) = match_readline_wrapped_echo(&chars, &cmd) {
                    let echo: String = chars[start..end].iter().collect();
                    let candidate: String = chars[..start]
                        .iter()
                        .copied()
                        .chain(echo.replace(" \r", "").chars())
                        .chain(chars[end..].iter().copied())
                        .collect();
                    let candidate_lines = prompt_cut_lines(&candidate, prompt_regex, completion);
                    if candidate_lines
                        .first()
                        .is_some_and(|line| py_strip(line).ends_with(stripped))
                    {
                        lines = candidate_lines[1..].to_vec();
                    }
                }
            }
        }
    }
    lines.join("\n").trim_matches('\n').to_string()
}

/// `clean_text` 後依最後一個 prompt（completion 模式下只裁完成標記之後的尾 prompt）截斷並切行。
fn prompt_cut_lines(raw: &str, prompt_regex: &PyRegex, completion: Option<&PyRegex>) -> Vec<String> {
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

/// 終端寬度扣掉 prompt 後每段回顯仍至少約 20 字元（Python `_READLINE_WRAP_MIN_SEGMENT`；BusyBox 路徑以此估計最多佔幾行）。
const WRAP_MIN_SEGMENT: usize = 20;

/// Python `_strip_wrapped_command_echo`：移除含 BusyBox 雙 CR 折行標記的命令回顯；`lines` 已 `clean_text` 並切行。
/// 只接合不同輸出行（行內空白保持原樣），前綴（殘留 prompt）須短於第一行；回傳要移除的行數。
fn strip_wrapped_command_echo(lines: &[String], command: &str) -> Option<usize> {
    if command.is_empty() || lines.len() < 2 {
        return None;
    }
    let cmd_len = command.chars().count();
    let first_len = lines[0].chars().count();
    let mut joined = String::new();
    for (i, line) in lines.iter().take(cmd_len / WRAP_MIN_SEGMENT + 2).enumerate() {
        joined.push_str(line);
        if joined.ends_with(command) && joined.chars().count() - cmd_len < first_len {
            return Some(i + 1);
        }
    }
    None
}

/// Python `_match_readline_wrapped_echo`（842c014 後語意）：於原始文字首個非空行內，找「命令原文＋readline 折行標記
/// `" \r"`」的起訖位置（字元索引）。標記不得在第一個命令字元前，彼此（第一個自命令起點）至少相隔
/// [`WRAP_MIN_SEGMENT`] 個命令字元；標記優先於命令字元比對（折行點緊貼命令原有空白時仍可辨識），回顯須結束於行尾。
fn match_readline_wrapped_echo(text: &[char], command: &[char]) -> Option<(usize, usize)> {
    if text.is_empty() || command.is_empty() {
        return None;
    }
    let mut line_start = 0usize;
    let line_end = loop {
        if line_start >= text.len() {
            return None;
        }
        let line_end = text[line_start..]
            .iter()
            .position(|c| *c == '\n')
            .map_or(text.len(), |p| line_start + p);
        if text[line_start..line_end].iter().any(|c| *c != '\r') {
            break line_end;
        }
        if line_end == text.len() {
            return None;
        }
        line_start = line_end + 1;
    };
    let line = &text[line_start..line_end];
    for command_start in 0..line.len() {
        if line[command_start] != command[0] {
            continue;
        }
        let (mut cursor, mut matched) = (command_start, 0usize);
        let mut last_wrap: Option<usize> = None;
        while matched < command.len() && cursor < line.len() {
            if matched > 0 && line[cursor] == ' ' && line.get(cursor + 1) == Some(&'\r') {
                let segment = last_wrap.map_or(matched, |w| matched - w);
                if segment < WRAP_MIN_SEGMENT {
                    break;
                }
                last_wrap = Some(matched);
                cursor += 2;
                continue;
            }
            if line[cursor] == command[matched] {
                cursor += 1;
                matched += 1;
                continue;
            }
            break;
        }
        if matched != command.len() {
            continue;
        }
        if cursor == line.len() || line[cursor..] == ['\r'] {
            return Some((line_start + command_start, line_start + cursor));
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
        FenceReject::HumanAdmissionClosed | FenceReject::InputStale | FenceReject::ReplayRevoked => {
            "stale_owner_gen"
        }
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
            )
        }
        OpSpec::SelfTestProbe {
            timeout_s,
            probe,
            nonce,
        } => run_self_test_probe(ctx, grant, timeout_s, &probe, &nonce),
        OpSpec::InteractiveOpenCmd { command, owner } => {
            let payload = command_payload(&command);
            // `command.submit {mode:"interactive"}` 的命令：TX 帶原 cmd_id，未完整送出依命令帳結算（sol-backfill p5 #1）。
            lease_send(ctx, grant, payload, TxClass::InteractiveOpenCmd, &owner, grant.cmd_id.as_deref())
        }
        OpSpec::InteractiveSend { payload, owner } => {
            lease_send(ctx, grant, payload, TxClass::LeaseOwner, &owner, None)
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
        // 07 D-03：06 既有 permit 建構點一律 `None`。
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
        if matches!(q.req.origin, SubmitOrigin::HumanConsole { .. }) {
            // `_recover_after_failure` human 分支（DF-02）：不送救援鍵；actor 依前景來源改開 human lease。
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
                        let tx_outcome = if matches!(report.result.outcome, TxOutcome::Unknown { .. }) {
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
