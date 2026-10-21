//! CLI 輸出契約測試（P3-T11）：format_err_line、print_json、warn_version_mismatch、mirror_err。

use sw_cli::ctx::TextIo;
use sw_cli::output::{
    format_err_line, mirror_err, print_internal_error, print_json, warn_version_mismatch,
};
use sw_proto::pydict;
use sw_proto::pyjson::PyValue;

// ==========================================
// 1. format_err_line 測試表
// ==========================================

#[test]
fn code_and_message() {
    let resp = pydict! {
        "ok" => false,
        "error_code" => "SOCKET_ERROR",
        "message" => "[Errno 2] No such file or directory",
    };
    assert_eq!(
        format_err_line(&resp, Some("session.list")),
        "serialwrap: session.list failed: SOCKET_ERROR: [Errno 2] No such file or directory"
    );
}

#[test]
fn message_only() {
    let resp = pydict! {
        "ok" => false,
        "message" => "m",
    };
    assert_eq!(format_err_line(&resp, None), "serialwrap: failed: m");
}

#[test]
fn nothing() {
    let resp = pydict! {
        "ok" => false,
    };
    assert_eq!(format_err_line(&resp, None), "serialwrap: failed: UNKNOWN_ERROR");
}

#[test]
fn hint_fullwidth() {
    let resp = pydict! {
        "ok" => false,
        "error_code" => "NEEDS_SUDO",
        "hint" => "h",
    };
    assert_eq!(
        format_err_line(&resp, Some("service start")),
        "serialwrap: service start failed: NEEDS_SUDO（hint: h）"
    );
}

#[test]
fn empty_strings_skipped() {
    let resp = pydict! {
        "ok" => false,
        "error_code" => "X",
        "message" => "",
        "hint" => "",
    };
    assert_eq!(format_err_line(&resp, Some("c")), "serialwrap: c failed: X");
}

#[test]
fn falsy_code_uses_message() {
    let resp = pydict! {
        "ok" => false,
        "error_code" => 0i64,
        "message" => "m",
    };
    assert_eq!(format_err_line(&resp, Some("c")), "serialwrap: c failed: m");
}

#[test]
fn int_code_str() {
    let resp = pydict! {
        "ok" => false,
        "error_code" => 5i64,
    };
    assert_eq!(format_err_line(&resp, Some("c")), "serialwrap: c failed: 5");
}

#[test]
fn list_message_repr() {
    let resp = pydict! {
        "ok" => false,
        "error_code" => "E",
        "message" => vec![PyValue::from("a"), PyValue::from(1.0f64)],
    };
    assert_eq!(
        format_err_line(&resp, Some("c")),
        "serialwrap: c failed: E: ['a', 1.0]"
    );
}

#[test]
fn bool_hint() {
    let resp = pydict! {
        "ok" => false,
        "error_code" => "E",
        "hint" => true,
    };
    assert_eq!(
        format_err_line(&resp, Some("c")),
        "serialwrap: c failed: E（hint: True）"
    );
}

// ==========================================
// 2. print_json 測試表
// ==========================================

#[test]
fn sorted_nested() {
    let mut out = Vec::new();
    let mut err = Vec::new();
    let mut io = TextIo::new(&mut out, &mut err, "\n");
    let inner = pydict! {
        "d" => 1.0f64,
        "c" => "é",
    };
    let val = pydict! {
        "b" => 1i64,
        "a" => inner,
    };
    print_json(&mut io, &val).unwrap();
    assert_eq!(out, b"{\"a\":{\"c\":\"\xc3\xa9\",\"d\":1.0},\"b\":1}\n");
}

#[test]
fn crlf() {
    let mut out = Vec::new();
    let mut err = Vec::new();
    let mut io = TextIo::new(&mut out, &mut err, "\r\n");
    let val = pydict! {
        "ok" => true,
    };
    print_json(&mut io, &val).unwrap();
    assert_eq!(out, b"{\"ok\":true}\r\n");
}

#[test]
fn nan_inf() {
    let mut out = Vec::new();
    let mut err = Vec::new();
    let mut io = TextIo::new(&mut out, &mut err, "\n");
    let val = pydict! {
        "x" => f64::NAN,
        "y" => f64::NEG_INFINITY,
    };
    print_json(&mut io, &val).unwrap();
    assert_eq!(out, b"{\"x\":NaN,\"y\":-Infinity}\n");
}

#[test]
fn embedded_newline_escaped() {
    let mut out = Vec::new();
    let mut err = Vec::new();
    let mut io = TextIo::new(&mut out, &mut err, "\r\n");
    let val = pydict! {
        "m" => "a\nb",
    };
    print_json(&mut io, &val).unwrap();
    assert_eq!(out, b"{\"m\":\"a\\nb\"}\r\n");
}

// ==========================================
// 3. warn_version_mismatch 測試表
// ==========================================

#[test]
fn warn_version_mismatch_different() {
    let mut out = Vec::new();
    let mut err = Vec::new();
    let mut io = TextIo::new(&mut out, &mut err, "\n");
    let resp = pydict! {
        "version" => "9.9.9",
    };
    warn_version_mismatch(&mut io, &resp, "0.3.2").unwrap();
    let err_str = String::from_utf8(err).unwrap();
    assert_eq!(
        err_str,
        "serialwrap: client 版本 0.3.2 與 daemon 版本 9.9.9 不一致（建議統一安裝來源或重啟 daemon 對齊版本；serialwrap doctor 可查其他安裝）\n"
    );
}

#[test]
fn warn_version_mismatch_same() {
    let mut out = Vec::new();
    let mut err = Vec::new();
    let mut io = TextIo::new(&mut out, &mut err, "\n");
    let resp = pydict! {
        "version" => "0.3.2",
    };
    warn_version_mismatch(&mut io, &resp, "0.3.2").unwrap();
    assert!(err.is_empty());
}

#[test]
fn warn_version_mismatch_whitespace_or_missing() {
    for resp in [
        pydict! { "version" => " " },
        pydict! { "version" => 5i64 },
        pydict! {},
    ] {
        let mut out = Vec::new();
        let mut err = Vec::new();
        let mut io = TextIo::new(&mut out, &mut err, "\n");
        warn_version_mismatch(&mut io, &resp, "0.3.2").unwrap();
        assert!(err.is_empty());
    }
}

#[test]
fn warn_version_mismatch_unknown_cli() {
    let mut out = Vec::new();
    let mut err = Vec::new();
    let mut io = TextIo::new(&mut out, &mut err, "\n");
    let resp = pydict! {
        "version" => "9.9.9",
    };
    warn_version_mismatch(&mut io, &resp, "unknown").unwrap();
    assert!(err.is_empty());
}

// ==========================================
// 4. mirror_err 與 print_internal_error
// ==========================================

#[test]
fn mirror_err_on_ok_false() {
    let mut out = Vec::new();
    let mut err = Vec::new();
    let mut io = TextIo::new(&mut out, &mut err, "\n");
    let resp = pydict! {
        "ok" => false,
        "error_code" => "TEST_ERROR",
        "message" => "msg",
    };
    mirror_err(&mut io, &resp, Some("test.ctx")).unwrap();
    let err_str = String::from_utf8(err).unwrap();
    assert_eq!(err_str, "serialwrap: test.ctx failed: TEST_ERROR: msg\n");
}

#[test]
fn mirror_err_on_ok_true() {
    let mut out = Vec::new();
    let mut err = Vec::new();
    let mut io = TextIo::new(&mut out, &mut err, "\n");
    let resp = pydict! {
        "ok" => true,
    };
    mirror_err(&mut io, &resp, Some("test.ctx")).unwrap();
    assert!(err.is_empty());
}

#[test]
fn print_internal_error_behavior() {
    let mut out = Vec::new();
    let mut err = Vec::new();
    let mut io = TextIo::new(&mut out, &mut err, "\n");
    let rc = print_internal_error(&mut io, "test_ctx", "boom");
    assert_eq!(rc, 1);
    let out_str = String::from_utf8(out).unwrap();
    assert!(out_str.contains("\"error_code\":\"INTERNAL_ERROR\""));
    assert!(out_str.contains("\"message\":\"boom\""));
    assert!(out_str.contains("\"ok\":false"));
    let err_str = String::from_utf8(err).unwrap();
    assert_eq!(err_str, "serialwrap: test_ctx failed: INTERNAL_ERROR: boom\n");
}
