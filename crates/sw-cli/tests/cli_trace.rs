//! CLI RPC trace 契約測試（P3-T11）：format_trace_line 與 cli_trace.tsv golden 驗證。
mod common;

use sw_cli::cli_trace::{
    describe_trace_endpoint, format_trace_line, parse_log_level, resolve_cli_trace_level,
    trace_enabled, TraceMeta,
};
use sw_proto::pydict;
use sw_proto::pyjson::PyInt;

struct Row {
    id: String,
    op: String,
    input: String,
    expected: String,
}

fn unhex(s: &str) -> Vec<u8> {
    (0..s.len())
        .step_by(2)
        .map(|i| u8::from_str_radix(&s[i..i + 2], 16).expect("invalid hex"))
        .collect()
}

/// 每個操作的最少列數下限：golden 缺列或被截斷時，迭代為空不得因此 PASS（規格 §11.3「證據不足不得 PASS」）。
///
/// 下限取計畫 T11 的列數（`parse_log_level` ≥60、`describe_trace_endpoint` ≥30）；
/// `resolve_cli_trace_level`（≥80）與 `trace_enabled`（≥15）為審查提議值，皆低於目前列數（88／21）。
fn assert_row_floor(rows: &[Row], op: &str, floor: usize) {
    let n = rows.iter().filter(|r| r.op == op).count();
    assert!(n >= floor, "{op} 列數 {n} 低於下限 {floor}（golden 可能缺失或被截斷）");
}

fn load_golden() -> Vec<Row> {
    let root = common::data_root();
    let path = root.join("contracts/golden/cli_trace.tsv");
    let content = std::fs::read_to_string(&path)
        .unwrap_or_else(|e| panic!("無法讀取 {}: {e}", path.display()));
    content
        .lines()
        .filter(|line| !line.is_empty())
        .map(|line| {
            let parts: Vec<&str> = line.split('\t').collect();
            assert_eq!(parts.len(), 4, "TSV 欄位數必須為 4: {line}");
            Row {
                id: parts[0].to_string(),
                op: parts[1].to_string(),
                input: String::from_utf8(unhex(parts[2])).expect("input UTF-8"),
                expected: String::from_utf8(unhex(parts[3])).expect("expected UTF-8"),
            }
        })
        .collect()
}

// ==========================================
// 1. 單元測試：format_trace_line 精確比對
// ==========================================

#[test]
fn trace_line_exact() {
    let resp = pydict! {
        "ok" => false,
        "error_code" => "SOCKET_ERROR",
        "message" => "x",
    };
    let meta = TraceMeta {
        elapsed_ms: 500,
        retry_count: 1,
        errno: Some(2),
        errno_name: Some("ENOENT".into()),
    };
    assert_eq!(
        format_trace_line("/nonexistent/x.sock", "--socket", "session.list", 5.0, &resp, &meta),
        r#"{"endpoint_transport":"unix","endpoint_id":"534a413975f6a6393e9cc37ef545b4492fedd7cab94f4359d7949e0b54f82ec2","endpoint_source":"--socket","method":"session.list","elapsed_ms":500,"error_code":"SOCKET_ERROR","errno":2,"errno_name":"ENOENT","retry_count":1,"timeout_s":5.0}"#
    );
}

#[test]
fn trace_line_ok_response() {
    let resp = pydict! {
        "ok" => true,
    };
    let meta = TraceMeta {
        elapsed_ms: 12,
        retry_count: 0,
        errno: None,
        errno_name: None,
    };
    assert_eq!(
        format_trace_line("/nonexistent/x.sock", "--socket", "session.list", 5.0, &resp, &meta),
        r#"{"endpoint_transport":"unix","endpoint_id":"534a413975f6a6393e9cc37ef545b4492fedd7cab94f4359d7949e0b54f82ec2","endpoint_source":"--socket","method":"session.list","elapsed_ms":12,"error_code":null,"errno":null,"errno_name":null,"retry_count":0,"timeout_s":5.0}"#
    );
}

// ==========================================
// 2. cli_trace.tsv golden 驗證
// ==========================================

#[test]
fn test_cli_trace_parse_log_level() {
    let rows = load_golden();
    assert_row_floor(&rows, "parse_log_level", 60);
    let mut fails = Vec::new();
    for r in rows.into_iter().filter(|r| r.op == "parse_log_level") {
        let raw = if r.input == "__NONE__" {
            None
        } else {
            Some(r.input.strip_prefix("str:").expect("input prefix str:"))
        };
        let got = parse_log_level(raw);
        let got_str = match got {
            None => "none".to_string(),
            Some(PyInt::Small(n)) => format!("some:{n}"),
            Some(PyInt::Big(s)) => format!("some:{s}"),
        };
        if got_str != r.expected {
            fails.push(format!("{}: raw={:?} got={} want={}", r.id, raw, got_str, r.expected));
        }
    }
    assert!(fails.is_empty(), "parse_log_level 失敗：\n{}", fails.join("\n"));
}

#[test]
fn test_cli_trace_resolve_cli_trace_level() {
    let rows = load_golden();
    assert_row_floor(&rows, "resolve_cli_trace_level", 80);
    let mut fails = Vec::new();
    for r in rows.into_iter().filter(|r| r.op == "resolve_cli_trace_level") {
        let (verbose_str, env_str) = r.input.split_once('\x1f').expect("split verbose and env");
        let verbose: i64 = verbose_str.parse().expect("parse verbose int");
        let env_level = if env_str == "__NONE__" {
            None
        } else {
            Some(env_str)
        };
        let got = resolve_cli_trace_level(verbose, env_level);
        let got_str = match got {
            PyInt::Small(n) => n.to_string(),
            PyInt::Big(s) => s,
        };
        if got_str != r.expected {
            fails.push(format!(
                "{}: verbose={} env={:?} got={} want={}",
                r.id, verbose, env_level, got_str, r.expected
            ));
        }
    }
    assert!(fails.is_empty(), "resolve_cli_trace_level 失敗：\n{}", fails.join("\n"));
}

#[test]
fn test_cli_trace_trace_enabled() {
    let rows = load_golden();
    assert_row_floor(&rows, "trace_enabled", 15);
    let mut fails = Vec::new();
    for r in rows.into_iter().filter(|r| r.op == "trace_enabled") {
        let level = if let Ok(n) = r.input.parse::<i128>() {
            PyInt::Small(n)
        } else {
            PyInt::Big(r.input.clone())
        };
        let got = trace_enabled(&level);
        let want = r.expected == "true";
        if got != want {
            fails.push(format!("{}: level={} got={} want={}", r.id, r.input, got, want));
        }
    }
    assert!(fails.is_empty(), "trace_enabled 失敗：\n{}", fails.join("\n"));
}

#[test]
fn test_cli_trace_describe_trace_endpoint() {
    let rows = load_golden();
    assert_row_floor(&rows, "describe_trace_endpoint", 30);
    let mut fails = Vec::new();
    for r in rows.into_iter().filter(|r| r.op == "describe_trace_endpoint") {
        let (got_transport, got_id) = describe_trace_endpoint(&r.input);
        let (want_transport, want_id) = r.expected.split_once('\x1f').expect("split transport and id");
        if got_transport != want_transport || got_id != want_id {
            fails.push(format!(
                "{}: ep={:?} got=({}, {}) want=({}, {})",
                r.id, r.input, got_transport, got_id, want_transport, want_id
            ));
        }
    }
    assert!(fails.is_empty(), "describe_trace_endpoint 失敗：\n{}", fails.join("\n"));
}
