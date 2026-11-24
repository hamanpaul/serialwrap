//! p279：TCP raw owner 在 session 層的鏡像 lease、生命週期與仲裁。
#![cfg(feature = "test-fakes")]
mod support;

use std::time::Duration;

use support::*;
use sw_core::human_console::ConsoleTransportKind;
use sw_core::mailbox::{ControlEvent, DataEvent};
use sw_core::op_runner::OpOutcome;
use sw_core::session_core::{Effect, Effects, LeaseIndexOp, SessionRequest, SessionState};
use sw_core::tx_writer::{FlashMode, TxClass};
use sw_proto::pydict;
use sw_proto::pyjson::PyValue;
use sw_pal::traits::Clock;

fn tcp_ready(clients: &[&str]) -> CoreHarness {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    for client in clients {
        h.connect_stream_console(client, ConsoleTransportKind::TcpTelnet);
    }
    h
}

fn active_id(h: &CoreHarness) -> String {
    h.core
        .active_lease()
        .expect("存活 TCP raw owner 應有 session 鏡像 lease")
        .interactive_id
        .clone()
}

fn close_mirror_publicly(h: &mut CoreHarness, client_id: &str) {
    let lease = h.core.active_lease().expect("TCP raw owner mirror lease");
    assert_eq!(lease.owner, format!("human:{client_id}"));
    let interactive_id = lease.interactive_id.clone();
    let response = h
        .rpc(SessionRequest::InteractiveClose {
            interactive_id,
        })
        .expect("interactive-close RPC");
    assert_eq!(response.get("ok"), Some(&PyValue::Bool(true)), "{response:?}");
    assert!(h.core.human_console().consoles.contains_key(client_id), "close lease 保留 console");
}

#[test]
fn tcp_mirror_lifecycle_and_guards() {
    let mut no_bridge = CoreHarness::new(SHELL_PROFILE);
    no_bridge.connect_stream_console("no-bridge", ConsoleTransportKind::TcpTelnet);
    assert!(no_bridge.core.active_lease().is_none(), "無 bridge／非就緒狀態不建立鏡像");

    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    let before = h.core.generations().owner_gen;
    h.connect_stream_console("c1", ConsoleTransportKind::TcpTelnet);
    let lease = h.core.active_lease().expect("accept 同事件建立 mirror");
    assert_eq!(lease.owner, "human:c1");
    assert_eq!(h.core.generations().owner_gen, before + 1, "mirror 不額外增加 owner_gen");
    assert_eq!(h.lease_index_ops().len(), 1, "鏡像只新增一筆 LeaseIndex");

    let id = lease.interactive_id.clone();
    h.rpc(SessionRequest::GetState).expect("session.get_state");
    assert_eq!(h.core.active_lease().map(|active| active.interactive_id.as_str()), Some(id.as_str()));
    assert!(h.core.lease_context_now(h.clock.mono_ns()).get("mirror_client_lifetime").is_none());

    let mut attached = CoreHarness::new(SHELL_PROFILE);
    attached.to_attached(None);
    attached.connect_stream_console("attached", ConsoleTransportKind::TcpTelnet);
    assert_eq!(attached.core.state(), SessionState::Attached);
    assert_eq!(attached.core.active_lease().map(|lease| lease.owner.as_str()), Some("human:attached"));

    let mut guarded = CoreHarness::new(SHELL_PROFILE);
    guarded.to_ready();
    guarded.core.human_console_mut().ownership.flash_mode = true;
    guarded.fence.with(|fence| fence.flash_mode = FlashMode::Probe(9));
    guarded.connect_stream_console("flash", ConsoleTransportKind::TcpTelnet);
    assert!(guarded.core.active_lease().is_none(), "flash guard 不建立鏡像");

    let mut wrong_state = CoreHarness::new(SHELL_PROFILE);
    wrong_state.to_ready();
    wrong_state
        .rpc(SessionRequest::ForceStateForTest(SessionState::Recovering))
        .expect("測試狀態轉換");
    wrong_state.connect_stream_console("recovering", ConsoleTransportKind::TcpTelnet);
    assert!(wrong_state.core.active_lease().is_none(), "非 READY／ATTACHED 狀態不建立鏡像");

    let mut stopping = CoreHarness::new(SHELL_PROFILE);
    stopping.to_ready();
    stopping.begin_stop(sw_core::drain::StopKind::Shutdown);
    stopping.connect_stream_console("stopping", ConsoleTransportKind::TcpTelnet);
    assert!(stopping.core.active_lease().is_none(), "stop 中不建立鏡像");

    let mut frozen = CoreHarness::new(SHELL_PROFILE);
    frozen.to_ready();
    frozen.reg.admission_gate().freeze();
    frozen.connect_stream_console("frozen", ConsoleTransportKind::TcpTelnet);
    assert!(frozen.core.active_lease().is_none(), "handoff freeze 中不建立鏡像");

    let mut agent_lease = CoreHarness::new(SHELL_PROFILE);
    agent_lease.to_ready();
    let opened = agent_lease.rpc(open_req("agent:existing", 60.0, "", false)).expect("agent lease");
    let agent_id = opened.get("interactive_id").and_then(PyValue::as_str).unwrap().to_string();
    {
        let ownership = &mut agent_lease.core.human_console_mut().ownership;
        ownership.interactive_owner = None;
        ownership.admission_blocked = true;
    }
    agent_lease.connect_stream_console("pending", ConsoleTransportKind::TcpTelnet);
    let lease = agent_lease.core.active_lease().expect("agent lease preserved");
    assert_eq!(lease.interactive_id, agent_id, "mirror 不覆蓋既有 agent lease");
    assert_eq!(lease.owner, "agent:existing");
    assert!(lease.mirror_client_lifetime.is_none());

    let mut com0com = CoreHarness::new(SHELL_PROFILE);
    com0com.to_ready();
    com0com.connect_stream_console("serial-console", ConsoleTransportKind::Com0com);
    assert!(com0com.core.active_lease().is_none(), "com0com 不屬 TCP 鏡像範圍");
}

#[test]
fn tcp_mirror_retained_recover_preserves_lifetime_on_eof() {
    let mut h = tcp_ready(&["a"]);
    let lifetime = h.lifetime("a");

    h.begin_stop(sw_core::drain::StopKind::Recover);
    let (epoch, stop_id) = h.pending_stop();
    h.receipt(epoch, stop_id).feed();
    h.bridge_ok();
    let epoch = h.epoch();
    let fsm_gen = h.core.generations().fsm_gen;
    h.feed(ControlEvent::LoginFinished {
        epoch,
        fsm_gen,
        result: sw_core::login_fsm::LoginOutcome::Ready,
        after_rx_seq: None,
    });

    let restored = h.core.active_lease().expect("retained TCP human lease");
    assert_eq!(restored.owner, "human:a");
    assert_eq!(restored.mirror_client_lifetime, Some(lifetime), "recover 還原必須保留 TCP lifetime");
    assert!(h.lease_index_ops().iter().any(|op| matches!(op, LeaseIndexOp::InsertTcpMirror { interactive_id } if interactive_id == &restored.interactive_id)));

    h.disconnect("a");
    assert!(h.core.active_lease().is_none(), "TCP EOF 必須立即撤除 retained mirror");
    h.connect_stream_console("b", ConsoleTransportKind::TcpTelnet);
    let b_lifetime = h.lifetime("b");
    let b_id = active_id(&h);
    assert_eq!(h.core.active_lease().unwrap().mirror_client_lifetime, Some(b_lifetime));
    h.data(DataEvent::ConsoleDisconnected {
        client_id: "a".into(),
        client_lifetime: lifetime,
    });
    assert_eq!(h.core.active_lease().map(|lease| lease.interactive_id.as_str()), Some(b_id.as_str()), "A 的舊 lifetime 不得移除 B");
    assert_eq!(h.core.human_console().ownership.interactive_owner.as_deref(), Some("human:b"));
}

#[test]
fn tcp_mirror_retained_recover_respects_guards() {
    // 交接凍結期間就緒完成事件留在 actor 暫存區；解凍後保留的 TCP 所有者正常還原。
    let mut handoff = tcp_ready(&["handoff"]);
    handoff.begin_stop(sw_core::drain::StopKind::Recover);
    let (epoch, stop_id) = handoff.pending_stop();
    handoff.receipt(epoch, stop_id).feed();
    handoff.bridge_ok();
    let lifetime = handoff.lifetime("handoff");
    let freeze = handoff.reg.admission_gate().freeze_if_idle("p279-retained-restore");
    let sw_core::admission::FreezeOutcome::Frozen { token, created: true } = freeze else {
        panic!("handoff 測試需要新建立可撤銷的凍結：{freeze:?}");
    };
    let (epoch, fsm_gen) = (handoff.epoch(), handoff.core.generations().fsm_gen);
    handoff.feed(ControlEvent::LoginFinished {
        epoch,
        fsm_gen,
        result: sw_core::login_fsm::LoginOutcome::Ready,
        after_rx_seq: None,
    });
    assert!(handoff.core.active_lease().is_none(), "handoff 暫存 readiness 時不得建立 mirror");
    assert!(handoff.core.human_console().ownership.interactive_owner.is_none(), "handoff 暫存期間不得重授 raw owner");
    assert_eq!(handoff.core.retained_human_owner(), Some("human:handoff"), "handoff 暫存期間保留 retained 資訊");
    assert!(handoff.reg.admission_gate().thaw(&token));
    handoff.rpc(SessionRequest::GetState).expect("解凍後重播 readiness");
    let restored = handoff.core.active_lease().expect("解凍後還原 TCP mirror");
    assert_eq!(restored.owner, "human:handoff");
    assert_eq!(restored.mirror_client_lifetime, Some(lifetime));

    // 燒錄關鍵狀態期間就緒收尾照常完成；鏡像還原必須等完整 guard 開放後重試。
    let mut flash = tcp_ready(&["flash"]);
    let lifetime = flash.lifetime("flash");
    flash.begin_stop(sw_core::drain::StopKind::Recover);
    let (epoch, stop_id) = flash.pending_stop();
    flash.receipt(epoch, stop_id).feed();
    flash.bridge_ok();
    flash.fence.with(|fence| fence.flash_critical = true);
    let (epoch, fsm_gen) = (flash.epoch(), flash.core.generations().fsm_gen);
    flash.feed(ControlEvent::LoginFinished {
        epoch,
        fsm_gen,
        result: sw_core::login_fsm::LoginOutcome::Ready,
        after_rx_seq: None,
    });
    assert!(flash.core.active_lease().is_none(), "flash-critical 期間不得建立 retained TCP mirror");
    assert!(flash.core.human_console().ownership.interactive_owner.is_none(), "flash-critical 期間不得重授 retained TCP owner");
    assert_eq!(flash.core.retained_human_owner(), Some("human:flash"), "flash guard 暫時關閉時保留 retained 資訊");

    flash.fence.with(|fence| fence.flash_critical = false);
    flash.rpc(SessionRequest::GetState).expect("guard 開放後重試 retained restore");
    let restored = flash.core.active_lease().expect("flash guard 解除後還原 TCP mirror");
    assert_eq!(restored.owner, "human:flash");
    assert_eq!(restored.mirror_client_lifetime, Some(lifetime));
}

#[test]
fn tcp_mirror_self_heal_respects_flash_critical_window() {
    let mut h = tcp_ready(&["c1"]);
    close_mirror_publicly(&mut h, "c1");
    assert!(h.core.active_lease().is_none());
    assert!(h.core.human_console().ownership.interactive_owner.is_none());

    // 模擬 manager 已標記 flash-critical、但 FlashSyncProbeStart 尚未進 actor 的窗口。
    h.fence.with(|fence| fence.flash_critical = true);
    h.scan(None);
    assert!(h.core.active_lease().is_none(), "flash-critical 自癒不得建立 mirror");
    assert!(h.core.human_console().ownership.interactive_owner.is_none(), "flash-critical 自癒不得授予 raw owner");

    h.fence.with(|fence| fence.flash_critical = false);
    h.scan(None);
    let lease = h.core.active_lease().expect("解除 flash-critical 後 self-heal 建立 mirror");
    assert_eq!(lease.owner, "human:c1");
    assert_eq!(lease.mirror_client_lifetime, Some(h.lifetime("c1")));
    assert_eq!(h.core.human_console().ownership.interactive_owner.as_deref(), Some("human:c1"));
}

#[test]
fn tcp_mirror_tracks_active_suspended_pending() {
    let mut h = tcp_ready(&["c1"]);
    h.input("c1", b"x");
    let id = active_id(&h);
    let mut effects = Effects(Vec::new());
    let admission = h.core.fg_begin(true, &mut effects);
    assert!(admission.suspended_human);

    let busy = h.rpc(open_req("agent", 10.0, "", false)).expect("interactive-open");
    assert_eq!(busy.get("error_code"), Some(&PyValue::from("SESSION_INTERACTIVE_BUSY")));
    assert_eq!(h.core.active_lease().map(|lease| lease.interactive_id.as_str()), Some(id.as_str()));

    // admission 遮蔽時 ownership 落在 pending；refresh 必須保留同一筆 mirror。
    let ownership = &mut h.core.human_console_mut().ownership;
    ownership.interactive_owner = None;
    ownership.suspended_owner = None;
    ownership.pending_owner = Some("human:c1".to_string());
    let busy = h.rpc(open_req("agent", 10.0, "", false)).expect("pending owner interactive-open");
    assert_eq!(busy.get("error_code"), Some(&PyValue::from("SESSION_INTERACTIVE_BUSY")));
    assert_eq!(h.core.active_lease().map(|lease| lease.interactive_id.as_str()), Some(id.as_str()));
}

#[test]
fn tcp_mirror_does_not_mutate_owner_fence() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    let before = h.core.generations().owner_gen;
    let life = h.connect_stream_console("c1", ConsoleTransportKind::TcpTelnet);
    assert_eq!(h.core.generations().owner_gen, before + 1);
    let lease_id = active_id(&h);
    let context = h.core.lease_context_now(h.clock.mono_ns());
    assert_eq!(context.get("interactive_owner"), Some(&PyValue::from("human:c1")));
    let wire = h.wire(&context);
    assert!(!wire.contains("mirror_client_lifetime"), "內部 lifetime 不上 RPC wire：{wire}");

    h.disconnect("c1");
    assert!(h.core.active_lease().is_none(), "EOF 立即撤除該 lifetime 的 mirror");
    assert_eq!(h.core.generations().owner_gen, before + 2, "mirror 撤除不額外增加 owner_gen");
    assert!(h.lease_index_ops().iter().any(|op| matches!(op, LeaseIndexOp::Remove { interactive_id } if interactive_id == &lease_id)));
    // 舊 lifetime 的遲到 EOF 不應再改變簿記。
    h.data(DataEvent::ConsoleDisconnected { client_id: "c1".into(), client_lifetime: life });
    assert_eq!(h.core.generations().owner_gen, before + 2);
}

#[test]
fn tcp_mirror_eof_reconnect_preserves_new_owner() {
    let mut h = tcp_ready(&["a"]);
    let old_life = h.core.human_console().consoles["a"].client_lifetime;
    h.disconnect("a");
    assert!(h.core.active_lease().is_none());
    h.connect_stream_console("b", ConsoleTransportKind::TcpTelnet);
    let b_id = active_id(&h);
    assert_eq!(h.core.active_lease().unwrap().owner, "human:b");

    h.data(DataEvent::ConsoleDisconnected { client_id: "a".into(), client_lifetime: old_life });
    assert_eq!(h.core.active_lease().map(|lease| lease.interactive_id.as_str()), Some(b_id.as_str()));
    assert_eq!(h.core.human_console().ownership.interactive_owner.as_deref(), Some("human:b"));

    let txs = h.input("b", b"z");
    assert!(txs.iter().any(|effect| matches!(effect, Effect::HumanTx { permit, payload, .. } if permit.class == TxClass::HumanRaw && payload == b"z")));

    // B 在 A EOF 前已連入；沿既有 self-heal 時機接班，重授後仍須是 lifetime-bound mirror。
    let mut delayed = tcp_ready(&["a"]);
    let old_life = delayed.lifetime("a");
    delayed.connect_stream_console("b", ConsoleTransportKind::TcpTelnet);
    let b_life = delayed.lifetime("b");
    assert_eq!(delayed.core.active_lease().map(|lease| lease.owner.as_str()), Some("human:a"));
    delayed.disconnect("a");
    assert!(delayed.core.active_lease().is_none(), "A EOF 先撤除 A lease");
    delayed.scan(None);
    let b_lease = delayed.core.active_lease().expect("self-heal 為 B 建 mirror");
    assert_eq!(b_lease.owner, "human:b");
    assert_eq!(b_lease.mirror_client_lifetime, Some(b_life));
    delayed.data(DataEvent::ConsoleDisconnected { client_id: "a".into(), client_lifetime: old_life });
    assert_eq!(delayed.core.active_lease().map(|lease| lease.owner.as_str()), Some("human:b"));
    delayed.disconnect("b");
    assert!(delayed.core.active_lease().is_none(), "self-heal mirror 在 B EOF 立即移除");
}

#[test]
fn tcp_mirror_agent_open_arbitration() {
    let mut h = tcp_ready(&["c1"]);
    h.input("c1", b"x");
    let busy = h.rpc(open_req("agent", 2.0, "", false)).expect("active human gate");
    assert_eq!(busy.get("error_code"), Some(&PyValue::from("SESSION_INTERACTIVE_BUSY")));

    h.advance(Duration::from_secs(61));
    let opened = h.rpc(open_req("agent", 2.0, "", false)).expect("idle soft preempt");
    assert_eq!(opened.get("soft_preempted"), Some(&PyValue::Bool(true)));
    let agent_id = opened.get("interactive_id").and_then(PyValue::as_str).unwrap();
    h.advance(Duration::from_secs(3));
    assert_eq!(h.core.active_lease().map(|lease| lease.owner.as_str()), Some("human:c1"));
    assert_ne!(h.core.active_lease().map(|lease| lease.interactive_id.as_str()), Some(agent_id));
}

#[test]
fn tcp_mirror_preempt_reconnect_preserves_new_owner() {
    let mut h = tcp_ready(&["a"]);
    let opened = h.rpc(open_req("agent", 60.0, "", false)).expect("idle soft preempt");
    assert_eq!(opened.get("soft_preempted"), Some(&PyValue::Bool(true)));
    h.input("a", b"old");
    h.disconnect("a");
    assert_eq!(h.deferred_len("a"), 0, "EOF 立即清除 A 的 deferred bytes");

    h.connect_stream_console("b", ConsoleTransportKind::TcpTelnet);
    h.input("b", b"new");
    let agent_id = opened.get("interactive_id").and_then(PyValue::as_str).unwrap().to_string();
    h.rpc(close_req(&agent_id)).expect("agent interactive-close");
    let replay: Vec<_> = h.human_tx().into_iter().filter(|(class, _, _)| *class == TxClass::HumanReplay).collect();
    assert_eq!(replay, vec![(TxClass::HumanReplay, "human:b".into(), b"new".to_vec())]);
    assert_eq!(h.core.human_console().ownership.interactive_owner.as_deref(), Some("human:b"));

    // expiry 與 explicit close 共用 lifetime 保護；A 的 stash 已 EOF，B 的 deferred 只回放一次。
    let mut expired = tcp_ready(&["a"]);
    let opened = expired.rpc(open_req("agent:expiry", 1.0, "", false)).expect("expiry preempt");
    assert_eq!(opened.get("soft_preempted"), Some(&PyValue::Bool(true)));
    expired.input("a", b"old");
    expired.disconnect("a");
    expired.connect_stream_console("b", ConsoleTransportKind::TcpTelnet);
    let b_life = expired.lifetime("b");
    expired.input("b", b"new");
    expired.advance(Duration::from_secs(2));
    assert_eq!(expired.core.human_console().ownership.interactive_owner.as_deref(), Some("human:b"));
    assert_eq!(expired.core.active_lease().and_then(|lease| lease.mirror_client_lifetime), Some(b_life));
    let replay: Vec<_> = expired.human_tx().into_iter().filter(|(class, _, _)| *class == TxClass::HumanReplay).collect();
    assert_eq!(replay, vec![(TxClass::HumanReplay, "human:b".into(), b"new".to_vec())]);

    // TCP 接班保護不可外溢到一般 PTY stash 的既有 close 規則。
    let mut pty = CoreHarness::new(SHELL_PROFILE);
    pty.to_ready();
    pty.attach_pty_console("pty-a", true);
    let opened = pty.rpc(open_req("agent:pty", 60.0, "", false)).expect("PTY soft preempt");
    assert_eq!(opened.get("soft_preempted"), Some(&PyValue::Bool(true)));
    pty.disconnect("pty-a");
    pty.connect_stream_console("tcp-b", ConsoleTransportKind::TcpTelnet);
    assert_eq!(pty.core.human_console().ownership.suspended_owner.as_deref(), Some("human:tcp-b"));
    let agent_id = opened.get("interactive_id").and_then(PyValue::as_str).unwrap().to_string();
    pty.rpc(close_req(&agent_id)).expect("agent close");
    assert!(pty.core.active_lease().is_none(), "PTY stash 沿用原有無條件清 owner 語意");
    assert!(pty.core.human_console().ownership.interactive_owner.is_none(), "TCP owner 不得改變 PTY stash close 語意");

    // TCP stash 恢復也是 raw 所有權重新授予入口；守衛關閉時保留 stash 並延後恢復。
    let mut guarded = tcp_ready(&["guarded"]);
    let opened = guarded.rpc(open_req("agent:guarded", 60.0, "", false)).expect("guarded soft preempt");
    let agent_id = opened.get("interactive_id").and_then(PyValue::as_str).unwrap().to_string();
    guarded.fence.with(|fence| fence.flash_critical = true);
    guarded.rpc(close_req(&agent_id)).expect("guarded agent close");
    assert!(guarded.core.active_lease().is_none(), "flash-critical 期間不重授 TCP stash lease");
    assert!(guarded.core.human_console().ownership.interactive_owner.is_none(), "flash-critical 期間不重授 TCP raw owner");
    assert_eq!(guarded.core.stashed_human_lease().map(|lease| lease.owner.as_str()), Some("human:guarded"), "guard 關閉時保留 stash");

    guarded.fence.with(|fence| fence.flash_critical = false);
    guarded.scan(None);
    assert_eq!(guarded.core.active_lease().map(|lease| lease.owner.as_str()), Some("human:guarded"));
    assert_eq!(guarded.core.human_console().ownership.interactive_owner.as_deref(), Some("human:guarded"));

    // A 斷線後 B 在燒錄守衛期間接手；agent 租約關閉不可透過無 stash 的恢復繞過守衛。
    let mut replacement = tcp_ready(&["a"]);
    let opened = replacement.rpc(open_req("agent:replacement", 60.0, "", false)).expect("replacement soft preempt");
    let agent_id = opened.get("interactive_id").and_then(PyValue::as_str).unwrap().to_string();
    replacement.fence.with(|fence| fence.flash_critical = true);
    replacement.disconnect("a");
    replacement.connect_stream_console("b", ConsoleTransportKind::TcpTelnet);
    assert_eq!(replacement.core.human_console().ownership.suspended_owner.as_deref(), Some("human:b"));
    replacement.rpc(close_req(&agent_id)).expect("replacement agent close");
    assert!(replacement.core.active_lease().is_none(), "flash-critical 期間不建立替換 TCP mirror");
    assert!(replacement.core.human_console().ownership.interactive_owner.is_none(), "flash-critical 期間不重授替換 TCP raw owner");
    assert_eq!(replacement.core.human_console().ownership.suspended_owner.as_deref(), Some("human:b"), "guard 關閉時保留替換 owner 待恢復");

    replacement.fence.with(|fence| fence.flash_critical = false);
    replacement.scan(None);
    assert_eq!(replacement.core.active_lease().map(|lease| lease.owner.as_str()), Some("human:b"));
    assert_eq!(replacement.core.human_console().ownership.interactive_owner.as_deref(), Some("human:b"));
}

#[test]
fn tcp_mirror_foreground_refresh_keeps_lease() {
    let mut h = tcp_ready(&["c1"]);
    let mirror_id = active_id(&h);
    let started = h.start_foreground_command("echo work");
    h.input("c1", b"x");
    let _ = h.rpc(SessionRequest::InteractiveStatus { interactive_id: mirror_id, screen_chars: 80 }).expect("status refresh");
    assert!(h.core.active_lease().is_some_and(|lease| lease.is_human()), "refresh 不得拆除 suspended mirror");

    let fx = h.feed(ControlEvent::OpFinished {
        op_id: started.op_id,
        epoch: h.epoch(),
        outcome: OpOutcome::CommandResult(pydict! { "ok" => true, "stdout" => "work" }),
        after_rx_seq: None,
    });
    assert_eq!(
        fx.iter().filter(|effect| matches!(effect, Effect::HumanTx { permit, payload, .. } if permit.class == TxClass::HumanReplay && payload == b"x")).count(),
        1,
        "deferred input 恰回放一次"
    );
}

#[test]
fn tcp_mirror_reprobe_respects_activity_window() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_attached(Some("PROMPT_UNAVAILABLE"));
    h.connect_stream_console("c1", ConsoleTransportKind::TcpTelnet);
    h.input("c1", b"x");
    let lease_id = active_id(&h);
    let early = h.advance(Duration::from_secs(59));
    assert!(!early.iter().any(|effect| matches!(effect, Effect::StartLogin { .. })), "59s 活動窗不探測");
    assert_eq!(h.core.active_lease().map(|lease| lease.interactive_id.as_str()), Some(lease_id.as_str()));

    let at_boundary = h.advance(Duration::from_secs(1));
    assert!(!at_boundary.iter().any(|effect| matches!(effect, Effect::StartLogin { .. })), "60s 含邊界");
    let after = h.advance(Duration::from_secs(2));
    assert!(after.iter().any(|effect| matches!(effect, Effect::StartLogin { .. })), "超過 60s 才允許 reprobe");
}

#[test]
fn tcp_mirror_secondary_line_respects_lease_busy() {
    let mut h = tcp_ready(&["c1", "c2"]);
    let before = h.tx_effect_count();
    let rejected = h.input("c2", b"ls\r");
    assert!(!rejected.iter().any(|effect| matches!(effect, Effect::StartOp { .. })));
    assert_eq!(h.tx_effect_count(), before, "第二 console 遇 mirror lease 零 UART TX");
    let row = h.last_human_command().expect("第二 console 的 line command 應進 command registry");
    assert_eq!(row.get("source"), Some(&PyValue::from("human:c2")), "命令來源");
    assert_eq!(row.get("command"), Some(&PyValue::from("ls")), "命令內容");
    assert_eq!(row.get("status"), Some(&PyValue::from("error")), "命令應以錯誤終態結束");
    assert_eq!(row.get("error_code"), Some(&PyValue::from("SESSION_INTERACTIVE_BUSY")), "第二 console 必須得到精確 BUSY");

    close_mirror_publicly(&mut h, "c1");
    let accepted = h.input("c2", b"ls\r");
    assert!(accepted.iter().any(|effect| matches!(effect, Effect::StartOp { .. })), "解除 lease 後保留原 line submission");
}
