//! 串接 reboot warning 判定逐列對照 Python 產生的 golden。

use sw_core::quiet_gate::find_chained_reboot_segment;
use sw_proto::pyjson::{loads, PyValue};

const GOLDEN: &str = include_str!("../../../contracts/golden/expect_reboot_chained.tsv");

fn unhex_utf8(value: &str) -> String {
    let bytes: Vec<u8> = value
        .as_bytes()
        .chunks_exact(2)
        .map(|pair| {
            let hi = (pair[0] as char).to_digit(16).expect("hex digit");
            let lo = (pair[1] as char).to_digit(16).expect("hex digit");
            ((hi << 4) | lo) as u8
        })
        .collect();
    String::from_utf8(bytes).expect("Python golden 必須是 UTF-8")
}

#[test]
fn chained_reboot_segment_matches_python_for_40_commands() {
    let mut count = 0;
    for (line_number, line) in GOLDEN.lines().enumerate() {
        let fields: Vec<_> = line.split('\t').collect();
        assert_eq!(fields.len(), 4, "golden 第 {} 行欄位數", line_number + 1);
        let command = unhex_utf8(fields[1]);
        let expected_json = unhex_utf8(fields[3]);
        let expected = loads(&expected_json).expect("segment JSON");
        let actual = find_chained_reboot_segment(&command)
            .map(PyValue::from)
            .unwrap_or(PyValue::Null);
        assert_eq!(actual, expected, "{} command={command:?}", fields[0]);
        count += 1;
    }
    assert_eq!(count, 40, "find_chained_reboot_segment golden 必須有 40 案")
}
