//! P7 Task 7（08-slice23-login-events.md）：SC-30——會擋 probe 的 quiet arm 當下取消進行中的 login run 並收斂
//! （OD-35，2026-10-28 擁有者裁決維持 SC-30）。Python 基線不取消（attach thread 照常寫終態），此為已核准的
//! Rust-only 行為（`contracts/behavior_changes.yaml` SC-30）。取消訊號經 `RxShared::cancel_login_runs`：run 端
//! 在 StartLogin 後以 `RunCancel::new(&rx)` 建立的旗標於 arm 同一事件內轉真。
#![cfg(feature = "test-fakes")]

mod common;
mod support;

use std::time::Duration;

use support::*;
use sw_core::login_fsm::LoginOutcome;
use sw_core::mailbox::ControlEvent;
use sw_core::op_runner::OpOutcome;
use sw_core::session_core::{Effect, SessionRequest, SessionState};
use sw_core::uart_bridge::RunCancel;
use sw_proto::pyjson::PyValue;

const BANNER: &[u8] = b"\r\nU-Boot 2024.04 (Oct 01 2024)\r\n";
const UNLOCK_PROFILE: &str = "profiles:\n  c:\n    platform: shell\n    prompt_regex: '(?m)^root@t:.*# '\n    console_unlock_env: SW_T_UNLOCK\n    timeout_s: 2.0\ntargets:\n  - {com: COM0, profile: c, act_no: 1, alias: c+1, device_by_id: /s/by-id/usb-A}\n";

fn start_login(effs: &[Effect]) -> Option<(u64, u64)> {
    effs.iter().find_map(|e| match e {
        Effect::StartLogin { epoch, fsm_gen, .. } => Some((*epoch, *fsm_gen)),
        _ => None,
    })
}

fn finish(h: &mut CoreHarness, epoch: u64, fsm_gen: u64, result: LoginOutcome) -> Vec<Effect> {
    h.feed(ControlEvent::LoginFinished {
        epoch,
        fsm_gen,
        result,
        after_rx_seq: None,
    })
}

fn advance_collect(h: &mut CoreHarness, secs: u64) -> Vec<Effect> {
    let mut fx = Vec::new();
    for _ in 0..secs {
        fx.extend(h.advance(Duration::from_secs(1)));
    }
    fx
}

#[test]
fn manual_reprobe_banner_cancels_run_and_late_ready_is_ignored_sc30() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_attached(Some("SHELL_PROMPT_TIMEOUT"));
    let (reply, fx) = h.rpc_deferred(SessionRequest::SessionAttach);
    let (epoch, gen) = start_login(&fx).expect("ATTACHED 的 SessionAttach 起再探 run");
    let cancel = RunCancel::new(&h.rx);
    assert!(!cancel.is_triggered());
    h.rx(BANNER);
    assert!(cancel.is_triggered(), "arm 同一事件內取消 run");
    let r = reply
        .try_recv()
        .expect("等待者於 arm 同一事件回覆")
        .expect("ok");
    assert_eq!(r["error_code"], PyValue::from("SESSION_NOT_READY"));
    assert_eq!(r["last_error"], PyValue::from("PROMPT_UNAVAILABLE"));
    let p = h.public();
    assert_eq!(p["state"], PyValue::from("ATTACHED"));
    assert_eq!(p["last_error"], PyValue::from("PROMPT_UNAVAILABLE"));
    assert_eq!(p["last_error_detail"], PyValue::Null);
    // banner 之前 run 已取得、此時才抵達的 Ready：不得推 READY、不得解除 #162 gate。
    assert!(finish(&mut h, epoch, gen, LoginOutcome::Ready).is_empty());
    let p = h.public();
    assert_eq!(p["state"], PyValue::from("ATTACHED"));
    assert_eq!(p["ready_reconfirm_pending"], PyValue::Bool(true));
}

#[test]
fn attach_login_banner_cancels_run_and_adopts_bridge_sc30() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.attach_device();
    let fx = h.bridge_ok();
    let (epoch, gen) = start_login(&fx).expect("attach 起 login run");
    let cancel = RunCancel::new(&h.rx);
    h.rx(BANNER);
    assert!(cancel.is_triggered());
    assert_eq!(h.core.state(), SessionState::Attached, "不得停在 ATTACHING");
    assert_ne!(h.core.generations().bridge_generation, 0, "bridge 已採用");
    assert!(finish(&mut h, epoch, gen, LoginOutcome::Ready).is_empty());
    assert_eq!(h.core.state(), SessionState::Attached);
}

#[test]
fn ready_reconfirm_probe_banner_keeps_ready_and_gate_sc30() {
    // READY＋pending 的確認 probe（自動重探）進行中再見 banner：取消 run、state 維持 READY、pending 保留
    // （#162 gate 不因 banner 前取得的 Ready 而解除）。
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    h.rx(BANNER);
    assert_eq!(h.public()["ready_reconfirm_pending"], PyValue::Bool(true));
    let fx = advance_collect(&mut h, 190);
    let (epoch, gen) = start_login(&fx).expect("quiet 到期後起 READY 再確認 probe");
    let cancel = RunCancel::new(&h.rx);
    h.rx(BANNER);
    assert!(cancel.is_triggered(), "再確認 probe 被 banner 取消");
    assert!(finish(&mut h, epoch, gen, LoginOutcome::Ready).is_empty());
    let p = h.public();
    assert_eq!(p["state"], PyValue::from("READY"));
    assert_eq!(p["ready_reconfirm_pending"], PyValue::Bool(true), "不得解除 #162 gate");
    assert_ne!(p["boot_quiet_remaining_s"], PyValue::Null);
}

#[test]
fn reboot_recovery_banner_returns_to_wait_loop_sc30() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    let started = h.start_foreground_command("reboot");
    let epoch = h.epoch();
    h.feed(ControlEvent::UartTxCompleted {
        epoch,
        op_id: started.op_id,
        confirmed_bytes: 7,
        unknown: false,
    });
    h.feed(ControlEvent::OpFinished {
        op_id: started.op_id,
        epoch,
        outcome: OpOutcome::RebootExpected,
        after_rx_seq: None,
    });
    assert_eq!(h.core.state(), SessionState::Recovering);
    h.rx(b"\r\nroot@t:/# ");
    let fx = advance_collect(&mut h, 2);
    let (epoch, gen) = start_login(&fx).expect("reboot recovery 起 ensure_ready");
    let cancel = RunCancel::new(&h.rx);
    h.rx(BANNER);
    assert!(cancel.is_triggered(), "reboot recovery 的 run 被取消");
    assert!(finish(&mut h, epoch, gen, LoginOutcome::Ready).is_empty());
    assert_eq!(h.core.state(), SessionState::Recovering, "回等待迴圈，不推 READY");
    // quiet 解除（prompt）後迴圈重新起 run，該 run 的 Ready 才推 READY。
    h.rx(b"\r\nroot@t:/# ");
    let fx = advance_collect(&mut h, 3);
    let (epoch2, gen2) = start_login(&fx).expect("quiet 解除後重新 ensure_ready");
    assert!(gen2 > gen);
    finish(&mut h, epoch2, gen2, LoginOutcome::Ready);
    assert_eq!(h.core.state(), SessionState::Ready);
}

#[test]
fn console_unlock_banner_does_not_cancel_run() {
    // console unlock：`boot_quiet_blocks_probe` 為假——不取消、fence 世代不推進，run 完成照常 READY（SC-30 範圍外）。
    let mut h = CoreHarness::new(UNLOCK_PROFILE);
    h.set_env("SW_T_UNLOCK", "k");
    h.attach_device();
    let fx = h.bridge_ok();
    let (epoch, gen) = start_login(&fx).expect("unlock profile 起 login run");
    let cancel = RunCancel::new(&h.rx);
    h.rx(BANNER);
    assert!(!cancel.is_triggered(), "console unlock 的 arm 不取消 run");
    assert_eq!(h.core.state(), SessionState::Attaching);
    finish(&mut h, epoch, gen, LoginOutcome::Ready);
    assert_eq!(h.core.state(), SessionState::Ready);
}

#[test]
fn rx_cancel_login_runs_wakes_waiter_and_spares_later_runs() {
    // `RxShared::cancel_login_runs`：既存 RunCancel 轉真、等待中的 `wait_for_regex_cancellable` 立即回 Cancelled；
    // 之後新建的 RunCancel（arm 之後才起的 run）不受影響。
    use std::sync::Arc;
    use sw_core::uart_bridge::{RxShared, WaitOutcome};
    let rx = RxShared::new(4096, 0);
    let clock: Arc<dyn sw_pal::traits::Clock> = sw_pal::native().clock;
    let re = sw_proto::pyre::compile("never-matches", Default::default()).expect("regex");
    let old = RunCancel::new(&rx);
    let waiter = {
        let (rx, old, clock) = (rx.clone(), old.clone(), clock.clone());
        std::thread::spawn(move || {
            let t0 = std::time::Instant::now();
            let out = rx.wait_for_regex_cancellable(&re, None, Duration::from_secs(10), &*clock, &old);
            (out, t0.elapsed())
        })
    };
    std::thread::sleep(Duration::from_millis(50));
    rx.cancel_login_runs();
    let (out, elapsed) = waiter.join().unwrap();
    assert_eq!(out, WaitOutcome::Cancelled);
    assert!(elapsed < Duration::from_secs(2), "取消須立即喚醒：{elapsed:?}");
    assert!(old.is_triggered());
    assert!(!RunCancel::new(&rx).is_triggered(), "取消後新建的 run 不受影響");
}
