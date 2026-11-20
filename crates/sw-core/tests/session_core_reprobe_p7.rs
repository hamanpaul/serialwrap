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

// ── OD-81／DF-27（docs/rust-refactor/reviews/rulings/s2.md「OD-81」）：登入後命令逾時的完整重探生命週期。
// Python 基線：attach 登入（`_attach_by_id` 的 ensure_ready）回 `POST_LOGIN_CMD_TIMEOUT`（`origin/main:sw_core/login_fsm.py:174`）
// → 關 bridge、DETACHED；`_is_reprobe_prompt_error`（`origin/main:sw_core/session_manager.py:2112-2136`）不含此碼 → 永不重探。
// 擁有者裁決 Rust 改為沿用既有退避重探（Python 依 OD-75 不改）：drain 收尾後才 DETACHED、tick 依 RX idle／backoff
// 重新開橋（真正開橋才記一次 attempt，2／4／8／15 s 上限）、登入在途與 drain 未完成不重複開橋、最後成功回 READY 並重置進度；
// 期間被拒的 agent 請求不得產生任何 TX。

const DF27_PROFILE: &str = "profiles:\n  b:\n    platform: shell\n    prompt_regex: '(?m)^root@t:.*# '\n    post_login_cmd: sh\n    timeout_s: 2.0\ntargets:\n  - {com: COM0, profile: b, act_no: 1, alias: b+1, device_by_id: /s/by-id/usb-A}\n";
const DF27: &str = "POST_LOGIN_CMD_TIMEOUT";
const SEC: u64 = 1_000_000_000;

fn df27_open_count(effs: &[Effect]) -> usize {
    effs.iter()
        .filter(|e| matches!(e, Effect::OpenBridge { .. }))
        .count()
}

/// 任何 TX 類效果（系統登入、前景／背景 op、human 轉送）。
fn df27_has_tx(effs: &[Effect]) -> bool {
    effs.iter().any(|e| {
        matches!(
            e,
            Effect::StartLogin { .. } | Effect::StartOp { .. } | Effect::HumanTx { .. }
        )
    })
}

/// agent `command.submit`：非 READY 必須同步拒絕且零 TX。
fn df27_assert_submit_rejected(h: &mut CoreHarness, ctx: &str) {
    let (resp, fx) = h.rpc_full(sw_core::session_core::SessionRequest::Submit(agent_submit(
        "echo df27-must-not-tx",
    )));
    let resp = resp.unwrap_or_else(|e| panic!("{ctx}：submit 不應拋例外：{e:?}"));
    assert_eq!(resp["ok"], PyValue::Bool(false), "{ctx}：submit 必須被拒：{resp:?}");
    assert!(!df27_has_tx(&fx), "{ctx}：被拒的 agent 請求不得產生 TX 效果");
}

/// 開橋成功 → attach 登入起跑 → 立即回 `Failed(POST_LOGIN_CMD_TIMEOUT)`；回傳 drain 的 (epoch, stop_id)。
fn df27_open_and_fail(h: &mut CoreHarness, ctx: &str) -> (u64, sw_core::drain::StopId) {
    let fx = h.bridge_ok();
    let (epoch, gen) = start_login(&fx).unwrap_or_else(|| panic!("{ctx}：開橋後應起 attach 登入"));
    let fx = h.feed(ControlEvent::LoginFinished {
        epoch,
        fsm_gen: gen,
        result: LoginOutcome::Failed(DF27.into()),
        after_rx_seq: None,
    });
    assert!(
        fx.iter().any(|e| matches!(e, Effect::BeginDrain { .. })),
        "{ctx}：attach 登入失敗須先 drain（不得直接 DETACHED／READY）"
    );
    assert_ne!(h.core.state(), SessionState::Ready, "{ctx}：失敗不得升 READY");
    assert_ne!(h.core.state(), SessionState::Detached, "{ctx}：drain 未完成前不得 DETACHED");
    assert_eq!(h.public()["last_error"], PyValue::from(DF27), "{ctx}");
    h.pending_stop()
}

/// 推進至第一次 OpenBridge，回傳經過的秒數；`max_s` 內未開橋則 panic。開橋前每一秒都不得有 TX。
fn df27_secs_until_open(h: &mut CoreHarness, max_s: u64, ctx: &str) -> u64 {
    for s in 1..=max_s {
        let fx = h.advance(Duration::from_secs(1));
        assert!(
            !fx.iter().any(|e| matches!(e, Effect::StartLogin { .. } | Effect::StartOp { .. })),
            "{ctx}：DETACHED 等待重探期間不得 TX"
        );
        match df27_open_count(&fx) {
            0 => continue,
            1 => return s,
            n => panic!("{ctx}：同一 tick 開橋 {n} 次"),
        }
    }
    panic!("{ctx}：{max_s} s 內未重新開橋（DF-27：POST_LOGIN_CMD_TIMEOUT 應依退避重探）");
}

#[test]
fn df27_failed_login_drains_then_retries_to_ready() {
    let mut h = CoreHarness::new(DF27_PROFILE);
    h.attach_device();
    let fx = h.bridge_ok();
    let (epoch, gen) = start_login(&fx).expect("首次 attach 起登入");
    // 登入期間的 RX（post_login_cmd 的 echo）：reader 先寫共享視窗再投遞 data lane；RX idle 由此起算。
    let t0 = h.clock.mono_ns();
    h.rx.append(b"sh\r\n", t0, epoch, 9_001);
    h.rx(b"sh\r\n");
    let fx = h.feed(ControlEvent::LoginFinished {
        epoch,
        fsm_gen: gen,
        result: LoginOutcome::Failed(DF27.into()),
        after_rx_seq: None,
    });
    assert!(
        fx.iter().any(|e| matches!(e, Effect::BeginDrain { .. })),
        "attach 登入失敗 → 先 drain（`StopKind::LoginFailed`）"
    );
    assert_ne!(h.core.state(), SessionState::Detached, "stop receipt 前不得 DETACHED");
    assert_ne!(h.core.state(), SessionState::Ready);
    df27_assert_submit_rejected(&mut h, "首次失敗 drain 中");

    // stop receipt 完成 → DETACHED，錯誤碼保留、尚未計任何 attempt。
    let (stop_epoch, stop) = h.pending_stop();
    h.receipt(stop_epoch, stop).feed();
    let p = h.public();
    assert_eq!(p["state"], PyValue::from("DETACHED"));
    assert_eq!(p["last_error"], PyValue::from(DF27));
    assert_eq!(p["reprobe_attempts"], PyValue::from(0));
    assert_eq!(p["next_reprobe_at"], PyValue::Null);
    df27_assert_submit_rejected(&mut h, "DETACHED");

    // 首次重試：依既有 RX idle（≥3 s，`REPROBE_RX_IDLE_S`）語意，於最後一筆 RX 後第一個合格 tick 開橋，不多加固定等待。
    let waited = df27_secs_until_open(&mut h, 30, "首次重試");
    assert_eq!(h.clock.mono_ns(), t0 + 3 * SEC, "首次重試應落在 RX idle 3 s 的第一個 tick（等了 {waited} s）");
    let p = h.public();
    assert_eq!(p["state"], PyValue::from("ATTACHING"));
    assert_eq!(p["last_error"], PyValue::Null, "重新 attach 時清 last_error（`_prepare_reprobe_locked`）");
    assert_eq!(p["reprobe_attempts"], PyValue::from(1), "真正開橋一次才記一次 attempt");
    df27_assert_submit_rejected(&mut h, "開橋在途");

    // 開橋在途：tick 不得重複開橋。
    let fx = advance_collect(&mut h, 5);
    assert_eq!(df27_open_count(&fx), 0, "開橋在途不得重複開橋");
    let a1 = h.clock.mono_ns() - 5 * SEC;

    // 第 1 次重試失敗，drain 刻意延到退避期限（a1+2 s）之後：drain 未完成前不得開橋。
    let (stop_epoch, stop) = df27_open_and_fail(&mut h, "第 1 次重試");
    let fx = advance_collect(&mut h, 2);
    assert_eq!(df27_open_count(&fx), 0, "drain 未完成不得重新開橋");
    assert!(h.clock.mono_ns() >= a1 + 2 * SEC);
    df27_assert_submit_rejected(&mut h, "第 1 次重試 drain 中");
    h.receipt(stop_epoch, stop).feed();
    assert_eq!(h.public()["state"], PyValue::from("DETACHED"));
    assert_eq!(h.public()["reprobe_attempts"], PyValue::from(1), "drain 收尾不計 attempt");
    // 期限已過且 RX 早已閒置 → 下一個 tick 開橋。
    assert_eq!(df27_secs_until_open(&mut h, 30, "第 2 次重試"), 1);
    assert_eq!(h.public()["reprobe_attempts"], PyValue::from(2));

    // 第 2、3、4 次重試皆立即失敗並即時收尾：開橋間隔依序為 backoff 4／8／15 s（2 s 一段已於上方以期限驗證）。
    for (attempt, gap) in [(3i64, 4u64), (4, 8), (5, 15)] {
        let ctx = format!("第 {attempt} 次重試");
        let (stop_epoch, stop) = df27_open_and_fail(&mut h, &ctx);
        h.receipt(stop_epoch, stop).feed();
        assert_eq!(h.public()["state"], PyValue::from("DETACHED"), "{ctx}");
        assert_eq!(
            df27_secs_until_open(&mut h, 30, &ctx),
            gap,
            "{ctx}：開橋間隔應為退避 {gap} s"
        );
        assert_eq!(h.public()["reprobe_attempts"], PyValue::from(attempt), "{ctx}");
    }

    // 最後一次：登入在途期間 tick 不得重開橋、不得起第二個登入；之後成功 → READY 並重置進度。
    let fx = h.bridge_ok();
    let (epoch, gen) = start_login(&fx).expect("最後一次開橋起登入");
    let fx = advance_collect(&mut h, 20);
    assert_eq!(df27_open_count(&fx), 0, "登入在途不得重複開橋");
    assert!(start_login(&fx).is_none(), "登入在途不得另起登入");
    df27_assert_submit_rejected(&mut h, "登入在途");
    h.feed(ControlEvent::LoginFinished {
        epoch,
        fsm_gen: gen,
        result: LoginOutcome::Ready,
        after_rx_seq: None,
    });
    let p = h.public();
    assert_eq!(p["state"], PyValue::from("READY"));
    assert_eq!(p["last_error"], PyValue::Null);
    assert_eq!(p["reprobe_attempts"], PyValue::from(0), "READY 後重置 attempts");
    assert_eq!(p["next_reprobe_at"], PyValue::Null, "READY 後清 backoff");
    assert_eq!(p["reprobe_exhausted"], PyValue::Bool(false));
}

#[test]
fn df27_attached_post_login_timeout_reprobes_existing_bridge_to_ready() {
    // DF-27 的 ATTACHED 路徑：既有 bridge 自動重探（`_probe_existing_bridge`）的登入回 `Failed(POST_LOGIN_CMD_TIMEOUT)`
    // → 停 ATTACHED（不 drain）、`_finish_probe_reprobe` 記一次 attempt；之後依 backoff（2 s）與 RX idle 再排 probe，成功回 READY。
    // Python 基線此時 `_is_reprobe_prompt_error("ATTACHED", "POST_LOGIN_CMD_TIMEOUT")` 為 False，不再重探。
    let mut h = CoreHarness::new(DF27_PROFILE);
    h.to_attached(Some("PROMPT_UNAVAILABLE"));
    let fx = advance_collect(&mut h, 4);
    let (epoch, gen) = start_login(&fx).expect("ATTACHED＋PROMPT_UNAVAILABLE → 自動重探");
    let fx = h.feed(ControlEvent::LoginFinished {
        epoch,
        fsm_gen: gen,
        result: LoginOutcome::Failed(DF27.into()),
        after_rx_seq: None,
    });
    assert!(
        !fx.iter().any(|e| matches!(e, Effect::BeginDrain { .. })),
        "既有 bridge 重探失敗停 ATTACHED、不 drain"
    );
    let p = h.public();
    assert_eq!(p["state"], PyValue::from("ATTACHED"));
    assert_eq!(p["last_error"], PyValue::from(DF27));
    assert_eq!(p["reprobe_attempts"], PyValue::from(1));
    df27_assert_submit_rejected(&mut h, "ATTACHED＋DF-27");
    // backoff(0)=2 s：1 s 後不排、2 s 時排下一次 probe（同一 bridge，不重新開橋）。
    let fx = advance_collect(&mut h, 1);
    assert!(start_login(&fx).is_none(), "未到退避期限不得 probe");
    let fx = advance_collect(&mut h, 1);
    assert_eq!(df27_open_count(&fx), 0, "ATTACHED 重探沿用既有 bridge");
    let (epoch, gen) = start_login(&fx).expect("DF-27：ATTACHED＋POST_LOGIN_CMD_TIMEOUT 應依退避重探");
    h.feed(ControlEvent::LoginFinished {
        epoch,
        fsm_gen: gen,
        result: LoginOutcome::Ready,
        after_rx_seq: None,
    });
    let p = h.public();
    assert_eq!(p["state"], PyValue::from("READY"));
    assert_eq!(p["last_error"], PyValue::Null);
    assert_eq!(p["reprobe_attempts"], PyValue::from(0));
    assert_eq!(p["next_reprobe_at"], PyValue::Null);
}
