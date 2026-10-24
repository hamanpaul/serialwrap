//! P6 Task 3：`shlex_split`（06 T0c 交付）對 CPython 3.12 `shlex.split(s, posix=True)` 的回歸 golden（≥500 列）。
//!
//! golden 以 `include_str!` 嵌入（Windows runner 不需 staging）；欄位為 P3 `_tsv.py` 的 hex(UTF-8)。
//! 06 的 `ShlexError` 為 `struct { message }`（Python `str(ValueError)`），本檔依該型別斷言（計畫寫成 enum 變體，見 PR 說明）。

use sw_proto::pycompat::{shlex_split, ShlexError};

const GOLDEN: &str = include_str!("../../../contracts/golden/shlex_split.tsv");

fn unhex_utf8(s: &str) -> String {
    let b: Vec<u8> = (0..s.len()).step_by(2).map(|i| u8::from_str_radix(&s[i..i + 2], 16).unwrap()).collect();
    String::from_utf8(b).expect("golden 應為合法 UTF-8")
}

fn render(r: Result<Vec<String>, ShlexError>) -> String {
    match r {
        Ok(tokens) => format!("OK:{}", tokens.join("\x1f")),
        Err(e) => format!("ERR:{}", e.message),
    }
}

#[test]
fn golden_matches_python() {
    let mut n = 0;
    for line in GOLDEN.split('\n').filter(|l| !l.is_empty()) {
        let cols: Vec<&str> = line.split('\t').collect();
        assert_eq!(cols.len(), 4);
        assert_eq!(cols[1], "shlex_split");
        let input = unhex_utf8(cols[2]);
        let want = unhex_utf8(cols[3]);
        assert_eq!(render(shlex_split(&input)), want, "case {} input={input:?}", cols[0]);
        n += 1;
    }
    assert!(n >= 500, "golden 案例數 {n}");
}

#[test]
fn handwritten_cases() {
    assert_eq!(shlex_split("a 'b c' \"d\\\"e\"").unwrap(), vec!["a", "b c", "d\"e"]);
    assert_eq!(shlex_split("x 'y"), Err(ShlexError { message: "No closing quotation".into() }));
    assert_eq!(shlex_split("x \\"), Err(ShlexError { message: "No escaped character".into() }));
}
