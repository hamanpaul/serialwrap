//! argv 可逆字串表示與非 UTF-8 攔截輔助（P3-T14，Q-19）。
//!
//! 本檔不含平台 cfg（cfg 邊界由 `scripts/rust/check_cfg_boundary.py` 強制）；原始位元組往返由 `sw-pal` 的 `osstr` 測試負責。

use std::ffi::OsStr;

use sw_cli::argv::{decode, encode, has_escape, value_is_tainted};
use sw_proto::pyfmt::{escape_surrogate, py_display_argv, py_repr_argv, py_repr_str};
use sw_proto::pyjson::{PyDict, PyValue};

fn escaped(prefix: &str, cp: u32, suffix: &str) -> String {
    let mut text = String::from(prefix);
    escape_surrogate(cp, &mut text);
    text.push_str(suffix);
    text
}

#[test]
fn encode_decode_round_trip_preserves_escapes() {
    let text = escaped("a", 0xDCFF, "b");
    assert_eq!(encode(&decode(&text)), text);
    let plain = "--selector=COM0";
    assert_eq!(encode(&decode(plain)), plain);
}

#[test]
fn valid_argv_is_unchanged_and_untainted() {
    let text = encode(OsStr::new("--selector=COM0"));
    assert_eq!(text, "--selector=COM0");
    assert!(!has_escape(&text));
    assert!(!value_is_tainted(&PyValue::Str(text)));
}

#[test]
fn invalid_argv_is_tainted_wherever_it_sits() {
    let text = escaped("--cmd=echo ", 0xDCE9, "");
    assert!(has_escape(&text));
    assert!(value_is_tainted(&PyValue::Str(text.clone())));
    let list = PyValue::List(vec![PyValue::Str("ok".into()), PyValue::Str(text)]);
    assert!(value_is_tainted(&list));
    let mut dict = PyDict::new();
    dict.insert("cmd", PyValue::Str(escaped("x", 0xDCFF, "")));
    assert!(value_is_tainted(&PyValue::Dict(dict)));
}

#[test]
fn repr_renders_escapes_as_python_surrogate_text() {
    let text = escaped("unix://rel", 0xDCFF, "");
    assert_eq!(py_repr_argv(&text), "'unix://rel\\udcff'");
}

#[test]
fn repr_matches_plain_repr_without_escapes() {
    for text in ["plain", "it's", "tab\tnew\nline", "中文", "a\\b"] {
        assert_eq!(py_repr_argv(text), py_repr_str(text));
    }
}

#[test]
fn display_renders_escapes_as_backslashreplace_text() {
    let text = escaped("x", 0xDCFF, "y");
    assert_eq!(py_display_argv(&text), "x\\udcffy");
}
