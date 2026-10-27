//! P6 Task 6：SessionCore 的 console 事件、前景 admission／suspend 與 BC-02 失效（07 Task 6 Step 1）。
//!
//! 06 `CoreHarness`＋FakeClock；預設 `set_auto_complete_human_tx(true)`；每個「拒絕／丟棄」列另斷言
//! `h.tx_effect_count()` 不變。
#![cfg(feature = "test-fakes")]
mod support;

use support::*;
use sw_core::arbiter::SubmitOrigin;
use sw_core::deferred::InvalidateReason;
use sw_core::drain::StopKind;
use sw_core::human_console::out_ring::OutItem;
use sw_core::human_console::ConsoleTransportKind;
use sw_core::mailbox::ControlEvent;
use sw_core::op_runner::{OpOutcome, OpSpec};
use sw_core::session_core::*;
use sw_core::tx_writer::TxClass;
use sw_pal::traits::TxOutcome;
use sw_proto::pydict;
use sw_proto::pyjson::PyValue;

fn human_txs(effects: &[Effect]) -> Vec<(TxClass, String, Vec<u8>)> {
    effects
        .iter()
        .filter_map(|e| match e {
            Effect::HumanTx { permit, payload, .. } => {
                Some((permit.class, permit.source.clone(), payload.clone()))
            }
            _ => None,
        })
        .collect()
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

fn ready_with_tcp(cids: &[&str]) -> CoreHarness {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    for cid in cids {
        h.connect_stream_console(cid, ConsoleTransportKind::TcpTelnet);
    }
    h
}

fn owner(h: &CoreHarness) -> Option<String> {
    h.core.human_console().ownership.interactive_owner.clone()
}

#[test]
fn raw_input_becomes_human_tx() {
    let mut h = ready_with_tcp(&["c1"]);
    let fx = h.input("c1", b"\x1b[A");
    let txs: Vec<_> = fx
        .iter()
        .filter_map(|e| match e {
            Effect::HumanTx { permit, payload, .. } => Some((permit.clone(), payload.clone())),
            _ => None,
        })
        .collect();
    assert_eq!(txs.len(), 1);
    let (permit, payload) = &txs[0];
    assert_eq!(permit.class, TxClass::HumanRaw);
    assert_eq!(permit.source, "human:c1");
    assert_eq!(permit.cmd_id, None);
    assert!(permit.log);
    assert_eq!(permit.input_epoch, Some(h.core.generations().input_epoch));
    assert_eq!(permit.owner_gen, h.fence.with(|f| f.owner_gen));
    assert_eq!(permit.replay_hold, None);
    assert_eq!(payload, b"\x1b[A");
}

#[test]
fn stale_epoch_input_dropped_and_diagnosed() {
    let mut h = ready_with_tcp(&["c1"]);
    let life = h.lifetime("c1");
    let old = h.core.generations().input_epoch;
    h.invalidate(InvalidateReason::QuietRearm);
    let before = h.tx_effect_count();
    let warns_before = h.warns().len();
    for _ in 0..2 {
        let fx = h.input_with("c1", life, old, b"x");
        assert!(human_txs(&fx).is_empty());
        assert!(!fx.iter().any(|e| matches!(e, Effect::ConsoleOut { .. })));
    }
    let new_warns: Vec<String> = h.warns()[warns_before..].to_vec();
    assert_eq!(new_warns.len(), 1, "D-19 限速：{new_warns:?}");
    assert!(new_warns[0].contains("client_id=c1") && new_warns[0].contains("reason=stale_epoch"));
    assert_eq!(h.tx_effect_count(), before);
}

#[test]
fn unknown_or_old_lifetime_dropped() {
    let mut h = ready_with_tcp(&["c1"]);
    let life = h.lifetime("c1");
    let epoch = h.core.generations().input_epoch;
    let before = h.tx_effect_count();
    let fx = h.input_with("ghost", 1, epoch, b"x");
    assert!(human_txs(&fx).is_empty());
    let fx = h.input_with("c1", life + 1000, epoch, b"x");
    assert!(human_txs(&fx).is_empty());
    assert!(h.warns().iter().any(|w| w.contains("client_id=ghost") && w.contains("reason=unknown_client")));
    assert!(h.warns().iter().any(|w| w.contains("client_id=c1") && w.contains("reason=unknown_client")));
    assert_eq!(h.tx_effect_count(), before);
}

#[test]
fn line_input_echo_then_submit_order() {
    let mut h = ready_with_tcp(&["c1", "c2"]);
    let fx = h.input("c2", b"ls\r");
    let echo = fx
        .iter()
        .position(|e| {
            matches!(e, Effect::ConsoleOut { client_id, item: OutItem::Display { bytes, iac_escaped: false } }
                if client_id == "c2" && bytes == b"ls\r")
        })
        .expect("先回顯");
    let start = fx
        .iter()
        .position(|e| matches!(e, Effect::StartOp { spec: OpSpec::Command(_), .. }))
        .expect("後提交並出列");
    assert!(echo < start);
    let Some(OpSpec::Command(q)) = start_ops(&fx).into_iter().next() else { unreachable!() };
    assert_eq!(q.req.command, "ls");
    assert_eq!(q.req.source, "human:c2");
    assert_eq!(q.req.origin, SubmitOrigin::HumanConsole { client_id: "c2".into() });
    assert_eq!(q.req.mode, "line");
    assert_eq!(q.req.timeout_s, 30.0);
    assert_eq!(q.req.priority, 100);
    assert_eq!(q.req.expected_duration_s, None);
    assert_eq!(q.req.completion_regex, None);
}

#[test]
fn line_rejected_logs_warning_zero_tx() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_attached(None);
    h.connect_stream_console("c1", ConsoleTransportKind::TcpTelnet);
    h.connect_stream_console("c2", ConsoleTransportKind::TcpTelnet);
    let before = h.tx_effect_count();
    let fx = h.input("c2", b"ls\r");
    assert!(start_ops(&fx).is_empty());
    assert!(h
        .warns()
        .iter()
        .any(|w| w.contains("client_id=c2") && w.contains("error_code=SESSION_NOT_READY")));
    assert_eq!(h.tx_effect_count(), before);
}

#[test]
fn agent_command_suspends_and_resume_replays() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    h.set_auto_complete_human_tx(false);
    h.attach_pty_console("c1", true);
    assert_eq!(owner(&h).as_deref(), Some("human:c1"));
    let (adm, fx) = h.fg_begin(true);
    assert!(adm.suspended_human);
    assert!(human_txs(&fx).is_empty());
    let fx = h.input("c1", b"x");
    assert!(human_txs(&fx).is_empty(), "fg 期間無 HumanRaw");
    let fx = h.fg_finish(adm);
    assert_eq!(human_txs(&fx), vec![(TxClass::HumanReplay, "human:c1".to_string(), b"x".to_vec())]);
    let permit = h.last_human_permit().expect("回放 permit");
    assert_eq!(permit.input_epoch, Some(h.core.generations().input_epoch));
    let hold = permit.replay_hold.expect("D-22 hold");
    assert_eq!(h.fence_replay_auth(), Some(hold));
    assert_eq!(owner(&h).as_deref(), Some("human:c1"));
    assert!(h.foreground_busy(), "回放結算前前景忙（D-22(2)）");
    h.complete_human_tx(TxOutcome::Completed(1), None);
    assert!(!h.foreground_busy());
    assert_eq!(h.fence_replay_auth(), None);
    assert_eq!(h.replay_hold(), None);
}

#[test]
fn gate_closed_input_replayed_on_finish() {
    let mut h = ready_with_tcp(&["c1"]);
    let (adm, _) = h.fg_begin(false);
    assert!(!adm.suspended_human);
    let fx = h.input("c1", b"y");
    assert!(human_txs(&fx).is_empty());
    assert_eq!(h.deferred_len("c1"), 1);
    let fx = h.fg_finish(adm);
    assert_eq!(human_txs(&fx), vec![(TxClass::HumanReplay, "human:c1".to_string(), b"y".to_vec())]);
}

#[test]
fn admission_toggle_does_not_bump_owner_gen() {
    let mut h = ready_with_tcp(&["c1"]);
    let g = h.core.generations().owner_gen;
    let (adm, _) = h.fg_begin(false);
    h.input("c1", b"y");
    let fx = h.fg_finish(adm);
    assert_eq!(h.core.generations().owner_gen, g, "D-02：admission 切換不遞增 owner_gen");
    assert_eq!(h.fence.with(|f| f.owner_gen), g);
    assert_eq!(human_txs(&fx).len(), 1);
    assert!(!h.warns().iter().any(|w| w.contains("human TX")), "回放未被 OwnerMismatch 擋下");
    assert_eq!(h.deferred_len("c1"), 0);
}

#[test]
fn banner_invalidates_deferred_and_bumps_epoch() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    h.attach_pty_console("c1", true);
    let e0 = h.core.generations().input_epoch;
    let (adm, _) = h.fg_begin(true);
    h.input("c1", b"Z");
    h.invalidate(InvalidateReason::BootBanner);
    let fx = h.fg_finish(adm);
    assert!(human_txs(&fx).is_empty());
    assert_eq!(h.core.generations().input_epoch, e0 + 1);
    assert_eq!(h.fence.with(|f| f.input_epoch), e0 + 1);
    assert_eq!(h.input_epoch_pub(), e0 + 1);
    assert!(h.warns().iter().any(|w| w.contains("reason=boot_banner")));
}

#[test]
fn drain_phase1_resets_console_state_keeps_consoles() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    h.attach_pty_console("c1", true);
    let (_adm, _) = h.fg_begin(true);
    h.input("c1", b"q");
    let e0 = h.core.generations().input_epoch;
    h.begin_stop(StopKind::Recover);
    let hc = h.core.human_console();
    assert!(hc.consoles.contains_key("c1"), "consoles 保留");
    assert_eq!(
        hc.ownership,
        sw_core::human_console::ownership::OwnershipState::default()
    );
    assert_eq!(h.deferred_len("c1"), 0);
    assert_eq!(h.core.generations().input_epoch, e0 + 1);
    assert_eq!(h.core.retained_human_owner(), Some("human:c1"));
}

#[test]
fn release_phase1_closes_all_consoles() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    h.attach_pty_console("c1", true);
    let (rx, fx) = h.rpc_deferred(SessionRequest::Release { source: "agent".into(), reason: PyValue::Null });
    assert_eq!(fx.iter().filter(|e| matches!(e, Effect::ConsoleCloseAll)).count(), 1);
    assert!(h.core.human_console().consoles.is_empty());
    let (epoch, stop) = h.pending_stop();
    h.receipt(epoch, stop).feed();
    let resp = rx.try_recv().unwrap().unwrap();
    assert_eq!(resp["closed_consoles"], PyValue::from(1));
}

#[test]
fn input_without_active_epoch_dropped_sc31() {
    let mut h = ready_with_tcp(&["c1"]);
    h.begin_stop(StopKind::Recover);
    assert_eq!(h.fence.with(|f| f.active_epoch), None);
    let before = h.tx_effect_count();
    let fx = h.input("c1", b"R");
    assert!(human_txs(&fx).is_empty());
    assert_eq!(h.deferred_len("c1"), 0);
    assert!(h.warns().iter().any(|w| w.contains("client_id=c1") && w.contains("reason=no_active_epoch")));
    assert_eq!(h.tx_effect_count(), before);
}

#[test]
fn owner_change_updates_fence_before_tx() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    let g = h.fence.with(|f| f.owner_gen);
    h.connect_stream_console("c1", ConsoleTransportKind::TcpTelnet); // owner None→human:c1
    let fx = h.input("c1", b"a");
    let permit = fx
        .iter()
        .find_map(|e| match e {
            Effect::HumanTx { permit, .. } => Some(permit.clone()),
            _ => None,
        })
        .unwrap();
    assert_eq!(permit.owner_gen, g + 1);
    assert_eq!(h.fence.with(|f| f.owner_gen), g + 1);
}

#[test]
fn second_console_while_lease_gets_interactive_busy() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    h.attach_pty_console("c1", true);
    h.connect_stream_console("c2", ConsoleTransportKind::TcpTelnet);
    let before = h.tx_effect_count();
    let fx = h.input("c2", b"ls\r");
    assert!(start_ops(&fx).is_empty());
    let row = h.last_human_command().expect("console 行已進 registry");
    assert_eq!(row["error_code"], PyValue::from("SESSION_INTERACTIVE_BUSY"));
    assert_eq!(h.tx_effect_count(), before);
}

#[test]
fn flash_drops_input_diagnosed() {
    let mut h = ready_with_tcp(&["c1"]);
    let (_adm, _) = h.fg_begin(false);
    h.input("c1", b"a");
    assert_eq!(h.deferred_len("c1"), 1);
    h.core.human_console_mut().ownership.flash_mode = true;
    let before = h.tx_effect_count();
    let fx = h.input("c1", b"b");
    assert!(human_txs(&fx).is_empty());
    assert!(h.warns().iter().any(|w| w.contains("client_id=c1") && w.contains("reason=flash")));
    assert_eq!(h.deferred_len("c1"), 1, "flash 輸入不進 deferred");
    h.invalidate(InvalidateReason::Flash);
    assert_eq!(h.deferred_len("c1"), 0);
    assert_eq!(h.tx_effect_count(), before);
}

#[test]
fn self_test_probe_suspends_human_without_admission() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    h.attach_pty_console("c1", true);
    let (_rx, fx) = h.rpc_deferred(SessionRequest::SelfTest { timeout_s: 2.0, strict_human_lock: false });
    let grant = fx
        .iter()
        .find_map(|e| match e {
            Effect::StartOp { grant, spec: OpSpec::SelfTestProbe { .. } } => Some(grant.clone()),
            _ => None,
        })
        .expect("StartOp(SelfTestProbe)");
    let fx = h.input("c1", b"k");
    assert!(human_txs(&fx).is_empty(), "probe 期間無 HumanRaw");
    assert!(!h.fence.with(|f| f.human_admission_blocked), "self-test 只 suspend、不動 admission");
    let epoch = h.epoch();
    let fx = h.feed(ControlEvent::OpFinished {
        op_id: grant.op_id,
        epoch,
        outcome: OpOutcome::SelfTest(pydict! { "probe_ok" => true, "rx_bytes_last_10s" => 0 }),
        after_rx_seq: None,
    });
    assert_eq!(human_txs(&fx), vec![(TxClass::HumanReplay, "human:c1".to_string(), b"k".to_vec())]);
    let order = h.tx_order();
    let probe = order.iter().position(|(c, _)| *c == TxClass::SelfTestProbe).expect("probe");
    let replay = order.iter().position(|(c, _)| *c == TxClass::HumanReplay).expect("replay");
    assert!(probe < replay, "回放位於 SelfTestProbe 之後");
}

#[test]
fn last_line_before_disconnect_is_submitted() {
    let mut h = ready_with_tcp(&["c1", "c2"]);
    h.input("c2", b"reboot\r");
    h.disconnect("c2");
    let row = h.last_human_command().expect("reboot 已提交");
    assert_eq!(row["command"], PyValue::from("reboot"));
    assert_eq!(row["source"], PyValue::from("human:c2"));
}

#[test]
fn queued_command_waits_for_replay_settle() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    h.set_auto_complete_human_tx(false);
    h.attach_pty_console("c1", true);
    let a = h.start_foreground_command("echo a");
    h.input("c1", b"x");
    let b = h
        .rpc(SessionRequest::Submit(agent_submit("echo b")))
        .unwrap()["cmd_id"]
        .as_str()
        .unwrap()
        .to_string();
    let epoch = h.epoch();
    let fx = h.feed(ControlEvent::OpFinished {
        op_id: a.op_id,
        epoch,
        outcome: OpOutcome::CommandResult(pydict! { "ok" => true, "stdout" => "a" }),
        after_rx_seq: None,
    });
    assert_eq!(human_txs(&fx), vec![(TxClass::HumanReplay, "human:c1".to_string(), b"x".to_vec())]);
    assert!(start_ops(&fx).is_empty(), "回放結算前 B 不出列");
    assert_eq!(h.reg.get(&b).unwrap()["status"], PyValue::from("accepted"));
    assert!(h.foreground_busy());
    let fx = h.complete_human_tx(TxOutcome::Completed(1), None);
    assert_eq!(start_ops(&fx).len(), 1, "結算後 B 才出列");
    match start_ops(&fx)[0] {
        OpSpec::Command(q) => assert_eq!(q.cmd_id, b, "出列的是 B"),
        _ => panic!("應為 OpSpec::Command"),
    }
    assert_eq!(
        h.tx_order(),
        vec![
            (TxClass::Command, b"echo a\n".to_vec()),
            (TxClass::HumanReplay, b"x".to_vec()),
            (TxClass::Command, b"echo b\n".to_vec()),
        ]
    );
}

#[test]
fn foreground_busy_tracks_foreground_without_hold() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    assert!(!h.foreground_busy());
    let a = h.start_foreground_command("echo a");
    assert!(h.foreground_busy());
    let epoch = h.epoch();
    h.feed(ControlEvent::OpFinished {
        op_id: a.op_id,
        epoch,
        outcome: OpOutcome::CommandResult(pydict! { "ok" => true, "stdout" => "a" }),
        after_rx_seq: None,
    });
    assert!(!h.foreground_busy());
}

#[test]
fn console_disconnect_drops_owner_and_deferred() {
    let mut h = ready_with_tcp(&["c1"]);
    let (_adm, _) = h.fg_begin(false);
    h.input("c1", b"z");
    assert_eq!(h.deferred_len("c1"), 1);
    h.disconnect("c1");
    assert!(!h.core.human_console().consoles.contains_key("c1"));
    assert_eq!(h.deferred_len("c1"), 0);
    assert_eq!(owner(&h), None);
}

#[test]
fn reboot_command_invalidates_before_send() {
    // 增補 #13：`bridge.invalidate_deferred("reboot")` 在送出 reboot 之前（session_manager.py `_execute_command_inner`）。
    let mut h = ready_with_tcp(&["c1"]);
    let e0 = h.core.generations().input_epoch;
    let started = h.start_foreground_command("reboot");
    let _ = started;
    assert_eq!(h.core.generations().input_epoch, e0 + 1, "StartOp(reboot) 時 input_epoch 已遞增");
    assert_eq!(h.fence.with(|f| f.input_epoch), e0 + 1);
    let life = h.lifetime("c1");
    let fx = h.input_with("c1", life, e0, b"k");
    assert!(human_txs(&fx).is_empty(), "reboot 前擷取的輸入一律 stale");
}

// ---------------------------------------------------------------------------
// p6-cloud 審查回歸（ac46a0e..1f1fa67）
// ---------------------------------------------------------------------------

/// High（1f1fa67）：reboot 送出後 arm quiet 時再失效一次（Python `arm_boot_quiet` → `invalidate_deferred("boot_quiet")`）。
#[test]
fn reboot_tx_settled_invalidates_again_on_quiet_arm() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    h.attach_pty_console("c1", true);
    let e0 = h.core.generations().input_epoch;
    let started = h.start_foreground_command("reboot");
    assert_eq!(h.core.generations().input_epoch, e0 + 1);
    h.input("c1", b"k"); // agent 命令 suspend 中 → deferred（epoch e0+1）
    assert_eq!(h.deferred_len("c1"), 1);
    let epoch = h.epoch();
    h.feed(ControlEvent::UartTxCompleted {
        epoch,
        op_id: started.op_id,
        confirmed_bytes: 7,
        unknown: false,
    });
    assert_eq!(h.core.generations().input_epoch, e0 + 2, "arm quiet 為第二個失效點");
    assert_eq!(h.fence.with(|f| f.input_epoch), e0 + 2);
    assert_eq!(h.deferred_len("c1"), 0, "reboot 在途期間的按鍵被清掉");
    let fx = h.feed(ControlEvent::OpFinished {
        op_id: started.op_id,
        epoch,
        outcome: OpOutcome::RebootExpected,
        after_rx_seq: None,
    });
    assert!(
        human_txs(&fx).iter().all(|(_, _, p)| p != b"k"),
        "resume 不得回放 k：{:?}",
        human_txs(&fx)
    );
}

/// 對照：reboot TX 完全未送出（confirmed 0、非 unknown）時不 arm、不做第二次失效。
#[test]
fn reboot_tx_not_submitted_no_second_invalidation() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    h.attach_pty_console("c1", true);
    let e0 = h.core.generations().input_epoch;
    let started = h.start_foreground_command("reboot");
    h.input("c1", b"k");
    let epoch = h.epoch();
    h.feed(ControlEvent::UartTxCompleted {
        epoch,
        op_id: started.op_id,
        confirmed_bytes: 0,
        unknown: false,
    });
    assert_eq!(h.core.generations().input_epoch, e0 + 1);
    assert_eq!(h.deferred_len("c1"), 1);
    assert!(!h.fence.with(|f| f.quiet_active));
}

/// Medium（2f4f76a）：interactive 模式先關 admission 再開 lease（Python `_begin_foreground_operation_locked`
/// 先於 `_open_interactive_locked`）；第二個 console 的 interactive 命令只把自己記為 pending owner。
#[test]
fn interactive_line_command_does_not_steal_raw_owner() {
    let mut h = ready_with_tcp(&["c1", "c2"]);
    assert_eq!(owner(&h).as_deref(), Some("human:c1"));
    let fx = h.input("c2", b"vi\r");
    assert_eq!(start_ops(&fx).len(), 1, "interactive 命令出列");
    assert_eq!(owner(&h).as_deref(), Some("human:c1"), "raw owner 仍是 c1");
    assert_eq!(
        h.core.human_console().ownership.pending_owner.as_deref(),
        Some("human:c2")
    );
    // c1 的按鍵仍以 raw 路徑處理（admission 關 → defer），不會走 line editor 成為命令。
    let before = h.reg.records_for_test().len();
    h.input("c1", b"ls\r");
    assert_eq!(h.reg.records_for_test().len(), before, "c1 的輸入不得被當成命令送出");
}

/// Medium（2f4f76a）：空命令的 interactive 也要成對 begin／finish（admission 恢復開放）。
#[test]
fn empty_interactive_command_reopens_admission() {
    let mut h = ready_with_tcp(&["c1"]);
    let (resp, _fx) = h.rpc_full(SessionRequest::Submit({
        let mut r = agent_submit("");
        r.mode = "interactive".into();
        r
    }));
    assert_eq!(resp.unwrap().get("ok"), Some(&PyValue::Bool(true)));
    assert!(!h.fence.with(|f| f.human_admission_blocked), "admission 應恢復開放");
    assert!(!h.core.human_console().ownership.admission_blocked);
}

/// Low（2f4f76a）：drain Phase 1 時在途的 human TX，若 report 仍屬現行 conn_epoch，晚到的結算照樣送 SettleTx（D-15(6)）。
#[test]
fn phase1_in_flight_human_tx_still_settles_ledger() {
    let mut h = ready_with_tcp(&["c1"]);
    h.set_auto_complete_human_tx(false);
    h.input("c1", b"a");
    let permit = h.last_human_permit().unwrap();
    h.begin_stop(StopKind::IoError);
    assert_eq!(h.epoch(), permit.epoch, "Phase 1 期間 conn_epoch 仍為舊值");
    let fx = h.complete_human_tx(TxOutcome::Completed(1), None);
    assert!(
        fx.iter().any(|e| matches!(e, Effect::SettleTx { op_id, .. } if *op_id == permit.op_id)),
        "晚到結算仍須結算帳本"
    );
    assert!(human_txs(&fx).is_empty(), "不得因此送出新的 human TX");
}
