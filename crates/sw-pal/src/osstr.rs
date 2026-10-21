//! OS 字串與 Python surrogateescape 語意的平台橋接（P3-T14，Q-19）。
//!
//! 平台相依轉換只在本 crate 進行；上層（`sw-cli`）決定如何把無效單位跳脫成文字。
//! 無效單位以 Python 的 lone surrogate 碼位表示：
//! - POSIX：位元組 `b` 對應 `0xDC00 + b`（`os.fsdecode` 的 surrogateescape）。
//! - Windows：未配對 UTF-16 單位本身（`surrogatepass` 語意）。

use std::ffi::{OsStr, OsString};

/// 依目前平台將位元組轉成 OS 字串；POSIX 保留非 UTF-8 位元組。
#[cfg(unix)]
pub fn os_string_from_bytes(bytes: &[u8]) -> OsString {
    use std::os::unix::ffi::OsStringExt;

    OsString::from_vec(bytes.to_vec())
}

/// 依目前平台將位元組轉成 OS 字串；Windows 子行程輸出以 UTF-8 替代字元解碼。
#[cfg(windows)]
pub fn os_string_from_bytes(bytes: &[u8]) -> OsString {
    OsString::from(String::from_utf8_lossy(bytes).into_owned())
}

/// OS 字串的一段：有效文字，或一個無效單位（lone surrogate 碼位，`0xD800..=0xDFFF`）。
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum OsPiece {
    Text(String),
    Lone(u32),
}

/// 把 OS 字串拆成有效文字段與無效單位；有效 OS 字串只回傳單一 `Text`（或空）。
#[cfg(unix)]
pub fn split_os_str(s: &OsStr) -> Vec<OsPiece> {
    use std::os::unix::ffi::OsStrExt;

    let mut pieces = Vec::new();
    for chunk in s.as_bytes().utf8_chunks() {
        let valid = chunk.valid();
        if !valid.is_empty() {
            pieces.push(OsPiece::Text(valid.to_string()));
        }
        for &byte in chunk.invalid() {
            pieces.push(OsPiece::Lone(0xDC00 + u32::from(byte)));
        }
    }
    pieces
}

/// [`split_os_str`] 的反向：`Lone(cp)` 還原為原始位元組（取低 8 位）。
#[cfg(unix)]
pub fn join_os_pieces(pieces: &[OsPiece]) -> OsString {
    use std::os::unix::ffi::OsStringExt;

    let mut bytes = Vec::new();
    for piece in pieces {
        match piece {
            OsPiece::Text(text) => bytes.extend_from_slice(text.as_bytes()),
            OsPiece::Lone(cp) => bytes.push((cp & 0xFF) as u8),
        }
    }
    OsString::from_vec(bytes)
}

/// 把 OS 字串拆成有效文字段與未配對 UTF-16 單位（見模組說明）。
#[cfg(windows)]
pub fn split_os_str(s: &OsStr) -> Vec<OsPiece> {
    use std::os::windows::ffi::OsStrExt;

    let units: Vec<u16> = s.encode_wide().collect();
    let mut pieces = Vec::new();
    let mut text = String::new();
    for item in char::decode_utf16(units.iter().copied()) {
        match item {
            Ok(c) => text.push(c),
            Err(error) => {
                if !text.is_empty() {
                    pieces.push(OsPiece::Text(std::mem::take(&mut text)));
                }
                pieces.push(OsPiece::Lone(u32::from(error.unpaired_surrogate())));
            }
        }
    }
    if !text.is_empty() {
        pieces.push(OsPiece::Text(text));
    }
    pieces
}

/// [`split_os_str`] 的反向：`Lone(cp)` 還原為原始 UTF-16 單位。
#[cfg(windows)]
pub fn join_os_pieces(pieces: &[OsPiece]) -> OsString {
    use std::os::windows::ffi::OsStringExt;

    let mut units: Vec<u16> = Vec::new();
    for piece in pieces {
        match piece {
            OsPiece::Text(text) => units.extend(text.encode_utf16()),
            OsPiece::Lone(cp) => units.push((cp & 0xFFFF) as u16),
        }
    }
    OsString::from_wide(&units)
}

#[cfg(all(test, unix))]
mod tests {
    use super::os_string_from_bytes;
    use std::os::unix::ffi::OsStrExt;

    #[test]
    fn os_string_from_bytes_preserves_invalid_posix_bytes() {
        assert_eq!(
            os_string_from_bytes(&[0xff, 0xfe]).as_bytes(),
            &[0xff, 0xfe]
        );
    }
}
