//! P6 Task 6：human TX lane（07 D-15、D-22）。
//!
//! 一律 `set_auto_complete_human_tx(false)`，以 `complete_human_tx` 手動結算。
#![cfg(feature = "test-fakes")]
mod support;

use support::*;
use sw_core::admission::{AdmissionFlag, FreezeOutcome};
use sw_core::deferred::InvalidateReason;
use sw_core::human_console::human_tx::{BacklogItem, BacklogKind, HumanTxTag};
use sw_core::human_console::ConsoleTransportKind;
use sw_core::mailbox::ControlEvent;
use sw_core::session_core::*;
use sw_core::tx_writer::{FenceReject, TxClass, TxReport};
use sw_pal::traits::{TxOutcome, TxResult};
use sw_proto::constants::HUMAN_TX_BACKLOG_MAX_BYTES;

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

fn manual(cids: &[&str]) -> CoreHarness {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    h.set_auto_complete_human_tx(false);
    for cid in cids {
        h.connect_stream_console(cid, ConsoleTransportKind::TcpTelnet);
    }
    h
}

/// c1 human lease（PTY，自動開 lease 並成為 raw owner）。
fn manual_lease() -> CoreHarness {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    h.set_auto_complete_human_tx(false);
    h.attach_pty_console("c1", true);
    h
}

#[test]
fn single_job_in_flight() {
    let mut h = manual(&["c1"]);
    let fx1 = h.input("c1", b"a");
    let fx2 = h.input("c1", b"b");
    assert_eq!(human_txs(&fx1), vec![(TxClass::HumanRaw, "human:c1".to_string(), b"a".to_vec())]);
    assert!(human_txs(&fx2).is_empty(), "同時只有一筆在途");
    let fx3 = h.complete_human_tx(TxOutcome::Completed(1), None);
    assert_eq!(human_txs(&fx3), vec![(TxClass::HumanRaw, "human:c1".to_string(), b"b".to_vec())]);
}

#[test]
fn rejected_then_new_input_keeps_order() {
    let mut h = manual(&["c1"]);
    h.input("c1", b"a");
    let (adm, _) = h.fg_begin(true);
    h.input("c1", b"b");
    h.complete_human_tx(TxOutcome::NotSubmitted, Some(FenceReject::HumanAdmissionClosed));
    let fx = h.fg_finish(adm);
    assert_eq!(human_txs(&fx), vec![(TxClass::HumanReplay, "human:c1".to_string(), b"ab".to_vec())]);
}

#[test]
fn late_rejection_after_finish_is_replayed() {
    let mut h = manual(&["c1"]);
    h.input("c1", b"a");
    let (adm, _) = h.fg_begin(true);
    h.fg_finish(adm);
    let fx = h.complete_human_tx(TxOutcome::NotSubmitted, Some(FenceReject::OwnerMismatch));
    assert_eq!(human_txs(&fx), vec![(TxClass::HumanReplay, "human:c1".to_string(), b"a".to_vec())]);
}

#[test]
fn rejected_with_stale_epoch_dropped() {
    let mut h = manual(&["c1"]);
    h.input("c1", b"a");
    h.invalidate(InvalidateReason::QuietRearm);
    let fx = h.complete_human_tx(TxOutcome::NotSubmitted, Some(FenceReject::InputStale));
    assert!(human_txs(&fx).is_empty());
    assert_eq!(h.deferred_len("c1"), 0);
    assert!(h.warns().iter().any(|w| w.contains("reason=stale_epoch")));
}

#[test]
fn human_raw_partial_not_resent() {
    let mut h = manual(&["c1"]);
    h.input("c1", b"abc");
    let fx = h.complete_human_tx(TxOutcome::Completed(1), None);
    assert!(human_txs(&fx).is_empty());
    assert_eq!(h.deferred_len("c1"), 0);
    assert!(h
        .warns()
        .iter()
        .any(|w| w.contains("outcome=partial") && w.contains("written=1") && w.contains("len=3")));
}

#[test]
fn human_raw_unknown_not_resent() {
    let mut h = manual(&["c1"]);
    h.input("c1", b"abc");
    let fx = h.complete_human_tx(TxOutcome::Unknown { confirmed: 1 }, None);
    assert!(human_txs(&fx).is_empty());
    assert!(h.warns().iter().any(|w| w.contains("outcome=unknown")));
}

#[test]
fn backlog_overflow_clears_client() {
    let mut h = manual(&["c1", "c2"]);
    h.input("c1", b"a"); // 在途、不結算
    h.input("c2", b"q\r"); // c2 line（不進 lane）
    let chunk = vec![b'z'; 8192];
    let mut sent = 0usize;
    while sent <= HUMAN_TX_BACKLOG_MAX_BYTES {
        h.input("c1", &chunk);
        sent += chunk.len();
    }
    assert!(h.warns().iter().any(|w| w.contains("human TX backlog") && w.contains("client_id=c1")));
    // 結算 a 後 lane 不會再送出 c1 被清空的 backlog
    let fx = h.complete_human_tx(TxOutcome::Completed(1), None);
    let after: usize = human_txs(&fx).iter().map(|(_, _, p)| p.len()).sum();
    assert!(after < HUMAN_TX_BACKLOG_MAX_BYTES, "溢位時整段清空");
}

#[test]
fn replay_items_are_serialized() {
    let mut h = manual(&["c1", "c2"]);
    let (adm, _) = h.fg_begin(false);
    h.input("c1", b"x"); // owner＋admission 關 → defer
    h.seed_deferred("c2", b"y");
    let fx = h.fg_finish(adm);
    assert_eq!(human_txs(&fx), vec![(TxClass::HumanReplay, "human:c1".to_string(), b"x".to_vec())]);
    let hold = h.last_human_permit().unwrap().replay_hold;
    assert!(hold.is_some());
    assert_eq!(h.replay_hold().unwrap().remaining, 2);
    let fx = h.complete_human_tx(TxOutcome::Completed(1), None);
    assert_eq!(human_txs(&fx), vec![(TxClass::HumanReplay, "human:c2".to_string(), b"y".to_vec())]);
    assert_eq!(h.last_human_permit().unwrap().replay_hold, hold, "同一 hold");
    assert!(h.replay_hold().is_some());
    h.complete_human_tx(TxOutcome::Completed(1), None);
    assert_eq!(h.replay_hold(), None, "remaining 降到 0 後才釋放");
    assert_eq!(h.fence_replay_auth(), None);
}

#[test]
fn ownership_change_mid_replay_redefers_unsent() {
    let mut h = manual_lease();
    h.connect_stream_console("c2", ConsoleTransportKind::TcpTelnet);
    // 建立 c1 "x"、c2 "y" 的 deferred：c1 suspend 期間 defer；c2 以 owner 身分在 admission 關閉時 defer
    let (adm, _) = h.fg_begin(true);
    h.input("c1", b"x");
    h.seed_deferred("c2", b"y");
    let fx = h.fg_finish(adm);
    assert_eq!(human_txs(&fx), vec![(TxClass::HumanReplay, "human:c1".to_string(), b"x".to_vec())]);
    let first_hold = h.last_human_permit().unwrap().replay_hold.unwrap();
    let mark = h.human_tx().len();
    let (adm2, _) = h.fg_begin(true);
    assert_eq!(h.fence_replay_auth(), None);
    assert_eq!(h.deferred_len("c2"), 1, "y 已立即放回");
    h.input("c1", b"n");
    h.complete_human_tx(TxOutcome::NotSubmitted, Some(FenceReject::ReplayRevoked));
    assert_eq!(h.human_tx().len(), mark, "兩次 fg 之間無新 HumanTx");
    let fx = h.fg_finish(adm2);
    assert_eq!(human_txs(&fx), vec![(TxClass::HumanReplay, "human:c1".to_string(), b"xn".to_vec())]);
    let second_hold = h.last_human_permit().unwrap().replay_hold.unwrap();
    assert_ne!(second_hold, first_hold);
    let fx = h.complete_human_tx(TxOutcome::Completed(2), None);
    assert_eq!(human_txs(&fx), vec![(TxClass::HumanReplay, "human:c2".to_string(), b"y".to_vec())]);
    assert_eq!(h.last_human_permit().unwrap().replay_hold, Some(second_hold));
}

#[test]
fn revoked_replay_completed_before_revoke_is_kept() {
    let mut h = manual_lease();
    h.connect_stream_console("c2", ConsoleTransportKind::TcpTelnet);
    let (adm, _) = h.fg_begin(true);
    h.input("c1", b"x");
    h.seed_deferred("c2", b"y");
    h.fg_finish(adm);
    let (adm2, _) = h.fg_begin(true);
    let warns = h.warns().len();
    let fx = h.complete_human_tx(TxOutcome::Completed(1), None);
    assert!(human_txs(&fx).is_empty(), "不重送 x");
    assert_eq!(h.warns().len(), warns, "已提交者照實記錄、無 WARNING");
    assert_eq!(h.deferred_len("c2"), 1);
    let fx = h.fg_finish(adm2);
    assert_eq!(human_txs(&fx), vec![(TxClass::HumanReplay, "human:c2".to_string(), b"y".to_vec())]);
}

#[test]
fn replay_hold_released_on_stale_epoch() {
    let mut h = manual_lease();
    let (adm, _) = h.fg_begin(true);
    h.input("c1", b"x");
    h.fg_finish(adm);
    assert!(h.replay_hold().is_some());
    h.invalidate(InvalidateReason::QuietRearm);
    h.complete_human_tx(TxOutcome::NotSubmitted, Some(FenceReject::InputStale));
    assert_eq!(h.deferred_len("c1"), 0);
    assert!(h.warns().iter().any(|w| w.contains("reason=stale_epoch")));
    assert_eq!(h.replay_hold(), None);
    assert_eq!(h.fence_replay_auth(), None);
    assert!(!h.foreground_busy(), "失效不會讓 hold 卡住前景");
}

#[test]
fn new_replay_point_waits_for_revoked_in_flight() {
    let mut h = manual_lease();
    let (adm, _) = h.fg_begin(true);
    h.input("c1", b"x");
    h.fg_finish(adm);
    let (adm2, _) = h.fg_begin(true); // 撤銷
    h.input("c1", b"n");
    let fx = h.fg_finish(adm2);
    assert!(human_txs(&fx).is_empty(), "revoked 在途未結算前不回放");
    assert!(!h.foreground_busy(), "revoked hold 不算前景忙");
    let fx = h.complete_human_tx(TxOutcome::NotSubmitted, Some(FenceReject::ReplayRevoked));
    assert_eq!(human_txs(&fx), vec![(TxClass::HumanReplay, "human:c1".to_string(), b"xn".to_vec())]);
}

#[test]
fn human_tx_settles_ledger_each_chunk() {
    let mut h = manual(&["c1"]);
    h.set_auto_complete_human_tx(true);
    let mut settles = 0usize;
    let mut ops = Vec::new();
    let mut settled_ops = Vec::new();
    for _ in 0..1000 {
        let fx = h.input("c1", b"k");
        for e in &fx {
            match e {
                Effect::HumanTx { permit, .. } => ops.push(permit.op_id),
                Effect::SettleTx { op_id, .. } => {
                    settles += 1;
                    assert!(ops.contains(op_id));
                    settled_ops.push(*op_id);
                }
                _ => {}
            }
        }
    }
    assert_eq!(ops.len(), 1000);
    assert_eq!(settles, 1000, "每個 HumanTxCompleted 恰一個 SettleTx（D-15(6)）");
    assert_eq!(settled_ops, ops, "SettleTx 的 op_id 與各 HumanTx permit 一一對應（不重複、不遺漏）");
}

// ---------------------------------------------------------------------------
// p6-cloud 審查回歸（ac46a0e..1f1fa67）
// ---------------------------------------------------------------------------

/// High（2f4f76a）：revoked 在途回放結算時，若新前景命令已出列，延後的回放不得在命令執行中送出。
#[test]
fn deferred_replay_after_revoked_waits_for_idle_foreground() {
    use sw_core::mailbox::ControlEvent;
    use sw_core::op_runner::OpOutcome;
    use sw_proto::pydict;
    let mut h = manual_lease();
    let (adm, _) = h.fg_begin(true);
    h.input("c1", b"x");
    h.fg_finish(adm); // x 以 HumanReplay 在途
    let (adm2, _) = h.fg_begin(true); // 撤銷
    h.input("c1", b"n");
    h.fg_finish(adm2); // revoked 在途 → 延後
    let c = h.start_foreground_command("echo c");
    let fx = h.complete_human_tx(TxOutcome::NotSubmitted, Some(FenceReject::ReplayRevoked));
    assert!(human_txs(&fx).is_empty(), "前景命令執行中不得回放：{:?}", human_txs(&fx));
    assert_eq!(h.fence_replay_auth(), None, "命令執行中不得授權回放");
    assert_eq!(h.deferred_len("c1"), 2, "x、n 仍留在 deferred");
    let epoch = h.epoch();
    let fx = h.feed(ControlEvent::OpFinished {
        op_id: c.op_id,
        epoch,
        outcome: OpOutcome::CommandResult(pydict! { "ok" => true, "stdout" => "c" }),
        after_rx_seq: None,
    });
    assert_eq!(human_txs(&fx), vec![(TxClass::HumanReplay, "human:c1".to_string(), b"xn".to_vec())]);
    let order = h.tx_order();
    let pos_cmd = order.iter().position(|(c, p)| *c == TxClass::Command && p == b"echo c\n").unwrap();
    let pos_xn = order.iter().position(|(c, p)| *c == TxClass::HumanReplay && p == b"xn").unwrap();
    assert!(pos_cmd < pos_xn, "回放在命令之後：{order:?}");
}

/// High（2f4f76a）：斷線刪掉 backlog 中的回放時要結算 hold，佇列不得永久卡住。
#[test]
fn disconnect_settles_backlogged_replay_hold() {
    let mut h = manual(&["c1", "c2"]);
    let (adm, _) = h.fg_begin(false);
    h.input("c1", b"x");
    h.seed_deferred("c2", b"y");
    h.fg_finish(adm);
    assert_eq!(h.replay_hold().unwrap().remaining, 2);
    h.disconnect("c2");
    h.complete_human_tx(TxOutcome::Completed(1), None);
    assert_eq!(h.replay_hold(), None, "c2 的回放已隨斷線刪除，hold 應釋放");
    assert_eq!(h.fence_replay_auth(), None);
    assert!(!h.foreground_busy(), "hold 釋放後前景不忙");
    let c = h.start_foreground_command("echo after");
    let _ = c;
}

/// High（2f4f76a）：backlog 溢位清掉某 client 的回放時同樣要結算 hold，且 hold 釋放後佇列可出列。
#[test]
fn overflow_settles_backlogged_replay_hold() {
    let mut h = manual(&["c1", "c2"]);
    let (adm, _) = h.fg_begin(false);
    h.input("c1", b"x");
    h.seed_deferred("c2", b"y");
    h.fg_finish(adm);
    assert_eq!(h.replay_hold().unwrap().remaining, 2);
    // c2 的 "y" 回放在 backlog；c2 後續輸入排在其後直到溢位、整段清空（含 y）。
    let chunk = vec![b'z'; 8192];
    let mut sent = 0usize;
    while sent <= HUMAN_TX_BACKLOG_MAX_BYTES {
        h.input("c2", &chunk);
        sent += chunk.len();
    }
    assert!(h.warns().iter().any(|w| w.contains("human TX backlog") && w.contains("client_id=c2")));
    let fx = h.complete_human_tx(TxOutcome::Completed(1), None);
    assert!(human_txs(&fx).is_empty(), "c2 的 backlog 已清空：{:?}", human_txs(&fx).len());
    assert_eq!(h.replay_hold(), None, "hold 應釋放");
    assert!(!h.foreground_busy());
}

/// Medium（8fdf62f）：溢位只清該 client（含觸發的那一筆），其他 client 的 backlog 保留。
#[test]
fn backlog_overflow_clears_only_that_client() {
    let mut h = manual(&["c1", "c2"]);
    h.input("c1", b"a"); // c1 在途、不結算
    let (adm, _) = h.fg_begin(false);
    h.seed_deferred("c2", b"q");
    let _ = adm; // c2 的 q 以 owner 外 client 的 deferred 保留，稍後回放
    let chunk = vec![b'z'; 8192];
    let mut sent = 0usize;
    while sent <= HUMAN_TX_BACKLOG_MAX_BYTES {
        h.input("c1", &chunk);
        sent += chunk.len();
    }
    assert!(h.warns().iter().any(|w| w.contains("human TX backlog") && w.contains("client_id=c1")));
    let fx = h.complete_human_tx(TxOutcome::Completed(1), None);
    assert!(
        human_txs(&fx).iter().all(|(_, src, _)| src != "human:c1"),
        "c1 的 backlog 應整段清空：{} 筆",
        human_txs(&fx).len()
    );
    assert_eq!(h.deferred_len("c2"), 1, "其他 client 不受影響");
}

/// Medium（8fdf62f）：真 actor＋TxWriter 下，1000 個按鍵的 op 逐一結算、帳本 2 s 內歸 0。
#[test]
fn human_tx_ledger_drains_after_1000_keys() {
    use std::time::{Duration, Instant};
    use sw_core::mailbox::{ControlEvent, DataEvent};
    let rig = ActorRig::new(SHELL_PROFILE).ready();
    rig.send_control(ControlEvent::ConsoleConnected {
        client_id: "c1".into(),
        client_lifetime: 1,
        label: "127.0.0.1:50000".into(),
        vtty: "127.0.0.1:50000".into(),
        kind: ConsoleTransportKind::TcpTelnet,
        close_gen: 0,
    });
    let _ = rig.get_state();
    for _ in 0..1000 {
        let epoch = rig.fence.with(|f| f.input_epoch);
        rig.data_sender()
            .send(DataEvent::ConsoleInputBytes {
                client_id: "c1".into(),
                client_lifetime: 1,
                input_epoch: epoch,
                bytes: b"k".to_vec(),
            })
            .unwrap();
    }
    let deadline = Instant::now() + Duration::from_secs(10);
    while rig.wal_tx_payloads().iter().filter(|p| p.as_slice() == b"k").count() < 1000 {
        assert!(Instant::now() < deadline, "1000 個按鍵未全部寫出");
        std::thread::sleep(Duration::from_millis(5));
    }
    let settle_deadline = Instant::now() + Duration::from_secs(2);
    loop {
        if rig.tx_ledger_gauge().live_ops == 0 {
            break;
        }
        assert!(Instant::now() < settle_deadline, "最後結算後 2 s 內 live_ops 應歸 0：{:?}", rig.tx_ledger_gauge().live_ops);
        std::thread::sleep(Duration::from_millis(5));
    }
}

#[test]
fn i294_r13_human_completion_preflight_covers_later_backlog_job() {
    let mut h = manual_lease();
    h.set_auto_complete_human_tx(false);
    let first_effects = h.input("c1", b"in-flight");
    let (first_tag, first_permit, first_payload) = first_effects
        .iter()
        .find_map(|effect| match effect {
            Effect::HumanTx { tag, permit, payload } => {
                Some((tag.clone(), permit.clone(), payload.clone()))
            }
            _ => None,
        })
        .expect("第一筆 HumanTx 已進入 lane");

    h.invalidate(InvalidateReason::QuietRearm);
    let epoch = h.core.generations().input_epoch;
    let lifetime = h.lifetime("c1");
    {
        let lane = &mut h.core.human_console_mut().lane;
        let stale_seq = lane.next_seq();
        lane.push_backlog(BacklogItem {
            tag: HumanTxTag {
                seq: stale_seq,
                client_id: "c1".into(),
                client_lifetime: lifetime,
                input_epoch: epoch.saturating_sub(1),
                class: TxClass::HumanRaw,
                len: 5,
            },
            payload: b"stale".to_vec(),
            kind: BacklogKind::Input,
        })
        .expect("第一個 backlog 項目已排入");
        let later_seq = lane.next_seq();
        lane.push_backlog(BacklogItem {
            tag: HumanTxTag {
                seq: later_seq,
                client_id: "c1".into(),
                client_lifetime: lifetime,
                input_epoch: epoch,
                class: TxClass::HumanReplay,
                len: 5,
            },
            payload: b"later".to_vec(),
            kind: BacklogKind::Replay { hold: 1 },
        })
        .expect("後續可送出的 backlog 項目已排入");
    }

    let make_completion = || ControlEvent::HumanTxCompleted {
        tag: first_tag.clone(),
        report: TxReport {
            op_id: first_permit.op_id,
            epoch: first_permit.epoch,
            result: TxResult {
                outcome: TxOutcome::Completed(first_payload.len()),
                error: None,
            },
            wal: None,
            reject: None,
        },
    };
    let starts_before = h.rx.human_submit_start_count_for_test();
    assert_eq!(h.rx.human_in_flight_for_test(), 0, "transition 前不建立預判 guard");
    let effects = h.feed(make_completion());
    assert_eq!(
        human_txs(&effects),
        vec![(TxClass::HumanReplay, "human:c1".into(), b"later".to_vec())],
        "首項因 stale epoch 略過後，transition 仍會送出後續 replay job"
    );
    let effect_guard = h.rx.begin_human_tx_effect();
    assert_eq!(h.rx.human_in_flight_for_test(), 1, "只對實際輸出的 HumanTx 建 guard");
    assert_eq!(h.rx.human_submit_start_count_for_test(), starts_before);
    drop(effect_guard);
    assert_eq!(h.rx.human_in_flight_for_test(), 0);
    assert_eq!(h.rx.human_submit_start_count_for_test(), starts_before);
}

#[test]
fn i294_r13_handoff_replay_is_included_in_preflight() {
    let mut h = manual_lease();
    h.set_auto_complete_human_tx(false);
    let gate = h.reg.admission_gate();
    // CoreHarness 的 human lease 已在 gate 發布旗標；此測試只需建立可撤銷凍結，保留 raw ownership。
    gate.set_flag("COM0", AdmissionFlag::Lease, false);
    let token = match gate.freeze_if_idle("i294-r13-handoff-replay") {
        FreezeOutcome::Frozen { token, .. } => token,
        FreezeOutcome::Busy(busy) => panic!("測試 gate 應可凍結：{busy:?}"),
    };
    assert!(h.input("c1", b"echo HANDOFF\n").is_empty());
    assert!(gate.thaw(&token), "測試凍結已解除");

    assert_eq!(h.rx.human_in_flight_for_test(), 0);
    let effects = h.feed(ControlEvent::Tick);
    assert_eq!(
        human_txs(&effects),
        vec![(TxClass::HumanRaw, "human:c1".into(), b"echo HANDOFF\n".to_vec())],
        "解凍後處理 Tick 前，core 先重放 handoff queue 中的 console event"
    );
    let effect_guard = h.rx.begin_human_tx_effect();
    assert_eq!(h.rx.human_in_flight_for_test(), 1);
    assert_eq!(h.rx.human_submit_start_count_for_test(), 0);
    drop(effect_guard);
    assert_eq!(h.rx.human_in_flight_for_test(), 0);
    assert_eq!(h.rx.human_submit_start_count_for_test(), 0);
}

#[test]
fn i294_r13_overprediction_without_effect_drops_guard_and_history() {
    let mut h = manual_lease();
    h.set_auto_complete_human_tx(false);
    let first_effects = h.input("c1", b"in-flight");
    let (first_tag, first_permit, first_payload) = first_effects
        .iter()
        .find_map(|effect| match effect {
            Effect::HumanTx { tag, permit, payload } => {
                Some((tag.clone(), permit.clone(), payload.clone()))
            }
            _ => None,
        })
        .expect("第一筆 HumanTx 已進入 lane");
    h.invalidate(InvalidateReason::QuietRearm);
    let lifetime = h.lifetime("c1");
    let epoch = h.core.generations().input_epoch;
    let seq = h.core.human_console_mut().lane.next_seq();
    h.core
        .human_console_mut()
        .lane
        .push_backlog(BacklogItem {
            tag: HumanTxTag {
                seq,
                client_id: "c1".into(),
                client_lifetime: lifetime,
                input_epoch: epoch.saturating_sub(1),
                class: TxClass::HumanRaw,
                len: 5,
            },
            payload: b"stale".to_vec(),
            kind: BacklogKind::Input,
        })
        .expect("stale backlog 項目已排入");
    let make_completion = || ControlEvent::HumanTxCompleted {
        tag: first_tag.clone(),
        report: TxReport {
            op_id: first_permit.op_id,
            epoch: first_permit.epoch,
            result: TxResult {
                outcome: TxOutcome::Completed(first_payload.len()),
                error: None,
            },
            wal: None,
            reject: None,
        },
    };
    let starts_before = h.rx.human_submit_start_count_for_test();
    assert_eq!(h.rx.human_in_flight_for_test(), 0);
    let effects = h.feed(make_completion());
    assert!(human_txs(&effects).is_empty(), "唯一 backlog 項目因 stale epoch 被略過");
    assert_eq!(h.rx.human_in_flight_for_test(), 0);
    assert_eq!(h.rx.human_submit_start_count_for_test(), starts_before);
}

#[test]
fn i294_r13_pre_event_guard_does_not_record_submit_without_human_tx() {
    let mut h = manual_lease();
    let starts_before = h.rx.human_submit_start_count_for_test();
    let effects = h.feed(ControlEvent::Tick);
    assert!(human_txs(&effects).is_empty());
    assert_eq!(h.rx.human_in_flight_for_test(), 0);
    assert_eq!(
        h.rx.human_submit_start_count_for_test(),
        starts_before,
        "沒有實際 HumanTx effect 的 transition 不碰 submit 訊號"
    );
}

#[test]
fn i294_r13_pre_event_guard_cleans_up_on_panic_refusal_and_cancel() {
    use std::panic::{catch_unwind, AssertUnwindSafe};

    let h = manual_lease();
    let panic_guard = h.rx.begin_human_tx_effect();
    let panic_result = catch_unwind(AssertUnwindSafe(|| {
        let _guard = panic_guard;
        panic!("inject actor panic while transition guard is held");
    }));
    assert!(panic_result.is_err());
    assert_eq!(h.rx.human_in_flight_for_test(), 0, "panic 展開時 guard 由 RAII 清理");

    let refused_guard = h.rx.begin_human_tx_effect();
    drop(refused_guard);
    assert_eq!(h.rx.human_in_flight_for_test(), 0, "writer 拒絕後 drop 清理 guard");
    assert_eq!(h.rx.human_submit_start_count_for_test(), 0, "拒絕 enqueue 不記 submit 起點");

    let cancelled_guard = h.rx.begin_human_tx_effect();
    drop(cancelled_guard);
    assert_eq!(h.rx.human_in_flight_for_test(), 0, "pump 取消後 drop 清理 guard");
    assert_eq!(h.rx.human_submit_start_count_for_test(), 0, "取消且未 enqueue 不記 submit 起點");
}
