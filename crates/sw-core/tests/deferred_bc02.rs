//! P6 Task 4：DeferredBuffer（BC-02；07 D-02／D-07／D-15／D-22；07 Task 4 Step 1）。
use sw_core::deferred::*;
const CAP: usize = 262144;
fn live(_: &str, _: u64) -> bool { true }

fn rp(cid: &str, life: u64, bytes: &[u8]) -> Replay {
    Replay { client_id: cid.into(), client_lifetime: life, source: format!("human:{cid}"), bytes: bytes.to_vec() }
}

#[test] fn overflow_deletes_whole_entry_then_new_bytes_kept() {   // BC-02，P1 T09 規則 1（對照改寫後的 test_deferred_buffer_capped）
    let mut d = DeferredBuffer::new(CAP);
    assert_eq!(d.append("c1", 1, 0, 0, &vec![b'a'; CAP]), DeferOutcome::Appended { len: CAP });
    assert_eq!(d.append("c1", 1, 0, 0, b"b"), DeferOutcome::Overflowed { dropped: CAP + 1 });
    assert_eq!(d.len_for("c1"), 0);
    assert_eq!(d.append("c1", 1, 0, 0, &[b'B'; 10]), DeferOutcome::Appended { len: 10 }, "溢位後的 bytes 開新 entry 並保留");
    assert_eq!(d.take_replay(0, &live), vec![Replay { client_id: "c1".into(), client_lifetime: 1, source: "human:c1".into(), bytes: vec![b'B'; 10] }]);
}
#[test] fn stale_epoch_bytes_never_enter_buffer() {
    let mut d = DeferredBuffer::new(CAP);
    assert_eq!(d.append("c1", 1, 3, 4, b"old"), DeferOutcome::StaleEpoch);
    assert_eq!(d.total_bytes(), 0);
}
#[test] fn invalidate_then_replay_sends_nothing_old() {
    let mut d = DeferredBuffer::new(CAP);
    d.append("c1", 1, 0, 0, b"ZZ");
    assert_eq!(d.invalidate_all(), 2);
    assert!(d.take_replay(1, &live).is_empty());
}
#[test] fn replay_preserves_first_insert_order_and_source() {
    let mut d = DeferredBuffer::new(CAP);
    d.append("bb", 1, 0, 0, b"1"); d.append("aa", 2, 0, 0, b"2"); d.append("bb", 1, 0, 0, b"3");
    assert_eq!(d.take_replay(0, &live), vec![
        Replay { client_id: "bb".into(), client_lifetime: 1, source: "human:bb".into(), bytes: b"13".to_vec() },
        Replay { client_id: "aa".into(), client_lifetime: 2, source: "human:aa".into(), bytes: b"2".to_vec() },
    ]);
    assert_eq!(d.total_bytes(), 0);
}

#[test] fn exact_cap_kept() {
    let mut d = DeferredBuffer::new(CAP);
    assert_eq!(d.append("c1", 1, 0, 0, &vec![b'a'; CAP]), DeferOutcome::Appended { len: CAP });
    assert_eq!(d.len_for("c1"), CAP);
}
#[test] fn dead_client_not_replayed() {
    let mut d = DeferredBuffer::new(CAP);
    d.append("c1", 1, 0, 0, b"x");
    assert!(d.take_replay(0, &|c: &str, _| c != "c1").is_empty());
    assert_eq!(d.total_bytes(), 0, "entry 已清");
    assert_eq!(d.len_for("c1"), 0);
}
#[test] fn lifetime_mismatch_not_replayed() {
    let mut d = DeferredBuffer::new(CAP);
    d.append("c1", 1, 0, 0, b"x");
    assert!(d.take_replay(0, &|_: &str, life| life == 2).is_empty());
}
#[test] fn entry_epoch_mismatch_not_replayed() {
    let mut d = DeferredBuffer::new(CAP);
    d.append("c1", 1, 0, 0, b"x");
    assert!(d.take_replay(1, &live).is_empty());
}
#[test] fn new_lifetime_replaces_old_entry() {
    let mut d = DeferredBuffer::new(CAP);
    d.append("c1", 1, 0, 0, b"x");
    d.append("c1", 2, 0, 0, b"y");
    assert_eq!(d.take_replay(0, &live), vec![rp("c1", 2, b"y")]);
}
/// P1 T09 規則 1 的 owner_gen（Rust：input_epoch）取代。
#[test] fn new_epoch_replaces_old_entry() {
    let mut d = DeferredBuffer::new(CAP);
    d.append("c1", 1, 0, 0, b"x");
    d.append("c1", 1, 1, 1, b"y");
    assert_eq!(d.take_replay(1, &live), vec![rp("c1", 1, b"y")]);
}
#[test] fn remove_client_keeps_others_order() {
    let mut d = DeferredBuffer::new(CAP);
    d.append("a", 1, 0, 0, b"1"); d.append("b", 1, 0, 0, b"2"); d.append("c", 1, 0, 0, b"3");
    assert_eq!(d.remove_client("b"), 1);
    assert_eq!(d.take_replay(0, &live), vec![rp("a", 1, b"1"), rp("c", 1, b"3")]);
}
#[test] fn overflow_isolated_per_client() {
    let mut d = DeferredBuffer::new(CAP);
    d.append("c1", 1, 0, 0, &vec![b'a'; CAP]);
    assert_eq!(d.append("c1", 1, 0, 0, b"!"), DeferOutcome::Overflowed { dropped: CAP + 1 });
    d.append("c2", 1, 0, 0, b"x");
    assert_eq!(d.take_replay(0, &live), vec![rp("c2", 1, b"x")]);
}
#[test] fn take_replay_for_only_that_client() {
    let mut d = DeferredBuffer::new(CAP);
    d.append("a", 1, 0, 0, b"1"); d.append("b", 1, 0, 0, b"2");
    assert_eq!(d.take_replay_for("b", 0, &live), Some(rp("b", 1, b"2")));
    assert_eq!(d.len_for("a"), 1, "a 仍在");
    assert_eq!(d.len_for("b"), 0);
}
/// Python 只回放非空 buffer。
#[test] fn empty_append_noop() {
    let mut d = DeferredBuffer::new(CAP);
    assert_eq!(d.append("c1", 1, 0, 0, b""), DeferOutcome::Appended { len: 0 });
    assert!(d.take_replay(0, &live).is_empty());
}
/// 07 D-22(4)：被撤銷的舊按鍵排在同 client 新輸入之前，跨 client 保持原回放順序。
#[test] fn restore_front_precedes_newer_bytes_and_clients() {
    let mut d = DeferredBuffer::new(CAP);
    d.append("c3", 1, 0, 0, b"z");
    d.append("c1", 1, 0, 0, b"new");
    assert_eq!(d.restore_front(vec![(rp("c1", 1, b"x"), 0), (rp("c2", 1, b"y"), 0)], 0), 0);
    assert_eq!(d.take_replay(0, &live), vec![rp("c1", 1, b"xnew"), rp("c2", 1, b"y"), rp("c3", 1, b"z")]);
}
#[test] fn restore_front_drops_stale_epoch() {
    let mut d = DeferredBuffer::new(CAP);
    assert_eq!(d.restore_front(vec![(rp("c1", 1, b"x"), 0)], 1), 1);
    assert_eq!(d.total_bytes(), 0);
}
/// 07 D-07：合併後長度 > cap 刪除整個 entry。
#[test] fn restore_front_overflow_deletes_entry() {
    let mut d = DeferredBuffer::new(CAP);
    d.append("c1", 1, 0, 0, &vec![b'a'; CAP]);
    assert_eq!(d.restore_front(vec![(rp("c1", 1, b"x"), 0)], 0), CAP + 1);
    assert_eq!(d.len_for("c1"), 0);
}
#[test] fn invalidate_reason_strings() {
    let all = [
        (InvalidateReason::BootBanner, "boot_banner"), (InvalidateReason::QuietRearm, "quiet_rearm"),
        (InvalidateReason::RebootCommand, "reboot_command"), (InvalidateReason::Recovery, "recovery"),
        (InvalidateReason::Release, "release"), (InvalidateReason::Flash, "flash"),
    ];
    for (r, s) in all { assert_eq!(r.as_str(), s); }
}
