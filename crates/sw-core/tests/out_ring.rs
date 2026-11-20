//! P6 Task 5：每 console 出向 ring（SC-15、07 D-05；07 Task 5 Step 1）。
use sw_core::human_console::out_ring::*;
use sw_proto::constants::CONSOLE_OUT_RING_BYTES;
fn disp(b: &[u8]) -> OutItem { OutItem::Display { bytes: b.to_vec(), iac_escaped: false } }

#[test] fn drop_oldest_keeps_newest_within_cap() {
    let mut r = OutRing::new(10);
    r.push(disp(b"aaaa")); r.push(disp(b"bbbb"));
    assert_eq!(r.push(disp(b"cccc")), PushOutcome::DroppedOldest { bytes: 4 });
    assert_eq!(r.front(), Some(&b"bbbb"[..])); assert_eq!(r.display_bytes(), 8);
}
#[test] fn control_items_are_never_dropped() {
    let mut r = OutRing::new(4);
    r.push(OutItem::Control(vec![0xFF, 0xFB, 0x01]));
    r.push(disp(b"xxxx")); r.push(disp(b"yyyy"));
    assert_eq!(r.front(), Some(&[0xFF, 0xFB, 0x01][..]));
    r.consume(3); assert_eq!(r.front(), Some(&b"yyyy"[..]));
}
#[test] fn oversized_escaped_item_never_starts_inside_iac_pair() {
    let mut r = OutRing::new(4);
    // escaped 串：61 FF FF FF FF 62 → 取尾 4 bytes 起點落在 FF 對中間 → 後移 1
    r.push(OutItem::Display { bytes: vec![0x61, 0xFF, 0xFF, 0xFF, 0xFF, 0x62], iac_escaped: true });
    assert_eq!(r.front(), Some(&[0xFF, 0xFF, 0x62][..]));
}
#[test] fn partially_written_front_is_protected() {
    let mut r = OutRing::new(8);
    r.push(disp(b"abcdefgh")); r.consume(3);
    r.push(disp(b"ZZZZ"));
    assert_eq!(r.front(), Some(&b"defgh"[..]), "已部分寫出的元素不得被丟，避免輸出斷尾");
}

#[test] fn fifo_order_mixed() {
    let mut r = OutRing::new(64);
    r.push(OutItem::Control(b"A".to_vec())); r.push(disp(b"B")); r.push(OutItem::Control(b"C".to_vec()));
    assert_eq!(r.front(), Some(&b"A"[..])); r.consume(1);
    assert_eq!(r.front(), Some(&b"B"[..])); r.consume(1);
    assert_eq!(r.front(), Some(&b"C"[..])); r.consume(1);
    assert!(r.is_empty());
    assert_eq!(r.front(), None);
}
#[test] fn oversized_raw_item_keeps_tail() {
    let mut r = OutRing::new(4);
    r.push(disp(b"abcdef"));
    assert_eq!(r.front(), Some(&b"cdef"[..]));
}
#[test] fn clear_display_keeps_control() {
    let mut r = OutRing::new(64);
    r.push(disp(b"xx")); r.push(OutItem::Control(b"CC".to_vec())); r.push(disp(b"yyy"));
    assert_eq!(r.clear_display(), 5);
    assert_eq!(r.display_bytes(), 0);
    assert_eq!(r.front(), Some(&b"CC"[..]));
    r.consume(2);
    assert!(r.is_empty(), "只剩 Control");
}
#[test] fn dropped_total_accumulates() {
    let mut r = OutRing::new(2);
    for b in [b"1", b"2", b"3", b"4", b"5"] { r.push(disp(b)); }
    assert_eq!(r.dropped_total(), 3);
}
#[test] fn one_mib_flood_bounded() {
    let mut r = OutRing::new(CONSOLE_OUT_RING_BYTES);
    let n = (3 * CONSOLE_OUT_RING_BYTES) / 8192;
    for i in 0..n { r.push(disp(&[(i % 256) as u8; 8192])); }
    assert!(r.display_bytes() <= 1 << 20);
    let last = (n - 1) % 256;
    assert_eq!(r.back(), Some(&OutItem::Display { bytes: vec![last as u8; 8192], iac_escaped: false }));
    assert!(r.dropped_total() > 0);
}
#[test] fn control_bytes_tracks_unwritten() {
    let mut r = OutRing::new(64);
    r.push(OutItem::Control(vec![1, 2, 3])); r.push(OutItem::Control(vec![4, 5, 6, 7, 8]));
    r.consume(4);
    assert_eq!(r.control_bytes(), 4);
}

// parity-amendments-2026-10-26 #178（P6 審查 F7）：保護部分寫出的 front 時，被丟的是剛推入的新項，
// 回報不得謊稱 DroppedOldest。
#[test] fn partially_written_front_drops_newest_reported() {
    let mut r = OutRing::new(8);
    r.push(disp(b"abcdefgh")); r.consume(3);
    assert_eq!(r.push(disp(b"ZZZZ")), PushOutcome::DroppedNewest { bytes: 4 });
    assert_eq!(r.back(), Some(&disp(b"abcdefgh")), "back() 仍為舊項");
    assert_eq!(r.front(), Some(&b"defgh"[..]));
    assert_eq!(r.dropped_total(), 4);
}
#[test] fn older_dropped_before_newest_still_reports_newest() {
    // front 部分寫出（剩 5）＋B（2）＋新項 C（4）：先丟較舊的 B 仍超出，再丟新項 C → 以 DroppedNewest 回報合計。
    let mut r = OutRing::new(8);
    r.push(disp(b"abcdefgh")); r.consume(3);
    assert_eq!(r.push(disp(b"BB")), PushOutcome::Queued);
    assert_eq!(r.push(disp(b"CCCC")), PushOutcome::DroppedNewest { bytes: 6 });
    assert_eq!(r.back(), Some(&disp(b"abcdefgh")));
}
#[test] fn drop_of_older_only_stays_dropped_oldest() {
    let mut r = OutRing::new(8);
    r.push(disp(b"abcdefgh")); r.consume(3);
    r.push(OutItem::Control(b"C".to_vec()));
    // front 剩 5，新項 3 → 合計 8 不超出
    assert_eq!(r.push(disp(b"xyz")), PushOutcome::Queued);
    r.consume(5);
    // front 現為 Control（未部分寫出）；Display 只剩 xyz（3），再推 8 → 丟最舊的 xyz
    assert_eq!(r.push(disp(b"12345678")), PushOutcome::DroppedOldest { bytes: 3 });
}
