mod common;

use sw_proto::endpoint::{is_loopback_tcp, parse_endpoint, Endpoint, EndpointError};

#[test]
fn test_endpoint_golden() {
    let mut fails = Vec::new();
    for r in common::golden("endpoint.tsv") {
        let got = match parse_endpoint(&r.input) {
            Ok(Endpoint::Unix(p)) => format!("ok:unix:{p}"),
            Ok(Endpoint::Tcp { host, port }) => format!("ok:tcp:{host}:{port}"),
            Err(EndpointError(msg)) => format!("err:{msg}"),
        };
        if got != r.expected {
            fails.push(format!("{}: {}\n got  {got}\n want {}", r.id, r.input, r.expected));
        }
    }
    assert!(fails.is_empty(), "{}", fails.join("\n"));
}

#[test]
fn test_is_loopback_tcp() {
    assert!(is_loopback_tcp("tcp://127.0.0.1:48700"));
    assert!(is_loopback_tcp("tcp://localhost:48700"));
    assert!(is_loopback_tcp("tcp://LOCALHOST:48700"));
    assert!(is_loopback_tcp("tcp://[::1]:48700"));
    assert!(!is_loopback_tcp("tcp://192.168.1.1:48700"));
    assert!(!is_loopback_tcp("tcp://example.com:48700"));
    assert!(!is_loopback_tcp("/path/to/sock"));
    assert!(!is_loopback_tcp("unix:///path/to/sock"));
}
