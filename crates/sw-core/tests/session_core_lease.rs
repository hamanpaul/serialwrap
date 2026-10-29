//! P6 Task 7：interactive lease 差量（human lease、soft preempt、stash、tombstone、ATTACHED 救援、self-heal）。
//!
//! 06 `CoreHarness`＋FakeClock；`h.wire()` 以 `RPC_WIRE` 比對 key 順序；open／status／close 用 Task 1 的 helper。
//! 凡期望為 `ok:false` 的列另斷言 `h.tx_effect_count()` 不變；open 類拒絕列帶 `command:"help"` 斷言沒有 StartOp。
#![cfg(feature = "test-fakes")]
mod support;

use std::time::Duration;

use support::*;
use sw_core::drain::StopKind;
use sw_core::human_console::ConsoleTransportKind;
use sw_core::login_fsm::LoginOutcome;
use sw_core::mailbox::ControlEvent;
use sw_core::op_runner::{OpOutcome, OpSpec};
use sw_core::session_core::*;
use sw_core::tx_writer::{FenceReject, TxClass};
use sw_pal::traits::{Clock, TxOutcome};
use sw_proto::pydict;
use sw_proto::pyjson::{PyDict, PyValue};

const BOOT: &str = "profiles:\n  b:\n    platform: shell\n    prompt_regex: '(?m)^root@t:.*# '\n    login_regex: 'login: ?$'\n    password_regex: 'Password: ?$'\n    bootloader_prompts: ['^=> $']\n    timeout_s: 2.0\ntargets:\n  - {com: COM0, profile: b, act_no: 1, alias: b+1, device_by_id: /s/by-id/usb-A}\n";
const BOOT_NO_PROMPTS: &str = "profiles:\n  n:\n    platform: shell\n    prompt_regex: '(?m)^root@t:.*# '\n    login_regex: 'login: ?$'\n    password_regex: 'Password: ?$'\n    bootloader_prompts: []\n    timeout_s: 2.0\ntargets:\n  - {com: COM0, profile: n, act_no: 1, alias: n+1, device_by_id: /s/by-id/usb-A}\n";

/// `pyjson::dumps(RPC_WIRE)`（同 `CoreHarness::wire`；自由函式以免與 `&mut h` 借用衝突）。
fn wire(d: &PyDict) -> String {
    sw_proto::pyjson::dumps_dict(d, &sw_proto::pyjson::RPC_WIRE).trim_end_matches('\n').to_string()
}

fn sv(v: &PyValue) -> String {
    v.as_str().expect("字串").to_string()
}

fn iid(d: &PyDict) -> String {
    sv(d.get("interactive_id").expect("interactive_id"))
}

fn start_ops(effects: &[Effect]) -> Vec<&OpSpec> {
    effects
        .iter()
        .filter_map(|e| match e {
            Effect::StartOp { spec, .. } => Some(spec),
            _ => None,
        })
        .collect()
}

fn human_txs(effects: &[Effect]) -> Vec<(TxClass, String, Vec<u8>)> {
    effects
        .iter()
        .filter_map(|e| match e {
            Effect::HumanTx { permit, payload, .. } => Some((permit.class, permit.source.clone(), payload.clone())),
            _ => None,
        })
        .collect()
}

/// 比照 reader：先寫入 RX 視窗（`RxShared::append`）再送 `UartRxChunk`。
fn feed_rx(h: &mut CoreHarness, bytes: &[u8]) -> Vec<Effect> {
    let epoch = h.epoch();
    let now = h.clock.mono_ns();
    h.rx.append(bytes, now, epoch, 0);
    h.rx(bytes)
}

fn ready() -> CoreHarness {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    h
}

fn attached(profile: &str) -> CoreHarness {
    let mut h = CoreHarness::new(profile);
    h.to_attached(None);
    h
}

/// 送 RPC，取出同批的 StartOp grant（若有）。
fn rpc_with_op(h: &mut CoreHarness, req: SessionRequest) -> (crossbeam_channel::Receiver<Result<PyDict, sw_proto::pycompat::PyErr>>, Option<OpGrant>, Vec<Effect>) {
    let (rx, fx) = h.rpc_deferred(req);
    let grant = fx.iter().find_map(|e| match e {
        Effect::StartOp { grant, .. } => Some(grant.clone()),
        _ => None,
    });
    (rx, grant, fx)
}

fn finish_op(h: &mut CoreHarness, grant: &OpGrant, outcome: OpOutcome) -> Vec<Effect> {
    let epoch = h.epoch();
    h.feed(ControlEvent::OpFinished { op_id: grant.op_id, epoch, outcome, after_rx_seq: None })
}

fn send_ok(h: &mut CoreHarness, id: &str, data: &str, enc: &str) -> (PyDict, OpSpec) {
    let (rx, grant, fx) = rpc_with_op(h, send_req(id, data, enc));
    let spec = fx
        .into_iter()
        .find_map(|e| match e {
            Effect::StartOp { spec, .. } => Some(spec),
            _ => None,
        })
        .expect("StartOp(InteractiveSend)");
    let n = match &spec {
        OpSpec::InteractiveSend { payload, .. } => payload.len(),
        _ => panic!("非 InteractiveSend"),
    };
    finish_op(h, &grant.unwrap(), OpOutcome::Sent { bytes: n });
    (rx.try_recv().unwrap().unwrap(), spec)
}

fn expired_wire(id: &str) -> String {
    format!(r#"{{"ok":false,"error_code":"INTERACTIVE_EXPIRED","interactive_id":"{id}"}}"#)
}

/// soft preempt 後回傳 (agent lease id)。
fn soft_preempted(h: &mut CoreHarness) -> String {
    h.attach_pty_console("c1", true);
    let r = h.rpc(open_req("agent", 60.0, "", false)).unwrap();
    assert_eq!(r.get("soft_preempted"), Some(&true.into()));
    iid(&r)
}

// ---------------------------------------------------------------- 計畫內示例

#[test]
fn soft_preempt_idle_human_then_close_restores_and_replays() {
    // #53
    let mut h = ready();
    let c1 = h.attach_pty_console("c1", true).get("client_id").unwrap().clone();
    let r = h.rpc(open_req("agent", 60.0, "", false)).unwrap();
    assert_eq!(r.get("soft_preempted"), Some(&true.into()));
    assert_eq!(r.get("recovery_mode"), Some(&false.into()));
    h.input("c1", b"Q"); // 進 deferred
    assert!(h.human_tx().is_empty());
    let id = iid(&r);
    h.rpc(close_req(&id)).unwrap();
    assert_eq!(h.human_tx(), vec![(TxClass::HumanReplay, "human:c1".to_string(), b"Q".to_vec())]);
    assert_eq!(c1, PyValue::from("c1"));
}

#[test]
fn tick_expiry_restores_human_and_tombstone_is_repeatable() {
    // SC-13（P1 T12 規則 1～2）
    let mut h = ready();
    h.attach_pty_console("c1", true);
    let id = iid(&h.rpc(open_req("agent", 1.0, "", false)).unwrap());
    h.input("c1", b"W");
    h.advance_s(2.0); // 投遞 06 的 ControlEvent::Tick
    assert_eq!(h.human_tx(), vec![(TxClass::HumanReplay, "human:c1".into(), b"W".to_vec())]);
    let expired = expired_wire(&id);
    for req in [status_req(&id, 2048), status_req(&id, 2048), send_req(&id, "x", "plain"), close_req(&id), close_req(&id)] {
        assert_eq!(wire(&h.rpc(req).unwrap()), expired, "tombstone 可重複讀且不移除");
    }
}

#[test]
fn tick_uses_current_deadline_after_touch() {
    let mut h = ready();
    let id = iid(&h.rpc(open_req("agent", 2.0, "", false)).unwrap());
    h.advance_s(1.5);
    h.rpc(status_req(&id, 2048)).unwrap(); // touch
    h.advance_s(1.0); // 距 touch 1.0 s，Tick 不得到期
    assert_eq!(h.rpc(status_req(&id, 2048)).unwrap().get("ok"), Some(&true.into()));
}

#[test]
fn lease_index_insert_precedes_reply() {
    let mut h = ready();
    let mark = h.effect_kinds().len();
    h.rpc(open_req("agent", 60.0, "", false)).unwrap();
    let kinds = h.effect_kinds()[mark..].to_vec();
    let li = kinds.iter().position(|k| *k == "LeaseIndex").expect("LeaseIndex");
    let rp = kinds.iter().position(|k| *k == "Reply").expect("Reply");
    assert!(li < rp);
    assert!(matches!(h.lease_index_ops().last(), Some(LeaseIndexOp::Insert { .. })));
}

// ---------------------------------------------------------------- open

#[test]
fn open_twice_busy() {
    let mut h = ready();
    let id0 = iid(&h.rpc(open_req("agent", 60.0, "", false)).unwrap());
    let before = h.tx_effect_count();
    let r = h.rpc(open_req("agent", 60.0, "help", false)).unwrap();
    assert_eq!(
        wire(&r),
        format!(r#"{{"ok":false,"error_code":"SESSION_INTERACTIVE_BUSY","interactive_session_id":"{id0}"}}"#)
    );
    assert_eq!(h.tx_effect_count(), before);
}

#[test]
fn open_active_human_busy() {
    let mut h = ready();
    let r = h.attach_pty_console("c1", true);
    let hid = sv(r.get("interactive_session_id").unwrap());
    h.input("c1", b"a"); // raw → last_human_input_at
    let before = h.tx_effect_count();
    let r = h.rpc(open_req("agent", 60.0, "help", false)).unwrap();
    assert_eq!(
        wire(&r),
        format!(r#"{{"ok":false,"error_code":"SESSION_INTERACTIVE_BUSY","interactive_session_id":"{hid}"}}"#)
    );
    assert_eq!(h.tx_effect_count(), before);
}

#[test]
fn open_human_owner_never_preempts() {
    let mut h = ready();
    h.attach_pty_console("c1", true);
    let before = h.tx_effect_count();
    let r = h.rpc(open_req("human:x", 60.0, "help", false)).unwrap();
    assert_eq!(r.get("error_code"), Some(&"SESSION_INTERACTIVE_BUSY".into()));
    assert_eq!(h.tx_effect_count(), before);
}

#[test]
fn open_ready_fg_busy() {
    let mut h = ready();
    h.start_foreground_command("sleep 9");
    let before = h.tx_effect_count();
    let r = h.rpc(open_req("agent", 60.0, "help", false)).unwrap();
    assert_eq!(wire(&r), r#"{"ok":false,"error_code":"SESSION_BUSY","selector":"COM0"}"#);
    assert_eq!(h.tx_effect_count(), before);
}

#[test]
fn open_not_ready_without_allow() {
    let mut h = attached(BOOT);
    let r = h.rpc(open_req("agent", 60.0, "help", false)).unwrap();
    assert_eq!(wire(&r), r#"{"ok":false,"error_code":"SESSION_NOT_READY","selector":"COM0"}"#);
}

#[test]
fn open_state_matrix() {
    for (state, code) in [
        (SessionState::Recovering, "SESSION_RECOVERING"),
        (SessionState::Flashing, "FLASHING_BUSY"),
        (SessionState::Attaching, "SESSION_NOT_READY"),
        (SessionState::Detached, "SESSION_NOT_READY"),
        (SessionState::Released, "SESSION_NOT_READY"),
    ] {
        let mut h = ready();
        h.force_state(state);
        let before = h.tx_effect_count();
        let r = h.rpc(open_req("agent", 60.0, "help", true)).unwrap();
        assert_eq!(wire(&r), format!(r#"{{"ok":false,"error_code":"{code}","selector":"COM0"}}"#), "{state:?}");
        assert_eq!(h.tx_effect_count(), before);
    }
}

#[test]
fn attached_unhealthy_not_ready() {
    for case in ["running", "serial_alive", "vtty_alive"] {
        let mut h = attached(BOOT);
        feed_rx(&mut h, b"\r\n=> ");
        h.set_bridge_health(case != "running", case != "serial_alive", case != "vtty_alive");
        let r = h.rpc(open_req("agent", 60.0, "", true)).unwrap();
        assert_eq!(wire(&r), r#"{"ok":false,"error_code":"SESSION_NOT_READY","selector":"COM0"}"#, "{case}");
    }
}

#[test]
fn attached_bootloader_prompt() {
    let mut h = attached(BOOT);
    feed_rx(&mut h, b"U-Boot 2020\r\n=> ");
    let r = h.rpc(open_req("agent", 60.0, "", true)).unwrap();
    let keys: Vec<&str> = r.0.keys().map(String::as_str).collect();
    assert_eq!(keys, vec!["ok", "interactive_id", "session", "recovery_mode"]);
    assert_eq!(r.get("recovery_mode"), Some(&true.into()));
}

#[test]
fn attached_banner_boot_interrupt() {
    let mut h = attached(BOOT);
    feed_rx(&mut h, b"Hit any key to stop autoboot:  3");
    let r = h.rpc(open_req("agent", 60.0, "", true)).unwrap();
    assert_eq!(r.get("boot_interrupt"), Some(&true.into()));
    assert!(r.get("login_required").is_none());
}

#[test]
fn attached_login_required() {
    let mut h = attached(BOOT);
    feed_rx(&mut h, b"\x1b[1m(none) login:\x1b[0m ");
    let r = h.rpc(open_req("agent", 60.0, "", true)).unwrap();
    assert_eq!(r.get("login_required"), Some(&true.into()));
    assert!(r.get("boot_interrupt").is_none());
}

#[test]
fn attached_bootloader_priority_over_login() {
    let mut h = attached(BOOT);
    feed_rx(&mut h, b"login: \r\n=> ");
    let r = h.rpc(open_req("agent", 60.0, "", true)).unwrap();
    assert_eq!(r.get("ok"), Some(&true.into()));
    assert!(r.get("login_required").is_none());
}

#[test]
fn attached_banner_priority_over_login() {
    let mut h = attached(BOOT);
    feed_rx(&mut h, b"Hit any key to stop autoboot:  1\r\nlogin: ");
    let r = h.rpc(open_req("agent", 60.0, "", true)).unwrap();
    assert_eq!(r.get("boot_interrupt"), Some(&true.into()));
    assert!(r.get("login_required").is_none());
}

#[test]
fn attached_not_bootloader() {
    let mut h = attached(BOOT);
    feed_rx(&mut h, b"random text");
    let before = h.tx_effect_count();
    let r = h.rpc(open_req("agent", 60.0, "help", true)).unwrap();
    assert_eq!(
        wire(&r),
        r#"{"ok":false,"error_code":"SESSION_NOT_READY","selector":"COM0","error_detail":"NOT_BOOTLOADER"}"#
    );
    assert_eq!(h.tx_effect_count(), before);
}

#[test]
fn attached_fallback_prompts_not_used() {
    let mut h = attached(BOOT_NO_PROMPTS);
    feed_rx(&mut h, b"\r\n=> ");
    let r = h.rpc(open_req("agent", 60.0, "", true)).unwrap();
    assert_eq!(r.get("error_detail"), Some(&"NOT_BOOTLOADER".into()));
}

#[test]
fn recovery_lease_timeout_clamped() {
    let mut h = attached(BOOT);
    feed_rx(&mut h, b"\r\n=> ");
    h.rpc(open_req("agent", 500.0, "", true)).unwrap();
    assert_eq!(h.core.active_lease().unwrap().timeout_s, 120.0);
}

#[test]
fn recovery_small_timeout_passthrough() {
    let mut h = attached(BOOT);
    feed_rx(&mut h, b"\r\n=> ");
    h.rpc(open_req("agent", 30.0, "", true)).unwrap();
    assert_eq!(h.core.active_lease().unwrap().timeout_s, 30.0);
}

#[test]
fn ready_allow_attached_no_clamp() {
    let mut h = ready();
    let r = h.rpc(open_req("agent", 500.0, "", true)).unwrap();
    assert_eq!(r.get("recovery_mode"), Some(&false.into()));
    assert_eq!(h.core.active_lease().unwrap().timeout_s, 500.0);
}

#[test]
fn attached_stashes_human_lease() {
    let mut h = attached(BOOT);
    let a = h.attach_pty_console("c1", true);
    let hid = sv(a.get("interactive_session_id").unwrap());
    feed_rx(&mut h, b"Hit any key to stop autoboot:  3");
    let r = h.rpc(open_req("agent", 60.0, "", true)).unwrap();
    assert_eq!(r.get("ok"), Some(&true.into()));
    let lease = h.core.active_lease().unwrap();
    assert!(lease.suspended_human && lease.recovery_mode);
    assert_eq!(h.core.stashed_human_lease().map(|l| l.interactive_id.clone()), Some(hid));
    assert_eq!(h.core.human_console().ownership.suspend_depth, 1);
}

#[test]
fn attached_existing_agent_busy() {
    let mut h = attached(BOOT);
    feed_rx(&mut h, b"\r\n=> ");
    let id0 = iid(&h.rpc(open_req("agent", 60.0, "", true)).unwrap());
    let r = h.rpc(open_req("agent:b", 60.0, "", true)).unwrap();
    assert_eq!(
        wire(&r),
        format!(r#"{{"ok":false,"error_code":"SESSION_INTERACTIVE_BUSY","interactive_session_id":"{id0}"}}"#)
    );
}

#[test]
fn open_command_sent_after_open() {
    let mut h = ready();
    let (rx, grant, fx) = rpc_with_op(&mut h, open_req("agent", 60.0, "help", false));
    assert!(rx.try_recv().is_err(), "回覆等 TX 結算");
    let li = fx.iter().position(|e| matches!(e, Effect::LeaseIndex(LeaseIndexOp::Insert { .. })));
    let so = fx.iter().position(|e| matches!(e, Effect::StartOp { .. })).expect("StartOp");
    assert!(li.is_some_and(|li| li < so), "lease 建立後才送命令");
    assert!(matches!(
        start_ops(&fx)[0],
        OpSpec::InteractiveOpenCmd { command, owner } if command == "help" && owner == "agent"
    ));
    finish_op(&mut h, &grant.unwrap(), OpOutcome::Sent { bytes: 5 });
    assert_eq!(rx.try_recv().unwrap().unwrap().get("ok"), Some(&true.into()));
}

#[test]
fn open_with_command_reply_waits_tx_and_expired_maps() {
    // parity-amendments #35（B:D-71）
    let mut h = ready();
    let (rx, grant, _) = rpc_with_op(&mut h, open_req("agent", 60.0, "help", false));
    assert!(rx.try_recv().is_err());
    finish_op(&mut h, &grant.unwrap(), OpOutcome::SendRefused(FenceReject::OwnerMismatch));
    let r = rx.try_recv().unwrap().unwrap();
    assert_eq!(wire(&r), r#"{"ok":false,"error_code":"INTERACTIVE_EXPIRED"}"#);
}

// ---------------------------------------------------------------- send

#[test]
fn send_plain() {
    let mut h = ready();
    let id = iid(&h.rpc(open_req("agent", 60.0, "", false)).unwrap());
    let (r, spec) = send_ok(&mut h, &id, "ab", "plain");
    assert_eq!(wire(&r), format!(r#"{{"ok":true,"interactive_id":"{id}","bytes":2}}"#));
    assert!(matches!(spec, OpSpec::InteractiveSend { payload, .. } if payload == b"ab"));
}

#[test]
fn send_key_ctrl_c() {
    let mut h = ready();
    let id = iid(&h.rpc(open_req("agent", 60.0, "", false)).unwrap());
    let (r, spec) = send_ok(&mut h, &id, " CTRL-C ", "key");
    assert_eq!(r.get("bytes"), Some(&PyValue::from(1)));
    assert!(matches!(spec, OpSpec::InteractiveSend { payload, .. } if payload == b"\x03"));
}

#[test]
fn send_base64() {
    let mut h = ready();
    let id = iid(&h.rpc(open_req("agent", 60.0, "", false)).unwrap());
    let (r, spec) = send_ok(&mut h, &id, "aGk=", "base64");
    assert_eq!(r.get("bytes"), Some(&PyValue::from(2)));
    assert!(matches!(spec, OpSpec::InteractiveSend { payload, .. } if payload == b"hi"));
}

#[test]
fn send_invalid_encoding_is_pyerr() {
    let mut h = ready();
    let id = iid(&h.rpc(open_req("agent", 60.0, "", false)).unwrap());
    let before = h.tx_effect_count();
    let e = h.rpc(send_req(&id, "x", "hex")).unwrap_err();
    assert!(e.message.contains("INVALID_INTERACTIVE_ENCODING"), "{}", e.message);
    assert_eq!(h.tx_effect_count(), before);
}

#[test]
fn send_checks_order() {
    // 無 lease
    let mut h = ready();
    let r = h.rpc(send_req("nope", "x", "plain")).unwrap();
    assert_eq!(wire(&r), r#"{"ok":false,"error_code":"INTERACTIVE_NOT_FOUND","interactive_id":"nope"}"#);
    // 過期（lazy，Tick 前）
    let id = iid(&h.rpc(open_req("agent", 1.0, "", false)).unwrap());
    h.clock.advance(Duration::from_millis(1001));
    let before = h.tx_effect_count();
    assert_eq!(wire(&h.rpc(send_req(&id, "x", "plain")).unwrap()), expired_wire(&id));
    assert_eq!(h.tx_effect_count(), before);
    // FLASHING
    let mut h = ready();
    let id = iid(&h.rpc(open_req("agent", 60.0, "", false)).unwrap());
    h.force_state(SessionState::Flashing);
    let r = h.rpc(send_req(&id, "x", "plain")).unwrap();
    assert_eq!(wire(&r), format!(r#"{{"ok":false,"error_code":"FLASHING_BUSY","interactive_id":"{id}"}}"#));
    // fg busy
    let mut h = ready();
    let id = iid(&h.rpc(open_req("agent", 60.0, "", false)).unwrap());
    let (_rx, _g, _) = rpc_with_op(&mut h, send_req(&id, "a", "plain")); // 第一筆在途
    let before = h.tx_effect_count();
    let r = h.rpc(send_req(&id, "x", "plain")).unwrap();
    assert_eq!(wire(&r), format!(r#"{{"ok":false,"error_code":"SESSION_BUSY","interactive_id":"{id}"}}"#));
    assert_eq!(h.tx_effect_count(), before);
}

#[test]
fn send_refused_at_submit_maps_codes() {
    // parity-amendments #36（B:D-74）：提交點拒絕的映射
    for (reject, code) in [
        (FenceReject::OwnerMismatch, "INTERACTIVE_EXPIRED"),
        (FenceReject::Quiet, "AUTOBOOT_QUIET"),
        (FenceReject::Frozen, "SESSION_BUSY"),
        (FenceReject::Cancelled, "SESSION_BUSY"),
        (FenceReject::StaleEpoch, "SESSION_BUSY"),
        (FenceReject::Flash, "SESSION_BUSY"),
    ] {
        let mut h = ready();
        let id = iid(&h.rpc(open_req("agent", 60.0, "", false)).unwrap());
        let (rx, grant, _) = rpc_with_op(&mut h, send_req(&id, "a", "plain"));
        finish_op(&mut h, &grant.unwrap(), OpOutcome::SendRefused(reject));
        let r = rx.try_recv().unwrap().unwrap();
        assert_eq!(wire(&r), format!(r#"{{"ok":false,"error_code":"{code}","interactive_id":"{id}"}}"#), "{reject:?}");
    }
}

#[test]
fn send_invalid_encoding_after_busy_check() {
    let mut h = ready();
    let id = iid(&h.rpc(open_req("agent", 60.0, "", false)).unwrap());
    let (_rx, _g, _) = rpc_with_op(&mut h, send_req(&id, "a", "plain"));
    let r = h.rpc(send_req(&id, "x", "hex")).unwrap();
    assert_eq!(r.get("error_code"), Some(&"SESSION_BUSY".into()));
}

#[test]
fn send_allowed_in_attached() {
    let mut h = attached(BOOT);
    feed_rx(&mut h, b"\r\n=> ");
    let id = iid(&h.rpc(open_req("agent", 60.0, "", true)).unwrap());
    let (r, _) = send_ok(&mut h, &id, "ab", "plain");
    assert_eq!(r.get("ok"), Some(&true.into()));
}

// ---------------------------------------------------------------- status／close

#[test]
fn status_shape_and_touch() {
    let mut h = ready();
    let id = iid(&h.rpc(open_req("agent", 60.0, "", false)).unwrap());
    feed_rx(&mut h, b"0123456789abcdef");
    h.clock.advance(Duration::from_secs(5));
    let r = h.rpc(status_req(&id, 10)).unwrap();
    let keys: Vec<&str> = r.0.keys().map(String::as_str).collect();
    assert_eq!(keys, vec!["ok", "interactive_id", "owner", "status", "recovery_mode", "screen", "session"]);
    assert_eq!(r.get("screen"), Some(&"6789abcdef".into()));
    assert_eq!(h.core.active_lease().unwrap().last_activity, h.clock.mono_ns(), "status 會 touch");
}

#[test]
fn status_screen_chars_negative() {
    let mut h = ready();
    let id = iid(&h.rpc(open_req("agent", 60.0, "", false)).unwrap());
    feed_rx(&mut h, b"abcdef");
    let r = h.rpc(status_req(&id, -3)).unwrap();
    assert_eq!(r.get("screen"), Some(&"def".into()));
}

#[test]
fn status_expired_closes_restores() {
    let mut h = attached(BOOT);
    h.attach_pty_console("c1", true);
    feed_rx(&mut h, b"Hit any key to stop autoboot:  3");
    let id = iid(&h.rpc(open_req("agent", 1.0, "", true)).unwrap());
    h.input("c1", b"k");
    h.clock.advance(Duration::from_millis(1500));
    assert_eq!(wire(&h.rpc(status_req(&id, 10)).unwrap()), expired_wire(&id));
    assert!(h.core.active_lease().is_some_and(|l| l.owner == "human:c1"), "stash 還原");
    assert_eq!(h.human_tx(), vec![(TxClass::HumanReplay, "human:c1".to_string(), b"k".to_vec())]);
}

#[test]
fn close_unknown() {
    let mut h = ready();
    let r = h.rpc(close_req("zz")).unwrap();
    assert_eq!(wire(&r), r#"{"ok":false,"error_code":"INTERACTIVE_NOT_FOUND","interactive_id":"zz"}"#);
}

#[test]
fn tombstone_fifo_16() {
    let mut h = ready();
    let mut ids = Vec::new();
    for _ in 0..17 {
        ids.push(iid(&h.rpc(open_req("agent", 1.0, "", false)).unwrap()));
        h.advance_s(2.0);
    }
    assert!(h.lease_index_ops().contains(&LeaseIndexOp::Remove { interactive_id: ids[0].clone() }));
    let r = h.rpc(status_req(&ids[0], 10)).unwrap();
    assert_eq!(r.get("error_code"), Some(&"INTERACTIVE_NOT_FOUND".into()));
    assert_eq!(wire(&h.rpc(status_req(&ids[16], 10)).unwrap()), expired_wire(&ids[16]));
}

#[test]
fn lazy_expiry_keeps_tombstone_expired() {
    // parity-amendments #54
    let mut h = ready();
    let id = iid(&h.rpc(open_req("agent", 1.0, "", false)).unwrap());
    h.clock.advance(Duration::from_millis(1001)); // Tick 前
    assert_eq!(wire(&h.rpc(send_req(&id, "x", "plain")).unwrap()), expired_wire(&id));
    for req in [send_req(&id, "x", "plain"), status_req(&id, 10), close_req(&id)] {
        assert_eq!(wire(&h.rpc(req).unwrap()), expired_wire(&id));
    }
    for _ in 0..16 {
        h.rpc(open_req("agent", 1.0, "", false)).unwrap();
        h.advance_s(2.0);
    }
    assert_eq!(h.rpc(status_req(&id, 10)).unwrap().get("error_code"), Some(&"INTERACTIVE_NOT_FOUND".into()));
}

#[test]
fn human_lease_not_expired_by_tick() {
    let mut h = ready();
    h.attach_pty_console("c1", true);
    h.clock.advance(Duration::from_secs(1_000_000));
    h.feed(ControlEvent::Tick);
    assert!(h.core.active_lease().is_some_and(|l| l.owner == "human:c1" && l.status == "active"));
}

// ---------------------------------------------------------------- stash 與 D-16

#[test]
fn stash_dropped_when_peer_gone_sc31() {
    let mut h = ready();
    let id = soft_preempted(&mut h);
    h.input("c1", b"Q");
    h.scan(Some(&[])); // c1 peer 消失
    h.rpc(close_req(&id)).unwrap();
    assert_eq!(h.public().get("interactive_session_id"), Some(&PyValue::Null));
    assert!(!h.human_tx().iter().any(|(_, _, p)| p == b"Q"));
    assert!(h.warns().iter().any(|w| w.contains("client_id=c1") && w.contains("reason=lease_dropped")));
    assert_eq!(h.core.human_console().ownership.interactive_owner, None);
}

#[test]
fn stash_dropped_when_expired_sc31() {
    let mut h = ready();
    h.attach_pty_console("c1", true);
    let r = h.rpc(open_req("agent", 1.0e9, "", false)).unwrap();
    let id = iid(&r);
    h.input("c1", b"Q");
    h.clock.advance(Duration::from_secs(86_401)); // stash（human lease 86400 s）過期；不投遞 Tick
    h.rpc(close_req(&id)).unwrap();
    assert_eq!(h.public().get("interactive_session_id"), Some(&PyValue::Null));
    assert!(!h.human_tx().iter().any(|(_, _, p)| p == b"Q"));
    assert!(h.warns().iter().any(|w| w.contains("reason=lease_dropped")));
    assert_eq!(h.core.human_console().ownership.interactive_owner, None);
}

#[test]
fn close_restore_replay_holds_foreground() {
    let mut h = ready();
    h.set_auto_complete_human_tx(false);
    let id = soft_preempted(&mut h);
    h.input("c1", b"Q");
    h.rpc(close_req(&id)).unwrap();
    assert_eq!(h.human_tx(), vec![(TxClass::HumanReplay, "human:c1".to_string(), b"Q".to_vec())]);
    let hold = h.last_human_permit().unwrap().replay_hold.expect("hold");
    assert_eq!(h.fence_replay_auth(), Some(hold));
    assert!(h.foreground_busy());
    let (r, fx) = h.rpc_full(SessionRequest::Submit(agent_submit("echo b")));
    assert_eq!(r.unwrap().get("ok"), Some(&true.into()));
    assert!(start_ops(&fx).is_empty(), "回放結算前不出列");
    let fx = h.complete_human_tx(TxOutcome::Completed(1), None);
    assert_eq!(start_ops(&fx).len(), 1);
}

#[test]
fn interactive_rpc_busy_during_replay_hold() {
    let mut h = ready();
    h.set_auto_complete_human_tx(false);
    let id = soft_preempted(&mut h);
    h.input("c1", b"Q");
    h.rpc(close_req(&id)).unwrap();
    let before = h.tx_effect_count();
    let (r, fx) = h.rpc_full(open_req("agent", 60.0, "help", false));
    assert_eq!(wire(&r.unwrap()), r#"{"ok":false,"error_code":"SESSION_BUSY","selector":"COM0"}"#);
    assert!(start_ops(&fx).is_empty());
    assert_eq!(h.tx_effect_count(), before);
    assert_eq!(h.public().get("foreground_busy"), Some(&true.into()));
    h.complete_human_tx(TxOutcome::Completed(1), None);
    assert_eq!(h.rpc(open_req("agent", 60.0, "", false)).unwrap().get("ok"), Some(&true.into()));
}

#[test]
fn tick_close_restore_returns_session_interactive_busy() {
    // parity-amendments #56（D-22(2) 例外；對照 test_tick_blocks_new_lease_until_owner_restore_finishes）
    let mut h = ready();
    h.set_auto_complete_human_tx(false);
    h.attach_pty_console("c1", true);
    let r = h.rpc(open_req("agent:old", 1.0, "", false)).unwrap();
    assert_eq!(r.get("soft_preempted"), Some(&true.into()));
    h.input("c1", b"T");
    h.advance_s(2.0); // tick 到期 → stash 還原、回放 T（未結算）
    assert_eq!(h.human_tx().len(), 1);
    let r = h.rpc(open_req("agent:new", 60.0, "", false)).unwrap();
    // `session_manager.py` READY 分支：帶目前（已還原的 human）lease id。
    let human_id = h.core.active_lease().unwrap().interactive_id.clone();
    assert_eq!(
        wire(&r),
        format!(r#"{{"ok":false,"error_code":"SESSION_INTERACTIVE_BUSY","interactive_session_id":"{human_id}"}}"#)
    );
    let r = h.rpc(SessionRequest::ConsoleAttach { label: None, pty: Some(PtyInfo { token: 99, slave_path: "/dev/pts/199".into(), client_id: "c9".into() }) }).unwrap();
    assert_eq!(wire(&r), r#"{"ok":false,"error_code":"SESSION_INTERACTIVE_BUSY","selector":"COM0"}"#);
    h.complete_human_tx(TxOutcome::Completed(1), None);
    // 還原結算後：human lease（c1）仍在 → agent:new 走 soft preempt 成功
    let r = h.rpc(open_req("agent:new", 60.0, "", false)).unwrap();
    assert_eq!(r.get("ok"), Some(&true.into()));
    assert_eq!(h.core.active_lease().unwrap().owner, "agent:new");
}

#[test]
fn attached_rescue_open_mid_replay_redefers() {
    // R2-F4，D-22(4)
    let mut h = attached(BOOT);
    h.set_auto_complete_human_tx(false);
    h.attach_pty_console("c1", true);
    feed_rx(&mut h, b"Hit any key to stop autoboot:  3");
    let a = iid(&h.rpc(open_req("agent", 60.0, "", true)).unwrap());
    h.input("c1", b"k");
    h.rpc(close_req(&a)).unwrap(); // stash 還原 → 回放 k（hold h，未結算）
    let first = h.last_human_permit().unwrap().replay_hold.unwrap();
    let mark = h.effect_kinds().len();
    let b = h.rpc(open_req("agent", 60.0, "", true)).unwrap();
    assert_eq!(b.get("ok"), Some(&true.into()), "ATTACHED 分支不判前景");
    assert!(!h.effect_kinds()[mark..].contains(&"HumanTx"));
    assert_eq!(h.fence_replay_auth(), None);
    assert!(h.replay_hold().unwrap().revoked);
    let fx = h.complete_human_tx(TxOutcome::NotSubmitted, Some(FenceReject::ReplayRevoked));
    assert!(human_txs(&fx).is_empty(), "無重送");
    assert_eq!(h.deferred_len("c1"), 1);
    assert_eq!(h.replay_hold(), None);
    let bid = iid(&b);
    let n_tx = h.human_tx().len();
    let (_r, _) = send_ok(&mut h, &bid, "ab", "plain");
    assert_eq!(h.human_tx().len(), n_tx, "B 的 send 期間無新 HumanTx");
    h.rpc(close_req(&bid)).unwrap();
    assert_eq!(h.human_tx().last().unwrap(), &(TxClass::HumanReplay, "human:c1".to_string(), b"k".to_vec()));
    assert_ne!(h.last_human_permit().unwrap().replay_hold, Some(first));
    let order = h.tx_order();
    let ab = order.iter().position(|(c, p)| *c == TxClass::LeaseOwner && p == b"ab").unwrap();
    let k2 = order.iter().rposition(|(c, p)| *c == TxClass::HumanReplay && p == b"k").unwrap();
    assert!(ab < k2);
}

// ---------------------------------------------------------------- refresh／self-heal

#[test]
fn peer_flap_within_grace_keeps_lease() {
    let mut h = ready();
    h.attach_pty_console("c1", true);
    h.scan(Some(&[]));
    assert!(h.core.active_lease().unwrap().peer_lost_at.is_some());
    h.clock.advance(Duration::from_secs(2));
    h.scan(Some(&["/dev/pts/101"]));
    let lease = h.core.active_lease().unwrap();
    assert_eq!(lease.owner, "human:c1");
    assert_eq!(lease.peer_lost_at, None);
}

#[test]
fn peer_gone_past_grace_tears_down() {
    let mut h = ready();
    h.attach_pty_console("c1", true);
    h.scan(Some(&[]));
    h.clock.advance(Duration::from_millis(3100));
    let fx = h.scan(Some(&[]));
    assert!(fx.iter().any(|e| matches!(e, Effect::ConsoleClose { client_id } if client_id == "c1")));
    assert!(h.core.active_lease().is_none());
    assert!(!h.core.human_console().consoles.contains_key("c1"));
}

#[test]
fn refresh_closes_when_owner_mismatch() {
    let mut h = ready();
    let r = h.attach_pty_console("c1", true);
    let old = sv(r.get("interactive_session_id").unwrap());
    h.core.human_console_mut().ownership.interactive_owner = Some("human:zz".into());
    h.scan(Some(&["/dev/pts/101"]));
    // refresh 關閉舊 lease（owner 不符）；同一次掃描的 self-heal 可能再授予 primary 新 lease（id 不同）。
    assert!(h.lease_index_ops().contains(&LeaseIndexOp::Remove { interactive_id: old.clone() }));
    assert!(h.core.active_lease().is_none_or(|l| l.interactive_id != old));
}

#[test]
fn self_heal_regrants_after_owner_loss() {
    let mut h = ready();
    let r = h.attach_pty_console("c1", true);
    let id = sv(r.get("interactive_session_id").unwrap());
    h.rpc(close_req(&id)).unwrap();
    assert_eq!(h.core.human_console().ownership.interactive_owner, None);
    let g = h.core.generations().owner_gen;
    h.scan(Some(&["/dev/pts/101"]));
    assert_eq!(h.core.human_console().ownership.interactive_owner.as_deref(), Some("human:c1"));
    let lease = h.core.active_lease().unwrap();
    assert_eq!(lease.owner, "human:c1");
    assert_eq!(lease.timeout_s, 86_400.0);
    assert!(h.core.generations().owner_gen > g, "grant 屬 owner 變更（§5.3）");
}

#[test]
fn self_heal_skips_during_agent_active() {
    let mut h = ready();
    let r = h.attach_pty_console("c1", true);
    let id = sv(r.get("interactive_session_id").unwrap());
    h.rpc(close_req(&id)).unwrap();
    h.core.human_console_mut().ownership.suspend();
    h.scan(Some(&["/dev/pts/101"]));
    assert!(h.core.active_lease().is_none());
}

#[test]
fn self_heal_skips_non_ready_attached_states() {
    let mut h = ready();
    let r = h.attach_pty_console("c1", true);
    let id = sv(r.get("interactive_session_id").unwrap());
    h.rpc(close_req(&id)).unwrap();
    h.force_state(SessionState::Recovering);
    h.scan(Some(&["/dev/pts/101"]));
    assert!(h.core.active_lease().is_none());
}

/// Recover drain 完成並重新 attach，以 `login` 收尾。
fn recover_and_reattach(h: &mut CoreHarness, login: LoginOutcome) {
    h.begin_stop(StopKind::Recover);
    let (epoch, stop) = h.pending_stop();
    h.receipt(epoch, stop).feed();
    h.bridge_ok();
    let (epoch, fsm_gen) = (h.epoch(), h.core.generations().fsm_gen);
    h.feed(ControlEvent::LoginFinished { epoch, fsm_gen, result: login, after_rx_seq: None });
}

#[test]
fn retained_human_restored_after_ready() {
    let mut h = ready();
    h.attach_pty_console("c1", true);
    recover_and_reattach(&mut h, LoginOutcome::Ready);
    assert_eq!(h.core.state(), SessionState::Ready);
    let lease = h.core.active_lease().expect("重開 human lease");
    assert_eq!(lease.owner, "human:c1");
    assert_eq!(lease.timeout_s, 86_400.0);
    assert_eq!(h.core.human_console().ownership.interactive_owner.as_deref(), Some("human:c1"));
}

#[test]
fn retained_human_restored_after_attached() {
    let mut h = ready();
    h.attach_pty_console("c1", true);
    recover_and_reattach(&mut h, LoginOutcome::NotReady("PROMPT_UNAVAILABLE".into()));
    assert_eq!(h.core.state(), SessionState::Attached);
    assert_eq!(h.core.active_lease().map(|l| l.owner.clone()).as_deref(), Some("human:c1"));
    assert_eq!(h.core.human_console().ownership.interactive_owner.as_deref(), Some("human:c1"));
}

#[test]
fn cmd_interactive_mode_opens_lease() {
    let mut h = ready();
    let mut req = agent_submit("vim");
    req.mode = "interactive".into();
    req.timeout_s = 5.0;
    let (r, fx) = h.rpc_full(SessionRequest::Submit(req));
    let cmd_id = sv(r.unwrap().get("cmd_id").unwrap());
    let lease = h.core.active_lease().expect("lease");
    assert_eq!(lease.owner, "agent");
    assert_eq!(lease.timeout_s, 5.0_f64.max(profile_of(SHELL_PROFILE).template.hard_timeout_s));
    let grant = fx
        .iter()
        .find_map(|e| match e {
            Effect::StartOp { grant, .. } => Some(grant.clone()),
            _ => None,
        })
        .expect("StartOp");
    assert_eq!(grant.cmd_id.as_deref(), Some(cmd_id.as_str()));
    finish_op(&mut h, &grant, OpOutcome::Sent { bytes: 4 });
    let row = h.reg.get(&cmd_id).unwrap();
    assert_eq!(row["execution_mode"], PyValue::from("interactive"));
    assert_eq!(row["status"], PyValue::from("interactive"), "arbiter：result status interactive 寫回紀錄");
}

#[test]
fn human_reboot_opens_lease() {
    let mut h = ready();
    h.connect_stream_console("c1", ConsoleTransportKind::TcpTelnet);
    h.connect_stream_console("c2", ConsoleTransportKind::TcpTelnet);
    let e0 = h.core.generations().input_epoch;
    let fx = h.input("c2", b"reboot\r");
    let grant = fx
        .iter()
        .find_map(|e| match e {
            Effect::StartOp { grant, .. } => Some(grant.clone()),
            _ => None,
        })
        .expect("reboot 出列");
    assert_eq!(h.core.generations().input_epoch, e0 + 1, "invalidate_human_input(RebootCommand)");
    finish_op(&mut h, &grant, OpOutcome::RebootExpected);
    assert_eq!(h.core.state(), SessionState::Attached);
    assert_eq!(h.public().get("last_error"), Some(&"REBOOTING".into()));
    let lease = h.core.active_lease().expect("human lease");
    assert_eq!(lease.owner, "human:c2");
    let row = h.last_human_command().unwrap();
    assert_eq!(row["recovery_action"], PyValue::from("PROMOTE_HUMAN_INTERACTIVE"));
    assert_eq!(row["interactive_session_id"], PyValue::from(lease.interactive_id.clone()));
}

#[test]
fn lease_context_shapes_in_core() {
    let mut h = ready();
    assert_eq!(
        wire(&h.core.lease_context_now(h.clock.mono_ns())),
        r#"{"interactive_owner":null,"human_attached":false,"human_active":false,"recovery_mode":false}"#
    );
    h.attach_pty_console("c1", true);
    assert_eq!(
        wire(&h.core.lease_context_now(h.clock.mono_ns())),
        r#"{"interactive_owner":"human:c1","human_attached":true,"human_active":false,"recovery_mode":false}"#
    );
    h.input("c1", b"a");
    assert_eq!(
        wire(&h.core.lease_context_now(h.clock.mono_ns())),
        r#"{"interactive_owner":"human:c1","human_attached":true,"human_active":true,"recovery_mode":false}"#
    );
    let _ = pydict! {};
}

// ---------------------------------------------------------------- console_detach／console_list（`session_manager.py:6775-6804`）

#[test]
fn console_detach_closes_human_lease() {
    let mut h = ready();
    h.attach_pty_console("c1", true);
    let (r, fx) = h.rpc_full(SessionRequest::ConsoleDetach { client_id: "c1".into() });
    let r = r.unwrap();
    let keys: Vec<&str> = r.0.keys().map(String::as_str).collect();
    assert_eq!(keys, vec!["ok", "client_id", "session"]);
    assert!(fx.iter().any(|e| matches!(e, Effect::ConsoleClose { client_id } if client_id == "c1")));
    assert!(h.core.active_lease().is_none());
    assert!(!h.core.human_console().consoles.contains_key("c1"));
    assert_eq!(h.core.human_console().ownership.interactive_owner, None);
    assert_eq!(h.core.human_console().vtty_cache, None, "vtty 隨 primary 重算（無 console → None）");
}

#[test]
fn console_detach_unknown() {
    let mut h = ready();
    let r = h.rpc(SessionRequest::ConsoleDetach { client_id: "zz".into() }).unwrap();
    assert_eq!(wire(&r), r#"{"ok":false,"error_code":"CONSOLE_NOT_FOUND","client_id":"zz"}"#);
}

#[test]
fn console_ops_without_bridge_not_ready() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    for req in [SessionRequest::ConsoleDetach { client_id: "c1".into() }, SessionRequest::ConsoleList] {
        let r = h.rpc(req).unwrap();
        assert_eq!(wire(&r), r#"{"ok":false,"error_code":"SESSION_NOT_READY","selector":"COM0"}"#);
    }
}

#[test]
fn console_list_shape_sorted_and_prunes_stale() {
    let mut h = ready();
    h.attach_pty_console("c1", true); // primary、raw owner
    h.attach_pty_console("c2", false); // 無 peer
    h.connect_stream_console("t9", ConsoleTransportKind::TcpTelnet);
    h.clock.advance(Duration::from_millis(2100)); // 超過 STALE_CONSOLE_GRACE_S
    let (r, fx) = h.rpc_full(SessionRequest::ConsoleList);
    let r = r.unwrap();
    let keys: Vec<&str> = r.0.keys().map(String::as_str).collect();
    assert_eq!(keys, vec!["ok", "consoles", "session"]);
    let PyValue::List(rows) = r.get("consoles").unwrap() else { panic!("consoles 應為 list") };
    let ids: Vec<String> = rows
        .iter()
        .map(|row| match row {
            PyValue::Dict(d) => sv(d.get("client_id").unwrap()),
            _ => panic!(),
        })
        .collect();
    // c2 無 peer 且已過 grace → prune；依 (label, client_id) 排序（label 預設為 client_id／TCP peer）
    // t9 的 label 為 TCP peer "127.0.0.1:…"，排序在 "c1" 之前
    assert_eq!(ids, vec!["t9".to_string(), "c1".to_string()]);
    assert!(fx.iter().any(|e| matches!(e, Effect::ConsoleClose { client_id } if client_id == "c2")));
    let PyValue::Dict(c1) = &rows[1] else { panic!() };
    assert_eq!(wire(c1), r#"{"client_id":"c1","label":"c1","vtty":"/dev/pts/101","interactive_owner":true}"#);
}

/// High（2f4f76a）：line editor（`SubmitOrigin::HumanConsole`）命令逾時改開 human lease，不走 agent 救援。
#[test]
fn human_timeout_promoted_only_for_internal_origin() {
    let mut h = ready();
    h.connect_stream_console("c1", ConsoleTransportKind::TcpTelnet);
    h.connect_stream_console("c2", ConsoleTransportKind::TcpTelnet);
    let fx = h.input("c2", b"ls\r");
    let grant = fx
        .iter()
        .find_map(|e| match e {
            Effect::StartOp { grant, .. } => Some(grant.clone()),
            _ => None,
        })
        .expect("ls 出列");
    finish_op(&mut h, &grant, OpOutcome::PromptTimeout { stdout: String::new() });
    assert_eq!(h.core.state(), SessionState::Ready, "human 逾時不轉 ATTACHED");
    let lease = h.core.active_lease().expect("human lease");
    assert_eq!(lease.owner, "human:c2");
    let row = h.last_human_command().unwrap();
    assert_eq!(row["recovery_action"], PyValue::from("PROMOTE_HUMAN_INTERACTIVE"));
    assert_eq!(row["partial"], PyValue::Bool(true));
    assert_eq!(row["execution_mode"], PyValue::from("interactive"));
    assert_eq!(row["interactive_session_id"], PyValue::from(lease.interactive_id.clone()));

    // 對照：RPC 來源即使 source 為 `human:x` 仍走 agent 分支（DF-02）。
    let mut h = ready();
    let (resp, fx) = h.rpc_full(SessionRequest::Submit({
        let mut r = agent_submit("ls");
        r.source = "human:x".into();
        r
    }));
    assert_eq!(resp.unwrap().get("ok"), Some(&PyValue::Bool(true)));
    let grant = fx
        .iter()
        .find_map(|e| match e {
            Effect::StartOp { grant, .. } => Some(grant.clone()),
            _ => None,
        })
        .expect("ls 出列");
    finish_op(&mut h, &grant, OpOutcome::PromptTimeout { stdout: String::new() });
    assert_eq!(h.core.state(), SessionState::Attached);
    assert!(h.core.active_lease().is_none());
}

/// High（2f4f76a）：真 OpRunner 下，line editor 命令逾時不送 CTRL-C／CTRL-D。
#[test]
fn human_line_timeout_sends_no_rescue_keys() {
    use std::time::Instant;
    use sw_core::mailbox::DataEvent;
    let rig = ActorRig::new(SHELL_PROFILE).ready();
    for (cid, life) in [("c1", 1u64), ("c2", 2u64)] {
        rig.send_control(ControlEvent::ConsoleConnected {
            client_id: cid.into(),
            client_lifetime: life,
            label: format!("127.0.0.1:5000{life}"),
            vtty: format!("127.0.0.1:5000{life}"),
            kind: ConsoleTransportKind::TcpTelnet,
            close_gen: 0,
        });
    }
    let _ = rig.get_state();
    let epoch = rig.fence.with(|f| f.input_epoch);
    rig.data_sender()
        .send(DataEvent::ConsoleInputBytes {
            client_id: "c2".into(),
            client_lifetime: 2,
            input_epoch: epoch,
            bytes: b"ls\r".to_vec(),
        })
        .unwrap();
    rig.wait_for("ls 寫出", |r| r.wal_tx_payloads().iter().any(|p| p == b"ls\n"));
    std::thread::sleep(Duration::from_millis(100));
    let deadline = Instant::now() + Duration::from_secs(10);
    loop {
        let st = rig.get_state();
        if st.get("interactive_session_id").is_some_and(|v| !v.is_null()) {
            break;
        }
        assert!(Instant::now() < deadline, "逾時後應開 human lease：{st:?}");
        rig.clock.advance(Duration::from_millis(500));
        rig.send_control(ControlEvent::Tick);
        std::thread::sleep(Duration::from_millis(20));
    }
    let payloads = rig.wal_tx_payloads();
    assert!(!payloads.iter().any(|p| p == b"\x03" || p == b"\x04"), "human 命令不得送救援鍵：{payloads:?}");
    assert_eq!(rig.get_state()["state"], PyValue::from("READY"));
}

// ---------------------------------------------------------------------------
// p6-cloud 審查回歸（1f1fa67..4f710c3）
// ---------------------------------------------------------------------------

/// High（6eafa6a）：READY open 的 refresh 惰性結束過期 agent lease、還原 human 並建立回放 hold 時，
/// 本次 open 回 SESSION_BUSY（D-22(2)），不得 revoke 剛建立的回放再 soft preempt。
#[test]
fn refresh_created_replay_hold_blocks_reopen() {
    let mut h = ready();
    h.set_auto_complete_human_tx(false);
    h.attach_pty_console("c1", true);
    let r = h.rpc(open_req("agent:a", 1.0, "", false)).unwrap();
    assert_eq!(r.get("soft_preempted"), Some(&true.into()));
    h.input("c1", b"Q"); // 進 deferred
    h.clock.advance(Duration::from_millis(1500)); // 過期，但尚未投遞 Tick
    let r = h.rpc(open_req("agent:b", 60.0, "", false)).unwrap();
    assert_eq!(wire(&r), r#"{"ok":false,"error_code":"SESSION_BUSY","selector":"COM0"}"#);
    assert_eq!(h.human_tx(), vec![(TxClass::HumanReplay, "human:c1".to_string(), b"Q".to_vec())]);
    assert!(h.fence_replay_auth().is_some(), "回放授權未被撤銷");
    h.complete_human_tx(TxOutcome::Completed(1), None);
    let r = h.rpc(open_req("agent:b", 60.0, "", false)).unwrap();
    assert_eq!(r.get("soft_preempted"), Some(&true.into()), "回放結算後下一次 open 才 preempt");
}

/// Medium（6eafa6a，parity #56）：tick 到期還原結算前，interactive 模式命令回 SESSION_INTERACTIVE_BUSY。
#[test]
fn interactive_command_during_tick_close_is_busy() {
    let mut h = ready();
    h.set_auto_complete_human_tx(false);
    h.attach_pty_console("c1", true);
    let r = h.rpc(open_req("agent:old", 1.0, "", false)).unwrap();
    assert_eq!(r.get("soft_preempted"), Some(&true.into()));
    h.input("c1", b"T");
    h.advance_s(2.0); // tick 到期 → 還原、回放 T（未結算）
    let mut req = agent_submit("vim");
    req.mode = "interactive".into();
    let resp = h.rpc(SessionRequest::Submit(req)).unwrap();
    let cmd_id = resp["cmd_id"].as_str().unwrap().to_string();
    h.complete_human_tx(TxOutcome::Completed(1), None);
    let row = h.reg.get(&cmd_id).unwrap();
    assert_eq!(row["error_code"], PyValue::from("SESSION_INTERACTIVE_BUSY"), "{row:?}");
    assert_eq!(h.core.active_lease().unwrap().owner, "human:c1", "未開新 lease");
}

/// Medium（35359438）：FLASHING 拒絕零 TX；bridge 掉線後 lease 已隨 detach 移除（Python `session_manager.py:2518-2522`
/// `_interactive.pop`），send／status 回 INTERACTIVE_NOT_FOUND、零 TX（`SESSION_NOT_READY` 分支在此路徑不可達）。
#[test]
fn send_checks_order_detached_and_flashing_zero_tx() {
    let mut h = ready();
    let id = iid(&h.rpc(open_req("agent", 60.0, "", false)).unwrap());
    h.force_state(SessionState::Flashing);
    let before = h.tx_effect_count();
    let r = h.rpc(send_req(&id, "x", "plain")).unwrap();
    assert_eq!(wire(&r), format!(r#"{{"ok":false,"error_code":"FLASHING_BUSY","interactive_id":"{id}"}}"#));
    assert_eq!(h.tx_effect_count(), before);
    let mut h = ready();
    let id = iid(&h.rpc(open_req("agent", 60.0, "", false)).unwrap());
    h.begin_stop(StopKind::IoError);
    let before = h.tx_effect_count();
    let r = h.rpc(send_req(&id, "x", "plain")).unwrap();
    assert_eq!(wire(&r), format!(r#"{{"ok":false,"error_code":"INTERACTIVE_NOT_FOUND","interactive_id":"{id}"}}"#));
    assert_eq!(h.tx_effect_count(), before);
}

/// Medium（35359438）：`status_not_ready_without_bridge`——同上，bridge 掉線時 lease 已移除。
#[test]
fn status_not_ready_without_bridge() {
    let mut h = ready();
    let id = iid(&h.rpc(open_req("agent", 60.0, "", false)).unwrap());
    h.begin_stop(StopKind::IoError);
    let r = h.rpc(status_req(&id, 2048)).unwrap();
    assert_eq!(wire(&r), format!(r#"{{"ok":false,"error_code":"INTERACTIVE_NOT_FOUND","interactive_id":"{id}"}}"#));
}

/// Medium（審查 f6859e8..3317987 #3，R3-12 回歸）：ATTACHED 救援分支 refresh 惰性結束過期 lease 並建立回放 hold 時，
/// Python ATTACHED 分支無 SESSION_BUSY 出口（`retry_after_post` 後照常開 lease）；Rust 依 D-22(4) 撤銷該 hold、
/// `Q` 回 deferred，開 lease 回 `recovery_mode:true`，close B 後才回放 `Q`。
#[test]
fn attached_refresh_created_replay_still_opens_recovery() {
    let mut h = attached(BOOT);
    h.set_auto_complete_human_tx(false);
    h.attach_pty_console("c1", true);
    feed_rx(&mut h, b"U-Boot 2020\r\n=> ");
    let a = h.rpc(open_req("agent:a", 1.0, "", true)).unwrap();
    assert_eq!(a.get("recovery_mode"), Some(&true.into()), "{a:?}");
    h.input("c1", b"Q"); // 進 deferred
    assert!(h.human_tx().is_empty());
    h.clock.advance(Duration::from_millis(1500)); // 過期，但尚未投遞 Tick
    let b = h.rpc(open_req("agent:b", 60.0, "", true)).unwrap();
    assert_eq!(b.get("ok"), Some(&true.into()), "ATTACHED 分支無 SESSION_BUSY 出口：{b:?}");
    assert_eq!(b.get("recovery_mode"), Some(&true.into()));
    assert_eq!(h.core.active_lease().unwrap().owner, "agent:b");
    assert_eq!(h.fence_replay_auth(), None, "refresh 建立的回放 hold 被撤銷");
    assert!(h.replay_hold().unwrap().revoked);
    let fx = h.complete_human_tx(TxOutcome::NotSubmitted, Some(FenceReject::ReplayRevoked));
    assert!(human_txs(&fx).is_empty(), "無重送");
    assert_eq!(h.deferred_len("c1"), 1, "Q restore_front 回 deferred");
    let bid = iid(&b);
    let n_tx = h.human_tx().len();
    h.rpc(close_req(&bid)).unwrap();
    assert!(h.human_tx().len() > n_tx, "close B 後回放");
    assert_eq!(h.human_tx().last().unwrap(), &(TxClass::HumanReplay, "human:c1".to_string(), b"Q".to_vec()));
}

/// sol 補審 p6-20261028 High #1（6eafa6a，`lease_p6.rs:139`）：命令出列時的 lazy expiry 還原 human 並建立回放 hold，
/// 回放必須先於該命令送出且不得被其 `command_fg_begin` 撤銷（Python `execute_command` 於送出命令前同步
/// `post.execute()`，UART 順序為 Q → B）；reboot 命令同樣不得讓 Q 因失效而遺失。
#[test]
fn dequeue_lazy_expiry_replays_before_next_command() {
    for cmd in ["echo B", "reboot"] {
        let mut h = ready();
        h.set_auto_complete_human_tx(false);
        h.attach_pty_console("c1", true);
        let r = h.rpc(open_req("agent:a", 1.0, "", false)).unwrap();
        assert_eq!(r.get("soft_preempted"), Some(&true.into()));
        h.input("c1", b"Q"); // 進 deferred
        h.clock.advance(Duration::from_millis(1500)); // A 過期，尚未投遞 Tick
        let resp = h.rpc(SessionRequest::Submit(agent_submit(cmd))).unwrap();
        assert_eq!(resp.get("ok"), Some(&true.into()), "{cmd}: {resp:?}");
        let order = h.tx_order();
        assert!(
            !order.iter().any(|(c, _)| *c == TxClass::Command),
            "{cmd}: 回放結算前命令不得送出：{order:?}"
        );
        assert_eq!(
            order.last(),
            Some(&(TxClass::HumanReplay, b"Q".to_vec())),
            "{cmd}: lazy expiry 的回放先送出：{order:?}"
        );
        assert!(h.fence_replay_auth().is_some(), "{cmd}: 回放授權未被撤銷");
        h.complete_human_tx(TxOutcome::Completed(1), None);
        let order = h.tx_order();
        let q = order.iter().position(|(c, p)| *c == TxClass::HumanReplay && p == b"Q");
        let b = order
            .iter()
            .position(|(c, p)| *c == TxClass::Command && p == format!("{cmd}\n").as_bytes());
        assert!(q.is_some() && b.is_some() && q < b, "{cmd}: UART 順序須為 Q → 命令：{order:?}");
        assert_eq!(h.deferred_len("c1"), 0, "{cmd}: Q 不得留在 deferred");
        assert_eq!(
            order.iter().filter(|(c, p)| *c == TxClass::HumanReplay && p == b"Q").count(),
            1,
            "{cmd}: Q 恰送出一次"
        );
    }
}

/// 同上：命令停在回放 hold 期間遇 drain，該命令以未送出終結（零 Command TX），不得於 drain 後補送。
#[test]
fn dequeue_parked_command_aborted_on_drain() {
    let mut h = ready();
    h.set_auto_complete_human_tx(false);
    h.attach_pty_console("c1", true);
    let r = h.rpc(open_req("agent:a", 1.0, "", false)).unwrap();
    assert_eq!(r.get("soft_preempted"), Some(&true.into()));
    h.input("c1", b"Q");
    h.clock.advance(Duration::from_millis(1500));
    let resp = h.rpc(SessionRequest::Submit(agent_submit("echo B"))).unwrap();
    let cmd_id = resp["cmd_id"].as_str().unwrap().to_string();
    h.begin_stop(StopKind::IoError);
    let row = h.reg.get(&cmd_id).unwrap();
    assert!(!row.get("done_at").unwrap_or(&PyValue::Null).is_null(), "drain 時終結：{row:?}");
    assert!(
        !h.tx_order().iter().any(|(c, _)| *c == TxClass::Command),
        "停放的命令零 TX：{:?}",
        h.tx_order()
    );
}

/// 建立停在回放 hold 的命令 B（同 `dequeue_lazy_expiry_replays_before_next_command` 的序列），回傳 (B 的 cmd_id, session_id)。
fn parked_command(h: &mut CoreHarness) -> (String, String) {
    h.set_auto_complete_human_tx(false);
    h.attach_pty_console("c1", true);
    let r = h.rpc(open_req("agent:a", 1.0, "", false)).unwrap();
    assert_eq!(r.get("soft_preempted"), Some(&true.into()));
    h.input("c1", b"Q");
    h.clock.advance(Duration::from_millis(1500));
    let resp = h.rpc(SessionRequest::Submit(agent_submit("echo B"))).unwrap();
    let cmd_id = resp["cmd_id"].as_str().unwrap().to_string();
    let row = h.reg.get(&cmd_id).unwrap();
    assert_eq!(row["status"], PyValue::from("running"), "B 已出列、停在回放 hold：{row:?}");
    let sid = row["session_id"].as_str().unwrap().to_string();
    assert_eq!(h.reg.pending(&sid), 1);
    (cmd_id, sid)
}

/// sol P6-e9b5c61-2（High）：停放命令被 `command.cancel` 後，回放結算的 kick 跳過該命令時必須結算 registry
/// 的在途指標（Python worker 收尾 `_running.pop`）；修正前 canceled＋in_flight 永久多算一筆 pending。
#[test]
fn canceled_parked_command_releases_pending_slot() {
    let mut h = ready();
    let (cmd_id, sid) = parked_command(&mut h);
    assert_eq!(h.reg.cancel(&cmd_id, "wall")["ok"], PyValue::from(true));
    h.rpc(SessionRequest::CancelQueued { cmd_id: cmd_id.clone() }).unwrap();
    let fx = h.complete_human_tx(TxOutcome::Completed(1), None);
    assert!(
        !fx.iter().any(|e| matches!(e, Effect::StartOp { spec: OpSpec::Command(_), .. })),
        "canceled 的停放命令不送出"
    );
    assert!(!h.tx_order().iter().any(|(c, _)| *c == TxClass::Command), "零 Command TX");
    assert_eq!(h.reg.get(&cmd_id).unwrap()["status"], PyValue::from("canceled"), "保留 canceled 紀錄");
    assert_eq!(h.reg.pending(&sid), 0, "停放命令取消後不得占用 pending 額度");
    // 後續命令照常出列、完成後 pending 歸零（容量完整恢復）。
    let c = h.start_foreground_command("echo C");
    assert_eq!(h.reg.get(&c.cmd_id).unwrap()["status"], PyValue::from("running"));
    let epoch = h.epoch();
    h.feed(ControlEvent::OpFinished {
        op_id: c.op_id,
        epoch,
        outcome: OpOutcome::CommandResult(pydict! { "ok" => true, "stdout" => "C" }),
        after_rx_seq: None,
    });
    assert_eq!(h.reg.pending(&sid), 0);
}

/// 同上：停放命令取消後才遇 drain，同樣結算在途指標（`abort_parked_command` 不得因已取消而略過收尾）。
#[test]
fn canceled_parked_command_then_drain_releases_pending_slot() {
    let mut h = ready();
    let (cmd_id, sid) = parked_command(&mut h);
    assert_eq!(h.reg.cancel(&cmd_id, "wall")["ok"], PyValue::from(true));
    h.begin_stop(StopKind::IoError);
    assert_eq!(h.reg.get(&cmd_id).unwrap()["status"], PyValue::from("canceled"));
    assert_eq!(h.reg.pending(&sid), 0, "drain 後 canceled 停放命令不得占用 pending");
    assert!(!h.tx_order().iter().any(|(c, _)| *c == TxClass::Command));
}

/// 接線稽核（review-convergence-rules §5；R3-8 worker health）：ATTACHED 救援的 worker 健康須取自 manager
/// 正式注入的 `SelfTestProbes::bridge_snapshot`（現行 epoch 的 IoWorker 快照，`session_manager.rs`），不得依賴
/// 只有測試會呼叫的 `set_worker_health`。Python `interactive_open` ATTACHED 分支 `bridge.snapshot()` 的
/// running／serial_alive 任一為假 → SESSION_NOT_READY（零 TX）。修正前未注入 worker_health 即視為健康、照開 lease。
#[test]
fn attached_rescue_uses_manager_bridge_snapshot_for_worker_health() {
    use std::sync::Arc;
    for (running, serial_alive) in [(false, true), (true, false)] {
        let mut h = attached(BOOT);
        feed_rx(&mut h, b"\r\n=> ");
        h.core.set_self_test_probes(sw_core::self_test::SelfTestProbes {
            bridge_snapshot: Some(Arc::new(move |_epoch| {
                Some(sw_core::uart_bridge::WorkerSnapshot { running, serial_alive })
            })),
            vtty_alive: Some(Arc::new(|| Some(true))),
            ..Default::default()
        });
        let before = h.tx_effect_count();
        let r = h.rpc(open_req("agent", 60.0, "", true)).unwrap();
        assert_eq!(
            wire(&r),
            r#"{"ok":false,"error_code":"SESSION_NOT_READY","selector":"COM0"}"#,
            "running={running} serial_alive={serial_alive}"
        );
        assert_eq!(h.tx_effect_count(), before);
        assert!(h.core.active_lease().is_none());
    }
    // 對照：快照健康時照常開救援 lease。
    let mut h = attached(BOOT);
    feed_rx(&mut h, b"\r\n=> ");
    h.core.set_self_test_probes(sw_core::self_test::SelfTestProbes {
        bridge_snapshot: Some(Arc::new(|_epoch| {
            Some(sw_core::uart_bridge::WorkerSnapshot { running: true, serial_alive: true })
        })),
        vtty_alive: Some(Arc::new(|| Some(true))),
        ..Default::default()
    });
    let r = h.rpc(open_req("agent", 60.0, "", true)).unwrap();
    assert_eq!(r.get("recovery_mode"), Some(&true.into()), "{r:?}");
}
