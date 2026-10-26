//! 第二 console 起的 line-buffer 編輯（P6 Task 3；`uart_io.py` `_pop_last_console_char`／`_consume_console_input`＋SC-16）。
//!
//! 本地回顯、backspace 刪一個 UTF-8 字元、CR／LF 摺疊（每次呼叫重設，UIO-12.10-a）、16385 bytes 行上限（07 D-06）。
//! 溢位語意以 P1 Python 產生的 golden（`contracts/golden/line_editor.tsv`）為準：緩衝已滿時再收到非終止、非 backspace 的
//! byte 即清空緩衝並進入 discarding（該 byte 不回顯）；discarding 期間所有非終止 byte（含 backspace）丟棄且不回顯；
//! 下一個 CR／LF 結束 discarding，不回顯 `\r`、不送 arbiter，`overflowed_lines` 加一。
//! 溢位 WARNING 的時機與 Python 相同：在第 16386 個 byte 到達當下（`uart_io.py:1706-1711`），與之後有無終止字元無關；
//! 呼叫端每次 `consume` 後以 `take_overflow_started()` 取出本段期間進入 discarding 的次數並逐次寫 WARNING
//! （parity-amendments-2026-10-26 #177，P6 審查 F6）。

use sw_proto::constants::CONSOLE_LINE_BUFFER_MAX;
use sw_proto::pycompat::decode_utf8_replace;

/// 每個 line-mode console 的半行緩衝（07 D-21：BC-02 失效時不清除）。
#[derive(Debug, Default, Clone)]
pub struct LineEditor {
    buf: Vec<u8>,
    discarding: bool,
    /// 自上次 `take_overflow_started` 後 discarding 由 false→true 的次數（每次對應 Python 一筆 WARNING）。
    overflow_started: u32,
}

/// 一次 `consume` 的輸出：完成的行（UTF-8 replace 解碼）、要回顯給該 console 的 bytes、因超長整行丟棄的行數。
#[derive(Debug, Default, PartialEq, Eq)]
pub struct LineOutput {
    pub lines: Vec<String>,
    pub echo: Vec<u8>,
    pub overflowed_lines: u32,
}

impl LineEditor {
    pub fn new() -> Self {
        LineEditor::default()
    }

    /// `_consume_console_input`＋SC-16。
    pub fn consume(&mut self, data: &[u8]) -> LineOutput {
        let mut o = LineOutput::default();
        // 每次呼叫重設（CR 與 LF 分在兩個 chunk 時多送一個空行，UIO-12.10-a KEEP）。
        let mut last_term: Option<u8> = None;
        for &b in data {
            let is_term = b == 0x0A || b == 0x0D;
            if self.discarding && !is_term {
                last_term = None;
                continue;
            }
            match b {
                0x08 | 0x7F => {
                    last_term = None;
                    if pop_last_utf8_char(&mut self.buf) {
                        o.echo.extend_from_slice(b"\x08 \x08");
                    }
                }
                0x0A | 0x0D => {
                    if matches!(last_term, Some(t) if t != b) && self.buf.is_empty() {
                        last_term = None;
                        continue;
                    }
                    if self.discarding {
                        self.buf.clear();
                        self.discarding = false;
                        last_term = Some(b);
                        o.overflowed_lines += 1;
                        continue;
                    }
                    o.lines.push(decode_utf8_replace(&self.buf));
                    self.buf.clear();
                    o.echo.push(b'\r');
                    last_term = Some(b);
                }
                _ => {
                    last_term = None;
                    if self.buf.len() >= CONSOLE_LINE_BUFFER_MAX {
                        self.buf.clear();
                        self.discarding = true;
                        self.overflow_started += 1;
                        continue;
                    }
                    self.buf.push(b);
                    if b == 0x09 || (0x20..=0x7E).contains(&b) || b >= 0x80 {
                        o.echo.push(b);
                    }
                }
            }
        }
        o
    }

    /// 取出並歸零「進入 discarding」次數；呼叫端據此寫 SC-16 溢位 WARNING（次數與 Python 相同，
    /// 同一 chunk 內兩行溢位即回 2）。
    pub fn take_overflow_started(&mut self) -> u32 {
        std::mem::take(&mut self.overflow_started)
    }

    pub fn buffered_len(&self) -> usize {
        self.buf.len()
    }
}

/// `_pop_last_console_char`：刪掉最後一個 UTF-8 字元；緩衝為空時回 false。
/// 與 Python 相同：往前跳過 continuation byte 但停在 index 0（只剩 continuation 時整段刪除）。
pub fn pop_last_utf8_char(buf: &mut Vec<u8>) -> bool {
    if buf.is_empty() {
        return false;
    }
    let mut idx = buf.len() - 1;
    while idx > 0 && (buf[idx] & 0xC0) == 0x80 {
        idx -= 1;
    }
    buf.truncate(idx);
    true
}
