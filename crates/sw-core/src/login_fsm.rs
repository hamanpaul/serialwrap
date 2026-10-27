//! 登入狀態機（P5-T13 最小版 → P7 Task 3 全平台；05 §8；逐函式對照 `sw_core/login_fsm.py`）。
//!
//! - `choose_login_plan`：sans-IO 的計畫選擇（passthrough／不具 command capability／boot quiet／帳密未解析／
//!   Slice 2.1 不支援能力 → 不送任何 TX；Slice 2.1 能力閘的退役待 08 §0.6 退役清單的 L1 鎖裁決）。
//! - `probe_ready`／`ensure_ready`／`detect_template`：在 OpRunner（或測試 executor）thread 執行；TX 一律經
//!   `LoginIo.tx`（TxWriter，class 由呼叫端以 `LoginIo.class` 決定），RX 比對整個 RX 視窗（Python
//!   `wait_for_regex` 語意，SES-16.6-05 KEEP），等待可經 `LoginIo.cancel` 取消（SC-30）。
//! - TX 回報對應（§11.3 不得自動重送）：fence 拒絕 → `Aborted(Refused)`；未提交且無拒絕 → `Aborted(Cancelled)`；
//!   部分寫出或結果不明 → `Aborted(TxIncomplete)`；之後一律不再呼叫 `tx`。Python 會拋例外處（無效 regex）
//!   → `Raised("error")`。
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
use crate::uart_bridge::{RunCancel, RxShared, WaitOutcome};

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
    /// P7 Task 3：TX 被拒／取消／結果不完整，之後零 TX。
    Aborted(LoginAbort),
    /// P7 Task 3：Python 會拋例外處（無效 regex → `"error"`），值為例外型別名。
    Raised(String),
}

/// `LoginOutcome::Aborted` 的原因。
#[derive(Debug, Clone, PartialEq)]
pub enum LoginAbort {
    Refused(FenceReject),
    Cancelled,
    TxIncomplete,
}

/// 登入 FSM 所需的 I/O 注入點（TX 經 TxWriter；測試以閉包替身）。
pub struct LoginIo<'a> {
    pub rx: &'a RxShared,
    /// P7 Task 3：回 06 T9 `TxReport`（取得 fence 拒絕原因）。
    pub tx: &'a dyn Fn(&[u8], TxClass, &str, bool) -> TxReport,
    pub clock: &'a dyn Clock,
    pub nonce: &'a dyn Fn() -> String,
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
/// 宣告但未解析的帳密（`CredentialsUnresolved`）→ Slice 2.1 不支援能力（`None`）→ `EnsureReady`／`Probe`。
/// 帳密閘先於能力閘：未解析帳密的 `CREDENTIALS_UNRESOLVED` 終態與 Python 一致（T11 `console_unlock_env_is_a_credential_source`）。
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
    if crate::quiet_gate::slice21_unsupported_feature(p).is_some() {
        return (LoginPlan::None, None);
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
    finish(ensure_ready_inner(io, p, auth, env), true)
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
                input_epoch: None,
                replay_hold: None,
            },
            payload: payload.to_vec(),
            reply,
        };
        let not_submitted = TxReport {
            op_id,
            epoch,
            result: TxResult {
                outcome: TxOutcome::NotSubmitted,
                error: None,
            },
            wal: None,
            reject: None,
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
        Duration::from_secs_f64(s)
    } else {
        Duration::ZERO
    }
}

fn compile_or_raise(pattern: &str) -> Step<PyRegex> {
    compile(pattern, PyFlags(0)).map_err(|_| LoginOutcome::Raised("error".to_string()))
}

fn search_or_raise(pattern: &str, text: &str) -> Step<bool> {
    Ok(compile_or_raise(pattern)?.search(text).is_some())
}

fn send(io: &LoginIo, payload: &str, source: &str, log: bool) -> Step<()> {
    let mut bytes = payload.as_bytes().to_vec();
    if !bytes.ends_with(b"\n") {
        bytes.push(b'\n');
    }
    let report = (io.tx)(&bytes, io.class, source, log);
    if let Some(reject) = report.reject {
        return Err(LoginOutcome::Aborted(LoginAbort::Refused(reject)));
    }
    match report.result.outcome {
        TxOutcome::Completed(n) if n >= bytes.len() => Ok(()),
        TxOutcome::NotSubmitted => Err(LoginOutcome::Aborted(LoginAbort::Cancelled)),
        TxOutcome::Completed(_) | TxOutcome::Unknown { .. } => {
            Err(LoginOutcome::Aborted(LoginAbort::TxIncomplete))
        }
    }
}

fn send_command(io: &LoginIo, cmd: &str) -> Step<()> {
    send(io, cmd, "system", true)
}

fn send_secret(io: &LoginIo, secret: &str) -> Step<()> {
    send(io, secret, "system:secret", false)
}

/// `bridge.wait_for_regex(pattern, timeout_s)`：regex 先編譯（失敗 → `Raised`），再以整個 RX 視窗等待。
fn wait_regex(io: &LoginIo, pattern: &str, timeout: Duration) -> Step<bool> {
    let re = compile_or_raise(pattern)?;
    match io
        .rx
        .wait_for_regex_cancellable(&re, None, timeout, io.clock, io.cancel)
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
        send_secret(io, &password)?;
        let ok = wait_regex(io, &t.prompt_regex, timeout)?;
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
        send_secret(io, secret)?;
        if !wait_regex(io, &t.prompt_regex, timeout)? {
            return Ok(Some("CONSOLE_UNLOCK_PROMPT_TIMEOUT".into()));
        }
    }
    finalize_ready(io, p)
}
