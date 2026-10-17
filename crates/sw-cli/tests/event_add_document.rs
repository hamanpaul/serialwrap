//! `event add --file` 與 argv 非 UTF-8 攔截的回歸測試（P3-T15 審查修正）。
//!
//! - 檔案內容（JSON）的 NUL 等碼位是合法資料，必須原樣送上 wire（`ParamOrigin::Document`）。
//! - argv 衍生參數的非 UTF-8 原文仍須於送出前攔截（Q-19 (b)），錯誤文字與 CPython `UnicodeEncodeError` 相同（含區間）。
//! - stdout 寫入失敗時走 Python 的 `except Exception` 路徑（INTERNAL_ERROR、略過版本警告與錯誤行）。
//!
//! 期望值取自 Python 3.12.3 基線（`sw_core/cli.py:_dispatch_event`）的實測輸出。

use std::ffi::OsString;
use std::io::{self, Write};
use std::path::PathBuf;
use std::sync::Arc;

use sw_pal::fakes::{
    non_utf8_os_string, FakeClock, FakeConn, FakeEnv, MemFs, RecordingProcess, ScriptedConnector,
    StreamStep,
};
use sw_pal::{
    BackendKind, ClientCaps, ClientPlatform, Clock, PalPathFlavor, RpcConnector, SkillPlatform,
};

const SOCKET_ARG: &str = "--socket=/s.sock";

fn platform(
    files: &[(&str, Vec<u8>)],
    script: Vec<FakeConn>,
) -> (ClientPlatform, Arc<ScriptedConnector>) {
    let fs = MemFs::default();
    {
        let mut map = fs.files.lock().unwrap_or_else(|e| e.into_inner());
        for (path, bytes) in files {
            map.insert(PathBuf::from(path), Ok(bytes.clone()));
        }
    }
    let clock = Arc::new(FakeClock::default());
    let conn = Arc::new(ScriptedConnector::new(script, Arc::clone(&clock), true));
    let p = ClientPlatform {
        env: Arc::new(FakeEnv::default()),
        fs: Arc::new(fs),
        clock: Arc::clone(&clock) as Arc<dyn Clock>,
        rpc: Arc::clone(&conn) as Arc<dyn RpcConnector>,
        process: Arc::new(RecordingProcess::default()),
        caps: ClientCaps {
            path_flavor: PalPathFlavor::Posix,
            af_unix: true,
            native_backend: BackendKind::Posix,
            default_skill: SkillPlatform::Linux,
            text_newline: "\n",
            exec_replace: true,
        },
    };
    (p, conn)
}

fn ok_reply() -> Vec<FakeConn> {
    vec![FakeConn::stream(vec![StreamStep::Reply(
        b"{\"ok\":true}\n".to_vec(),
    )])]
}

fn run_cli(args: Vec<OsString>, p: &ClientPlatform) -> (i32, String, String) {
    let mut argv = vec![OsString::from("serialwrap")];
    argv.extend(args);
    let mut out = Vec::new();
    let mut err = Vec::new();
    let rc = sw_cli::run(argv, p, &mut out, &mut err);
    (
        rc,
        String::from_utf8(out).unwrap(),
        String::from_utf8(err).unwrap(),
    )
}

fn args(parts: &[&str]) -> Vec<OsString> {
    let mut out = vec![OsString::from(SOCKET_ARG)];
    out.extend(parts.iter().map(OsString::from));
    out
}

fn sent_text(conn: &ScriptedConnector) -> Vec<String> {
    conn.sent()
        .into_iter()
        .map(|bytes| String::from_utf8(bytes).unwrap())
        .collect()
}

#[test]
fn add_keeps_nul_in_key_and_value_on_the_wire() {
    let (p, conn) = platform(
        &[("/f.json", br#"{"k\u0000":"v\u0000"}"#.to_vec())],
        ok_reply(),
    );
    let (rc, out, err) = run_cli(args(&["event", "add", "--file", "/f.json"]), &p);
    assert_eq!(rc, 0, "stderr={err}");
    assert_eq!(
        sent_text(&conn),
        vec![
            r#"{"id":1,"method":"event.rule_set","params":{"k\u0000":"v\u0000"}}"#.to_string()
                + "\n"
        ]
    );
    assert_eq!(out, "{\"ok\":true}\n");
}

#[test]
fn add_keeps_key_order_nan_and_non_ascii_on_the_wire() {
    let (p, conn) = platform(
        &[(
            "/f.json",
            "{\"b\":1,\"a\":[1.5,NaN],\"z\":\"é\"}".as_bytes().to_vec(),
        )],
        ok_reply(),
    );
    let (rc, _, err) = run_cli(args(&["event", "add", "--file", "/f.json"]), &p);
    assert_eq!(rc, 0, "stderr={err}");
    assert_eq!(
        sent_text(&conn),
        vec![
            "{\"id\":1,\"method\":\"event.rule_set\",\"params\":{\"b\":1,\"a\":[1.5,NaN],\"z\":\"é\"}}\n"
                .to_string()
        ]
    );
}

#[test]
fn add_sends_non_object_document_unchanged() {
    let (p, conn) = platform(&[("/f.json", b"[1,2]".to_vec())], ok_reply());
    let (rc, _, _) = run_cli(args(&["event", "add", "--file", "/f.json"]), &p);
    assert_eq!(rc, 0);
    assert_eq!(
        sent_text(&conn),
        vec!["{\"id\":1,\"method\":\"event.rule_set\",\"params\":[1,2]}\n".to_string()]
    );
}

#[test]
fn add_integer_digit_limit_matches_python_boundary() {
    // CPython 3.12 預設上限 4300 位：4300 位照送，4301 位於送出前以 INTERNAL_ERROR 結束。
    let ok_doc = format!("{{\"a\":{}}}", "1".repeat(4300));
    let (p, conn) = platform(&[("/ok.json", ok_doc.into_bytes())], ok_reply());
    let (rc, _, err) = run_cli(args(&["event", "add", "--file", "/ok.json"]), &p);
    assert_eq!(rc, 0, "stderr={err}");
    assert_eq!(sent_text(&conn).len(), 1);

    let too_long = format!("{{\"a\":{}}}", "1".repeat(4301));
    let (p, conn) = platform(&[("/big.json", too_long.into_bytes())], vec![]);
    let (rc, out, err) = run_cli(args(&["event", "add", "--file", "/big.json"]), &p);
    assert_eq!(rc, 1);
    assert!(conn.sent().is_empty());
    assert_eq!(
        out,
        "{\"error_code\":\"INTERNAL_ERROR\",\"message\":\"Exceeds the limit (4300 digits) for integer string conversion: value has 4301 digits; use sys.set_int_max_str_digits() to increase the limit\",\"ok\":false}\n"
    );
    assert!(err.starts_with(
        "serialwrap: event.rule_set failed: INTERNAL_ERROR: Exceeds the limit (4300 digits)"
    ));
}

#[test]
fn add_document_errors_match_python_text_and_send_nothing() {
    let cases: Vec<(&str, Vec<u8>, &str)> = vec![
        (
            "bom",
            b"\xef\xbb\xbf{\"a\":1}".to_vec(),
            "Unexpected UTF-8 BOM (decode using utf-8-sig): line 1 column 1 (char 0)",
        ),
        (
            "badutf",
            b"{\"a\":\xff}".to_vec(),
            "'utf-8' codec can't decode byte 0xff in position 5: invalid start byte",
        ),
        // CRLF 文字模式（universal newlines）後的位置：第 2 行第 3 欄。
        (
            "crlf",
            b"[1,\r\n2,]\r\n".to_vec(),
            "Expecting value: line 2 column 3 (char 6)",
        ),
    ];
    for (name, bytes, message) in cases {
        let path = format!("/{name}.json");
        let (p, conn) = platform(&[(path.as_str(), bytes)], vec![]);
        let (rc, out, err) = run_cli(args(&["event", "add", "--file", &path]), &p);
        assert_eq!(rc, 1, "case {name}");
        assert!(conn.sent().is_empty(), "case {name}");
        assert_eq!(
            out,
            format!(
                "{{\"error_code\":\"INTERNAL_ERROR\",\"message\":\"{}\",\"ok\":false}}\n",
                message
            ),
            "case {name}"
        );
        assert_eq!(
            err,
            format!("serialwrap: event.rule_set failed: INTERNAL_ERROR: {message}\n"),
            "case {name}"
        );
    }
}

#[test]
fn argv_non_utf8_is_rejected_before_send_with_python_range_text() {
    // `$'\xff\xfe'`：連續兩個 surrogate → 區間 58-59（Python 實測）。
    let (p, conn) = platform(&[], vec![FakeConn::stream(vec![])]);
    let token = non_utf8_os_string(&[0xff, 0xfe]);
    let (rc, out, _) = run_cli(
        vec![
            OsString::from(SOCKET_ARG),
            "event".into(),
            "rm".into(),
            token,
        ],
        &p,
    );
    assert_eq!(rc, 1);
    assert!(conn.sent().is_empty());
    assert_eq!(
        out,
        "{\"error_code\":\"INTERNAL_ERROR\",\"message\":\"'utf-8' codec can't encode characters in position 58-59: surrogates not allowed\",\"ok\":false}\n"
    );
}

#[test]
fn argv_single_surrogate_uses_code_point_text() {
    let (p, conn) = platform(&[], vec![FakeConn::stream(vec![])]);
    let token = non_utf8_os_string(&[0xff]);
    let (rc, out, _) = run_cli(
        vec![
            OsString::from(SOCKET_ARG),
            "event".into(),
            "rm".into(),
            token,
        ],
        &p,
    );
    assert_eq!(rc, 1);
    assert!(conn.sent().is_empty());
    assert_eq!(
        out,
        "{\"error_code\":\"INTERNAL_ERROR\",\"message\":\"'utf-8' codec can't encode character '\\\\udcff' in position 58: surrogates not allowed\",\"ok\":false}\n"
    );
}

#[test]
fn argv_three_surrogate_run_uses_range() {
    // `\xed\xa0\x80` 解碼為三個 lone surrogate（U+DCED、U+DCA0、U+DC80）→ 區間 58-60（Python 實測）。
    let (p, conn) = platform(&[], vec![FakeConn::stream(vec![])]);
    let token = non_utf8_os_string(&[0xed, 0xa0, 0x80]);
    let (rc, out, _) = run_cli(
        vec![
            OsString::from(SOCKET_ARG),
            "event".into(),
            "rm".into(),
            token,
        ],
        &p,
    );
    assert_eq!(rc, 1);
    assert!(conn.sent().is_empty());
    assert!(
        out.contains("can't encode characters in position 58-60"),
        "out={out}"
    );
}

/// stdout 寫入失敗（Python `_print` 拋例外）：改印 INTERNAL_ERROR、stderr 只出現該行（略過版本警告）、回 1。
struct BrokenPipeWriter;

impl Write for BrokenPipeWriter {
    fn write(&mut self, _buf: &[u8]) -> io::Result<usize> {
        Err(io::Error::from_raw_os_error(32))
    }

    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}

#[test]
fn stdout_write_failure_follows_python_except_path() {
    let padded = format!(
        "{{\"ok\":true,\"version\":\"other\",\"rules\":[\"{}\"]}}\n",
        "x".repeat(8000)
    );
    let (p, _) = platform(
        &[],
        vec![FakeConn::stream(vec![StreamStep::Reply(
            padded.into_bytes(),
        )])],
    );
    let mut out = BrokenPipeWriter;
    let mut err = Vec::new();
    let argv = vec![
        OsString::from("serialwrap"),
        OsString::from(SOCKET_ARG),
        OsString::from("event"),
        OsString::from("list"),
    ];
    let rc = sw_cli::run(argv, &p, &mut out, &mut err);
    assert_eq!(rc, 1);
    assert_eq!(
        String::from_utf8(err).unwrap(),
        "serialwrap: event.rule_list failed: INTERNAL_ERROR: [Errno 32] Broken pipe\n"
    );
}
