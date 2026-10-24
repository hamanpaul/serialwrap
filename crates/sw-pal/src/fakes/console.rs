//! console 記憶體替身（P6 Task 1 骨架；本體由 Task 8 交付）：`FakeConsoleFactory`、`FakeIo`、`FakeConsoleRef`。
//! 替身不得冒充原生 gate（arch §11.2、OD-08）。

use std::collections::HashSet;

/// 記憶體 console 工廠（`ConsoleTransportFactory` 替身；Task 8 實作 trait）。
pub struct FakeConsoleFactory {
    _stub: (),
}

/// 可注入行為的假 console I/O（停滯、無上限、只收 n bytes、讀取失敗）。
pub struct FakeIo {
    _stub: (),
}

/// 測試端握有的 console 參照（讀寫出／注入輸入／觀察關閉）。
pub struct FakeConsoleRef {
    _stub: (),
}

impl FakeIo {
    /// `try_write` 永遠回 `Ok(0)`（慢速 console）。
    pub fn stalled() -> FakeIo {
        todo!("P6 Task 8")
    }
    /// `try_write` 全收。
    pub fn unbounded() -> FakeIo {
        todo!("P6 Task 8")
    }
    /// 總共只收 `n` bytes，之後回 `Ok(0)`。
    pub fn accept_n(_n: usize) -> FakeIo {
        todo!("P6 Task 8")
    }
    /// `read` 回錯誤（G-01：handler 例外時 console 續行）。
    pub fn failing_read() -> FakeIo {
        todo!("P6 Task 8")
    }
}

impl FakeConsoleRef {
    pub fn client_id(&self) -> String {
        todo!("P6 Task 8")
    }
    pub fn written(&self) -> Vec<u8> {
        todo!("P6 Task 8")
    }
    pub fn written_len(&self) -> usize {
        todo!("P6 Task 8")
    }
    pub fn closed(&self) -> bool {
        todo!("P6 Task 8")
    }
    pub fn push_input(&self, _b: &[u8]) {
        todo!("P6 Task 8")
    }
}

impl FakeConsoleFactory {
    /// 模擬一個 TCP client 連入 listener。
    pub fn connect(&self) -> FakeConsoleRef {
        todo!("P6 Task 8")
    }
    /// 下一次 `open_com0com_console` 失敗。
    pub fn fail_next_com0com(&self) {
        todo!("P6 Task 8")
    }
    /// `held_slave_paths` 的回傳值。
    pub fn set_held(&self, _held: Option<HashSet<String>>) {
        todo!("P6 Task 8")
    }
}
