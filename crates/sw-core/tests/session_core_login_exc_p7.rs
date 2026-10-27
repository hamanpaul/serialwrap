//! 審查 fa847c7（grok #1 High、Opus #3／#4）：login run 的「Python 例外」結果依 run 種類收斂。
//!
//! Python 對照：TX 未完整（`TxIncompleteError`，非 `_login_tx_refused`）或 WAL 不健康（`WalUnavailableError`）在
//! - attach（`_attach_by_id`）：穿出 `except Exception` → 關 bridge、`DETACHED`、`ATTACH_FAILED:<型別>`（`:4873-4909`）；
//! - 手動 `attach_session`／`recover_session` 的既有 bridge 再探：穿出 manager → RPC `EXCEPTION`，session 不變（`:4479-4481`）；
//! - 自動重探：`ATTACHED`＋`REPROBE_FAILED:<型別>`＋記 attempt（`:2199-2213`）；
//! - reboot recovery：`RECOVERY_ERROR`＋WARNING、續迴圈（`:5931-5945`）；quiet／新世代撤銷 → `continue`（不記錯）。
//! 偏離：結果不明（`Unknown`）在 Python 為 `TxUnknownError`；`LoginAbort` 未區分，型別名與例外訊息全文待 Task 7
//! `LoginReport` 帶出 TxResult（p7-fix-responses.md 登記）。
#![cfg(feature = "test-fakes")]
mod support;

use std::time::Duration;

use support::*;
use sw_core::login_fsm::{LoginAbort, LoginOutcome};
use sw_core::mailbox::ControlEvent;
use sw_core::op_runner::OpOutcome;
use sw_core::session_core::{Effect, SessionRequest, SessionState};
use sw_core::tx_writer::FenceReject;
use sw_proto::pyjson::PyValue;

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

#[test]
fn attach_wal_unhealthy_refusal_detaches_wal_unavailable_error() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.feed(ControlEvent::DeviceAttached {
        by_id: DEVICE_BY_ID.into(),
        real_path: REAL_PATH.into(),
    });
    h.bridge_ok();
    let (epoch, gen) = (h.epoch(), h.core.generations().fsm_gen);
    finish(
        &mut h,
        epoch,
        gen,
        LoginOutcome::Aborted(LoginAbort::Refused(FenceReject::WalUnhealthy)),
    );
    let (stop_epoch, stop) = h.pending_stop();
    h.receipt(stop_epoch, stop).feed();
    assert_eq!(h.public()["state"], PyValue::from("DETACHED"));
    assert_eq!(
        h.public()["last_error"],
        PyValue::from("ATTACH_FAILED:WalUnavailableError")
    );
}

#[test]
fn manual_reprobe_tx_incomplete_is_rpc_exception_and_session_unchanged() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_attached(Some("SHELL_PROMPT_TIMEOUT"));
    let (reply, fx) = h.rpc_deferred(SessionRequest::SessionAttach);
    let (epoch, gen) = start_login(&fx).expect("既有 bridge 再探");
    finish(
        &mut h,
        epoch,
        gen,
        LoginOutcome::Aborted(LoginAbort::TxIncomplete),
    );
    let resp = reply.try_recv().expect("例外即回覆");
    assert!(
        resp.is_err(),
        "Python 例外穿出 manager → RPC framing EXCEPTION：{resp:?}"
    );
    let p = h.public();
    assert_eq!(p["state"], PyValue::from("ATTACHED"));
    assert_eq!(
        p["last_error"],
        PyValue::from("SHELL_PROMPT_TIMEOUT"),
        "例外穿出前不改 session"
    );
}

#[test]
fn manual_recover_reprobe_raised_is_rpc_exception() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_attached(Some("SHELL_PROMPT_TIMEOUT"));
    let (reply, fx) = h.rpc_deferred(SessionRequest::Recover {
        timeout_s: 10.0,
        force: false,
    });
    let (epoch, gen) = start_login(&fx).expect("recover 的 ATTACHED 再探");
    finish(&mut h, epoch, gen, LoginOutcome::Raised("error".into()));
    assert!(reply.try_recv().expect("回覆").is_err());
    assert_eq!(
        h.public()["last_error"],
        PyValue::from("SHELL_PROMPT_TIMEOUT")
    );
}

#[test]
fn auto_reprobe_tx_incomplete_records_reprobe_failed() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_attached(Some("SHELL_PROMPT_TIMEOUT"));
    let mut fx = Vec::new();
    for _ in 0..4 {
        fx.extend(h.advance(Duration::from_secs(1)));
    }
    let (epoch, gen) = start_login(&fx).expect("自動重探");
    finish(
        &mut h,
        epoch,
        gen,
        LoginOutcome::Aborted(LoginAbort::Refused(FenceReject::WalUnhealthy)),
    );
    let p = h.public();
    assert_eq!(p["state"], PyValue::from("ATTACHED"));
    assert_eq!(
        p["last_error"],
        PyValue::from("REPROBE_FAILED:WalUnavailableError")
    );
    assert_eq!(p["reprobe_attempts"], PyValue::from(1));
}

/// READY → reboot → RECOVERING → quiet 由 prompt 解除 → 下一個 tick 起 reboot recovery 的 ensure_ready run。
fn reboot_recovery_run(h: &mut CoreHarness) -> (u64, u64) {
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
    let mut fx = Vec::new();
    for _ in 0..2 {
        fx.extend(h.advance(Duration::from_secs(1)));
        if start_login(&fx).is_some() {
            break;
        }
    }
    start_login(&fx).expect("reboot recovery 起 ensure_ready")
}

#[test]
fn reboot_recovery_tx_incomplete_records_recovery_error() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    let (epoch, gen) = reboot_recovery_run(&mut h);
    let fx = finish(
        &mut h,
        epoch,
        gen,
        LoginOutcome::Aborted(LoginAbort::TxIncomplete),
    );
    assert_eq!(h.core.state(), SessionState::Recovering, "續迴圈待逾時收尾");
    assert_eq!(h.public()["last_error"], PyValue::from("RECOVERY_ERROR"));
    assert!(
        fx.iter().any(|e| matches!(e, Effect::Warn(m) if m == "reboot-recovery ensure_ready TX 例外（session=t:COM0）")),
        "TX 例外 WARNING"
    );
    // 下一輪（≥1 s 後）仍會再起 ensure_ready（續迴圈）。
    let mut fx = Vec::new();
    for _ in 0..2 {
        fx.extend(h.advance(Duration::from_secs(1)));
    }
    assert!(start_login(&fx).is_some());
}

#[test]
fn reboot_recovery_raised_records_recovery_error() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    let (epoch, gen) = reboot_recovery_run(&mut h);
    let fx = finish(&mut h, epoch, gen, LoginOutcome::Raised("error".into()));
    assert_eq!(h.public()["last_error"], PyValue::from("RECOVERY_ERROR"));
    assert!(fx.iter().any(
        |e| matches!(e, Effect::Warn(m) if m == "reboot-recovery ensure_ready 例外（session=t:COM0），續迴圈待逾時收尾")
    ));
}

#[test]
fn reboot_recovery_quiet_refusal_continues_without_error() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    let (epoch, gen) = reboot_recovery_run(&mut h);
    let before = h.public()["last_error"].clone();
    finish(
        &mut h,
        epoch,
        gen,
        LoginOutcome::Aborted(LoginAbort::Refused(FenceReject::Quiet)),
    );
    assert_eq!(h.core.state(), SessionState::Recovering);
    assert_eq!(
        h.public()["last_error"],
        before,
        "quiet 撤銷 → continue，不記錯"
    );
}
