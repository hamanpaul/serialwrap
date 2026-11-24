//! p279：guard 關閉時 TCP raw 路由與修前 Rust ownership 路徑對照。
#![cfg(feature = "test-fakes")]
mod support;

use support::*;
use sw_core::human_console::ownership::InputRoute;
use sw_core::human_console::ConsoleTransportKind;
use sw_core::mailbox::ControlEvent;
use sw_core::op_runner::{OpOutcome, OpSpec};
use sw_core::session_core::{Effect, SessionRequest};
use sw_core::tx_writer::TxClass;
use sw_proto::pydict;
use sw_pal::traits::TxOutcome;

#[derive(Clone, Copy)]
enum GuardKind {
    Flash,
    Handoff,
}

fn run_guard_off_raw_case(guard: GuardKind) {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    h.set_auto_complete_human_tx(false);
    h.connect_stream_console("a", ConsoleTransportKind::TcpTelnet);

    let (_, effects) = h.rpc_deferred(SessionRequest::SelfTest {
        timeout_s: 2.0,
        strict_human_lock: false,
    });
    let probe_op = effects
        .iter()
        .find_map(|effect| match effect {
            Effect::StartOp {
                grant,
                spec: OpSpec::SelfTestProbe { .. },
            } => Some(grant.op_id),
            _ => None,
        })
        .expect("non-strict self-test probe");
    let opened = h
        .rpc(open_req("agent:r5-nested", 1.0, "", false))
        .expect("agent soft preempt");
    assert_eq!(opened.get("soft_preempted"), Some(&true.into()));
    let agent_id = opened["interactive_id"].as_str().unwrap().to_string();

    if matches!(guard, GuardKind::Flash) {
        h.fence.with(|fence| fence.flash_critical = true);
    }
    h.rpc(close_req(&agent_id)).expect("agent soft-preempt 沿既有 close/resume 路徑結算");
    assert_eq!(h.core.human_console().ownership.interactive_owner, None);
    assert_eq!(h.core.human_console().ownership.suspended_owner.as_deref(), Some("human:a"));
    assert!(h.core.human_console().ownership.agent_active);
    assert!(!h.core.human_console().ownership.admission_blocked);

    h.disconnect("a");
    assert_eq!(h.core.human_console().ownership.suspend_depth, 1, "EOF 依既有 on_drop 保留 self-test suspend");
    assert_eq!(h.core.human_console().ownership.suspended_owner, None);
    assert!(h.core.human_console().ownership.agent_active);
    h.connect_stream_console("b", ConsoleTransportKind::TcpTelnet);
    assert_eq!(
        h.core.human_console().ownership.interactive_owner.as_deref(),
        None,
        "B 在 self-test suspend 中等待既有 resume",
    );
    assert_eq!(h.core.human_console().ownership.suspended_owner.as_deref(), Some("human:b"));
    assert_eq!(h.core.human_console().ownership.suspend_depth, 1);
    assert!(h.core.human_console().ownership.agent_active);
    assert!(!h.core.human_console().ownership.admission_blocked);
    let deferred = h.input("b", b"deferred-before-resume");
    assert!(deferred.iter().all(|effect| !matches!(effect, Effect::HumanTx { .. })));
    assert_eq!(h.deferred_len("b"), b"deferred-before-resume".len());

    let finish = h.feed(ControlEvent::OpFinished {
        op_id: probe_op,
        epoch: h.epoch(),
        outcome: OpOutcome::SelfTest(pydict! {
            "probe_ok" => true,
            "rx_bytes_last_10s" => 0,
        }),
        after_rx_seq: None,
    });
    assert!(finish.iter().any(|effect| matches!(
        effect,
        Effect::HumanTx { permit, payload, .. }
            if permit.class == TxClass::HumanReplay && payload == b"deferred-before-resume"
    )), "self-test resume 沿既有路徑回放 deferred bytes");
    assert_eq!(h.core.human_console().ownership.interactive_owner.as_deref(), Some("human:b"));
    assert_eq!(h.core.human_console().ownership.suspended_owner, None);
    assert_eq!(h.core.human_console().ownership.suspend_depth, 0);
    assert!(!h.core.human_console().ownership.agent_active);
    assert_eq!(
        h.core.human_console().ownership.peek_route("b"),
        InputRoute::Raw { owner: "human:b".into() },
    );
    h.complete_human_tx(TxOutcome::Completed(b"deferred-before-resume".len()), None);

    let input = b"echo guard baseline\r\n";
    let effects = match guard {
        GuardKind::Flash => h.input("b", input),
        GuardKind::Handoff => {
            h.connect_stream_console("c", ConsoleTransportKind::TcpTelnet);
            let outcome = h.reg.admission_gate().freeze_if_idle("p279-guard-off-baseline");
            let sw_core::admission::FreezeOutcome::Frozen { token, created: true } = outcome else {
                panic!("handoff guard 必須由本測試建立：{outcome:?}");
            };
            assert!(h.reg.admission_gate().commit_handoff(&token), "交接成功後 guard 轉為 permanent");
            h.fence.with(|fence| fence.handoff_frozen = true);
            h.disconnect("c");
            assert!(h.core.active_lease().is_none(), "既有 disconnect/sync 路徑於 guard 關閉時撤除 mirror 簿記");
            assert_eq!(h.core.human_console().ownership.interactive_owner.as_deref(), Some("human:b"));
            assert_eq!(h.core.human_console().ownership.suspend_depth, 0);
            assert_eq!(
                h.core.human_console().ownership.peek_route("b"),
                InputRoute::Raw { owner: "human:b".into() },
                "mirror 撤簿不改變 B 的 raw route",
            );
            let closed_input = h.input("b", input);
            assert!(closed_input.is_empty(), "permanent handoff freeze 的輸入沿修前基線不產生副作用");
            assert_eq!(h.deferred_len("b"), 0);
            Vec::new()
        }
    };
    assert!(effects.iter().all(|effect| !matches!(effect, Effect::StartOp { .. })), "含換行 bytes 不進 line command");
    assert_eq!(h.deferred_len("b"), 0, "B raw input 不進 deferred");
    match guard {
        GuardKind::Flash => assert!(effects.iter().any(|effect| matches!(effect, Effect::HumanTx { permit, payload, .. } if permit.class == TxClass::HumanRaw && payload == input)), "flash guard 期間 TCP bytes 沿 raw 路徑送出"),
        GuardKind::Handoff => assert!(effects.is_empty(), "handoff guard 下的 input event 沿修前基線被丟棄"),
    }

    assert_eq!(h.core.human_console().ownership.interactive_owner.as_deref(), Some("human:b"));
    assert_eq!(
        h.human_tx()
            .iter()
            .filter(|(class, source, payload)| *class == TxClass::HumanRaw && source == "human:b" && payload == input)
            .count(),
        usize::from(matches!(guard, GuardKind::Flash)),
        "B raw bytes 恰送一次",
    );
    assert_eq!(
        h.human_tx()
            .iter()
            .filter(|(class, source, payload)| *class == TxClass::HumanReplay && source == "human:b" && payload == b"deferred-before-resume")
            .count(),
        1,
        "self-test deferred replay 只發生一次",
    );

    match guard {
        GuardKind::Flash => h.fence.with(|fence| fence.flash_critical = false),
        GuardKind::Handoff => {}
    }
    assert_eq!(h.core.human_console().ownership.interactive_owner.as_deref(), Some("human:b"));
    match guard {
        GuardKind::Flash => {
            h.scan(None);
            h.complete_human_tx(TxOutcome::Completed(input.len()), None);
        }
        GuardKind::Handoff => assert!(h.core.active_lease().is_none(), "permanent guard 期間 mirror 保持已撤除"),
    }
}

#[test]
fn guard_off_tcp_raw_matches_pre_package_baseline() {
    run_guard_off_raw_case(GuardKind::Flash);
    run_guard_off_raw_case(GuardKind::Handoff);
}
