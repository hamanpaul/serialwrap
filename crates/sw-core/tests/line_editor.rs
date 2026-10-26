//! P6 Task 3：line editor（`_consume_console_input`＋SC-16）對照 P1 Python golden（07 Task 3 Step 1）。
#![allow(dead_code)]

mod common;
use common::golden::{rows, unhex, unhex_utf8};
use sw_core::human_console::line_editor::{LineEditor, LineOutput};

fn out(lines: &[&str], echo: &[u8], overflowed: u32) -> LineOutput {
    LineOutput { lines: lines.iter().map(|s| s.to_string()).collect(), echo: echo.to_vec(), overflowed_lines: overflowed }
}

#[test] fn backspace_deletes_one_utf8_char_and_echoes() {
    let mut e = LineEditor::new();
    assert_eq!(e.consume("ab中".as_bytes()), out(&[], "ab中".as_bytes(), 0));
    assert_eq!(e.consume(b"\x7f"), out(&[], b"\x08 \x08", 0));
    assert_eq!(e.consume(b"\r"), out(&["ab"], b"\r", 0));
}
#[test] fn crlf_within_one_chunk_is_one_line() {
    assert_eq!(LineEditor::new().consume(b"ls\r\n"), out(&["ls"], b"ls\r", 0));
}
#[test] fn cr_and_lf_in_separate_chunks_emit_extra_empty_line() {   // UIO-12.10-a KEEP
    let mut e = LineEditor::new();
    assert_eq!(e.consume(b"ls\r"), out(&["ls"], b"ls\r", 0));
    assert_eq!(e.consume(b"\n"), out(&[""], b"\r", 0));
}
#[test] fn exactly_16385_bytes_is_submitted_for_arbiter_to_reject() {
    let mut e = LineEditor::new();
    let mut data = vec![b'a'; 16385]; data.push(b'\r');
    let o = e.consume(&data);
    assert_eq!(o.lines.len(), 1); assert_eq!(o.lines[0].len(), 16385); assert_eq!(o.overflowed_lines, 0);
}
#[test] fn byte_16386_discards_whole_line_until_terminator() {
    let mut e = LineEditor::new();
    let mut data = vec![b'a'; 16386]; data.extend_from_slice(b"\x7f\x7fbb\r\n");
    let o = e.consume(&data);
    assert!(o.lines.is_empty()); assert_eq!(o.overflowed_lines, 1);
    assert_eq!(e.buffered_len(), 0);
    assert_eq!(e.consume(b"ok\r"), out(&["ok"], b"ok\r", 0));
}
#[test] fn golden_matches_python() {
    // contracts/golden/line_editor.tsv：op "line_consume"；input＝以 "," 分隔的 chunk hex；
    // expected＝每 chunk 一組 "<行數>:<line1 hex>+<line2 hex>…/<echo hex>"，以 "," 分隔（行為 UTF-8 hex，可含 U+FFFD）
    let mut n = 0;
    for r in rows(include_str!("../../../contracts/golden/line_editor.tsv")).into_iter().filter(|r| r.op == "line_consume") {
        let mut e = LineEditor::new();
        let chunks: Vec<&str> = if r.input.is_empty() { vec![] } else { r.input.split(',').collect() };
        let outs: Vec<&str> = if r.expected.is_empty() { vec![] } else { r.expected.split(',').collect() };
        assert_eq!(chunks.len(), outs.len(), "case {}", r.id);
        for (c, o) in chunks.iter().zip(&outs) {
            let (lines_part, echo) = o.split_once('/').unwrap();
            let (count, joined) = lines_part.split_once(':').unwrap();
            let want: Vec<String> = if count == "0" { vec![] } else { joined.split('+').map(unhex_utf8).collect() };
            let got = e.consume(&unhex(c));
            assert_eq!(got.lines, want, "case {}", r.id);
            assert_eq!(got.echo, unhex(echo), "case {}", r.id);
        }
        n += 1;
    }
    assert!(n >= 1000, "golden 案例數 {n}");
}

#[test] fn lfcr_folds() { assert_eq!(LineEditor::new().consume(b"x\n\r"), out(&["x"], b"x\r", 0)); }
#[test] fn cr_cr_gives_empty_second_line() { assert_eq!(LineEditor::new().consume(b"x\r\r"), out(&["x", ""], b"x\r\r", 0)); }
#[test] fn backspace_on_empty_no_echo() { assert_eq!(LineEditor::new().consume(b"\x08"), out(&[], b"", 0)); }
#[test] fn backspace_resets_terminator_fold() { assert_eq!(LineEditor::new().consume(b"\r\x7f\n"), out(&["", ""], b"\r\r", 0)); }
#[test] fn control_bytes_buffered_not_echoed() {
    assert_eq!(LineEditor::new().consume(b"a\x03\x1bb\r"), out(&["a\x03\x1bb"], b"ab\r", 0));
}
#[test] fn tab_echoed() { assert_eq!(LineEditor::new().consume(b"\t\r"), out(&["\t"], b"\t\r", 0)); }
#[test] fn invalid_utf8_line_decoded_with_replace() {
    assert_eq!(LineEditor::new().consume(&[0xFF, 0x0D]), out(&["\u{FFFD}"], &[0xFF, 0x0D], 0));
}
/// Python 迴圈 `idx>0` 停在 0 → 整段刪除。
#[test] fn pop_continuation_only_buffer() {
    assert_eq!(
        LineEditor::new().consume(&[0x80, 0x80, 0x7F, 0x0D]),
        out(&[""], &[0x80, 0x80, 0x08, 0x20, 0x08, 0x0D], 0)
    );
}
/// P1 T12 規則 4：第 16386 個 byte 起丟棄且不回顯；終止字元回顯以 golden（`le-ovf-16386-z-bs-cr`）為準：P1 Python 不回顯 `\r`。
#[test] fn discarding_does_not_echo() {
    let mut data = vec![b'a'; 16386];
    data.extend_from_slice(b"z\x7f\r");
    let o = LineEditor::new().consume(&data);
    assert_eq!(o.overflowed_lines, 1);
    assert!(o.lines.is_empty());
    assert_eq!(o.echo, vec![b'a'; 16385]);
}

// parity-amendments-2026-10-26 #177（P6 審查 F6）：溢位 WARNING 時機與 Python 相同——第 16386 個 byte 到達當下
// （`uart_io.py:1706-1711`），與之後有無終止字元無關；`take_overflow_started` 回傳自上次取出後 discarding
// 由 false→true 的次數（同一 chunk 兩行溢位時 Python 寫兩次 WARNING，見 golden `le-ovf-two-lines`）。
#[test] fn overflow_warning_at_trigger_byte() {
    let mut e = LineEditor::new();
    let o = e.consume(&vec![b'a'; 16385]);
    assert_eq!(o.overflowed_lines, 0);
    assert_eq!(e.take_overflow_started(), 0, "恰 16385 bytes 尚未溢位");
    let o = e.consume(b"a");
    assert_eq!(o.overflowed_lines, 0, "overflowed_lines 仍只在終止字元結束 discarding 時加一（三欄語意不變）");
    assert_eq!(e.take_overflow_started(), 1, "第 16386 個 byte 到達當下即須回報");
    assert_eq!(e.take_overflow_started(), 0, "取出後歸零");
    let o = e.consume(b"\r");
    assert_eq!(o.overflowed_lines, 1);
    assert_eq!(e.take_overflow_started(), 0, "終止字元不再回報 WARNING");
}
#[test] fn overflow_without_terminator_still_warns() {
    let mut e = LineEditor::new();
    let o = e.consume(&vec![b'a'; 16386]);
    assert!(o.lines.is_empty());
    assert_eq!(o.overflowed_lines, 0);
    assert_eq!(e.take_overflow_started(), 1);
    let _ = e.consume(b"bb\x7fcc");
    assert_eq!(e.take_overflow_started(), 0, "discarding 期間不重複回報");
}
#[test] fn overflow_across_chunks_warns_once() {
    let mut e = LineEditor::new();
    let mut total = 0;
    for chunk in [&vec![b'a'; 16000][..], &vec![b'a'; 385][..], b"ab", b"c", b"\r"] {
        let _ = e.consume(chunk);
        total += e.take_overflow_started();
    }
    assert_eq!(total, 1);
    let _ = e.consume(b"ok\r");
    assert_eq!(e.take_overflow_started(), 0);
}
#[test] fn two_overflows_in_one_chunk_warn_twice() {
    let mut data = vec![b'a'; 16386];
    data.push(b'\r');
    data.extend(vec![b'a'; 16386]);
    data.push(b'\r');
    let mut e = LineEditor::new();
    assert_eq!(e.consume(&data).overflowed_lines, 2);
    assert_eq!(e.take_overflow_started(), 2);
}
#[test] fn golden_warn_count_matches_python() {
    // contracts/golden/line_editor.tsv：op "line_warn"；expected＝每 chunk 的 Python 溢位 WARNING 次數。
    let mut n = 0;
    for r in rows(include_str!("../../../contracts/golden/line_editor.tsv")).into_iter().filter(|r| r.op == "line_warn") {
        let mut e = LineEditor::new();
        let chunks: Vec<&str> = if r.input.is_empty() { vec![] } else { r.input.split(',').collect() };
        let want: Vec<u32> = r.expected.split(',').map(|w| w.parse().unwrap()).collect();
        assert_eq!(chunks.len(), want.len(), "case {}", r.id);
        for (c, w) in chunks.iter().zip(&want) {
            let _ = e.consume(&unhex(c));
            assert_eq!(e.take_overflow_started(), *w, "case {}", r.id);
        }
        n += 1;
    }
    assert!(n >= 13, "line_warn 案例數 {n}");
}
