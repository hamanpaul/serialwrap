//! P7 Task 5（08-slice23-login-events.md）：SessionCore 1 s tick 的 readiness reprobe 接線
//! （`reconcile_readiness` → `_prepare_reprobe_locked` → `_probe_existing_bridge` → `_finish_probe_reprobe`）。
//!
//! 偏離計畫原文：`StartLogin.kind`（Task 7 `ExistingBridgeProbe{AutoReprobe}`）尚未凍結，本檔只斷言 StartLogin 發出與收斂；
//! `human_active` 依 P6 `lease_context`（07 T7 骨架），human lease 尚不可建立，列 BLOCKED-UPSTREAM。
#![cfg(feature = "test-fakes")]
mod support;

use std::time::Duration;

use support::*;
use sw_core::login_fsm::LoginOutcome;
use sw_core::mailbox::ControlEvent;
use sw_core::session_core::{Effect, SessionState};
use sw_pal::traits::Clock;
use sw_proto::pyjson::PyValue;

const PRPL_PROFILE: &str = "profiles:\n  r:\n    platform: prpl\n    prompt_regex: '(?m)^root@prplOS:.*# '\n    timeout_s: 2.0\ntargets:\n  - {com: COM0, profile: r, act_no: 1, alias: r+1, device_by_id: /s/by-id/usb-A}\n";

fn start_login(effs: &[Effect]) -> Option<(u64, u64)> {
    effs.iter().find_map(|e| match e {
        Effect::StartLogin { epoch, fsm_gen, .. } => Some((*epoch, *fsm_gen)),
        _ => None,
    })
}

/// 推進 FakeClock 並收集每秒 Tick 的效果。
fn advance_collect(h: &mut CoreHarness, secs: u64) -> Vec<Effect> {
    let mut all = Vec::new();
    for _ in 0..secs {
        all.extend(h.advance(Duration::from_secs(1)));
    }
    all
}

#[test]
fn attached_prompt_error_is_reprobed_after_rx_idle_and_ready_resets_progress() {
    let mut h = CoreHarness::new(PRPL_PROFILE);
    h.to_attached(Some("PROMPT_UNAVAILABLE"));
    let fx = advance_collect(&mut h, 4);
    let (epoch, gen) =
        start_login(&fx).expect("ATTACHED＋PROMPT_UNAVAILABLE、RX 安靜 ≥3 s → tick 自動重探");
    let p = h.public();
    assert_eq!(
        p["reprobe_attempts"],
        PyValue::from(0),
        "排 probe 時不計 attempt"
    );
    assert!(
        matches!(p["next_reprobe_at"], PyValue::Float(_)),
        "next_reprobe_at 為 monotonic 秒"
    );
    h.feed(ControlEvent::LoginFinished {
        epoch,
        fsm_gen: gen,
        result: LoginOutcome::Ready,
        after_rx_seq: None,
    });
    let p = h.public();
    assert_eq!(p["state"], PyValue::from("READY"));
    assert_eq!(p["reprobe_attempts"], PyValue::from(0));
    assert_eq!(p["next_reprobe_at"], PyValue::Null);
}

#[test]
fn reprobe_failure_records_attempt_and_backs_off() {
    let mut h = CoreHarness::new(PRPL_PROFILE);
    h.to_attached(Some("PROMPT_UNAVAILABLE"));
    let fx = advance_collect(&mut h, 4);
    let (epoch, gen) = start_login(&fx).expect("自動重探");
    h.feed(ControlEvent::LoginFinished {
        epoch,
        fsm_gen: gen,
        result: LoginOutcome::NotReady("PROMPT_UNAVAILABLE".into()),
        after_rx_seq: None,
    });
    let p = h.public();
    assert_eq!(p["state"], PyValue::from("ATTACHED"));
    assert_eq!(p["reprobe_attempts"], PyValue::from(1));
    // record_attempt：next = 完成時刻 + 2 s；1 s 後不再排、≥2 s 後才再排。
    assert!(start_login(&advance_collect(&mut h, 1)).is_none());
    assert!(start_login(&advance_collect(&mut h, 2)).is_some());
}

#[test]
fn credentials_unresolved_never_reprobes() {
    let mut h = CoreHarness::new(PRPL_PROFILE);
    h.to_attached(Some("CREDENTIALS_UNRESOLVED"));
    assert!(start_login(&advance_collect(&mut h, 20)).is_none());
    assert_eq!(h.public()["reprobe_attempts"], PyValue::from(0));
}

#[test]
fn reprobe_bootloader_tail_sets_bootloader_stuck() {
    let mut h = CoreHarness::new(PRPL_PROFILE);
    h.to_attached(Some("PROMPT_UNAVAILABLE"));
    let fx = advance_collect(&mut h, 4);
    let (epoch, gen) = start_login(&fx).expect("自動重探");
    // reader 先把 RX 寫入共享視窗（RxShared）再投遞 data lane；harness 的 rx() 只投遞 data lane。
    let now = h.clock.mono_ns();
    h.rx.append(b"\r\n=> ", now, epoch, 9_999);
    h.rx(b"\r\n=> ");
    h.feed(ControlEvent::LoginFinished {
        epoch,
        fsm_gen: gen,
        result: LoginOutcome::NotReady("PRPL_PROMPT_TIMEOUT".into()),
        after_rx_seq: None,
    });
    let p = h.public();
    assert_eq!(p["last_error"], PyValue::from("BOOTLOADER_STUCK"));
    assert_eq!(
        p["last_error_detail"],
        PyValue::from("matched_prompt=^=> $")
    );
    assert_eq!(p["reprobe_exhausted"], PyValue::Bool(true));
    assert!(
        start_login(&advance_collect(&mut h, 30)).is_none(),
        "exhausted 後不再重探"
    );
}

#[test]
fn ready_pending_schedules_confirm_probe_and_failure_counts_reconfirm_attempt() {
    // #162：名義 READY＋pending、quiet 已過期 → 排確認 probe；失敗 → ATTACHED、ready_reconfirm 次數累加（不 flush）。
    let mut h = CoreHarness::new(PRPL_PROFILE);
    h.to_ready();
    h.rx(b"\r\nU-Boot 2024.04\r\n");
    h.rx(b"root@prplOS:~# "); // prompt 解除 quiet、保留 pending
    assert_eq!(h.public()["ready_reconfirm_pending"], PyValue::Bool(true));
    let fx = advance_collect(&mut h, 4);
    let (epoch, gen) = start_login(&fx).expect("READY＋pending → 確認 probe");
    let before = h.core.quiet_for_test().attempts;
    h.feed(ControlEvent::LoginFinished {
        epoch,
        fsm_gen: gen,
        result: LoginOutcome::NotReady("READY_NONCE_TIMEOUT".into()),
        after_rx_seq: None,
    });
    assert_eq!(h.core.state(), SessionState::Attached);
    assert_eq!(h.public()["ready_reconfirm_pending"], PyValue::Bool(true));
    // 審查 c11aa89_92e0223 Opus M-2：#162 A6 pending 中失敗累加 ready_reconfirm_attempts。
    assert_eq!(h.core.quiet_for_test().attempts, before + 1);
}

#[test]
fn detached_timeout_error_reattaches() {
    // DETACHED＋SHELL/PRPL_PROMPT_TIMEOUT、裝置在 → ATTACHING＋OpenBridge（`_prepare_reprobe_locked` attach 分支）。
    let mut h = CoreHarness::new(PRPL_PROFILE);
    h.feed(ControlEvent::DeviceAttached {
        by_id: DEVICE_BY_ID.into(),
        real_path: REAL_PATH.into(),
    });
    h.bridge_ok();
    let (epoch, gen) = (h.epoch(), h.core.generations().fsm_gen);
    h.set_env("UNUSED", "x");
    h.feed(ControlEvent::LoginFinished {
        epoch,
        fsm_gen: gen,
        result: LoginOutcome::Failed("PRPL_PROMPT_TIMEOUT".into()),
        after_rx_seq: None,
    });
    let (stop_epoch, stop) = h.pending_stop();
    h.receipt(stop_epoch, stop).feed();
    assert_eq!(h.public()["state"], PyValue::from("DETACHED"));
    let fx = advance_collect(&mut h, 4);
    assert!(
        fx.iter().any(|e| matches!(e, Effect::OpenBridge { .. })),
        "DETACHED 逾時類失敗 → 自動 attach"
    );
    assert_eq!(h.public()["state"], PyValue::from("ATTACHING"));
    assert_eq!(h.public()["reprobe_attempts"], PyValue::from(1));
}

#[test]
fn manual_attach_during_auto_reprobe_waits_then_probes_afresh() {
    // Python：手動 attach／recover 取 probe 鎖時會等自動重探 worker 結束，再自行 `_probe_existing_bridge`；
    // 自動重探的（舊世代）結果不得回覆給手動請求。
    let mut h = CoreHarness::new(PRPL_PROFILE);
    h.to_attached(Some("PROMPT_UNAVAILABLE"));
    let fx = advance_collect(&mut h, 4);
    let (epoch, auto_gen) = start_login(&fx).expect("自動重探");
    let (reply, fx) = h.rpc_deferred(sw_core::session_core::SessionRequest::SessionAttach);
    assert!(start_login(&fx).is_none(), "自動重探進行中：手動請求先等待");
    assert!(reply.try_recv().is_err());
    let fx = h.feed(ControlEvent::LoginFinished {
        epoch,
        fsm_gen: auto_gen,
        result: LoginOutcome::NotReady("PROMPT_UNAVAILABLE".into()),
        after_rx_seq: None,
    });
    let (_, manual_gen) = start_login(&fx).expect("auto 收斂後手動請求以新世代重新 probe");
    assert!(manual_gen > auto_gen);
    assert!(reply.try_recv().is_err(), "手動 probe 尚未完成前不回覆");
    h.feed(ControlEvent::LoginFinished {
        epoch,
        fsm_gen: manual_gen,
        result: LoginOutcome::Ready,
        after_rx_seq: None,
    });
    let resp = reply
        .try_recv()
        .expect("手動 probe 完成即回覆")
        .expect("ok");
    assert_eq!(resp["ok"], PyValue::Bool(true));
    assert_eq!(h.public()["state"], PyValue::from("READY"));
}

#[test]
fn quiet_arm_during_auto_reprobe_settles_and_resumes() {
    // 審查 c11aa89_92e0223 Opus H-1：自動重探中 banner arm quiet → FSM 以原世代回報；Python `_login_tx_refused`
    // → probe_refused（ATTACHED＋PROMPT_UNAVAILABLE）＋`_finish_probe_reprobe` 記 attempt、清 inflight；手動 attach 照常回覆。
    let mut h = CoreHarness::new(PRPL_PROFILE);
    h.to_attached(Some("PROMPT_UNAVAILABLE"));
    let fx = advance_collect(&mut h, 4);
    let (epoch, gen) = start_login(&fx).expect("自動重探");
    h.rx(b"\r\nU-Boot 2024.04\r\n");
    let (reply, fx) = h.rpc_deferred(sw_core::session_core::SessionRequest::SessionAttach);
    assert!(start_login(&fx).is_none());
    h.feed(ControlEvent::LoginFinished {
        epoch,
        fsm_gen: gen,
        result: LoginOutcome::Aborted(sw_core::login_fsm::LoginAbort::Cancelled),
        after_rx_seq: None,
    });
    let p = h.public();
    assert_eq!(p["state"], PyValue::from("ATTACHED"));
    assert_eq!(p["last_error"], PyValue::from("PROMPT_UNAVAILABLE"));
    assert_eq!(p["reprobe_attempts"], PyValue::from(1));
    assert!(matches!(p["next_reprobe_at"], PyValue::Float(_)));
    // quiet 中手動 attach 不送 probe、直接回覆（`_probe_existing_bridge` quiet gate）。
    assert!(reply.try_recv().is_ok(), "手動 SessionAttach 須回覆");
    // quiet 到期且 RX 安靜後恢復重探。
    let fx = advance_collect(&mut h, 200);
    assert!(start_login(&fx).is_some(), "quiet 到期後恢復自動重探");
}

#[test]
fn quiet_arm_during_auto_reprobe_ready_result_is_ready() {
    // 審查 92e0223_76de0cf d5b2dd6-1：Python `_probe_existing_bridge` 只依 probe 實際結果收斂；probe 期間 banner 才 arm、
    // FSM 仍回 ok → READY＋confirm_ready（#139）。
    let mut h = CoreHarness::new(PRPL_PROFILE);
    h.to_attached(Some("PROMPT_UNAVAILABLE"));
    let fx = advance_collect(&mut h, 4);
    let (epoch, gen) = start_login(&fx).expect("自動重探");
    h.rx(b"\r\nU-Boot 2024.04\r\n");
    assert_eq!(h.public()["ready_reconfirm_pending"], PyValue::Bool(true));
    h.feed(ControlEvent::LoginFinished {
        epoch,
        fsm_gen: gen,
        result: LoginOutcome::Ready,
        after_rx_seq: None,
    });
    let p = h.public();
    assert_eq!(p["state"], PyValue::from("READY"));
    assert_eq!(p["last_error"], PyValue::Null);
    assert_eq!(p["ready_reconfirm_pending"], PyValue::Bool(false));
    assert_eq!(p["reprobe_attempts"], PyValue::from(0));
}

#[test]
fn quiet_arm_during_auto_reprobe_not_ready_keeps_failure_code() {
    let mut h = CoreHarness::new(PRPL_PROFILE);
    h.to_attached(Some("PROMPT_UNAVAILABLE"));
    let fx = advance_collect(&mut h, 4);
    let (epoch, gen) = start_login(&fx).expect("自動重探");
    h.rx(b"\r\nU-Boot 2024.04\r\n");
    h.feed(ControlEvent::LoginFinished {
        epoch,
        fsm_gen: gen,
        result: LoginOutcome::NotReady("READY_NONCE_TIMEOUT".into()),
        after_rx_seq: None,
    });
    let p = h.public();
    assert_eq!(p["state"], PyValue::from("ATTACHED"));
    assert_eq!(p["last_error"], PyValue::from("READY_NONCE_TIMEOUT"));
    assert_eq!(p["reprobe_attempts"], PyValue::from(1));
}

#[test]
fn reprobe_tick_while_manual_probe_busy_advances_next_reprobe_at() {
    // 審查 92e0223_76de0cf d5b2dd6-3（L-3 回歸）：手動 attach 持有 probe 時 `_prepare_reprobe_locked` 照常排程，
    // worker 取不到 probe 鎖才跳過——`next_reprobe_at` 前移、不起新的 StartLogin。
    let mut h = CoreHarness::new(PRPL_PROFILE);
    h.to_attached(Some("PROMPT_UNAVAILABLE"));
    assert_eq!(h.public()["next_reprobe_at"], PyValue::Null);
    let (_reply, fx) = h.rpc_deferred(sw_core::session_core::SessionRequest::SessionAttach);
    assert!(start_login(&fx).is_some(), "手動再探起 run");
    let fx = advance_collect(&mut h, 4);
    assert!(
        start_login(&fx).is_none(),
        "probe 被占用：不起新的 StartLogin"
    );
    assert!(
        matches!(h.public()["next_reprobe_at"], PyValue::Float(_)),
        "prepare 照常排程 next_reprobe_at"
    );
}
