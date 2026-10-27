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
