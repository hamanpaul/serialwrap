//! P8 Task 14：`AdmissionGate`（arch §6.2 第 7 步；Python `sw_core/admission.py`）。
//!
//! 計畫 09 Task 14 Step 1 表的 `gate_admit_vs_freeze_linearizable`：8 個執行緒反覆 `admit("file.push","COM0")`／`done`，
//! 1 個執行緒 `freeze_if_idle("update")`，10,000 輪（`loom` 不適用，以 Barrier＋隨機 yield）。每輪恰為二者之一：
//! - freeze 回 token ⇒ 當下 live ticket 數為 0，其後所有 `admit` 回 `None`；
//! - freeze 回 busy ⇒ `frozen==false`，且 busy 的 `ops` 含當下持有的 ticket kind。
//!
//! 其餘案例對照 Python `AdmissionGate` 的語意：旗標判定、`flash.` 前綴的 FLASHING、`com=None` 列於 `"*"`、
//! `thaw` 只撤銷 `freeze_if_idle` 新建立的凍結（shutdown `freeze()` 接手後不得撤銷）。

use std::collections::hash_map::RandomState;
use std::hash::{BuildHasher, Hasher};
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Arc, Barrier};
use std::thread;

use sw_core::admission::{AdmissionFlag, AdmissionGate, BusyCom, FreezeOutcome};

const ROUNDS: usize = 10_000;
const WORKERS: usize = 8;

/// 每輪不同的少量隨機 yield（打散 admit／done／freeze 的交錯）。
fn jitter(seed: u64) {
    let mut h = RandomState::new().build_hasher();
    h.write_u64(seed);
    for _ in 0..(h.finish() % 4) {
        thread::yield_now();
    }
}

fn token_of(outcome: &FreezeOutcome) -> Option<(&str, bool)> {
    match outcome {
        FreezeOutcome::Frozen { token, created } => Some((token.as_str(), *created)),
        FreezeOutcome::Busy(_) => None,
    }
}

fn is_hex32(s: &str) -> bool {
    s.len() == 32 && s.bytes().all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}

#[test]
fn gate_admit_vs_freeze_linearizable() {
    let mut frozen_rounds = 0usize;
    let mut busy_rounds = 0usize;
    for round in 0..ROUNDS {
        let gate = Arc::new(AdmissionGate::new());
        let barrier = Arc::new(Barrier::new(WORKERS + 1));
        let stop = Arc::new(AtomicBool::new(false));
        // admit 回 None 的次數（凍結之後才可能出現）。
        let refused = Arc::new(AtomicUsize::new(0));
        // 凍結之後仍取得 ticket（違反線性化）。
        let admitted_after_frozen = Arc::new(AtomicUsize::new(0));
        let workers: Vec<_> = (0..WORKERS)
            .map(|w| {
                let (gate, barrier, stop) = (gate.clone(), barrier.clone(), stop.clone());
                let (refused, after) = (refused.clone(), admitted_after_frozen.clone());
                thread::spawn(move || {
                    barrier.wait();
                    let mut i = 0u64;
                    while !stop.load(Ordering::SeqCst) {
                        let frozen_before = gate.frozen();
                        match gate.admit("file.push", Some("COM0")) {
                            Some(ticket) => {
                                if frozen_before {
                                    after.fetch_add(1, Ordering::SeqCst);
                                }
                                jitter(round as u64 * 31 + w as u64 * 7 + i);
                                gate.done(ticket);
                            }
                            None => {
                                refused.fetch_add(1, Ordering::SeqCst);
                            }
                        }
                        i += 1;
                        if i > 64 {
                            break;
                        }
                    }
                })
            })
            .collect();
        barrier.wait();
        jitter(round as u64);
        let outcome = gate.freeze_if_idle("update");
        match &outcome {
            FreezeOutcome::Frozen { token, created } => {
                assert!(*created, "第 {round} 輪：新凍結必為建立者");
                assert!(is_hex32(token), "第 {round} 輪：token 須為 32 hex：{token}");
                assert_eq!(gate.live_tickets(), 0, "第 {round} 輪：token 發出時不得仍有 live ticket");
                assert!(gate.frozen(), "第 {round} 輪");
                assert!(gate.admit("file.push", Some("COM0")).is_none(), "第 {round} 輪：凍結後 admit 必回 None");
                frozen_rounds += 1;
            }
            FreezeOutcome::Busy(busy) => {
                assert!(!gate.frozen(), "第 {round} 輪：忙碌不得凍結");
                assert_eq!(
                    busy.by_com.get("COM0").map(|row| row.ops.clone()),
                    Some(vec!["file.push".to_string()]),
                    "第 {round} 輪：busy 的 ops 須含當下持有的 ticket kind：{busy:?}"
                );
                assert!(!busy.flashing, "第 {round} 輪：file.push 不是 flash");
                busy_rounds += 1;
            }
        }
        stop.store(true, Ordering::SeqCst);
        for w in workers {
            w.join().expect("worker panic");
        }
        assert_eq!(
            admitted_after_frozen.load(Ordering::SeqCst),
            0,
            "第 {round} 輪：觀察到凍結之後仍取得 ticket"
        );
        match token_of(&outcome) {
            Some(_) => {
                assert_eq!(gate.live_tickets(), 0, "第 {round} 輪：凍結後不得新增 ticket");
            }
            None => {
                assert_eq!(refused.load(Ordering::SeqCst), 0, "第 {round} 輪：未凍結時 admit 不得被拒");
                assert_eq!(gate.live_tickets(), 0, "第 {round} 輪：全部 ticket 已歸還");
                // 忙碌未凍結：之後閒置即可凍結。
                assert!(token_of(&gate.freeze_if_idle("update")).is_some(), "第 {round} 輪");
            }
        }
    }
    assert_eq!(frozen_rounds + busy_rounds, ROUNDS);
}

#[test]
fn idle_freeze_rejects_admit_and_repeat_returns_same_token() {
    let gate = AdmissionGate::new();
    assert!(!gate.frozen());
    let first = gate.freeze_if_idle("rollback");
    let (token, created) = token_of(&first).expect("閒置必凍結");
    assert!(created && is_hex32(token));
    assert!(gate.frozen());
    assert!(gate.admit("command.submit", Some("COM0")).is_none());
    // 重複呼叫：回既有 token，但不是建立者（之後 thaw 不得撤銷）。
    let again = gate.freeze_if_idle("update");
    assert_eq!(token_of(&again), Some((token, false)), "{again:?}");
    assert!(!gate.thaw(token), "非建立者的重複呼叫之後 thaw 不得撤銷");
    assert!(gate.frozen());
}

#[test]
fn thaw_only_undoes_freeze_created_by_freeze_if_idle() {
    let gate = AdmissionGate::new();
    let FreezeOutcome::Frozen { token, created: true } = gate.freeze_if_idle("update") else {
        panic!("閒置必凍結");
    };
    assert!(!gate.thaw("00000000000000000000000000000000"), "token 不符不得撤銷");
    assert!(gate.frozen());
    assert!(gate.thaw(&token), "建立者的 token 可撤銷");
    assert!(!gate.frozen());
    let ticket = gate.admit("file.push", Some("COM0")).expect("撤銷後恢復受理");
    gate.done(ticket);
    assert!(!gate.thaw(&token), "已撤銷的 token 再次 thaw 不動作");
}

#[test]
fn shutdown_freeze_is_never_thawed_by_handoff() {
    // 先交接凍結、shutdown 再接手：之後交接的 thaw 不得解除 shutdown 凍結。
    let gate = AdmissionGate::new();
    let FreezeOutcome::Frozen { token, .. } = gate.freeze_if_idle("update") else {
        panic!("閒置必凍結");
    };
    gate.freeze();
    assert!(!gate.thaw(&token), "shutdown 接手後 handoff 的 thaw 不得撤銷");
    assert!(gate.frozen());
    // 先 shutdown 凍結：交接回既有 token、非建立者、thaw 不動作。
    let gate = AdmissionGate::new();
    gate.freeze();
    let outcome = gate.freeze_if_idle("rollback");
    let (token, created) = token_of(&outcome).expect("已凍結回既有 token");
    assert!(!created && is_hex32(token));
    assert!(!gate.thaw(token));
    assert!(gate.frozen());
    assert!(gate.admit("device.release", Some("COM0")).is_none());
}

#[test]
fn busy_rows_group_by_com_with_sorted_ops_and_flags() {
    let gate = AdmissionGate::new();
    let a = gate.admit("file.push", Some("COM1")).unwrap();
    let b = gate.admit("command.submit", Some("COM1")).unwrap();
    let c = gate.admit("command.submit", Some("COM1")).unwrap();
    let d = gate.admit("session.recover", None).unwrap();
    gate.set_flag("COM0", AdmissionFlag::Recovering, true);
    gate.set_flag("COM0", AdmissionFlag::Lease, true);
    let FreezeOutcome::Busy(busy) = gate.freeze_if_idle("update") else {
        panic!("有 ticket／旗標必忙碌");
    };
    assert!(!busy.flashing);
    assert!(!gate.frozen());
    assert_eq!(busy.by_com.keys().map(String::as_str).collect::<Vec<_>>(), ["*", "COM0", "COM1"]);
    assert_eq!(
        busy.by_com["COM1"],
        BusyCom { ops: vec!["command.submit".into(), "file.push".into()], flags: vec![] }
    );
    assert_eq!(
        busy.by_com["COM0"],
        BusyCom { ops: vec![], flags: vec![AdmissionFlag::Lease, AdmissionFlag::Recovering] }
    );
    assert_eq!(busy.by_com["*"].ops, vec!["session.recover".to_string()]);
    for t in [a, b, c, d] {
        gate.done(t);
    }
    assert_eq!(gate.live_tickets(), 0);
    // 旗標仍在：仍忙碌；清除後才閒置。
    assert!(matches!(gate.freeze_if_idle("update"), FreezeOutcome::Busy(_)));
    gate.set_flag("COM0", AdmissionFlag::Recovering, false);
    gate.set_flag("COM0", AdmissionFlag::Lease, false);
    assert!(token_of(&gate.freeze_if_idle("update")).is_some());
}

#[test]
fn flash_ticket_or_flag_reports_flashing() {
    let gate = AdmissionGate::new();
    let probe = gate.admit("flash.sync_probe", None).unwrap();
    let FreezeOutcome::Busy(busy) = gate.freeze_if_idle("rollback") else {
        panic!("flash ticket 必忙碌");
    };
    assert!(busy.flashing, "flash. 前綴的 ticket ⇒ FLASHING_BUSY");
    assert_eq!(busy.by_com["*"].ops, vec!["flash.sync_probe".to_string()]);
    gate.done(probe);
    gate.set_flag("COM2", AdmissionFlag::Flashing, true);
    let FreezeOutcome::Busy(busy) = gate.freeze_if_idle("rollback") else {
        panic!("flashing 旗標必忙碌");
    };
    assert!(busy.flashing);
    assert_eq!(busy.by_com["COM2"].flags, vec![AdmissionFlag::Flashing]);
    gate.set_flag("COM2", AdmissionFlag::Flashing, false);
    assert!(token_of(&gate.freeze_if_idle("rollback")).is_some());
}

#[test]
fn reason_does_not_change_freeze_semantics() {
    for reason in ["update", "rollback", "revert", "supervision_set"] {
        let gate = AdmissionGate::new();
        let t = gate.admit("file.push", Some("COM0")).unwrap();
        let FreezeOutcome::Busy(busy) = gate.freeze_if_idle(reason) else {
            panic!("{reason}：有 ticket 必忙碌");
        };
        assert_eq!(busy.by_com["COM0"].ops, vec!["file.push".to_string()], "{reason}");
        assert!(!gate.frozen(), "{reason}");
        gate.done(t);
        let (token, created) = token_of(&gate.freeze_if_idle(reason)).map(|(t, c)| (t.to_string(), c)).unwrap();
        assert!(created && is_hex32(&token), "{reason}");
    }
}

#[test]
fn tickets_carry_kind_com_and_increasing_seq() {
    let gate = AdmissionGate::new();
    let a = gate.admit("file.pull", Some("COM3")).unwrap();
    let b = gate.admit("device.release", None).unwrap();
    assert_eq!((a.kind(), a.com()), ("file.pull", Some("COM3")));
    assert_eq!((b.kind(), b.com()), ("device.release", None));
    assert!(b.seq() > a.seq());
    assert_eq!(gate.live_tickets(), 2);
    gate.done(a);
    gate.done(b);
    assert_eq!(gate.live_tickets(), 0);
}
