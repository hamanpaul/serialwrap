//! OS 字串橋接（`sw_pal::split_os_str`／`join_os_pieces`）的往返與無效單位測試（P3-T14，Q-19）。

use std::ffi::OsString;

use sw_pal::{join_os_pieces, split_os_str, OsPiece};

#[cfg(unix)]
#[test]
fn valid_utf8_is_single_text_piece() {
    let pieces = split_os_str(std::ffi::OsStr::new("abc中"));
    assert_eq!(pieces, vec![OsPiece::Text("abc中".to_string())]);
}

#[cfg(unix)]
#[test]
fn invalid_bytes_become_surrogateescape_code_points() {
    use std::os::unix::ffi::OsStringExt;

    let raw = OsString::from_vec(b"a\xffb\xe9\x80".to_vec());
    let pieces = split_os_str(&raw);
    // 每個無效位元組各自成為 `0xDC00 + byte`（surrogateescape），有效段保持原序。
    assert_eq!(
        pieces,
        vec![
            OsPiece::Text("a".to_string()),
            OsPiece::Lone(0xDCFF),
            OsPiece::Text("b".to_string()),
            OsPiece::Lone(0xDCE9),
            OsPiece::Lone(0xDC80),
        ]
    );
}

#[cfg(unix)]
#[test]
fn join_restores_original_bytes_exactly() {
    use std::os::unix::ffi::OsStringExt;

    for bytes in [
        b"plain".to_vec(),
        b"a\xffb".to_vec(),
        b"\xe9\x80\x80".to_vec(),
        b"\xe4\xb8\xadx\xff\xe6\x96\x87".to_vec(),
    ] {
        let raw = OsString::from_vec(bytes.clone());
        let pieces = split_os_str(&raw);
        assert_eq!(join_os_pieces(&pieces).into_encoded_bytes(), bytes);
    }
}

#[test]
fn join_of_text_only_matches_utf8() {
    let pieces = vec![OsPiece::Text("路徑".to_string())];
    assert_eq!(join_os_pieces(&pieces), OsString::from("路徑"));
}
