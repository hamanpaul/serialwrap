//! 登入狀態機（P5-T13 最小版 → P7 Task 3 全平台；05 §8；逐函式對照 `sw_core/login_fsm.py`）。
//!
//! - `choose_login_plan`：sans-IO 的計畫選擇（passthrough／不具 command capability／boot quiet／帳密未解析
//!   → 不送任何 TX；Slice 2.1 能力閘已依 08 §0.6 退役清單於 P7 Task 3 移除）。
//! - `probe_ready`／`ensure_ready`／`detect_template`：在 OpRunner（或測試 executor）thread 執行；TX 一律經
//!   `LoginIo.tx`（TxWriter，class 由呼叫端以 `LoginIo.class` 決定），RX 比對整個 RX 視窗（Python
//!   `wait_for_regex` 語意，SES-16.6-05 KEEP），等待可經 `LoginIo.cancel` 取消（SC-30）。
//! - TX 回報對應（§11.3 不得自動重送）：quiet／較新 login run 的 fence 拒絕（`_login_tx_refused`）→
//!   `Aborted(Refused)`；未提交且無拒絕 → `Aborted(Cancelled)`；其餘拒絕、部分寫出或結果不明 → `Raised`
//!   （P5 F16 `classify_login_tx_failure`：類別名與 `str(exc)`）；之後一律不再呼叫 `tx`。Python 會拋例外處
//!   （無效 regex）→ `Raised { exc: "error", .. }`。
//! - console unlock 的 INFO log 依 GAP-G-01 不輸出。

use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::Duration;

use sw_pal::traits::{Clock, SysEnv, TxOutcome, TxResult};
use sw_proto::config_types::{ProfileTemplate, SessionProfile};
use sw_proto::pycompat::clean_text;
use sw_proto::pyre::{PyFlags, PyRegex, compile, escape};

use crate::session_auth::{AuthResolution, SessionAuth, credentials_declared_but_unresolved};
use crate::tx_writer::{FenceReject, OpId, TxClass, TxJob, TxPermit, TxReport, TxWriterHandle};
use crate::uart_bridge::{RunCancel, RxShared, SecretEchoGuard, WaitOutcome};

pub use crate::quiet_gate::detect_boot_banner;

/// 登入計畫（`Effect::StartLogin` 內容）。
#[derive(Clone)]
pub enum LoginPlan {
    /// passthrough／Slice 2.1 能力閘：不登入、不 probe。
    None,
    Probe,
    EnsureReady {
        auth: SessionAuth,
    },
    CredentialsUnresolved {
        reason: &'static str,
        env_file: String,
    },
}

/// 登入結果（`ControlEvent::LoginFinished` 內容）。
#[derive(Debug, Clone, PartialEq)]
pub enum LoginOutcome {
    Ready,
    /// probe 失敗：停 ATTACHED，內含 last_error。
    NotReady(String),
    /// `ensure_ready` 失敗：attach 路徑 → drain（`LoginFailed`），既有 bridge 路徑 → ATTACHED。
    Failed(String),
    /// P7 Task 3：TX 被拒（quiet／較新 login run：Python `_login_tx_refused` 為真）或取消，之後零 TX。
    Aborted(LoginAbort),
    /// TX 未完整提交且不是 quiet／較新 login run 的拒絕（Python `_login_tx_refused` 為假 → 例外上拋；審查 F16），
    /// 或 Python 會拋例外處（無效 regex → `exc == "error"`）：`exc` 為 Python 例外類別名
    /// （`WalUnavailableError`／`TxUnknownError`／`TxIncompleteError`／`error`…），`message` 為 `str(exc)`。
    /// attach 路徑 → DETACHED＋`ATTACH_FAILED:<exc>`；既有 bridge 再探 → RPC `EXCEPTION`、state 不變；
    /// reboot recovery → `RECOVERY_ERROR`。
    Raised { exc: String, message: String },
}

/// `LoginOutcome::Aborted` 的原因。
#[derive(Debug, Clone, PartialEq)]
pub enum LoginAbort {
    Refused(FenceReject),
    Cancelled,
    /// 保留給收斂表（08 Task 7）；merge P5 F16 後 login FSM 對部分寫出／結果不明改回 `Raised`（帶 `str(exc)`）。
    TxIncomplete,
}

/// 登入 FSM 所需的 I/O 注入點（TX 經 TxWriter；測試以閉包替身）。
pub struct LoginIo<'a> {
    pub rx: &'a RxShared,
    /// P7 Task 3：回 06 T9 `TxReport`（取得 fence 拒絕原因）。
    pub tx: &'a dyn Fn(&[u8], TxClass, &str, bool) -> TxReport,
    pub clock: &'a dyn Clock,
    pub nonce: &'a dyn Fn() -> String,
    /// secret TX 前寫入只含 byte 長度的 WAL META；失敗時 fail closed，不送出憑證。
    pub audit_secret_tx: &'a dyn Fn(usize) -> bool,
    /// P7 Task 3：SC-30 取消。
    pub cancel: &'a RunCancel,
    /// P7 Task 3：`LoginProbe`｜`ReadyReconfirm`（呼叫端決定）。
    pub class: TxClass,
}

/// RX_FLOOD 可遮蔽的失敗碼（login_fsm.py `_FLOOD_MASKABLE_ERRORS` 逐字）。
pub const FLOOD_MASKABLE_ERRORS: [&str; 5] = [
    "PROMPT_UNAVAILABLE",
    "READY_NONCE_TIMEOUT",
    "READY_PROMPT_TIMEOUT",
    "POST_LOGIN_CMD_TIMEOUT",
    "LOGIN_PROMPT_TIMEOUT",
];

/// 需要補 `last_error_detail`（RX tail）的失敗碼（#174）。
pub const LOGIN_FSM_DETAIL_ERRORS: [&str; 12] = [
    "USER_ENV_MISSING",
    "PASS_ENV_REQUIRED",
    "PASS_ENV_MISSING",
    "LOGIN_USER_REQUIRED",
    "LOGIN_PROMPT_TIMEOUT",
    "BCM_PROMPT_TIMEOUT",
    "SHELL_PROMPT_TIMEOUT",
    "PRPL_PROMPT_TIMEOUT",
    "POST_LOGIN_CMD_TIMEOUT",
    "READY_NONCE_TIMEOUT",
    "READY_PROMPT_TIMEOUT",
    "LOGIN_REQUIRED",
];

/// 依 profile、待自動登入旗標、已解析帳密與 quiet 狀態選擇登入計畫（05 §8.2＋`_attach_by_id`）。
/// 回傳 `(計畫, 合成的 last_error)`。
///
/// 順序：passthrough（SC-35）→ 不具 command capability → boot quiet（合成 `PROMPT_UNAVAILABLE`）→
/// 宣告但未解析的帳密（`CredentialsUnresolved`）→ `EnsureReady`／`Probe`。
pub fn choose_login_plan(
    p: &SessionProfile,
    pending_auto_login: bool,
    auth: (SessionAuth, AuthResolution),
    quiet_blocks: bool,
) -> (LoginPlan, Option<&'static str>) {
    let t = &p.template;
    if t.platform == "passthrough" || !t.command_capable() {
        return (LoginPlan::None, None);
    }
    if quiet_blocks {
        return (LoginPlan::None, Some("PROMPT_UNAVAILABLE"));
    }
    let (auth, resolution) = auth;
    let require_login =
        pending_auto_login || !t.login_regex.is_empty() || declared(&t.console_unlock_env);
    if require_login && credentials_declared_but_unresolved(p, &resolution) {
        return (
            LoginPlan::CredentialsUnresolved {
                reason: resolution.reason,
                env_file: resolution.env_file_path.unwrap_or_default(),
            },
            Some("CREDENTIALS_UNRESOLVED"),
        );
    }
    let has = |v: &Option<String>| v.as_deref().is_some_and(|s| !s.is_empty());
    if require_login && (has(&auth.console_unlock) || (has(&auth.username) && has(&auth.password)))
    {
        return (LoginPlan::EnsureReady { auth }, None);
    }
    (LoginPlan::Probe, None)
}

/// 依計畫執行登入（OpRunner／executor thread 用）；`None`／`CredentialsUnresolved` 不送任何 TX，回 `None`。
pub fn run_login_plan(
    io: &LoginIo,
    p: &SessionProfile,
    plan: &LoginPlan,
    env: &dyn SysEnv,
) -> Option<LoginOutcome> {
    match plan {
        LoginPlan::Probe => Some(probe_ready(io, p)),
        LoginPlan::EnsureReady { auth } => Some(ensure_ready(io, p, auth, env)),
        LoginPlan::None | LoginPlan::CredentialsUnresolved { .. } => None,
    }
}

/// `err.endswith("_PROMPT_TIMEOUT")`（`login_fsm.py:57`）。
pub fn is_prompt_timeout_error(err: &str) -> bool {
    err.ends_with("_PROMPT_TIMEOUT")
}

/// `_maybe_reclassify_flood`（#153）：可遮蔽碼且 10 s 視窗 raw RX ≥ `RX_FLOOD_BYTES_PER_10S` → `RX_FLOOD`。
pub fn maybe_reclassify_flood(io: &LoginIo, err: Option<String>) -> Option<String> {
    let err = err?;
    if !FLOOD_MASKABLE_ERRORS.contains(&err.as_str()) && !is_prompt_timeout_error(&err) {
        return Some(err);
    }
    if io.rx.rx_stats(io.clock.mono_ns()).0 >= RX_FLOOD_BYTES_PER_10S {
        return Some("RX_FLOOD".to_string());
    }
    Some(err)
}

/// `login_fsm.detect_template`：送 `\n`、`time.sleep(probe_timeout_s)`，依序比對各 template。
///
/// 略過 passthrough 與宣告 `console_unlock_env` 者；先 `prompt_regex`（命中即回），否則記第一個
/// `login_regex` 命中者為備選。無效 regex 略過（Python `except re.error: continue`）。TX 中止 → `Err`。
pub fn detect_template<'t>(
    io: &LoginIo,
    templates: &'t [ProfileTemplate],
    probe_timeout_s: f64,
) -> Result<Option<&'t ProfileTemplate>, LoginOutcome> {
    io.rx.clear();
    send_command(io, "")?;
    io.clock.sleep(secs(probe_timeout_s));
    let snapshot = io.rx.tail(RX_TAIL_CHARS);
    let mut login_candidate = None;
    for tpl in templates {
        if tpl.platform == "passthrough" || declared(&tpl.console_unlock_env) {
            continue;
        }
        match compile(&tpl.prompt_regex, PyFlags(0)) {
            Ok(re) if re.search(&snapshot).is_some() => return Ok(Some(tpl)),
            Ok(_) => {}
            Err(_) => continue,
        }
        if login_candidate.is_none() {
            match compile(&tpl.login_regex, PyFlags(0)) {
                Ok(re) if re.search(&snapshot).is_some() => login_candidate = Some(tpl),
                Ok(_) => {}
                Err(_) => continue,
            }
        }
    }
    Ok(login_candidate)
}

/// `session_manager._matches_any_bootloader_prompt`（重用 06 T13 `quiet_gate` 的實作，不另寫）。
pub fn matches_any_bootloader_prompt(text: &str, prompts: &[String]) -> Option<String> {
    crate::quiet_gate::matches_any_bootloader_prompt(text, prompts)
}

/// `login_fsm._console_at_bootloader`：尾行＝`clean_text(tail).rstrip("\r\n").split("\n")[-1]`；
/// 無效 regex → `Err(Raised("error"))`（與 `matches_any_bootloader_prompt` 略過不同）。
pub fn console_at_bootloader(rx_tail: &str, prompts: &[String]) -> Result<bool, LoginOutcome> {
    let tail = console_tail_line(rx_tail);
    for pattern in prompts {
        if compile_or_raise(pattern)?.search(&tail).is_some() {
            return Ok(true);
        }
    }
    Ok(false)
}

/// 只做 probe：送 `\n` 並等 prompt；失敗時依視窗分類（05 §8.3；`login_fsm.probe_ready`）。
pub fn probe_ready(io: &LoginIo, p: &SessionProfile) -> LoginOutcome {
    finish(probe_ready_inner(io, p), false)
}

fn probe_ready_inner(io: &LoginIo, p: &SessionProfile) -> Step<Option<String>> {
    if !probe_prompt(io, p)? {
        let code = classify_non_ready_state(io, p).to_string();
        return Ok(maybe_reclassify_flood(io, Some(code)));
    }
    let err = finalize_ready(io, p)?;
    Ok(maybe_reclassify_flood(io, err))
}

/// 完整登入：login → password → prompt → post_login_cmd → nonce 驗證；console unlock profile 改走
/// 被動靜默解鎖（05 §8.3；`login_fsm.ensure_ready`）。
pub fn ensure_ready(
    io: &LoginIo,
    p: &SessionProfile,
    auth: &SessionAuth,
    env: &dyn SysEnv,
) -> LoginOutcome {
    let _secret_cleanup = SecretEchoFlowCleanup(io.rx);
    finish(ensure_ready_inner(io, p, auth, env), true)
}

struct SecretEchoFlowCleanup<'a>(&'a RxShared);

impl Drop for SecretEchoFlowCleanup<'_> {
    fn drop(&mut self) {
        self.0.disarm_secret_echo_for_login_end();
    }
}

fn ensure_ready_inner(
    io: &LoginIo,
    p: &SessionProfile,
    auth: &SessionAuth,
    env: &dyn SysEnv,
) -> Step<Option<String>> {
    if declared(&p.template.console_unlock_env) {
        return ensure_console_unlocked(io, p, auth);
    }
    if !probe_prompt(io, p)? {
        let (ok, err) = maybe_login(io, p, auth, env)?;
        if err.is_some() {
            return Ok(maybe_reclassify_flood(io, err));
        }
        if !ok {
            let code = prompt_timeout_error(&p.template.platform).to_string();
            return Ok(maybe_reclassify_flood(io, Some(code)));
        }
    }
    let err = finalize_ready(io, p)?;
    Ok(maybe_reclassify_flood(io, err))
}

/// 依 platform 回傳 prompt 逾時碼（bcm→BCM_PROMPT_TIMEOUT、shell→SHELL_PROMPT_TIMEOUT、其他→PRPL_PROMPT_TIMEOUT）。
pub fn prompt_timeout_error(platform: &str) -> &'static str {
    match platform {
        "bcm" => "BCM_PROMPT_TIMEOUT",
        "shell" => "SHELL_PROMPT_TIMEOUT",
        _ => "PRPL_PROMPT_TIMEOUT",
    }
}

/// `text` 是否命中 `login_regex` 或 `password_regex`（先比 `clean_text` 後的文字再比原文；不合法 regex 略過）。
pub fn matches_login_or_password(text: &str, p: &SessionProfile) -> bool {
    if text.is_empty() {
        return false;
    }
    let cleaned = clean_text(text);
    [p.template.login_regex.as_str(), p.template.password_regex.as_str()]
        .into_iter()
        .filter(|pattern| !pattern.is_empty())
        .any(|pattern| match compile(pattern, PyFlags(0)) {
            Ok(re) => re.search(&cleaned).is_some() || re.search(text).is_some(),
            Err(_) => false,
        })
}

/// `last_error ∈ LOGIN_FSM_DETAIL_ERRORS` 時的 `last_error_detail`：RX tail 經 `clean_text` 後截尾 300 字元（#174）；
/// 其他碼或空 tail 回 `None`（`_login_fsm_rx_tail_detail`）。
pub fn login_error_detail(rx: &RxShared, code: &str) -> Option<String> {
    if !LOGIN_FSM_DETAIL_ERRORS.contains(&code) {
        return None;
    }
    let raw = rx.tail(RX_TAIL_CHARS);
    if raw.is_empty() {
        return None;
    }
    let cleaned = clean_text(&raw);
    let count = cleaned.chars().count();
    let tail: String = cleaned
        .chars()
        .skip(count.saturating_sub(DETAIL_TAIL_CHARS))
        .collect();
    (!tail.is_empty()).then_some(tail)
}

/// 經 `TxWriterHandle` 送出登入 TX 的閉包（class 由呼叫端決定；permit 帶 StartLogin 的 epoch／fsm_gen）。
/// 每筆 TX 取得 `TxReport` 後立即 settle（登入 op 不參與 BC-03 映射）；TxWriter 已結束時回 `NotSubmitted`。
pub fn login_tx_sender(
    handle: TxWriterHandle,
    epoch: u64,
    owner_gen: u64,
    fsm_gen: u64,
) -> impl Fn(&[u8], TxClass, &str, bool) -> TxReport + Send + Sync {
    let handle = Arc::new(handle);
    move |payload: &[u8], class: TxClass, source: &str, log: bool| -> TxReport {
        let op_id = OpId(LOGIN_OP_ID_BASE | NEXT_LOGIN_OP.fetch_add(1, Ordering::Relaxed));
        let (reply, report) = crossbeam_channel::bounded(1);
        let job = TxJob {
            permit: TxPermit {
                op_id,
                epoch,
                owner_gen,
                fsm_gen,
                class,
                source: source.to_string(),
                cmd_id: None,
                log,
                secret_tx: None,
                input_epoch: None,
                replay_hold: None,
                probe_id: None,
            },
            payload: payload.to_vec(),
            reply,
            aggregate: None,
        };
        // TxWriter 已關閉（drain／shutdown）：等同 fence 凍結（Python `_tx_frozen` → `draining`）。
        let not_submitted = TxReport {
            op_id,
            epoch,
            result: TxResult {
                outcome: TxOutcome::NotSubmitted,
                error: None,
            },
            wal: None,
            reject: Some(FenceReject::Frozen),
        };
        if handle.jobs.send(job).is_err() {
            return not_submitted;
        }
        let result = handle.wait_report(&report).unwrap_or(not_submitted);
        let _ = handle.settle(op_id);
        result
    }
}

// ---------------------------------------------------------------------------
// 內部步驟（login_fsm.py 逐步承接）
// ---------------------------------------------------------------------------

/// `RX_FLOOD_BYTES_PER_10S`（constants.py）。
const RX_FLOOD_BYTES_PER_10S: u64 = 20_000;
/// `rx_tail()` 預設字元數（uart_io.py）。
const RX_TAIL_CHARS: usize = 4096;
/// `_login_fsm_rx_tail_detail` 截尾字元數。
const DETAIL_TAIL_CHARS: usize = 300;
/// `_wait_console_idle` 的輪詢上限／下限（`time.sleep(min(0.05, max(0.001, deadline - now)))`）。
const IDLE_POLL_MAX: Duration = Duration::from_millis(50);
const IDLE_POLL_MIN: Duration = Duration::from_millis(1);
/// console unlock 空行辨識等待上限（`min(1.0, …)`）。
const BLANK_LINE_PROBE_MAX: Duration = Duration::from_secs(1);
/// 字面預設 ready_probe（console unlock 時改用錨定 nonce 樣式）。
const DEFAULT_READY_PROBE: &str = "echo __READY__${nonce}";
/// 登入 TX 的 op_id 區段（與 SessionCore 前景 op 的遞增序號不重疊）。
const LOGIN_OP_ID_BASE: u64 = 1 << 62;

static NEXT_LOGIN_OP: AtomicU64 = AtomicU64::new(0);

/// 中途結束（`Aborted`／`Raised`）以 `Err` 傳遞；`Ok(None)` 為成功，`Ok(Some(code))` 為失敗碼。
type Step<T> = Result<T, LoginOutcome>;

fn finish(step: Step<Option<String>>, ensure: bool) -> LoginOutcome {
    match step {
        Ok(None) => LoginOutcome::Ready,
        Ok(Some(code)) if ensure => LoginOutcome::Failed(code),
        Ok(Some(code)) => LoginOutcome::NotReady(code),
        Err(outcome) => outcome,
    }
}

fn declared(value: &Option<String>) -> bool {
    value.as_deref().is_some_and(|v| !v.is_empty())
}

fn secs(s: f64) -> Duration {
    if s.is_finite() && s > 0.0 {
        Duration::try_from_secs_f64(s).unwrap_or(Duration::MAX)
    } else {
        Duration::ZERO
    }
}

fn compile_or_raise(pattern: &str) -> Step<PyRegex> {
    compile(pattern, PyFlags(0)).map_err(|err| compile_raised(&err))
}

/// `re.compile` 失敗的 Python 例外（類別名、`str(exc)`）：`re.error` 的類別名為 `error`。
fn compile_raised(err: &sw_proto::pyre::CompileError) -> LoginOutcome {
    use sw_proto::pyre::CompileError;
    let (exc, message) = match err {
        CompileError::Re(e) if e.value_error => ("ValueError", e.to_string()),
        CompileError::Re(e) => ("error", e.to_string()),
        CompileError::Recursion => ("RecursionError", "maximum recursion depth exceeded".to_string()),
        CompileError::Overflow(m) => ("OverflowError", m.clone()),
        CompileError::Untranslatable(m) => ("error", m.clone()),
    };
    LoginOutcome::Raised {
        exc: exc.to_string(),
        message,
    }
}

fn search_or_raise(pattern: &str, text: &str) -> Step<bool> {
    Ok(compile_or_raise(pattern)?.search(text).is_some())
}

/// 登入 TX 未完整提交（Python `send_command` 拋 `TxIncompleteError` 家族）。
#[derive(Debug, Clone, PartialEq)]
pub enum TxFail {
    /// `_login_tx_refused` 為真（fence 以 quiet 或較新 login run 拒絕）→ `PROMPT_UNAVAILABLE`，不精煉。
    Refused,
    /// 其餘拒絕、結果不明或部分寫出：例外上拋（類別名與 `str(exc)`）。
    Raised { exc: &'static str, message: String },
}

/// 依 Python `_login_tx_refused` 與 `UARTBridge._send_locked` 分類登入 TX 失敗（審查 F16）：
/// - `Quiet`／`ReconfirmPending`（`quiet`）、`FsmMismatch`（`stale_login_gen`）→ [`TxFail::Refused`]；
/// - `WalUnhealthy` → `WalUnavailableError("tx incomplete: not_submitted 0/N WAL_UNHEALTHY")`（系統 TX 於寫前拒絕）；
/// - `Unknown` → `TxUnknownError`；其餘拒絕（draining／stale_generation／cancelled／flashing…）與部分寫出 →
///   `TxIncompleteError`；訊息皆為 `tx incomplete: {outcome} {confirmed}/{requested} {error}`。
pub fn classify_login_tx_failure(report: &TxReport, requested: usize) -> TxFail {
    match report.reject {
        Some(FenceReject::Quiet | FenceReject::ReconfirmPending | FenceReject::FsmMismatch) => {
            TxFail::Refused
        }
        Some(FenceReject::WalUnhealthy) => TxFail::Raised {
            exc: "WalUnavailableError",
            message: format!("tx incomplete: not_submitted 0/{requested} WAL_UNHEALTHY"),
        },
        _ => TxFail::Raised {
            exc: if matches!(report.result.outcome, TxOutcome::Unknown { .. }) {
                "TxUnknownError"
            } else {
                "TxIncompleteError"
            },
            message: crate::op_runner::tx_incomplete_message(report, requested),
        },
    }
}

fn send(io: &LoginIo, payload: &str, source: &str, log: bool) -> Step<()> {
    let mut bytes = payload.as_bytes().to_vec();
    if !bytes.ends_with(b"\n") {
        bytes.push(b'\n');
    }
    let report = (io.tx)(&bytes, io.class, source, log);
    match report.result.outcome {
        TxOutcome::Completed(n) if n >= bytes.len() => return Ok(()),
        // 未提交且無 fence 原因：run 被取消（SC-30 RunCancel）。
        TxOutcome::NotSubmitted if report.reject.is_none() => {
            return Err(LoginOutcome::Aborted(LoginAbort::Cancelled));
        }
        _ => {}
    }
    Err(match classify_login_tx_failure(&report, bytes.len()) {
        TxFail::Refused => LoginOutcome::Aborted(LoginAbort::Refused(
            report.reject.unwrap_or(FenceReject::Quiet),
        )),
        TxFail::Raised { exc, message } => LoginOutcome::Raised {
            exc: exc.to_string(),
            message,
        },
    })
}

fn send_command(io: &LoginIo, cmd: &str) -> Step<()> {
    send(io, cmd, "system", true)
}

fn send_secret(io: &LoginIo<'_>, secret: &str) -> Step<SecretEchoGuard> {
    let guard = io.rx.arm_secret_echo(secret.as_bytes());
    let secret_len = secret.as_bytes().len();
    if !(io.audit_secret_tx)(secret_len) {
        return Err(LoginOutcome::Raised {
            exc: "WalUnavailableError".to_string(),
            message: "secret TX audit unavailable".to_string(),
        });
    }
    let mut bytes = secret.as_bytes().to_vec();
    if !bytes.ends_with(b"\n") {
        bytes.push(b'\n');
    }
    let report = (io.tx)(&bytes, io.class, "system:secret", false);
    match report.result.outcome {
        TxOutcome::Completed(n) if n >= bytes.len() => {
            return Ok(guard);
        }
        TxOutcome::NotSubmitted if report.reject.is_none() => {
            return Err(LoginOutcome::Aborted(LoginAbort::Cancelled));
        }
        _ => {}
    }
    Err(match classify_login_secret_tx_failure(&report) {
        TxFail::Refused => LoginOutcome::Aborted(LoginAbort::Refused(
            report.reject.unwrap_or(FenceReject::Quiet),
        )),
        TxFail::Raised { exc, message } => LoginOutcome::Raised {
            exc: exc.to_string(),
            message,
        },
    })
}

fn classify_login_secret_tx_failure(report: &TxReport) -> TxFail {
    match report.reject {
        Some(FenceReject::Quiet | FenceReject::ReconfirmPending | FenceReject::FsmMismatch) => {
            TxFail::Refused
        }
        Some(FenceReject::WalUnhealthy) => TxFail::Raised {
            exc: "WalUnavailableError",
            message: "secret TX incomplete: WAL_UNHEALTHY".to_string(),
        },
        _ if matches!(report.result.outcome, TxOutcome::Unknown { .. }) => TxFail::Raised {
            exc: "TxUnknownError",
            message: "secret TX outcome unknown".to_string(),
        },
        _ => TxFail::Raised {
            exc: "TxIncompleteError",
            message: "secret TX incomplete".to_string(),
        },
    }
}

/// `bridge.wait_for_regex(pattern, timeout_s)`：regex 先編譯（失敗 → `Raised`），再以整個 RX 視窗等待。
fn wait_regex(io: &LoginIo, pattern: &str, timeout: Duration) -> Step<bool> {
    wait_regex_from(io, pattern, None, timeout)
}

fn wait_regex_from(
    io: &LoginIo,
    pattern: &str,
    from: Option<u64>,
    timeout: Duration,
) -> Step<bool> {
    let re = compile_or_raise(pattern)?;
    match io
        .rx
        .wait_for_regex_cancellable(&re, from, timeout, io.clock, io.cancel)
    {
        WaitOutcome::Matched => Ok(true),
        WaitOutcome::TimedOut => Ok(false),
        WaitOutcome::Cancelled => Err(LoginOutcome::Aborted(LoginAbort::Cancelled)),
    }
}

fn probe_prompt(io: &LoginIo, p: &SessionProfile) -> Step<bool> {
    io.rx.clear();
    send_command(io, "")?;
    wait_regex(io, &p.template.prompt_regex, secs(p.template.timeout_s))
}

fn classify_non_ready_state(io: &LoginIo, p: &SessionProfile) -> &'static str {
    if matches_login_or_password(&io.rx.tail(RX_TAIL_CHARS), p) {
        "LOGIN_REQUIRED"
    } else {
        "PROMPT_UNAVAILABLE"
    }
}

fn tail_requires_login(io: &LoginIo, p: &SessionProfile) -> bool {
    matches_login_or_password(&io.rx.tail(RX_TAIL_CHARS), p)
}

/// `_finalize_ready`：post_login_cmd（含 #174 login guard）→ nonce probe → prompt；`Ok(None)` 為成功。
fn finalize_ready(io: &LoginIo, p: &SessionProfile) -> Step<Option<String>> {
    let t = &p.template;
    let timeout = secs(t.timeout_s);
    if !t.post_login_cmd.is_empty() {
        if tail_requires_login(io, p) {
            return Ok(Some("LOGIN_REQUIRED".into()));
        }
        send_command(io, &t.post_login_cmd)?;
        if !wait_regex(io, &t.prompt_regex, timeout)? {
            if tail_requires_login(io, p) {
                return Ok(Some("LOGIN_REQUIRED".into()));
            }
            return Ok(Some("POST_LOGIN_CMD_TIMEOUT".into()));
        }
    }
    let nonce = (io.nonce)();
    let probe = t.ready_probe.replace("${nonce}", &nonce);
    let unlock = declared(&t.console_unlock_env);
    if unlock {
        // 隱藏 lock 的舊 prompt 與 terminal echo 都不能作為解鎖證據。
        io.rx.clear();
    }
    send_command(io, &probe)?;
    let nonce_pattern = if unlock && t.ready_probe == DEFAULT_READY_PROBE {
        format!("(?m)^__READY__{}\\r?$", escape(&nonce))
    } else {
        nonce
    };
    if !wait_regex(io, &nonce_pattern, timeout)? {
        if tail_requires_login(io, p) {
            return Ok(Some("LOGIN_REQUIRED".into()));
        }
        return Ok(Some("READY_NONCE_TIMEOUT".into()));
    }
    if !wait_regex(io, &t.prompt_regex, timeout)? {
        return Ok(Some("READY_PROMPT_TIMEOUT".into()));
    }
    Ok(None)
}

/// `_resolve_login_user`：auth → `user_env`（空值即缺，**不** fallback `sp.username`）→ `sp.username`。
fn resolve_login_user(
    p: &SessionProfile,
    auth: &SessionAuth,
    env: &dyn SysEnv,
) -> (Option<String>, Option<&'static str>) {
    let t = &p.template;
    if let Some(user) = auth.username.as_deref().filter(|u| !u.is_empty()) {
        return (Some(user.to_string()), None);
    }
    if let Some(key) = t.user_env.as_deref().filter(|k| !k.is_empty()) {
        return match env.var(key).filter(|v| !v.is_empty()) {
            Some(user) => (Some(user), None),
            None => (None, Some("USER_ENV_MISSING")),
        };
    }
    if let Some(user) = t.username.as_deref().filter(|u| !u.is_empty()) {
        return (Some(user.to_string()), None);
    }
    (None, None)
}

fn resolve_login_password(
    p: &SessionProfile,
    auth: &SessionAuth,
    env: &dyn SysEnv,
) -> Result<String, &'static str> {
    if let Some(password) = auth.password.as_deref().filter(|v| !v.is_empty()) {
        return Ok(password.to_string());
    }
    let Some(key) = p.template.pass_env.as_deref().filter(|k| !k.is_empty()) else {
        return Err("PASS_ENV_REQUIRED");
    };
    env.var(key)
        .filter(|v| !v.is_empty())
        .ok_or("PASS_ENV_MISSING")
}

/// `_maybe_login`：回傳 `(ok, err)`；`USER_ENV_MISSING` 照 Python 被後續等待覆寫而不外傳（X3）。
fn maybe_login(
    io: &LoginIo,
    p: &SessionProfile,
    auth: &SessionAuth,
    env: &dyn SysEnv,
) -> Step<(bool, Option<String>)> {
    let t = &p.template;
    let timeout = secs(t.timeout_s);
    let (user, _unreachable_err) = resolve_login_user(p, auth, env);
    if user.is_none() && !declared(&t.pass_env) {
        return Ok((false, None));
    }
    if !wait_regex(io, &t.login_regex, timeout)? {
        return Ok((false, Some("LOGIN_PROMPT_TIMEOUT".into())));
    }
    let Some(user) = user else {
        return Ok((false, Some("LOGIN_USER_REQUIRED".into())));
    };
    send_command(io, &user)?;
    let timeout_code = prompt_timeout_error(&t.platform);
    if wait_regex(io, &t.password_regex, timeout)? {
        let password = match resolve_login_password(p, auth, env) {
            Ok(password) => password,
            Err(code) => return Ok((false, Some(code.into()))),
        };
        let guard = send_secret(io, &password)?;
        let ok = wait_regex_from(io, &t.prompt_regex, Some(guard.start_from()), timeout)?;
        io.rx.clear();
        return Ok((ok, (!ok).then(|| timeout_code.into())));
    }
    let ok = wait_regex(io, &t.prompt_regex, timeout)?;
    Ok((ok, (!ok).then(|| timeout_code.into())))
}

/// `_console_tail_line` 的純函式部分：`clean_text(tail).rstrip("\r\n").split("\n")[-1]`。
fn console_tail_line(rx_tail: &str) -> String {
    let cleaned = clean_text(rx_tail);
    let trimmed = cleaned.trim_end_matches(['\r', '\n']);
    trimmed.rsplit('\n').next().unwrap_or("").to_string()
}

/// `_wait_console_idle`：每筆真實 RX 都重設靜默計時；此階段完全不送 UART bytes。
fn wait_console_idle(io: &LoginIo, p: &SessionProfile, deadline: u64) -> Step<bool> {
    let idle_s = p.template.console_unlock_idle_s;
    let idle_ns = secs(idle_s).as_nanos().min(u64::MAX as u128) as u64;
    let start = io.clock.mono_ns();
    loop {
        if io.cancel.is_triggered() {
            return Err(LoginOutcome::Aborted(LoginAbort::Cancelled));
        }
        let now = io.clock.mono_ns();
        if now >= deadline {
            return Ok(false);
        }
        if now.saturating_sub(start) >= idle_ns && io.rx.idle_s(now) >= idle_s {
            return Ok(true);
        }
        let remaining = Duration::from_nanos(deadline - now);
        io.clock
            .sleep(IDLE_POLL_MAX.min(IDLE_POLL_MIN.max(remaining)));
    }
}

/// `_ensure_console_unlocked`：隱藏 console lock——靜默後辨識 prompt，必要時才送不記錄的 secret。
fn ensure_console_unlocked(
    io: &LoginIo,
    p: &SessionProfile,
    auth: &SessionAuth,
) -> Step<Option<String>> {
    let t = &p.template;
    let Some(secret) = auth.console_unlock.as_deref().filter(|s| !s.is_empty()) else {
        return Ok(Some("CREDENTIALS_UNRESOLVED".into()));
    };
    let timeout = secs(t.timeout_s);
    let deadline = io
        .clock
        .mono_ns()
        .saturating_add(timeout.as_nanos().min(u64::MAX as u128) as u64);
    if !wait_console_idle(io, p, deadline)? {
        return Ok(Some("CONSOLE_UNLOCK_IDLE_TIMEOUT".into()));
    }
    if console_at_bootloader(&io.rx.tail(RX_TAIL_CHARS), &t.bootloader_prompts)? {
        return Ok(Some("CONSOLE_UNLOCK_BOOTLOADER".into()));
    }
    // 新 bridge 未必收到早已顯示的 shell prompt；靜默滿額後才送空行辨識。
    let mut prompt = search_or_raise(&t.prompt_regex, &console_tail_line(&io.rx.tail(RX_TAIL_CHARS)))?;
    if !prompt {
        io.rx.clear();
        send_command(io, "")?;
        let remaining = Duration::from_nanos(deadline.saturating_sub(io.clock.mono_ns()));
        prompt = wait_regex(io, &t.prompt_regex, BLANK_LINE_PROBE_MAX.min(remaining))?;
    }
    if !prompt {
        if !wait_console_idle(io, p, deadline)? {
            return Ok(Some("CONSOLE_UNLOCK_IDLE_TIMEOUT".into()));
        }
        if console_at_bootloader(&io.rx.tail(RX_TAIL_CHARS), &t.bootloader_prompts)? {
            return Ok(Some("CONSOLE_UNLOCK_BOOTLOADER".into()));
        }
        // 此 buffer 已在空行 probe 前清空；接受等待期間到達的新 prompt。
        if search_or_raise(&t.prompt_regex, &clean_text(&io.rx.text_from(0)))? {
            return finalize_ready(io, p);
        }
        if io.rx.idle_s(io.clock.mono_ns()) < t.console_unlock_idle_s {
            return Ok(Some("CONSOLE_UNLOCK_IDLE_TIMEOUT".into()));
        }
        io.rx.clear();
        let guard = send_secret(io, secret)?;
        let prompt = wait_regex_from(io, &t.prompt_regex, Some(guard.start_from()), timeout)?;
        if !prompt {
            return Ok(Some("CONSOLE_UNLOCK_PROMPT_TIMEOUT".into()));
        }
    }
    finalize_ready(io, p)
}

#[cfg(test)]
mod issue311_tests {
    use super::{
        classify_login_secret_tx_failure, send_secret, LoginIo, LoginOutcome, TxFail,
    };
    use crate::mailbox::{ControlEvent, DataEvent};
    use crate::tx_writer::{Fence, OpId, TxClass, TxReport};
    use crate::uart_bridge::{IoWorker, IoWorkerArgs, RunCancel, RxShared};
    use crate::wal::{WalConfig, WalWriter};
    use std::path::PathBuf;
    use std::sync::Arc;
    use std::time::{Duration, Instant};
    use sw_pal::fakes::FakeClock;
    use sw_pal::fakes::serial::{FakeSerialBackend, FakeWriteStep};
    use sw_pal::fakes::wal_fs::FakeWalFs;
    use sw_pal::traits::{
        Clock, DevicePath, PalError, PalErrorKind, SerialBackend, SerialConfig, TxOutcome,
        TxResult,
    };

    #[test]
    fn issue311_secret_tx_failure_does_not_disclose_length() {
        let report = TxReport {
            op_id: OpId(1),
            epoch: 1,
            result: TxResult {
                outcome: TxOutcome::Completed(1),
                error: None,
            },
            wal: None,
            reject: None,
        };
        let TxFail::Raised { exc, message } = classify_login_secret_tx_failure(&report) else {
            panic!("部分 secret TX 應回 Raised");
        };
        assert_eq!(exc, "TxIncompleteError");
        assert!(!message.contains("/11"), "secret TX 長度外洩：{message:?}");
        assert_eq!(message, "secret TX incomplete");
    }

    #[test]
    fn issue311_partial_and_unknown_secret_tx_keep_late_echo_guarded() {
        const SECRET: &str = "i311secret";
        let reports = [TxOutcome::Completed(1), TxOutcome::Unknown { confirmed: 1 }];
        for outcome in reports {
            let rx = RxShared::new(128, 0);
            let clock = FakeClock::new();
            let cancel = RunCancel::new(&rx);
            let report = TxReport {
                op_id: OpId(1),
                epoch: 1,
                result: TxResult {
                    outcome,
                    error: Some(PalError::plain(PalErrorKind::Io, "injected partial TX")),
                },
                wal: None,
                reject: None,
            };
            let tx = move |_bytes: &[u8], _class, _source: &str, _log: bool| report.clone();
            let nonce = || "n".to_string();
            let audit_rx = Arc::clone(&rx);
            let audit = |_length: usize| {
                assert_eq!(
                    audit_rx.issue311_test_mask_secret_echo(b"\r\n"),
                    b"\r\n".to_vec(),
                    "內容遮罩在 secret TX 前已啟用，不受前置換行影響"
                );
                true
            };
            let io = LoginIo {
                rx: &rx,
                tx: &tx,
                clock: clock.as_ref() as &dyn Clock,
                nonce: &nonce,
                audit_secret_tx: &audit,
                cancel: &cancel,
                class: TxClass::LoginProbe,
            };
            let result = send_secret(&io, SECRET);
            assert!(
                matches!(result, Err(LoginOutcome::Raised { .. })),
                "possible partial submission keeps the original TX error"
            );
            let visible = rx.issue311_test_mask_secret_echo(SECRET.as_bytes());
            assert_eq!(
                visible,
                vec![b'*'; SECRET.len()],
                "登入流程結束前遲到的 secret echo 必須遮罩"
            );
            let prompt = b"\r\nroot@t:/# ";
            assert_eq!(
                rx.issue311_test_mask_secret_echo(prompt),
                prompt,
                "prompt 本身保持原文"
            );
            assert_eq!(
                rx.issue311_test_mask_secret_echo(SECRET.as_bytes()),
                SECRET.as_bytes(),
                "第一個完整命中後停用內容遮罩"
            );

            let result = send_secret(&io, SECRET);
            assert!(matches!(result, Err(LoginOutcome::Raised { .. })));
            rx.disarm_secret_echo_for_login_end();
            assert_eq!(
                rx.issue311_test_mask_secret_echo(SECRET.as_bytes()),
                SECRET.as_bytes(),
                "login flow failure 結束時停用內容遮罩"
            );
        }
    }

    #[test]
    fn issue311_secret_submit_with_full_data_lane_allows_shutdown_to_finish() {
        const SECRET: &str = "i311secret";
        const EPOCH: u64 = 3;
        let serial = FakeSerialBackend::new();
        let device = serial.add_device("/dev/issue311");
        let handle = serial
            .open_exclusive(
                &DevicePath("/dev/issue311".into()),
                &SerialConfig::from_uart_profile(115_200, 8, "N", 1, "none", false),
            )
            .expect("開啟 fake UART");
        let rx = RxShared::new(128, 0);
        let clock = FakeClock::new();
        let clock_dyn: Arc<dyn Clock> = clock.clone();
        let wal = Arc::new(WalWriter::open(
            WalConfig {
                dir: PathBuf::from("/w"),
                rotate_bytes: 1 << 20,
            },
            FakeWalFs::new(),
            clock_dyn.clone(),
        ));
        let fence = Fence::new();
        fence.with(|state| state.active_epoch = Some(EPOCH));
        let (data_tx, data_rx) = crossbeam_channel::bounded(1);
        data_tx
            .send(DataEvent::UartRxChunk {
                epoch: EPOCH,
                rx_seq: 0,
                wal_seq: 0,
                bytes: vec![0],
            })
            .expect("先填滿 data lane");
        let (control_tx, control_rx) = crossbeam_channel::bounded::<ControlEvent>(16);
        let worker = IoWorker::start(
            handle,
            IoWorkerArgs {
                com: "COM0".into(),
                epoch: EPOCH,
                wal,
                rx: rx.clone(),
                fence: fence.clone(),
                sentinel: None,
                sentinel_keepalive: None,
                data: data_tx,
                control: control_tx.clone(),
                clock: clock_dyn.clone(),
                hub: None,
            },
        );
        let input = b"reader is backpressured by a full data lane";
        device.push_rx(input);
        let read_deadline = Instant::now() + Duration::from_secs(2);
        while device.rx_bytes_read() < input.len() || rx.text_from(0).len() < input.len() {
            assert!(Instant::now() < read_deadline, "reader 未填滿 data lane");
            std::thread::sleep(Duration::from_millis(2));
        }
        assert_eq!(data_rx.len(), 1, "reader 發布前 data lane 必須仍滿載");

        device.script_write(vec![FakeWriteStep::PauseInsideSubmit]);
        let tx_sender = super::login_tx_sender(worker.tx().clone(), EPOCH, 0, 0);
        let rx_for_secret = rx.clone();
        let clock_for_secret = clock_dyn.clone();
        let (secret_done_tx, secret_done_rx) = crossbeam_channel::bounded(1);
        let (actor_done_tx, actor_done_rx) = crossbeam_channel::bounded(1);
        let (actor_started_tx, actor_started_rx) = crossbeam_channel::bounded(1);
        let at_os_submit = std::thread::scope(|scope| {
            scope.spawn(move || {
                let tx = move |bytes: &[u8], class, source: &str, log| {
                    tx_sender(bytes, class, source, log)
                };
                let nonce = || "n".to_string();
                let audit = |_length: usize| true;
                let cancel = RunCancel::new(&rx_for_secret);
                let io = LoginIo {
                    rx: &rx_for_secret,
                    tx: &tx,
                    clock: clock_for_secret.as_ref(),
                    nonce: &nonce,
                    audit_secret_tx: &audit,
                    cancel: &cancel,
                    class: TxClass::LoginProbe,
                };
                let result = send_secret(&io, SECRET);
                let _ = secret_done_tx.send(result.map(drop).is_ok());
            });

            let reached_submit = device.wait_paused(Duration::from_secs(1));
            let shutdown_fence = fence.clone();
            scope.spawn(move || {
                let _ = actor_started_tx.send(());
                shutdown_fence.with(|state| {
                    state.frozen = true;
                    state.active_epoch = None;
                });
                let _ = actor_done_tx.send(());
            });
            actor_started_rx
                .recv_timeout(Duration::from_secs(1))
                .expect("shutdown actor 已開始取得 fence");
            let actor_blocked_before_cleanup = if reached_submit {
                std::thread::sleep(Duration::from_millis(10));
                device.release_pause();
                false
            } else {
                let blocked = actor_done_rx
                    .recv_timeout(Duration::from_millis(100))
                    .is_err();
                // 舊提交 hook 可能卡在 reader 的滿載發布；開始停止讓基準重現可收尾。
                worker.begin_stop();
                if device.wait_paused(Duration::from_secs(1)) {
                    device.release_pause();
                }
                blocked
            };
            let actor_done = actor_done_rx.recv_timeout(Duration::from_secs(2));
            let secret_done = secret_done_rx.recv_timeout(Duration::from_secs(2));
            worker.begin_stop();
            (
                reached_submit,
                actor_blocked_before_cleanup,
                secret_done,
                actor_done,
            )
        });

        worker.finish_async(crate::drain::StopId(311), control_tx);
        let stop_deadline = Instant::now() + Duration::from_secs(2);
        let mut worker_stopped = false;
        while Instant::now() < stop_deadline {
            match control_rx.recv_timeout(Duration::from_millis(50)) {
                Ok(ControlEvent::WorkerStopped(receipt)) => {
                    assert!(receipt.handle_closed, "fake UART handle 應已關閉");
                    worker_stopped = true;
                    break;
                }
                Ok(_) | Err(crossbeam_channel::RecvTimeoutError::Timeout) => {}
                Err(crossbeam_channel::RecvTimeoutError::Disconnected) => break,
            }
        }

        assert!(
            at_os_submit.0
                && !at_os_submit.1
                && at_os_submit.2.as_ref().is_ok_and(|completed| *completed)
                && at_os_submit.3.is_ok()
                && worker_stopped,
            "滿 data lane 下 secret submit 與 shutdown 必須在期限內完成；\n             os_submit={}; actor_blocked_before_cleanup={}; secret={:?}; shutdown={:?}; worker_stopped={worker_stopped}",
            at_os_submit.0,
            at_os_submit.1,
            at_os_submit.2,
            at_os_submit.3
        );
    }
}
