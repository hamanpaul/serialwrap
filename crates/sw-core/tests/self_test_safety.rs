#![cfg(feature = "test-fakes")]

mod common;
mod support;

use std::sync::Arc;
use std::time::Duration;

use support::*;
use sw_core::mailbox::{ControlEvent, DetachReason};
use sw_core::op_runner::OpOutcome;
use sw_core::op_runner::OpSpec;
use sw_core::self_test::SelfTestProbes;
use sw_core::session_core::{Effect, SessionRequest};
use sw_core::tx_writer::OpId;
use sw_core::uart_bridge::WorkerSnapshot;
use sw_pal::fakes::serial::FakeWriteStep;
use sw_pal::traits::TxOutcome;
use sw_proto::pydict;
use sw_proto::pyjson::{PyDict, PyValue};

fn tx_row_count(h: &ActorRig) -> usize {
    h.wal_rows()
        .iter()
        .filter(|row| row.get("dir").and_then(PyValue::as_str) == Some("TX"))
        .count()
}

fn wait_for_recover_tx_wal(h: &ActorRig) {
    h.wait_for("Recover 救援鍵 WAL TX", |h| {
        h.wal_rows().iter().any(|row| {
            row.get("dir").and_then(PyValue::as_str) == Some("TX")
                && row.get("source").and_then(PyValue::as_str) == Some("system:recover")
        })
    });
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

fn self_test_op_id(effects: &[Effect]) -> (OpId, u64) {
    effects
        .iter()
        .find_map(|effect| match effect {
            Effect::StartOp {
                grant,
                spec: OpSpec::SelfTestProbe { .. },
            } => Some((grant.op_id, grant.epoch)),
            _ => None,
        })
        .expect("SelfTestProbe StartOp")
}

fn verified_self_test_outcome() -> OpOutcome {
    OpOutcome::SelfTest(pydict! {
        "probe_ok" => true,
        "probe_evidence" => pydict! {
            "tx_complete" => true,
            "execution_marker" => true,
            "probe_rc_zero" => true,
            "prompt_after_marker" => true,
        },
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

const FAILED_RC_PROFILE: &str = "profiles:\n  t:\n    platform: shell\n    prompt_regex: '(?m)^root@t:.*# '\n    ready_probe: 'echo __READY__${nonce}; false'\n    timeout_s: 2.0\ntargets:\n  - {com: COM0, profile: t, act_no: 1, alias: t+1, device_by_id: /s/by-id/usb-A}\n";

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

fn install_executing_shell_target(h: &ActorRig) {
    h.dev.clear_replies();
    h.dev.on_tx_match(
        r"__READY__[0-9a-f]+",
        Box::new(|line: &[u8]| {
            let text = String::from_utf8_lossy(line).into_owned();
            let token = text
                .split("__READY__")
                .nth(1)
                .unwrap_or("")
                .chars()
                .take_while(|c| c.is_ascii_hexdigit())
                .collect::<String>();
            let expected =
                format!("( echo __READY__{token} ) && printf '\\n__SW_ST_%s_OK__\\n' '{token}'");
            if token.len() == 8 && text.contains(&expected) {
                format!("__READY__{token}\r\n__SW_ST_{token}_OK__\r\nroot@t:/# ").into_bytes()
            } else {
                format!("__READY__{token}\r\nroot@t:/# ").into_bytes()
            }
        }),
    );
}

fn read_self_test_probe(h: &ActorRig, tx_start: usize) -> (String, String) {
    h.wait_for("self-test probe TX", |rig| rig.dev.tx_len() > tx_start);
    let tx = String::from_utf8_lossy(&h.dev.tx_since(tx_start)).into_owned();
    let nonce = tx
        .split("__READY__")
        .nth(1)
        .unwrap_or("")
        .chars()
        .take_while(|c| c.is_ascii_hexdigit())
        .collect::<String>();
    assert_eq!(nonce.len(), 8, "probe TX 應含完整 nonce：{tx:?}");
    (tx, nonce)
}

fn receive_self_test_after_deadline(
    h: &ActorRig,
    pending: crossbeam_channel::Receiver<Result<PyDict, sw_proto::pycompat::PyErr>>,
) -> PyDict {
    h.advance(Duration::from_secs(3));
    pending
        .recv_timeout(Duration::from_secs(2))
        .expect("self-test 應於兩階段期限內結束")
        .expect("self-test RPC 應回字典")
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
fn recover_waiter_remains_busy_after_human_console_attach() {
    let h = ActorRig::new_with_hub(SHELL_PROFILE).ready();
    let tx_offset = h.dev.tx_len();
    let recover = h.request_async(SessionRequest::Recover {
        timeout_s: 30.0,
        force: false,
    });
    h.wait_for("Recover 救援鍵 TX", |h| {
        h.dev.tx_since(tx_offset).contains(&0x03)
    });
    wait_for_recover_tx_wal(&h);

    let _console = h.attach_pty_console("human-recover");
    assert!(
        !h.get_state()["interactive_session_id"].is_null(),
        "human console attach 必須建立 lease 並推進 owner 世代"
    );

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
        "有效 Recover 期間不得新增 UART bytes"
    );
    assert_eq!(
        h.dev.write_calls(),
        before_writes,
        "有效 Recover 期間不得新增 UART write"
    );
    assert_eq!(
        h.dev.os_submits(),
        before_submits,
        "有效 Recover 期間不得新增 OS submit"
    );
    assert_eq!(
        tx_row_count(&h),
        before_wal,
        "有效 Recover 期間不得新增 WAL TX"
    );
    assert_eq!(
        h.get_state()["last_probe_at"],
        before_state["last_probe_at"],
        "有效 Recover 期間不得更新 probe 時間"
    );
    assert!(
        recover.try_recv().is_err(),
        "self-test 不得結束 Recover waiter"
    );
}

#[test]
fn agent_lease_does_not_override_passthrough_classification() {
    let mut h = CoreHarness::new(PASSTHROUGH_PROFILE);
    h.to_attached(None);
    h.force_state(sw_core::session_core::SessionState::Ready);
    let opened = h.rpc(open_interactive("agent:passthrough")).unwrap();
    assert_eq!(opened["ok"], PyValue::Bool(true));

    let (reply, effects) = h.rpc_full(self_test_req());
    let reply = reply.expect("passthrough 分類");
    assert_eq!(reply["classification"], PyValue::from("PASSTHROUGH"));
    assert_eq!(reply["recommended_action"], PyValue::from("none"));
    assert!(!has_self_test_probe(&effects), "PASSTHROUGH 不得建立 probe");
}

#[test]
fn agent_lease_does_not_override_quiet_classification() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    h.rx(b"U-Boot 2024.01\r\n");
    let opened = h.rpc(open_interactive("agent:quiet")).unwrap();
    assert_eq!(opened["ok"], PyValue::Bool(true));

    let (reply, effects) = h.rpc_full(self_test_req());
    let reply = reply.expect("quiet 分類");
    assert_eq!(reply["classification"], PyValue::from("AUTOBOOT_QUIET"));
    assert_eq!(reply["recommended_action"], PyValue::from("wait"));
    assert!(
        !has_self_test_probe(&effects),
        "AUTOBOOT_QUIET 不得建立 probe"
    );
}

#[test]
fn agent_lease_does_not_override_bridge_down_classification() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    let opened = h.rpc(open_interactive("agent:bridge-down")).unwrap();
    assert_eq!(opened["ok"], PyValue::Bool(true));
    h.core.set_self_test_probes(SelfTestProbes {
        bridge_snapshot: Some(Arc::new(|_| {
            Some(WorkerSnapshot {
                serial_alive: false,
                running: false,
            })
        })),
        ..SelfTestProbes::default()
    });

    let (reply, effects) = h.rpc_full(self_test_req());
    let reply = reply.expect("bridge 異常分類");
    assert_eq!(reply["classification"], PyValue::from("BRIDGE_DOWN"));
    assert_eq!(reply["recommended_action"], PyValue::from("recover"));
    assert!(!has_self_test_probe(&effects), "BRIDGE_DOWN 不得建立 probe");
}

#[test]
fn accepted_queue_blocks_self_test_without_foreground_slot() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    let cmd_id = "accepted-queue-only".to_string();
    h.core
        .seed_accepted_command_queue_for_test(agent_submit("echo queued"), cmd_id.clone())
        .expect("建立有效 accepted queue 項目");
    let command = h.reg.get(&cmd_id).expect("queue 命令登錄");
    assert_eq!(command["status"], PyValue::from("accepted"));
    assert_eq!(h.core.state(), sw_core::session_core::SessionState::Ready);
    assert!(!h.core.foreground_busy(), "測試時不得有前景槽持有者");

    let before = h.public()["last_probe_at"].clone();
    let (reply_rx, effects) = h.rpc_deferred(self_test_req());
    assert!(
        !has_self_test_probe(&effects),
        "有效 queue 不得被 probe 插隊"
    );
    let reply = reply_rx.try_recv().expect("accepted queue busy").unwrap();
    assert_busy_reply(&reply, "SESSION_BUSY");
    assert_eq!(h.fake_device.tx_len(), 0, "有效 queue 期間不得新增 UART TX");
    assert_eq!(
        h.public()["last_probe_at"],
        before,
        "有效 queue 不得更新 probe 時間"
    );
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
    wait_for_recover_tx_wal(&h);
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

#[test]
fn idle_probe_reserves_slot_before_tx() {
    let h = ActorRig::new(SHELL_PROFILE).ready();
    install_executing_shell_target(&h);
    h.dev.script_write(vec![FakeWriteStep::PauseBeforeSubmit]);
    let first_tx = h.dev.tx_len();
    let first = h.request_async(self_test_req());
    assert!(
        h.dev.wait_paused(Duration::from_secs(2)),
        "probe 應停在提交前"
    );

    let second = h.request_async(self_test_req());
    let second_reply = second.recv_timeout(Duration::from_millis(100)).ok();
    h.dev.on_tx_reply(b"i306-slot-command\n", b"root@t:/# ");
    let cmd_id = h.submit_ok("i306-slot-command");
    let command_started_while_probe_paused =
        h.get_state()["foreground_busy"] == PyValue::Bool(true);
    let tx_unchanged_before_release = h.dev.tx_len() == first_tx;

    h.dev.release_pause();
    let (probe_tx, _) = read_self_test_probe(&h, first_tx);
    let first_reply = first
        .recv_timeout(Duration::from_secs(5))
        .expect("第一個 probe 應完成")
        .expect("第一個 self-test 應回字典");
    h.wait_done(&cmd_id);
    let second_reply = second_reply.or_else(|| second.recv_timeout(Duration::from_secs(5)).ok());
    let tx = String::from_utf8_lossy(&h.dev.tx_since(first_tx)).into_owned();
    let command_after_probe = tx
        .find("i306-slot-command\n")
        .is_some_and(|command_at| command_at >= probe_tx.len());

    let interactive_rig = ActorRig::new(SHELL_PROFILE).ready();
    install_executing_shell_target(&interactive_rig);
    interactive_rig
        .dev
        .script_write(vec![FakeWriteStep::PauseBeforeSubmit]);
    let interactive_tx = interactive_rig.dev.tx_len();
    let active_probe = interactive_rig.request_async(self_test_req());
    assert!(
        interactive_rig.dev.wait_paused(Duration::from_secs(2)),
        "第二個 probe 應停在提交前"
    );
    let interactive = interactive_rig.request(open_interactive("agent:interleaver"));
    let interactive_refused = interactive.get("error_code") == Some(&PyValue::from("SESSION_BUSY"));
    interactive_rig.dev.release_pause();
    let _ = read_self_test_probe(&interactive_rig, interactive_tx);
    let _ = active_probe
        .recv_timeout(Duration::from_secs(5))
        .expect("interactive 交錯 probe 應完成")
        .expect("interactive 交錯 self-test 應回字典");

    let second_is_busy = second_reply
        .and_then(Result::ok)
        .is_some_and(|reply| reply["classification"].as_str() == Some("SESSION_BUSY"));
    assert!(second_is_busy, "第二次 self-test 應被動回覆 busy");
    assert!(
        tx_unchanged_before_release,
        "probe 提交前 UART 不應有額外 TX"
    );
    assert!(
        !command_started_while_probe_paused,
        "probe 保留前景槽時 command 必須等待"
    );
    assert!(
        command_after_probe,
        "command bytes 必須排在 probe payload 後"
    );
    assert!(
        interactive_refused,
        "probe 保留前景槽時 interactive open 必須回 SESSION_BUSY"
    );
    assert_eq!(first_reply["probe_ok"], PyValue::Bool(true));
}

#[test]
fn echo_only_never_confirms_ready() {
    for segmented in [false, true] {
        for with_prompt in [false, true] {
            let h = ActorRig::new(SHELL_PROFILE).ready();
            h.dev.clear_replies();
            let tx_start = h.dev.tx_len();
            let pending = h.request_async(self_test_req());
            let (_, nonce) = read_self_test_probe(&h, tx_start);
            let echo = format!("__READY__{nonce}\r\n");
            if segmented {
                let split = echo.find(&nonce).unwrap() + nonce.len() / 2;
                h.inject_rx(&echo.as_bytes()[..split]);
                h.inject_rx(&echo.as_bytes()[split..]);
            } else {
                h.inject_rx(echo.as_bytes());
            }
            if with_prompt {
                h.inject_rx(b"root@t:/# ");
            }
            let reply = receive_self_test_after_deadline(&h, pending);
            assert_eq!(
                reply["probe_ok"],
                PyValue::Bool(false),
                "segmented={segmented}, with_prompt={with_prompt}: {reply:?}"
            );
            let PyValue::Dict(evidence) = &reply["probe_evidence"] else {
                panic!("probe_evidence 應為字典：{reply:?}");
            };
            assert_eq!(evidence["tx_complete"], PyValue::Bool(true));
            assert_eq!(evidence["execution_marker"], PyValue::Bool(false));
            assert_eq!(evidence["probe_rc_zero"], PyValue::Bool(false));
            assert_eq!(evidence["prompt_after_marker"], PyValue::Bool(false));
        }
    }
}

#[test]
fn execution_marker_requires_following_prompt() {
    for response in [0, 1, 2, 3] {
        let h = ActorRig::new(SHELL_PROFILE).ready();
        h.dev.clear_replies();
        let tx_start = h.dev.tx_len();
        let pending = h.request_async(self_test_req());
        let (_, nonce) = read_self_test_probe(&h, tx_start);
        let marker = match response {
            0 => format!("__SW_ST_{nonce}_OK__\r\n"),
            1 => format!("root@t:/# __SW_ST_{nonce}_OK__\r\n"),
            2 => "__SW_ST_00000000_OK__\r\nroot@t:/# ".to_string(),
            _ => format!("__SW_ST_{nonce}_\r\nnoise_OK__\r\nroot@t:/# "),
        };
        h.inject_rx(marker.as_bytes());
        let reply = receive_self_test_after_deadline(&h, pending);
        assert_eq!(
            reply["probe_ok"],
            PyValue::Bool(false),
            "response={response}: {reply:?}"
        );
        let PyValue::Dict(evidence) = &reply["probe_evidence"] else {
            panic!("probe_evidence 應為字典：{reply:?}");
        };
        assert_eq!(evidence["tx_complete"], PyValue::Bool(true));
        assert_eq!(evidence["execution_marker"], PyValue::Bool(response == 0));
        assert_eq!(evidence["probe_rc_zero"], PyValue::Bool(response == 0));
        assert_eq!(evidence["prompt_after_marker"], PyValue::Bool(false));
    }
}

#[test]
fn marker_and_prompt_in_same_chunk_confirm_ready() {
    let h = ActorRig::new(SHELL_PROFILE).ready();
    install_executing_shell_target(&h);
    let tx_start = h.dev.tx_len();
    let pending = h.request_async(self_test_req());
    let (tx, nonce) = read_self_test_probe(&h, tx_start);
    assert!(tx.contains("( echo __READY__"));
    assert!(
        !tx.contains(&format!("__SW_ST_{nonce}_OK__")),
        "完整執行標記不得出現在 TX 回顯中"
    );
    let reply = pending
        .recv_timeout(Duration::from_secs(2))
        .expect("marker 與 prompt 同 chunk 應完成")
        .expect("self-test 回應應為字典");
    assert_eq!(reply["probe_ok"], PyValue::Bool(true), "{reply:?}");
    assert_eq!(
        reply["probe_evidence"],
        PyValue::Dict(pydict! {
            "tx_complete" => true,
            "execution_marker" => true,
            "probe_rc_zero" => true,
            "prompt_after_marker" => true,
        })
    );
}

#[test]
fn failed_probe_rc_never_confirms_ready() {
    let h = ActorRig::new(FAILED_RC_PROFILE).ready();
    h.dev.clear_replies();
    h.dev.on_tx_match(
        r"__READY__[0-9a-f]+",
        Box::new(|line: &[u8]| {
            let text = String::from_utf8_lossy(line).into_owned();
            let token = text
                .split("__READY__")
                .nth(1)
                .unwrap_or("")
                .chars()
                .take_while(|c| c.is_ascii_hexdigit())
                .collect::<String>();
            assert!(text.contains(&format!(
                "( echo __READY__{token}; false ) && printf '\\n__SW_ST_%s_OK__\\n' '{token}'"
            )));
            format!("__READY__{token}\r\nroot@t:/# ").into_bytes()
        }),
    );
    let tx_start = h.dev.tx_len();
    let pending = h.request_async(self_test_req());
    let (tx, nonce) = read_self_test_probe(&h, tx_start);
    assert!(tx.contains(&format!("__READY__{nonce}; false")));
    let reply = receive_self_test_after_deadline(&h, pending);
    assert_eq!(reply["probe_ok"], PyValue::Bool(false), "{reply:?}");
    assert_eq!(
        reply["probe_evidence"]["execution_marker"],
        PyValue::Bool(false)
    );
    assert_eq!(
        reply["probe_evidence"]["probe_rc_zero"],
        PyValue::Bool(false)
    );
}

#[test]
fn execution_marker_noise_around_complete_line_is_accepted() {
    let h = ActorRig::new(SHELL_PROFILE).ready();
    h.dev.clear_replies();
    let tx_start = h.dev.tx_len();
    let pending = h.request_async(self_test_req());
    let (_, nonce) = read_self_test_probe(&h, tx_start);
    let output = format!("noise before\r\n__SW_ST_{nonce}_OK__\r\nnoise after\r\nroot@t:/# ");
    h.inject_rx(output.as_bytes());
    let reply = pending
        .recv_timeout(Duration::from_secs(2))
        .expect("完整 marker 後的 prompt 應完成")
        .expect("self-test 回應應為字典");
    assert_eq!(reply["probe_ok"], PyValue::Bool(true), "{reply:?}");
    assert_eq!(
        reply["probe_evidence"]["execution_marker"],
        PyValue::Bool(true)
    );
    assert_eq!(
        reply["probe_evidence"]["prompt_after_marker"],
        PyValue::Bool(true)
    );
}

#[test]
fn stale_probe_cannot_confirm_new_generation() {
    let mut boot = CoreHarness::new(SHELL_PROFILE);
    boot.to_ready();
    let (boot_reply, boot_effects) = boot.rpc_deferred(self_test_req());
    let (boot_op, boot_epoch) = self_test_op_id(&boot_effects);
    boot.rx(b"U-Boot 2026.01\r\n");
    assert_eq!(
        boot.public()["ready_reconfirm_pending"],
        PyValue::Bool(true),
        "boot banner 應建立新 readiness pending"
    );
    boot.feed(ControlEvent::OpFinished {
        op_id: boot_op,
        epoch: boot_epoch,
        outcome: verified_self_test_outcome(),
        after_rx_seq: None,
    });
    let boot_result = boot_reply.try_recv().unwrap().unwrap();
    assert_eq!(boot_result["probe_ok"], PyValue::Bool(false));
    assert_ne!(boot_result["classification"], PyValue::from("OK"));
    assert_eq!(
        boot.public()["ready_reconfirm_pending"],
        PyValue::Bool(true)
    );

    let mut recover = CoreHarness::new(SHELL_PROFILE);
    recover.to_ready();
    recover.attach_pty_console("c1", true);
    let (probe_reply, probe_effects) = recover.rpc_deferred(self_test_req());
    let (probe_op, epoch) = self_test_op_id(&probe_effects);
    assert_eq!(recover.core.human_console().ownership.suspend_depth, 1);
    let (recover_reply, recover_effects) = recover.rpc_deferred(SessionRequest::Recover {
        timeout_s: 30.0,
        force: false,
    });
    let (recover_op, recover_epoch) = recover_effects
        .iter()
        .find_map(|effect| match effect {
            Effect::StartOp {
                grant,
                spec: OpSpec::Recover { .. },
            } => Some((grant.op_id, grant.epoch)),
            _ => None,
        })
        .expect("Recover 必須仍可介入進行中的 self-test");
    assert_eq!(recover.core.human_console().ownership.suspend_depth, 1);
    recover.feed(ControlEvent::OpFinished {
        op_id: recover_op,
        epoch: recover_epoch,
        outcome: OpOutcome::Recovered {
            key: "ready",
            stdout: String::new(),
        },
        after_rx_seq: None,
    });
    assert!(recover_reply.try_recv().is_ok());
    assert_eq!(
        recover.core.human_console().ownership.suspend_depth,
        1,
        "Recover 自身完成不得代結算 probe suspend"
    );
    recover.feed(ControlEvent::OpFinished {
        op_id: probe_op,
        epoch,
        outcome: verified_self_test_outcome(),
        after_rx_seq: None,
    });
    let probe_result = probe_reply.try_recv().unwrap().unwrap();
    assert_eq!(probe_result["probe_ok"], PyValue::Bool(false));
    assert_ne!(probe_result["classification"], PyValue::from("OK"));
    assert_eq!(recover.core.human_console().ownership.suspend_depth, 0);

    let mut reconnect = CoreHarness::new(SHELL_PROFILE);
    reconnect.to_ready();
    let (old_reply, old_effects) = reconnect.rpc_deferred(self_test_req());
    let (old_op, old_epoch) = self_test_op_id(&old_effects);
    reconnect.feed(ControlEvent::DeviceDetached {
        by_id: DEVICE_BY_ID.into(),
        reason: DetachReason::Rebound,
    });
    let (drain_epoch, stop_id) = reconnect.pending_stop();
    reconnect.receipt(drain_epoch, stop_id).feed();
    reconnect.to_ready();
    let new_epoch = reconnect.epoch();
    assert_ne!(new_epoch, old_epoch, "重連必須使用新 connection epoch");
    reconnect.feed(ControlEvent::LoginFinished {
        epoch: new_epoch,
        fsm_gen: reconnect.core.generations().fsm_gen,
        result: sw_core::login_fsm::LoginOutcome::Ready,
        after_rx_seq: None,
    });
    reconnect.rx(b"U-Boot 2026.01\r\n");
    assert_eq!(
        reconnect.public()["ready_reconfirm_pending"],
        PyValue::Bool(true)
    );
    reconnect.feed(ControlEvent::OpFinished {
        op_id: old_op,
        epoch: old_epoch,
        outcome: verified_self_test_outcome(),
        after_rx_seq: None,
    });
    let old_result = old_reply.try_recv().unwrap().unwrap();
    assert_eq!(old_result["probe_ok"], PyValue::Bool(false));
    assert_ne!(old_result["classification"], PyValue::from("OK"));
    assert_eq!(
        reconnect.public()["ready_reconfirm_pending"],
        PyValue::Bool(true),
        "舊 connection 的晚到結果不得清除新 pending"
    );
}

#[test]
fn self_test_terminal_paths_release_only_own_reservation() {
    for outcome in [
        OpOutcome::SelfTest(pydict! {
            "probe_ok" => false,
            "probe_evidence" => pydict! {
                "tx_complete" => true,
                "execution_marker" => false,
                "probe_rc_zero" => false,
                "prompt_after_marker" => false,
            },
        }),
        OpOutcome::SelfTestRefused(sw_core::tx_writer::FenceReject::Quiet),
        OpOutcome::Error(sw_proto::pycompat::PyErr::value_error(
            "injected runner error",
        )),
    ] {
        let mut h = CoreHarness::new(SHELL_PROFILE);
        h.to_ready();
        let (reply, effects) = h.rpc_deferred(self_test_req());
        let (op_id, epoch) = self_test_op_id(&effects);
        assert_eq!(
            h.fence.with(|fence| fence.self_test_probe_owner),
            Some(op_id),
            "StartOp 前應已授權 probe owner"
        );
        h.feed(ControlEvent::OpFinished {
            op_id,
            epoch,
            outcome,
            after_rx_seq: None,
        });
        assert!(reply.try_recv().is_ok(), "每個終結路徑都應回覆一次");
        assert!(reply.try_recv().is_err(), "self-test 不得重覆回覆");
        assert_eq!(h.fence.with(|fence| fence.self_test_probe_owner), None);
        let (_next_reply, next_effects) = h.rpc_deferred(self_test_req());
        assert!(
            has_self_test_probe(&next_effects),
            "成功／拒絕／runner error 後都應可開始下一個操作"
        );
    }

    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    let (_reply, effects) = h.rpc_deferred(self_test_req());
    let (op_id, epoch) = self_test_op_id(&effects);
    let other_owner = OpId(u64::MAX);
    h.fence
        .with(|fence| fence.self_test_probe_owner = Some(other_owner));
    h.feed(ControlEvent::OpFinished {
        op_id,
        epoch,
        outcome: verified_self_test_outcome(),
        after_rx_seq: None,
    });
    assert_eq!(
        h.fence.with(|fence| fence.self_test_probe_owner),
        Some(other_owner),
        "晚到 op 只能釋放自己的 reservation"
    );
}
