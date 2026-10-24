//! Telnet 協定層（P6 Task 2；逐字移植 `sw_core/telnet_console.py:63-139`，06 §8.4 狀態表）。
//!
//! 每 client 一個跨 chunk 狀態機：入向 NVT 摺疊（CR NUL／CR LF → CR）、`IAC IAC` 還原、選項協商回覆、
//! 吞掉 SB 子協商與其他命令；出向只做 IAC 逸出（宣告 WILL BINARY，不做 LF→CRLF）。
//! Task 1 骨架：型別與簽章凍結，函式本體為 `todo!("P6 Task 2")`。

use std::borrow::Cow;

pub use sw_proto::constants::telnet::TELNET_GREETING;

/// 一次 `feed` 的結果：`uart` 為要送往 DUT 的資料、`reply` 為要回給 client 的協商回覆。
#[derive(Debug, Default, PartialEq, Eq, Clone)]
pub struct TelnetFeed {
    pub uart: Vec<u8>,
    pub reply: Vec<u8>,
}

/// 入向 parser 狀態（06 §8.4）。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
#[allow(dead_code)]
enum St {
    Data,
    DataCr,
    Iac,
    OptWill,
    OptWont,
    OptDo,
    OptDont,
    Sb,
    SbIac,
}

/// 每 client 一個的 Telnet 入向 filter。
#[derive(Debug, Clone)]
pub struct TelnetFilter {
    #[allow(dead_code)]
    st: St,
    #[allow(dead_code)]
    local_on: [bool; 256],
}

impl TelnetFilter {
    /// `local_on` 初值為 {ECHO, SGA, BINARY}（greeting 已宣告 WILL）。
    pub fn new() -> Self {
        todo!("P6 Task 2")
    }

    /// 餵入一段 bytes；狀態跨呼叫保留。
    pub fn feed(&mut self, _data: &[u8]) -> TelnetFeed {
        todo!("P6 Task 2")
    }
}

impl Default for TelnetFilter {
    fn default() -> Self {
        Self::new()
    }
}

/// 出向 IAC 逸出：`0xFF` → `0xFF 0xFF`；不含 `0xFF` 時回 `Cow::Borrowed`（同一物件）。
pub fn escape_iac(_payload: &[u8]) -> Cow<'_, [u8]> {
    todo!("P6 Task 2")
}
