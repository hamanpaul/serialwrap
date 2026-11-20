//! RX 行切分（P7 Task 12；`event_engine/line_buffer.py`）。
//!
//! 每次 `feed` 先附加位元組，再反覆切行：找到 `\n` 且行長 ≤ `max` → 去尾端單一 `\r` 後輸出；行長 > `max`
//!（嚴格）→ 輸出前 `max` bytes 並丟棄到該 `\n`；無 `\n` 而緩衝 > `max` → 只切一段 `max` bytes 後停止。
//! 每段經 `decode_utf8_replace` → `strip_ansi`，截斷段追加 `" ...truncated"`（截斷段不剝 `\r`）。

use sw_proto::pycompat::{decode_utf8_replace, strip_ansi};

pub struct LineBuffer {
    buf: Vec<u8>,
    max: usize,
}

impl LineBuffer {
    pub fn new(max_line_bytes: usize) -> Self {
        LineBuffer {
            buf: Vec::new(),
            max: max_line_bytes,
        }
    }

    pub fn feed(&mut self, data: &[u8]) -> Vec<String> {
        self.buf.extend_from_slice(data);
        let mut out = Vec::new();
        loop {
            let Some(idx) = self.buf.iter().position(|b| *b == b'\n') else {
                if self.buf.len() > self.max {
                    let chunk: Vec<u8> = self.buf.drain(..self.max).collect();
                    out.push(finalize(&chunk, true));
                }
                break;
            };
            if idx > self.max {
                let chunk = self.buf[..self.max].to_vec();
                self.buf.drain(..=idx);
                out.push(finalize(&chunk, true));
            } else {
                let mut chunk: Vec<u8> = self.buf.drain(..=idx).collect();
                chunk.pop();
                if chunk.last() == Some(&b'\r') {
                    chunk.pop();
                }
                out.push(finalize(&chunk, false));
            }
        }
        out
    }
}

fn finalize(raw: &[u8], truncated: bool) -> String {
    let mut text = strip_ansi(&decode_utf8_replace(raw));
    if truncated {
        text.push_str(" ...truncated");
    }
    text
}
