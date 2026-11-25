//! p279：guard 關閉時共同 raw 行為與修前 Rust ownership 路徑對照。
//!
//! 本檔只記錄修前／本包皆適用的 ownership、suspend、admission、deferred、TX 與輸入路由。
//! mirror lease、soft_preempted 回應與 LeaseIndex 差異由 tcp_console_lease.rs 的套件專屬測試驗證。
#![cfg(feature = "test-fakes")]
mod support;

use std::time::Duration;

use support::*;
use sw_core::human_console::ownership::InputRoute;
use sw_core::human_console::ConsoleTransportKind;
use sw_core::mailbox::ControlEvent;
use sw_core::op_runner::OpOutcome;
use sw_core::session_core::{Effect, SessionRequest};
use sw_core::tx_writer::TxClass;
use sw_pal::traits::TxOutcome;
use sw_proto::{pydict, pyjson::PyValue};

/// cc1746d8 的 TCP accept 不建立 lease；用一般 human lease 配出與 HEAD 相同的
/// agent soft-preempt 前置。HEAD 已有 TCP mirror 時保留該 mirror，差異只屬套件層簿記。
fn align_human_lease_precondition(h: &mut CoreHarness) {
    if h.core.active_lease().is_none() {
        h.rpc(open_req("human:a", 60.0, "", false))
            .expect("修前基線以一般 human lease 建立相同 soft-preempt 前置");
    }
    assert_eq!(h.core.active_lease().map(|lease| lease.owner.as_str()), Some("human:a"));
    assert_eq!(
        h.core.human_console().ownership.interactive_owner.as_deref(),
        Some("human:a"),
        "共同前置為 TCP A raw owner",
    );
}

fn setup_soft_preempt(timeout_s: f64) -> (CoreHarness, String) {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    h.connect_stream_console("a", ConsoleTransportKind::TcpTelnet);
    align_human_lease_precondition(&mut h);
    h.connect_stream_console("c", ConsoleTransportKind::TcpTelnet);

    let opened = h
        .rpc(open_req("agent:r7-baseline", timeout_s, "", false))
        .expect("兩邊相同的 human lease 前置應允許 agent soft-preempt");
    assert_eq!(opened.get("soft_preempted"), Some(&PyValue::Bool(true)));
    let agent_id = opened
        .get("interactive_id")
        .and_then(PyValue::as_str)
        .expect("agent interactive id")
        .to_string();
    assert_eq!(
        h.core.human_console().ownership.interactive_owner.as_deref(),
        Some("agent:r7-baseline"),
    );
    assert_eq!(h.core.human_console().ownership.suspended_owner.as_deref(), Some("human:a"));
    assert_eq!(h.core.human_console().ownership.suspend_depth, 1);
    assert!(h.core.human_console().ownership.agent_active);
    trace_common("soft-preempt", &h);
    (h, agent_id)
}

fn assert_no_start_op(effects: &[Effect]) {
    assert!(
        effects.iter().all(|effect| !matches!(effect, Effect::StartOp { .. })),
        "raw／line 路由對照不得啟動 UART operation",
    );
}

fn tx_payloads(h: &CoreHarness, class: TxClass) -> Vec<String> {
    h.human_tx()
        .into_iter()
        .filter(|(tx_class, _, _)| *tx_class == class)
        .map(|(_, source, payload)| {
            format!("{source}:{}", String::from_utf8_lossy(&payload))
        })
        .collect()
}

/// 供 cc1746d8／HEAD log 逐行比對的共同層觀測；刻意不輸出 lease 或 LeaseIndex。
fn trace_common(phase: &str, h: &CoreHarness) {
    let ownership = &h.core.human_console().ownership;
    eprintln!(
        "R7-COMMON phase={phase} owner={:?} suspended={:?} pending={:?} depth={} agent_active={} admission_blocked={} deferred_a={} deferred_b={} route_a={:?} route_b={:?} route_c={:?} replay={:?} raw_tx={:?}",
        ownership.interactive_owner,
        ownership.suspended_owner,
        ownership.pending_owner,
        ownership.suspend_depth,
        ownership.agent_active,
        ownership.admission_blocked,
        h.deferred_len("a"),
        h.deferred_len("b"),
        ownership.peek_route("a"),
        ownership.peek_route("b"),
        ownership.peek_route("c"),
        tx_payloads(h, TxClass::HumanReplay),
        tx_payloads(h, TxClass::HumanRaw),
    );
}

fn expiry_while_flash_guard_is_closed() {
    let (mut h, _agent_id) = setup_soft_preempt(1.0);
    let deferred = h.input("a", b"expiry-deferred");
    assert_no_start_op(&deferred);
    assert_eq!(h.deferred_len("a"), b"expiry-deferred".len());
    assert_eq!(h.core.human_console().ownership.peek_route("a"), InputRoute::Defer);

    h.fence.with(|fence| fence.flash_critical = true);
    let sync = h.scan(None);
    assert_no_start_op(&sync);
    trace_common("expiry-guard-closed-before-expiry", &h);

    let expired = h.advance(Duration::from_secs(2));
    assert_no_start_op(&expired);
    assert_eq!(h.core.human_console().ownership.interactive_owner.as_deref(), Some("human:a"));
    assert_eq!(h.core.human_console().ownership.suspended_owner, None);
    assert_eq!(h.core.human_console().ownership.suspend_depth, 0);
    assert!(!h.core.human_console().ownership.agent_active);
    assert_eq!(
        tx_payloads(&h, TxClass::HumanReplay),
        vec!["human:a:expiry-deferred"],
        "guard 關閉期間 expiry 的 replay 沿修前 raw 行為恰發生一次",
    );
    trace_common("expiry-settled-under-flash-guard", &h);

    let raw = h.input("a", b"expiry-raw\r\n");
    assert_no_start_op(&raw);
    assert!(raw.iter().any(|effect| matches!(
        effect,
        Effect::HumanTx { permit, payload, .. }
            if permit.class == TxClass::HumanRaw && payload == b"expiry-raw\r\n"
    )));
    assert_eq!(h.core.human_console().ownership.peek_route("a"), InputRoute::Raw { owner: "human:a".into() });
    assert_eq!(h.core.human_console().ownership.peek_route("c"), InputRoute::Line);
    trace_common("expiry-raw-and-line-routes", &h);
}

fn eof_before_agent_close() {
    let (mut h, agent_id) = setup_soft_preempt(60.0);
    let old_input = h.input("a", b"old-a");
    assert_no_start_op(&old_input);
    assert_eq!(h.deferred_len("a"), b"old-a".len());

    let eof = h.disconnect("a");
    assert_no_start_op(&eof);
    assert_eq!(h.deferred_len("a"), 0, "EOF 先清除 A 的 deferred bytes");
    trace_common("eof-before-close", &h);

    h.rpc(close_req(&agent_id)).expect("EOF 後關閉 agent lease");
    assert_eq!(h.core.human_console().ownership.interactive_owner, None);
    assert_eq!(h.core.human_console().ownership.suspended_owner, None);
    assert_eq!(h.core.human_console().ownership.suspend_depth, 0);
    assert!(!h.core.human_console().ownership.agent_active);
    assert_eq!(h.deferred_len("a"), 0);
    assert!(tx_payloads(&h, TxClass::HumanReplay).is_empty(), "A EOF 後不得回放其 deferred bytes");
    trace_common("eof-owner-cleared-after-close", &h);
    assert_eq!(h.core.human_console().ownership.peek_route("c"), InputRoute::Line);
}

fn handoff_freezes_before_close_eof_and_replacement_accept() {
    let (mut h, agent_id) = setup_soft_preempt(60.0);
    let deferred = h.input("a", b"handoff-deferred");
    assert_no_start_op(&deferred);
    assert_eq!(h.deferred_len("a"), b"handoff-deferred".len());

    // permanent handoff guard 在 agent close、A EOF 與 B accept 前成立。
    h.reg.admission_gate().freeze();
    h.fence.with(|fence| fence.handoff_frozen = true);
    let sync = h.scan(None);
    assert_no_start_op(&sync);
    trace_common("handoff-frozen-before-close-eof-b", &h);

    h.rpc(close_req(&agent_id)).expect("handoff guard 期間既有 agent lease 仍可 close");
    assert_eq!(h.core.human_console().ownership.interactive_owner.as_deref(), Some("human:a"));
    assert_eq!(h.core.human_console().ownership.suspend_depth, 0);
    trace_common("handoff-closed-agent-before-eof-b", &h);

    let eof = h.disconnect("a");
    assert_no_start_op(&eof);
    h.connect_stream_console("b", ConsoleTransportKind::TcpTelnet);
    assert_eq!(h.core.human_console().ownership.interactive_owner, None);
    assert_eq!(h.core.human_console().ownership.peek_route("b"), InputRoute::Line);
    assert_eq!(h.core.human_console().ownership.peek_route("c"), InputRoute::Line);
    trace_common("handoff-eof-a-then-accept-b", &h);

    let input = h.input("b", b"handoff-raw\r\n");
    assert!(input.is_empty(), "permanent handoff guard 的 input event 沿修前基線被丟棄");
    assert_no_start_op(&input);
    assert_eq!(h.deferred_len("b"), 0);
    trace_common("handoff-b-input-under-freeze", &h);
}

fn start_self_test(h: &mut CoreHarness) -> sw_core::drain::OpId {
    let (_reply, effects) = h.rpc_deferred(SessionRequest::SelfTest {
        timeout_s: 2.0,
        strict_human_lock: false,
    });
    effects
        .iter()
        .find_map(|effect| match effect {
            Effect::StartOp {
                grant,
                spec: sw_core::op_runner::OpSpec::SelfTestProbe { .. },
            } => Some(grant.op_id),
            _ => None,
        })
        .expect("非 strict self-test 應啟動 probe")
}

fn finish_self_test(h: &mut CoreHarness, op_id: sw_core::drain::OpId) -> Vec<Effect> {
    h.feed(ControlEvent::OpFinished {
        op_id,
        epoch: h.epoch(),
        outcome: OpOutcome::SelfTest(pydict! {
            "probe_ok" => true,
            "rx_bytes_last_10s" => 0,
        }),
        after_rx_seq: None,
    })
}

fn explicit_close_during_self_test() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    h.connect_stream_console("a", ConsoleTransportKind::TcpTelnet);
    align_human_lease_precondition(&mut h);
    h.set_auto_complete_human_tx(false);
    let id = h.core.active_lease().expect("共同 human lease").interactive_id.clone();
    let probe = start_self_test(&mut h);
    let deferred = h.input("a", b"explicit-close-deferred");
    assert_no_start_op(&deferred);
    assert_eq!(h.deferred_len("a"), b"explicit-close-deferred".len());
    trace_common("explicit-close-self-test-suspended", &h);

    let closed = h.rpc(close_req(&id)).expect("明確 interactive close");
    assert_eq!(closed.get("ok"), Some(&PyValue::Bool(true)));
    assert_eq!(h.core.human_console().ownership.suspend_depth, 1);
    trace_common("explicit-close-after-close", &h);

    let refreshed = h.scan(None);
    assert_no_start_op(&refreshed);
    assert_eq!(h.core.human_console().ownership.suspend_depth, 1);
    trace_common("explicit-close-after-refresh", &h);

    let resumed = finish_self_test(&mut h, probe);
    assert_eq!(
        resumed.iter().filter(|effect| matches!(effect, Effect::HumanTx { permit, payload, .. } if permit.class == TxClass::HumanReplay && payload == b"explicit-close-deferred")).count(),
        1,
        "self-test completion follows existing deferred replay path once",
    );
    assert_eq!(h.core.human_console().ownership.suspend_depth, 0);
    assert_eq!(h.core.human_console().ownership.interactive_owner.as_deref(), Some("human:a"));
    trace_common("explicit-close-after-resume", &h);
    h.complete_human_tx(TxOutcome::Completed(b"explicit-close-deferred".len()), None);

    let raw = h.input("a", b"explicit-close-raw");
    assert_no_start_op(&raw);
    assert!(raw.iter().any(|effect| matches!(effect, Effect::HumanTx { permit, payload, .. } if permit.class == TxClass::HumanRaw && payload == b"explicit-close-raw")));
    trace_common("explicit-close-raw-input", &h);
}

fn explicit_console_detach_during_self_test() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    h.connect_stream_console("a", ConsoleTransportKind::TcpTelnet);
    align_human_lease_precondition(&mut h);
    let probe = start_self_test(&mut h);
    let deferred = h.input("a", b"detach-deferred");
    assert_no_start_op(&deferred);
    assert_eq!(h.deferred_len("a"), b"detach-deferred".len());
    trace_common("console-detach-self-test-suspended", &h);

    let detached = h
        .rpc(SessionRequest::ConsoleDetach { client_id: "a".into() })
        .expect("明確 console detach");
    assert_eq!(detached.get("ok"), Some(&PyValue::Bool(true)));
    assert_eq!(h.core.human_console().ownership.suspend_depth, 0);
    assert_eq!(h.deferred_len("a"), 0);
    trace_common("console-detach-after-detach", &h);

    let resumed = finish_self_test(&mut h, probe);
    assert!(resumed.iter().all(|effect| !matches!(effect, Effect::HumanTx { .. })));
    assert_eq!(h.core.human_console().ownership.suspend_depth, 0);
    assert!(h.core.human_console().ownership.interactive_owner.is_none());
    trace_common("console-detach-after-probe-complete", &h);
}

fn close_scan_self_heal_then_self_test() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    h.connect_stream_console("a", ConsoleTransportKind::TcpTelnet);
    align_human_lease_precondition(&mut h);
    h.set_auto_complete_human_tx(false);

    let id = h.core.active_lease().expect("TCP human lease").interactive_id.clone();
    let closed = h.rpc(close_req(&id)).expect("明確 close TCP human lease");
    assert_eq!(closed.get("ok"), Some(&PyValue::Bool(true)));
    trace_common("close-scan-self-test-after-close", &h);

    let scanned = h.scan(None);
    assert_no_start_op(&scanned);
    assert_eq!(h.core.human_console().ownership.interactive_owner.as_deref(), Some("human:a"));
    trace_common("close-scan-self-test-after-hub-scan", &h);

    let probe = start_self_test(&mut h);
    assert_eq!(h.core.human_console().ownership.suspend_depth, 1);
    assert_eq!(h.core.human_console().ownership.suspended_owner.as_deref(), Some("human:a"));
    trace_common("close-scan-self-test-after-probe-start", &h);

    let input = h.input("a", b"r9-self-test-deferred");
    assert_no_start_op(&input);
    assert!(input.iter().all(|effect| !matches!(effect, Effect::HumanTx { .. })));
    assert_eq!(h.deferred_len("a"), b"r9-self-test-deferred".len());
    trace_common("close-scan-self-test-during-probe", &h);

    let resumed = finish_self_test(&mut h, probe);
    assert_eq!(
        resumed.iter().filter(|effect| matches!(effect, Effect::HumanTx { permit, payload, .. }
            if permit.class == TxClass::HumanReplay && payload == b"r9-self-test-deferred")).count(),
        1,
        "self-test 完成後 deferred bytes 恰回放一次",
    );
    assert_eq!(h.deferred_len("a"), 0);
    assert_eq!(h.core.human_console().ownership.suspend_depth, 0);
    assert_eq!(h.core.human_console().ownership.interactive_owner.as_deref(), Some("human:a"));
    trace_common("close-scan-self-test-after-resume", &h);
    h.complete_human_tx(TxOutcome::Completed(b"r9-self-test-deferred".len()), None);

    let raw = h.input("a", b"r9-self-test-raw");
    assert_no_start_op(&raw);
    assert_eq!(
        raw.iter().filter(|effect| matches!(effect, Effect::HumanTx { permit, payload, .. }
            if permit.class == TxClass::HumanRaw && payload == b"r9-self-test-raw")).count(),
        1,
        "self-test 完成後 TCP 輸入回到 raw 路由",
    );
    trace_common("close-scan-self-test-after-raw-input", &h);
}

fn close_scan_self_heal_then_agent_command() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    h.connect_stream_console("a", ConsoleTransportKind::TcpTelnet);
    align_human_lease_precondition(&mut h);
    h.set_auto_complete_human_tx(false);

    let id = h.core.active_lease().expect("TCP human lease").interactive_id.clone();
    let closed = h.rpc(close_req(&id)).expect("明確 close TCP human lease");
    assert_eq!(closed.get("ok"), Some(&PyValue::Bool(true)));
    trace_common("close-scan-agent-command-after-close", &h);

    let scanned = h.scan(None);
    assert_no_start_op(&scanned);
    assert_eq!(h.core.human_console().ownership.interactive_owner.as_deref(), Some("human:a"));
    trace_common("close-scan-agent-command-after-hub-scan", &h);

    let command = h.start_foreground_command("echo r9-agent-command");
    assert_eq!(h.core.human_console().ownership.suspend_depth, 1);
    assert_eq!(h.core.human_console().ownership.suspended_owner.as_deref(), Some("human:a"));
    trace_common("close-scan-agent-command-after-start", &h);

    let input = h.input("a", b"r9-agent-deferred");
    assert_no_start_op(&input);
    assert!(input.iter().all(|effect| !matches!(effect, Effect::HumanTx { .. })));
    assert_eq!(h.deferred_len("a"), b"r9-agent-deferred".len());
    trace_common("close-scan-agent-command-during-command", &h);

    let resumed = h.feed(ControlEvent::OpFinished {
        op_id: command.op_id,
        epoch: h.epoch(),
        outcome: OpOutcome::CommandResult(pydict! { "ok" => true, "stdout" => "" }),
        after_rx_seq: None,
    });
    assert_eq!(
        resumed.iter().filter(|effect| matches!(effect, Effect::HumanTx { permit, payload, .. }
            if permit.class == TxClass::HumanReplay && payload == b"r9-agent-deferred")).count(),
        1,
        "agent command 完成後 deferred bytes 恰回放一次",
    );
    assert_eq!(h.deferred_len("a"), 0);
    assert_eq!(h.core.human_console().ownership.suspend_depth, 0);
    assert_eq!(h.core.human_console().ownership.interactive_owner.as_deref(), Some("human:a"));
    trace_common("close-scan-agent-command-after-resume", &h);
    h.complete_human_tx(TxOutcome::Completed(b"r9-agent-deferred".len()), None);

    let raw = h.input("a", b"r9-agent-raw");
    assert_no_start_op(&raw);
    assert_eq!(
        raw.iter().filter(|effect| matches!(effect, Effect::HumanTx { permit, payload, .. }
            if permit.class == TxClass::HumanRaw && payload == b"r9-agent-raw")).count(),
        1,
        "agent command 完成後 TCP 輸入回到 raw 路由",
    );
    trace_common("close-scan-agent-command-after-raw-input", &h);
}

#[test]
fn guard_off_tcp_raw_matches_pre_package_baseline() {
    // mirror 簿記的套件專屬斷言位於 tcp_console_lease.rs；以下共同觀測必須在 cc1746d8 與 HEAD 逐行相同。
    expiry_while_flash_guard_is_closed();
    eof_before_agent_close();
    handoff_freezes_before_close_eof_and_replacement_accept();
    explicit_close_during_self_test();
    explicit_console_detach_during_self_test();
}

#[test]
fn guard_off_tcp_self_heal_preserves_suspend_and_agent_baseline() {
    close_scan_self_heal_then_self_test();
    close_scan_self_heal_then_agent_command();
}
