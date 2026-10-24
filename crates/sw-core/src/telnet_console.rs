//! Telnet 協定層（P6 Task 2；逐字移植 `sw_core/telnet_console.py:63-139`，06 §8.4 狀態表）。
//!
//! 每 client 一個跨 chunk 狀態機：入向 NVT 摺疊（CR NUL／CR LF → CR）、`IAC IAC` 還原、選項協商回覆、
//! 吞掉 SB 子協商與其他命令；出向只做 IAC 逸出（宣告 WILL BINARY，不做 LF→CRLF）。

use std::borrow::Cow;

use sw_proto::constants::telnet::{DO, DONT, IAC, OPT_BINARY, OPT_ECHO, OPT_SGA, SB, SE, WILL, WONT};
pub use sw_proto::constants::telnet::TELNET_GREETING;

/// 一次 `feed` 的結果：`uart` 為要送往 DUT 的資料、`reply` 為要回給 client 的協商回覆。
#[derive(Debug, Default, PartialEq, Eq, Clone)]
pub struct TelnetFeed {
    pub uart: Vec<u8>,
    pub reply: Vec<u8>,
}

/// 入向 parser 狀態（06 §8.4）。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum St {
    Data,
    /// 剛 emit CR：下一個 NUL／LF 吞掉一次。
    DataCr,
    Iac,
    OptWill,
    OptWont,
    OptDo,
    OptDont,
    Sb,
    /// 子協商中收到 IAC：SE 結束、其他（含 IAC IAC 逸出）續吞。
    SbIac,
}

/// 每 client 一個的 Telnet 入向 filter。
#[derive(Debug, Clone)]
pub struct TelnetFilter {
    st: St,
    /// 我方已宣告（greeting WILL）且仍生效的 local options。
    local_on: [bool; 256],
}

impl TelnetFilter {
    /// `local_on` 初值為 {ECHO, SGA, BINARY}（greeting 已宣告 WILL）。
    pub fn new() -> Self {
        let mut local_on = [false; 256];
        for opt in [OPT_ECHO, OPT_SGA, OPT_BINARY] {
            local_on[opt as usize] = true;
        }
        TelnetFilter { st: St::Data, local_on }
    }

    /// 餵入一段 bytes；狀態跨呼叫保留。
    pub fn feed(&mut self, data: &[u8]) -> TelnetFeed {
        let mut out = TelnetFeed::default();
        for &b in data {
            match self.st {
                St::Data | St::DataCr => {
                    if self.st == St::DataCr {
                        self.st = St::Data;
                        if b == 0x00 || b == 0x0A {
                            continue; // CR NUL／CR LF：CR 已即時 emit，吞後綴一次
                        }
                    }
                    if b == IAC {
                        self.st = St::Iac;
                    } else if b == 0x0D {
                        out.uart.push(0x0D); // CR 立即 emit（不押住鍵擊等下一 byte）
                        self.st = St::DataCr;
                    } else {
                        out.uart.push(b);
                    }
                }
                St::Iac => {
                    self.st = match b {
                        IAC => {
                            out.uart.push(0xFF); // IAC IAC → 資料 0xFF
                            St::Data
                        }
                        WILL => St::OptWill,
                        WONT => St::OptWont,
                        DO => St::OptDo,
                        DONT => St::OptDont,
                        SB => St::Sb,
                        _ => St::Data, // SE／NOP／DM／BRK／IP／AO／AYT／EC／EL／GA 一律吞
                    }
                }
                St::OptWill => {
                    // WILL SGA 為我方 DO SGA 的 ack → 靜默；其餘（含 WILL BINARY，入向刻意留 NVT）一律 DONT。
                    if b != OPT_SGA {
                        out.reply.extend_from_slice(&[IAC, DONT, b]);
                    }
                    self.st = St::Data;
                }
                St::OptDo => {
                    if !self.local_on[b as usize] {
                        out.reply.extend_from_slice(&[IAC, WONT, b]);
                    }
                    self.st = St::Data;
                }
                St::OptDont => {
                    // 狀態變更才回（防乒乓）：WONT 一次並標 off。
                    if self.local_on[b as usize] {
                        self.local_on[b as usize] = false;
                        out.reply.extend_from_slice(&[IAC, WONT, b]);
                    }
                    self.st = St::Data;
                }
                St::OptWont => self.st = St::Data, // 不回（我方 DONT 的 ack 或 client 自行撤回）
                St::Sb => {
                    if b == IAC {
                        self.st = St::SbIac;
                    }
                }
                St::SbIac => self.st = if b == SE { St::Data } else { St::Sb },
            }
        }
        out
    }
}

impl Default for TelnetFilter {
    fn default() -> Self {
        Self::new()
    }
}

/// 出向 IAC 逸出：`0xFF` → `0xFF 0xFF`；不含 `0xFF` 時回 `Cow::Borrowed`（同一物件）。
pub fn escape_iac(payload: &[u8]) -> Cow<'_, [u8]> {
    if !payload.contains(&0xFF) {
        return Cow::Borrowed(payload);
    }
    let extra = payload.iter().filter(|&&b| b == 0xFF).count();
    let mut v = Vec::with_capacity(payload.len() + extra);
    for &b in payload {
        v.push(b);
        if b == 0xFF {
            v.push(0xFF);
        }
    }
    Cow::Owned(v)
}
