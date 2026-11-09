#![cfg(feature = "test-fakes")]

mod support;

use std::time::Duration;

use crossbeam_channel::bounded;
use support::*;
use sw_core::drain::{StopId, WorkerStoppedReceipt};
use sw_core::login_fsm::{LoginOutcome, LoginPlan};
use sw_core::mailbox::{ControlEvent, DetachReason, OpenFailure, TimerEvent};
use sw_core::op_runner::OpOutcome;
use sw_core::session_core::{Effect, SessionRequest};
use sw_core::tx_writer::{EvidenceGap, OpId, TxLedger, TxOpLedger};
use sw_pal::traits::{HolderReport, HolderScanStatus, PalError};
use sw_proto::pyjson::{PyDict, PyValue};

fn release() -> SessionRequest {
    SessionRequest::Release {
        source: "cli".into(),
        reason: PyValue::Null,
    }
}

fn session_error(response: &PyDict, code: &str) {
    assert_eq!(response.get("ok"), Some(&PyValue::Bool(false)));
    assert_eq!(response.get("error_code"), Some(&PyValue::from(code)));
}

fn nested_session_state(response: &PyDict) -> PyValue {
    match response.get("session") {
        Some(PyValue::Dict(session)) => session["state"].clone(),
        other => panic!("回應缺少 session dict：{other:?}"),
    }
}

fn holder(pids: Vec<u32>, holder_unknown: bool) -> HolderReport {
    HolderReport {
        pids,
        holder_unknown,
        status: HolderScanStatus::Ok,
    }
}

#[test]
fn release_waits_for_receipt_then_persists_and_maps_bc03() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    let cmd = h.start_foreground_command("sleep 20");
    let (rx, _) = h.rpc_deferred(release());
    assert_eq!(h.public()["state"], PyValue::from("RECOVERING"));
    assert!(
        h.fence
            .with(|state| state.frozen && state.active_epoch.is_none())
    );
    assert!(rx.try_recv().is_err(), "收到關閉回條前不得回覆");
    let (epoch, stop) = h.pending_stop();
    h.receipt(epoch, stop)
        .confirmed(cmd.op_id, 4)
        .handle_closed(true)
        .feed();
    let response = rx.try_recv().unwrap().unwrap();
    assert_eq!(response.get("ok"), Some(&PyValue::Bool(true)));
    assert_eq!(response.get("aborted_cmd"), Some(&PyValue::Bool(true)));
    let record = h.reg.get(&cmd.cmd_id).unwrap();
    assert_eq!(record.get("status"), Some(&PyValue::from("error")));
    assert_eq!(
        record.get("error_code"),
        Some(&PyValue::from("COMMAND_ABORTED_IN_FLIGHT"))
    );
    let meta = h
        .wal_meta_events()
        .into_iter()
        .find(|row| row.get("event") == Some(&PyValue::from("command_aborted_in_flight")))
        .unwrap();
    assert_eq!(meta.get("confirmed_tx_bytes"), Some(&PyValue::from(4)));
    assert_eq!(meta.get("tx_outcome"), Some(&PyValue::from("partial")));
    assert!(h.state_store.last_released().contains_key("t:COM0"));
    assert_eq!(h.public()["state"], PyValue::from("RELEASED"));
}

#[test]
fn drain_timeout_marks_stalled_and_late_receipt_completes() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    let (rx, _) = h.rpc_deferred(release());
    let (epoch, stop) = h.pending_stop();
    h.advance(Duration::from_millis(3001));
    let response = rx.try_recv().unwrap().unwrap();
    assert_eq!(
        response.0.keys().map(String::as_str).collect::<Vec<_>>(),
        vec![
            "ok",
            "error_code",
            "drain_stalled",
            "drain_timeout_epoch",
            "session"
        ]
    );
    session_error(&response, "DRAIN_STALLED");
    assert_eq!(response.get("drain_stalled"), Some(&PyValue::Bool(true)));
    assert_eq!(response.get("drain_timeout_epoch"), Some(&PyValue::from(1)));
    assert_eq!(h.public()["state"], PyValue::from("RECOVERING"));
    let again = h.rpc(release()).unwrap();
    session_error(&again, "DRAIN_STALLED");
    let attach = h
        .rpc(SessionRequest::DeviceAttach { force: false })
        .unwrap();
    session_error(&attach, "SESSION_RECOVERING");
    h.receipt(epoch, stop).handle_closed(true).feed();
    assert_eq!(h.public()["state"], PyValue::from("RELEASED"));
    assert_eq!(h.public()["drain_stalled"], PyValue::Bool(false));
    assert_eq!(h.public()["drain_timeout_epoch"], PyValue::from(1));
    assert_eq!(
        h.rpc(release()).unwrap()["already_released"],
        PyValue::Bool(true)
    );
}

#[test]
fn release_success_response_shape() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    h.feed(ControlEvent::ConsoleCountChanged { count: 1 });
    let (rx, _) = h.rpc_deferred(release());
    let (epoch, stop) = h.pending_stop();
    h.receipt(epoch, stop).feed();
    let response = rx.try_recv().unwrap().unwrap();
    assert_eq!(
        response.0.keys().map(String::as_str).collect::<Vec<_>>(),
        vec!["ok", "session", "closed_consoles", "aborted_cmd"]
    );
    assert_eq!(response["closed_consoles"], PyValue::from(1));
    assert_eq!(response["aborted_cmd"], PyValue::Bool(false));
    assert_eq!(nested_session_state(&response), PyValue::from("RELEASED"));
}

#[test]
fn release_flushes_accepted_with_flushed_by_recovery() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    let running = h.start_foreground_command("sleep 20");
    let mut queued = Vec::new();
    for index in 0..3 {
        let response = h
            .rpc(SessionRequest::Submit(agent_submit(&format!(
                "echo {index}"
            ))))
            .unwrap();
        queued.push(response["cmd_id"].as_str().unwrap().to_string());
    }
    let (_rx, _) = h.rpc_deferred(release());
    for cmd_id in queued {
        let row = h.reg.get(&cmd_id).unwrap();
        assert_eq!(row["status"], PyValue::from("error"));
        assert_eq!(row["error_code"], PyValue::from("FLUSHED_BY_RECOVERY"));
    }
    let (epoch, stop) = h.pending_stop();
    h.receipt(epoch, stop).confirmed(running.op_id, 0).feed();
}

#[test]
fn in_flight_zero_bytes_is_flushed_by_recovery() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    let cmd = h.start_foreground_command("printf x");
    let (_rx, _) = h.rpc_deferred(release());
    let (epoch, stop) = h.pending_stop();
    h.receipt(epoch, stop).confirmed(cmd.op_id, 0).feed();
    let row = h.reg.get(&cmd.cmd_id).unwrap();
    assert_eq!(row["error_code"], PyValue::from("FLUSHED_BY_RECOVERY"));
    assert!(
        !h.wal_meta_events()
            .iter()
            .any(|m| { m.get("event") == Some(&PyValue::from("command_aborted_in_flight")) })
    );
}

#[test]
fn in_flight_unknown_is_send_failed() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    let cmd = h.start_foreground_command("printf x");
    let (_rx, _) = h.rpc_deferred(release());
    let (epoch, stop) = h.pending_stop();
    h.receipt(epoch, stop).unknown(cmd.op_id).feed();
    assert_eq!(
        h.reg.get(&cmd.cmd_id).unwrap()["error_code"],
        PyValue::from("SEND_FAILED")
    );
}

#[test]
fn full_write_aborted_meta_complete() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    let command = "printf done";
    let cmd = h.start_foreground_command(command);
    let (_rx, _) = h.rpc_deferred(release());
    let (epoch, stop) = h.pending_stop();
    h.receipt(epoch, stop)
        .confirmed(cmd.op_id, command.len() + 1)
        .feed();
    assert_eq!(
        h.reg.get(&cmd.cmd_id).unwrap()["error_code"],
        PyValue::from("COMMAND_ABORTED_IN_FLIGHT")
    );
    assert_eq!(
        h.wal_meta_events()[0]["tx_outcome"],
        PyValue::from("complete")
    );
}

#[test]
fn abort_mapping_independent_of_opfinished_order() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    let cmd = h.start_foreground_command("echo x");
    let (_rx, _) = h.rpc_deferred(release());
    let effects = h.feed(ControlEvent::OpFinished {
        op_id: cmd.op_id,
        epoch: h.epoch(),
        outcome: OpOutcome::SendFailed,
        after_rx_seq: None,
    });
    assert!(
        !effects
            .iter()
            .any(|effect| matches!(effect, Effect::SettleTx { .. }))
    );
    assert_eq!(
        h.reg.get(&cmd.cmd_id).unwrap()["status"],
        PyValue::from("running")
    );
    let (epoch, stop) = h.pending_stop();
    h.receipt(epoch, stop).confirmed(cmd.op_id, 2).feed();
    assert_eq!(
        h.reg.get(&cmd.cmd_id).unwrap()["error_code"],
        PyValue::from("COMMAND_ABORTED_IN_FLIGHT")
    );
}

#[test]
fn duplicate_release_joins_same_stop_op() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    let (first, effects) = h.rpc_deferred(release());
    let (epoch, stop) = h.pending_stop();
    let (second, second_effects) = h.rpc_deferred(release());
    assert!(
        !second_effects
            .iter()
            .any(|effect| matches!(effect, Effect::BeginDrain { .. }))
    );
    assert_eq!(h.pending_stop(), (epoch, stop));
    assert!(first.try_recv().is_err() && second.try_recv().is_err());
    assert_eq!(
        effects
            .iter()
            .filter(|e| matches!(e, Effect::BeginDrain { .. }))
            .count(),
        1
    );
    h.receipt(epoch, stop).feed();
    assert!(first.try_recv().unwrap().unwrap()["ok"].as_bool().unwrap());
    assert!(second.try_recv().unwrap().unwrap()["ok"].as_bool().unwrap());
}

#[test]
fn cross_kind_request_during_drain_is_session_recovering() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    let (_release_rx, _) = h.rpc_deferred(release());
    for request in [
        SessionRequest::Clear,
        SessionRequest::Bind {
            by_id: "/s/by-id/other".into(),
        },
        SessionRequest::Recover {
            timeout_s: 1.0,
            force: false,
        },
        SessionRequest::DeviceAttach { force: false },
    ] {
        let response = h.rpc(request).unwrap();
        session_error(&response, "SESSION_RECOVERING");
        assert!(response.0.contains_key("drain_stalled"));
        assert!(response.0.contains_key("drain_timeout_epoch"));
    }
}

#[test]
fn internal_events_during_drain_recorded_as_post_kind() {
    let mut release_h = CoreHarness::new(SHELL_PROFILE);
    release_h.to_ready();
    let (release_rx, _) = release_h.rpc_deferred(release());
    let (epoch, stop) = release_h.pending_stop();
    release_h.feed(ControlEvent::DeviceDetached {
        by_id: DEVICE_BY_ID.into(),
        reason: DetachReason::Removed,
    });
    release_h.receipt(epoch, stop).feed();
    assert_eq!(
        release_rx.try_recv().unwrap().unwrap()["session"]["state"],
        PyValue::from("RELEASED")
    );

    let mut recover_h = CoreHarness::new(SHELL_PROFILE);
    recover_h.to_ready();
    recover_h.feed(ControlEvent::UartIoError {
        epoch: recover_h.epoch(),
        reason: "SERIAL_READ:5".into(),
    });
    let (recover_epoch, recover_stop) = recover_h.pending_stop();
    recover_h.feed(ControlEvent::DeviceDetached {
        by_id: DEVICE_BY_ID.into(),
        reason: DetachReason::Removed,
    });
    recover_h.receipt(recover_epoch, recover_stop).feed();
    assert_eq!(recover_h.public()["state"], PyValue::from("DETACHED"));
    assert_eq!(
        recover_h.public()["last_error"],
        PyValue::from("DEVICE_REMOVED")
    );
}

#[test]
fn stale_receipt_after_reattach_keeps_evidence_only() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    let cmd = h.start_foreground_command("echo old");
    let (_rx, _) = h.rpc_deferred(release());
    let (old_epoch, old_stop) = h.pending_stop();
    h.receipt(old_epoch, old_stop)
        .confirmed(cmd.op_id, 2)
        .feed();
    let attach_rx = h
        .rpc_deferred(SessionRequest::DeviceAttach { force: false })
        .0;
    h.bridge_ok();
    let _ = attach_rx.try_recv().unwrap().unwrap();
    let new_epoch = h.core.generations().conn_epoch.unwrap();
    let old = WorkerStoppedReceipt {
        epoch: old_epoch,
        stop_id: old_stop,
        ledger: TxLedger {
            epoch: old_epoch,
            ops: [(
                cmd.op_id,
                TxOpLedger {
                    confirmed_bytes: 7,
                    ..TxOpLedger::default()
                },
            )]
            .into_iter()
            .collect(),
            ..TxLedger::default()
        },
        unrecorded: Vec::new(),
        wal_error: None,
        handle_closed: true,
        close_error: None,
    };
    h.feed(ControlEvent::WorkerStopped(old));
    assert_eq!(h.core.generations().conn_epoch, Some(new_epoch));
    assert_eq!(h.public()["state"], PyValue::from("ATTACHING"));
    assert_eq!(
        h.reg.get(&cmd.cmd_id).unwrap()["error_code"],
        PyValue::from("COMMAND_ABORTED_IN_FLIGHT")
    );
}

#[test]
fn settle_emitted_once_at_terminal_point() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    let cmd = h.start_foreground_command("echo done");
    let effects = h.feed(ControlEvent::OpFinished {
        op_id: cmd.op_id,
        epoch: h.epoch(),
        outcome: OpOutcome::CommandResult(sw_proto::pydict! { "ok" => true, "stdout" => "done" }),
        after_rx_seq: None,
    });
    assert_eq!(
        h.reg.get(&cmd.cmd_id).unwrap()["status"],
        PyValue::from("done")
    );
    assert_eq!(
        effects
            .iter()
            .filter(
                |effect| matches!(effect, Effect::SettleTx { op_id, .. } if *op_id == cmd.op_id)
            )
            .count(),
        1
    );
    let duplicate = h.feed(ControlEvent::OpFinished {
        op_id: cmd.op_id,
        epoch: h.epoch(),
        outcome: OpOutcome::SendFailed,
        after_rx_seq: None,
    });
    assert!(
        !duplicate
            .iter()
            .any(|effect| matches!(effect, Effect::SettleTx { .. }))
    );
}

#[test]
fn aborted_op_is_never_settled_during_drain() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    let cmd = h.start_foreground_command("echo pending");
    let (_rx, _) = h.rpc_deferred(release());
    let effects = h.feed(ControlEvent::OpFinished {
        op_id: cmd.op_id,
        epoch: h.epoch(),
        outcome: OpOutcome::CommandResult(sw_proto::pydict! { "ok" => true }),
        after_rx_seq: None,
    });
    assert!(
        !effects
            .iter()
            .any(|effect| matches!(effect, Effect::SettleTx { .. }))
    );
    let (epoch, stop) = h.pending_stop();
    h.receipt(epoch, stop).confirmed(cmd.op_id, 3).feed();
    assert!(
        h.wal_meta_events()
            .iter()
            .any(|meta| meta["event"] == PyValue::from("command_aborted_in_flight"))
    );
}

#[test]
fn missing_ledger_entry_maps_to_flushed() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    let cmd = h.start_foreground_command("echo unsent");
    let (_rx, _) = h.rpc_deferred(release());
    let (epoch, stop) = h.pending_stop();
    h.receipt(epoch, stop).feed();
    assert_eq!(
        h.reg.get(&cmd.cmd_id).unwrap()["error_code"],
        PyValue::from("FLUSHED_BY_RECOVERY")
    );
}

#[test]
fn tx_ledger_saturated_drains_as_io_error() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    let effects = h.feed(ControlEvent::UartIoError {
        epoch: h.epoch(),
        reason: "TX_LEDGER_SATURATED".into(),
    });
    assert_eq!(h.public()["state"], PyValue::from("RECOVERING"));
    assert!(effects.iter().any(|effect| matches!(effect, Effect::Warn(message) if message.contains("TX_LEDGER_SATURATED") || message.contains("上限"))));
    let (epoch, stop) = h.pending_stop();
    h.receipt(epoch, stop).feed();
    assert_eq!(h.public()["state"], PyValue::from("ATTACHING"));
}

#[test]
fn wal_gap_lists_unrecorded_and_still_completes() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    let (rx, _) = h.rpc_deferred(release());
    let (epoch, stop) = h.pending_stop();
    h.receipt(epoch, stop).gap(None, "RX", 9).feed();
    assert!(rx.try_recv().unwrap().unwrap()["ok"].as_bool().unwrap());
    assert!(h.public()["state"] == PyValue::from("RELEASED"));
}

#[test]
fn close_failure_keeps_recovering_and_no_success() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    let (rx, _) = h.rpc_deferred(release());
    let (epoch, stop) = h.pending_stop();
    h.receipt(epoch, stop).handle_closed(false).feed();
    session_error(&rx.try_recv().unwrap().unwrap(), "DRAIN_STALLED");
    assert_eq!(h.public()["state"], PyValue::from("RECOVERING"));
    assert_eq!(h.public()["drain_stalled"], PyValue::Bool(true));
    assert!(
        h.rpc(SessionRequest::DeviceAttach { force: false })
            .unwrap()["error_code"]
            == PyValue::from("SESSION_RECOVERING")
    );
    h.receipt(epoch, stop).handle_closed(true).feed();
    assert_eq!(h.public()["state"], PyValue::from("RELEASED"));
}

#[test]
fn release_commit_failure_is_exception_but_memory_released() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    h.state_store
        .fail_next_commit(PalError::io(5, "state write failed"));
    let (rx, _) = h.rpc_deferred(release());
    let (epoch, stop) = h.pending_stop();
    h.receipt(epoch, stop).feed();
    assert!(rx.try_recv().unwrap().is_err());
    assert_eq!(h.public()["state"], PyValue::from("RELEASED"));
    assert!(h.core.release_persist_pending_for_test());
    assert!(
        h.feed(ControlEvent::DeviceAttached {
            by_id: DEVICE_BY_ID.into(),
            real_path: REAL_PATH.into()
        })
        .is_empty()
    );
    let response = h.rpc(release()).unwrap();
    assert_eq!(response["already_released"], PyValue::Bool(true));
    assert!(!h.core.release_persist_pending_for_test());
    assert!(h.state_store.last_released().contains_key("t:COM0"));
}

#[test]
fn attach_precheck_holder_still_held() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    let (rx, effects) = h.rpc_deferred(release());
    let (epoch, stop) = h.pending_stop();
    h.receipt(epoch, stop).feed();
    let commits = h.state_store.commits();
    let (attach_rx, open_effects) = h.rpc_deferred(SessionRequest::DeviceAttach { force: false });
    assert!(open_effects.iter().any(|effect| matches!(
        effect,
        Effect::OpenBridge {
            precheck_holders: true,
            ..
        }
    )));
    h.feed(ControlEvent::BridgeOpened {
        epoch_hint: 0,
        result: Err(OpenFailure::Held(holder(vec![4242], false))),
    });
    let response = attach_rx.try_recv().unwrap().unwrap();
    session_error(&response, "DEVICE_STILL_HELD");
    assert_eq!(response["pids"], PyValue::List(vec![PyValue::from(4242)]));
    assert_eq!(response["holder_unknown"], PyValue::Bool(false));
    assert_eq!(h.public()["state"], PyValue::from("RELEASED"));
    assert_eq!(h.state_store.commits(), commits);
    assert!(rx.try_recv().unwrap().unwrap()["ok"].as_bool().unwrap());
    assert!(
        effects
            .iter()
            .any(|effect| matches!(effect, Effect::BeginDrain { .. }))
    );
}

#[test]
fn attach_open_held_reports_holder_unknown() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    let (_rx, _) = h.rpc_deferred(release());
    let (epoch, stop) = h.pending_stop();
    h.receipt(epoch, stop).feed();
    let (attach_rx, _) = h.rpc_deferred(SessionRequest::DeviceAttach { force: false });
    h.feed(ControlEvent::BridgeOpened {
        epoch_hint: 0,
        result: Err(OpenFailure::Held(holder(vec![], true))),
    });
    let response = attach_rx.try_recv().unwrap().unwrap();
    session_error(&response, "DEVICE_STILL_HELD");
    assert_eq!(response["holder_unknown"], PyValue::Bool(true));
    assert_eq!(response["pids"], PyValue::List(vec![]));
}

#[test]
fn attach_access_denied_reports_holder_unknown() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    let (_rx, _) = h.rpc_deferred(release());
    let (epoch, stop) = h.pending_stop();
    h.receipt(epoch, stop).feed();
    let (attach_rx, _) = h.rpc_deferred(SessionRequest::DeviceAttach { force: true });
    h.feed(ControlEvent::BridgeOpened {
        epoch_hint: 0,
        result: Err(OpenFailure::AccessDenied(holder(vec![], true))),
    });
    let response = attach_rx.try_recv().unwrap().unwrap();
    session_error(&response, "DEVICE_STILL_HELD");
    assert_eq!(response["holder_unknown"], PyValue::Bool(true));
}

#[test]
fn attach_not_found_device_not_present() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    let (_rx, _) = h.rpc_deferred(release());
    let (epoch, stop) = h.pending_stop();
    h.receipt(epoch, stop).feed();
    let (attach_rx, _) = h.rpc_deferred(SessionRequest::DeviceAttach { force: false });
    h.feed(ControlEvent::BridgeOpened {
        epoch_hint: 0,
        result: Err(OpenFailure::NotFound),
    });
    let response = attach_rx.try_recv().unwrap().unwrap();
    session_error(&response, "DEVICE_NOT_PRESENT");
    assert_eq!(response["selector"], PyValue::from("COM0"));
    assert_eq!(response["device_by_id"], PyValue::from(DEVICE_BY_ID));
    assert_eq!(h.public()["state"], PyValue::from("RELEASED"));
}

#[test]
fn attach_other_open_error_detached_attach_failed() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    let (_rx, _) = h.rpc_deferred(release());
    let (epoch, stop) = h.pending_stop();
    h.receipt(epoch, stop).feed();
    let (attach_rx, _) = h.rpc_deferred(SessionRequest::DeviceAttach { force: false });
    h.feed(ControlEvent::BridgeOpened {
        epoch_hint: 0,
        result: Err(OpenFailure::Other(PalError::io(5, "open failed"))),
    });
    let response = attach_rx.try_recv().unwrap().unwrap();
    assert_eq!(response["ok"], PyValue::Bool(true));
    assert_eq!(h.public()["state"], PyValue::from("DETACHED"));
    assert_eq!(
        h.public()["last_error"],
        PyValue::from("ATTACH_FAILED:OSError")
    );
    assert!(!h.state_store.last_released().contains_key("t:COM0"));
}

#[test]
fn hotplug_held_is_detached_attach_failed() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.feed(ControlEvent::DeviceAttached {
        by_id: DEVICE_BY_ID.into(),
        real_path: REAL_PATH.into(),
    });
    h.feed(ControlEvent::BridgeOpened {
        epoch_hint: 0,
        result: Err(OpenFailure::Held(holder(vec![4242], false))),
    });
    assert_eq!(h.public()["state"], PyValue::from("DETACHED"));
    assert_eq!(
        h.public()["last_error"],
        PyValue::from("ATTACH_FAILED:DeviceHeldError")
    );
}

#[test]
fn attach_success_commits_then_attaching() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    let (_release_rx, _) = h.rpc_deferred(release());
    let (epoch, stop) = h.pending_stop();
    h.receipt(epoch, stop).feed();
    let (attach_rx, open_effects) = h.rpc_deferred(SessionRequest::DeviceAttach { force: false });
    assert!(
        open_effects
            .iter()
            .any(|effect| matches!(effect, Effect::OpenBridge { .. }))
    );
    let effects = h.bridge_ok();
    assert!(
        effects
            .iter()
            .any(|effect| matches!(effect, Effect::StartLogin { .. }))
    );
    assert_eq!(
        attach_rx.try_recv().unwrap().unwrap()["session"]["state"],
        PyValue::from("ATTACHING")
    );
    assert!(!h.state_store.last_released().contains_key("t:COM0"));
    assert_eq!(h.public()["state"], PyValue::from("ATTACHING"));
}

#[test]
fn attach_during_drain_is_session_recovering() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    let (_release_rx, _) = h.rpc_deferred(release());
    let response = h
        .rpc(SessionRequest::DeviceAttach { force: false })
        .unwrap();
    session_error(&response, "SESSION_RECOVERING");
    assert!(h.core.drain_in_progress());
}

#[test]
fn attach_force_does_not_bypass_identified_external_holder() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    let (_rx, _) = h.rpc_deferred(release());
    let (epoch, stop) = h.pending_stop();
    h.receipt(epoch, stop).feed();
    let commits = h.state_store.commits();
    let (attach_rx, effects) = h.rpc_deferred(SessionRequest::DeviceAttach { force: true });
    assert!(effects.iter().any(|effect| matches!(
        effect,
        Effect::OpenBridge {
            precheck_holders: false,
            ..
        }
    )));
    h.feed(ControlEvent::BridgeOpened {
        epoch_hint: 0,
        result: Err(OpenFailure::Held(holder(vec![4242], false))),
    });
    let response = attach_rx.try_recv().unwrap().unwrap();
    assert_eq!(
        response.0.keys().map(String::as_str).collect::<Vec<_>>(),
        vec![
            "ok",
            "error_code",
            "session",
            "pids",
            "holder_unknown",
            "selector"
        ]
    );
    assert_eq!(response["ok"], PyValue::Bool(false));
    assert_eq!(response["error_code"], PyValue::from("DEVICE_STILL_HELD"));
    assert_eq!(response["pids"], PyValue::List(vec![PyValue::from(4242)]));
    assert_eq!(response["holder_unknown"], PyValue::Bool(false));
    assert_eq!(response["selector"], PyValue::from("COM0"));
    assert_eq!(h.public()["state"], PyValue::from("RELEASED"));
    assert_eq!(h.state_store.commits(), commits);
    assert!(!effects.iter().any(|effect| matches!(
        effect,
        Effect::StartLogin { .. } | Effect::StartOp { .. } | Effect::Persist { .. }
    )));
}

#[test]
fn auto_attach_plain_external_holder_no_io() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.feed(ControlEvent::DeviceAttached {
        by_id: DEVICE_BY_ID.into(),
        real_path: REAL_PATH.into(),
    });
    h.feed(ControlEvent::BridgeOpened {
        epoch_hint: 0,
        result: Err(OpenFailure::Held(holder(vec![4242], false))),
    });
    assert_eq!(h.public()["state"], PyValue::from("DETACHED"));
    assert_eq!(
        h.public()["last_error"],
        PyValue::from("ATTACH_FAILED:DeviceHeldError")
    );
    assert_eq!(h.core.generations().conn_epoch, None);
}

#[test]
fn already_attached() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    let response = h
        .rpc(SessionRequest::DeviceAttach { force: false })
        .unwrap();
    assert_eq!(response["already_attached"], PyValue::Bool(true));
}

#[test]
fn missing_device_tick_keeps_released() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    let (_rx, _) = h.rpc_deferred(release());
    let (epoch, stop) = h.pending_stop();
    h.receipt(epoch, stop).feed();
    h.feed(ControlEvent::DeviceDetached {
        by_id: DEVICE_BY_ID.into(),
        reason: DetachReason::Removed,
    });
    assert_eq!(h.public()["state"], PyValue::from("RELEASED"));
}

#[test]
fn io_error_drain_then_reattach_when_present() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    h.feed(ControlEvent::UartIoError {
        epoch: h.epoch(),
        reason: "SERIAL_READ:5".into(),
    });
    assert_eq!(h.public()["state"], PyValue::from("RECOVERING"));
    let (epoch, stop) = h.pending_stop();
    h.receipt(epoch, stop).feed();
    assert_eq!(h.public()["state"], PyValue::from("ATTACHING"));
    assert!(h.core.generations().conn_epoch.is_none());
}

#[test]
fn rebound_receipt_sets_device_rebound_required_then_reopens() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    h.feed(ControlEvent::DeviceDetached {
        by_id: DEVICE_BY_ID.into(),
        reason: DetachReason::Rebound,
    });
    let (epoch, stop) = h.pending_stop();
    h.feed(ControlEvent::DeviceAttached {
        by_id: DEVICE_BY_ID.into(),
        real_path: "/fake/tty-B".into(),
    });
    let effects = h.receipt(epoch, stop).feed();
    assert_eq!(
        h.public()["last_error"],
        PyValue::from("DEVICE_REBOUND_REQUIRED")
    );
    assert!(
        effects.iter().any(
            |effect| matches!(effect, Effect::OpenBridge { dev, .. } if dev.0 == "/fake/tty-B")
        )
    );
}

#[test]
fn login_failure_detaches_after_receipt() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.feed(ControlEvent::DeviceAttached {
        by_id: DEVICE_BY_ID.into(),
        real_path: REAL_PATH.into(),
    });
    h.bridge_ok();
    let epoch = h.epoch();
    let fsm_gen = h.core.generations().fsm_gen;
    h.feed(ControlEvent::LoginFinished {
        epoch,
        fsm_gen,
        result: LoginOutcome::Failed("LOGIN_PROMPT_TIMEOUT".into()),
        after_rx_seq: None,
    });
    let (stop_epoch, stop) = h.pending_stop();
    h.receipt(stop_epoch, stop).feed();
    assert_eq!(h.public()["state"], PyValue::from("DETACHED"));
    assert_eq!(
        h.public()["last_error"],
        PyValue::from("LOGIN_PROMPT_TIMEOUT")
    );
    assert_eq!(h.public()["bridge_generation"], PyValue::from(0));
}

#[test]
fn shutdown_flushes_with_flushed_by_shutdown() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    let running = h.start_foreground_command("sleep 20");
    let queued = h
        .rpc(SessionRequest::Submit(agent_submit("echo queued")))
        .unwrap()["cmd_id"]
        .as_str()
        .unwrap()
        .to_string();
    let (reply, rx) = crossbeam_channel::bounded(1);
    h.feed(ControlEvent::Shutdown { reply });
    assert_eq!(
        h.reg.get(&queued).unwrap()["error_code"],
        PyValue::from("FLUSHED_BY_SHUTDOWN")
    );
    let (epoch, stop) = h.pending_stop();
    h.receipt(epoch, stop).confirmed(running.op_id, 0).feed();
    assert!(rx.try_recv().unwrap().unwrap()["ok"].as_bool().unwrap());
    assert!(h.core.shutdown_complete());
}

#[test]
fn stale_reply_context_rejected_after_release() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    let cmd = h.start_foreground_command("echo x");
    let grant = h.fence.with(|state| (state.owner_gen, state.fsm_gen));
    let (_rx, _) = h.rpc_deferred(release());
    let (epoch, stop) = h.pending_stop();
    h.receipt(epoch, stop).confirmed(cmd.op_id, 1).feed();
    assert_eq!(h.fence.with(|state| state.active_epoch), None);
    let permit = sw_core::tx_writer::TxPermit {
        op_id: cmd.op_id,
        epoch,
        owner_gen: grant.0,
        fsm_gen: grant.1,
        class: sw_core::tx_writer::TxClass::Command,
        source: "agent".into(),
        cmd_id: Some(cmd.cmd_id),
        log: true,
        input_epoch: None,
        replay_hold: None,
        probe_id: None,
    };
    assert_eq!(
        h.fence
            .with(|state| sw_core::tx_writer::Fence::admit(state, &permit)),
        Err(sw_core::tx_writer::FenceReject::Frozen)
    );
    let response = h
        .rpc(SessionRequest::InteractiveClose {
            interactive_id: "old-lease".into(),
        })
        .unwrap();
    assert_eq!(
        response["error_code"],
        PyValue::from("INTERACTIVE_NOT_FOUND")
    );
}

#[test]
fn release_during_pending_open_bridge_closes_late_handle() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.feed(ControlEvent::DeviceAttached {
        by_id: DEVICE_BY_ID.into(),
        real_path: REAL_PATH.into(),
    });
    let (rx, effects) = h.rpc_deferred(release());
    assert!(
        !effects
            .iter()
            .any(|effect| matches!(effect, Effect::OpenBridge { .. }))
    );
    assert_eq!(h.public()["state"], PyValue::from("RECOVERING"));
    let (_, expected_stop_id) = h.pending_stop();
    assert!(h.core.drain_in_progress());
    let opened = h.opened_bridge(REAL_PATH);
    let late_epoch = opened.epoch;
    let drain = h.feed(ControlEvent::BridgeOpened {
        epoch_hint: 0,
        result: Ok(opened),
    });
    assert_eq!(h.public()["state"], PyValue::from("RECOVERING"));
    assert!(drain.iter().any(|effect| matches!(effect, Effect::BeginDrain { stop_id, epoch } if *stop_id == expected_stop_id && *epoch == late_epoch)));
    h.receipt(late_epoch, expected_stop_id).feed();
    assert!(rx.try_recv().unwrap().unwrap()["ok"].as_bool().unwrap());
    assert_eq!(h.public()["state"], PyValue::from("RELEASED"));
}

#[test]
fn release_during_pending_open_drain_stalled() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.feed(ControlEvent::DeviceAttached {
        by_id: DEVICE_BY_ID.into(),
        real_path: REAL_PATH.into(),
    });
    let (rx, _) = h.rpc_deferred(release());
    h.advance(Duration::from_millis(3001));
    let response = rx.try_recv().unwrap().unwrap();
    session_error(&response, "DRAIN_STALLED");
    assert_eq!(response["drain_stalled"], PyValue::Bool(true));
    assert_eq!(response["drain_timeout_epoch"], PyValue::from(0));
    assert_eq!(h.public()["state"], PyValue::from("RECOVERING"));
}

#[test]
fn release_retry_persists_after_commit_failure() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    h.state_store
        .fail_next_commit(PalError::io(5, "first commit fails"));
    let (rx, _) = h.rpc_deferred(release());
    let (epoch, stop) = h.pending_stop();
    h.receipt(epoch, stop).feed();
    assert!(rx.try_recv().unwrap().is_err());
    let response = h.rpc(release()).unwrap();
    assert_eq!(response["already_released"], PyValue::Bool(true));
    assert!(h.state_store.last_released().contains_key("t:COM0"));
}

#[test]
fn late_receipt_persist_failure_sets_pending_and_retries() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    let (rx, _) = h.rpc_deferred(release());
    let (epoch, stop) = h.pending_stop();
    h.advance(Duration::from_millis(3001));
    let timeout = rx.try_recv().unwrap().unwrap();
    session_error(&timeout, "DRAIN_STALLED");

    h.state_store
        .fail_next_commit(PalError::io(5, "late receipt commit fails"));
    h.receipt(epoch, stop).feed();
    assert!(rx.try_recv().is_err(), "逾時後的 waiter 不應重複收到回覆");
    assert_eq!(h.public()["state"], PyValue::from("RELEASED"));
    assert!(h.core.release_persist_pending_for_test());
    assert!(h.state_store.last_released().is_empty());

    let retry = h.rpc(release()).unwrap();
    assert_eq!(retry["already_released"], PyValue::Bool(true));
    assert!(!h.core.release_persist_pending_for_test());
    assert!(h.state_store.last_released().contains_key("t:COM0"));
    assert_eq!(h.state_store.commits(), 1);
}

#[test]
fn single_release_persister_waiters_take_over() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    h.state_store
        .fail_next_commit(PalError::io(5, "first commit fails"));
    h.defer_state_commits();

    let (owner_rx, _) = h.rpc_deferred(release());
    let (epoch, stop) = h.pending_stop();
    h.receipt(epoch, stop).feed();
    let first_commit = h.pending_state_commit_ids();
    assert_eq!(first_commit.len(), 1);

    let (waiter_rx, _) = h.rpc_deferred(release());
    assert!(owner_rx.try_recv().is_err());
    assert!(waiter_rx.try_recv().is_err());
    h.complete_state_commit(first_commit[0]);
    assert!(owner_rx.try_recv().unwrap().is_err());
    assert!(
        waiter_rx.try_recv().is_err(),
        "等待者應接手重試而非一併收到錯誤"
    );

    let retry_commit = h.pending_state_commit_ids();
    assert_eq!(retry_commit.len(), 1, "失敗後應為 waiter 發出第二次 Persist");
    h.complete_state_commit(retry_commit[0]);
    let response = waiter_rx.try_recv().unwrap().unwrap();
    assert_eq!(response["ok"], PyValue::Bool(true));
    assert_eq!(response["already_released"], PyValue::Bool(true));
    assert!(h.state_store.last_released().contains_key("t:COM0"));
}

#[test]
fn device_attach_dedup_same_by_id_inflight() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    let (_rx, _) = h.rpc_deferred(release());
    let (epoch, stop) = h.pending_stop();
    h.receipt(epoch, stop).feed();
    let (_first, first_effects) = h.rpc_deferred(SessionRequest::DeviceAttach { force: false });
    let opens = h.fake_device.open_count();
    let second = h
        .rpc(SessionRequest::DeviceAttach { force: false })
        .unwrap();
    session_error(&second, "SESSION_RECOVERING");
    assert!(
        first_effects
            .iter()
            .any(|effect| matches!(effect, Effect::OpenBridge { .. }))
    );
    assert_eq!(h.fake_device.open_count(), opens);
}

// ---- WP5-8 第 2 項（G13，S1X-a-3）：`device.attach` 遇一般 attach 在途回 ok＋session ----
//
// Python 基線（`origin/main:sw_core/session_manager.py`）：
// - `:3384-3398` `_attach_inflight_draining_locked`：在途表命中時，只有 pending drain、by_id 正在 drain、在途 op
//   帶 release 請求或已取消、或 session 已 RELEASED 才算 drain／release 進行中。
// - `:3428-3443`、`:3457-3467`：其餘（probe／login 進行中的一般 attach）回 `{"ok":true,"session"}`；drain／release
//   進行中仍回 `SESSION_RECOVERING`＋`drain_stalled`＋`drain_timeout_epoch`＋`session`（SC-04）。
// - 一般 attach 的 probe／login 期間 `session.bridge` 仍為 None（bridge 只在 `_attach_bridges`／`attach_bridge`，
//   `:4996-5000`），所以不走 `:3421` 的 `already_attached` 分支。
// 對應 Python 測試 `tests/test_s1x_a3_attach_inflight.py:52-144`；回應形狀 `contracts/rpc_schema.json` device.attach。

fn g13_wire(response: &PyDict) -> String {
    sw_proto::pyjson::dumps_dict(response, &sw_proto::pyjson::RPC_WIRE)
}

/// Python `{"ok": True, "session": session.to_public_dict()}`（`session_manager.py:3437`）。
fn g13_ok_session(h: &CoreHarness) -> String {
    let mut expected = PyDict::new();
    expected.insert("ok", PyValue::Bool(true));
    expected.insert("session", PyValue::Dict(h.public()));
    g13_wire(&expected)
}

/// Python drain／release 在途回應（`session_manager.py:3438-3444`），鍵序 ok／error_code／drain_stalled／
/// drain_timeout_epoch／session。
fn g13_recovering(h: &CoreHarness) -> String {
    let public = h.public();
    let mut expected = PyDict::new();
    expected.insert("ok", PyValue::Bool(false));
    expected.insert("error_code", PyValue::from("SESSION_RECOVERING"));
    expected.insert("drain_stalled", public["drain_stalled"].clone());
    expected.insert("drain_timeout_epoch", public["drain_timeout_epoch"].clone());
    expected.insert("session", PyValue::Dict(public));
    g13_wire(&expected)
}

/// 第二次 attach 不得再開埠、再登入、再落盤或再交 manager 解析。
fn g13_attach_side_effects(effects: &[Effect]) -> usize {
    effects
        .iter()
        .filter(|effect| {
            matches!(
                effect,
                Effect::OpenBridge { .. }
                    | Effect::StartLogin { .. }
                    | Effect::Persist { .. }
                    | Effect::Reattach { .. }
            )
        })
        .count()
}

/// 對應 Python `_detached` fixture：DETACHED、裝置在場、沒有 bridge（自動開埠失敗後停 DETACHED）。
fn g13_detached_with_device() -> CoreHarness {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.attach_device();
    h.feed(ControlEvent::BridgeOpened {
        epoch_hint: 0,
        result: Err(OpenFailure::Other(PalError::io(5, "open failed"))),
    });
    assert_eq!(h.public()["state"], PyValue::from("DETACHED"));
    assert_eq!(h.core.generations().conn_epoch, None);
    h
}

/// 停在一般 attach 的登入途中（bridge 已開、`StartLogin` 已發、`LoginFinished` 未到）。
fn g13_login_in_progress() -> CoreHarness {
    let mut h = g13_detached_with_device();
    let (first_rx, first_effects) = h.rpc_deferred(SessionRequest::DeviceAttach { force: false });
    assert!(first_effects
        .iter()
        .any(|effect| matches!(effect, Effect::OpenBridge { .. })));
    let effects = h.bridge_ok();
    assert!(
        effects
            .iter()
            .any(|effect| matches!(effect, Effect::StartLogin { .. })),
        "第一次 attach 應進入登入"
    );
    let first = first_rx
        .try_recv()
        .expect("bridge 開啟且 unreleased 落盤後回覆第一次 attach")
        .unwrap();
    assert_eq!(first.get("ok"), Some(&PyValue::Bool(true)));
    assert_eq!(nested_session_state(&first), PyValue::from("ATTACHING"));
    assert_eq!(h.public()["state"], PyValue::from("ATTACHING"));
    assert!(h.core.generations().conn_epoch.is_some());
    h
}

#[test]
fn wp58_g13_second_attach_during_login_returns_ok_session() {
    // Python `test_second_attach_during_login_returns_ok_session`：第一次 attach 回 ATTACHING 後、probe／login
    // 進行中再呼叫一次 → ok＋session（state=ATTACHING），非 SESSION_RECOVERING，也沒有 `already_attached`。
    let mut h = g13_login_in_progress();
    let opens = h.fake_device.open_count();
    let epoch = h.core.generations().conn_epoch;

    let (second, effects) = h.rpc_full(SessionRequest::DeviceAttach { force: false });
    let second = second.unwrap();
    assert_eq!(second.get("error_code"), None, "{}", g13_wire(&second));
    assert_eq!(second.get("already_attached"), None, "{}", g13_wire(&second));
    assert_eq!(g13_wire(&second), g13_ok_session(&h));
    match second.get("session") {
        Some(PyValue::Dict(session)) => {
            assert_eq!(session.get("state"), Some(&PyValue::from("ATTACHING")));
            assert_eq!(session.get("com"), Some(&PyValue::from("COM0")));
        }
        other => panic!("回應缺少 session dict：{other:?}"),
    }
    assert_eq!(g13_attach_side_effects(&effects), 0);
    assert_eq!(h.fake_device.open_count(), opens);
    assert_eq!(h.core.generations().conn_epoch, epoch);

    // 第一次 attach 的登入照常完成。
    let (epoch, fsm_gen) = (h.epoch(), h.core.generations().fsm_gen);
    h.feed(ControlEvent::LoginFinished {
        epoch,
        fsm_gen,
        result: LoginOutcome::Ready,
        after_rx_seq: None,
    });
    assert_eq!(h.public()["state"], PyValue::from("READY"));
}

/// Python `test_inflight_attach_tables_without_drain_return_ok` 的四表參數化，對應 Rust 的五個在途階段。
mod wp58_g13_plain_attach_inflight_returns_ok {
    use super::*;

    /// 對 `force` 兩值各呼叫一次（Python 在途判斷位於 holder 檢查之前，`force` 不影響）：回應逐位元組等於
    /// `{"ok":true,"session"}`，且不新增 `OpenBridge`／`StartLogin`／`Persist`／`Reattach`、不改 state 與 epoch。
    fn assert_plain_inflight_ok(h: &mut CoreHarness, phase: &str) {
        for force in [false, true] {
            let opens = h.fake_device.open_count();
            let epoch = h.core.generations().conn_epoch;
            let state = h.public()["state"].clone();
            let (response, effects) = h.rpc_full(SessionRequest::DeviceAttach { force });
            let response = response.unwrap();
            assert_eq!(
                g13_wire(&response),
                g13_ok_session(h),
                "{phase}（force={force}）應回 ok＋session"
            );
            assert_eq!(
                g13_attach_side_effects(&effects),
                0,
                "{phase}（force={force}）不得重複開埠或登入"
            );
            assert_eq!(h.fake_device.open_count(), opens, "{phase}（force={force}）");
            assert_eq!(h.core.generations().conn_epoch, epoch, "{phase}（force={force}）");
            assert_eq!(h.public()["state"], state, "{phase}（force={force}）");
        }
    }

    #[test]
    fn reattach_pending() {
        // 待開埠：clear 收尾後交 manager 再解析（`Effect::Reattach`），ATTACHING、尚未送 `OpenBridge`。
        let mut h = CoreHarness::new(SHELL_PROFILE);
        h.to_ready();
        let (_clear_rx, _) = h.rpc_deferred(SessionRequest::Clear);
        let (epoch, stop) = h.pending_stop();
        let effects = h.receipt(epoch, stop).feed();
        assert!(effects
            .iter()
            .any(|effect| matches!(effect, Effect::Reattach { .. })));
        assert_eq!(h.public()["state"], PyValue::from("ATTACHING"));
        assert_eq!(h.core.generations().conn_epoch, None);
        assert_plain_inflight_ok(&mut h, "待開埠");

        // 原流程照常：manager 再解析後只開一次埠。
        let effects = h.feed(ControlEvent::DeviceAttached {
            by_id: DEVICE_BY_ID.into(),
            real_path: REAL_PATH.into(),
        });
        let opens = effects
            .iter()
            .filter(|effect| matches!(effect, Effect::OpenBridge { .. }))
            .count();
        assert_eq!(opens, 1);
    }

    #[test]
    fn hotplug_opening() {
        // 開埠中（自動 attach）：`DeviceAttached` 已送 `OpenBridge`、`BridgeOpened` 未到。
        let mut h = CoreHarness::new(SHELL_PROFILE);
        let effects = h.feed(ControlEvent::DeviceAttached {
            by_id: DEVICE_BY_ID.into(),
            real_path: REAL_PATH.into(),
        });
        assert!(effects
            .iter()
            .any(|effect| matches!(effect, Effect::OpenBridge { .. })));
        assert_eq!(h.public()["state"], PyValue::from("ATTACHING"));
        assert_plain_inflight_ok(&mut h, "自動 attach 開埠中");

        let effects = h.bridge_ok();
        assert!(effects
            .iter()
            .any(|effect| matches!(effect, Effect::StartLogin { .. })));
    }

    #[test]
    fn manual_opening() {
        // 開埠中（`device.attach` 發起）：對應 Python `_attach_ops`；Python 此時 state 仍為 DETACHED
        // （`:3599` 開埠成功後才設 ATTACHING），Rust 同樣維持原 state。
        let mut h = g13_detached_with_device();
        let (first_rx, first_effects) = h.rpc_deferred(SessionRequest::DeviceAttach { force: false });
        assert!(first_effects
            .iter()
            .any(|effect| matches!(effect, Effect::OpenBridge { .. })));
        assert!(first_rx.try_recv().is_err(), "開埠完成前不回覆第一次 attach");
        assert_plain_inflight_ok(&mut h, "device.attach 開埠中");

        h.bridge_ok();
        let first = first_rx.try_recv().unwrap().unwrap();
        assert_eq!(first.get("ok"), Some(&PyValue::Bool(true)));
        assert_eq!(nested_session_state(&first), PyValue::from("ATTACHING"));
    }

    #[test]
    fn opened_commit_pending() {
        // 已開埠、等 unreleased 落盤：對應 Python `_preopened_ports`（開埠成功後、`_save_state` 與 attach thread 接手前）。
        let mut h = g13_detached_with_device();
        h.defer_state_commits();
        let (first_rx, _) = h.rpc_deferred(SessionRequest::DeviceAttach { force: false });
        h.bridge_ok();
        let commits = h.pending_state_commit_ids();
        assert_eq!(commits.len(), 1, "開埠後應先落盤 unreleased");
        assert!(first_rx.try_recv().is_err());
        assert_plain_inflight_ok(&mut h, "已開埠等落盤");
        assert_eq!(h.pending_state_commit_ids(), commits, "第二次 attach 不得再落盤");

        let effects = h.complete_state_commit(commits[0]);
        assert!(effects
            .iter()
            .any(|effect| matches!(effect, Effect::StartLogin { .. })));
        let first = first_rx.try_recv().unwrap().unwrap();
        assert_eq!(first.get("ok"), Some(&PyValue::Bool(true)));
    }

    #[test]
    fn login_in_progress() {
        // bridge 已開但登入未完成：對應 Python `_attach_bridges`（`session.bridge` 仍為 None）。
        let mut h = g13_login_in_progress();
        assert_plain_inflight_ok(&mut h, "登入中");

        let (epoch, fsm_gen) = (h.epoch(), h.core.generations().fsm_gen);
        h.feed(ControlEvent::LoginFinished {
            epoch,
            fsm_gen,
            result: LoginOutcome::Ready,
            after_rx_seq: None,
        });
        assert_eq!(h.public()["state"], PyValue::from("READY"));
    }
}

/// Python `test_inflight_attach_with_release_request_keeps_recovering`：release 進行中（或 session 仍 RELEASED）時，
/// attach 在途仍回 SESSION_RECOVERING（SC-04）。
mod wp58_g13_release_inflight_keeps_recovering {
    use super::*;

    fn assert_recovering(h: &mut CoreHarness, phase: &str) {
        let opens = h.fake_device.open_count();
        let (response, effects) = h.rpc_full(SessionRequest::DeviceAttach { force: false });
        let response = response.unwrap();
        assert_eq!(g13_wire(&response), g13_recovering(h), "{phase}");
        assert_eq!(g13_attach_side_effects(&effects), 0, "{phase}");
        assert_eq!(h.fake_device.open_count(), opens, "{phase}");
    }

    #[test]
    fn release_during_manual_open() {
        // 對應 Python 在途 op 帶 `release_request`：device.attach 開埠途中收到 release。
        let mut h = g13_detached_with_device();
        let (attach_rx, _) = h.rpc_deferred(SessionRequest::DeviceAttach { force: false });
        let (_release_rx, _) = h.rpc_deferred(release());
        session_error(&attach_rx.try_recv().unwrap().unwrap(), "SESSION_RECOVERING");
        assert!(h.core.drain_in_progress());
        assert_recovering(&mut h, "device.attach 開埠途中 release");
    }

    #[test]
    fn release_during_login() {
        let mut h = g13_login_in_progress();
        let (_release_rx, _) = h.rpc_deferred(release());
        assert!(h.core.drain_in_progress());
        assert_eq!(h.public()["state"], PyValue::from("RECOVERING"));
        assert_recovering(&mut h, "登入途中 release");
    }

    #[test]
    fn released_attach_opening() {
        // 對應 Python `session.state == "RELEASED"`：自 RELEASED 發起的 attach 仍在開埠。
        let mut h = CoreHarness::new(SHELL_PROFILE);
        h.to_ready();
        let (_rx, _) = h.rpc_deferred(release());
        let (epoch, stop) = h.pending_stop();
        h.receipt(epoch, stop).feed();
        let (_first, _) = h.rpc_deferred(SessionRequest::DeviceAttach { force: false });
        assert_eq!(h.public()["state"], PyValue::from("RELEASED"));
        assert_recovering(&mut h, "RELEASED 開埠中");
    }

    #[test]
    fn released_attach_commit_pending() {
        // 自 RELEASED 開埠成功、unreleased 尚未落盤：Rust state 仍為 RELEASED（落盤失敗會退回 RELEASED），
        // 依 Python `_attach_inflight_draining_locked` 的 RELEASED 判斷仍屬 release 進行中。
        let mut h = CoreHarness::new(SHELL_PROFILE);
        h.to_ready();
        let (_rx, _) = h.rpc_deferred(release());
        let (epoch, stop) = h.pending_stop();
        h.receipt(epoch, stop).feed();
        h.defer_state_commits();
        let (_first, _) = h.rpc_deferred(SessionRequest::DeviceAttach { force: false });
        h.bridge_ok();
        assert_eq!(h.pending_state_commit_ids().len(), 1);
        assert_eq!(h.public()["state"], PyValue::from("RELEASED"));
        assert_recovering(&mut h, "RELEASED 開埠後等落盤");
    }
}

/// Python `test_inflight_attach_while_draining_keeps_recovering`：by_id 正在 drain 時仍回 SESSION_RECOVERING，
/// 並完整帶出 `drain_stalled`、`drain_timeout_epoch`、`session`。
mod wp58_g13_inflight_attach_while_draining_keeps_recovering {
    use super::*;

    #[test]
    fn clear_drain_during_open() {
        // 開埠途中 clear：pending open stop（drain 在途、bridge 尚未接上）。
        let mut h = CoreHarness::new(SHELL_PROFILE);
        h.attach_device();
        let (_clear_rx, _) = h.rpc_deferred(SessionRequest::Clear);
        assert!(h.core.drain_in_progress());
        let opens = h.fake_device.open_count();
        let (response, effects) = h.rpc_full(SessionRequest::DeviceAttach { force: false });
        let response = response.unwrap();
        assert_eq!(g13_wire(&response), g13_recovering(&h));
        assert_eq!(response.get("drain_stalled"), Some(&PyValue::Bool(false)));
        assert_eq!(g13_attach_side_effects(&effects), 0);
        assert_eq!(h.fake_device.open_count(), opens);
    }

    #[test]
    fn stalled_clear_drain_during_login() {
        // 登入途中 clear 且 drain 逾時：`drain_stalled=true`、`drain_timeout_epoch` 前進，兩欄與 session 原樣帶出。
        let mut h = g13_login_in_progress();
        let (_clear_rx, _) = h.rpc_deferred(SessionRequest::Clear);
        assert!(h.core.drain_in_progress());
        h.advance(Duration::from_millis(3001));
        assert_eq!(h.public()["drain_stalled"], PyValue::Bool(true));
        assert_ne!(h.public()["drain_timeout_epoch"], PyValue::Null);
        let opens = h.fake_device.open_count();
        let (response, effects) = h.rpc_full(SessionRequest::DeviceAttach { force: false });
        let response = response.unwrap();
        assert_eq!(g13_wire(&response), g13_recovering(&h));
        assert_eq!(response.get("drain_stalled"), Some(&PyValue::Bool(true)));
        assert_eq!(
            response.get("drain_timeout_epoch"),
            Some(&h.public()["drain_timeout_epoch"])
        );
        assert_eq!(g13_attach_side_effects(&effects), 0);
        assert_eq!(h.fake_device.open_count(), opens);
    }
}

/// 審查 P8-140c92e-1：Python `attach_device` 的檢查順序為 drain → `already_attached` → `by_id not in self._devices`
/// → 在途表（`origin/main:sw_core/session_manager.py:3410-3443`）。`update_devices`（`:4022-4030`）先把
/// `_devices` 換成新快照，所以在途 attach 期間裝置已拔除時，第二次 `device.attach` 一律回 `DEVICE_NOT_PRESENT`
/// （先於在途表的 ok／SESSION_RECOVERING 分流，含 RELEASED），且不得重複開埠。
mod wp58_g13_inflight_attach_after_removal_returns_not_present {
    use super::*;

    /// Python `{"ok": False, "error_code": "DEVICE_NOT_PRESENT", "selector": selector, "device_by_id": by_id}`（`:3424`）。
    fn g13_not_present() -> String {
        let mut expected = PyDict::new();
        expected.insert("ok", PyValue::Bool(false));
        expected.insert("error_code", PyValue::from("DEVICE_NOT_PRESENT"));
        expected.insert("selector", PyValue::from("COM0"));
        expected.insert("device_by_id", PyValue::from(DEVICE_BY_ID));
        g13_wire(&expected)
    }

    /// manager 的拔除順序（`session_manager.rs` 先送 `DeviceMissing` 再送 `DeviceDetached`，對應 Python
    /// `_mark_missing_devices_locked`，審查 F60）。
    fn remove_device(h: &mut CoreHarness) {
        h.feed(ControlEvent::DeviceMissing);
        h.feed(ControlEvent::DeviceDetached {
            by_id: DEVICE_BY_ID.into(),
            reason: DetachReason::Removed,
        });
    }

    /// 對 `force` 兩值各呼叫一次（Python 的裝置在場檢查位於 holder 檢查之前，`force` 不影響）：回應逐位元組等於
    /// `DEVICE_NOT_PRESENT`，且不新增 `OpenBridge`／`StartLogin`／`Persist`／`Reattach`、不改 state。
    fn assert_not_present(h: &mut CoreHarness, phase: &str) {
        for force in [false, true] {
            let opens = h.fake_device.open_count();
            let state = h.public()["state"].clone();
            let (response, effects) = h.rpc_full(SessionRequest::DeviceAttach { force });
            let response = response.unwrap();
            assert_eq!(
                g13_wire(&response),
                g13_not_present(),
                "{phase}（force={force}）應回 DEVICE_NOT_PRESENT"
            );
            assert_eq!(
                g13_attach_side_effects(&effects),
                0,
                "{phase}（force={force}）不得重複開埠或登入"
            );
            assert_eq!(h.fake_device.open_count(), opens, "{phase}（force={force}）");
            assert_eq!(h.public()["state"], state, "{phase}（force={force}）");
        }
    }

    #[test]
    fn manual_opening_device_detached_only() {
        // 審查原情境：DETACHED、device.attach 開埠中，只收到 `DeviceDetached{Removed}`（state 為 DETACHED，
        // 不走 begin_stop，`pending_attach` 留著）。
        let mut h = g13_detached_with_device();
        let (first_rx, first_effects) = h.rpc_deferred(SessionRequest::DeviceAttach { force: false });
        assert!(first_effects
            .iter()
            .any(|effect| matches!(effect, Effect::OpenBridge { .. })));
        h.feed(ControlEvent::DeviceDetached {
            by_id: DEVICE_BY_ID.into(),
            reason: DetachReason::Removed,
        });
        assert!(!h.core.drain_in_progress());
        assert!(first_rx.try_recv().is_err(), "開埠完成前不回覆第一次 attach");
        assert_eq!(h.public()["state"], PyValue::from("DETACHED"));
        assert_not_present(&mut h, "device.attach 開埠中拔除（僅 DeviceDetached）");
    }

    #[test]
    fn manual_opening() {
        let mut h = g13_detached_with_device();
        let (_first_rx, _) = h.rpc_deferred(SessionRequest::DeviceAttach { force: false });
        remove_device(&mut h);
        assert!(!h.core.drain_in_progress());
        assert_eq!(h.public()["state"], PyValue::from("DETACHED"));
        assert_not_present(&mut h, "device.attach 開埠中拔除");
    }

    #[test]
    fn hotplug_opening() {
        // 自動 attach 開埠中拔除：`DeviceMissing` 把 ATTACHING 收回 DETACHED（無 bridge、無 stop），開埠仍在途。
        let mut h = CoreHarness::new(SHELL_PROFILE);
        let effects = h.feed(ControlEvent::DeviceAttached {
            by_id: DEVICE_BY_ID.into(),
            real_path: REAL_PATH.into(),
        });
        assert!(effects
            .iter()
            .any(|effect| matches!(effect, Effect::OpenBridge { .. })));
        remove_device(&mut h);
        assert!(!h.core.drain_in_progress());
        assert_eq!(h.public()["state"], PyValue::from("DETACHED"));
        assert_not_present(&mut h, "自動 attach 開埠中拔除");
    }

    #[test]
    fn opened_commit_pending() {
        // 已開埠等 unreleased 落盤時拔除：對應 Python `_preopened_ports`。
        let mut h = g13_detached_with_device();
        h.defer_state_commits();
        let (_first_rx, _) = h.rpc_deferred(SessionRequest::DeviceAttach { force: false });
        h.bridge_ok();
        let commits = h.pending_state_commit_ids();
        assert_eq!(commits.len(), 1, "開埠後應先落盤 unreleased");
        remove_device(&mut h);
        assert!(!h.core.drain_in_progress());
        assert_not_present(&mut h, "已開埠等落盤時拔除");
        assert_eq!(h.pending_state_commit_ids(), commits, "第二次 attach 不得再落盤");
    }

    #[test]
    fn released_attach_opening() {
        // 自 RELEASED 發起的 attach 開埠中拔除：Python 的裝置在場檢查先於 `_attach_inflight_draining_locked`
        // 的 RELEASED 判斷，回 DEVICE_NOT_PRESENT 而非 SESSION_RECOVERING。
        let mut h = CoreHarness::new(SHELL_PROFILE);
        h.to_ready();
        let (_rx, _) = h.rpc_deferred(release());
        let (epoch, stop) = h.pending_stop();
        h.receipt(epoch, stop).feed();
        let (_first, _) = h.rpc_deferred(SessionRequest::DeviceAttach { force: false });
        remove_device(&mut h);
        assert!(!h.core.drain_in_progress());
        assert_eq!(h.public()["state"], PyValue::from("RELEASED"));
        assert_not_present(&mut h, "RELEASED 開埠中拔除");
    }
}

// ---- WP5-8 第 3 項（G14，S1X-a-7）：clear 收尾在裝置在場時立即回報 `last_error=null` ----
//
// Python 基線（`origin/main:sw_core/session_manager.py`）：
// - 有 bridge（drain）：`_begin_drain_locked` `:2778-2779` 於 drain 進行中設 RECOVERING＋`last_error="CLEARED"`；
//   `_complete_drain` `:2939-2953` 先設 DETACHED＋`CLEARED`，`job.kind == "clear"` 且 by_id 在 `_devices` 時改為
//   `state="ATTACHING"`、`last_error=None`，再 `_spawn_attach`；`clear_session` `:3065-3082` 於 `_await_drain` 後
//   組回應，所以回應與即時狀態都是 ATTACHING＋null，不必等新的 `DeviceAttached`（裁決 D6，取代審查 F97）。
// - 無 bridge：`clear_session` `:3059-3064`，裝置在場 ATTACHING＋None，不在場 DETACHED＋`CLEARED`。
// 對應 Python `tests/test_s1x_a7_drain_fields.py:64-93`；該檔要求的 detached_at 更新與 reprobe 歸零見下方 G15 段。

/// clear 回應＝Python `{"ok": True, "session": session.to_public_dict()}`（同一時點逐位元組相同），並核對即時狀態。
fn g14_assert_clear(reply: &PyDict, h: &CoreHarness, state: &str, last_error: PyValue) {
    assert_eq!(g13_wire(reply), g13_ok_session(h), "clear 回應須為 ok＋同一時點公開欄位");
    let public = h.public();
    assert_eq!(public["state"], PyValue::from(state), "{}", g13_wire(&public));
    assert_eq!(public["last_error"], last_error, "{}", g13_wire(&public));
}

fn g14_has_reattach(effects: &[Effect]) -> bool {
    effects
        .iter()
        .any(|effect| matches!(effect, Effect::Reattach { .. }))
}

fn g14_has_open_bridge(effects: &[Effect]) -> bool {
    effects
        .iter()
        .any(|effect| matches!(effect, Effect::OpenBridge { .. }))
}

#[test]
fn wp58_g14_clear_present_returns_null_before_reattach() {
    // Python `test_clear_with_device_present_clears_last_error`：READY（有 bridge）→ clear → drain 收尾後回應與
    // 即時狀態為 ATTACHING＋null。此處完成 drain receipt，但不投遞 manager 再解析後的 `DeviceAttached`。
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    let (rx, _) = h.rpc_deferred(SessionRequest::Clear);
    assert!(rx.try_recv().is_err(), "drain 回條前不得回覆 clear");
    let during = h.public();
    assert_eq!(during["state"], PyValue::from("RECOVERING"));
    assert_eq!(
        during["last_error"],
        PyValue::from("CLEARED"),
        "drain 進行中維持 CLEARED（`_begin_drain_locked`）"
    );
    let (epoch, stop) = h.pending_stop();
    let effects = h.receipt(epoch, stop).feed();
    assert!(g14_has_reattach(&effects), "裝置在場需交 manager 再解析後 attach");
    assert!(!g14_has_open_bridge(&effects), "再解析前不得直接開埠");
    let reply = rx.try_recv().expect("drain 收尾後回覆 clear").expect("ok");
    g14_assert_clear(&reply, &h, "ATTACHING", PyValue::Null);

    // 再解析後的 DeviceAttached 仍被接受並開埠，last_error 維持 null。
    let effects = h.feed(ControlEvent::DeviceAttached {
        by_id: DEVICE_BY_ID.into(),
        real_path: REAL_PATH.into(),
    });
    assert!(g14_has_open_bridge(&effects), "ATTACHING（等待再 attach）須接受 DeviceAttached 並開埠");
    assert_eq!(h.public()["last_error"], PyValue::Null);
}

#[test]
fn wp58_g14_clear_absent_keeps_cleared() {
    // Python `test_clear_without_device_keeps_cleared`：READY（有 bridge）時裝置已不在 `_devices`（tick 只記錄、
    // 不動有 bridge 的 session）→ clear → drain 收尾後 DETACHED＋CLEARED，不 `_spawn_attach`。
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    h.feed(ControlEvent::DeviceMissing);
    assert_eq!(h.public()["state"], PyValue::from("READY"), "有 bridge 時 tick 不動 session");
    let (rx, _) = h.rpc_deferred(SessionRequest::Clear);
    assert!(rx.try_recv().is_err(), "drain 回條前不得回覆 clear");
    let (epoch, stop) = h.pending_stop();
    let effects = h.receipt(epoch, stop).feed();
    assert!(!g14_has_reattach(&effects) && !g14_has_open_bridge(&effects), "裝置不在不得再 attach");
    let reply = rx.try_recv().expect("drain 收尾後回覆 clear").expect("ok");
    g14_assert_clear(&reply, &h, "DETACHED", PyValue::from("CLEARED"));
}

#[test]
fn wp58_g14_clear_present_without_worker_returns_null() {
    // 無 worker 對照（Python `clear_session` `:3059-3064`）：DETACHED、裝置在場、沒有 bridge → 立即回覆
    // ATTACHING＋null，交 manager 再解析、不直接開埠。
    let mut h = g13_detached_with_device();
    assert_ne!(h.public()["last_error"], PyValue::Null, "前置：開埠失敗留有錯誤");
    let (rx, effects) = h.rpc_deferred(SessionRequest::Clear);
    assert!(g14_has_reattach(&effects), "裝置在場需交 manager 再解析後 attach");
    assert!(!g14_has_open_bridge(&effects), "再解析前不得直接開埠");
    let reply = rx.try_recv().expect("無 bridge 立即回覆").expect("ok");
    g14_assert_clear(&reply, &h, "ATTACHING", PyValue::Null);
}

#[test]
fn wp58_g14_clear_absent_without_worker_keeps_cleared() {
    // 無 worker 對照：DETACHED、沒有 bridge、裝置已不在 → 立即回覆 DETACHED＋CLEARED，不再 attach。
    let mut h = g13_detached_with_device();
    h.feed(ControlEvent::DeviceMissing);
    let (rx, effects) = h.rpc_deferred(SessionRequest::Clear);
    assert!(!g14_has_reattach(&effects) && !g14_has_open_bridge(&effects), "裝置不在不得再 attach");
    let reply = rx.try_recv().expect("無 bridge 立即回覆").expect("ok");
    g14_assert_clear(&reply, &h, "DETACHED", PyValue::from("CLEARED"));
}

// ---- WP5-8 第 4 項（G15，S1X-a-7）：release／clear 收尾更新 `detached_at` 並重置 reprobe 進度 ----
//
// Python 基線（`origin/main:sw_core/session_manager.py`）：
// - 有 bridge（drain）：`_complete_drain` release 分支 `:2920-2927` 設 RELEASED＋`released_*`，並比照 0.3.2
//   `detached_at = now_iso()`＋`_reset_reprobe_progress_locked`；clear 分支 `:2936-2953` 設 DETACHED＋
//   `detached_at = now_iso()`＋reset，裝置在場再改 ATTACHING＋`last_error=None`。
// - 無 bridge：release `:3176-3183` 與 clear `:3059-3064` 經 `_detach_session_locked` `:2702-2707`
//   （`detached_at`＋reset）。
// - reset＝`SessionRuntime.reset_reprobe_progress` `:651-654`：attempts=0、`next_reprobe_at=None`、exhausted=False。
// - 已 RELEASED 的冪等 release（`:3139-3141`；開埠在途 `_finish_attach_op` `:4343-4348` 只在非 RELEASED 才
//   detach）不更新 `detached_at`、`released_at`、`released_by`、`released_reason`。
// 對應 Python `tests/test_s1x_a7_drain_fields.py:49-93`；fixture 以真實 tick／登入結果事件累積 reprobe 進度，
// 不以預設為零的新 session 代替。

/// 第一個 `StartLogin` 的 `(epoch, fsm_gen)`。
fn g15_start_login(effects: &[Effect]) -> Option<(u64, u64)> {
    effects.iter().find_map(|effect| match effect {
        Effect::StartLogin { epoch, fsm_gen, .. } => Some((*epoch, *fsm_gen)),
        _ => None,
    })
}

/// 逐秒推進 FakeClock 並收集 Tick 效果。
fn g15_advance(h: &mut CoreHarness, secs: u64) -> Vec<Effect> {
    let mut all = Vec::new();
    for _ in 0..secs {
        all.extend(h.advance(Duration::from_secs(1)));
    }
    all
}

/// 對應 Python `_ready` fixture 的舊欄位：`detached_at` 為舊時間戳、`reprobe_attempts=2`、`reprobe_exhausted=True`。
/// 1. 自動開埠失敗 → DETACHED（`set_state` 寫入舊 `detached_at`）。
/// 2. 手動 attach → 登入未就緒 → ATTACHED＋`PROMPT_UNAVAILABLE`（不經 DETACHED，`detached_at` 維持舊值）。
/// 3. 第一次自動重探失敗 → attempts=1；第二次 RX 尾行為 bootloader → attempts=2＋exhausted＋`BOOTLOADER_STUCK`。
///
/// 回傳舊 `detached_at`；bridge 仍在（release／clear 走 drain）。
fn g15_attached_with_reprobe_history() -> (CoreHarness, PyValue) {
    let mut h = g13_detached_with_device();
    let old_detached_at = h.public()["detached_at"].clone();
    assert!(
        matches!(old_detached_at, PyValue::Str(_)),
        "前置：開埠失敗後 detached_at 已寫入：{old_detached_at:?}"
    );
    let (attach_rx, _) = h.rpc_deferred(SessionRequest::DeviceAttach { force: false });
    let effects = h.bridge_ok();
    let (epoch, fsm_gen) = g15_start_login(&effects).expect("手動 attach 進入登入");
    assert_eq!(attach_rx.try_recv().unwrap().unwrap()["ok"], PyValue::Bool(true));
    h.feed(ControlEvent::LoginFinished {
        epoch,
        fsm_gen,
        result: LoginOutcome::NotReady("PROMPT_UNAVAILABLE".into()),
        after_rx_seq: None,
    });
    assert_eq!(h.public()["state"], PyValue::from("ATTACHED"));

    let (epoch, fsm_gen) =
        g15_start_login(&g15_advance(&mut h, 4)).expect("ATTACHED＋PROMPT_UNAVAILABLE 自動重探");
    h.feed(ControlEvent::LoginFinished {
        epoch,
        fsm_gen,
        result: LoginOutcome::NotReady("PROMPT_UNAVAILABLE".into()),
        after_rx_seq: None,
    });
    assert_eq!(h.public()["reprobe_attempts"], PyValue::from(1));

    let (epoch, fsm_gen) = g15_start_login(&g15_advance(&mut h, 3)).expect("退避後第二次自動重探");
    let now = sw_pal::traits::Clock::mono_ns(&*h.clock);
    h.rx.append(b"\r\n=> ", now, epoch, 9_999);
    h.rx(b"\r\n=> ");
    h.feed(ControlEvent::LoginFinished {
        epoch,
        fsm_gen,
        result: LoginOutcome::NotReady("SHELL_PROMPT_TIMEOUT".into()),
        after_rx_seq: None,
    });
    let public = h.public();
    assert_eq!(public["state"], PyValue::from("ATTACHED"));
    assert_eq!(public["last_error"], PyValue::from("BOOTLOADER_STUCK"));
    assert_eq!(public["reprobe_attempts"], PyValue::from(2), "前置：attempts 非零");
    assert_eq!(public["reprobe_exhausted"], PyValue::Bool(true), "前置：exhausted");
    assert_eq!(public["detached_at"], old_detached_at, "前置：ATTACHED 期間 detached_at 未變");
    assert!(h.core.generations().conn_epoch.is_some(), "前置：bridge 仍在");
    (h, old_detached_at)
}

/// 無 bridge 版：DETACHED、裝置在場、attempts=1 且 `next_reprobe_at` 非 null。
/// 1. 開埠成功 → 登入 `SHELL_PROMPT_TIMEOUT` 終態 → drain → DETACHED（`detached_at` 寫入）。
/// 2. tick：DETACHED＋逾時類錯誤 → `_prepare_reprobe_locked` attach 分支記一次 attempt（設 `next_reprobe_at`）並開埠。
/// 3. 開埠失敗 → DETACHED（`detached_at` 再寫一次，即本 fixture 的舊值），沒有 bridge。
fn g15_detached_with_reprobe_history() -> (CoreHarness, PyValue) {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.attach_device();
    h.bridge_ok();
    let (epoch, fsm_gen) = (h.epoch(), h.core.generations().fsm_gen);
    h.feed(ControlEvent::LoginFinished {
        epoch,
        fsm_gen,
        result: LoginOutcome::Failed("SHELL_PROMPT_TIMEOUT".into()),
        after_rx_seq: None,
    });
    let (stop_epoch, stop) = h.pending_stop();
    h.receipt(stop_epoch, stop).feed();
    assert_eq!(h.public()["state"], PyValue::from("DETACHED"));
    let effects = g15_advance(&mut h, 4);
    assert!(
        effects
            .iter()
            .any(|effect| matches!(effect, Effect::OpenBridge { .. })),
        "DETACHED＋逾時類錯誤 → 自動 attach"
    );
    h.feed(ControlEvent::BridgeOpened {
        epoch_hint: 0,
        result: Err(OpenFailure::Other(PalError::io(5, "open failed"))),
    });
    let public = h.public();
    assert_eq!(public["state"], PyValue::from("DETACHED"));
    assert_eq!(public["reprobe_attempts"], PyValue::from(1), "前置：attempts 非零");
    assert!(
        matches!(public["next_reprobe_at"], PyValue::Float(_)),
        "前置：next_reprobe_at 非 null：{:?}",
        public["next_reprobe_at"]
    );
    assert!(matches!(public["detached_at"], PyValue::Str(_)));
    assert_eq!(h.core.generations().conn_epoch, None, "前置：沒有 bridge");
    assert!(!h.core.drain_in_progress(), "前置：沒有開埠在途");
    (h, public["detached_at"].clone())
}

/// 推進 FakeClock 與 wall，讓收尾寫入的時間戳可與舊值區分。
fn g15_move_time(h: &mut CoreHarness) {
    g15_advance(h, 1);
    h.jump_wall_us(5_000_000);
}

fn g15_now_iso(h: &CoreHarness) -> PyValue {
    PyValue::from(format!("wall-us-{}", h.wall_us()))
}

/// reprobe 三欄已歸零（Python `reset_reprobe_progress`）。
fn g15_assert_reprobe_reset(public: &PyDict, case: &str) {
    assert_eq!(public["reprobe_attempts"], PyValue::from(0), "{case}：{}", g13_wire(public));
    assert_eq!(public["reprobe_exhausted"], PyValue::Bool(false), "{case}：{}", g13_wire(public));
    assert_eq!(public["next_reprobe_at"], PyValue::Null, "{case}：{}", g13_wire(public));
}

fn g15_assert_released(reply: &PyDict, h: &CoreHarness, old_detached_at: &PyValue, case: &str) {
    assert_eq!(reply["ok"], PyValue::Bool(true), "{case}：{}", g13_wire(reply));
    let public = h.public();
    assert_eq!(reply["session"], PyValue::Dict(public.clone()), "{case}：回應 session＝即時公開欄位");
    assert_eq!(public["state"], PyValue::from("RELEASED"), "{case}");
    assert_eq!(public["last_error"], PyValue::from("RELEASED"), "{case}");
    assert_ne!(&public["detached_at"], old_detached_at, "{case}：detached_at 須更新");
    assert_eq!(public["detached_at"], g15_now_iso(h), "{case}：detached_at＝收尾時刻");
    assert_eq!(public["released_at"], g15_now_iso(h), "{case}：released_at＝收尾時刻");
    g15_assert_reprobe_reset(&public, case);
    assert!(
        h.state_store.last_released().contains_key("t:COM0"),
        "{case}：release 已落盤"
    );
}

#[test]
fn wp58_g15_release_refreshes_detached_at_and_resets_reprobe() {
    // Python `test_release_refreshes_detached_at_and_resets_reprobe`：有 bridge 的 release 走 drain，
    // receipt 與落盤完成後 RELEASED、detached_at 更新、reprobe 歸零、last_error=RELEASED。
    let (mut h, old_detached_at) = g15_attached_with_reprobe_history();
    g15_move_time(&mut h);
    let (rx, _) = h.rpc_deferred(release());
    assert!(rx.try_recv().is_err(), "drain 回條前不得回覆 release");
    let (epoch, stop) = h.pending_stop();
    h.receipt(epoch, stop).handle_closed(true).feed();
    let reply = rx.try_recv().expect("drain 收尾後回覆 release").expect("ok");
    g15_assert_released(&reply, &h, &old_detached_at, "drain release");
}

#[test]
fn wp58_g15_release_without_worker_refreshes_detached_at_and_resets_reprobe() {
    // 無 bridge 對照（Python `release_device` `:3176-3183` → `_detach_session_locked`）。
    let (mut h, old_detached_at) = g15_detached_with_reprobe_history();
    g15_move_time(&mut h);
    let (rx, _) = h.rpc_deferred(release());
    let reply = rx.try_recv().expect("無 bridge 立即回覆 release").expect("ok");
    g15_assert_released(&reply, &h, &old_detached_at, "無 worker release");
}

#[test]
fn wp58_g15_clear_present_refreshes_detached_at_and_resets_reprobe() {
    // Python `test_clear_with_device_present_clears_last_error`：state=ATTACHING、last_error=None、
    // detached_at 更新、reprobe_attempts=0（另核對 exhausted／next_reprobe_at 也歸零）。
    let (mut h, old_detached_at) = g15_attached_with_reprobe_history();
    g15_move_time(&mut h);
    let (rx, _) = h.rpc_deferred(SessionRequest::Clear);
    assert!(rx.try_recv().is_err(), "drain 回條前不得回覆 clear");
    let (epoch, stop) = h.pending_stop();
    let effects = h.receipt(epoch, stop).feed();
    assert!(g14_has_reattach(&effects), "裝置在場需交 manager 再解析後 attach");
    let reply = rx.try_recv().expect("drain 收尾後回覆 clear").expect("ok");
    g14_assert_clear(&reply, &h, "ATTACHING", PyValue::Null);
    let public = h.public();
    assert_ne!(public["detached_at"], old_detached_at, "detached_at 須更新");
    assert_eq!(public["detached_at"], g15_now_iso(&h), "detached_at＝收尾時刻");
    g15_assert_reprobe_reset(&public, "drain clear（裝置在場）");
}

#[test]
fn wp58_g15_clear_absent_keeps_cleared_and_resets_reprobe() {
    // Python `test_clear_without_device_keeps_cleared`：state=DETACHED、last_error=CLEARED；`_complete_drain`
    // `:2936-2949` 同樣更新 detached_at 並 reset（不論裝置是否在場）。
    let (mut h, old_detached_at) = g15_attached_with_reprobe_history();
    h.feed(ControlEvent::DeviceMissing);
    g15_move_time(&mut h);
    let (rx, _) = h.rpc_deferred(SessionRequest::Clear);
    let (epoch, stop) = h.pending_stop();
    let effects = h.receipt(epoch, stop).feed();
    assert!(!g14_has_reattach(&effects) && !g14_has_open_bridge(&effects), "裝置不在不得再 attach");
    let reply = rx.try_recv().expect("drain 收尾後回覆 clear").expect("ok");
    g14_assert_clear(&reply, &h, "DETACHED", PyValue::from("CLEARED"));
    let public = h.public();
    assert_ne!(public["detached_at"], old_detached_at, "detached_at 須更新");
    assert_eq!(public["detached_at"], g15_now_iso(&h), "detached_at＝收尾時刻");
    g15_assert_reprobe_reset(&public, "drain clear（裝置不在）");
}

#[test]
fn wp58_g15_clear_without_worker_refreshes_detached_at_and_resets_reprobe() {
    // 無 bridge 對照（Python `clear_session` `:3059-3064`：`_detach_session_locked` `:2702-2707` 無條件寫
    // `detached_at`＋reset 後再 reset）：裝置在場 ATTACHING＋None、不在場 DETACHED＋CLEARED，兩路 detached_at
    // 皆更新（即使原本已是 DETACHED）、reprobe 皆歸零。
    let (mut h, old_detached_at) = g15_detached_with_reprobe_history();
    g15_move_time(&mut h);
    let (rx, effects) = h.rpc_deferred(SessionRequest::Clear);
    assert!(g14_has_reattach(&effects), "裝置在場需交 manager 再解析後 attach");
    let reply = rx.try_recv().expect("無 bridge 立即回覆").expect("ok");
    g14_assert_clear(&reply, &h, "ATTACHING", PyValue::Null);
    let public = h.public();
    assert_ne!(public["detached_at"], old_detached_at, "裝置在場：detached_at 須更新");
    assert_eq!(public["detached_at"], g15_now_iso(&h), "裝置在場：detached_at＝收尾時刻");
    g15_assert_reprobe_reset(&public, "無 worker clear（裝置在場）");

    let (mut h, old_detached_at) = g15_detached_with_reprobe_history();
    h.feed(ControlEvent::DeviceMissing);
    g15_move_time(&mut h);
    let (rx, effects) = h.rpc_deferred(SessionRequest::Clear);
    assert!(!g14_has_reattach(&effects) && !g14_has_open_bridge(&effects), "裝置不在不得再 attach");
    let reply = rx.try_recv().expect("無 bridge 立即回覆").expect("ok");
    g14_assert_clear(&reply, &h, "DETACHED", PyValue::from("CLEARED"));
    let public = h.public();
    assert_ne!(public["detached_at"], old_detached_at, "裝置不在：detached_at 須更新");
    assert_eq!(public["detached_at"], g15_now_iso(&h), "裝置不在：detached_at＝收尾時刻");
    g15_assert_reprobe_reset(&public, "無 worker clear（裝置不在）");
}

/// release 的 provenance 欄位（重複 release 不得改寫）。
fn g15_provenance(h: &CoreHarness) -> Vec<(&'static str, PyValue)> {
    let public = h.public();
    ["released_by", "released_at", "released_reason", "detached_at", "last_error", "state"]
        .into_iter()
        .map(|key| (key, public[key].clone()))
        .collect()
}

#[test]
fn wp58_g15_repeat_release_keeps_provenance() {
    // 已 RELEASED 的冪等 release（Python `release_device` `:3139-3141` → `already_released`）不 detach：
    // 第二次 release 不刷新 detached_at／released_*。
    let (mut h, _) = g15_attached_with_reprobe_history();
    let (rx, _) = h.rpc_deferred(release());
    let (epoch, stop) = h.pending_stop();
    h.receipt(epoch, stop).feed();
    assert_eq!(rx.try_recv().unwrap().unwrap()["ok"], PyValue::Bool(true));
    let first = g15_provenance(&h);
    g15_move_time(&mut h);
    let again = h.rpc(release()).unwrap();
    assert_eq!(again["already_released"], PyValue::Bool(true));
    assert_eq!(g15_provenance(&h), first, "冪等 release 不得刷新 provenance");
}

#[test]
fn wp58_g15_repeat_release_during_released_attach_keeps_provenance() {
    // 自 RELEASED 發起 attach、開埠在途時再 release（Python `_finish_attach_op` `:4343-4348` 只在非 RELEASED
    // 才 detach、`released_at = released_at or now_iso()`）：開埠完成後 drain 收尾，provenance 與 detached_at 不變。
    let (mut h, _) = g15_attached_with_reprobe_history();
    let (rx, _) = h.rpc_deferred(release());
    let (epoch, stop) = h.pending_stop();
    h.receipt(epoch, stop).feed();
    assert_eq!(rx.try_recv().unwrap().unwrap()["ok"], PyValue::Bool(true));
    let first = g15_provenance(&h);
    g15_move_time(&mut h);
    let (_attach_rx, _) = h.rpc_deferred(SessionRequest::DeviceAttach { force: false });
    let (again_rx, _) = h.rpc_deferred(release());
    let opened = h.opened_bridge(REAL_PATH);
    let late_epoch = opened.epoch;
    let drain = h.feed(ControlEvent::BridgeOpened {
        epoch_hint: 0,
        result: Ok(opened),
    });
    let stop_id = drain
        .iter()
        .find_map(|effect| match effect {
            Effect::BeginDrain { stop_id, epoch } if *epoch == late_epoch => Some(*stop_id),
            _ => None,
        })
        .expect("晚到的 bridge 須 drain");
    h.receipt(late_epoch, stop_id).feed();
    let again = again_rx.try_recv().expect("drain 收尾後回覆").expect("ok");
    assert_eq!(again["already_released"], PyValue::Bool(true), "{}", g13_wire(&again));
    assert_eq!(g15_provenance(&h), first, "冪等 release 不得刷新 provenance");
}

#[test]
fn stale_bridge_opened_after_real_path_change_closed() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.feed(ControlEvent::DeviceAttached {
        by_id: DEVICE_BY_ID.into(),
        real_path: REAL_PATH.into(),
    });
    h.feed(ControlEvent::DeviceAttached {
        by_id: DEVICE_BY_ID.into(),
        real_path: "/fake/tty-new".into(),
    });
    let opened = h.opened_bridge(REAL_PATH);
    let opened_epoch = opened.epoch;
    let effects = h.feed(ControlEvent::BridgeOpened {
        epoch_hint: 0,
        result: Ok(opened),
    });
    assert!(effects.iter().any(
        |effect| matches!(effect, Effect::BeginDrain { epoch, .. } if *epoch == opened_epoch)
    ));
    assert_eq!(h.public()["state"], PyValue::from("ATTACHING"));
    assert_eq!(h.core.generations().conn_epoch, None);
}

#[test]
fn stale_bridge_opened_after_rebind_closed() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    let (_rx, _) = h.rpc_deferred(release());
    let (epoch, stop) = h.pending_stop();
    h.receipt(epoch, stop).feed();
    let (attach_rx, _) = h.rpc_deferred(SessionRequest::DeviceAttach { force: false });
    h.feed(ControlEvent::DeviceAttached {
        by_id: DEVICE_BY_ID.into(),
        real_path: "/fake/tty-new".into(),
    });
    let opened = h.opened_bridge(REAL_PATH);
    let opened_epoch = opened.epoch;
    let effects = h.feed(ControlEvent::BridgeOpened {
        epoch_hint: 0,
        result: Ok(opened),
    });
    assert!(effects.iter().any(
        |effect| matches!(effect, Effect::BeginDrain { epoch, .. } if *epoch == opened_epoch)
    ));
    session_error(
        &attach_rx.try_recv().unwrap().unwrap(),
        "SESSION_RECOVERING",
    );
    assert_eq!(h.core.generations().conn_epoch, None);
}

#[test]
fn io_error_with_pending_auto_login_goes_recovering_not_attach() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    h.core.set_pending_auto_login_for_test(true);
    h.feed(ControlEvent::UartIoError {
        epoch: h.epoch(),
        reason: "SERIAL_READ:5".into(),
    });
    let (epoch, stop) = h.pending_stop();
    let effects = h.receipt(epoch, stop).feed();
    assert_eq!(h.public()["state"], PyValue::from("RECOVERING"));
    assert!(
        !effects
            .iter()
            .any(|effect| matches!(effect, Effect::OpenBridge { .. }))
    );
}

#[test]
fn release_response_closed_consoles_and_aborted_cmd() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    h.feed(ControlEvent::ConsoleCountChanged { count: 1 });
    let cmd = h.start_foreground_command("echo x");
    let (rx, _) = h.rpc_deferred(release());
    let (epoch, stop) = h.pending_stop();
    h.receipt(epoch, stop).confirmed(cmd.op_id, 1).feed();
    let response = rx.try_recv().unwrap().unwrap();
    assert_eq!(response["closed_consoles"], PyValue::from(1));
    assert_eq!(response["aborted_cmd"], PyValue::Bool(true));
}

#[test]
fn probe_stop_timeout_waits_late_receipt() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.feed(ControlEvent::DeviceAttached {
        by_id: DEVICE_BY_ID.into(),
        real_path: REAL_PATH.into(),
    });
    let probe_fsm_gen = h.core.generations().fsm_gen;
    let opened = h.opened_bridge(REAL_PATH);
    let epoch = opened.epoch;
    let probe_effects = h.feed(ControlEvent::BridgeOpened {
        epoch_hint: 0,
        result: Ok(opened),
    });
    assert!(probe_effects.iter().any(|effect| matches!(
        effect,
        Effect::StartLogin { plan: LoginPlan::Probe, .. }
    )));
    let (rx, _) = h.rpc_deferred(release());
    let (stop_epoch, stop) = h.pending_stop();
    assert_eq!(stop_epoch, epoch);
    let before_receipt = h.advance(Duration::from_millis(3001));
    assert!(!before_receipt
        .iter()
        .any(|effect| matches!(effect, Effect::OpenBridge { .. })));
    h.feed(ControlEvent::Timer(TimerEvent::DrainTimeout {
        epoch: stop_epoch,
        stop_id: stop,
    }));
    assert_eq!(
        rx.try_recv().unwrap().unwrap()["error_code"],
        PyValue::from("DRAIN_STALLED")
    );
    assert_eq!(h.public()["state"], PyValue::from("RECOVERING"));
    let late_effects = h.receipt(epoch, stop).feed();
    assert!(!late_effects
        .iter()
        .any(|effect| matches!(effect, Effect::OpenBridge { .. })));
    assert_eq!(h.public()["state"], PyValue::from("RELEASED"));
    let stale_probe = h.feed(ControlEvent::LoginFinished {
        epoch,
        fsm_gen: probe_fsm_gen,
        result: LoginOutcome::Ready,
        after_rx_seq: None,
    });
    assert!(!stale_probe
        .iter()
        .any(|effect| matches!(effect, Effect::OpenBridge { .. })));
    assert_eq!(h.public()["state"], PyValue::from("RELEASED"));
}

#[test]
fn session_attach_with_release_request_session_recovering() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    let (_rx, _) = h.rpc_deferred(release());
    let response = h.rpc(SessionRequest::SessionAttach).unwrap();
    session_error(&response, "SESSION_RECOVERING");
}

#[test]
fn release_during_pending_open_reply_is_stalled_until_close_receipt() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.feed(ControlEvent::DeviceAttached {
        by_id: DEVICE_BY_ID.into(),
        real_path: REAL_PATH.into(),
    });
    let (rx, _) = h.rpc_deferred(release());
    let (_epoch, stop) = h.pending_stop();
    h.advance(Duration::from_millis(3001));
    assert_eq!(
        rx.try_recv().unwrap().unwrap()["error_code"],
        PyValue::from("DRAIN_STALLED")
    );
    let opened = h.opened_bridge(REAL_PATH);
    let opened_epoch = opened.epoch;
    let effects = h.feed(ControlEvent::BridgeOpened {
        epoch_hint: 0,
        result: Ok(opened),
    });
    let drain_id = effects
        .iter()
        .find_map(|effect| match effect {
            Effect::BeginDrain { stop_id, .. } => Some(*stop_id),
            _ => None,
        })
        .unwrap();
    assert_eq!(drain_id, stop);
    assert_eq!(h.public()["state"], PyValue::from("RECOVERING"));
    h.receipt(opened_epoch, drain_id).handle_closed(true).feed();
    assert_eq!(h.public()["state"], PyValue::from("RELEASED"));
    assert_eq!(h.public()["drain_timeout_epoch"], PyValue::from(0));
    let _ = stop;
}

#[test]
fn stale_bridge_opened_epoch_after_released_attach_is_closed() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    let (_rx, _) = h.rpc_deferred(release());
    let (epoch, stop) = h.pending_stop();
    h.receipt(epoch, stop).feed();
    let (attach_rx, _) = h.rpc_deferred(SessionRequest::DeviceAttach { force: false });
    let opened = h.opened_bridge(REAL_PATH);
    let new_epoch = opened.epoch;
    h.feed(ControlEvent::BridgeOpened {
        epoch_hint: 0,
        result: Ok(opened),
    });
    assert!(
        attach_rx.try_recv().unwrap().unwrap()["ok"]
            .as_bool()
            .unwrap()
    );
    let stale = h.opened_bridge("/fake/tty-stale");
    let stale_epoch = stale.epoch;
    let effects = h.feed(ControlEvent::BridgeOpened {
        epoch_hint: 0,
        result: Ok(stale),
    });
    assert!(
        effects.iter().any(
            |effect| matches!(effect, Effect::BeginDrain { epoch, .. } if *epoch == stale_epoch)
        )
    );
    assert_eq!(h.core.generations().conn_epoch, Some(new_epoch));
}

#[test]
fn worker_receipt_type_keeps_unsettled_only_ledger_shape() {
    let op = OpId(41);
    let mut ledger = TxLedger {
        epoch: 7,
        ..TxLedger::default()
    };
    ledger.ops.insert(
        op,
        TxOpLedger {
            confirmed_bytes: 3,
            ..TxOpLedger::default()
        },
    );
    let receipt = WorkerStoppedReceipt {
        epoch: 7,
        stop_id: StopId(2),
        ledger,
        unrecorded: vec![EvidenceGap {
            op_id: Some(op),
            dir: "TX",
            len: 1,
            error: PalError::io(28, "full"),
        }],
        wal_error: None,
        handle_closed: true,
        close_error: None,
    };
    assert_eq!(receipt.ledger.ops.len(), 1);
    assert_eq!(receipt.unrecorded.len(), 1);
}

#[test]
fn duplicate_pending_open_manual_attach_only_opens_once() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    let (_rx, _) = h.rpc_deferred(release());
    let (epoch, stop) = h.pending_stop();
    h.receipt(epoch, stop).feed();
    let (_first, first_effects) = h.rpc_deferred(SessionRequest::DeviceAttach { force: false });
    assert_eq!(
        first_effects
            .iter()
            .filter(|e| matches!(e, Effect::OpenBridge { .. }))
            .count(),
        1
    );
    let response = h
        .rpc(SessionRequest::DeviceAttach { force: false })
        .unwrap();
    session_error(&response, "SESSION_RECOVERING");
    assert_eq!(h.fake_device.open_count(), 1);
}

#[test]
fn session_attach_released_matches_python_without_opening() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    let (_release_rx, _) = h.rpc_deferred(release());
    let (epoch, stop) = h.pending_stop();
    h.receipt(epoch, stop).feed();
    assert_eq!(h.public()["state"], PyValue::from("RELEASED"));

    let (rx, effects) = h.rpc_deferred(SessionRequest::SessionAttach);
    assert!(!effects
        .iter()
        .any(|effect| matches!(effect, Effect::OpenBridge { .. })));
    let response = rx.try_recv().expect("RELEASED session.attach 應立即回覆").unwrap();
    assert_eq!(response["ok"], PyValue::Bool(true));
    assert_eq!(response["released"], PyValue::Bool(true));
    assert_eq!(response["recommended_action"], PyValue::from("device_attach"));
    assert_eq!(response["session"]["state"], PyValue::from("RELEASED"));
    assert_eq!(h.public()["state"], PyValue::from("RELEASED"));
}

#[test]
fn session_attach_ready_matches_python_response_shape() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    let response = h.rpc(SessionRequest::SessionAttach).unwrap();
    assert_eq!(response["ok"], PyValue::Bool(true));
    assert!(response.get("session").is_some());
    assert!(response.get("already_attached").is_none());
}

#[test]
fn session_attach_attaching_bridge_matches_python_response_shape() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.feed(ControlEvent::DeviceAttached {
        by_id: DEVICE_BY_ID.into(),
        real_path: REAL_PATH.into(),
    });
    let opened = h.opened_bridge(REAL_PATH);
    h.feed(ControlEvent::BridgeOpened {
        epoch_hint: 0,
        result: Ok(opened),
    });
    assert_eq!(h.public()["state"], PyValue::from("ATTACHING"));

    let response = h.rpc(SessionRequest::SessionAttach).unwrap();
    assert_eq!(response["ok"], PyValue::Bool(true));
    assert!(response.get("session").is_some());
    assert!(response.get("already_attached").is_none());
}

#[test]
fn pending_open_timeout_is_rearmed_with_opened_bridge_epoch() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.feed(ControlEvent::DeviceAttached {
        by_id: DEVICE_BY_ID.into(),
        real_path: REAL_PATH.into(),
    });
    let (rx, _) = h.rpc_deferred(release());
    let (pending_epoch, stop_id) = h.pending_stop();
    assert_eq!(pending_epoch, 0);

    let opened = h.opened_bridge(REAL_PATH);
    let opened_epoch = opened.epoch;
    let effects = h.feed(ControlEvent::BridgeOpened {
        epoch_hint: 0,
        result: Ok(opened),
    });
    assert!(effects.iter().any(|effect| matches!(
        effect,
        Effect::BeginDrain { stop_id: actual_id, epoch }
            if *actual_id == stop_id && *epoch == opened_epoch
    )));

    h.advance(Duration::from_millis(3001));
    let response = rx.try_recv().expect("逾時應回覆").unwrap();
    session_error(&response, "DRAIN_STALLED");
    assert_eq!(response["drain_timeout_epoch"], PyValue::from(0));
    assert_eq!(h.public()["state"], PyValue::from("RECOVERING"));
    let receipt_effects = h.receipt(opened_epoch, stop_id).feed();
    assert!(!receipt_effects
        .iter()
        .any(|effect| matches!(effect, Effect::OpenBridge { .. })));
    assert_eq!(h.public()["state"], PyValue::from("RELEASED"));
}

#[test]
fn shutdown_during_io_error_drain_never_reopens_device() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    let epoch = h.epoch();
    h.feed(ControlEvent::UartIoError {
        epoch,
        reason: "read failed".into(),
    });
    let (stop_epoch, stop_id) = h.pending_stop();
    assert_eq!(stop_epoch, epoch);

    let (shutdown_tx, shutdown_rx) = bounded(1);
    h.feed(ControlEvent::Shutdown { reply: shutdown_tx });
    let effects = h.receipt(stop_epoch, stop_id).feed();
    assert!(!effects
        .iter()
        .any(|effect| matches!(effect, Effect::OpenBridge { .. })));
    assert_eq!(h.public()["state"], PyValue::from("DETACHED"));
    assert!(h.core.shutdown_complete());
    assert!(shutdown_rx.try_recv().unwrap().unwrap()["ok"] == PyValue::Bool(true));
    let late_device = h.feed(ControlEvent::DeviceAttached {
        by_id: DEVICE_BY_ID.into(),
        real_path: "/fake/tty-late-after-shutdown".into(),
    });
    assert!(!late_device
        .iter()
        .any(|effect| matches!(effect, Effect::OpenBridge { .. })));
    let attach = h
        .rpc(SessionRequest::DeviceAttach { force: false })
        .unwrap();
    session_error(&attach, "SESSION_RECOVERING");
}

#[test]
fn device_removed_clears_real_path_before_manual_attach() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    let epoch = h.epoch();
    h.feed(ControlEvent::DeviceDetached {
        by_id: DEVICE_BY_ID.into(),
        reason: DetachReason::Removed,
    });
    let (stop_epoch, stop_id) = h.pending_stop();
    assert_eq!(stop_epoch, epoch);
    h.receipt(stop_epoch, stop_id).feed();
    assert_eq!(h.public()["state"], PyValue::from("DETACHED"));

    let (rx, effects) = h.rpc_deferred(SessionRequest::DeviceAttach { force: false });
    let open_path = effects.iter().find_map(|effect| match effect {
        Effect::OpenBridge { dev, .. } => Some(dev.0.as_str()),
        _ => None,
    });
    assert!(open_path.is_none(), "已移除裝置不可重開舊 real_path");
    let response = rx.try_recv().expect("移除裝置 attach 應立即拒絕").unwrap();
    session_error(&response, "DEVICE_NOT_PRESENT");
}

#[test]
fn holder_unknown_is_true_when_no_pids_even_if_probe_flag_is_false() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    let (_rx, _) = h.rpc_deferred(release());
    let (epoch, stop) = h.pending_stop();
    h.receipt(epoch, stop).feed();
    let (attach_rx, _) = h.rpc_deferred(SessionRequest::DeviceAttach { force: false });
    h.feed(ControlEvent::BridgeOpened {
        epoch_hint: 0,
        result: Err(OpenFailure::Held(holder(vec![], false))),
    });
    let response = attach_rx.try_recv().unwrap().unwrap();
    session_error(&response, "DEVICE_STILL_HELD");
    assert_eq!(response["pids"], PyValue::List(vec![]));
    assert_eq!(response["holder_unknown"], PyValue::Bool(true));
}

#[test]
fn release_preserves_last_error_released_on_all_completion_paths() {
    let mut attached = CoreHarness::new(SHELL_PROFILE);
    attached.to_ready();
    let (_rx, _) = attached.rpc_deferred(release());
    let (epoch, stop) = attached.pending_stop();
    attached.receipt(epoch, stop).feed();
    assert_eq!(attached.public()["last_error"], PyValue::from("RELEASED"));

    let mut detached = CoreHarness::new(SHELL_PROFILE);
    detached.rpc(release()).unwrap();
    assert_eq!(detached.public()["last_error"], PyValue::from("RELEASED"));

    let mut late = CoreHarness::new(SHELL_PROFILE);
    late.to_ready();
    let (late_rx, _) = late.rpc_deferred(release());
    let (late_epoch, late_stop) = late.pending_stop();
    late.advance(Duration::from_millis(3001));
    session_error(&late_rx.try_recv().unwrap().unwrap(), "DRAIN_STALLED");
    late.receipt(late_epoch, late_stop).feed();
    assert_eq!(late.public()["last_error"], PyValue::from("RELEASED"));
}

#[test]
fn release_of_released_session_cancels_inflight_device_attach() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    let (_initial_release, _) = h.rpc_deferred(release());
    let (initial_epoch, initial_stop) = h.pending_stop();
    h.receipt(initial_epoch, initial_stop).feed();
    assert_eq!(h.public()["state"], PyValue::from("RELEASED"));

    let (attach_rx, attach_effects) =
        h.rpc_deferred(SessionRequest::DeviceAttach { force: false });
    assert!(attach_effects
        .iter()
        .any(|effect| matches!(effect, Effect::OpenBridge { .. })));
    let (release_rx, release_effects) = h.rpc_deferred(release());
    assert!(!release_effects
        .iter()
        .any(|effect| matches!(effect, Effect::OpenBridge { .. })));
    assert!(release_rx.try_recv().is_err(), "release 應等候在途 open 關閉");

    let opened = h.opened_bridge(REAL_PATH);
    let opened_epoch = opened.epoch;
    let effects = h.feed(ControlEvent::BridgeOpened {
        epoch_hint: 0,
        result: Ok(opened),
    });
    let drain_id = effects
        .iter()
        .find_map(|effect| match effect {
            Effect::BeginDrain { stop_id, .. } => Some(*stop_id),
            _ => None,
        })
        .expect("遲到 handle 必須進 drain");
    assert!(attach_rx.try_recv().unwrap().unwrap()["error_code"] == PyValue::from("SESSION_RECOVERING"));
    h.receipt(opened_epoch, drain_id).feed();
    let response = release_rx.try_recv().unwrap().unwrap();
    assert_eq!(response["ok"], PyValue::Bool(true));
    assert_eq!(response["already_released"], PyValue::Bool(true));
    assert_eq!(h.public()["state"], PyValue::from("RELEASED"));
    assert!(h.state_store.last_released().contains_key("t:COM0"));
}

#[test]
fn shutdown_waits_for_opened_bridge_pending_state_commit() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    let (_release_rx, _) = h.rpc_deferred(release());
    let (release_epoch, release_stop) = h.pending_stop();
    h.receipt(release_epoch, release_stop).feed();
    assert_eq!(h.public()["state"], PyValue::from("RELEASED"));

    h.defer_state_commits();
    let (attach_rx, _) = h.rpc_deferred(SessionRequest::DeviceAttach { force: false });
    let opened = h.opened_bridge(REAL_PATH);
    let opened_epoch = opened.epoch;
    h.feed(ControlEvent::BridgeOpened {
        epoch_hint: 0,
        result: Ok(opened),
    });
    let attach_commit = h.pending_state_commit_ids();
    assert_eq!(attach_commit.len(), 1);

    let (shutdown_tx, shutdown_rx) = bounded(1);
    h.feed(ControlEvent::Shutdown { reply: shutdown_tx });
    assert!(!h.core.shutdown_complete());
    assert!(shutdown_rx.try_recv().is_err());

    let effects = h.complete_state_commit(attach_commit[0]);
    assert!(effects.iter().any(|effect| matches!(
        effect,
        Effect::BeginDrain { epoch, .. } if *epoch == opened_epoch
    )));
    session_error(
        &attach_rx.try_recv().unwrap().unwrap(),
        "SESSION_RECOVERING",
    );
    assert!(!h.core.shutdown_complete());

    let (epoch, stop) = h.pending_stop();
    assert_eq!(epoch, opened_epoch);
    let effects = h.receipt(epoch, stop).feed();
    assert!(!effects
        .iter()
        .any(|effect| matches!(effect, Effect::OpenBridge { .. })));
    assert!(h.core.shutdown_complete());
    assert_eq!(h.public()["state"], PyValue::from("DETACHED"));
    assert_eq!(shutdown_rx.try_recv().unwrap().unwrap()["ok"], PyValue::Bool(true));
}

#[test]
fn release_during_released_attach_commit_restores_original_metadata() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    let (_initial_release, _) = h.rpc_deferred(release());
    let (initial_epoch, initial_stop) = h.pending_stop();
    h.receipt(initial_epoch, initial_stop).feed();
    let released_before = h.state_store.last_released()["t:COM0"].clone();

    h.defer_state_commits();
    let (attach_rx, _) = h.rpc_deferred(SessionRequest::DeviceAttach { force: false });
    let opened = h.opened_bridge(REAL_PATH);
    let opened_epoch = opened.epoch;
    h.feed(ControlEvent::BridgeOpened {
        epoch_hint: 0,
        result: Ok(opened),
    });
    let attach_commit = h.pending_state_commit_ids();
    assert_eq!(attach_commit.len(), 1);

    let (release_rx, _) = h.rpc_deferred(release());
    assert!(release_rx.try_recv().is_err());
    let (_epoch, _stop) = h.pending_stop();

    let effects = h.complete_state_commit(attach_commit[0]);
    assert!(effects.iter().any(|effect| matches!(
        effect,
        Effect::BeginDrain { epoch, .. } if *epoch == opened_epoch
    )));
    session_error(
        &attach_rx.try_recv().unwrap().unwrap(),
        "SESSION_RECOVERING",
    );
    let (epoch, stop) = h.pending_stop();
    h.receipt(epoch, stop).feed();
    let restore_commit = h.pending_state_commit_ids();
    assert_eq!(restore_commit.len(), 1, "Unreleased 成功後須恢復原 Released 列");
    h.complete_state_commit(restore_commit[0]);

    let response = release_rx.try_recv().unwrap().unwrap();
    assert_eq!(response["already_released"], PyValue::Bool(true));
    assert_eq!(h.state_store.last_released()["t:COM0"], released_before);
    assert_eq!(h.public()["state"], PyValue::from("RELEASED"));
}

#[test]
fn release_closed_console_count_comes_from_owner_and_is_platform_independent() {
    let mut empty = CoreHarness::new(SHELL_PROFILE);
    empty.to_ready();
    empty.feed(ControlEvent::ConsoleCountChanged { count: 0 });
    let (empty_rx, _) = empty.rpc_deferred(release());
    let (epoch, stop) = empty.pending_stop();
    empty.receipt(epoch, stop).feed();
    assert_eq!(
        empty_rx.try_recv().unwrap().unwrap()["closed_consoles"],
        PyValue::from(0)
    );

    let mut several = CoreHarness::new(SHELL_PROFILE);
    several.to_ready();
    several.feed(ControlEvent::ConsoleCountChanged { count: 3 });
    let (several_rx, _) = several.rpc_deferred(release());
    let (epoch, stop) = several.pending_stop();
    several.receipt(epoch, stop).feed();
    assert_eq!(
        several_rx.try_recv().unwrap().unwrap()["closed_consoles"],
        PyValue::from(3)
    );
}

#[test]
fn clear_reopen_attaching_clears_last_error() {
    // P5-T20 黑箱揭露：Python `clear_session` drain 收尾設 ATTACHING＋`CLEARED` 後立即 `_spawn_attach`，
    // `_attach_by_id` 開埠前即 `last_error = None`（實測 recover 救援失敗 → clear → ATTACHING 期間 last_error 為 null）。
    // Rust 重新開埠（reopen_known_device）時亦須清除 last_error，不得帶著 `CLEARED` 停在 ATTACHING。
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    let (rx, _) = h.rpc_deferred(SessionRequest::Clear);
    let (epoch, stop) = h.pending_stop();
    let effects = h.receipt(epoch, stop).feed();
    // 審查 F62：開埠前先交 manager 再解析（Python `_spawn_attach` → `_reresolve_profile_on_reattach`）。
    assert!(effects
        .iter()
        .any(|effect| matches!(effect, Effect::Reattach { .. })));
    // 裁決 D6（S1X-a-7，取代審查 F97）：`origin/main:sw_core/session_manager.py:2939-2953` 的 `_complete_drain`
    // 在 clear 且裝置在場時，鎖內即設 `ATTACHING`＋`last_error=None` 後才 `_spawn_attach`；`clear_session` 在
    // `_await_drain` 後組回應 → 回應與即時狀態皆為 null，不必等再解析後的 `DeviceAttached`。
    let reply = rx.try_recv().unwrap().expect("clear 回應");
    match reply.get("session") {
        Some(PyValue::Dict(session)) => {
            assert_eq!(session.get("state"), Some(&PyValue::from("ATTACHING")));
            assert_eq!(session.get("last_error"), Some(&PyValue::Null));
        }
        other => panic!("缺 session：{other:?}"),
    }
    let s = h.public();
    assert_eq!(s["state"], PyValue::from("ATTACHING"));
    assert_eq!(s["last_error"], PyValue::Null);
    let effects = h.feed(ControlEvent::DeviceAttached {
        by_id: DEVICE_BY_ID.into(),
        real_path: REAL_PATH.into(),
    });
    assert!(effects
        .iter()
        .any(|effect| matches!(effect, Effect::OpenBridge { .. })));
    assert_eq!(h.public()["last_error"], PyValue::Null, "開埠前清除（`_attach_by_id`）");
}

#[test]
fn aborted_in_flight_with_unknown_tail_writes_unknown_outcome() {
    // 審查 F42（Medium，afe4853）：帳本 `confirmed_bytes>0` 且 `unknown=true`（`TxOpLedger::record` 對
    // `Unknown{confirmed}` 同時累加 bytes 並設 unknown）時，Python `_map_aborted_result` 先看 unknown：
    // `COMMAND_ABORTED_IN_FLIGHT`＋META `tx_outcome:"unknown"`。舊 Rust 依長度寫 partial／complete。
    // 輸入：`echo x`（payload 7 bytes），帳本 confirmed 2＋unknown。
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    let cmd = h.start_foreground_command("echo x");
    let (rx, _) = h.rpc_deferred(release());
    let (epoch, stop) = h.pending_stop();
    h.receipt(epoch, stop)
        .confirmed(cmd.op_id, 2)
        .unknown(cmd.op_id)
        .handle_closed(true)
        .feed();
    let _ = rx.try_recv();
    assert_eq!(
        h.reg.get(&cmd.cmd_id).unwrap().get("error_code"),
        Some(&PyValue::from("COMMAND_ABORTED_IN_FLIGHT"))
    );
    let meta = h
        .wal_meta_events()
        .into_iter()
        .find(|row| row.get("event") == Some(&PyValue::from("command_aborted_in_flight")))
        .expect("META");
    assert_eq!(meta.get("confirmed_tx_bytes"), Some(&PyValue::from(2)));
    assert_eq!(meta.get("tx_outcome"), Some(&PyValue::from("unknown")));
}

/// 審查 F41：Python `_begin_drain_locked` 在等回條前（Phase 1）即設 `last_error=<reason>`（setter 清 detail）、
/// 清前景欄位並停 capture；drain 中的 `GetState` 與 `DRAIN_STALLED` 的 `session` 皆為此快照。
#[test]
fn phase1_snapshot_sets_reason_and_clears_foreground() {
    for (request, reason) in [
        (release(), "RELEASED"),
        (SessionRequest::Clear, "CLEARED"),
        (
            SessionRequest::Bind {
                by_id: "/s/by-id/other".into(),
            },
            "REBOUND",
        ),
    ] {
        let mut h = CoreHarness::new(SHELL_PROFILE);
        h.to_ready();
        h.core
            .set_last_error_for_test(Some("LOGIN_REQUIRED"), Some("keep-me"));
        let _running = h.start_foreground_command("sleep 20");
        assert_eq!(h.public()["foreground_busy"], PyValue::Bool(true));
        let (rx, _) = h.rpc_deferred(request);
        let snapshot = h.public();
        assert_eq!(snapshot["state"], PyValue::from("RECOVERING"), "{reason}");
        assert_eq!(snapshot["last_error"], PyValue::from(reason), "{reason}");
        assert_eq!(snapshot["last_error_detail"], PyValue::Null, "{reason}");
        assert_eq!(snapshot["foreground_busy"], PyValue::Bool(false), "{reason}");
        assert_eq!(snapshot["outstanding_commands"], PyValue::from(0), "{reason}");
        assert_eq!(snapshot["fg_cmd_expected_duration_s"], PyValue::Null, "{reason}");
        if reason == "RELEASED" {
            h.advance(Duration::from_millis(3001));
            let response = rx.try_recv().unwrap().unwrap();
            session_error(&response, "DRAIN_STALLED");
            let PyValue::Dict(session) = &response["session"] else {
                panic!("DRAIN_STALLED 應帶 session");
            };
            assert_eq!(session["last_error"], PyValue::from("RELEASED"));
            assert_eq!(session["last_error_detail"], PyValue::Null);
            assert_eq!(session["foreground_busy"], PyValue::Bool(false));
            assert_eq!(session["outstanding_commands"], PyValue::from(0));
        }
    }
}

// wp510 第 13 項：未確認關閉的 by_id（`UnconfirmedCloses`，Python `_draining_by_ids`）擋下 actor 內部的再開埠入口。

/// 正式 bridge 的關閉回條 `handle_closed:false`：by_id 登記為未確認關閉（Python session drain 未取得安全回條即留在
/// `_draining_by_ids`，`origin/main:sw_core/session_manager.py:2774-2775`、`:2918-2919`）。
#[test]
fn wp510_review7_worker_close_unconfirmed_registers_by_id() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    h.feed(ControlEvent::UartIoError {
        epoch: h.epoch(),
        reason: "BRIDGE_DOWN:EIO".into(),
    });
    let (epoch, stop) = h.pending_stop();
    assert!(!h.unconfirmed.contains(DEVICE_BY_ID), "回條前不得登記");
    let effects = h.receipt(epoch, stop).handle_closed(false).feed();
    assert!(h.unconfirmed.contains(DEVICE_BY_ID), "handle_closed:false 必須登記未確認關閉");
    assert!(
        !effects.iter().any(|effect| matches!(effect, Effect::OpenBridge { .. })),
        "未確認關閉不得重新開埠"
    );
}

/// io_error 收尾的再 attach（`reopen_known_device`）：Python `_complete_drain` 設 ATTACHING＋last_error=None 後
/// `_spawn_attach`，因 `_draining_by_ids` 早退（`origin/main:sw_core/session_manager.py:2971-2988`、`:4113-4114`）。
#[test]
fn wp510_review7_io_error_reopen_blocked_by_unconfirmed_close() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    h.feed(ControlEvent::UartIoError {
        epoch: h.epoch(),
        reason: "TX_LEDGER_SATURATED".into(),
    });
    let (epoch, stop) = h.pending_stop();
    h.unconfirmed.insert(DEVICE_BY_ID);
    let effects = h.receipt(epoch, stop).feed();
    assert!(
        !effects.iter().any(|effect| matches!(effect, Effect::OpenBridge { .. })),
        "未確認關閉的 by_id 不得重新開埠"
    );
    assert_eq!(h.public()["state"], PyValue::from("ATTACHING"));
    assert_eq!(h.public()["last_error"], PyValue::Null);
}

/// 裝置改指收尾的再 attach：Python `recover` 類 drain 收尾維持 DETACHED＋DEVICE_REBOUND_REQUIRED，`_spawn_attach` 早退
/// （`origin/main:sw_core/session_manager.py:2936-2948`、`:2969-2970`、`:4113-4114`）。
#[test]
fn wp510_review7_rebound_reopen_blocked_by_unconfirmed_close() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    h.feed(ControlEvent::DeviceDetached {
        by_id: DEVICE_BY_ID.into(),
        reason: DetachReason::Rebound,
    });
    let (epoch, stop) = h.pending_stop();
    h.feed(ControlEvent::DeviceAttached {
        by_id: DEVICE_BY_ID.into(),
        real_path: "/fake/tty-B".into(),
    });
    h.unconfirmed.insert(DEVICE_BY_ID);
    let effects = h.receipt(epoch, stop).feed();
    assert!(
        !effects.iter().any(|effect| matches!(effect, Effect::OpenBridge { .. })),
        "未確認關閉的 by_id 不得重新開埠"
    );
    assert_eq!(h.public()["state"], PyValue::from("DETACHED"));
    assert_eq!(
        h.public()["last_error"],
        PyValue::from("DEVICE_REBOUND_REQUIRED")
    );
}

/// hotplug 的 `DeviceAttached`：Python `_spawn_attach`／`_attach_by_id` 早退、不改狀態
/// （`origin/main:sw_core/session_manager.py:4113-4114`、`:4847-4848`）。
#[test]
fn wp510_review7_device_attached_blocked_by_unconfirmed_close() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.unconfirmed.insert(DEVICE_BY_ID);
    let effects = h.feed(ControlEvent::DeviceAttached {
        by_id: DEVICE_BY_ID.into(),
        real_path: REAL_PATH.into(),
    });
    assert!(
        !effects.iter().any(|effect| matches!(effect, Effect::OpenBridge { .. })),
        "未確認關閉的 by_id 不得開埠"
    );
    assert_eq!(h.public()["state"], PyValue::from("DETACHED"));
}

/// 開埠 gate 於 `OpenBridge` 發出後才發現 by_id 未確認關閉（`OpenFailure::CloseBlocked`，未開埠）：`device.attach`
/// 照 Python 開埠後重驗 drain 的分支回 SESSION_RECOVERING、狀態不變（`origin/main:sw_core/session_manager.py:3576-3588`、
/// `:3614-3631`）；`session.attach` 照 `attach_session` 停在 ATTACHING 回成功（`:3933-3944`、`:3999-4000`）。
#[test]
fn wp510_review7_open_gate_close_blocked_maps_per_entry() {
    for session_attach in [false, true] {
        let mut h = CoreHarness::new(SHELL_PROFILE);
        h.feed(ControlEvent::DeviceAttached {
            by_id: DEVICE_BY_ID.into(),
            real_path: REAL_PATH.into(),
        });
        h.feed(ControlEvent::BridgeOpened {
            epoch_hint: 0,
            result: Err(OpenFailure::Other(PalError::io(5, "open failed"))),
        });
        assert_eq!(h.public()["state"], PyValue::from("DETACHED"));
        let request = if session_attach {
            SessionRequest::SessionAttach
        } else {
            SessionRequest::DeviceAttach { force: false }
        };
        let (rx, effects) = h.rpc_deferred(request);
        assert!(
            effects.iter().any(|effect| matches!(effect, Effect::OpenBridge { .. })),
            "前置：尚未登記時照常發出 OpenBridge"
        );
        h.unconfirmed.insert(DEVICE_BY_ID);
        h.feed(ControlEvent::BridgeOpened {
            epoch_hint: 0,
            result: Err(OpenFailure::CloseBlocked),
        });
        let response = rx.try_recv().unwrap().unwrap();
        if session_attach {
            assert_eq!(response.get("ok"), Some(&PyValue::Bool(true)), "{response:?}");
            assert_eq!(nested_session_state(&response), PyValue::from("ATTACHING"));
            assert_eq!(h.public()["last_error"], PyValue::Null);
        } else {
            session_error(&response, "SESSION_RECOVERING");
            assert_eq!(response.get("drain_stalled"), Some(&PyValue::Bool(false)));
            assert_eq!(response.get("drain_timeout_epoch"), Some(&PyValue::Null));
            assert_eq!(nested_session_state(&response), PyValue::from("DETACHED"));
        }
    }
}

/// wp510 第 15 項（第九次整包審查 High P8-8463b22-1）：交接 handle 由 release 認領、`ReleaseAttachPending` 已把 session
/// 設為 RECOVERING（`origin/main:sw_core/session_manager.py:3133-3139`），release 於期限回 DRAIN_STALLED
/// （`:3192-3204`）之後，在途開埠（actor 已發 `OpenBridge`）的結果才回報。`h` 停在遲到收尾前的狀態並回傳收尾所用的 token。
fn wp510_review11_release_stalled_with_open_in_flight(h: &mut CoreHarness) -> u64 {
    let effects = h.feed(ControlEvent::DeviceAttached {
        by_id: DEVICE_BY_ID.into(),
        real_path: REAL_PATH.into(),
    });
    assert!(
        effects.iter().any(|effect| matches!(effect, Effect::OpenBridge { .. })),
        "前置：actor 已發出 OpenBridge"
    );
    h.rpc(SessionRequest::ReleaseAttachPending {
        by_id: DEVICE_BY_ID.into(),
        handoff_token: 77,
    })
    .unwrap();
    assert_eq!(h.public()["state"], PyValue::from("RECOVERING"));
    let token = 4242;
    let stalled = h
        .rpc(SessionRequest::PlaceholderAttachStalled { token, epoch: 0 })
        .unwrap();
    assert_eq!(stalled.get("drain_stalled"), Some(&PyValue::Bool(true)));
    token
}

/// 遲到的關閉收尾（Python `_finish_attach_op` 的 release 分支，`:4340-4363`）：不需第二次 release，即轉 RELEASED、
/// `released_by`／`released_reason` 取第一個 release 請求、清 `drain_stalled`／`drain_timeout_epoch` 並落盤。
fn wp510_review11_assert_late_settle_completes_release(h: &mut CoreHarness, token: u64) {
    wp510_review11_late_settle(h, token);
    wp510_review11_assert_release_completed(h);
}

/// 送出遲到的關閉收尾（`PlaceholderAttachSettled`，第一個 release 請求為 `cli`／`late-close`）。
fn wp510_review11_late_settle(h: &mut CoreHarness, token: u64) {
    h.rpc(SessionRequest::PlaceholderAttachSettled {
        token,
        release_source: "cli".into(),
        release_reason: PyValue::from("late-close"),
    })
    .unwrap();
}

/// 遲到收尾完成 release 的斷言（見 `wp510_review11_assert_late_settle_completes_release`）。
fn wp510_review11_assert_release_completed(h: &mut CoreHarness) {
    let public = h.public();
    assert_eq!(public["state"], PyValue::from("RELEASED"), "{public:?}");
    assert_eq!(public["released_by"], PyValue::from("cli"));
    assert_eq!(public["released_reason"], PyValue::from("late-close"));
    assert_eq!(public["drain_stalled"], PyValue::Bool(false));
    assert_eq!(public["drain_timeout_epoch"], PyValue::Null);
    assert!(
        h.state_store.last_released().contains_key("t:COM0"),
        "RELEASED 必須落盤"
    );
}

/// 被取消的開埠以一般開埠錯誤結束（審查實測：撤銷標記被清除後另開新 handle，因原 handle 仍開啟而失敗）：開埠已到終態，
/// actor 不得再把它當成在途開埠，遲到的關閉收尾須完成 RELEASED。
#[test]
fn wp510_review11_open_failure_after_release_stalled_lets_late_settle_release() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    let token = wp510_review11_release_stalled_with_open_in_flight(&mut h);
    h.feed(ControlEvent::BridgeOpened {
        epoch_hint: 0,
        result: Err(OpenFailure::Other(PalError::io(16, "Device or resource busy"))),
    });
    wp510_review11_assert_late_settle_completes_release(&mut h, token);
}

/// 同上，但遲到的開埠成功：actor 不接手（非 ATTACHING），退休 bridge 後開埠同樣已到終態，遲到收尾須完成 RELEASED。
/// 退休 bridge 的 handle 仍由其 worker 持有到排空回條（wp510 第十次整包審查 High P8-14fe592-1）：收尾須等配對且
/// `handle_closed:true` 的回條才完成 RELEASED 並落盤（Python `_finish_attach_op` 確認關閉後才完成，`:4320-4363`）。
#[test]
fn wp510_review11_open_success_after_release_stalled_lets_late_settle_release() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    let token = wp510_review11_release_stalled_with_open_in_flight(&mut h);
    let opened = h.opened_bridge(REAL_PATH);
    let retired_epoch = opened.epoch;
    h.feed(ControlEvent::BridgeOpened {
        epoch_hint: 0,
        result: Ok(opened),
    });
    let (epoch, stop) = h.pending_stop();
    assert_eq!(epoch, retired_epoch, "前置：退休 bridge 開始排空");
    wp510_review11_late_settle(&mut h, token);
    assert_eq!(
        h.public()["state"],
        PyValue::from("RECOVERING"),
        "退休 bridge 的 handle 確認關閉前不得完成 RELEASED"
    );
    assert!(
        !h.state_store.last_released().contains_key("t:COM0"),
        "退休 bridge 的 handle 確認關閉前不得落盤"
    );
    h.receipt(epoch, stop).handle_closed(true).feed();
    wp510_review11_assert_release_completed(&mut h);
}

/// `Effect::ClearRevokedHandoff` 的 `(by_id, token)` 清單。
fn wp510_review11_revoke_clears(effects: &[Effect]) -> Vec<(String, u64)> {
    effects
        .iter()
        .filter_map(|effect| match effect {
            Effect::ClearRevokedHandoff { by_id, token } => Some((by_id.clone(), *token)),
            _ => None,
        })
        .collect()
}

/// 撤銷標記的生命週期（wp510 第九次整包審查 High P8-8463b22-1）：開埠在途時 `ReleaseAttachPending` 不清除標記，
/// 留到該開埠的 `BridgeOpened` 終態才清除（開埠 worker 撞到標記即不開埠；Python 取消旗標隨作業保留，
/// `origin/main:sw_core/session_manager.py:3125`、`:4986-4996`），之後的開埠結果不再重複清除。
#[test]
fn wp510_review11_revoke_kept_until_in_flight_open_terminal() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.feed(ControlEvent::DeviceAttached {
        by_id: DEVICE_BY_ID.into(),
        real_path: REAL_PATH.into(),
    });
    let (_, effects) = h.rpc_full(SessionRequest::ReleaseAttachPending {
        by_id: DEVICE_BY_ID.into(),
        handoff_token: 77,
    });
    assert!(
        wp510_review11_revoke_clears(&effects).is_empty(),
        "開埠在途時不得清除撤銷標記"
    );
    let effects = h.feed(ControlEvent::BridgeOpened {
        epoch_hint: 0,
        result: Err(OpenFailure::CloseBlocked),
    });
    assert_eq!(
        wp510_review11_revoke_clears(&effects),
        vec![(DEVICE_BY_ID.to_string(), 77)],
        "在途開埠的終態清除撤銷標記"
    );
    let effects = h.feed(ControlEvent::BridgeOpened {
        epoch_hint: 0,
        result: Err(OpenFailure::CloseBlocked),
    });
    assert!(wp510_review11_revoke_clears(&effects).is_empty(), "只清除一次");
}

/// 無開埠在途：之後的 `DeviceAttached` 在 RECOVERING 不開埠（Python `_attach_by_id` 見取消中止），標記不會再被取用，
/// `ReleaseAttachPending` 即清除。
#[test]
fn wp510_review11_revoke_cleared_when_no_open_in_flight() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    let (_, effects) = h.rpc_full(SessionRequest::ReleaseAttachPending {
        by_id: DEVICE_BY_ID.into(),
        handoff_token: 78,
    });
    assert_eq!(
        wp510_review11_revoke_clears(&effects),
        vec![(DEVICE_BY_ID.to_string(), 78)]
    );
    assert_eq!(h.public()["state"], PyValue::from("RECOVERING"));
    let effects = h.feed(ControlEvent::DeviceAttached {
        by_id: DEVICE_BY_ID.into(),
        real_path: REAL_PATH.into(),
    });
    assert!(
        !effects.iter().any(|effect| matches!(effect, Effect::OpenBridge { .. })),
        "RECOVERING 的 DeviceAttached 不得開埠"
    );
}

/// wp510 第十次整包審查 High（P8-14fe592-1）：bind 等待已取走交接 handle 的開埠逾時（`attach_op_stalled_reply` →
/// `MarkAttachStalled`，Python `bind_session` `origin/main:sw_core/session_manager.py:3697-3711`）把 session 設為
/// RECOVERING，之後開埠成功但 actor 不接手而退休 bridge。`h` 停在退休 bridge 已開始排空、關閉回條尚未抵達的時點，
/// 回傳該 bridge 的 `(epoch, stop_id)`。
fn wp510_review12_retired_bridge_after_bind_stall(h: &mut CoreHarness) -> (u64, StopId) {
    let effects = h.feed(ControlEvent::DeviceAttached {
        by_id: DEVICE_BY_ID.into(),
        real_path: REAL_PATH.into(),
    });
    assert!(
        effects.iter().any(|effect| matches!(effect, Effect::OpenBridge { .. })),
        "前置：actor 已發出 OpenBridge"
    );
    let stalled = h
        .rpc(SessionRequest::MarkAttachStalled { epoch: 0 })
        .unwrap();
    assert_eq!(stalled.get("error_code"), Some(&PyValue::from("SESSION_RECOVERING")));
    assert_eq!(h.public()["state"], PyValue::from("RECOVERING"));
    let opened = h.opened_bridge(REAL_PATH);
    let retired_epoch = opened.epoch;
    let effects = h.feed(ControlEvent::BridgeOpened {
        epoch_hint: 0,
        result: Ok(opened),
    });
    assert!(
        effects.iter().any(|effect| matches!(
            effect,
            Effect::BeginDrain { epoch, .. } if *epoch == retired_epoch
        )),
        "前置：非 ATTACHING 的開埠結果不接手、退休 bridge 並開始排空"
    );
    let (epoch, stop) = h.pending_stop();
    assert_eq!(epoch, retired_epoch);
    assert_eq!(h.core.generations().conn_epoch, None, "前置：退休 bridge 未被接手");
    (epoch, stop)
}

/// 退休 bridge 的 handle 尚未確認關閉時，release 不得回成功、不得轉 RELEASED 或落盤；收到配對且 `handle_closed:true`
/// 的回條後才完成 RELEASED 並落盤（Python release 等待被取消的作業 `done`，`_finish_attach_op` 確認關閉後才完成
/// RELEASED，`origin/main:sw_core/session_manager.py:3188-3214`、`:4320-4363`）。
#[test]
fn wp510_review12_release_waits_retired_bridge_close_after_bind_stall() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    let (epoch, stop) = wp510_review12_retired_bridge_after_bind_stall(&mut h);
    let commits_before = h.state_store.commits();
    let (rx, _) = h.rpc_deferred(release());
    assert!(
        rx.try_recv().is_err(),
        "退休 bridge 的 handle 關閉確認前不得回覆 release"
    );
    assert_eq!(h.public()["state"], PyValue::from("RECOVERING"));
    assert_eq!(h.state_store.commits(), commits_before, "關閉確認前不得落盤");
    assert!(!h.state_store.last_released().contains_key("t:COM0"));

    // 不配對的回條（同 epoch、不同 stop_id）不得完成 release。
    h.receipt(epoch, StopId(stop.0 + 1000)).handle_closed(true).feed();
    assert!(rx.try_recv().is_err(), "不配對的回條不得完成 release");
    assert_eq!(h.public()["state"], PyValue::from("RECOVERING"));

    h.receipt(epoch, stop).handle_closed(true).feed();
    let response = rx
        .try_recv()
        .expect("handle 確認關閉後 release 應回覆")
        .unwrap();
    assert_eq!(response.get("ok"), Some(&PyValue::Bool(true)), "{response:?}");
    assert_eq!(nested_session_state(&response), PyValue::from("RELEASED"));
    let public = h.public();
    assert_eq!(public["state"], PyValue::from("RELEASED"), "{public:?}");
    assert_eq!(public["released_by"], PyValue::from("cli"));
    assert_eq!(public["drain_stalled"], PyValue::Bool(false));
    assert!(
        h.state_store.last_released().contains_key("t:COM0"),
        "RELEASED 必須落盤"
    );
    assert!(!h.unconfirmed.contains(DEVICE_BY_ID));
}

/// 同上，但退休 bridge 的 handle 關閉失敗（`handle_closed:false`）：release 回 DRAIN_STALLED，不轉 RELEASED、不落盤，
/// by_id 登記為未確認關閉（Python `_finish_attach_op` 關閉失敗即回 False、保留作業且不設 `done`，release 回
/// DRAIN_STALLED，`origin/main:sw_core/session_manager.py:4322-4332`、`:3192-3204`）；之後的 release 仍為 DRAIN_STALLED，
/// 推進時間也不會轉 RELEASED。
#[test]
fn wp510_review12_retired_bridge_close_failure_keeps_release_stalled() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    let (epoch, stop) = wp510_review12_retired_bridge_after_bind_stall(&mut h);
    let commits_before = h.state_store.commits();
    let (rx, _) = h.rpc_deferred(release());
    assert!(rx.try_recv().is_err(), "關閉回條前不得回覆 release");
    h.receipt(epoch, stop).handle_closed(false).feed();
    let response = rx
        .try_recv()
        .expect("關閉失敗應回覆 release")
        .unwrap();
    session_error(&response, "DRAIN_STALLED");
    assert_eq!(response.get("drain_stalled"), Some(&PyValue::Bool(true)));
    assert_eq!(h.public()["state"], PyValue::from("RECOVERING"));
    assert_eq!(h.public()["drain_stalled"], PyValue::Bool(true));
    assert!(
        h.unconfirmed.contains(DEVICE_BY_ID),
        "退休 bridge 的 handle 關閉未確認須登記阻擋"
    );

    let again = h.rpc(release()).unwrap();
    session_error(&again, "DRAIN_STALLED");
    h.advance(Duration::from_secs(4));
    assert_eq!(h.public()["state"], PyValue::from("RECOVERING"));
    assert_eq!(h.state_store.commits(), commits_before, "關閉未確認不得落盤");
    assert!(!h.state_store.last_released().contains_key("t:COM0"));
}

/// 關閉回條先於 release 抵達但為 `handle_closed:false`：之後的 release 不得因無 bridge 而直接 RELEASED，期限到回
/// DRAIN_STALLED（Python 作業保留在表內，release 找到作業後等待 `done` 至期限，`:3104-3107`、`:3188-3204`）。
#[test]
fn wp510_review12_release_after_retired_close_failure_reports_drain_stalled() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    let (epoch, stop) = wp510_review12_retired_bridge_after_bind_stall(&mut h);
    h.receipt(epoch, stop).handle_closed(false).feed();
    assert!(h.unconfirmed.contains(DEVICE_BY_ID));
    let commits_before = h.state_store.commits();
    let (rx, _) = h.rpc_deferred(release());
    assert!(rx.try_recv().is_err(), "期限前不得回覆");
    assert_eq!(h.public()["state"], PyValue::from("RECOVERING"));
    h.advance(Duration::from_secs(4));
    let response = rx.try_recv().expect("期限到應回覆").unwrap();
    session_error(&response, "DRAIN_STALLED");
    assert_eq!(h.public()["state"], PyValue::from("RECOVERING"));
    assert_eq!(h.state_store.commits(), commits_before);
    assert!(!h.state_store.last_released().contains_key("t:COM0"));
}

/// 開埠途中裝置改指（#50）：舊路徑的開埠結果不接手而退休，改以新路徑重開。release 等待新開埠時，新開埠即使失敗，
/// 也要等退休 bridge 的 handle 確認關閉才完成 RELEASED（開埠終態與 handle 關閉終態分開追蹤）。
#[test]
fn wp510_review12_release_after_rebound_open_failure_waits_retired_close() {
    const NEW_PATH: &str = "/fake/tty-A2";
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.feed(ControlEvent::DeviceAttached {
        by_id: DEVICE_BY_ID.into(),
        real_path: REAL_PATH.into(),
    });
    h.feed(ControlEvent::DeviceAttached {
        by_id: DEVICE_BY_ID.into(),
        real_path: NEW_PATH.into(),
    });
    let opened = h.opened_bridge(REAL_PATH);
    let effects = h.feed(ControlEvent::BridgeOpened {
        epoch_hint: 0,
        result: Ok(opened),
    });
    assert!(
        effects.iter().any(|effect| matches!(
            effect,
            Effect::OpenBridge { dev, .. } if dev.0 == NEW_PATH
        )),
        "前置：改以新路徑重開"
    );
    let (epoch, stop) = h.pending_stop();
    let (rx, _) = h.rpc_deferred(release());
    h.feed(ControlEvent::BridgeOpened {
        epoch_hint: 0,
        result: Err(OpenFailure::Other(PalError::io(2, "No such file"))),
    });
    assert!(
        rx.try_recv().is_err(),
        "退休 bridge 的 handle 關閉確認前不得回覆 release"
    );
    assert_eq!(h.public()["state"], PyValue::from("RECOVERING"));
    assert!(!h.state_store.last_released().contains_key("t:COM0"));
    h.receipt(epoch, stop).handle_closed(true).feed();
    let response = rx.try_recv().expect("確認關閉後應回覆").unwrap();
    assert_eq!(response.get("ok"), Some(&PyValue::Bool(true)), "{response:?}");
    assert_eq!(h.public()["state"], PyValue::from("RELEASED"));
    assert!(h.state_store.last_released().contains_key("t:COM0"));
}

/// 同上，但新開埠成功並由 release 的排空接手：新 bridge 確認關閉後，仍要等退休 bridge 的 handle 確認關閉才完成
/// RELEASED。
#[test]
fn wp510_review12_release_after_rebound_open_success_waits_retired_close() {
    const NEW_PATH: &str = "/fake/tty-A2";
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.feed(ControlEvent::DeviceAttached {
        by_id: DEVICE_BY_ID.into(),
        real_path: REAL_PATH.into(),
    });
    h.feed(ControlEvent::DeviceAttached {
        by_id: DEVICE_BY_ID.into(),
        real_path: NEW_PATH.into(),
    });
    let opened = h.opened_bridge(REAL_PATH);
    h.feed(ControlEvent::BridgeOpened {
        epoch_hint: 0,
        result: Ok(opened),
    });
    let (retired_epoch, retired_stop) = h.pending_stop();
    let (rx, _) = h.rpc_deferred(release());
    let opened = h.opened_bridge(NEW_PATH);
    let new_epoch = opened.epoch;
    let effects = h.feed(ControlEvent::BridgeOpened {
        epoch_hint: 0,
        result: Ok(opened),
    });
    let new_stop = effects
        .iter()
        .find_map(|effect| match effect {
            Effect::BeginDrain { stop_id, epoch } if *epoch == new_epoch => Some(*stop_id),
            _ => None,
        })
        .expect("前置：新 bridge 由 release 的排空接手");
    h.receipt(new_epoch, new_stop).handle_closed(true).feed();
    assert!(
        rx.try_recv().is_err(),
        "退休 bridge 的 handle 關閉確認前不得回覆 release"
    );
    assert_eq!(h.public()["state"], PyValue::from("RECOVERING"));
    assert!(!h.state_store.last_released().contains_key("t:COM0"));
    h.receipt(retired_epoch, retired_stop)
        .handle_closed(true)
        .feed();
    let response = rx.try_recv().expect("確認關閉後應回覆").unwrap();
    assert_eq!(response.get("ok"), Some(&PyValue::Bool(true)), "{response:?}");
    assert_eq!(h.public()["state"], PyValue::from("RELEASED"));
    assert!(h.state_store.last_released().contains_key("t:COM0"));
}

/// RELEASED 上的 `device.attach` 開埠成功但 unreleased 落盤失敗：Rust 還原 RELEASED 並退休 bridge（Python 存檔失敗時
/// 作業留在 `_preopened_ports`，`origin/main:sw_core/session_manager.py:3633-3637`）。之後的 release 須等退休 bridge 的
/// handle 確認關閉才回 already_released（Python release 取出作業、`_finish_attach_op` 關閉後才回覆，`:3108-3111`、
/// `:3188-3214`）。
#[test]
fn wp510_review12_released_release_waits_retired_bridge_after_attach_commit_failure() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    let (rx, _) = h.rpc_deferred(release());
    let (epoch, stop) = h.pending_stop();
    h.receipt(epoch, stop).feed();
    assert_eq!(
        nested_session_state(&rx.try_recv().unwrap().unwrap()),
        PyValue::from("RELEASED")
    );
    h.state_store
        .fail_next_commit(PalError::io(28, "[Errno 28] No space left on device"));
    let (attach_rx, effects) = h.rpc_deferred(SessionRequest::DeviceAttach { force: true });
    assert!(
        effects.iter().any(|effect| matches!(effect, Effect::OpenBridge { .. })),
        "前置：device.attach 發出 OpenBridge"
    );
    let opened = h.opened_bridge(REAL_PATH);
    let retired_epoch = opened.epoch;
    h.feed(ControlEvent::BridgeOpened {
        epoch_hint: 0,
        result: Ok(opened),
    });
    assert!(
        attach_rx.try_recv().expect("落盤失敗應回覆").is_err(),
        "前置：unreleased 落盤失敗"
    );
    assert_eq!(h.public()["state"], PyValue::from("RELEASED"));
    let (r_epoch, r_stop) = h.pending_stop();
    assert_eq!(r_epoch, retired_epoch, "前置：落盤失敗後退休 bridge");

    let (rx, _) = h.rpc_deferred(release());
    assert!(
        rx.try_recv().is_err(),
        "退休 bridge 的 handle 關閉確認前不得回覆 release"
    );
    h.receipt(r_epoch, r_stop).handle_closed(true).feed();
    let response = rx.try_recv().expect("確認關閉後應回覆").unwrap();
    assert_eq!(response.get("ok"), Some(&PyValue::Bool(true)), "{response:?}");
    assert_eq!(response.get("already_released"), Some(&PyValue::Bool(true)), "{response:?}");
    assert_eq!(h.public()["state"], PyValue::from("RELEASED"));
}
