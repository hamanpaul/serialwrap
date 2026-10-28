//! P5-T13：最小登入狀態機（05 §8；對照 `sw_core/login_fsm.py` 與 `session_manager._attach_by_id` 的計畫選擇）。
//!
//! - `choose_login_plan`：sans-IO 的計畫選擇（passthrough／不具 command capability／boot quiet／帳密未解析／
//!   Slice 2.1 不支援能力 → 不送任何 TX）。
//! - `probe_ready`／`ensure_ready`：在 OpRunner（或測試 executor）thread 執行；TX 一律經 `LoginIo.tx`
//!   （TxWriter，class `LoginProbe`），RX 比對整個 RX 視窗（Python `wait_for_regex` 語意，SES-16.6-05 KEEP）。
//! - Slice 2.1 不承接 `post_login_cmd` 與 console unlock（能力閘在 `choose_login_plan` 擋下）。

use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::Duration;

use sw_pal::traits::{Clock, SysEnv, TxOutcome, TxResult};
use sw_proto::config_types::SessionProfile;
use sw_proto::pycompat::clean_text;
use sw_proto::pyre::{PyFlags, compile};

use crate::session_auth::{AuthResolution, SessionAuth, credentials_declared_but_unresolved};
use crate::tx_writer::{FenceReject, OpId, TxClass, TxJob, TxPermit, TxReport, TxWriterHandle};
use crate::uart_bridge::RxShared;

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
    /// probe 失敗或 TX 遭 fence 拒絕：停 ATTACHED，內含 last_error。
    NotReady(String),
    /// `ensure_ready` 失敗：attach 路徑 → drain（`LoginFailed`），既有 bridge 路徑 → ATTACHED。
    Failed(String),
    /// TX 未完整提交且不是 quiet／較新 login run 的拒絕（Python `_login_tx_refused` 為假 → 例外上拋；審查 F16）：
    /// `exc` 為 Python 例外類別名（`WalUnavailableError`／`TxUnknownError`／`TxIncompleteError`），`message` 為 `str(exc)`。
    /// attach 路徑 → DETACHED＋`ATTACH_FAILED:<exc>`；既有 bridge 再探 → RPC `EXCEPTION`、state 不變；
    /// reboot recovery → `RECOVERY_ERROR`。
    Raised { exc: String, message: String },
}

/// 登入 FSM 所需的 I/O 注入點（TX 經 TxWriter；測試以閉包替身）。
pub struct LoginIo<'a> {
    pub rx: &'a RxShared,
    /// 送出一筆登入 TX，回傳 TxWriter 的結果（含 fence 拒絕原因，供 `_login_tx_refused` 分流）。
    pub tx: &'a dyn Fn(&[u8], TxClass, &str, bool) -> TxReport,
    pub clock: &'a dyn Clock,
    pub nonce: &'a dyn Fn() -> String,
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

/// `RX_FLOOD_BYTES_PER_10S`（constants.py）。
const RX_FLOOD_BYTES_PER_10S: u64 = 20_000;
/// `rx_tail()` 預設字元數（uart_io.py）。
const RX_TAIL_CHARS: usize = 4096;
/// `_login_fsm_rx_tail_detail` 截尾字元數。
const DETAIL_TAIL_CHARS: usize = 300;
/// 等待輪詢：每輪最多推進的注入時鐘時間（Python 以 50 ms 輪詢）。
const WAIT_CLOCK_SLICE: Duration = Duration::from_millis(10);
/// 等待輪詢：每輪的實時讓出（讓 reader thread 交付 RX；期限一律以注入 Clock 判定）。
const WAIT_REAL_YIELD: Duration = Duration::from_millis(5);
/// 登入 TX 的 op_id 區段（與 SessionCore 前景 op 的遞增序號不重疊）。
const LOGIN_OP_ID_BASE: u64 = 1 << 62;

static NEXT_LOGIN_OP: AtomicU64 = AtomicU64::new(0);

fn declared(value: &Option<String>) -> bool {
    value.as_deref().is_some_and(|v| !v.is_empty())
}

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

/// 只做 probe：送 `\n` 並等 prompt；失敗時依視窗分類（05 §8.3；`login_fsm.probe_ready`）。
pub fn probe_ready(io: &LoginIo, p: &SessionProfile) -> LoginOutcome {
    match probe_prompt(io, p) {
        Err(fail) => return fail.outcome(),
        Ok(false) => {
            let code = classify_non_ready_state(io, p);
            return LoginOutcome::NotReady(maybe_reclassify_flood(io, code).to_string());
        }
        Ok(true) => {}
    }
    match finalize_ready(io, p) {
        Err(fail) => fail.outcome(),
        Ok(None) => LoginOutcome::Ready,
        Ok(Some(code)) => LoginOutcome::NotReady(maybe_reclassify_flood(io, code).to_string()),
    }
}

/// 完整登入：login → password → prompt → nonce 驗證（05 §8.3；`login_fsm.ensure_ready`）。
pub fn ensure_ready(
    io: &LoginIo,
    p: &SessionProfile,
    auth: &SessionAuth,
    env: &dyn SysEnv,
) -> LoginOutcome {
    if declared(&p.template.console_unlock_env) {
        // Slice 2.1 不承接 console unlock（能力閘已在 choose_login_plan 擋下，不自動送 TX）。
        if auth.console_unlock.as_deref().is_none_or(str::is_empty) {
            return LoginOutcome::Failed("CREDENTIALS_UNRESOLVED".into());
        }
        return LoginOutcome::NotReady("PROMPT_UNAVAILABLE".into());
    }
    match probe_prompt(io, p) {
        Err(fail) => return fail.outcome(),
        Ok(true) => {}
        Ok(false) => match maybe_login(io, p, auth, env) {
            Err(fail) => return fail.outcome(),
            Ok((_, Some(code))) => {
                return LoginOutcome::Failed(maybe_reclassify_flood(io, code).to_string());
            }
            Ok((false, None)) => {
                let code = prompt_timeout_error(&p.template.platform);
                return LoginOutcome::Failed(maybe_reclassify_flood(io, code).to_string());
            }
            Ok((true, None)) => {}
        },
    }
    match finalize_ready(io, p) {
        Err(fail) => fail.outcome(),
        Ok(None) => LoginOutcome::Ready,
        Ok(Some(code)) => LoginOutcome::Failed(maybe_reclassify_flood(io, code).to_string()),
    }
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
/// 每筆 TX 取得 `TxReport` 後立即 settle（登入 op 不參與 BC-03 映射）。
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
                // 07 D-03：06 既有 permit 建構點一律 `None`。
                input_epoch: None,
                replay_hold: None,
            },
            payload: payload.to_vec(),
            reply,
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

/// 登入 TX 未完整提交（Python `send_command` 拋 `TxIncompleteError` 家族）。
#[derive(Debug, Clone, PartialEq)]
pub enum TxFail {
    /// `_login_tx_refused` 為真（fence 以 quiet 或較新 login run 拒絕）→ `PROMPT_UNAVAILABLE`，不精煉。
    Refused,
    /// 其餘拒絕、結果不明或部分寫出：例外上拋（類別名與 `str(exc)`）。
    Raised { exc: &'static str, message: String },
}

impl TxFail {
    fn outcome(self) -> LoginOutcome {
        match self {
            TxFail::Refused => LoginOutcome::NotReady("PROMPT_UNAVAILABLE".into()),
            TxFail::Raised { exc, message } => LoginOutcome::Raised {
                exc: exc.to_string(),
                message,
            },
        }
    }
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

fn send(io: &LoginIo, payload: &str, source: &str, log: bool) -> Result<(), TxFail> {
    let mut bytes = payload.as_bytes().to_vec();
    if !bytes.ends_with(b"\n") {
        bytes.push(b'\n');
    }
    let report = (io.tx)(&bytes, TxClass::LoginProbe, source, log);
    match report.result.outcome {
        TxOutcome::Completed(n) if n >= bytes.len() => Ok(()),
        _ => Err(classify_login_tx_failure(&report, bytes.len())),
    }
}

fn send_command(io: &LoginIo, cmd: &str) -> Result<(), TxFail> {
    send(io, cmd, "system", true)
}

fn send_secret(io: &LoginIo, secret: &str) -> Result<(), TxFail> {
    send(io, secret, "system:secret", false)
}

/// 以整個 RX 視窗等待 `pred` 成立；期限依注入 Clock（每輪最多推進 `WAIT_CLOCK_SLICE`）。
fn wait_for(io: &LoginIo, timeout_s: f64, pred: &dyn Fn(&str) -> bool) -> bool {
    let timeout_ns = (timeout_s.max(0.0) * 1_000_000_000.0) as u64;
    let deadline = io.clock.mono_ns().saturating_add(timeout_ns);
    let slice_ns = WAIT_CLOCK_SLICE.as_nanos() as u64;
    loop {
        if pred(&io.rx.text_from(0)) {
            return true;
        }
        let now = io.clock.mono_ns();
        if now >= deadline {
            return false;
        }
        let step = deadline.saturating_sub(now).min(slice_ns);
        std::thread::sleep(WAIT_REAL_YIELD.min(Duration::from_nanos(step)));
        let advanced = io.clock.mono_ns().saturating_sub(now);
        if advanced < step {
            io.clock.sleep(Duration::from_nanos(step - advanced));
        }
    }
}

fn wait_for_regex(io: &LoginIo, pattern: &str, timeout_s: f64) -> bool {
    match compile(pattern, PyFlags(0)) {
        Ok(re) => wait_for(io, timeout_s, &|text| re.search(text).is_some()),
        // Python `re.compile` 會拋例外；Rust 以「永不命中」收斂（逾時）。
        Err(_) => wait_for(io, timeout_s, &|_| false),
    }
}

fn probe_prompt(io: &LoginIo, p: &SessionProfile) -> Result<bool, TxFail> {
    io.rx.clear();
    send_command(io, "")?;
    Ok(wait_for_regex(io, &p.template.prompt_regex, p.template.timeout_s))
}

fn classify_non_ready_state(io: &LoginIo, p: &SessionProfile) -> &'static str {
    if matches_login_or_password(&io.rx.tail(RX_TAIL_CHARS), p) {
        "LOGIN_REQUIRED"
    } else {
        "PROMPT_UNAVAILABLE"
    }
}

fn maybe_reclassify_flood(io: &LoginIo, code: &'static str) -> &'static str {
    if !FLOOD_MASKABLE_ERRORS.contains(&code) && !code.ends_with("_PROMPT_TIMEOUT") {
        return code;
    }
    if io.rx.rx_stats(io.clock.mono_ns()).0 >= RX_FLOOD_BYTES_PER_10S {
        "RX_FLOOD"
    } else {
        code
    }
}

/// nonce 驗證（Slice 2.1 不送 `post_login_cmd`）；`Ok(None)` 為成功。
fn finalize_ready(io: &LoginIo, p: &SessionProfile) -> Result<Option<&'static str>, TxFail> {
    let t = &p.template;
    let nonce = (io.nonce)();
    let probe = t.ready_probe.replace("${nonce}", &nonce);
    send_command(io, &probe)?;
    // 裸 nonce 字串（8 位 hex，regex 與字面相同）比對整個視窗：命令回顯即可滿足（SES-16.6-05 KEEP）。
    if !wait_for(io, t.timeout_s, &|text| text.contains(nonce.as_str())) {
        if matches_login_or_password(&io.rx.tail(RX_TAIL_CHARS), p) {
            return Ok(Some("LOGIN_REQUIRED"));
        }
        return Ok(Some("READY_NONCE_TIMEOUT"));
    }
    if !wait_for_regex(io, &t.prompt_regex, t.timeout_s) {
        return Ok(Some("READY_PROMPT_TIMEOUT"));
    }
    Ok(None)
}

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
) -> Result<(bool, Option<&'static str>), TxFail> {
    let t = &p.template;
    let (user, _unreachable_err) = resolve_login_user(p, auth, env);
    let needs_login = user.is_some() || t.pass_env.as_deref().is_some_and(|k| !k.is_empty());
    if !needs_login {
        return Ok((false, None));
    }
    if !wait_for_regex(io, &t.login_regex, t.timeout_s) {
        return Ok((false, Some("LOGIN_PROMPT_TIMEOUT")));
    }
    let Some(user) = user else {
        return Ok((false, Some("LOGIN_USER_REQUIRED")));
    };
    send_command(io, &user)?;
    if wait_for_regex(io, &t.password_regex, t.timeout_s) {
        let password = match resolve_login_password(p, auth, env) {
            Ok(password) => password,
            Err(code) => return Ok((false, Some(code))),
        };
        send_secret(io, &password)?;
        let ok = wait_for_regex(io, &t.prompt_regex, t.timeout_s);
        io.rx.clear();
        return Ok(if ok {
            (true, None)
        } else {
            (false, Some(prompt_timeout_error(&t.platform)))
        });
    }
    let ok = wait_for_regex(io, &t.prompt_regex, t.timeout_s);
    Ok(if ok {
        (true, None)
    } else {
        (false, Some(prompt_timeout_error(&t.platform)))
    })
}
