#![cfg(feature = "test-fakes")]

mod common;
mod support;

use std::time::Duration;

use support::*;
use sw_core::mailbox::{ControlEvent, DetachReason};
use sw_core::op_runner::OpOutcome;
use sw_core::op_runner::OpSpec;
use sw_core::session_core::{Effect, SessionRequest};
use sw_pal::traits::TxOutcome;
use sw_proto::pyjson::{PyDict, PyValue};

fn tx_row_count(h: &ActorRig) -> usize {
    h.wal_rows()
        .iter()
        .filter(|row| row.get("dir").and_then(PyValue::as_str) == Some("TX"))
        .count()
}

fn has_self_test_probe(effects: &[Effect]) -> bool {
    effects.iter().any(|effect| {
        matches!(
            effect,
            Effect::StartOp {
                spec: OpSpec::SelfTestProbe { .. },
                ..
            }
        )
    })
}

fn assert_busy_reply(reply: &PyDict, classification: &str) {
    assert_eq!(reply["ok"], PyValue::Bool(true), "{reply:?}");
    assert_eq!(
        reply["classification"],
        PyValue::from(classification),
        "{reply:?}"
    );
    assert_eq!(
        reply["recommended_action"],
        PyValue::from("wait"),
        "{reply:?}"
    );
    assert!(
        reply.get("probe_ok").is_none(),
        "busy response 不得宣稱 probe 結果：{reply:?}"
    );
}

fn self_test_req() -> SessionRequest {
    SessionRequest::SelfTest {
        timeout_s: 2.0,
        strict_human_lock: false,
    }
}

fn open_interactive(owner: &str) -> SessionRequest {
    SessionRequest::InteractiveOpen {
        owner: owner.to_string(),
        timeout_s: 30.0,
        command: String::new(),
        allow_attached: false,
    }
}

fn close_interactive(id: &str) -> SessionRequest {
    SessionRequest::InteractiveClose {
        interactive_id: id.to_string(),
    }
}

#[test]
fn foreground_command_busy_self_test_is_passive() {
    let h = ActorRig::new(SHELL_PROFILE).ready();
    let tx_offset = h.dev.tx_len();
    let submit_offset = h.dev.os_submits();
    let wal_offset = tx_row_count(&h);
    let cmd_id = h.submit_ok("sleep 20");
    h.wait_running(&cmd_id);
    h.wait_for("前景命令 TX 已完整交給假 OS", |h| {
        h.dev.os_submits() > submit_offset
            && h.dev.tx_since(tx_offset).ends_with(b"sleep 20\n")
            && tx_row_count(h) > wal_offset
    });

    let before_bytes = h.dev.tx_len();
    let before_writes = h.dev.write_calls();
    let before_submits = h.dev.os_submits();
    let before_wal = tx_row_count(&h);
    let before_state = h.get_state();

    let r = h.request(self_test_req());
    assert_busy_reply(&r, "SESSION_BUSY");
    assert_eq!(
        h.dev.tx_len(),
        before_bytes,
        "前景忙碌時不得新增 UART bytes"
    );
    assert_eq!(
        h.dev.write_calls(),
        before_writes,
        "前景忙碌時不得新增 UART write"
    );
    assert_eq!(
        h.dev.os_submits(),
        before_submits,
        "前景忙碌時不得新增 OS submit"
    );
    assert_eq!(tx_row_count(&h), before_wal, "前景忙碌時不得新增 WAL TX");
    let after_state = h.get_state();
    assert_eq!(
        after_state["ready_reconfirm_pending"], before_state["ready_reconfirm_pending"],
        "前景忙碌時不應改變 ready_reconfirm_pending"
    );
    assert_eq!(
        after_state["last_probe_at"], before_state["last_probe_at"],
        "前景忙碌不得更新 probe 時間"
    );
    assert_eq!(
        after_state["foreground_busy"],
        before_state["foreground_busy"]
    );
}

#[test]
fn background_capture_busy_self_test_is_passive() {
    let h = ActorRig::new(SHELL_PROFILE).ready();
    h.dev.on_tx_reply(b"sleep 30", b"root@t:/# ");
    let cmd_id = h.submit_with("sleep 30", |req| {
        req.mode = "background".into();
        req.timeout_s = 30.0;
    });
    let command = h.wait_done(&cmd_id);
    assert_eq!(command["execution_mode"], PyValue::from("background"));
    let active_capture = h.request(SessionRequest::ResultTail {
        cmd_id: cmd_id.clone(),
        from_chunk: 0,
        limit: 200,
    });
    assert_eq!(
        active_capture["status"],
        PyValue::from("active"),
        "測試前 capture 必須仍 active"
    );
    let idle_state = h.get_state();
    assert_eq!(
        idle_state["foreground_busy"],
        PyValue::Bool(false),
        "測試前景槽必須已空"
    );
    assert_eq!(
        idle_state["outstanding_commands"],
        PyValue::from(1),
        "只剩 background capture 記錄"
    );

    let before_bytes = h.dev.tx_len();
    let before_writes = h.dev.write_calls();
    let before_submits = h.dev.os_submits();
    let before_wal = tx_row_count(&h);
    let before_state = h.get_state();
    let r = h.request(self_test_req());

    assert_busy_reply(&r, "SESSION_BUSY");
    assert_eq!(
        h.dev.tx_len(),
        before_bytes,
        "背景 capture 活動時不得新增 UART bytes"
    );
    assert_eq!(
        h.dev.write_calls(),
        before_writes,
        "背景 capture 活動時不得新增 UART write"
    );
    assert_eq!(
        h.dev.os_submits(),
        before_submits,
        "背景 capture 活動時不得新增 OS submit"
    );
    assert_eq!(
        tx_row_count(&h),
        before_wal,
        "背景 capture 活動時不得新增 WAL TX"
    );
    let still_active = h.request(SessionRequest::ResultTail {
        cmd_id: cmd_id.clone(),
        from_chunk: 0,
        limit: 200,
    });
    assert_eq!(
        still_active["status"],
        PyValue::from("active"),
        "busy self-test 不得 finalize capture"
    );
    let after_state = h.get_state();
    assert_eq!(after_state["foreground_busy"], PyValue::Bool(false));
    assert_eq!(after_state["outstanding_commands"], PyValue::from(1));
    assert_eq!(
        after_state["ready_reconfirm_pending"], before_state["ready_reconfirm_pending"],
        "背景 capture 活動時不應改變 ready_reconfirm_pending"
    );
    assert_eq!(after_state["last_probe_at"], before_state["last_probe_at"]);

    h.advance(Duration::from_millis(2_100));
    let completed_capture = h.request(SessionRequest::ResultTail {
        cmd_id,
        from_chunk: 0,
        limit: 200,
    });
    assert_eq!(completed_capture["status"], PyValue::from("done"));
    let after_capture = h.request(self_test_req());
    assert_eq!(
        after_capture["classification"],
        PyValue::from("OK"),
        "歷史 capture 不得永久 busy"
    );
    assert_eq!(after_capture["probe_ok"], PyValue::Bool(true));
}

#[test]
fn agent_lease_busy_self_test_is_passive() {
    let h = ActorRig::new(SHELL_PROFILE).ready();
    let r = h.request(open_interactive("agent:a"));
    let interactive_id = r["interactive_id"].as_str().unwrap().to_string();
    let before_bytes = h.dev.tx_len();
    let before_writes = h.dev.write_calls();
    let before_submits = h.dev.os_submits();
    let before_wal_tx = tx_row_count(&h);
    let before_state = h.get_state();

    let r = h.request(self_test_req());
    assert_eq!(r["ok"], PyValue::Bool(true));
    assert_eq!(
        r["classification"],
        PyValue::from("SESSION_INTERACTIVE_BUSY")
    );
    assert_eq!(r["recommended_action"], PyValue::from("wait"));
    assert!(
        r.get("probe_ok").is_none(),
        "agent lease 活動時不應有 probe_ok: {r:?}"
    );
    assert_eq!(
        h.dev.tx_len(),
        before_bytes,
        "agent lease 活動時不得新增 UART bytes"
    );
    assert_eq!(
        h.dev.write_calls(),
        before_writes,
        "agent lease 活動時不得新增 UART write"
    );
    assert_eq!(
        h.dev.os_submits(),
        before_submits,
        "agent lease 活動時不得新增 OS submit"
    );
    assert_eq!(
        tx_row_count(&h),
        before_wal_tx,
        "agent lease 活動時不得新增 WAL TX"
    );
    assert_eq!(
        h.get_state()["ready_reconfirm_pending"],
        before_state["ready_reconfirm_pending"],
        "agent lease 活動時不應改變 ready_reconfirm_pending"
    );
    assert_eq!(
        h.get_state()["last_probe_at"],
        before_state["last_probe_at"]
    );

    let close = h.request(close_interactive(&interactive_id));
    assert_eq!(close["ok"], PyValue::Bool(true));
}

#[test]
fn file_transfer_and_interactive_send_block_self_test() {
    use support::file_transfer_session::{pull_req, push_req};

    for request in [
        push_req("/local", "/remote"),
        pull_req("/remote", Some("/local")),
    ] {
        let mut h = CoreHarness::new(SHELL_PROFILE);
        h.to_ready();
        let (_transfer_reply, transfer_effects) =
            h.rpc_deferred(SessionRequest::FileTransfer(request));
        assert!(
            transfer_effects.iter().any(|effect| matches!(
                effect,
                Effect::StartOp {
                    spec: OpSpec::FileTransfer(_),
                    ..
                }
            )),
            "push／pull 都必須先建立前景傳輸 op"
        );
        let (reply, effects) = h.rpc_full(self_test_req());
        assert_busy_reply(&reply.expect("self-test busy"), "SESSION_BUSY");
        assert!(
            !has_self_test_probe(&effects),
            "傳輸期間不得建立 SelfTestProbe"
        );
    }

    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    let open = h.attach_pty_console("c1", true);
    let interactive_id = open["interactive_session_id"]
        .as_str()
        .expect("interactive_id")
        .to_string();
    let suspend_depth = h.core.human_console().ownership.suspend_depth;
    let (_send_reply, send_effects) = h.rpc_deferred(SessionRequest::InteractiveSend {
        interactive_id,
        data: "x".into(),
        encoding: "plain".into(),
    });
    assert!(
        send_effects.iter().any(|effect| matches!(
            effect,
            Effect::StartOp {
                spec: OpSpec::InteractiveSend { .. },
                ..
            }
        )),
        "interactive_send 必須先建立前景 op"
    );
    let (reply, effects) = h.rpc_full(self_test_req());
    assert_busy_reply(&reply.expect("self-test busy"), "SESSION_BUSY");
    assert!(
        !has_self_test_probe(&effects),
        "interactive_send 期間不得建立 SelfTestProbe"
    );
    assert_eq!(
        h.core.human_console().ownership.suspend_depth,
        suspend_depth,
        "busy self-test 不得增加 human suspend depth"
    );
}

#[test]
fn replay_hold_blocks_self_test_until_settled() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    h.set_auto_complete_human_tx(false);
    h.attach_pty_console("c1", true);
    let (admission, _) = h.fg_begin(true);
    h.input("c1", b"x");
    let replay_effects = h.fg_finish(admission);
    assert!(
        h.replay_hold().is_some(),
        "回放尚未結算時必須保留 replay hold"
    );
    assert!(replay_effects
        .iter()
        .any(|effect| matches!(effect, Effect::HumanTx { .. })));

    let (reply, effects) = h.rpc_full(self_test_req());
    assert_busy_reply(&reply.expect("self-test busy"), "SESSION_BUSY");
    assert!(
        !has_self_test_probe(&effects),
        "replay hold 期間不得建立 SelfTestProbe"
    );

    h.complete_human_tx(TxOutcome::Completed(1), None);
    assert!(h.replay_hold().is_none(), "TX 結算後 replay hold 必須解除");
    let (_reply, effects) = h.rpc_deferred(self_test_req());
    assert!(
        has_self_test_probe(&effects),
        "replay hold 結算後 self-test 可執行"
    );
}

#[test]
fn queued_command_is_not_overtaken_by_self_test() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    let first = h.start_foreground_command("echo first");
    let second = h
        .rpc(SessionRequest::Submit(agent_submit("echo second")))
        .expect("第二筆命令入列")["cmd_id"]
        .as_str()
        .expect("cmd_id")
        .to_string();

    let (reply, effects) = h.rpc_full(self_test_req());
    assert_busy_reply(&reply.expect("queued command busy"), "SESSION_BUSY");
    assert!(
        !has_self_test_probe(&effects),
        "已受理命令不得被 self-test 插隊"
    );

    let canceled = h
        .rpc(SessionRequest::CancelQueued { cmd_id: second })
        .expect("取消排隊命令");
    assert_eq!(canceled["ok"], PyValue::Bool(true));
    let mut command_result = PyDict::new();
    command_result.insert("ok", PyValue::Bool(true));
    command_result.insert("stdout", PyValue::from("first"));
    let finish = h.feed(ControlEvent::OpFinished {
        op_id: first.op_id,
        epoch: h.epoch(),
        outcome: OpOutcome::CommandResult(command_result),
        after_rx_seq: None,
    });
    assert!(
        finish.iter().all(|effect| !matches!(
            effect,
            Effect::StartOp {
                spec: OpSpec::Command(_),
                ..
            }
        )),
        "取消的 queue residue 不得重新出列"
    );
    let (_reply, effects) = h.rpc_deferred(self_test_req());
    assert!(
        has_self_test_probe(&effects),
        "終態 queue residue 不得永久 busy"
    );
}

#[test]
fn agent_lease_and_probe_operations_block_self_test() {
    let mut agent = CoreHarness::new(SHELL_PROFILE);
    agent.to_ready();
    let opened = agent
        .rpc(SessionRequest::InteractiveOpen {
            owner: "agent:busy".into(),
            timeout_s: 30.0,
            command: String::new(),
            allow_attached: false,
        })
        .expect("agent lease");
    assert_eq!(opened["ok"], PyValue::Bool(true));
    let (reply, effects) = agent.rpc_full(self_test_req());
    assert_busy_reply(
        &reply.expect("agent lease busy"),
        "SESSION_INTERACTIVE_BUSY",
    );
    assert!(!has_self_test_probe(&effects));

    let mut login = CoreHarness::new(SHELL_PROFILE);
    login.attach_device();
    let login_effects = login.bridge_ok();
    assert!(login_effects
        .iter()
        .any(|effect| matches!(effect, Effect::StartLogin { .. })));
    let (reply, effects) = login.rpc_full(self_test_req());
    assert_busy_reply(&reply.expect("login run busy"), "SESSION_BUSY");
    assert!(
        !has_self_test_probe(&effects),
        "login run 期間不得建立 SelfTestProbe"
    );

    let h = ActorRig::new(SHELL_PROFILE).ready();
    let tx_offset = h.dev.tx_len();
    let recover = h.request_async(SessionRequest::Recover {
        timeout_s: 30.0,
        force: false,
    });
    h.wait_for("READY Recover 救援鍵 TX", |h| {
        h.dev.tx_since(tx_offset).contains(&0x03)
    });
    let before_bytes = h.dev.tx_len();
    let before_writes = h.dev.write_calls();
    let before_submits = h.dev.os_submits();
    let before_wal = tx_row_count(&h);
    let before_state = h.get_state();

    let reply = h.request(self_test_req());
    assert_busy_reply(&reply, "SESSION_BUSY");
    assert_eq!(
        h.dev.tx_len(),
        before_bytes,
        "有效 Recover 期間不得新增 probe bytes"
    );
    assert_eq!(h.dev.write_calls(), before_writes);
    assert_eq!(h.dev.os_submits(), before_submits);
    assert_eq!(
        tx_row_count(&h),
        before_wal,
        "有效 Recover 期間不得新增 WAL TX"
    );
    assert_eq!(
        h.get_state()["last_probe_at"],
        before_state["last_probe_at"]
    );
    assert!(
        recover.try_recv().is_err(),
        "probe 不得結束或回覆 Recover waiter"
    );
}

#[test]
fn rebound_recover_waiter_does_not_keep_self_test_busy() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    let (recover, effects) = h.rpc_deferred(SessionRequest::Recover {
        timeout_s: 30.0,
        force: false,
    });
    assert!(effects.iter().any(|effect| matches!(
        effect,
        Effect::StartOp {
            spec: OpSpec::Recover { .. },
            ..
        }
    )));

    h.feed(ControlEvent::DeviceDetached {
        by_id: DEVICE_BY_ID.into(),
        reason: DetachReason::Rebound,
    });
    h.feed(ControlEvent::DeviceAttached {
        by_id: DEVICE_BY_ID.into(),
        real_path: "/fake/tty-B".into(),
    });
    let (epoch, stop_id) = h.pending_stop();
    h.receipt(epoch, stop_id).feed();
    assert!(
        h.core.generations().conn_epoch.is_none(),
        "Rebound drain 已使 bridge epoch 失效"
    );
    assert!(
        recover.try_recv().is_err(),
        "測試須保留尚未到達的舊 Recover waiter"
    );

    let (reply, effects) = h.rpc_full(self_test_req());
    assert_eq!(
        reply.expect("bridge down 回覆")["classification"],
        PyValue::from("BRIDGE_DOWN"),
        "已撤銷 side-op 的舊 waiter 不得再回 SESSION_BUSY"
    );
    assert!(
        !has_self_test_probe(&effects),
        "bridge down 不得建立 SelfTestProbe"
    );
}
