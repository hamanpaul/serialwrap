//! argv 的可逆字串表示（P3-T14，Q-19）。
//!
//! 非 UTF-8 的 argv 位元組（Python 以 surrogateescape 保留）跳脫為 `ESCAPE_MARK` + 偏移字元（見 `sw_proto::pyfmt`）。
//! argv 不可能含 NUL，故跳脫標記無歧義；解析、插入 `Namespace`、組裝 RPC 參數都只看字串，
//! 路徑型值在 PAL 邊界以 [`decode`] 還原為原始 `OsString`，非路徑型值於送出前以 [`value_is_tainted`] 攔截。

use std::ffi::{OsStr, OsString};

use sw_pal::OsPiece;
use sw_proto::pyfmt::{escape_surrogate, surrogate_of_escape, ESCAPE_MARK};
use sw_proto::pyjson::PyValue;

/// 非 UTF-8 參數於 Python 會以未攔截的 UnicodeEncodeError 結束時的 stderr 訊息（Q-19）。
pub const NOT_UTF8_MESSAGE: &str = "argument is not valid UTF-8";

/// 單一 argv 片段 → 可逆字串（有效 UTF-8 原樣輸出）。
pub fn encode(token: &OsStr) -> String {
    let mut out = String::new();
    for piece in sw_pal::split_os_str(token) {
        match piece {
            OsPiece::Text(text) => out.push_str(&text),
            OsPiece::Lone(cp) => escape_surrogate(cp, &mut out),
        }
    }
    out
}

/// [`encode`] 的反向：還原為與原 argv 片段位元組相同的 `OsString`。
pub fn decode(text: &str) -> OsString {
    let mut pieces = Vec::new();
    let mut run = String::new();
    let mut chars = text.chars().peekable();
    while let Some(c) = chars.next() {
        if c == ESCAPE_MARK {
            if let Some(cp) = chars.peek().and_then(|next| surrogate_of_escape(*next)) {
                chars.next();
                if !run.is_empty() {
                    pieces.push(OsPiece::Text(std::mem::take(&mut run)));
                }
                pieces.push(OsPiece::Lone(cp));
                continue;
            }
        }
        run.push(c);
    }
    if !run.is_empty() {
        pieces.push(OsPiece::Text(run));
    }
    sw_pal::join_os_pieces(&pieces)
}

/// 字串是否含非 UTF-8 跳脫（即來源 argv 含無效位元組）。
pub fn has_escape(text: &str) -> bool {
    text.contains(ESCAPE_MARK)
}

/// 值樹是否含非 UTF-8 跳脫（Q-19 (b)：要寫進 RPC 請求者，送出前失敗）。
///
/// 只用於本地組裝的請求參數；daemon 回應是合法 JSON，其中的 NUL 屬於正常內容，不得檢查。
pub fn value_is_tainted(value: &PyValue) -> bool {
    match value {
        PyValue::Str(text) => has_escape(text),
        PyValue::List(items) => items.iter().any(value_is_tainted),
        PyValue::Dict(dict) => dict
            .0
            .iter()
            .any(|(key, item)| has_escape(key) || value_is_tainted(item)),
        _ => false,
    }
}
