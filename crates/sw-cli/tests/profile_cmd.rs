//! `profile test` 單元測試（P3-T15）。
use sw_cli::commands::profile_cmd::{bootloader_check, last_nonblank_line, profile_test_check};

fn j(d: &sw_proto::pyjson::PyDict) -> String {
    sw_proto::pyjson::dumps(&d.clone().into(), &sw_proto::pyjson::CLI_STDOUT)
}

#[test]
fn matched_line_is_full_line() {
    assert_eq!(
        j(&profile_test_check("ogin:", "boot\nfoo login: \nbar")),
        r#"{"error":null,"matched":true,"matched_line":"foo login: ","pattern":"ogin:"}"#
    );
}

#[test]
fn empty_pattern_not_matched() {
    assert_eq!(
        j(&profile_test_check("", "x")),
        r#"{"error":null,"matched":false,"matched_line":null,"pattern":""}"#
    );
}

#[test]
fn invalid_regex_reports_python_message() {
    assert_eq!(
        j(&profile_test_check("(", "x")),
        r#"{"error":"missing ), unterminated subpattern at position 0","matched":false,"matched_line":null,"pattern":"("}"#
    );
}

#[test]
fn bootloader_uses_last_nonblank_line() {
    assert_eq!(last_nonblank_line("a\n=> \n  \n"), Some("=> "));
    assert_eq!(
        j(&bootloader_check("^=> $", Some("=> "))),
        r#"{"error":null,"matched":true,"matched_line":"=> ","pattern":"^=> $"}"#
    );
    assert_eq!(
        j(&bootloader_check("^=> $", None)),
        r#"{"error":null,"matched":false,"matched_line":null,"pattern":"^=> $"}"#
    );
}
