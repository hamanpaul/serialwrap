//! 第二 console 起的 line-buffer 編輯（P6 Task 3；`uart_io.py:712-750` `_pop_last_console_char`／`_consume_console_input`＋SC-16）。
//!
//! 本地回顯、backspace 刪一個 UTF-8 字元、CR／LF 摺疊（每次呼叫重設，UIO-12.10-a）、16385 bytes 行上限（07 D-06）。
//! Task 1 骨架：型別與簽章凍結，函式本體為 `todo!("P6 Task 3")`。

/// 每個 line-mode console 的半行緩衝（07 D-21：BC-02 失效時不清除）。
#[derive(Debug, Default, Clone)]
pub struct LineEditor {
    #[allow(dead_code)]
    buf: Vec<u8>,
    #[allow(dead_code)]
    discarding: bool,
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
        todo!("P6 Task 3")
    }

    /// `_consume_console_input`（`uart_io.py:721-750`）＋SC-16。
    pub fn consume(&mut self, _data: &[u8]) -> LineOutput {
        todo!("P6 Task 3")
    }

    pub fn buffered_len(&self) -> usize {
        todo!("P6 Task 3")
    }
}

/// `_pop_last_console_char`（`uart_io.py:712-719`）：刪掉最後一個 UTF-8 字元；緩衝為空時回 false。
pub fn pop_last_utf8_char(_buf: &mut Vec<u8>) -> bool {
    todo!("P6 Task 3")
}
