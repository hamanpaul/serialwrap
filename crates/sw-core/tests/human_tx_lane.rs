//! P6 Task 6：human TX lane（07 D-15、D-22）。
//!
//! 一律 `set_auto_complete_human_tx(false)`，以 `complete_human_tx` 手動結算。
#![cfg(feature = "test-fakes")]
mod support;

use support::*;
use sw_core::deferred::InvalidateReason;
use sw_core::human_console::ConsoleTransportKind;
use sw_core::session_core::*;
use sw_core::tx_writer::{FenceReject, TxClass};
use sw_pal::traits::TxOutcome;
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
    for _ in 0..1000 {
        let fx = h.input("c1", b"k");
        for e in &fx {
            match e {
                Effect::HumanTx { permit, .. } => ops.push(permit.op_id),
                Effect::SettleTx { op_id, .. } => {
                    settles += 1;
                    assert!(ops.contains(op_id));
                }
                _ => {}
            }
        }
    }
    assert_eq!(ops.len(), 1000);
    assert_eq!(settles, 1000, "每個 HumanTxCompleted 恰一個 SettleTx（D-15(6)）");
}
