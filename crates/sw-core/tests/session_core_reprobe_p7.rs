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
fn auto_reprobe_result_does_not_change_state_after_handoff_freeze() {
    let mut h = CoreHarness::new(PRPL_PROFILE);
    h.to_attached(Some("PROMPT_UNAVAILABLE"));
    let effects = advance_collect(&mut h, 4);
    let (epoch, gen) = start_login(&effects).expect("開始自動重探");
    h.reg.admission_gate().freeze();
    let effects = h.feed(ControlEvent::LoginFinished {
        epoch,
        fsm_gen: gen,
        result: LoginOutcome::Ready,
        after_rx_seq: None,
    });
    assert_eq!(h.core.state(), SessionState::Attached, "凍結後的 probe 結果不得升 READY");
    assert_eq!(h.public()["last_error"], PyValue::from("PROMPT_UNAVAILABLE"));
    assert!(!effects.iter().any(|e| matches!(e, Effect::Notify(_))), "凍結後不得發布狀態推進通知");
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
    // 審查 92e0223_76de0cf d5b2dd6-1 的原期望（Python `_probe_existing_bridge` 只依 probe 實際結果收斂；probe 期間 banner
    // 才 arm、FSM 仍回 ok → READY＋confirm_ready，#139）依 OD-35（2026-10-28）被已核准 SC-30 取代（非削弱）：
    // arm 當下即取消自動重探並以 quiet 拒絕收斂（記 attempt、#162 pending 保留），之後抵達的舊世代 Ready 被丟棄。
    // nodeid 已入 L1 鎖，名稱為歷史名稱。
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
    assert_eq!(p["state"], PyValue::from("ATTACHED"));
    assert_eq!(p["last_error"], PyValue::from("PROMPT_UNAVAILABLE"));
    assert_eq!(p["ready_reconfirm_pending"], PyValue::Bool(true), "不得解除 #162 gate");
    assert_eq!(p["reprobe_attempts"], PyValue::from(1));
}

#[test]
fn quiet_arm_during_auto_reprobe_not_ready_keeps_failure_code() {
    // OD-35（SC-30）：原期望「舊世代 NotReady 的失敗碼照套用」被取代——arm 當下已以 quiet 拒絕收斂
    // （`PROMPT_UNAVAILABLE`、attempt 記一次），之後抵達的舊世代結果被丟棄、不再記第二次 attempt。名稱為歷史名稱。
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
    assert_eq!(p["last_error"], PyValue::from("PROMPT_UNAVAILABLE"));
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

#[test]
fn wp56_reprobe_tick_holds_confirm_probe_during_boot_evidence_wait() {
    // WP5-6 第 3 項（G8，#243）接線：1 s tick（`reprobe_tick`）→ `reprobe::prepare` 傳入同一份 `QuietState`。
    // `origin/main:sw_core/session_manager.py:2218-2223`：等待中不排確認 probe、不排 backoff、不增加 attempts；
    // 寬限到期（`now >= boot_evidence_wait_until`）後下一個 tick 恢復原規劃（`test_no_banner_confirms_after_grace`）。
    // 本項尚未接 reboot 路徑的 `begin`（後續子項），等待期限以 test-fakes hook 直接設定。
    let mut h = CoreHarness::new(PRPL_PROFILE);
    h.to_ready();
    h.rx(b"\r\nU-Boot 2024.04\r\n");
    h.rx(b"root@prplOS:~# "); // prompt 解除 quiet、保留 pending（等同非同步 reboot 後的過渡態）
    assert_eq!(h.public()["ready_reconfirm_pending"], PyValue::Bool(true));
    let deadline = h.core.quiet_for_test().deadline;
    let until = h.clock.mono_ns() + 60_000_000_000;
    h.core.set_boot_evidence_wait_until_for_test(until);

    // RX 閒置 ≥3 s 後，寬限內每個 tick 都不得起確認 probe。
    let fx = advance_collect(&mut h, 59);
    assert!(
        start_login(&fx).is_none(),
        "等待中 tick 不得送確認 probe"
    );
    assert!(h.clock.mono_ns() < until);
    let p = h.public();
    assert_eq!(p["state"], PyValue::from("READY"));
    assert_eq!(p["ready_reconfirm_pending"], PyValue::Bool(true));
    assert_eq!(p["reprobe_attempts"], PyValue::from(0), "不增加 attempts");
    assert_eq!(p["next_reprobe_at"], PyValue::Null, "不排 backoff");
    assert_eq!(p["reprobe_exhausted"], PyValue::Bool(false));
    assert_eq!(p["boot_evidence_wait_remaining_s"], PyValue::Float(1.0));
    assert_eq!(h.core.quiet_for_test().attempts, 0, "不增加 reconfirm attempts");

    // 恰好到期的 tick 恢復確認 probe；reconfirm deadline 不因等待延長。
    let fx = advance_collect(&mut h, 1);
    assert_eq!(h.clock.mono_ns(), until);
    assert!(start_login(&fx).is_some(), "寬限到期後照常排確認 probe");
    assert_eq!(h.public()["boot_evidence_wait_remaining_s"], PyValue::Null);
    assert_eq!(h.core.quiet_for_test().deadline, deadline);
}

const WP56_UNLOCK_PROFILE: &str = "profiles:\n  c:\n    platform: shell\n    prompt_regex: '(?m)^root@t:.*# '\n    console_unlock_env: SW_T_UNLOCK\n    timeout_s: 2.0\ntargets:\n  - {com: COM0, profile: c, act_no: 1, alias: c+1, device_by_id: /s/by-id/usb-A}\n";
const WP56_PASSTHROUGH_PROFILE: &str = "profiles:\n  u:\n    platform: passthrough\n    prompt_regex: '=> '\n    timeout_s: 2.0\ntargets:\n  - {com: COM0, profile: u, act_no: 1, alias: u+1, device_by_id: /s/by-id/usb-A}\n";
const WP56_GRACE_NS: u64 = 60_000_000_000;

fn wp56_self_test() -> sw_core::session_core::SessionRequest {
    sw_core::session_core::SessionRequest::SelfTest {
        timeout_s: 2.0,
        strict_human_lock: false,
    }
}

fn wp56_has_self_test_probe(effs: &[Effect]) -> bool {
    effs.iter().any(|e| {
        matches!(
            e,
            Effect::StartOp {
                spec: sw_core::op_runner::OpSpec::SelfTestProbe { .. },
                ..
            }
        )
    })
}

fn wp56_has_start_op(effs: &[Effect]) -> bool {
    effs.iter().any(|e| matches!(e, Effect::StartOp { .. }))
}

/// 以 test-fakes hook 建立自「現在」起算 60 s 的證據等待（reboot 路徑的 `begin` 屬後續子項），回傳期限。
fn wp56_begin_wait(h: &mut CoreHarness) -> u64 {
    let until = h.clock.mono_ns() + WP56_GRACE_NS;
    h.core.set_boot_evidence_wait_until_for_test(until);
    until
}

#[test]
fn wp56_probe_existing_bridge_is_noop_during_wait() {
    // WP5-6 第 4 項（G8，#243）：`origin/main:sw_core/session_manager.py:4746-4753`——`_probe_existing_bridge`
    // 於 quiet 或證據等待有效時不送 probe TX、不開新登入世代；非 READY 誠實回報 PROMPT_UNAVAILABLE。
    // 手動 attach（`:3952`）與非 force recover（`:7998`）兩個 ATTACHED 入口共用此 gate。
    use sw_core::session_core::SessionRequest;
    let recover = || SessionRequest::Recover {
        timeout_s: 2.0,
        force: false,
    };

    // 對照組：同一輸入未進等待時，手動 attach／recover 都會起 probe（證明下方的零 probe 來自等待守衛）。
    for (name, req) in [("attach", SessionRequest::SessionAttach), ("recover", recover())] {
        let mut h = CoreHarness::new(PRPL_PROFILE);
        h.to_attached(Some("READY_NONCE_TIMEOUT"));
        let (_reply, fx) = h.rpc_deferred(req);
        assert!(start_login(&fx).is_some(), "{name}：未等待時照常 probe");
    }

    for (name, req) in [("attach", SessionRequest::SessionAttach), ("recover", recover())] {
        let mut h = CoreHarness::new(PRPL_PROFILE);
        h.to_attached(Some("READY_NONCE_TIMEOUT"));
        let until = wp56_begin_wait(&mut h);
        let gen_before = h.core.generations().fsm_gen;
        let (reply, fx) = h.rpc_deferred(req);
        assert!(start_login(&fx).is_none(), "{name}：等待中不得起 probe");
        assert!(!wp56_has_start_op(&fx), "{name}：等待中不得有任何 TX op");
        let resp = reply
            .try_recv()
            .unwrap_or_else(|e| panic!("{name}：零 probe 時同步回覆：{e:?}"))
            .expect("ok");
        assert_eq!(
            h.core.generations().fsm_gen,
            gen_before,
            "{name}：不開新登入世代（`_begin_login_run_locked` 未執行）"
        );
        let p = h.public();
        assert_eq!(p["state"], PyValue::from("ATTACHED"));
        assert_eq!(p["last_error"], PyValue::from("PROMPT_UNAVAILABLE"), "{name}");
        assert_eq!(p["boot_evidence_wait_remaining_s"], PyValue::Float(60.0));
        assert_eq!(p["reprobe_attempts"], PyValue::from(0), "{name}：不加 attempts");
        assert_eq!(p["next_reprobe_at"], PyValue::Null, "{name}：不排 backoff");
        assert_eq!(resp["error_code"], PyValue::from("SESSION_NOT_READY"));
        assert_eq!(resp["last_error"], PyValue::from("PROMPT_UNAVAILABLE"));
        if name == "recover" {
            assert_eq!(resp["action"], PyValue::from("REPROBE"));
            assert_eq!(resp["recovered"], PyValue::Bool(false));
        }
        assert_eq!(h.core.quiet_for_test().boot_evidence_wait_until, until, "{name}：等待不被消耗");

        // 寬限恰好到期（`now >= until`）：同一入口恢復 probe。
        h.clock.advance(Duration::from_nanos(WP56_GRACE_NS));
        assert_eq!(h.clock.mono_ns(), until);
        let req = if name == "attach" {
            SessionRequest::SessionAttach
        } else {
            recover()
        };
        let (_reply, fx) = h.rpc_deferred(req);
        assert!(start_login(&fx).is_some(), "{name}：寬限到期後恢復 probe");
    }
}

#[test]
fn wp56_self_test_during_wait_returns_autoboot_quiet() {
    // WP5-6 第 4 項（G8，#243）：`origin/main:sw_core/session_manager.py:7816`——READY 且 quiet 或等待有效 →
    // `AUTOBOOT_QUIET`／`wait`，不送 nonce probe；分類優先序不變（PASSTHROUGH 與 ATTACHED 分支在前）。
    // 健康 READY bridge，banner＋prompt 後 quiet 已清、pending 保留（非同步 reboot 後的過渡態）。
    let setup = |h: &mut CoreHarness| {
        h.to_ready();
        h.rx(b"\r\nU-Boot 2024.04\r\n");
        h.rx(b"root@prplOS:~# ");
        assert!(!h.core.quiet_for_test().quiet_active(h.clock.mono_ns()));
        assert_eq!(h.public()["ready_reconfirm_pending"], PyValue::Bool(true));
    };

    // 對照組：同一狀態未進等待時，self-test 送 nonce probe。
    let mut h = CoreHarness::new(PRPL_PROFILE);
    setup(&mut h);
    let (_reply, fx) = h.rpc_deferred(wp56_self_test());
    assert!(wp56_has_self_test_probe(&fx), "未等待時照常送 self-test probe");

    let mut h = CoreHarness::new(PRPL_PROFILE);
    setup(&mut h);
    wp56_begin_wait(&mut h);
    let (reply, fx) = h.rpc_deferred(wp56_self_test());
    assert!(!wp56_has_self_test_probe(&fx), "等待中不得送 self-test probe");
    assert!(!wp56_has_start_op(&fx));
    let r = reply
        .try_recv()
        .expect("零 probe 時同步回覆")
        .expect("ok");
    assert_eq!(r["classification"], PyValue::from("AUTOBOOT_QUIET"));
    assert_eq!(r["recommended_action"], PyValue::from("wait"));
    assert_eq!(
        r.0.keys().cloned().collect::<Vec<_>>(),
        [
            "ok",
            "classification",
            "session",
            "recommended_action",
            "interactive_owner",
            "human_attached",
            "human_active",
            "recovery_mode",
            "command_capable"
        ],
        "與 quiet 分支同一形狀（Python 同一個 dict 字面量）"
    );
    let session = match &r["session"] {
        PyValue::Dict(d) => d.clone(),
        other => panic!("session 不是 object：{other:?}"),
    };
    assert_eq!(session["state"], PyValue::from("READY"));
    assert_eq!(session["boot_quiet_remaining_s"], PyValue::Null, "quiet 已清，分類來自等待");
    assert_eq!(session["boot_evidence_wait_remaining_s"], PyValue::Float(60.0));
    let p = h.public();
    assert_eq!(p["state"], PyValue::from("READY"));
    assert_eq!(p["last_error"], PyValue::Null);
    assert_eq!(p["ready_reconfirm_pending"], PyValue::Bool(true));

    // 寬限恰好到期：恢復 nonce probe。
    h.clock.advance(Duration::from_nanos(WP56_GRACE_NS));
    let (_reply, fx) = h.rpc_deferred(wp56_self_test());
    assert!(wp56_has_self_test_probe(&fx), "寬限到期後恢復 self-test probe");

    // passthrough 反向斷言（SC-35）：READY＋等待仍回 PASSTHROUGH／none，零 probe（不另設 gate）。
    let mut h = CoreHarness::new(WP56_PASSTHROUGH_PROFILE);
    h.to_attached(None);
    assert_eq!(h.core.state(), SessionState::Attached);
    advance_collect(&mut h, 31);
    assert_eq!(h.core.state(), SessionState::Ready, "passthrough 安靜 30 s 升 READY");
    wp56_begin_wait(&mut h);
    let (resp, fx) = h.rpc_full(wp56_self_test());
    let r = resp.expect("ok");
    assert_eq!(r["classification"], PyValue::from("PASSTHROUGH"));
    assert_eq!(r["recommended_action"], PyValue::from("none"));
    assert!(!wp56_has_start_op(&fx));
}

#[test]
fn wp56_unlock_profile_wait_is_not_exempt() {
    // WP5-6 第 4 項（G8，#243）：`origin/main:sw_core/session_manager.py:4746`——console unlock 只豁免 quiet
    // （`_boot_quiet_blocks_probe` `:1888-1890`），`boot_evidence_wait_active()` 另以 `or` 判定、不受豁免。
    // banner 先 note 解除等待、再 arm quiet（`:4560-4564`）：之後只剩 quiet 條件，unlock 恢復豁免。
    use sw_core::session_core::SessionRequest;
    let mut h = CoreHarness::new(WP56_UNLOCK_PROFILE);
    h.set_env("SW_T_UNLOCK", "k");
    h.to_attached(Some("PROMPT_UNAVAILABLE"));

    wp56_begin_wait(&mut h);
    let gen_before = h.core.generations().fsm_gen;
    let (reply, fx) = h.rpc_deferred(SessionRequest::SessionAttach);
    assert!(start_login(&fx).is_none(), "unlock profile 等待中仍不得起 probe");
    assert!(!wp56_has_start_op(&fx));
    reply
        .try_recv()
        .expect("零 probe 時同步回覆")
        .expect("ok");
    assert_eq!(h.core.generations().fsm_gen, gen_before);
    assert_eq!(h.public()["last_error"], PyValue::from("PROMPT_UNAVAILABLE"));

    // banner：等待解除、quiet 重臂；unlock 對 quiet 的豁免恢復 → 手動 attach 起 unlock 登入 probe。
    h.rx(b"\r\nU-Boot 2024.04\r\n");
    let p = h.public();
    assert_eq!(p["boot_evidence_wait_remaining_s"], PyValue::Null, "banner 解除等待");
    assert_eq!(p["boot_quiet_remaining_s"], PyValue::Float(180.0), "banner 重臂 quiet");
    let (_reply, fx) = h.rpc_deferred(SessionRequest::SessionAttach);
    assert!(
        start_login(&fx).is_some(),
        "banner 後只剩 quiet 條件：unlock profile 照原豁免起 probe"
    );
}

/// wp510 第 13 項：DETACHED 逾時類失敗的自動 attach 遇未確認關閉的 by_id。Python `_prepare_reprobe_locked` 已設
/// ATTACHING＋last_error=None 並記 attempt（`origin/main:sw_core/session_manager.py:2248-2252`），`reconcile_readiness`
/// 的 `_spawn_attach` 因 `_draining_by_ids` 早退、不開埠（`:2513-2515`、`:4113-4114`）。
#[test]
fn wp510_review7_detached_reprobe_attach_blocked_by_unconfirmed_close() {
    let mut h = CoreHarness::new(PRPL_PROFILE);
    h.feed(ControlEvent::DeviceAttached {
        by_id: DEVICE_BY_ID.into(),
        real_path: REAL_PATH.into(),
    });
    h.bridge_ok();
    let (epoch, gen) = (h.epoch(), h.core.generations().fsm_gen);
    h.feed(ControlEvent::LoginFinished {
        epoch,
        fsm_gen: gen,
        result: LoginOutcome::Failed("PRPL_PROMPT_TIMEOUT".into()),
        after_rx_seq: None,
    });
    let (stop_epoch, stop) = h.pending_stop();
    h.receipt(stop_epoch, stop).feed();
    assert_eq!(h.public()["state"], PyValue::from("DETACHED"));
    h.unconfirmed.insert(DEVICE_BY_ID);
    let fx = advance_collect(&mut h, 4);
    assert!(
        !fx.iter().any(|e| matches!(e, Effect::OpenBridge { .. })),
        "未確認關閉的 by_id 不得自動開埠"
    );
    assert_eq!(h.public()["state"], PyValue::from("ATTACHING"));
    assert_eq!(h.public()["last_error"], PyValue::Null);
    assert_eq!(h.public()["reprobe_attempts"], PyValue::from(1));
}
