//! P7 Task 5（08-slice23-login-events.md）：readiness reprobe 與 bootloader 證據（`reprobe.rs`）。
//!
//! 斷言值以 Python 基線 golden `contracts/golden/p7/{reprobe_decisions,reprobe_valid}.ndjson`（CPython 3.12、
//! `scripts/rust/p7_sm_goldens.py` 以真 `SessionManager` 驅動）為準。偏離計畫原文：`support::*_from_json` 放本檔；
//! golden `setup` 的 `lease`／`bridge.tail` 轉為 `human_active`／`rx_tail_512`（Python `_human_active_locked`：
//! human lease 且 `now - last_human_input_at <= HUMAN_ACTIVE_WINDOW_S`（60 s）；GoldenBridge 恆有外部 peer）。
mod common;

use common::p7::{golden_rows, session_profile_for, Row};
use sw_core::quiet_gate::QuietState;
use sw_core::reprobe::*;
use sw_core::Mono;
use sw_proto::config_types::SessionProfile;
use sw_proto::pyjson::{PyDict, PyValue};

fn t(s: f64) -> Mono {
    (s * 1e9).round() as Mono
}

fn secs(m: Mono) -> f64 {
    m as f64 / 1e9
}

fn num(v: &PyValue) -> f64 {
    match v {
        PyValue::Float(f) => *f,
        PyValue::Int(_) => v.as_i64().expect("int") as f64,
        other => panic!("不是數值：{other:?}"),
    }
}

fn opt_num(v: Option<&PyValue>) -> Option<f64> {
    match v {
        None | Some(PyValue::Null) => None,
        Some(v) => Some(num(v)),
    }
}

fn dict(v: &PyValue) -> &PyDict {
    match v {
        PyValue::Dict(d) => d,
        other => panic!("不是 object：{other:?}"),
    }
}

/// golden `setup`（`p7_sm_goldens.build` 的 spec）→ 純函式的輸入。
struct Owned {
    state: String,
    by_id: String,
    released: bool,
    device_present: bool,
    last_error: Option<String>,
    last_rx_mono: Option<Mono>,
    human_active: bool,
    has_bridge: bool,
    tail: String,
    profile: SessionProfile,
}

fn owned(setup: &PyDict, now: f64) -> Owned {
    let g = |k: &str| setup.get(k);
    let mut profile = session_profile_for(
        g("template")
            .and_then(PyValue::as_str)
            .unwrap_or("prpl-template"),
    );
    if let Some(PyValue::Dict(over)) = g("profile_over") {
        if let Some(PyValue::List(prompts)) = over.get("bootloader_prompts") {
            profile.template.bootloader_prompts = prompts
                .iter()
                .map(|p| p.as_str().expect("prompt").to_string())
                .collect();
        }
    }
    let bridge = g("bridge");
    let has_bridge = matches!(bridge, Some(PyValue::Dict(_)));
    let tail = match bridge {
        Some(PyValue::Dict(b)) => b
            .get("tail")
            .and_then(PyValue::as_str)
            .unwrap_or("")
            .to_string(),
        _ => String::new(),
    };
    let human_active = match g("lease") {
        Some(PyValue::Dict(l)) => {
            has_bridge
                && l.get("owner")
                    .and_then(PyValue::as_str)
                    .is_some_and(|o| o.starts_with("human:"))
                && opt_num(l.get("last_human_input_rel")).is_some_and(|rel| rel <= 60.0)
        }
        _ => false,
    };
    Owned {
        state: g("state")
            .and_then(PyValue::as_str)
            .unwrap_or("READY")
            .to_string(),
        by_id: profile.device_by_id.clone(),
        released: g("released_by_id")
            .and_then(PyValue::as_bool)
            .unwrap_or(false),
        device_present: g("device_present")
            .and_then(PyValue::as_bool)
            .unwrap_or(true),
        last_error: g("last_error")
            .and_then(PyValue::as_str)
            .map(str::to_string),
        last_rx_mono: opt_num(g("last_rx_rel")).map(|rel| t(now - rel)),
        human_active,
        has_bridge,
        tail,
        profile,
    }
}

impl Owned {
    fn input(&self) -> ReprobeInput<'_> {
        ReprobeInput {
            state: &self.state,
            by_id: &self.by_id,
            released: self.released,
            device_present: self.device_present,
            last_error: self.last_error.as_deref(),
            last_rx_mono: self.last_rx_mono,
            human_active: self.human_active,
            has_bridge: self.has_bridge,
            profile: &self.profile,
        }
    }
    fn valid(&self, same_bridge: bool) -> ValidInput<'_> {
        ValidInput {
            same_bridge,
            by_id: &self.by_id,
            released: self.released,
            device_present: self.device_present,
            state: &self.state,
            last_error: self.last_error.as_deref(),
            human_active: self.human_active,
            rx_tail_512: &self.tail,
            profile: &self.profile,
        }
    }
}

/// golden `before.quiet`（Python 秒；`0.0`＝未設定）→ `QuietState`。
fn quiet_of(q: &PyDict) -> QuietState {
    let g = |k: &str| q.get(k).unwrap_or_else(|| panic!("quiet 欄位 {k}"));
    let mono = |k: &str| {
        let s = num(g(k));
        if s > 0.0 {
            t(s)
        } else {
            0
        }
    };
    QuietState {
        boot_quiet_until: mono("boot_quiet_until"),
        boot_banner_tail: g("boot_banner_tail").as_str().unwrap().to_string(),
        pending: g("pending").as_bool().unwrap(),
        deadline: mono("deadline"),
        attempts: g("attempts").as_i64().unwrap() as u32,
        failed: g("failed").as_bool().unwrap(),
        boot_evidence_wait_until: 0,
    }
}

fn reprobe_before(row: &Row) -> ReprobeState {
    let b = dict(&row["before"]);
    ReprobeState {
        attempts: b.get("attempts").and_then(PyValue::as_i64).unwrap_or(0) as u32,
        next_reprobe_at: opt_num(b.get("next_reprobe_at")).map(t),
        exhausted: b
            .get("exhausted")
            .and_then(PyValue::as_bool)
            .unwrap_or(false),
        inflight: false,
    }
}

fn assert_rel(got: Option<f64>, want: Option<f64>, ctx: &str) {
    match (got, want) {
        (Some(a), Some(b)) => assert!((a - b).abs() < 1e-6, "{ctx}: {a} != {b}"),
        (a, b) => assert_eq!(a, b, "{ctx}"),
    }
}

#[test]
fn prepare_decisions_match_python_golden() {
    for row in golden_rows("reprobe_decisions") {
        let now = num(&row["now"]);
        let setup = dict(&row["setup"]);
        let o = owned(setup, now);
        let mut r = reprobe_before(&row);
        let mut q = quiet_of(dict(dict(&row["before"]).get("quiet").unwrap()));
        let id = row.id().to_string();
        let mut last_error = o.last_error.clone();
        let mut detail = setup
            .get("last_error_detail")
            .and_then(PyValue::as_str)
            .map(str::to_string);
        let mut state = o.state.clone();
        match row.str("op") {
            "prepare" => {
                let got = prepare(&mut r, &mut q, &o.input(), t(now));
                let got_s = got.map(|a| format!("{a:?}").to_lowercase());
                assert_eq!(got_s.as_deref(), row.opt_str("action"), "case {id} action");
                if got == Some(ReprobeAction::Attach) {
                    // `_prepare_reprobe_locked` 的 DETACHED 分支由呼叫端落 ATTACHING＋last_error=None。
                    state = "ATTACHING".into();
                    last_error = None;
                }
            }
            "record" => record_attempt(&mut r, t(now)),
            "finish" => {
                let became_ready = o.state == "READY" || row.str("result_state") == "READY";
                let matched = bootloader_evidence(&o.tail, &o.profile);
                if let Some(fin) = finish_probe(&mut r, &mut q, became_ready, matched, t(now)) {
                    if fin.last_error.is_some() {
                        last_error = fin.last_error;
                        detail = fin.last_error_detail;
                    }
                }
            }
            op => panic!("{op}"),
        }
        assert_eq!(
            r.attempts,
            row["attempts"].as_i64().unwrap() as u32,
            "case {id} attempts"
        );
        assert_eq!(r.exhausted, row.bool("exhausted"), "case {id} exhausted");
        assert_rel(
            r.next_reprobe_at.map(|m| secs(m) - now),
            opt_num(row.0.get("next_reprobe_at_rel")),
            &format!("case {id} next_reprobe_at_rel"),
        );
        assert_eq!(q.pending, row.bool("pending"), "case {id} pending");
        assert_eq!(q.failed, row.bool("failed"), "case {id} failed");
        assert_eq!(
            q.attempts,
            row["ready_reconfirm_attempts"].as_i64().unwrap() as u32,
            "case {id} ready_reconfirm_attempts"
        );
        assert_eq!(state, row.str("state"), "case {id} state");
        assert_eq!(
            last_error.as_deref(),
            row.opt_str("last_error"),
            "case {id} last_error"
        );
        assert_eq!(
            detail.as_deref(),
            row.opt_str("last_error_detail"),
            "case {id} last_error_detail"
        );
    }
}

#[test]
fn target_still_valid_matches_python_golden_for_every_early_return() {
    for row in golden_rows("reprobe_valid") {
        let now = num(&row["now"]);
        let o = owned(dict(&row["setup"]), now);
        let before = dict(&row["before"]);
        let rp = dict(before.get("reprobe").unwrap());
        let r = ReprobeState {
            attempts: rp.get("attempts").and_then(PyValue::as_i64).unwrap_or(0) as u32,
            next_reprobe_at: None,
            exhausted: rp
                .get("exhausted")
                .and_then(PyValue::as_bool)
                .unwrap_or(false),
            inflight: false,
        };
        let mut q = quiet_of(dict(before.get("quiet").unwrap()));
        let got = target_still_valid(&r, &mut q, &o.valid(row.bool("same_bridge")), t(now));
        assert_eq!(got, row.bool("valid"), "case {}", row.id());
        assert_eq!(
            (q.pending, q.failed),
            (row.bool("pending"), row.bool("failed")),
            "case {}",
            row.id()
        );
    }
}

#[test]
fn backoff_schedule_2_4_8_15_15_until_exhausted_at_10() {
    let mut r = ReprobeState::default();
    let mut gaps = vec![];
    for i in 0..10 {
        record_attempt(&mut r, t(100.0 * i as f64));
        gaps.push(r.next_reprobe_at.map(|m| secs(m) - 100.0 * i as f64));
    }
    assert_eq!(
        gaps,
        [
            Some(2.0),
            Some(4.0),
            Some(8.0),
            Some(15.0),
            Some(15.0),
            Some(15.0),
            Some(15.0),
            Some(15.0),
            Some(15.0),
            None
        ]
    );
    assert!(r.exhausted);
    reset(&mut r);
    assert_eq!(r, ReprobeState::default());
}

#[test]
fn reprobe_eligibility_attached_and_detached() {
    for e in [
        "PROMPT_UNAVAILABLE",
        "PROMPT_TIMEOUT",
        "SHELL_PROMPT_TIMEOUT",
        "READY_PROMPT_TIMEOUT",
        "RX_FLOOD",
        "TRANSPORT_STALL",
    ] {
        assert!(is_reprobe_prompt_error("ATTACHED", Some(e)), "ATTACHED {e}");
    }
    for e in [
        Some("LOGIN_REQUIRED"),
        Some("READY_NONCE_TIMEOUT"),
        Some("BOOTLOADER_STUCK"),
        Some("CREDENTIALS_UNRESOLVED"),
        None,
    ] {
        assert!(!is_reprobe_prompt_error("ATTACHED", e), "ATTACHED {e:?}");
    }
    assert!(!is_reprobe_prompt_error(
        "DETACHED",
        Some("PROMPT_UNAVAILABLE")
    ));
    assert!(is_reprobe_prompt_error(
        "DETACHED",
        Some("SHELL_PROMPT_TIMEOUT")
    ));
    assert!(!is_reprobe_prompt_error(
        "READY",
        Some("SHELL_PROMPT_TIMEOUT")
    ));
    assert!(!is_reprobe_prompt_error(
        "ATTACHING",
        Some("PROMPT_UNAVAILABLE")
    ));
}

// ── OD-81／DF-27（docs/rust-refactor/reviews/rulings/s2.md「OD-81」）：登入後命令逾時的重探資格。
// Python 基線 `origin/main:sw_core/session_manager.py:2112-2136` 的 `_is_reprobe_prompt_error` 不含
// `POST_LOGIN_CMD_TIMEOUT`（它不以 `_PROMPT_TIMEOUT` 結尾），session 停在 DETACHED 不再重探；擁有者裁決 Rust
// 改為與其他可重探的登入錯誤同樣以退避重探（Python 依 OD-75 不改，差異登記 DF-27）。只精確納入此一碼，
// 不泛化為「所有 `_TIMEOUT`」：`READY_NONCE_TIMEOUT` 等其餘碼維持 Python 的既有結果。

const DF27: &str = "POST_LOGIN_CMD_TIMEOUT";

#[test]
fn df27_post_login_timeout_is_reprobe_eligible() {
    // 兩種合法狀態：ATTACHED（既有 bridge 重探失敗停 ATTACHED）與 DETACHED（attach 登入失敗 drain 後）。
    for state in ["ATTACHED", "DETACHED"] {
        assert!(
            is_reprobe_prompt_error(state, Some(DF27)),
            "DF-27：{state}＋{DF27} 必須可重探"
        );
    }
    // 其他狀態不因此碼取得資格（Python 對 READY／ATTACHING 等一律 False，`:2136`）。
    for state in [
        "READY",
        "ATTACHING",
        "RECOVERING",
        "RELEASED",
        "FLASHING",
        "",
    ] {
        assert!(
            !is_reprobe_prompt_error(state, Some(DF27)),
            "DF-27：{state:?}＋{DF27} 不得可重探"
        );
    }
    // 只精確比對此碼：大小寫、前後綴與包裝前綴（REPROBE_FAILED:／ATTACH_FAILED:）都不算。
    for near in [
        "post_login_cmd_timeout",
        "POST_LOGIN_CMD_TIMEOUT_X",
        "X_POST_LOGIN_CMD_TIMEOUT",
        "REPROBE_FAILED:POST_LOGIN_CMD_TIMEOUT",
        "ATTACH_FAILED:POST_LOGIN_CMD_TIMEOUT",
    ] {
        for state in ["ATTACHED", "DETACHED"] {
            assert!(
                !is_reprobe_prompt_error(state, Some(near)),
                "DF-27 只精確納入 {DF27}：{state}＋{near:?} 不得可重探"
            );
        }
    }
    // 帳密終態與無關錯誤（含其他非 `_PROMPT_TIMEOUT` 結尾的 `_TIMEOUT`）維持 Python 既有結果：兩狀態皆不可重探。
    for e in [
        Some("CREDENTIALS_UNRESOLVED"),
        Some("USER_ENV_MISSING"),
        Some("PASS_ENV_REQUIRED"),
        Some("PASS_ENV_MISSING"),
        Some("LOGIN_USER_REQUIRED"),
        Some("LOGIN_REQUIRED"),
        Some("READY_NONCE_TIMEOUT"),
        Some("RECOVERY_TIMEOUT"),
        Some("CONSOLE_UNLOCK_IDLE_TIMEOUT"),
        Some("BOOTLOADER_STUCK"),
        Some("DEVICE_REMOVED"),
        Some("CLEARED"),
        Some("REBOUND"),
        Some("ATTACH_FAILED:OSError"),
        Some("REPROBE_FAILED:OSError"),
        Some(""),
        None,
    ] {
        for state in ["ATTACHED", "DETACHED"] {
            assert!(
                !is_reprobe_prompt_error(state, e),
                "{state}＋{e:?} 維持不可重探"
            );
        }
    }
    // 既有可重探碼不受影響（`:2121-2135`）：DETACHED 不含 PROMPT_UNAVAILABLE。
    for e in [
        "PROMPT_TIMEOUT",
        "SHELL_PROMPT_TIMEOUT",
        "PRPL_PROMPT_TIMEOUT",
        "LOGIN_PROMPT_TIMEOUT",
        "CONSOLE_UNLOCK_PROMPT_TIMEOUT",
        "RX_FLOOD",
        "TRANSPORT_STALL",
    ] {
        for state in ["ATTACHED", "DETACHED"] {
            assert!(is_reprobe_prompt_error(state, Some(e)), "{state}＋{e}");
        }
    }
    assert!(is_reprobe_prompt_error("ATTACHED", Some("PROMPT_UNAVAILABLE")));
    assert!(!is_reprobe_prompt_error("DETACHED", Some("PROMPT_UNAVAILABLE")));
}

#[test]
fn df27_post_login_timeout_obeys_reprobe_guards() {
    // 依 `wp56_wait_does_not_burn_reprobe_attempts` 的作法：非預設的既有進度（attempts=2、next_reprobe_at 已過）；
    // 有資格的對照組必須照既有規劃排 Probe／Attach，各守衛成立時不排動作、不動 attempts／backoff／inflight 與 quiet。
    let now = 1010.0;
    let r0 = ReprobeState {
        attempts: 2,
        next_reprobe_at: Some(t(1005.0)),
        exhausted: false,
        inflight: false,
    };
    for (state, planned) in [
        ("ATTACHED", ReprobeAction::Probe),
        ("DETACHED", ReprobeAction::Attach),
    ] {
        // 對照組：證明下方各個 None 來自守衛本身。
        {
            let o = wp56_owned(state, Some(DF27), now);
            let (mut r, mut q) = (r0.clone(), QuietState::default());
            assert_eq!(
                prepare(&mut r, &mut q, &o.input(), t(now)),
                Some(planned),
                "DF-27 {state}：守衛皆不成立時應照既有規劃"
            );
            match planned {
                // Probe：只排下次時間（backoff(attempts=2)=8 s）與 single-flight，不記 attempt（`finish_probe` 才記）。
                ReprobeAction::Probe => assert_eq!(
                    r,
                    ReprobeState {
                        attempts: 2,
                        next_reprobe_at: Some(t(now + 8.0)),
                        exhausted: false,
                        inflight: true,
                    },
                    "DF-27 {state}：Probe 規劃"
                ),
                // Attach：真正排一次重新 attach 才記一次 attempt（attempts 3 → next=now+backoff(2)=8 s）。
                ReprobeAction::Attach => assert_eq!(
                    r,
                    ReprobeState {
                        attempts: 3,
                        next_reprobe_at: Some(t(now + 8.0)),
                        exhausted: false,
                        inflight: false,
                    },
                    "DF-27 {state}：Attach 規劃記一次 attempt"
                ),
            }
        }

        type Guard = fn(&mut Owned, &mut ReprobeState, &mut QuietState);
        let guards: [(&str, Guard); 7] = [
            ("boot quiet", |_, _, q| q.arm(t(1000.0))),
            ("boot evidence wait", |_, _, q| {
                q.begin_boot_evidence_wait(t(1001.0))
            }),
            ("未到退避期限", |_, r, _| {
                r.next_reprobe_at = Some(t(1010.5))
            }),
            ("RX 未閒置 3 s", |o, _, _| o.last_rx_mono = Some(t(1008.0))),
            ("裝置不在場", |o, _, _| o.device_present = false),
            ("human active", |o, _, _| o.human_active = true),
            ("by_id 已 release", |o, _, _| o.released = true),
        ];
        for (name, apply) in guards {
            let mut o = wp56_owned(state, Some(DF27), now);
            let mut r = r0.clone();
            let mut q = QuietState::default();
            apply(&mut o, &mut r, &mut q);
            let (r_before, q_before) = (r.clone(), wp56_quiet_snapshot(&q));
            assert_eq!(
                prepare(&mut r, &mut q, &o.input(), t(now)),
                None,
                "DF-27 {state}／{name}：守衛成立時不得規劃 probe／attach"
            );
            assert_eq!(
                r, r_before,
                "DF-27 {state}／{name}：attempts／next_reprobe_at／exhausted／inflight 不得變動"
            );
            assert_eq!(
                wp56_quiet_snapshot(&q),
                q_before,
                "DF-27 {state}／{name}：quiet 與證據等待不得變動"
            );
        }

        // 已耗盡（10 次）不再規劃；只把 next_reprobe_at 清掉（既有 `prepare` 語意），attempts 不再增加。
        let o = wp56_owned(state, Some(DF27), now);
        let mut r = ReprobeState {
            attempts: REPROBE_MAX_ATTEMPTS,
            next_reprobe_at: None,
            exhausted: true,
            inflight: false,
        };
        let mut q = QuietState::default();
        assert_eq!(prepare(&mut r, &mut q, &o.input(), t(now)), None, "{state}：exhausted");
        assert_eq!(r.attempts, REPROBE_MAX_ATTEMPTS, "{state}：耗盡後不再計 attempt");
    }

    // ATTACHED 專屬：single-flight 與無 bridge 時不排 probe。
    for (name, inflight, has_bridge) in [("inflight", true, true), ("無 bridge", false, false)] {
        let mut o = wp56_owned("ATTACHED", Some(DF27), now);
        o.has_bridge = has_bridge;
        let mut r = ReprobeState {
            inflight,
            ..r0.clone()
        };
        let r_before = r.clone();
        let mut q = QuietState::default();
        assert_eq!(prepare(&mut r, &mut q, &o.input(), t(now)), None, "ATTACHED／{name}");
        assert_eq!(r, r_before, "ATTACHED／{name}：進度不得變動");
    }

    // 最終驗證（`_reprobe_target_still_valid_locked`）：ATTACHED＋DF-27 通過；寫入前才成立的 quiet／等待仍擋下。
    let o = wp56_owned("ATTACHED", Some(DF27), now);
    let r = ReprobeState::default();
    assert!(
        target_still_valid(&r, &mut QuietState::default(), &o.valid(true), t(now)),
        "DF-27 ATTACHED：最終驗證通過"
    );
    let mut q = QuietState::default();
    q.arm(t(now));
    assert!(!target_still_valid(&r, &mut q, &o.valid(true), t(now)), "quiet 中最終驗證擋下");
    let mut q = QuietState::default();
    q.begin_boot_evidence_wait(t(now));
    assert!(!target_still_valid(&r, &mut q, &o.valid(true), t(now)), "證據等待中最終驗證擋下");
}

#[test]
fn rx_not_idle_for_3s_and_single_flight() {
    let sp = session_profile_for("prpl-template");
    let inp = |rx: Option<Mono>| ReprobeInput {
        state: "ATTACHED",
        by_id: "/dev/serial/by-id/fake",
        released: false,
        device_present: true,
        last_error: Some("PROMPT_UNAVAILABLE"),
        last_rx_mono: rx,
        human_active: false,
        has_bridge: true,
        profile: &sp,
    };
    let mut r = ReprobeState::default();
    let mut q = QuietState::default();
    assert_eq!(
        prepare(&mut r, &mut q, &inp(Some(t(97.01))), t(100.0)),
        None
    );
    assert_eq!(
        prepare(&mut r, &mut q, &inp(Some(t(97.0))), t(100.0)),
        Some(ReprobeAction::Probe)
    );
    assert!(r.inflight);
    r.next_reprobe_at = None;
    assert_eq!(
        prepare(&mut r, &mut q, &inp(None), t(100.0)),
        None,
        "inflight 時不重複排（single-flight）"
    );
}

#[test]
fn bootloader_evidence_falls_back_to_uboot_prompts() {
    let mut sp = session_profile_for("prpl-template");
    sp.template.bootloader_prompts = Vec::new();
    assert_eq!(
        bootloader_evidence("\r\n=> ", &sp).as_deref(),
        Some("^=> $")
    );
    assert_eq!(bootloader_evidence("\r\nroot@prplOS:~# ", &sp), None);
    assert_eq!(
        bootloader_evidence("\x1b[1m\r\nCFE> \x1b[0m", &sp).as_deref(),
        Some("^CFE> $")
    );
}

// ── WP5-6 第 3 項（G8，#243）：非同步 reboot 開機證據等待的 reprobe 守衛。
// 依據 `origin/main:sw_core/session_manager.py`：`_prepare_reprobe_locked` 於 quiet 守衛（`:2216-2217`）之後
// `if session.boot_evidence_wait_active(now): return None`（`:2218-2223`，不排 backoff、不增加 attempts）；
// `_reprobe_target_still_valid_locked` 於 quiet 守衛（`:2335-2338`）之後同樣重驗（`:2339-2340`）。
// 對照 `tests/test_async_reboot_evidence.py` 的 `test_wait_does_not_burn_reprobe_attempts`、
// `test_still_valid_recheck_blocks_probe_when_wait_starts_after_collection`、`test_no_banner_confirms_after_grace`。

/// 與 golden `setup` 同形的輸入（`Owned`）；prpl-template、裝置在場、有 bridge、RX 已閒置 3.5 s。
fn wp56_owned(state: &str, last_error: Option<&str>, now: f64) -> Owned {
    let profile = session_profile_for("prpl-template");
    Owned {
        state: state.to_string(),
        by_id: profile.device_by_id.clone(),
        released: false,
        device_present: true,
        last_error: last_error.map(str::to_string),
        last_rx_mono: Some(t(now - 3.5)),
        human_active: false,
        has_bridge: true,
        tail: String::new(),
        profile,
    }
}

/// Python `arm_boot_quiet()`＋`clear_boot_quiet()`：quiet 已解除、reconfirm pending 留著（確認 probe 原本合法）。
fn wp56_pending_quiet(arm_at: f64) -> QuietState {
    let mut q = QuietState::default();
    q.arm(t(arm_at));
    q.clear_quiet();
    q
}

/// `QuietState` 未實作 `PartialEq`；逐欄位快照（含 `ready_reconfirm_attempts`＝`attempts` 與等待期限）。
fn wp56_quiet_snapshot(q: &QuietState) -> (Mono, String, bool, Mono, u32, bool, Mono) {
    (
        q.boot_quiet_until,
        q.boot_banner_tail.clone(),
        q.pending,
        q.deadline,
        q.attempts,
        q.failed,
        q.boot_evidence_wait_until,
    )
}

#[test]
fn wp56_wait_does_not_burn_reprobe_attempts() {
    // (名稱, state, last_error, 未等待時的既有規劃)：READY＋pending、ATTACHED＋可重探錯誤，
    // 以及 DETACHED（Python 的等待守衛在 state 分派之前，Attach 分支的 `_record_reprobe_attempt_locked` 也不得執行）。
    let cases: [(&str, &str, Option<&str>, ReprobeAction); 3] = [
        ("READY+pending", "READY", None, ReprobeAction::Probe),
        (
            "ATTACHED+PROMPT_UNAVAILABLE",
            "ATTACHED",
            Some("PROMPT_UNAVAILABLE"),
            ReprobeAction::Probe,
        ),
        (
            "DETACHED+SHELL_PROMPT_TIMEOUT",
            "DETACHED",
            Some("SHELL_PROMPT_TIMEOUT"),
            ReprobeAction::Attach,
        ),
    ];
    for (name, state, last_error, planned) in cases {
        // 非預設的既有進度：attempts=2、next_reprobe_at 已過、未 exhausted、無 inflight；pending 時另有 1 次再確認失敗。
        let r0 = ReprobeState {
            attempts: 2,
            next_reprobe_at: Some(t(1005.0)),
            exhausted: false,
            inflight: false,
        };
        let mut q0 = if state == "READY" {
            wp56_pending_quiet(1000.0)
        } else {
            QuietState::default()
        };
        q0.attempts = if state == "READY" { 1 } else { 0 };

        // 對照組：同一輸入、未進等待 → 既有規劃照常成立（證明下方的 None 來自等待守衛）。
        {
            let o = wp56_owned(state, last_error, 1010.0);
            let (mut r, mut q) = (r0.clone(), q0.clone());
            assert_eq!(
                prepare(&mut r, &mut q, &o.input(), t(1010.0)),
                Some(planned),
                "{name}：未等待時應照既有規劃"
            );
        }

        let mut r = r0.clone();
        let mut q = q0.clone();
        q.begin_boot_evidence_wait(t(1001.0)); // until = 1061 s
        let q_before = wp56_quiet_snapshot(&q);
        for now in [1010.0, 1020.0, 1030.0] {
            let o = wp56_owned(state, last_error, now);
            assert_eq!(
                prepare(&mut r, &mut q, &o.input(), t(now)),
                None,
                "{name} @{now}：等待中不得規劃 probe／attach"
            );
            assert_eq!(
                r, r0,
                "{name} @{now}：attempts／next_reprobe_at／exhausted／inflight 不得因等待變動"
            );
            assert_eq!(
                wp56_quiet_snapshot(&q),
                q_before,
                "{name} @{now}：reconfirm pending／deadline／attempts 與等待期限不得變動"
            );
        }
    }
}

#[test]
fn wp56_still_valid_recheck_blocks_probe_when_wait_starts_after_collection() {
    for (name, state, last_error) in [
        ("READY+pending", "READY", None),
        (
            "ATTACHED+PROMPT_UNAVAILABLE",
            "ATTACHED",
            Some("PROMPT_UNAVAILABLE"),
        ),
    ] {
        let now = 1010.0;
        let o = wp56_owned(state, last_error, now);
        let mut r = ReprobeState::default();
        let mut q = if state == "READY" {
            wp56_pending_quiet(1000.0)
        } else {
            QuietState::default()
        };
        // job 收集：先取得合法 Probe。
        assert_eq!(
            prepare(&mut r, &mut q, &o.input(), t(now)),
            Some(ReprobeAction::Probe),
            "{name}：收集時 Probe 合法"
        );
        assert!(
            target_still_valid(&r, &mut q, &o.valid(true), t(now)),
            "{name}：未等待時最終驗證通過"
        );
        // 收集後、worker 寫入前才進入等待（例如 reboot 剛送出）→ 最終驗證擋下。
        q.begin_boot_evidence_wait(t(now));
        let q_before = wp56_quiet_snapshot(&q);
        assert!(
            !target_still_valid(&r, &mut q, &o.valid(true), t(now)),
            "{name}：等待開始後最終驗證必須為 false"
        );
        assert_eq!(
            wp56_quiet_snapshot(&q),
            q_before,
            "{name}：重驗不得清等待、不得落 reconfirm 終態"
        );
    }
}

#[test]
fn wp56_probe_planning_resumes_at_grace_expiry() {
    // READY＋pending：arm 於 1000 s → reconfirm deadline 1300 s；等待於 1001 s 開始 → until 1061 s。
    let mut r = ReprobeState::default();
    let mut q = wp56_pending_quiet(1000.0);
    let deadline = q.deadline;
    assert_eq!(deadline, t(1300.0));
    q.begin_boot_evidence_wait(t(1001.0));
    let until = q.boot_evidence_wait_until;
    assert_eq!(until, t(1061.0));

    // 寬限到期前 1 ns 仍擋，且不留任何進度痕跡。
    let o = wp56_owned("READY", None, secs(until - 1));
    assert_eq!(prepare(&mut r, &mut q, &o.input(), until - 1), None);
    assert_eq!(r, ReprobeState::default());
    assert!(!target_still_valid(&r, &mut q, &o.valid(true), until - 1));

    // `boot_evidence_wait_active`：`now < until`；恰好到期即恢復原規劃（backoff(0)=2 s、single-flight）。
    let o = wp56_owned("READY", None, secs(until));
    assert!(target_still_valid(&r, &mut q, &o.valid(true), until));
    assert_eq!(
        prepare(&mut r, &mut q, &o.input(), until),
        Some(ReprobeAction::Probe)
    );
    assert_eq!(
        r,
        ReprobeState {
            attempts: 0,
            next_reprobe_at: Some(until + t(2.0)),
            exhausted: false,
            inflight: true,
        }
    );
    // 寬限不延長 READY_RECONFIRM_MAX_S：deadline 仍是 arm 時的 1300 s，pending 不變、無再確認失敗。
    assert_eq!(q.deadline, deadline);
    assert!(q.pending);
    assert!(!q.failed);
    assert_eq!(q.attempts, 0);
}
