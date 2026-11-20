//! P7 Task 12（08-slice23-login-events.md）：`LineBuffer` 對 Python 基線（`contracts/golden/p7/line_buffer.ndjson`）。
mod common;

use common::p7::{b64, golden_rows};
use sw_core::event_engine::line_buffer::LineBuffer;
use sw_proto::pyjson::PyValue;

#[test]
fn line_buffer_matches_python_golden() {
    for row in golden_rows("line_buffer") {
        let mut lb = LineBuffer::new(16384);
        let feeds = row["feeds"].as_array().expect("feeds");
        let outputs = row["outputs"].as_array().expect("outputs");
        for (i, feed) in feeds.iter().enumerate() {
            let want: Vec<String> = outputs[i]
                .as_array()
                .expect("output")
                .iter()
                .map(|v| v.as_str().expect("line").to_string())
                .collect();
            let data = b64(feed.as_str().expect("b64"));
            assert_eq!(lb.feed(&data), want, "case {} feed {}", row.id(), i);
        }
        let _ = PyValue::Null;
    }
}

#[test]
fn exactly_16384_byte_line_not_truncated_16385_is() {
    let mut lb = LineBuffer::new(16384);
    let mut d = vec![b'a'; 16384];
    d.push(b'\n');
    assert_eq!(lb.feed(&d), vec!["a".repeat(16384)]);
    let mut d2 = vec![b'b'; 16385];
    d2.push(b'\n');
    assert_eq!(lb.feed(&d2), vec![format!("{} ...truncated", "b".repeat(16384))]);
}

#[test] // 無換行超長塊：每次 feed 最多切一段，截斷塊不剝 `\r`
fn oversized_without_newline_cuts_one_chunk_per_feed() {
    let mut lb = LineBuffer::new(4);
    assert_eq!(lb.feed(b"abc\rdefghij"), vec!["abc\r ...truncated".to_string()]);
    assert_eq!(lb.feed(b""), vec!["defg ...truncated".to_string()]);
    assert_eq!(lb.feed(b"\n"), vec!["hij".to_string()]);
}
