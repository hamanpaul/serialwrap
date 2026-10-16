//! RPC client（白名單重試、退避、TIMEOUT 探測）測試：P3-T13 紅燈階段。
use sw_cli::client::{rpc_call, ClientCrash};
use sw_pal::fakes::{fake_platform, FakeConn, StreamStep::*};
use sw_proto::pyjson::{PyInt, PyValue};

fn empty() -> PyValue {
    PyValue::Dict(Default::default())
}

#[test]
fn whitelist_method_retries_socket_error_with_backoff() {
    let (p, conn, clock) = fake_platform(vec![
        FakeConn::refuse_errno(111, "[Errno 111] Connection refused"),
        FakeConn::refuse_errno(111, "[Errno 111] Connection refused"),
        FakeConn::stream(vec![Reply(br#"{"ok":true,"sessions":[]}"#.to_vec())]),
    ]);
    let out = rpc_call(&p, "/s.sock", "session.list", &empty(), 1, 5.0, &PyInt::Small(3)).unwrap();
    assert_eq!(
        sw_proto::pyjson::dumps(&out.resp.clone().into(), &sw_proto::pyjson::RPC_WIRE),
        r#"{"ok":true,"sessions":[]}"#
    );
    assert_eq!(clock.sleeps_s(), vec![0.5, 1.0]);
    assert_eq!(out.meta.retry_count, 2);
    assert_eq!(out.meta.errno, None);
    assert_eq!(
        conn.sent(),
        vec![b"{\"id\":1,\"method\":\"session.list\",\"params\":{}}\n".to_vec()]
    );
    assert_eq!(conn.connect_count(), 3);
}

#[test]
fn non_whitelist_never_retries() {
    // Method::known() 扣掉 11 個白名單＝44 個方法（含 daemon.handoff_stop）× 三種 transient 失敗；每種都只能 connect 一次
    let wl = sw_proto::constants::RETRYABLE_READONLY_METHODS;
    let methods: Vec<&str> = sw_proto::rpc_types::Method::known()
        .iter()
        .copied()
        .filter(|m| !wl.contains(m))
        .collect();
    assert_eq!(methods.len(), 44);
    for m in methods {
        for (kind, conn_step) in [
            (
                "refuse",
                FakeConn::refuse_errno(111, "[Errno 111] Connection refused"),
            ),
            ("timeout", FakeConn::Timeout),
            ("eof", FakeConn::stream(vec![Eof])),
        ] {
            // TIMEOUT 會觸發探測（health.ping 被拒後即停），所以腳本多給一條探測連線
            let (p, conn, clock) = fake_platform(vec![
                conn_step.clone(),
                FakeConn::refuse_errno(111, "x"),
            ]);
            let _ = rpc_call(&p, "/s.sock", m, &empty(), 1, 5.0, &PyInt::Small(3)).unwrap();
            let want_connects = if kind == "timeout" { 2 } else { 1 }; // 主呼叫 1 次＋（只在 TIMEOUT 時）ping 1 次
            assert_eq!(conn.connect_count(), want_connects, "{m} {kind}");
            assert!(
                conn.sent().iter().all(|b| !b.starts_with(br#"{"id":0"#)
                    || b.starts_with(br#"{"id":0,"method":"health.ping""#)),
                "{m} {kind}: 探測以外不得有額外請求"
            );
            assert!(
                conn.sent()
                    .iter()
                    .filter(|b| b.starts_with(br#"{"id":1,"#))
                    .count()
                    <= 1,
                "{m} {kind}"
            );
            assert!(clock.sleeps_s().is_empty(), "{m} {kind}");
        }
    }
}

#[test]
fn probe_only_sends_health_methods() {
    let (p, conn, _c) = fake_platform(vec![
        FakeConn::Timeout,
        FakeConn::stream(vec![Reply(br#"{"ok":true}"#.to_vec())]),
        FakeConn::stream(vec![Reply(br#"{"ok":true}"#.to_vec())]),
    ]);
    let _ = rpc_call(&p, "/s.sock", "command.submit", &empty(), 1, 5.0, &PyInt::Small(3)).unwrap();
    let sent = conn.sent();
    assert!(sent.iter().all(|b| {
        b.starts_with(br#"{"id":0,"method":"health.ping""#)
            || b.starts_with(br#"{"id":0,"method":"health.status""#)
    }));
}

#[test]
fn backoff_caps_at_5s() {
    let (p, _c, clock) = fake_platform(vec![FakeConn::Timeout; 7]);
    let out = rpc_call(&p, "/s.sock", "health.status", &empty(), 1, 5.0, &PyInt::Small(6)).unwrap();
    assert_eq!(clock.sleeps_s(), vec![0.5, 1.0, 2.0, 4.0, 5.0, 5.0]);
    assert_eq!(out.meta.retry_count, 6);
    assert!(out.resp.get("daemon_reachable").is_none()); // 探測方法自身不補探測
}

#[test]
fn timeout_enrich_reachable_busy_int_only() {
    let (p, conn, _clock) = fake_platform(vec![
        FakeConn::Timeout,
        FakeConn::stream(vec![Reply(br#"{"ok":true,"pong":true}"#.to_vec())]),
        FakeConn::stream(vec![Reply(
            br#"{"ok":true,"commands":3,"sessions":"x"}"#.to_vec(),
        )]),
    ]);
    let out = rpc_call(&p, "/s.sock", "device.list", &empty(), 1, 5.0, &PyInt::Small(0)).unwrap();
    assert_eq!(out.resp.get("daemon_reachable"), Some(&PyValue::Bool(true)));
    assert_eq!(
        sw_proto::pyjson::dumps(out.resp.get("daemon_busy").unwrap(), &sw_proto::pyjson::RPC_WIRE),
        r#"{"commands":3}"#
    );
    let sent = conn.sent();
    assert!(sent[0].starts_with(br#"{"id":0,"method":"health.ping","params":{}}"#));
    assert!(sent[1].starts_with(br#"{"id":0,"method":"health.status","params":{}}"#));
    assert_eq!(conn.timeouts_s(), vec![5.0, 1.0, 1.0]);
}

#[test]
fn invalid_timeout_crashes_only_after_endpoint_parse() {
    let (p, _c, _k) = fake_platform(vec![]);
    let bad = rpc_call(&p, "tcp://h:1/", "device.list", &empty(), 1, -1.0, &PyInt::Small(0)).unwrap();
    assert_eq!(bad.resp.get("error_code"), Some(&"INVALID_ENDPOINT".into()));
    for t in [-1.0, f64::NAN, f64::INFINITY, 1e10] {
        assert!(
            matches!(
                rpc_call(&p, "/s.sock", "device.list", &empty(), 1, t, &PyInt::Small(0)),
                Err(ClientCrash::InvalidTimeout(_))
            ),
            "{t}"
        );
    }
}

#[test]
fn tcp_resolution_precedes_timeout_validation() {
    // Q-10：create_connection 先 getaddrinfo；解析失敗 → SOCKET_ERROR（gaierror），不 crash
    let (p, conn, _k) = fake_platform(vec![]);
    conn.fail_resolve(
        "no-such-host.invalid",
        sw_pal::fakes::FakeConn::gai_error(-2, "[Errno -2] Name or service not known"),
    );
    let out = rpc_call(
        &p,
        "tcp://no-such-host.invalid:1",
        "device.list",
        &empty(),
        1,
        -1.0,
        &PyInt::Small(0),
    )
    .unwrap();
    assert_eq!(out.resp.get("error_code"), Some(&"SOCKET_ERROR".into()));
    assert!(matches!(
        rpc_call(&p, "tcp://127.0.0.1:1", "device.list", &empty(), 1, -1.0, &PyInt::Small(0)),
        Err(ClientCrash::InvalidTimeout(_))
    ));
    assert_eq!(conn.connect_count(), 0);
}

#[test]
fn negative_retries_one_attempt() {
    let (p, conn, clock) = fake_platform(vec![
        FakeConn::refuse_errno(111, "[Errno 111] Connection refused"),
    ]);
    let out = rpc_call(&p, "/s.sock", "session.list", &empty(), 1, 5.0, &PyInt::Small(-2)).unwrap();
    assert_eq!(out.resp.get("error_code"), Some(&"SOCKET_ERROR".into()));
    assert_eq!(conn.connect_count(), 1);
    assert!(clock.sleeps_s().is_empty());
    assert_eq!(out.meta.retry_count, 0);
}

#[test]
fn big_retries_saturates() {
    let (p, conn, clock) = fake_platform(vec![
        FakeConn::refuse_errno(111, "[Errno 111] Connection refused"),
        FakeConn::refuse_errno(111, "[Errno 111] Connection refused"),
        FakeConn::stream(vec![Reply(br#"{"ok":true,"sessions":[]}"#.to_vec())]),
    ]);
    let out = rpc_call(
        &p,
        "/s.sock",
        "session.list",
        &empty(),
        1,
        5.0,
        &PyInt::Big("99999999999999999999".into()),
    )
    .unwrap();
    assert_eq!(out.resp.get("ok"), Some(&PyValue::Bool(true)));
    assert_eq!(clock.sleeps_s(), vec![0.5, 1.0]);
    assert_eq!(out.meta.retry_count, 2);
    assert_eq!(conn.connect_count(), 3);
}

#[test]
fn non_transient_no_retry() {
    let (p, conn, clock) = fake_platform(vec![
        FakeConn::stream(vec![Reply(br#"{"ok":false,"error_code":"SESSION_NOT_FOUND"}"#.to_vec())]),
    ]);
    let out = rpc_call(&p, "/s.sock", "session.list", &empty(), 1, 5.0, &PyInt::Small(3)).unwrap();
    assert_eq!(out.resp.get("error_code"), Some(&"SESSION_NOT_FOUND".into()));
    assert_eq!(conn.connect_count(), 1);
    assert!(clock.sleeps_s().is_empty());
    assert_eq!(out.meta.retry_count, 0);
}

#[test]
fn invalid_response_not_transient() {
    let (p, conn, clock) = fake_platform(vec![
        FakeConn::stream(vec![Reply(b"[1]\n".to_vec())]),
    ]);
    let out = rpc_call(&p, "/s.sock", "session.list", &empty(), 1, 5.0, &PyInt::Small(3)).unwrap();
    assert_eq!(out.resp.get("error_code"), Some(&"INVALID_RESPONSE".into()));
    assert_eq!(conn.connect_count(), 1);
    assert!(clock.sleeps_s().is_empty());
    assert_eq!(out.meta.retry_count, 0);
}

#[test]
fn non_json_is_invalid_response() {
    let (p, conn, clock) = fake_platform(vec![
        FakeConn::stream(vec![Reply(b"garbage\n".to_vec())]),
    ]);
    let out = rpc_call(&p, "/s.sock", "session.list", &empty(), 1, 5.0, &PyInt::Small(3)).unwrap();
    assert_eq!(out.resp.get("error_code"), Some(&"INVALID_RESPONSE".into()));
    assert_eq!(conn.connect_count(), 1);
    assert!(clock.sleeps_s().is_empty());
    assert_eq!(out.meta.retry_count, 0);
}

#[test]
fn empty_response() {
    let (p, conn, clock) = fake_platform(vec![
        FakeConn::stream(vec![Eof]),
        FakeConn::stream(vec![Reply(br#"{"ok":true}"#.to_vec())]),
    ]);
    let out = rpc_call(&p, "/s.sock", "session.list", &empty(), 1, 5.0, &PyInt::Small(1)).unwrap();
    assert_eq!(out.resp.get("ok"), Some(&PyValue::Bool(true)));
    assert_eq!(conn.connect_count(), 2);
    assert_eq!(clock.sleeps_s(), vec![0.5]);
    assert_eq!(out.meta.retry_count, 1);
}

#[test]
fn whitespace_line_empty() {
    let (p, _conn, _clock) = fake_platform(vec![
        FakeConn::stream(vec![Reply(b"  \r\n".to_vec())]),
    ]);
    let (resp, errno) =
        sw_cli::client::rpc_call_once(&p, "/s.sock", "session.list", &empty(), 1, 5.0).unwrap();
    assert_eq!(resp.get("error_code"), Some(&"EMPTY_RESPONSE".into()));
    assert_eq!(errno, None);
}

#[test]
fn split_chunks_and_trailing() {
    let (p, _conn, _clock) = fake_platform(vec![
        FakeConn::stream(vec![
            Chunk(b"{\"ok\":".to_vec()),
            Chunk(b"true}\nJUNK".to_vec()),
        ]),
    ]);
    let (resp, errno) =
        sw_cli::client::rpc_call_once(&p, "/s.sock", "session.list", &empty(), 1, 5.0).unwrap();
    assert_eq!(resp.get("ok"), Some(&PyValue::Bool(true)));
    assert_eq!(errno, None);
}

#[test]
fn strip_vertical_tab() {
    let (p, _conn, _clock) = fake_platform(vec![
        FakeConn::stream(vec![Reply(b"\x0b{\"ok\":true}\x0c\n".to_vec())]),
    ]);
    let (resp, errno) =
        sw_cli::client::rpc_call_once(&p, "/s.sock", "session.list", &empty(), 1, 5.0).unwrap();
    assert_eq!(resp.get("ok"), Some(&PyValue::Bool(true)));
    assert_eq!(errno, None);
}

#[test]
fn utf8_replace() {
    let (p, _conn, _clock) = fake_platform(vec![
        FakeConn::stream(vec![Reply(b"{\"m\":\"\xff\"}\n".to_vec())]),
    ]);
    let (resp, errno) =
        sw_cli::client::rpc_call_once(&p, "/s.sock", "session.list", &empty(), 1, 5.0).unwrap();
    assert_eq!(resp.get("m"), Some(&"\u{FFFD}".into()));
    assert_eq!(errno, None);
}

#[test]
fn no_id_check() {
    let (p, _conn, _clock) = fake_platform(vec![
        FakeConn::stream(vec![Reply(br#"{"id":99,"ok":true}"#.to_vec())]),
    ]);
    let (resp, errno) =
        sw_cli::client::rpc_call_once(&p, "/s.sock", "session.list", &empty(), 1, 5.0).unwrap();
    assert_eq!(resp.get("id"), Some(&PyValue::Int(PyInt::Small(99))));
    assert_eq!(resp.get("ok"), Some(&PyValue::Bool(true)));
    assert_eq!(errno, None);
}

#[test]
fn etimedout_is_timeout() {
    let (p, _conn, _clock) = fake_platform(vec![
        FakeConn::Refuse(sw_pal::traits::PalError {
            kind: sw_pal::traits::PalErrorKind::TimedOut,
            os_code: Some(110),
            winerror: None,
            strerror: "Connection timed out".into(),
            py_message: "[Errno 110] Connection timed out".into(),
        }),
    ]);
    let (resp, errno) =
        sw_cli::client::rpc_call_once(&p, "/s.sock", "session.list", &empty(), 1, 5.0).unwrap();
    assert_eq!(resp.get("error_code"), Some(&"TIMEOUT".into()));
    assert_eq!(errno, None);
}

#[test]
fn recv_timeout() {
    let (p, _conn, _clock) = fake_platform(vec![
        FakeConn::stream(vec![RecvErr(sw_pal::traits::PalError {
            kind: sw_pal::traits::PalErrorKind::TimedOut,
            os_code: Some(11),
            winerror: None,
            strerror: "Resource temporarily unavailable".into(),
            py_message: "[Errno 11] Resource temporarily unavailable".into(),
        })]),
    ]);
    let (resp, errno) =
        sw_cli::client::rpc_call_once(&p, "/s.sock", "session.list", &empty(), 1, 5.0).unwrap();
    assert_eq!(resp.get("ok"), Some(&PyValue::Bool(false)));
    assert_eq!(resp.get("error_code"), Some(&"TIMEOUT".into()));
    assert_eq!(resp.get("message"), None);
    assert_eq!(errno, None);
}

#[test]
fn probe_unreachable() {
    let (p, _conn, _clock) = fake_platform(vec![
        FakeConn::Timeout,
        FakeConn::refuse_errno(111, "[Errno 111] Connection refused"),
    ]);
    let out = rpc_call(&p, "/s.sock", "device.list", &empty(), 1, 5.0, &PyInt::Small(0)).unwrap();
    assert_eq!(out.resp.get("error_code"), Some(&"TIMEOUT".into()));
    assert_eq!(out.resp.get("daemon_reachable"), Some(&PyValue::Bool(false)));
    assert_eq!(out.resp.get("daemon_busy"), None);
}

#[test]
fn probe_deadline() {
    let (p, conn, _clock) = fake_platform(vec![
        FakeConn::Timeout,
        FakeConn::stream(vec![
            Delay(std::time::Duration::from_millis(2600)),
            Reply(br#"{"ok":true,"pong":true}"#.to_vec()),
        ]),
    ]);
    let out = rpc_call(&p, "/s.sock", "device.list", &empty(), 1, 5.0, &PyInt::Small(0)).unwrap();
    assert_eq!(out.resp.get("daemon_reachable"), Some(&PyValue::Bool(true)));
    assert_eq!(out.resp.get("daemon_busy"), None);
    let sent = conn.sent();
    assert_eq!(sent.len(), 1);
    assert!(sent[0].starts_with(br#"{"id":0,"method":"health.ping""#));
}

#[test]
fn probe_bool_counts() {
    let (p, _conn, _clock) = fake_platform(vec![
        FakeConn::Timeout,
        FakeConn::stream(vec![Reply(br#"{"ok":true,"pong":true}"#.to_vec())]),
        FakeConn::stream(vec![Reply(br#"{"ok":true,"commands":true}"#.to_vec())]),
    ]);
    let out = rpc_call(&p, "/s.sock", "device.list", &empty(), 1, 5.0, &PyInt::Small(0)).unwrap();
    assert_eq!(out.resp.get("daemon_reachable"), Some(&PyValue::Bool(true)));
    assert_eq!(
        sw_proto::pyjson::dumps(out.resp.get("daemon_busy").unwrap(), &sw_proto::pyjson::RPC_WIRE),
        r#"{"commands":true}"#
    );
}

#[test]
fn errno_from_last_main_attempt() {
    let (p, _conn, _clock) = fake_platform(vec![
        FakeConn::refuse_errno(111, "[Errno 111] Connection refused"),
        FakeConn::refuse_errno(2, "[Errno 2] No such file or directory"),
        FakeConn::Timeout,
        FakeConn::refuse_errno(111, "x"),
    ]);
    let out = rpc_call(&p, "/s.sock", "session.list", &empty(), 1, 5.0, &PyInt::Small(2)).unwrap();
    assert_eq!(out.meta.retry_count, 2);
    assert_eq!(out.meta.errno, None);
}

#[test]
fn response_cap() {
    let chunk = vec![b'x'; 2048];
    let (p, _conn, _clock) = fake_platform(vec![
        FakeConn::stream(vec![Chunk(chunk)]),
    ]);
    let (resp, _errno) = sw_cli::client::rpc_call_once_capped(
        &p,
        "/s.sock",
        "session.list",
        &empty(),
        1,
        5.0,
        1024,
    )
    .unwrap();
    assert_eq!(resp.get("error_code"), Some(&"INVALID_RESPONSE".into()));
}

#[test]
fn large_response_ok() {
    let mut payload = Vec::with_capacity(20 * 1024 * 1024 + 32);
    payload.extend_from_slice(br#"{"ok":true,"data":""#);
    payload.resize(20 * 1024 * 1024, b'a');
    payload.extend_from_slice(b"\"}\n");
    let (p, _conn, _clock) = fake_platform(vec![
        FakeConn::stream(vec![Reply(payload)]),
    ]);
    let (resp, _errno) =
        sw_cli::client::rpc_call_once(&p, "/s.sock", "session.list", &empty(), 1, 5.0).unwrap();
    assert_eq!(resp.get("ok"), Some(&PyValue::Bool(true)));
}

#[test]
fn no_af_unix_message() {
    let (mut p, _conn, _clock) = fake_platform(vec![]);
    p.caps.af_unix = false;
    let (resp, errno) =
        sw_cli::client::rpc_call_once(&p, "/x.sock", "session.list", &empty(), 1, 5.0).unwrap();
    assert_eq!(resp.get("error_code"), Some(&"SOCKET_ERROR".into()));
    assert_eq!(
        resp.get("message"),
        Some(&"本平台無 AF_UNIX，無法連接 unix endpoint '/x.sock'（請改用 tcp://127.0.0.1:<port>，#131）".into())
    );
    assert_eq!(errno, None);
}

#[test]
fn elapsed_ms() {
    let (p, _conn, clock) = fake_platform(vec![
        FakeConn::refuse_errno(111, "refused"),
        FakeConn::refuse_errno(111, "refused"),
        FakeConn::stream(vec![Reply(br#"{"ok":true}"#.to_vec())]),
    ]);
    let out = rpc_call(&p, "/s.sock", "session.list", &empty(), 1, 5.0, &PyInt::Small(3)).unwrap();
    assert_eq!(out.meta.retry_count, 2);
    assert_eq!(clock.sleeps_s(), vec![0.5, 1.0]);
    assert_eq!(out.meta.elapsed_ms, 1500);
}
