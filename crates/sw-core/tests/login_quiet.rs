//! Slice 2.1 Task 13：最小登入、帳密未解析終態、boot quiet gate、Ready Reconfirm 與 Profile 能力閘的紅燈測試
//! （plan 06 Task 13，§5.3、§8.2、SC-03、SC-34、SC-35、DF-02、Review Focus 3）。
//!
//! 紅燈階段：行為尚未實作，失敗應為 `not implemented: slice21 T<n> …`（執行期 panic），而非編譯錯誤。
//! - 純函式列（QuietState、session_auth、login_fsm 的 probe／ensure）以 `LoginRig`（FakeClock＋RxShared＋模擬裝置）驅動；
//! - sans-IO 列以 `CoreHarness`（不起 thread）驅動；
//! - 需要真 actor thread 的列以 `ActorRig` 驅動。

#![cfg(feature = "test-fakes")]

mod support;

use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use support::*;
use sw_core::login_fsm::{
    choose_login_plan, ensure_ready, probe_ready, prompt_timeout_error, LoginIo, LoginOutcome,
    LoginPlan, FLOOD_MASKABLE_ERRORS, LOGIN_FSM_DETAIL_ERRORS,
};
use sw_core::quiet_gate::{
    detect_boot_banner, is_reboot_command, matches_any_bootloader_prompt,
    slice21_unsupported_feature, valid_completion_regex, QuietState, QuietTransition,
    BOOT_BANNER_PATTERNS,
};
use sw_core::session_auth::{
    credentials_declared_but_unresolved, parse_env_file, resolve_session_auth, AuthResolution,
    SessionAuth,
};
use sw_core::mailbox::ControlEvent;
use sw_core::session_core::SessionRequest;
use sw_core::tx_writer::TxClass;
use sw_core::Mono;
use sw_pal::fakes::clock::FakeClock;
use sw_pal::fakes::env::FakeEnv;
use sw_pal::traits::{Clock, PalError, TxOutcome, TxResult};
use sw_pal::SysEnv;
use sw_proto::pyjson::{PyDict, PyValue};

// ---------------------------------------------------------------------------
// 共用輔助
// ---------------------------------------------------------------------------

const BANNER: &str = "\r\nU-Boot 2024.01 (Jan 01)\r\n";
const NONCE: &str = "ab12cd34";
const LOGIN_PROFILE: &str = "profiles:\n  l:\n    platform: shell\n    prompt_regex: '(?m)^root@t:.*# '\n    login_regex: 'login: ?$'\n    password_regex: 'Password: ?$'\n    timeout_s: 2.0\ntargets:\n  - {com: COM0, profile: l, act_no: 1, alias: l+1, device_by_id: /s/by-id/usb-A}\n";
const LOGIN_ENV_PROFILE: &str = "profiles:\n  e:\n    platform: shell\n    prompt_regex: '(?m)^root@t:.*# '\n    login_regex: 'login: ?$'\n    password_regex: 'Password: ?$'\n    user_env: SW_T_U\n    pass_env: SW_T_P\n    timeout_s: 2.0\ntargets:\n  - {com: COM0, profile: e, act_no: 1, alias: e+1, device_by_id: /s/by-id/usb-A}\n";
const BOOTLOADER_PROFILE: &str = "profiles:\n  b:\n    platform: shell\n    prompt_regex: '(?m)^root@t:.*# '\n    bootloader_prompts: ['(?m)^=> $']\n    timeout_s: 2.0\ntargets:\n  - {com: COM0, profile: b, act_no: 1, alias: b+1, device_by_id: /s/by-id/usb-A}\n";
const BRCM_POST_LOGIN_PROFILE: &str = "profiles:\n  brcm-template:\n    platform: bcm\n    prompt_regex: '(?m)^root@t:.*# '\n    login_regex: 'login: ?$'\n    password_regex: 'Password: ?$'\n    post_login_cmd: sh\n    timeout_s: 2.0\ntargets:\n  - {com: COM0, profile: brcm-template, act_no: 1, alias: b+1, device_by_id: /s/by-id/usb-A}\n";

/// prompt 以 `$` 錨定視窗尾端：nonce 回顯後若無新 prompt，整窗比對即不命中（READY_PROMPT_TIMEOUT 可達）。
/// Python `wait_for_regex` 比對整個 RX 視窗（SES-16.6-05 KEEP），`(?m)^root@t:.*# ` 這類 prompt 會被 probe 早已收到的
/// prompt 滿足，READY_PROMPT_TIMEOUT 不可達（Python 實測 `probe_ready` 回 `(True, None)`）。
const ANCHORED_PROMPT_PROFILE: &str = "profiles:\n  a:\n    platform: shell\n    prompt_regex: 'root@t:/# $'\n    timeout_s: 2.0\ntargets:\n  - {com: COM0, profile: a, act_no: 1, alias: a+1, device_by_id: /s/by-id/usb-A}\n";

/// #93：`bootloader_prompts` 含不合法 regex（`[invalid(`）與合法樣式。
const INVALID_BOOTLOADER_PROFILE: &str = "profiles:\n  b:\n    platform: shell\n    prompt_regex: '(?m)^root@t:.*# '\n    bootloader_prompts: ['[invalid(', '(?m)^=> $']\n    timeout_s: 2.0\ntargets:\n  - {com: COM0, profile: b, act_no: 1, alias: b+1, device_by_id: /s/by-id/usb-A}\n";

fn secs(s: u64) -> Mono {
    s * 1_000_000_000
}

fn pget(d: &PyDict, k: &str) -> PyValue {
    d.get(k).cloned().unwrap_or(PyValue::Null)
}

fn tx_ok(n: usize) -> TxResult {
    TxResult {
        outcome: TxOutcome::Completed(n),
        error: None,
    }
}

/// `probe_ready`／`ensure_ready` 的模擬裝置行為（Shell＝正常 root shell；Login＝login/Password 流程）。
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
enum Dev {
    /// LF→prompt；nonce 探針→回顯＋prompt。
    Shell,
    /// 完全不回應。
    Silent,
    /// LF→`login:`。
    LoginPrompt,
    /// LF→prompt；nonce 探針無回應。
    NonceSilent,
    /// LF→prompt；nonce 探針只回顯、無 prompt。
    PromptNoNonce,
    /// LF→`login:`；user→`Password:`；secret→prompt；nonce→回顯＋prompt。
    Login,
}

/// 一筆 TX 紀錄（`LoginIo.tx` 的參數）。
#[derive(Clone, Debug)]
struct Sent {
    bytes: Vec<u8>,
    class: TxClass,
    source: String,
    log: bool,
}

fn device_reply(dev: Dev, bytes: &[u8]) -> Vec<u8> {
    let text = String::from_utf8_lossy(bytes).into_owned();
    const PROMPT: &str = "\r\nroot@t:/# ";
    let out: &str = if text.contains("__READY__") {
        match dev {
            Dev::Shell | Dev::Login => "__READY__ab12cd34\r\nroot@t:/# ",
            Dev::PromptNoNonce => "__READY__ab12cd34\r\n",
            Dev::Silent | Dev::LoginPrompt | Dev::NonceSilent => "",
        }
    } else if text.contains("s3cret") {
        match dev {
            Dev::Login => PROMPT,
            _ => "",
        }
    } else if text.contains("root") && dev == Dev::Login {
        "\r\nPassword: "
    } else {
        match dev {
            Dev::Shell | Dev::NonceSilent | Dev::PromptNoNonce => PROMPT,
            // 依 `Dev::Login` 文件：LF→`login:`（原列於 PROMPT 分支，與文件矛盾）。
            Dev::LoginPrompt | Dev::Login => "\r\nlogin: ",
            Dev::Silent => "",
        }
    };
    out.as_bytes().to_vec()
}

/// `probe_ready`／`ensure_ready` 的單元驅動：RX 視窗、FakeClock、TX 紀錄與模擬裝置。
struct LoginRig {
    rx: Arc<sw_core::uart_bridge::RxShared>,
    clock: Arc<FakeClock>,
    written: Arc<Mutex<Vec<Sent>>>,
}

impl LoginRig {
    fn new() -> LoginRig {
        let clock = FakeClock::new();
        let rx = sw_core::uart_bridge::RxShared::new(131_072, clock.mono_ns());
        LoginRig {
            rx,
            clock,
            written: Arc::new(Mutex::new(Vec::new())),
        }
    }

    /// 注入 `n` bytes 的 RX（rate window 內，供 RX_FLOOD 判定）。
    fn flood(&self, n: usize) {
        let now = self.clock.mono_ns();
        self.rx.append(&vec![b'x'; n], now, 1, 1);
    }

    fn sent(&self) -> Vec<Sent> {
        self.written
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .clone()
    }

    /// 以模擬裝置 `dev` 建立 `LoginIo` 後執行 `f`。
    fn run<R>(&self, dev: Dev, f: impl FnOnce(&LoginIo) -> R) -> R {
        let rx = Arc::clone(&self.rx);
        let clock = Arc::clone(&self.clock);
        let written = Arc::clone(&self.written);
        let seq = Arc::new(AtomicU64::new(100));
        let tx = move |b: &[u8], class: TxClass, source: &str, log: bool| -> TxResult {
            written
                .lock()
                .unwrap_or_else(|e| e.into_inner())
                .push(Sent {
                    bytes: b.to_vec(),
                    class,
                    source: source.to_string(),
                    log,
                });
            let reply = device_reply(dev, b);
            if !reply.is_empty() {
                let now = clock.mono_ns();
                let s = seq.fetch_add(1, Ordering::SeqCst);
                rx.append(&reply, now, 1, s);
            }
            tx_ok(b.len())
        };
        let nonce = || NONCE.to_string();
        let io = LoginIo {
            rx: &self.rx,
            tx: &tx,
            clock: &*self.clock,
            nonce: &nonce,
        };
        f(&io)
    }
}

/// 模擬裝置對 nonce 探針的正常回應（回顯 nonce＋prompt）。
fn answer_nonce(dev: &sw_pal::fakes::serial::FakeDevice) {
    dev.on_tx_match(
        r"__READY__[0-9a-f]+",
        Box::new(|line: &[u8]| {
            let text = String::from_utf8_lossy(line).into_owned();
            let token = text
                .split("__READY__")
                .nth(1)
                .unwrap_or("")
                .trim()
                .to_string();
            format!("__READY__{token}\r\nroot@t:/# ").into_bytes()
        }),
    );
}

fn outcome_code(o: &LoginOutcome) -> String {
    match o {
        LoginOutcome::NotReady(c) | LoginOutcome::Failed(c) => c.clone(),
        LoginOutcome::Ready => panic!("預期失敗碼，實際 Ready"),
    }
}

fn auth(user: Option<&str>, pass: Option<&str>) -> SessionAuth {
    SessionAuth {
        username: user.map(str::to_string),
        password: pass.map(str::to_string),
        console_unlock: None,
    }
}

fn resolution(reason: &'static str) -> AuthResolution {
    AuthResolution {
        reason,
        env_file_path: None,
    }
}

// ---------------------------------------------------------------------------
// 程式碼案例（Step 1 逐字）
// ---------------------------------------------------------------------------

#[test]
fn credentials_unresolved_is_terminal_attached_and_sends_no_credentials() {
    let h = ActorRig::new(CRED_PROFILE).attach(); // env_file 不存在、env 無 SW_T_U/SW_T_P
    h.wait_state("ATTACHED");
    let s = h.get_state();
    assert_eq!(
        pget(&s, "last_error"),
        PyValue::from("CREDENTIALS_UNRESOLVED")
    );
    assert_eq!(h.dev.write_calls(), 0, "no probe, no empty username");
    let metas = h.wal_meta_events();
    let credentials: Vec<&PyDict> = metas
        .iter()
        .filter(|m| pget(m, "event") == PyValue::from("credentials_unresolved"))
        .collect();
    assert_eq!(credentials.len(), 1);
    assert_eq!(
        pget(credentials[0], "reason"),
        PyValue::from("env_file_missing")
    );
    assert_eq!(
        h.stderr_lines()
            .iter()
            .filter(|l| l.contains("帳密解析未完成"))
            .count(),
        1
    );
    h.advance(Duration::from_secs(60));
    assert_eq!(h.dev.write_calls(), 0, "no automatic reprobe");
}

#[test]
fn banner_split_across_chunks_arms_quiet_and_blocks_agent_with_retry_after() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    h.rx(b"\r\nU-Bo");
    h.rx(b"ot 2024.01 (Jan 01)\r\n");
    let r = h.rpc(SessionRequest::Submit(agent_submit("ls"))).unwrap();
    assert_eq!(r.get("error_code"), Some(&PyValue::from("AUTOBOOT_QUIET")));
    assert_eq!(r.get("retry_after_s"), Some(&PyValue::Float(180.0)));
    h.rx(b"root@t:/# "); // prompt 命中 → 只清 quiet、保留 pending
    let r2 = h.rpc(SessionRequest::Submit(agent_submit("ls"))).unwrap();
    assert_eq!(r2.get("retry_after_s"), Some(&PyValue::Float(5.0)));
    assert_eq!(h.public()["ready_reconfirm_pending"], PyValue::Bool(true));
}

#[test]
fn human_prefixed_rpc_source_is_gated_like_agent_df02() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    h.rx(b"Hit any key to stop autoboot: 3\r\n");
    let mut req = agent_submit("ls");
    req.source = "human:spoof".into();
    assert_eq!(
        h.rpc(SessionRequest::Submit(req))
            .unwrap()
            .get("error_code"),
        Some(&PyValue::from("AUTOBOOT_QUIET"))
    );
}

// ---------------------------------------------------------------------------
// Step 1 表列（依表列順序）
// ---------------------------------------------------------------------------

#[test]
fn rearm_does_not_extend_reconfirm_deadline() {
    let mut q = QuietState::default();
    q.arm(secs(0));
    q.arm(secs(100));
    assert_eq!(q.reconfirm_remaining_s(secs(100)), Some(200.0));
    assert_eq!(q.remaining_s(secs(100)), Some(180.0));
}

#[test]
fn bootloader_prompt_tail_prevents_clear() {
    let p = profile_of(BOOTLOADER_PROFILE);
    let mut q = QuietState::default();
    let now = secs(10);
    q.arm(now);
    // 尾行 `=> ` 命中 bootloader_prompts（不用 fallback）→ 不清。
    assert_eq!(q.on_rx_text("=> ", &p, now), QuietTransition::None);
    assert!(q.quiet_active(now), "bootloader 提示不得清除 quiet");
    // prompt_regex 命中 → 清。
    assert_eq!(
        q.on_rx_text("\r\nroot@t:/# ", &p, now),
        QuietTransition::Cleared
    );
    assert!(!q.quiet_active(now));
}

#[test]
fn invalid_bootloader_regex_skipped() {
    // parity-amendments-2026-10-26 #93（B-43 部分）：不合法 regex 照 `_matches_any_bootloader_prompt`
    // 靜默略過（Python 只 `continue`、不 log），其餘樣式照常比對。
    let p = profile_of(INVALID_BOOTLOADER_PROFILE);
    assert_eq!(
        matches_any_bootloader_prompt("U-Boot\r\n=> ", &p.template.bootloader_prompts),
        Some("(?m)^=> $".to_string())
    );
    assert_eq!(
        matches_any_bootloader_prompt("=> ", &["[invalid(".to_string()]),
        None,
        "全部不合法 → 視為未命中"
    );

    // QuietState：合法樣式仍擋下解除；prompt_regex 照常解除。
    let mut q = QuietState::default();
    let now = secs(10);
    q.arm(now);
    assert_eq!(q.on_rx_text("=> ", &p, now), QuietTransition::None);
    assert!(q.quiet_active(now));
    assert_eq!(
        q.on_rx_text("\r\nroot@t:/# ", &p, now),
        QuietTransition::Cleared
    );

    // SessionCore：RX 路徑不 panic、不產生告警效果，quiet 維持啟用。
    let mut h = CoreHarness::new(INVALID_BOOTLOADER_PROFILE);
    h.to_ready();
    h.rx(BANNER.as_bytes());
    let effects = h.rx(b"=> ");
    assert!(
        !effects.iter().any(|e| matches!(e, sw_core::session_core::Effect::Warn(_))),
        "Python 不記錄 invalid bootloader regex"
    );
    assert!(h.fence.with(|f| f.quiet_active));
    assert_eq!(
        pget(&h.public(), "boot_quiet_remaining_s"),
        PyValue::Float(180.0)
    );
}

#[test]
fn reconfirm_exhausts_after_300s_ready_unconfirmed() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    h.rx(BANNER.as_bytes()); // 武裝 quiet 與 Ready Reconfirm
    h.advance(Duration::from_secs(300)); // 未經 nonce probe 再確認
    let r = h.rpc(SessionRequest::Submit(agent_submit("ls"))).unwrap();
    assert_eq!(r.get("ok"), Some(&PyValue::Bool(false)));
    assert_eq!(
        r.get("error_code"),
        Some(&PyValue::from("READY_UNCONFIRMED"))
    );
    assert_eq!(
        r.get("hint"),
        Some(&PyValue::from(
            "READY 已逾 300s／5 次未能經 nonce probe 再確認（可能卡在 bootloader）；請 session self-test 取分類後 interactive-open 處理"
        ))
    );
    assert_eq!(
        r.get("recommended_action"),
        Some(&PyValue::from("self_test"))
    );
    assert!(r.get("session").is_some(), "回應應含 session");
    assert!(
        r.get("retry_after_s").is_none(),
        "耗盡後不得再回 retry_after_s"
    );
}

#[test]
fn reconfirm_exhausts_after_5_attempts() {
    let mut q = QuietState::default();
    q.arm(secs(0));
    q.attempts = 5; // self-test probe 失敗 5 次
    let _ = q.expire(secs(1), false);
    assert!(q.failed, "attempts=5 必須標 failed");
}

#[test]
fn execute_time_gate_has_retry_after_sc03() {
    // 命令已排隊（hold 命令佔住 worker）後 banner 到達：出列時應被 quiet 閘擋下。
    let h = ActorRig::new(SHELL_PROFILE).ready();
    let hold = h.submit_with("sleep 60", |r| r.timeout_s = 120.0);
    h.wait_running(&hold);
    let queued = h.submit_ok("ls");
    h.inject_rx(BANNER.as_bytes());
    h.advance(Duration::from_secs(120)); // hold 逾時，佇列中的 ls 出列
    let done = h.wait_done(&queued);
    assert_eq!(
        done.get("error_code"),
        Some(&PyValue::from("AUTOBOOT_QUIET"))
    );
    let tx = String::from_utf8_lossy(&h.dev.take_tx()).into_owned();
    assert!(
        !tx.lines().any(|l| l.trim() == "ls"),
        "OpRunner 不得為被擋下的命令寫出 TX：{tx:?}"
    );
    // command.get 不含 retry（REC）；直接 execute 回應含 retry_after_s（SC-03）。
    assert!(h.command_get(&queued).get("retry_after_s").is_none());
    let direct = h.request(SessionRequest::Submit(agent_submit("ls")));
    assert_eq!(pget(&direct, "error_code"), PyValue::from("AUTOBOOT_QUIET"));
    assert!(matches!(pget(&direct, "retry_after_s"), PyValue::Float(_)));
}

#[test]
fn quiet_blocks_login_probe_zero_tx() {
    // Python `attach_session`：READY 不 probe（`_attach_state_result` → ok:true），ATTACHED 才經
    // `_probe_existing_bridge` 的 quiet 單點 gate（#130 Finding 2）合成 PROMPT_UNAVAILABLE。兩態皆零 TX。
    fn wait_quiet(h: &ActorRig) {
        wait_until("banner 已 arm quiet", || {
            pget(&h.get_state(), "boot_quiet_remaining_s") != PyValue::Null
        });
    }

    // READY：不 probe、不以合成碼覆寫 READY 的 last_error（#162）。
    let h = ActorRig::new(SHELL_PROFILE).ready();
    h.inject_rx(BANNER.as_bytes());
    wait_quiet(&h);
    let before = h.dev.write_calls();
    let resp = h.request(SessionRequest::SessionAttach);
    assert_eq!(pget(&resp, "ok"), PyValue::Bool(true));
    assert_eq!(pget(&h.get_state(), "last_error"), PyValue::Null);
    assert_eq!(h.dev.write_calls(), before, "quiet 中不得送出任何 TX");

    // ATTACHED（nonce 未回 → READY_NONCE_TIMEOUT）：quiet 中觸發 attach → LoginPlan 不啟動、PROMPT_UNAVAILABLE。
    let h = ActorRig::new(SHELL_PROFILE);
    h.dev.on_tx_reply(b"\n", b"\r\nroot@t:/# ");
    let h = h.attach();
    h.wait_state("ATTACHED");
    h.inject_rx(BANNER.as_bytes());
    wait_quiet(&h);
    let before = h.dev.write_calls();
    let resp = h.request(SessionRequest::SessionAttach); // quiet 中觸發 attach
    assert_eq!(
        pget(&resp, "error_code"),
        PyValue::from("SESSION_NOT_READY")
    );
    assert_eq!(
        pget(&resp, "last_error"),
        PyValue::from("PROMPT_UNAVAILABLE")
    );
    assert_eq!(
        h.dev.write_calls(),
        before,
        "quiet 中 login probe 不得送出任何 TX"
    );
}

/// reboot 命令 TX 的結算結果（TxWriter 每個 job 都送出 `UartTxCompleted`，與 `TxReport` 同一結算點）。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum RebootTx {
    /// `Completed(n≥1)`。
    Completed,
    /// `Unknown`（結果不明）。
    Unknown,
    /// `NotSubmitted`（Frozen／StaleEpoch／Cancelled／Quiet 拒絕：confirmed 0、非 unknown）。
    NotSubmitted,
}

const REBOOT_COMMANDS: [&str; 5] = [
    "reboot",
    "echo b > /proc/sysrq-trigger",
    "systemctl reboot",
    "shutdown -r now",
    "/sbin/reboot",
];

/// sans-IO（parity-amendments-2026-10-26 #55）：reboot 命令出列（`StartOp`）後，先斷言 TX 結算前未 arm，
/// 再以 `UartTxCompleted` 結算該命令的 TX。命令執行路徑（OpRunner）屬 Task 14，故於 SessionCore 層驗證。
fn reboot_case(cmd: &str, tx: RebootTx) -> CoreHarness {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    let started = h.start_foreground_command(cmd);
    assert!(
        !h.fence.with(|f| f.quiet_active),
        "{cmd}：出列後、TxReport 前不得 arm quiet"
    );
    assert_eq!(
        pget(&h.public(), "boot_quiet_remaining_s"),
        PyValue::Null,
        "{cmd}"
    );
    let (confirmed_bytes, unknown) = match tx {
        RebootTx::Completed => (cmd.len() + 1, false),
        RebootTx::Unknown => (0, true),
        RebootTx::NotSubmitted => (0, false),
    };
    let epoch = h.epoch();
    h.feed(ControlEvent::UartTxCompleted {
        epoch,
        op_id: started.op_id,
        confirmed_bytes,
        unknown,
    });
    h
}

fn assert_reboot_armed(h: &mut CoreHarness, cmd: &str, tx: RebootTx) {
    assert!(
        h.fence.with(|f| f.quiet_active),
        "{cmd}／{tx:?}：TX 結算後 fence quiet_active 應為 true"
    );
    assert_eq!(
        pget(&h.public(), "boot_quiet_remaining_s"),
        PyValue::Float(180.0),
        "{cmd}／{tx:?}"
    );
    assert_eq!(
        pget(&h.public(), "ready_reconfirm_pending"),
        PyValue::Bool(true),
        "{cmd}／{tx:?}"
    );
    let r = h.rpc(SessionRequest::Submit(agent_submit("ls"))).unwrap();
    assert_eq!(
        r.get("error_code"),
        Some(&PyValue::from("AUTOBOOT_QUIET")),
        "{cmd}／{tx:?}"
    );
    assert_eq!(
        r.get("retry_after_s"),
        Some(&PyValue::Float(180.0)),
        "{cmd}／{tx:?}"
    );
}

#[test]
fn reboot_command_arms_quiet_at_dequeue() {
    // 舊名保留（L1 鎖）；語意依 parity-amendments-2026-10-26 #55 修正：出列時不 arm，TX 已提交後才 arm。
    for cmd in REBOOT_COMMANDS {
        let mut h = reboot_case(cmd, RebootTx::Completed);
        assert_reboot_armed(&mut h, cmd, RebootTx::Completed);
    }
}

#[test]
fn reboot_command_arms_quiet_after_tx_submitted() {
    for cmd in REBOOT_COMMANDS {
        for tx in [RebootTx::Completed, RebootTx::Unknown] {
            let mut h = reboot_case(cmd, tx);
            assert_reboot_armed(&mut h, cmd, tx);
        }
    }
    // 非 reboot 命令的 TX 結算不 arm。
    for cmd in ["rebootx", "echo b", "shutdown -h now"] {
        let h = reboot_case(cmd, RebootTx::Completed);
        assert!(!h.fence.with(|f| f.quiet_active), "{cmd}");
        assert_eq!(
            pget(&h.public(), "boot_quiet_remaining_s"),
            PyValue::Null,
            "{cmd}"
        );
    }
}

#[test]
fn reboot_not_submitted_does_not_arm_quiet() {
    for cmd in REBOOT_COMMANDS {
        let mut h = reboot_case(cmd, RebootTx::NotSubmitted);
        assert!(!h.fence.with(|f| f.quiet_active), "{cmd}");
        let s = h.public();
        assert_eq!(pget(&s, "boot_quiet_remaining_s"), PyValue::Null, "{cmd}");
        assert_eq!(pget(&s, "ready_reconfirm_pending"), PyValue::Bool(false), "{cmd}");
        assert_eq!(pget(&s, "state"), PyValue::from("READY"), "{cmd}：不得進 RECOVERING");
        let r = h.rpc(SessionRequest::Submit(agent_submit("ls"))).unwrap();
        assert_ne!(
            r.get("error_code"),
            Some(&PyValue::from("AUTOBOOT_QUIET")),
            "{cmd}"
        );
        assert!(r.get("retry_after_s").is_none(), "{cmd}");
    }
}

#[test]
fn is_reboot_command_negative() {
    assert!(!is_reboot_command("rebootx"));
    assert!(!is_reboot_command("echo b"));
    assert!(!is_reboot_command("shutdown -h now"));
    assert!(is_reboot_command("reboot"));
    assert!(is_reboot_command("systemctl reboot"));
}

#[test]
fn probe_ready_sends_lf_and_matches_whole_window() {
    let p = profile_of(SHELL_PROFILE);
    let rig = LoginRig::new();
    // 過期的 prompt 位於 clear 之前，不得讓 probe 誤判成功。
    let now = rig.clock.mono_ns();
    rig.rx.append(b"stale\r\nroot@t:/# ", now, 1, 0);
    let out = rig.run(Dev::Shell, |io| probe_ready(io, &p));
    assert_eq!(out, LoginOutcome::Ready);
    let sent = rig.sent();
    assert_eq!(sent[0].bytes, b"\n".to_vec());
    assert_eq!(sent[0].class, TxClass::LoginProbe);
    assert_eq!(sent[0].source, "system");
    assert!(sent.iter().any(|s| {
        s.class == TxClass::LoginProbe
            && String::from_utf8_lossy(&s.bytes).contains("echo __READY__ab12cd34")
    }));
}

#[test]
fn ensure_ready_login_sequence() {
    let p = profile_of(LOGIN_PROFILE);
    let rig = LoginRig::new();
    let a = auth(Some("root"), Some("s3cret"));
    let env = FakeEnv::default();
    let out = rig.run(Dev::Login, |io| ensure_ready(io, &p, &a, &env));
    assert_eq!(out, LoginOutcome::Ready);
    let sent = rig.sent();
    // WAL 可見的 TX 只有 `\n`、user、nonce 命令；secret 以 log=false 送出。
    let logged: Vec<String> = sent
        .iter()
        .filter(|s| s.log)
        .map(|s| String::from_utf8_lossy(&s.bytes).into_owned())
        .collect();
    assert_eq!(
        logged.len(),
        3,
        "WAL TX 應只含 LF、user、nonce 命令：{logged:?}"
    );
    assert_eq!(logged[0], "\n");
    assert_eq!(logged[1].trim(), "root");
    assert!(logged[2].contains("__READY__ab12cd34"));
    let secret = sent
        .iter()
        .find(|s| String::from_utf8_lossy(&s.bytes).contains("s3cret"))
        .expect("應送出 secret");
    assert!(!secret.log, "secret 不得寫入 WAL");
    assert!(!logged.iter().any(|l| l.contains("s3cret")));
}

#[test]
fn prompt_timeout_codes_by_platform() {
    assert_eq!(prompt_timeout_error("shell"), "SHELL_PROMPT_TIMEOUT");
    assert_eq!(prompt_timeout_error("prpl"), "PRPL_PROMPT_TIMEOUT");
    assert_eq!(prompt_timeout_error("passthrough"), "PRPL_PROMPT_TIMEOUT");
    assert_eq!(prompt_timeout_error("bcm"), "BCM_PROMPT_TIMEOUT");
}

#[test]
fn rx_flood_reclassifies() {
    let p = profile_of(SHELL_PROFILE);
    let rig = LoginRig::new();
    rig.flood(20_000);
    let now = rig.clock.mono_ns();
    assert!(
        rig.rx.rx_stats(now).0 >= 20_000,
        "前置條件：10 s 內 RX ≥ 20000 B"
    );
    // 靜默裝置 → PROMPT_UNAVAILABLE，且 RX 洪水 → 改判 RX_FLOOD。
    let out = rig.run(Dev::Silent, |io| probe_ready(io, &p));
    assert_eq!(out, LoginOutcome::NotReady("RX_FLOOD".to_string()));
}

#[test]
fn flood_masking_exact_set() {
    // 五碼（login_fsm.py `_FLOOD_MASKABLE_ERRORS` 逐字）；POST_LOGIN_CMD_TIMEOUT 於 2.1 不會產生，只驗集合。
    assert_eq!(
        FLOOD_MASKABLE_ERRORS,
        [
            "PROMPT_UNAVAILABLE",
            "READY_NONCE_TIMEOUT",
            "READY_PROMPT_TIMEOUT",
            "POST_LOGIN_CMD_TIMEOUT",
            "LOGIN_PROMPT_TIMEOUT",
        ]
    );
    let shell_p = profile_of(SHELL_PROFILE);
    let anchored_p = profile_of(ANCHORED_PROMPT_PROFILE);
    let login_p = profile_of(LOGIN_PROFILE);
    let env_login = profile_of(LOGIN_ENV_PROFILE);
    let env_missing = FakeEnv::default();
    let a = auth(Some("root"), Some("s3cret"));

    // 可遮蔽碼：在 RX 洪水（10 s 內 ≥ 20000 B）下一律改判 RX_FLOOD。
    let flooded = |build: &dyn Fn(&LoginRig) -> LoginOutcome| {
        let rig = LoginRig::new();
        rig.flood(20_000);
        build(&rig)
    };
    assert_eq!(
        flooded(&|rig| rig.run(Dev::Silent, |io| probe_ready(io, &shell_p))),
        LoginOutcome::NotReady("RX_FLOOD".to_string()),
        "PROMPT_UNAVAILABLE"
    );
    assert_eq!(
        flooded(&|rig| rig.run(Dev::NonceSilent, |io| probe_ready(io, &shell_p))),
        LoginOutcome::NotReady("RX_FLOOD".to_string()),
        "READY_NONCE_TIMEOUT"
    );
    assert_eq!(
        flooded(&|rig| rig.run(Dev::PromptNoNonce, |io| probe_ready(io, &anchored_p))),
        LoginOutcome::NotReady("RX_FLOOD".to_string()),
        "READY_PROMPT_TIMEOUT"
    );
    assert_eq!(
        outcome_code(&flooded(&|rig| {
            rig.run(Dev::Silent, |io| {
                ensure_ready(io, &env_login, &a, &env_missing)
            })
        })),
        "RX_FLOOD",
        "LOGIN_PROMPT_TIMEOUT"
    );

    // 不可遮蔽碼：同樣的洪水條件下仍維持原碼。
    assert_eq!(
        outcome_code(&flooded(
            &|rig| rig.run(Dev::LoginPrompt, |io| probe_ready(io, &login_p))
        )),
        "LOGIN_REQUIRED"
    );
    // X3：USER_ENV_MISSING 對外不可達（`_maybe_login` 的 err 被後續等待覆寫）；user 缺＋pass_env 宣告時
    // Python 等到 login prompt 後回 LOGIN_USER_REQUIRED（不可遮蔽碼）。
    assert_eq!(
        outcome_code(&flooded(&|rig| {
            rig.run(Dev::LoginPrompt, |io| {
                ensure_ready(io, &env_login, &auth(None, None), &env_missing)
            })
        })),
        "LOGIN_USER_REQUIRED"
    );
    for code in ["LOGIN_REQUIRED", "USER_ENV_MISSING", "SHELL_PROMPT_TIMEOUT"] {
        assert!(
            !FLOOD_MASKABLE_ERRORS.contains(&code),
            "{code} 不得列入可遮蔽集合"
        );
    }
    assert_eq!(prompt_timeout_error("shell"), "SHELL_PROMPT_TIMEOUT");
}

#[test]
fn login_failure_code_table() {
    // (a) SessionAttach 可見的列：ActorRig 實機路徑。
    // Python `attach_session` 在 ATTACHED 分支會經 `_probe_existing_bridge` 重新 probe，故 FakeDevice 的一次性
    // 回應（`on_tx_reply`）於 SessionAttach 前重新佈署（`rearm`）；持續性比對（`on_tx_match`）只佈署一次（`setup`）。
    // LOGIN_PROMPT_TIMEOUT 列：Python 的 attach 路徑 `ensure_ready` 失敗會關 bridge 回 DETACHED，故改為先以
    // 未解析帳密停 ATTACHED（CREDENTIALS_UNRESOLVED），補 env 後由 SessionAttach 的既有 bridge 再探觸發
    // `ensure_ready` → LOGIN_PROMPT_TIMEOUT（停 ATTACHED）。
    struct Row {
        code: &'static str,
        yaml: &'static str,
        setup: fn(&ActorRig),
        rearm: fn(&ActorRig),
        env_after_attach: bool,
    }
    fn nothing(_: &ActorRig) {}
    let rows = [
        Row {
            code: "LOGIN_REQUIRED",
            yaml: LOGIN_PROFILE,
            setup: nothing,
            rearm: |rig: &ActorRig| rig.dev.on_tx_reply(b"\n", b"\r\nlogin: "),
            env_after_attach: false,
        },
        Row {
            code: "READY_NONCE_TIMEOUT",
            yaml: SHELL_PROFILE,
            setup: nothing,
            rearm: |rig: &ActorRig| rig.dev.on_tx_reply(b"\n", b"\r\nroot@t:/# "),
            env_after_attach: false,
        },
        Row {
            code: "READY_PROMPT_TIMEOUT",
            yaml: ANCHORED_PROMPT_PROFILE,
            setup: |rig: &ActorRig| {
                rig.dev.on_tx_match(
                    r"__READY__[0-9a-f]+",
                    Box::new(|line: &[u8]| {
                        let text = String::from_utf8_lossy(line).into_owned();
                        let token = text
                            .split("__READY__")
                            .nth(1)
                            .unwrap_or("")
                            .trim()
                            .to_string();
                        format!("__READY__{token}\r\n").into_bytes()
                    }),
                );
            },
            rearm: |rig: &ActorRig| rig.dev.on_tx_reply(b"\n", b"\r\nroot@t:/# "),
            env_after_attach: false,
        },
        Row {
            code: "LOGIN_PROMPT_TIMEOUT",
            yaml: LOGIN_ENV_PROFILE,
            setup: nothing,
            rearm: nothing,
            env_after_attach: true,
        },
    ];
    for row in rows {
        let code = row.code;
        let rig = ActorRig::new(row.yaml);
        (row.setup)(&rig);
        (row.rearm)(&rig);
        let rig = rig.attach();
        rig.wait_state("ATTACHED");
        if row.env_after_attach {
            rig.set_env("SW_T_U", "root");
            rig.set_env("SW_T_P", "s3cret");
        }
        let _ = rig.dev.take_tx();
        (row.rearm)(&rig);
        let resp = rig.request(SessionRequest::SessionAttach);
        assert_eq!(
            pget(&resp, "error_code"),
            PyValue::from("SESSION_NOT_READY"),
            "{code}"
        );
        assert_eq!(pget(&resp, "last_error"), PyValue::from(code), "{code}");
        assert_eq!(
            pget(&resp, "last_error_detail"),
            pget(&rig.get_state(), "last_error_detail"),
            "{code}：last_error_detail 應與 session 內同名欄位一致"
        );
        assert_eq!(pget(&rig.get_state(), "state"), PyValue::from("ATTACHED"), "{code}");
    }

    // (b) 只能於 env／帳密層判定的列：ensure_ready 的失敗碼（函式層；斷言值為 Python 基線實測）。
    // - X3：USER_ENV_MISSING 對外不可達——user_env 缺＋pass_env 宣告時，等到 login prompt 後回 LOGIN_USER_REQUIRED。
    // - user 缺且未宣告 pass_env：`needs_login` 為假，Python 回 `<PLATFORM>_PROMPT_TIMEOUT`（shell → SHELL_PROMPT_TIMEOUT）。
    // - PASS_ENV_REQUIRED／PASS_ENV_MISSING 需裝置先出現 password prompt（`Dev::Login`）。
    let env_missing = FakeEnv::default();
    let env_pass_only = FakeEnv::default();
    env_pass_only
        .vars
        .lock()
        .unwrap()
        .insert("SW_T_P".into(), "pw".into());
    let fn_rows: [(&str, &str, SessionAuth, &dyn SysEnv, Dev); 4] = [
        (
            "LOGIN_USER_REQUIRED",
            LOGIN_ENV_PROFILE,
            auth(None, None),
            &env_pass_only,
            Dev::LoginPrompt,
        ),
        (
            "PASS_ENV_REQUIRED",
            LOGIN_PROFILE,
            auth(Some("root"), None),
            &env_missing,
            Dev::Login,
        ),
        (
            "PASS_ENV_MISSING",
            LOGIN_ENV_PROFILE,
            auth(Some("root"), None),
            &env_missing,
            Dev::Login,
        ),
        (
            "SHELL_PROMPT_TIMEOUT",
            LOGIN_PROFILE,
            auth(None, Some("pw")),
            &env_missing,
            Dev::LoginPrompt,
        ),
    ];
    for (code, yaml, a, env, dev) in fn_rows {
        let p = profile_of(yaml);
        let rig = LoginRig::new();
        let out = rig.run(dev, |io| ensure_ready(io, &p, &a, env));
        assert_eq!(outcome_code(&out), code);
        assert_ne!(outcome_code(&out), "USER_ENV_MISSING", "X3：不可達");
    }

    // (c) probe／ensure 的裝置端失敗碼（函式層，與 (a) 相同的分類規則）。
    let login_p = profile_of(LOGIN_PROFILE);
    let shell_p = profile_of(SHELL_PROFILE);
    let anchored_p = profile_of(ANCHORED_PROMPT_PROFILE);
    let rig = LoginRig::new();
    let out = rig.run(Dev::LoginPrompt, |io| probe_ready(io, &login_p));
    assert_eq!(outcome_code(&out), "LOGIN_REQUIRED");
    let rig = LoginRig::new();
    let out = rig.run(Dev::NonceSilent, |io| probe_ready(io, &shell_p));
    assert_eq!(outcome_code(&out), "READY_NONCE_TIMEOUT");
    let rig = LoginRig::new();
    let out = rig.run(Dev::PromptNoNonce, |io| probe_ready(io, &anchored_p));
    assert_eq!(outcome_code(&out), "READY_PROMPT_TIMEOUT");
    // 整窗比對（SES-16.6-05 KEEP）：非錨定 prompt 會被 probe 先前收到的 prompt 滿足 → Ready（Python 實測）。
    let rig = LoginRig::new();
    let out = rig.run(Dev::PromptNoNonce, |io| probe_ready(io, &shell_p));
    assert_eq!(out, LoginOutcome::Ready);
    let env_login = profile_of(LOGIN_ENV_PROFILE);
    let rig = LoginRig::new();
    let a = auth(Some("root"), Some("s3cret"));
    let out = rig.run(Dev::Silent, |io| {
        ensure_ready(io, &env_login, &a, &env_pass_only)
    });
    assert_eq!(outcome_code(&out), "LOGIN_PROMPT_TIMEOUT");
}

#[test]
fn login_error_detail_carries_rx_tail() {
    // 命中 LOGIN_FSM_DETAIL_ERRORS（READY_NONCE_TIMEOUT）：detail 為失敗當下 RX tail（ANSI 去除）。
    let rig = ActorRig::new(SHELL_PROFILE);
    rig.dev
        .on_tx_reply(b"\n", b"\x1b[31mBOOT-NOISE\x1b[0m\r\nroot@t:/# ");
    let rig = rig.attach();
    rig.wait_state("ATTACHED");
    let s = rig.get_state();
    let code = pget(&s, "last_error");
    let code = code.as_str().unwrap_or("").to_string();
    assert!(
        LOGIN_FSM_DETAIL_ERRORS.contains(&code.as_str()),
        "前置：{code}"
    );
    let detail = pget(&s, "last_error_detail");
    let detail = detail.as_str().expect("detail 應為字串").to_string();
    assert!(detail.contains("BOOT-NOISE"));
    assert!(!detail.contains('\u{1b}'), "detail 不得含 ANSI 控制碼");

    // 非表列碼（PROMPT_UNAVAILABLE）：last_error_detail 為 null。
    let silent = ActorRig::new(SHELL_PROFILE).attach();
    silent.wait_state("ATTACHED");
    let s2 = silent.get_state();
    assert_eq!(pget(&s2, "last_error"), PyValue::from("PROMPT_UNAVAILABLE"));
    assert_eq!(pget(&s2, "last_error_detail"), PyValue::Null);
}

#[test]
fn disk_full_blocks_login_probe_but_not_agent_cmd() {
    // Review Focus 3：WAL 寫入持續失敗 → 登入 probe 被 fence（WalUnhealthy）拒絕，agent 命令仍照常寫出。
    // Python `attach_session` 只在 ATTACHED 分支 probe（READY 回 `_attach_state_result`、不 probe），
    // 故 probe 案例以 ATTACHED（nonce 未回）session 驗證；agent 命令案例以 READY session 驗證。
    fn make_unhealthy(h: &ActorRig) {
        h.fail_wal_writes(PalError::io(28, "No space left on device"));
        h.inject_rx(b"noise\r\n"); // RX WAL 寫入失敗 → WAL unhealthy
        wait_until("WAL 寫入失敗", || h.wal_write_failures() > 0);
    }
    let a = ActorRig::new(SHELL_PROFILE);
    a.dev.on_tx_reply(b"\n", b"\r\nroot@t:/# ");
    let a = a.attach();
    a.wait_state("ATTACHED");
    make_unhealthy(&a);
    let before = a.dev.write_calls();
    let resp = a.request(SessionRequest::SessionAttach);
    assert_eq!(
        a.dev.write_calls(),
        before,
        "unhealthy WAL 下 probe 不得寫出"
    );
    assert_eq!(
        pget(&resp, "last_error"),
        PyValue::from("PROMPT_UNAVAILABLE")
    );

    let h = ActorRig::new(SHELL_PROFILE).ready();
    make_unhealthy(&h);
    h.dev.on_tx_reply(b"echo x", b"x\r\nroot@t:/# ");
    let id = h.submit_ok("echo x");
    h.wait_done(&id);
    let tx = String::from_utf8_lossy(&h.dev.take_tx()).into_owned();
    assert!(tx.contains("echo x"), "agent 命令仍應寫出：{tx:?}");
}

#[test]
fn passthrough_no_ready_probe_no_tx() {
    let p = profile_of(OTHERS_PASSTHROUGH_PROFILE);
    // 函式層：passthrough 不進登入 FSM。
    let (plan, last_error) = choose_login_plan(
        &p,
        false,
        (SessionAuth::default(), resolution("not_configured")),
        false,
    );
    assert!(matches!(plan, LoginPlan::None));
    assert_eq!(last_error, None);
    // 實機路徑：attach → ATTACHED、無 last_error、零 TX。
    let h = ActorRig::new(OTHERS_PASSTHROUGH_PROFILE).attach();
    h.wait_state("ATTACHED");
    let s = h.get_state();
    assert_eq!(pget(&s, "last_error"), PyValue::Null);
    assert_eq!(h.dev.write_calls(), 0);
}

#[test]
fn uboot_template_no_probe_quiet_ready() {
    let h = ActorRig::new(UBOOT_PASSTHROUGH_PROFILE).attach();
    h.wait_state("ATTACHED");
    assert_eq!(
        h.dev.write_calls(),
        0,
        "passthrough 不送 LF、不送 nonce 探針"
    );
    h.advance(Duration::from_secs(30)); // 無 RX 推進 30 s
    h.wait_state("READY");
    assert_eq!(h.dev.write_calls(), 0, "升 READY 全程零 TX");
}

#[test]
fn passthrough_banner_does_not_arm_quiet() {
    let h = ActorRig::new(PASSTHROUGH_PROFILE).attach();
    h.wait_state("ATTACHED");
    h.inject_rx(BANNER.as_bytes());
    let s = h.get_state();
    assert_eq!(
        pget(&s, "boot_quiet_remaining_s"),
        PyValue::Null,
        "passthrough 不得 arm quiet"
    );
    assert_eq!(pget(&s, "ready_reconfirm_pending"), PyValue::Bool(false));
    // 同一 RX 只重設 passthrough 安靜視窗（30 s 自最後一次 RX 起算）。
    h.advance(Duration::from_secs(20));
    h.inject_rx(b"x");
    h.advance(Duration::from_secs(20));
    assert_eq!(pget(&h.get_state(), "state"), PyValue::from("ATTACHED"));
    h.advance(Duration::from_secs(11));
    h.wait_state("READY");
}

#[test]
fn unsupported_profile_no_auto_tx() {
    let p = profile_of(BRCM_POST_LOGIN_PROFILE);
    assert_eq!(slice21_unsupported_feature(&p), Some("post_login_cmd"));
    let h = ActorRig::new(BRCM_POST_LOGIN_PROFILE).attach();
    h.wait_state("ATTACHED");
    let s = h.get_state();
    assert_eq!(pget(&s, "last_error"), PyValue::Null);
    assert_eq!(h.dev.write_calls(), 0);
    let msg = "slice-2.1 不支援的 profile 能力（brcm-template: post_login_cmd）；不自動登入";
    assert_eq!(
        h.stderr_lines()
            .iter()
            .filter(|l| l.as_str() == msg)
            .count(),
        1
    );
    let r = h.request(SessionRequest::Submit(agent_submit("ls")));
    assert_eq!(pget(&r, "error_code"), PyValue::from("SESSION_NOT_READY"));
}

#[test]
fn parse_env_file_rules() {
    let m = parse_env_file("export A='x'\n# c\nB = \"y\"\n=z\nC\n");
    assert_eq!(m.len(), 2, "{m:?}");
    assert_eq!(m["A"], "x");
    assert_eq!(m["B"], "y");
}

#[test]
fn auth_reasons() {
    let env = FakeEnv::default();
    let shell = profile_of(SHELL_PROFILE);
    let cred = profile_of(CRED_PROFILE);
    let login_env = profile_of(LOGIN_ENV_PROFILE);

    // 未宣告 → not_configured。
    let none_read = |_: &str| -> Option<std::io::Result<String>> { None };
    let (_, r) = resolve_session_auth(&shell, &env, &none_read);
    assert_eq!(r.reason, "not_configured");
    assert_eq!(r.env_file_path, None);

    // env_file 缺 → env_file_missing。
    let (_, r) = resolve_session_auth(&cred, &env, &none_read);
    assert_eq!(r.reason, "env_file_missing");

    // 不可讀 → env_file_unreadable。
    let unreadable = |_: &str| -> Option<std::io::Result<String>> {
        Some(Err(std::io::Error::new(
            std::io::ErrorKind::PermissionDenied,
            "denied",
        )))
    };
    let (_, r) = resolve_session_auth(&cred, &env, &unreadable);
    assert_eq!(r.reason, "env_file_unreadable");

    // 檔在但缺 key → key_absent。
    let other_only =
        |_: &str| -> Option<std::io::Result<String>> { Some(Ok("OTHER=1\n".to_string())) };
    let (_, r) = resolve_session_auth(&cred, &env, &other_only);
    assert_eq!(r.reason, "key_absent");

    // env 有值 → ok。
    let env_ok = FakeEnv::default();
    env_ok
        .vars
        .lock()
        .unwrap()
        .insert("SW_T_U".into(), "root".into());
    env_ok
        .vars
        .lock()
        .unwrap()
        .insert("SW_T_P".into(), "pw".into());
    let (a, r) = resolve_session_auth(&login_env, &env_ok, &none_read);
    assert_eq!(r.reason, "ok");
    assert_eq!(a.username.as_deref(), Some("root"));
    assert!(!credentials_declared_but_unresolved(&login_env, &r));
}

#[test]
fn attach_and_recover_reset_credentials_warned() {
    let h = ActorRig::new(CRED_PROFILE).attach();
    h.wait_state("ATTACHED");
    let warned = |h: &ActorRig| {
        h.stderr_lines()
            .iter()
            .filter(|l| l.contains("帳密解析未完成"))
            .count()
    };
    assert_eq!(warned(&h), 1);
    // 仍未補齊：再次 attach 失敗時再告警一次。
    let _ = h.request(SessionRequest::SessionAttach);
    assert_eq!(warned(&h), 2);
    // 補上 env 與 env 檔後 attach → 重新解析並登入成功。
    h.set_env("SW_T_U", "root");
    h.set_env("SW_T_P", "pw");
    h.set_env_file("/nonexistent/s21.env", "SW_T_U=root\nSW_T_P=pw\n");
    h.dev.on_tx_reply(b"\n", b"\r\nlogin: ");
    h.dev.on_tx_reply(b"root", b"\r\nPassword: ");
    h.dev.on_tx_reply(b"pw", b"\r\nroot@t:/# ");
    answer_nonce(&h.dev);
    let _ = h.request(SessionRequest::SessionAttach);
    h.wait_state("READY");
}

#[test]
fn valid_completion_regex_rules() {
    assert!(valid_completion_regex(None));
    assert!(!valid_completion_regex(Some(&PyValue::from(""))));
    let long = "a".repeat(1025);
    assert!(!valid_completion_regex(Some(&PyValue::from(long.as_str()))));
    assert!(!valid_completion_regex(Some(&PyValue::from("(?"))));
    assert!(!valid_completion_regex(Some(&PyValue::from("x*"))));
    assert!(valid_completion_regex(Some(&PyValue::from("END$"))));
}

// ---------------------------------------------------------------------------
// 補充：常數與介面形狀（Produces 凍結）
// ---------------------------------------------------------------------------

#[test]
fn produced_constants_are_frozen() {
    assert_eq!(
        BOOT_BANNER_PATTERNS,
        ["U-Boot", "Hit any key to stop autoboot", "CFE version "]
    );
    assert_eq!(
        FLOOD_MASKABLE_ERRORS,
        [
            "PROMPT_UNAVAILABLE",
            "READY_NONCE_TIMEOUT",
            "READY_PROMPT_TIMEOUT",
            "POST_LOGIN_CMD_TIMEOUT",
            "LOGIN_PROMPT_TIMEOUT",
        ]
    );
    assert_eq!(LOGIN_FSM_DETAIL_ERRORS.len(), 12);
    assert!(detect_boot_banner("xx U-Boot 2024"));
}
