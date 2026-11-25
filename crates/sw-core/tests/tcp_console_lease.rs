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
use sw_pal::traits::TxOutcome;
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

fn start_non_strict_self_test(h: &mut CoreHarness) -> sw_core::drain::OpId {
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

fn start_non_strict_self_test_with_timeout(
    h: &mut CoreHarness,
    timeout_s: f64,
) -> sw_core::drain::OpId {
    let (_reply, effects) = h.rpc_deferred(SessionRequest::SelfTest {
        timeout_s,
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

    assert!(h.core.active_lease().is_none(), "retained TCP 快照不直接重授 mirror");
    assert!(h.core.human_console().ownership.interactive_owner.is_none());
    let insert_start = h.lease_index_ops().len();
    h.scan(None);
    let restored = h.core.active_lease().expect("retained TCP human lease");
    assert_eq!(restored.owner, "human:a");
    assert_eq!(restored.mirror_client_lifetime, Some(lifetime), "同步應依存活 TCP lifetime 重建 mirror");
    assert_eq!(
        h.lease_index_ops()[insert_start..]
            .iter()
            .filter(|op| matches!(op, LeaseIndexOp::InsertTcpMirror { interactive_id } if interactive_id == &restored.interactive_id))
            .count(),
        1,
        "recover 後既有同步路徑只重建一次 mirror",
    );

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
    // 交接凍結期間就緒完成事件留在 actor 暫存區；解凍後 readiness 消費 retained 快照，
    // 由下一次 console scan 依現有連線重建 TCP mirror。
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
    assert!(handoff.core.active_lease().is_none(), "retained TCP 快照不直接重授 mirror");
    assert!(handoff.core.human_console().ownership.interactive_owner.is_none());
    let handoff_insert_start = handoff.lease_index_ops().len();
    handoff.scan(None);
    let restored = handoff.core.active_lease().expect("同步依目前 TCP 連線建立 mirror");
    assert_eq!(restored.owner, "human:handoff");
    assert_eq!(restored.mirror_client_lifetime, Some(lifetime));
    assert_eq!(
        handoff.lease_index_ops()[handoff_insert_start..]
            .iter()
            .filter(|op| matches!(op, LeaseIndexOp::InsertTcpMirror { interactive_id } if interactive_id == &restored.interactive_id))
            .count(),
        1,
        "handoff guard 開放後同步只建立一次 mirror",
    );

    // 燒錄關鍵狀態期間 readiness 收尾照常完成並丟棄 retained mirror 資訊；
    // guard 開放後由既有 self-heal／sync 依 TCP 連線現況重建。
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
    assert_eq!(flash.core.retained_human_owner(), None, "flash guard 關閉時丟棄 retained TCP mirror 資訊");

    flash.fence.with(|fence| fence.flash_critical = false);
    let flash_insert_start = flash.lease_index_ops().len();
    flash.scan(None);
    let restored = flash.core.active_lease().expect("flash guard 開放後由 sync 重建 TCP mirror");
    assert_eq!(restored.owner, "human:flash");
    assert_eq!(restored.mirror_client_lifetime, Some(lifetime));
    assert_eq!(
        flash.lease_index_ops()[flash_insert_start..]
            .iter()
            .filter(|op| matches!(op, LeaseIndexOp::InsertTcpMirror { interactive_id } if interactive_id == &restored.interactive_id))
            .count(),
        1,
        "flash guard 開放後同步只建立一次 mirror",
    );
}

#[test]
fn tcp_mirror_self_heal_respects_flash_critical_window() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    // accept 發生在 flash guard 關閉期間；raw owner 依既有路徑建立，但 mirror 暫不建帳。
    h.fence.with(|fence| fence.flash_critical = true);
    h.connect_stream_console("c1", ConsoleTransportKind::TcpTelnet);
    assert!(h.core.active_lease().is_none());
    assert_eq!(h.core.human_console().ownership.interactive_owner.as_deref(), Some("human:c1"));
    // 將 raw owner 清空以走自癒分支；guard 期間自癒不能先授權或建 mirror。
    h.core.human_console_mut().ownership.interactive_owner = None;

    // 模擬 manager 已標記 flash-critical、但 FlashSyncProbeStart 尚未進 actor 的窗口。
    h.scan(None);
    assert!(h.core.active_lease().is_none(), "flash-critical 自癒不得建立 mirror");
    assert!(h.core.human_console().ownership.interactive_owner.is_none(), "flash-critical 期間自癒不得授予 raw owner");

    h.fence.with(|fence| fence.flash_critical = false);
    h.scan(None);
    let lease = h.core.active_lease().expect("解除 flash-critical 後 self-heal 建立 mirror");
    assert_eq!(lease.owner, "human:c1");
    assert_eq!(lease.mirror_client_lifetime, Some(h.lifetime("c1")));
    assert_eq!(h.core.human_console().ownership.interactive_owner.as_deref(), Some("human:c1"));

    // guard 關閉時撤除既有 mirror 簿記，但保留同一 TCP client 的 raw ownership 與路由。
    let mut active = tcp_ready(&["active"]);
    let active_mirror_id = active.core.active_lease().unwrap().interactive_id.clone();
    let owner_gen = active.core.generations().owner_gen;
    let insert_start = active.lease_index_ops().len();
    active.fence.with(|fence| fence.flash_critical = true);
    active.scan(None);
    assert!(active.core.active_lease().is_none(), "guard 關閉時撤除 active mirror");
    assert_eq!(active.core.human_console().ownership.interactive_owner.as_deref(), Some("human:active"));
    assert_eq!(active.core.generations().owner_gen, owner_gen, "撤除 mirror 不得改變 owner generation");
    let raw = active.input("active", b"raw-during-guard");
    assert!(raw.iter().any(|effect| matches!(effect, Effect::HumanTx { permit, payload, .. } if permit.class == TxClass::HumanRaw && payload == b"raw-during-guard")));
    assert_eq!(active.core.human_console().ownership.interactive_owner.as_deref(), Some("human:active"));

    active.fence.with(|fence| fence.flash_critical = false);
    let inserts_before_reopen = active.lease_index_ops().len();
    active.scan(None);
    let rebuilt = active.core.active_lease().expect("guard 開放後由既有 sync 重建 mirror").clone();
    assert_eq!(rebuilt.owner, "human:active");
    assert_eq!(active.core.human_console().ownership.interactive_owner.as_deref(), Some("human:active"));
    assert_eq!(active.core.generations().owner_gen, owner_gen, "重建 mirror 不改變 owner generation");
    assert_eq!(
        active.lease_index_ops()[inserts_before_reopen..]
            .iter()
            .filter(|op| matches!(op, LeaseIndexOp::InsertTcpMirror { interactive_id } if interactive_id == &rebuilt.interactive_id))
            .count(),
        1,
    );
    active.scan(None);
    assert_eq!(
        active.lease_index_ops()[inserts_before_reopen..]
            .iter()
            .filter(|op| matches!(op, LeaseIndexOp::InsertTcpMirror { interactive_id } if interactive_id == &rebuilt.interactive_id))
            .count(),
        1,
        "後續 refresh 不重複建立 mirror",
    );
    assert!(active.lease_index_ops()[insert_start..]
        .iter()
        .any(|op| matches!(op, LeaseIndexOp::Remove { interactive_id } if interactive_id == &active_mirror_id)));
}

#[test]
fn tcp_mirror_explicit_close_revokes_through_self_test_resume() {
    let mut h = tcp_ready(&["a"]);
    h.set_auto_complete_human_tx(false);
    let original_id = active_id(&h);
    let probe_op = start_non_strict_self_test(&mut h);
    assert_eq!(h.core.human_console().ownership.suspended_owner.as_deref(), Some("human:a"));
    assert_eq!(h.core.human_console().ownership.suspend_depth, 1);

    let deferred = h.input("a", b"explicit-close-deferred");
    assert!(deferred.iter().all(|effect| !matches!(effect, Effect::HumanTx { .. })));
    assert_eq!(h.deferred_len("a"), b"explicit-close-deferred".len());

    let closed = h.rpc(close_req(&original_id)).expect("明確關閉 mirror");
    assert_eq!(closed.get("ok"), Some(&PyValue::Bool(true)));
    assert!(h.core.active_lease().is_none(), "明確 close 不得從 self-test suspended owner 立即重建 mirror");
    let stale = h
        .rpc(status_req(&original_id, 80))
        .expect("舊 interactive id 查詢");
    assert_eq!(stale.get("error_code"), Some(&PyValue::from("INTERACTIVE_NOT_FOUND")));

    h.scan(None);
    assert!(h.core.active_lease().is_none(), "refresh 不得撤銷同一 lifetime 的明確 close");
    let still_deferred = h.input("a", b"-before-resume");
    assert!(still_deferred.iter().all(|effect| !matches!(effect, Effect::HumanTx { .. })));
    assert_eq!(h.deferred_len("a"), b"explicit-close-deferred-before-resume".len());
    assert_eq!(h.core.human_console().ownership.suspend_depth, 1, "refresh 不得結算 self-test suspend");

    let resumed = finish_self_test(&mut h, probe_op);
    let replay: Vec<_> = resumed
        .iter()
        .filter_map(|effect| match effect {
            Effect::HumanTx { permit, payload, .. } if permit.class == TxClass::HumanReplay => {
                Some((permit.source.clone(), payload.clone()))
            }
            _ => None,
        })
        .collect();
    assert_eq!(replay, vec![("human:a".into(), b"explicit-close-deferred-before-resume".to_vec())]);
    assert_eq!(h.core.human_console().ownership.suspend_depth, 0);
    assert_eq!(h.core.human_console().ownership.interactive_owner.as_deref(), Some("human:a"));
    assert!(h.core.active_lease().is_none(), "self-test resume 後仍尊重明確撤權");
    assert_eq!(
        h.human_tx()
            .iter()
            .filter(|(class, source, payload)| {
                *class == TxClass::HumanReplay
                    && source == "human:a"
                    && payload == b"explicit-close-deferred-before-resume"
            })
            .count(),
        1,
        "deferred bytes 在既有 resume 路徑恰回放一次",
    );
    h.complete_human_tx(TxOutcome::Completed(b"explicit-close-deferred-before-resume".len()), None);

    let raw = h.input("a", b"raw-after-resume");
    assert!(raw.iter().any(|effect| matches!(
        effect,
        Effect::HumanTx { permit, payload, .. }
            if permit.class == TxClass::HumanRaw && payload == b"raw-after-resume"
    )));
    assert_eq!(
        h.core.human_console().ownership.peek_route("a"),
        sw_core::human_console::ownership::InputRoute::Raw {
            owner: "human:a".into()
        }
    );
}

#[test]
fn tcp_mirror_console_detach_revokes_through_self_test_resume() {
    let mut h = tcp_ready(&["a"]);
    let original_id = active_id(&h);
    let probe_op = start_non_strict_self_test(&mut h);
    let deferred = h.input("a", b"detach-deferred");
    assert!(deferred.iter().all(|effect| !matches!(effect, Effect::HumanTx { .. })));

    let detached = h
        .rpc(SessionRequest::ConsoleDetach { client_id: "a".into() })
        .expect("console_detach");
    assert_eq!(detached.get("ok"), Some(&PyValue::Bool(true)));
    assert!(h.core.active_lease().is_none(), "console_detach 不得留下或重建已撤除的 mirror");
    let stale = h.rpc(status_req(&original_id, 80)).expect("舊 interactive id 查詢");
    assert_eq!(stale.get("error_code"), Some(&PyValue::from("INTERACTIVE_NOT_FOUND")));
    h.scan(None);
    assert!(h.core.active_lease().is_none(), "detach 後同步不得重建該 client 的 mirror");

    let resumed = finish_self_test(&mut h, probe_op);
    assert!(resumed.iter().all(|effect| !matches!(effect, Effect::HumanTx { .. })), "detach 已清除此 client 的 deferred 輸入");
    assert!(h.core.active_lease().is_none());
    assert!(h.core.human_console().ownership.interactive_owner.is_none());
    assert_eq!(h.core.human_console().ownership.suspend_depth, 0);
    assert_eq!(h.deferred_len("a"), 0);
}

#[test]
fn tcp_mirror_revocation_records_pruned_after_active_detach_without_eof() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();

    for client_id in ["detach-a", "detach-b", "detach-c"] {
        h.connect_stream_console(client_id, ConsoleTransportKind::TcpTelnet);
        assert_eq!(
            h.core.active_lease().map(|lease| lease.owner.as_str()),
            Some(format!("human:{client_id}").as_str()),
            "每個新連線都經 ConsoleConnected 登記其 TCP raw owner",
        );

        let (reply, effects) = h.rpc_full(SessionRequest::ConsoleDetach {
            client_id: client_id.to_string(),
        });
        let reply = reply.expect("ConsoleDetach RPC");
        assert_eq!(reply.get("ok"), Some(&PyValue::Bool(true)), "{reply:?}");
        assert!(
            effects.iter().any(|effect| matches!(effect, Effect::ConsoleClose { client_id: closed }
                if closed == client_id)),
            "主動 detach 必須送出 ConsoleClose 給 Hub",
        );
        assert!(
            !h.core.human_console().consoles.contains_key(client_id),
            "ConsoleDetach 已從 actor console table 移除 client",
        );
        assert_eq!(
            h.core.tcp_mirror_revocation_count_for_test(),
            0,
            "ConsoleClose 不產生 EOF；已移出 console table 的 lifetime 撤權記錄須立即清除",
        );
        // 刻意不呼叫 h.disconnect：Hub 的 ConsoleClose 以 notify=false 移除連線。
    }
}

#[test]
fn tcp_mirror_existing_interactive_open_clears_revocation() {
    let mut h = tcp_ready(&["a"]);
    let lifetime = h.lifetime("a");
    close_mirror_publicly(&mut h, "a");
    assert!(h.core.active_lease().is_none(), "明確 close 先撤除舊 mirror");
    assert_eq!(h.core.tcp_mirror_revocation_count_for_test(), 1);

    let opened = h
        .rpc(open_req("human:a", 60.0, "", false))
        .expect("既有 interactive_open 路徑重新授予同一 TCP client");
    assert_eq!(opened.get("ok"), Some(&PyValue::Bool(true)));
    let lease = h.core.active_lease().expect("interactive_open 已授予 human lease");
    assert_eq!(lease.owner, "human:a");
    assert_eq!(
        lease.mirror_client_lifetime,
        Some(lifetime),
        "既有路徑重授同一 TCP client 時 lease 保留 lifetime mirror 標記",
    );
    assert_eq!(h.core.tcp_mirror_revocation_count_for_test(), 0);

    let id = lease.interactive_id.clone();
    h.scan(None);
    assert_eq!(
        h.core.active_lease().map(|current| current.interactive_id.as_str()),
        Some(id.as_str()),
        "後續 sync 保留既有 interactive_open mirror",
    );
}

#[test]
fn tcp_mirror_new_lifetime_clears_explicit_revocation() {
    let mut h = tcp_ready(&["a"]);
    let original_id = active_id(&h);
    let old_lifetime = h.lifetime("a");
    let probe_op = start_non_strict_self_test(&mut h);
    let closed = h.rpc(close_req(&original_id)).expect("明確關閉舊 lifetime mirror");
    assert_eq!(closed.get("ok"), Some(&PyValue::Bool(true)));

    h.disconnect("a");
    assert!(h.core.active_lease().is_none(), "舊 lifetime 斷線後立即撤除 mirror");
    h.connect_stream_console("a", ConsoleTransportKind::TcpTelnet);
    let new_lifetime = h.lifetime("a");
    assert_ne!(new_lifetime, old_lifetime, "同一 client id 的 accept 仍建立新的 lifetime");
    let new_id = active_id(&h);
    assert_ne!(new_id, original_id, "新 lifetime 應有新的 mirror interactive id");
    assert_eq!(h.core.active_lease().unwrap().mirror_client_lifetime, Some(new_lifetime));
    let stale = h.rpc(status_req(&original_id, 80)).expect("舊 interactive id 查詢");
    assert_eq!(stale.get("error_code"), Some(&PyValue::from("INTERACTIVE_NOT_FOUND")));

    let deferred = h.input("a", b"new-lifetime-deferred");
    assert!(deferred.iter().all(|effect| !matches!(effect, Effect::HumanTx { .. })));
    assert_eq!(h.deferred_len("a"), b"new-lifetime-deferred".len());
    let resumed = finish_self_test(&mut h, probe_op);
    let replay: Vec<_> = resumed
        .iter()
        .filter_map(|effect| match effect {
            Effect::HumanTx { permit, payload, .. } if permit.class == TxClass::HumanReplay => {
                Some((permit.source.clone(), payload.clone()))
            }
            _ => None,
        })
        .collect();
    assert_eq!(replay, vec![("human:a".into(), b"new-lifetime-deferred".to_vec())]);
    assert_eq!(h.core.active_lease().map(|lease| lease.interactive_id.as_str()), Some(new_id.as_str()));
    assert_eq!(h.core.human_console().ownership.suspend_depth, 0);
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
    let deferred = h.input("b", b"new");
    assert!(deferred.iter().all(|effect| !matches!(effect, Effect::HumanTx { .. })));
    assert_eq!(h.deferred_len("b"), b"new".len(), "B 在既有 suspend 期間輸入進 deferred");
    let agent_id = opened.get("interactive_id").and_then(PyValue::as_str).unwrap().to_string();
    h.rpc(close_req(&agent_id)).expect("agent interactive-close");
    assert_eq!(h.core.human_console().ownership.interactive_owner.as_deref(), Some("human:b"));
    let replay: Vec<_> = h.human_tx().into_iter().filter(|(class, _, _)| *class == TxClass::HumanReplay).collect();
    assert_eq!(replay, vec![(TxClass::HumanReplay, "human:b".into(), b"new".to_vec())]);
    assert_eq!(h.core.human_console().ownership.interactive_owner.as_deref(), Some("human:b"));
    assert_eq!(h.core.active_lease().map(|lease| lease.mirror_client_lifetime), Some(Some(h.lifetime("b"))));

    // expiry 與 explicit close 共用 lifetime 保護；A 的 stash 已 EOF，B 的 deferred 只回放一次。
    let mut expired = tcp_ready(&["a"]);
    let opened = expired.rpc(open_req("agent:expiry", 1.0, "", false)).expect("expiry preempt");
    assert_eq!(opened.get("soft_preempted"), Some(&PyValue::Bool(true)));
    expired.input("a", b"old");
    expired.disconnect("a");
    expired.connect_stream_console("b", ConsoleTransportKind::TcpTelnet);
    let b_life = expired.lifetime("b");
    expired.input("b", b"new");
    assert_eq!(expired.deferred_len("b"), b"new".len());
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

    // TCP stash 只作鏡像簿記；guard 關閉時立即撤除，再沿原本 resume 路徑恢復 raw owner。
    let mut guarded = tcp_ready(&["guarded"]);
    let opened = guarded.rpc(open_req("agent:guarded", 60.0, "", false)).expect("guarded soft preempt");
    let agent_id = opened.get("interactive_id").and_then(PyValue::as_str).unwrap().to_string();
    guarded.fence.with(|fence| fence.flash_critical = true);
    guarded.rpc(close_req(&agent_id)).expect("guarded agent close");
    let owner_gen = guarded.core.generations().owner_gen;
    assert!(guarded.core.active_lease().is_none(), "flash-critical 期間不重授 TCP stash lease");
    assert_eq!(guarded.core.human_console().ownership.interactive_owner.as_deref(), Some("human:guarded"), "flash-critical 不改變既有 resume 的 raw owner");
    assert!(guarded.core.stashed_human_lease().is_none(), "guard 關閉時撤除 TCP stash mirror 簿記");
    assert_eq!(guarded.core.generations().owner_gen, owner_gen, "close 後 owner 已由 resume 路徑恢復，不由 mirror 簿記變更");

    guarded.fence.with(|fence| fence.flash_critical = false);
    guarded.scan(None);
    assert_eq!(guarded.core.active_lease().map(|lease| lease.owner.as_str()), Some("human:guarded"));
    assert_eq!(guarded.core.human_console().ownership.interactive_owner.as_deref(), Some("human:guarded"));
    assert_eq!(guarded.core.generations().owner_gen, owner_gen, "guard 開放重建 mirror 不改變 owner generation");

    // A 斷線後 B 在燒錄守衛期間接手；agent 租約關閉不可透過無 stash 的恢復繞過守衛。
    let mut replacement = tcp_ready(&["a"]);
    let opened = replacement.rpc(open_req("agent:replacement", 60.0, "", false)).expect("replacement soft preempt");
    let agent_id = opened.get("interactive_id").and_then(PyValue::as_str).unwrap().to_string();
    replacement.fence.with(|fence| fence.flash_critical = true);
    replacement.disconnect("a");
    replacement.connect_stream_console("b", ConsoleTransportKind::TcpTelnet);
    assert_eq!(replacement.core.human_console().ownership.interactive_owner.as_deref(), Some("agent:replacement"), "agent 尚持有 raw ownership 時 B 不立即接班");
    replacement.rpc(close_req(&agent_id)).expect("replacement agent close");
    assert!(replacement.core.active_lease().is_none(), "flash-critical 期間不建立替換 TCP mirror");
    assert_eq!(replacement.core.human_console().ownership.interactive_owner.as_deref(), Some("human:b"), "guard 關閉不延後既有 resume 對 B 的 raw owner 提升");
    assert_eq!(replacement.core.human_console().ownership.suspended_owner, None);

    replacement.connect_stream_console("c", ConsoleTransportKind::TcpTelnet);
    assert_eq!(replacement.core.human_console().ownership.interactive_owner.as_deref(), Some("human:b"), "後續 stream accept 不改變既有 B raw owner");
    replacement.fence.with(|fence| fence.flash_critical = false);
    replacement.scan(None);
    assert_eq!(replacement.core.active_lease().map(|lease| lease.owner.as_str()), Some("human:b"));
    assert_eq!(replacement.core.human_console().ownership.interactive_owner.as_deref(), Some("human:b"));
}

#[test]
fn tcp_mirror_reconnect_does_not_resume_self_test_suspend() {
    let mut h = tcp_ready(&["a"]);
    h.set_auto_complete_human_tx(false);
    h.core.set_tcp_console(true);
    let probe_op = start_non_strict_self_test(&mut h);
    assert_eq!(h.core.human_console().ownership.suspend_depth, 1);
    assert!(h.input("a", b"before-eof").iter().all(|effect| !matches!(effect, Effect::HumanTx { .. })));

    h.disconnect("a");
    assert_eq!(h.core.human_console().ownership.suspend_depth, 1, "EOF 不結算在途 probe 的 suspend");
    let owner_gen_before_b = h.core.generations().owner_gen;
    h.connect_stream_console("b", ConsoleTransportKind::TcpTelnet);
    assert_eq!(h.core.active_lease().map(|lease| lease.owner.as_str()), Some("human:b"));

    let during_probe = h.input("b", b"deferred");
    assert!(during_probe.iter().all(|effect| !matches!(effect, Effect::HumanTx { .. })), "probe 完成前 B 不得送出 HumanTx");
    assert_eq!(h.deferred_len("b"), b"deferred".len(), "B 輸入留在 deferred");
    assert_eq!(h.core.human_console().ownership.suspend_depth, 1);
    assert_eq!(h.core.generations().owner_gen, owner_gen_before_b, "鏡像建帳不得改變 owner generation");

    let finished = finish_self_test(&mut h, probe_op);
    let replay: Vec<_> = finished
        .iter()
        .filter_map(|effect| match effect {
            Effect::HumanTx { permit, payload, .. } if permit.class == TxClass::HumanReplay => {
                Some((permit.class, permit.source.clone(), payload.clone()))
            }
            _ => None,
        })
        .collect();
    assert_eq!(replay, vec![(TxClass::HumanReplay, "human:b".into(), b"deferred".to_vec())]);
    assert_eq!(h.core.human_console().ownership.suspend_depth, 0);
    assert_eq!(h.core.human_console().ownership.interactive_owner.as_deref(), Some("human:b"));
    assert_eq!(h.core.generations().owner_gen, owner_gen_before_b + 1, "probe 結束只因實際 raw owner 恢復增加一次 generation");
    assert_eq!(h.human_tx().iter().filter(|(class, source, payload)| *class == TxClass::HumanReplay && source == "human:b" && payload == b"deferred").count(), 1);
    assert!(h.replay_hold().is_some_and(|hold| !hold.revoked));
    let opened = h.rpc(open_req("agent:ordinary-self-test-replay", 60.0, "", false)).unwrap();
    assert_eq!(opened.get("error_code"), Some(&PyValue::from("SESSION_BUSY")), "一般 self-test replay 不得被標成 tick close");
    let attached = h.rpc(SessionRequest::ConsoleAttach { label: None, pty: None }).unwrap();
    assert_eq!(attached.get("ok"), Some(&PyValue::Bool(true)), "一般 self-test replay 不得套用 tick close 的 console attach BUSY");
    h.complete_human_tx(TxOutcome::Completed(b"deferred".len()), None);
}

#[test]
fn tcp_mirror_delayed_soft_preempt_close_preserves_nested_self_test_suspend() {
    let mut h = tcp_ready(&["a"]);
    h.set_auto_complete_human_tx(false);
    let probe_op = start_non_strict_self_test(&mut h);
    let opened = h.rpc(open_req("agent:nested", 60.0, "", false)).expect("agent soft preempt");
    assert_eq!(opened.get("soft_preempted"), Some(&PyValue::Bool(true)));
    let agent_id = opened.get("interactive_id").and_then(PyValue::as_str).unwrap().to_string();
    assert_eq!(h.core.human_console().ownership.suspend_depth, 2, "probe 與 agent lease 各持有一層 suspend");

    h.fence.with(|fence| fence.flash_critical = true);
    h.rpc(close_req(&agent_id)).expect("guard 關閉期間 close agent lease");
    assert!(h.core.active_lease().is_none());
    assert_eq!(h.core.human_console().ownership.suspend_depth, 1, "agent close 立即依既有路徑結算自己的 suspend");
    assert!(h.input("a", b"nested").iter().all(|effect| !matches!(effect, Effect::HumanTx { .. })));
    assert_eq!(h.deferred_len("a"), b"nested".len());

    let owner_gen_before_sync = h.core.generations().owner_gen;
    h.fence.with(|fence| fence.flash_critical = false);
    h.scan(None);
    let mirror = h.core.active_lease().expect("既有 sync 依仍存活的 A 建立 mirror");
    assert_eq!(mirror.owner, "human:a");
    assert_eq!(h.core.human_console().ownership.suspend_depth, 1, "同步不得結算 self-test suspend");
    assert_eq!(h.core.human_console().ownership.interactive_owner, None, "self-test 尚在途時 raw owner 仍 suspended");
    assert_eq!(h.core.generations().owner_gen, owner_gen_before_sync, "guard 後建鏡不因簿記改變 owner generation");
    assert!(h.input("a", b"still-deferred").iter().all(|effect| !matches!(effect, Effect::HumanTx { .. })));
    assert_eq!(h.deferred_len("a"), b"nestedstill-deferred".len());

    let finished = finish_self_test(&mut h, probe_op);
    let replay: Vec<_> = finished
        .iter()
        .filter_map(|effect| match effect {
            Effect::HumanTx { permit, payload, .. } if permit.class == TxClass::HumanReplay => {
                Some((permit.class, permit.source.clone(), payload.clone()))
            }
            _ => None,
        })
        .collect();
    assert_eq!(replay, vec![(TxClass::HumanReplay, "human:a".into(), b"nestedstill-deferred".to_vec())]);
    assert_eq!(h.core.human_console().ownership.suspend_depth, 0);
    assert_eq!(h.human_tx().iter().filter(|(class, source, payload)| *class == TxClass::HumanReplay && source == "human:a" && payload == b"nestedstill-deferred").count(), 1);
    assert!(h.replay_hold().is_some_and(|hold| !hold.revoked));
    let opened = h.rpc(open_req("agent:explicit-close-replay", 60.0, "", false)).unwrap();
    assert_eq!(opened.get("error_code"), Some(&PyValue::from("SESSION_BUSY")), "明確 close 後的巢狀 self-test replay 不得被標成 tick close");
    h.complete_human_tx(TxOutcome::Completed(b"nestedstill-deferred".len()), None);
}

fn guarded_nested_handback_after_eof(expire_agent_lease: bool) -> (CoreHarness, u64, u64) {
    let mut h = tcp_ready(&["a"]);
    h.set_auto_complete_human_tx(false);
    let probe_op = start_non_strict_self_test_with_timeout(&mut h, 10.0);
    let opened = h
        .rpc(open_req("agent:r5-nested", 1.0, "", false))
        .expect("agent soft preempt");
    assert_eq!(opened.get("soft_preempted"), Some(&PyValue::Bool(true)));
    let agent_id = opened
        .get("interactive_id")
        .and_then(PyValue::as_str)
        .unwrap()
        .to_string();
    assert_eq!(h.core.human_console().ownership.suspend_depth, 2);

    h.fence.with(|fence| fence.flash_critical = true);
    if expire_agent_lease {
        h.advance(Duration::from_secs(2));
        assert!(h.core.active_lease().is_none(), "agent lease timer 應已到期");
    } else {
        h.rpc(close_req(&agent_id))
            .expect("guard 關閉期間明確 close agent lease");
        assert!(h.core.active_lease().is_none());
    }
    assert_eq!(h.core.human_console().ownership.suspend_depth, 1, "close／expiry 立即結算 agent lease 的 suspend");

    h.disconnect("a");
    assert_eq!(h.core.human_console().ownership.suspend_depth, 1, "EOF 依既有 on_drop 保留 self-test suspend");
    h.connect_stream_console("b", ConsoleTransportKind::TcpTelnet);
    let b_lifetime = h.lifetime("b");
    assert_eq!(h.core.human_console().ownership.interactive_owner, None);
    assert_eq!(h.core.human_console().ownership.suspended_owner.as_deref(), Some("human:b"));
    assert!(h.core.active_lease().is_none(), "guard 關閉期間不得保留或建立 B 的 mirror");
    let deferred = h.input("b", b"r5-deferred");
    assert!(deferred.iter().all(|effect| !matches!(effect, Effect::HumanTx { .. })));
    assert_eq!(h.deferred_len("b"), b"r5-deferred".len(), "self-test suspend 中 B 輸入依基線進 deferred");

    let owner_gen_before_resume = h.core.generations().owner_gen;
    let effects = finish_self_test(&mut h, probe_op);
    let replay: Vec<_> = effects
        .iter()
        .filter_map(|effect| match effect {
            Effect::HumanTx { permit, payload, .. } if permit.class == TxClass::HumanReplay => {
                Some((permit.source.clone(), payload.clone()))
            }
            _ => None,
        })
        .collect();
    assert_eq!(replay, vec![("human:b".into(), b"r5-deferred".to_vec())], "guard 關閉時 self-test resume 沿基線回放一次");
    assert_eq!(
        h.human_tx()
            .iter()
            .filter(|(class, source, payload)| *class == TxClass::HumanReplay && source == "human:b" && payload == b"r5-deferred")
            .count(),
        1,
        "B 的 deferred bytes 恰回放一次",
    );
    assert_eq!(h.deferred_len("b"), 0);
    assert_eq!(h.core.human_console().ownership.interactive_owner.as_deref(), Some("human:b"), "self-test resume 依基線提升 B raw owner");
    assert_eq!(h.core.human_console().ownership.suspended_owner, None);
    assert_eq!(h.core.human_console().ownership.suspend_depth, 0);
    assert_eq!(h.core.generations().owner_gen, owner_gen_before_resume + 1, "owner 只在既有 resume 提升時變更");
    h.complete_human_tx(TxOutcome::Completed(b"r5-deferred".len()), None);

    let raw = h.input("b", b"r5-raw\r");
    assert!(raw.iter().any(|effect| matches!(effect, Effect::HumanTx { permit, payload, .. } if permit.class == TxClass::HumanRaw && payload == b"r5-raw\r")), "guard 尚關閉時 B 後續輸入沿基線 raw 路徑送出");
    assert!(!raw.iter().any(|effect| matches!(effect, Effect::StartOp { .. })), "含換行 bytes 不進 line command");
    assert_eq!(h.deferred_len("b"), 0);
    (h, b_lifetime, owner_gen_before_resume + 1)
}

fn assert_guarded_nested_handback_completes_once(
    mut h: CoreHarness,
    b_lifetime: u64,
    owner_gen_before_b: u64,
) {
    let lease_index_start = h.lease_index_ops().len();
    h.fence.with(|fence| fence.flash_critical = false);
    let effects = h.scan(None);

    let mirror = h.core.active_lease().expect("guard 開放後既有 sync 依 B 的 live ownership 建立 mirror");
    let mirror_id = mirror.interactive_id.clone();
    assert_eq!(mirror.owner, "human:b");
    assert_eq!(mirror.mirror_client_lifetime, Some(b_lifetime));
    assert_eq!(h.core.human_console().ownership.interactive_owner.as_deref(), Some("human:b"));
    assert_eq!(h.core.human_console().ownership.suspended_owner, None);
    assert_eq!(h.core.human_console().ownership.suspend_depth, 0);
    assert_eq!(h.core.generations().owner_gen, owner_gen_before_b, "重建 mirror 不得額外提升 owner generation");
    assert_eq!(
        h.lease_index_ops()[lease_index_start..]
            .iter()
            .filter(|op| matches!(op, LeaseIndexOp::InsertTcpMirror { interactive_id } if interactive_id == &mirror_id))
            .count(),
        1,
        "guard 開放後只重建一次 mirror index",
    );
    assert_eq!(h.reg.admission_gate().live_tickets(), 0, "同步 admission ticket 必須結算");

    let replay_effects: Vec<_> = effects
        .iter()
        .filter_map(|effect| match effect {
            Effect::HumanTx { permit, payload, .. } if permit.class == TxClass::HumanReplay => {
                Some((permit.source.clone(), payload.clone()))
            }
            _ => None,
        })
        .collect();
    assert!(replay_effects.is_empty(), "guard 開放的同步不得重播 guard 關閉期間已回放的 bytes");
    assert_eq!(
        h.human_tx()
            .iter()
            .filter(|(class, source, payload)| *class == TxClass::HumanReplay && source == "human:b" && payload == b"r5-deferred")
            .count(),
        1,
        "guard 開放後不得重播，歷史 deferred replay 仍恰一次",
    );

    let subsequent = h.scan(None);
    assert!(subsequent.iter().all(|effect| !matches!(effect, Effect::HumanTx { permit, .. } if permit.class == TxClass::HumanReplay)));
    assert_eq!(h.core.generations().owner_gen, owner_gen_before_b);
    assert_eq!(h.reg.admission_gate().live_tickets(), 0);
}

#[test]
fn tcp_mirror_nested_guarded_handback_after_eof_and_agent_close() {
    let (h, b_lifetime, owner_gen_before_b) = guarded_nested_handback_after_eof(false);
    assert_guarded_nested_handback_completes_once(h, b_lifetime, owner_gen_before_b);
}

#[test]
fn tcp_mirror_nested_guarded_handback_after_eof_and_agent_expiry() {
    let (h, b_lifetime, owner_gen_before_b) = guarded_nested_handback_after_eof(true);
    assert_guarded_nested_handback_completes_once(h, b_lifetime, owner_gen_before_b);
}

#[test]
fn tcp_mirror_nested_guarded_handback_detach_clears_pending_owner() {
    let (mut h, _b_lifetime, _) = guarded_nested_handback_after_eof(false);
    let detached = h
        .rpc(SessionRequest::ConsoleDetach { client_id: "b".into() })
        .expect("detach 已恢復 raw ownership 的 B");
    assert_eq!(detached.get("ok"), Some(&PyValue::Bool(true)));
    assert_eq!(h.core.human_console().ownership.suspend_depth, 0, "detach 不得洩漏 suspend depth");
    assert!(h.core.human_console().ownership.interactive_owner.is_none());

    h.fence.with(|fence| fence.flash_critical = false);
    let effects = h.scan(None);
    assert!(h.core.active_lease().is_none(), "已 detach 的 B 不得建立 mirror");
    assert!(h.core.human_console().ownership.interactive_owner.is_none());
    assert!(effects.iter().all(|effect| !matches!(effect, Effect::HumanTx { permit, .. } if permit.class == TxClass::HumanReplay)));
    assert_eq!(
        h.human_tx()
            .iter()
            .filter(|(class, source, payload)| *class == TxClass::HumanReplay && source == "human:b" && payload == b"r5-deferred")
            .count(),
        1,
        "detach 不撤銷 guard 關閉期間已由基線 resume 回放的 deferred bytes",
    );
    assert_eq!(h.reg.admission_gate().live_tickets(), 0);
}

#[test]
fn tcp_mirror_tick_close_guard_delayed_restoration_preserves_busy_semantics() {
    let mut h = tcp_ready(&["a"]);
    h.core.set_tcp_console(true);
    h.set_auto_complete_human_tx(false);
    h.rpc(open_req("agent:tick", 1.0, "", false)).expect("agent soft preempt");
    h.input("a", b"tick-deferred");
    assert!(h.human_tx().is_empty(), "soft-preempt 期間輸入須先留在 deferred");

    h.fence.with(|fence| fence.flash_critical = true);
    h.advance_s(2.0); // timer 到期；立即沿 execute_post_close resume 並開始 replay。
    assert!(h.core.active_lease().is_none(), "到期 agent lease 已關閉");
    assert_eq!(h.core.human_console().ownership.suspend_depth, 0);
    assert_eq!(h.core.human_console().ownership.interactive_owner.as_deref(), Some("human:a"));
    assert!(h.core.stashed_human_lease().is_none(), "guard 關閉時撤除 TCP stash mirror 簿記");
    assert_eq!(h.human_tx().len(), 1, "guard 關閉不延後修前 execute_post_close 的 replay");
    assert!(h.replay_hold().is_some_and(|hold| !hold.revoked));

    h.fence.with(|fence| fence.flash_critical = false);
    h.scan(None); // guard 開放後只依 raw owner 重建 mirror，不重播已開始的 bytes。
    let mirror_id = h.core.active_lease().expect("既有同步路徑重建 TCP mirror").interactive_id.clone();
    assert_eq!(h.human_tx().len(), 1, "guard 開放不重複開始 replay");

    let opened = h.rpc(open_req("agent:while-replay", 60.0, "", false)).unwrap();
    assert_eq!(
        h.wire(&opened),
        format!(r#"{{"ok":false,"error_code":"SESSION_INTERACTIVE_BUSY","interactive_session_id":"{mirror_id}"}}"#),
        "tick close 的 replay hold 期間 interactive-open 必須保留精確 BUSY 語意",
    );
    let attached = h
        .rpc(SessionRequest::ConsoleAttach { label: None, pty: None })
        .unwrap();
    assert_eq!(
        h.wire(&attached),
        r#"{"ok":false,"error_code":"SESSION_INTERACTIVE_BUSY","selector":"COM0"}"#,
        "tick close 的 replay hold 期間 TCP console attach 必須回精確 BUSY",
    );

    let tx_before_command = h.tx_effect_count();
    let mut req = agent_submit("must-not-run");
    req.mode = "interactive".into();
    let submitted = h.rpc(SessionRequest::Submit(req)).unwrap();
    let cmd_id = submitted["cmd_id"].as_str().unwrap().to_string();
    assert_eq!(h.tx_effect_count(), tx_before_command, "回放未完成時不得送 command TX");

    h.complete_human_tx(TxOutcome::Completed(b"tick-deferred".len()), None);
    let row = h.reg.get(&cmd_id).expect("interactive command 終態");
    assert_eq!(row["error_code"], PyValue::from("SESSION_INTERACTIVE_BUSY"), "{row:?}");
    assert_eq!(h.tx_effect_count(), tx_before_command, "tick close 的 interactive command 必須零 command TX");
    assert_eq!(h.core.active_lease().map(|lease| lease.owner.as_str()), Some("human:a"));
}

#[test]
fn tcp_mirror_delayed_replacement_eof_settles_suspend_without_grant() {
    let mut h = tcp_ready(&["a"]);
    let opened = h.rpc(open_req("agent:replace", 60.0, "", false)).expect("agent soft preempt");
    let agent_id = opened.get("interactive_id").and_then(PyValue::as_str).unwrap().to_string();
    h.fence.with(|fence| fence.flash_critical = true);

    h.disconnect("a");
    h.connect_stream_console("b", ConsoleTransportKind::TcpTelnet);
    assert_eq!(h.core.human_console().ownership.interactive_owner.as_deref(), Some("agent:replace"), "agent 仍 active 時既有規則不讓 B 立即接班");
    assert_eq!(h.core.human_console().ownership.suspended_owner.as_deref(), Some("human:b"));
    let deferred = h.input("b", b"replacement\r");
    assert!(deferred.iter().all(|effect| !matches!(effect, Effect::HumanTx { .. })));
    assert_eq!(h.deferred_len("b"), b"replacement\r".len());
    h.rpc(close_req(&agent_id)).expect("guard 關閉期間 close replacement");
    assert_eq!(h.core.human_console().ownership.suspend_depth, 0);
    assert!(h.core.active_lease().is_none());
    assert!(h.core.stashed_human_lease().is_none(), "replacement owner 不保留 mirror stash");
    assert_eq!(h.core.human_console().ownership.interactive_owner.as_deref(), Some("human:b"), "既有 resume 路徑提升 replacement owner");
    assert_eq!(
        h.human_tx()
            .iter()
            .filter(|(class, source, payload)| *class == TxClass::HumanReplay && source == "human:b" && payload == b"replacement\r")
            .count(),
        1,
        "guard 關閉時沿基線回放 B deferred 一次",
    );

    // B 已恢復 raw ownership 時，C 沿既有第二 console 規則等待 B 讓位。
    h.connect_stream_console("c", ConsoleTransportKind::TcpTelnet);
    assert_eq!(h.core.human_console().ownership.interactive_owner.as_deref(), Some("human:b"));
    h.disconnect("b");
    assert_eq!(h.core.human_console().ownership.suspend_depth, 0);
    assert_eq!(h.core.human_console().ownership.interactive_owner, None, "B EOF 沿既有 on_drop 清除 raw owner");
    assert!(h.core.active_lease().is_none());
    assert!(h.core.stashed_human_lease().is_none());

    h.scan(None);
    assert_eq!(h.core.human_console().ownership.interactive_owner, None, "guard 關閉時不自動提升 C");
    h.fence.with(|fence| fence.flash_critical = false);
    let inserts_before_sync = h.lease_index_ops().len();
    h.scan(None);
    assert_eq!(h.core.human_console().ownership.interactive_owner.as_deref(), Some("human:c"), "guard 開放後 self-heal 依 live primary 接班");
    assert_eq!(h.core.active_lease().map(|lease| lease.owner.as_str()), Some("human:c"));
    assert_eq!(
        h.lease_index_ops()[inserts_before_sync..]
            .iter()
            .filter(|op| matches!(op, LeaseIndexOp::InsertTcpMirror { .. }))
            .count(),
        1,
        "guard 開放後依 live connection 只建立一次 mirror",
    );
}

#[test]
fn tcp_mirror_delayed_same_id_replacement_eof_settles_suspend() {
    let mut h = tcp_ready(&["a"]);
    let opened = h.rpc(open_req("agent:same-id-replace", 60.0, "", false)).expect("agent soft preempt");
    let agent_id = opened.get("interactive_id").and_then(PyValue::as_str).unwrap().to_string();
    h.fence.with(|fence| fence.flash_critical = true);

    h.rpc(close_req(&agent_id)).expect("guard 關閉期間 close agent lease");
    assert_eq!(h.core.human_console().ownership.suspend_depth, 0);
    assert!(h.core.stashed_human_lease().is_none(), "TCP stash mirror guard 關閉時立即撤除");
    assert_eq!(h.core.human_console().ownership.interactive_owner.as_deref(), Some("human:a"));

    // 同一個 human owner 以新 lifetime 取代 TCP client；guard 關閉時只保留既有 raw 路由。
    let replacement_lifetime = h.connect_stream_console("a", ConsoleTransportKind::TcpTelnet);
    assert_eq!(h.core.human_console().consoles["a"].client_lifetime, replacement_lifetime);
    assert!(h.core.active_lease().is_none(), "flash guard 關閉時不建立替換 client mirror");
    assert_eq!(h.core.human_console().ownership.interactive_owner.as_deref(), Some("human:a"));

    h.disconnect("a");
    assert_eq!(h.core.human_console().ownership.suspend_depth, 0);
    assert_eq!(h.core.human_console().ownership.interactive_owner, None, "EOF 不重授已失效的 owner");
    assert!(h.core.stashed_human_lease().is_none());
}

#[test]
fn tcp_mirror_invalid_stash_replacement_drops_deferred_before_resume() {
    let mut h = tcp_ready(&["a"]);
    let opened = h.rpc(open_req("agent:replacement", 60.0, "", false)).expect("agent soft preempt");
    let agent_id = opened.get("interactive_id").and_then(PyValue::as_str).unwrap().to_string();
    h.seed_deferred("a", b"stale-stash-input");
    let old_lifetime = h.lifetime("a");

    // 模擬 replacement record 已更新、agent close 先於下一輪 sync 結算的交錯，
    // 讓 close_lease_inner 同時看到舊 stash 與新 TCP lifetime。
    let new_lifetime = old_lifetime + 1;
    h.core.human_console_mut().consoles.get_mut("a").unwrap().client_lifetime = new_lifetime;
    assert_ne!(new_lifetime, old_lifetime);
    assert_eq!(h.deferred_len("a"), b"stale-stash-input".len());

    h.fence.with(|fence| fence.flash_critical = true);
    h.rpc(close_req(&agent_id)).expect("關閉 agent lease 並同步替換 owner");
    assert!(h.core.active_lease().is_none(), "guard 關閉時不建立替換 mirror");
    assert_eq!(h.core.human_console().ownership.interactive_owner.as_deref(), Some("human:a"), "close 仍經既有 resume 路徑恢復 live raw owner");
    assert_eq!(h.deferred_len("a"), 0, "即使 mirror 受 guard 延後，失效 stash deferred 也須立即清除");
    assert!(
        h.warns().iter().any(|warning| warning.contains("client_id=a reason=lease_dropped dropped=17")),
        "清除有內容的失效 stash 時須保留 PostClose Warn 效果：{:?}",
        h.warns(),
    );

    h.fence.with(|fence| fence.flash_critical = false);
    let inserts_before_sync = h.lease_index_ops().len();
    h.scan(None);
    assert_eq!(h.core.active_lease().map(|lease| lease.owner.as_str()), Some("human:a"));
    assert_eq!(h.core.active_lease().and_then(|lease| lease.mirror_client_lifetime), Some(new_lifetime));
    assert!(h.human_tx().is_empty(), "失效 stash 的位元組不可被回放到新 lifetime");
    assert_eq!(
        h.lease_index_ops()[inserts_before_sync..]
            .iter()
            .filter(|op| matches!(op, LeaseIndexOp::InsertTcpMirror { .. }))
            .count(),
        1,
        "同步依新的 live lifetime 只重建一次 mirror",
    );
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
