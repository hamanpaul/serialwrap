//! 串接 reboot 詞法器逐列對照 Python 產生的 CPython golden。

use sw_proto::pycompat::{shlex_split_punctuation, ShlexError};
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
fn shlex_punctuation_matches_python_for_chained_reboot_cases() {
    let mut count = 0;
    for (line_number, line) in GOLDEN.lines().enumerate() {
        let fields: Vec<_> = line.split('\t').collect();
        assert_eq!(fields.len(), 4, "golden 第 {} 行欄位數", line_number + 1);
        let command = unhex_utf8(fields[1]);
        let expected_json = unhex_utf8(fields[2]);
        let expected = match loads(&expected_json).expect("token JSON") {
            PyValue::List(tokens) => tokens
                .into_iter()
                .map(|token| match token {
                    PyValue::Str(value) => value,
                    other => panic!("token 應為字串：{other:?}"),
                })
                .collect::<Vec<_>>(),
            PyValue::Dict(error) => {
                let message = error
                    .get("error")
                    .and_then(PyValue::as_str)
                    .expect("lexer error message");
                assert_eq!(
                    shlex_split_punctuation(&command),
                    Err(ShlexError {
                        message: message.to_string(),
                    }),
                    "{} command={command:?}",
                    fields[0]
                );
                count += 1;
                continue;
            }
            other => panic!("tokens 應為 JSON 陣列或錯誤物件：{other:?}"),
        };
        assert_eq!(
            shlex_split_punctuation(&command).expect("合法 Python lexer 輸入"),
            expected,
            "{} command={command:?}",
            fields[0]
        );
        count += 1;
    }
    assert_eq!(count, 40, "Python tokenizer golden 必須逐列固定為 40 案")
}
