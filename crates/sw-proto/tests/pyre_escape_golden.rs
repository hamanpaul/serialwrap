//! P7 Task 3 上游補件（P3 缺口）：`sw_proto::pyre::escape` 對 CPython 3.12 `re.escape(str)` 的回歸 golden。
//!
//! golden 以 `include_str!` 嵌入；欄位為 P3 `_tsv.py` 的 hex(UTF-8)。

use sw_proto::pyre::escape;

const GOLDEN: &str = include_str!("../../../contracts/golden/pyre_escape.tsv");

fn unhex_utf8(s: &str) -> String {
    let b: Vec<u8> = (0..s.len()).step_by(2).map(|i| u8::from_str_radix(&s[i..i + 2], 16).unwrap()).collect();
    String::from_utf8(b).expect("golden 應為合法 UTF-8")
}

#[test]
fn golden_matches_python() {
    let mut n = 0;
    for line in GOLDEN.split('\n').filter(|l| !l.is_empty()) {
        let cols: Vec<&str> = line.split('\t').collect();
        assert_eq!(cols.len(), 4);
        assert_eq!(cols[1], "re_escape");
        let input = unhex_utf8(cols[2]);
        let want = unhex_utf8(cols[3]);
        assert_eq!(escape(&input), want, "case {} input={input:?}", cols[0]);
        n += 1;
    }
    assert!(n >= 400, "golden 案例數 {n}");
}

#[test]
fn handwritten_cases() {
    assert_eq!(escape(""), "");
    assert_eq!(escape("__READY__ab12"), "__READY__ab12");
    assert_eq!(escape("a.b*c"), "a\\.b\\*c");
    assert_eq!(escape("\x00"), "\x00", "3.7+ 不跳脫 NUL");
    assert_eq!(escape("中 文"), "中\\ 文", "非 ASCII 原樣、空白跳脫");
    assert_eq!(escape("\t\n\r\x0b\x0c"), "\\\t\\\n\\\r\\\x0b\\\x0c");
}
