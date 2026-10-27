//! RpcListener conformance（arch §4.6、計畫 Task 17）：同一套斷言跑在任何 `RpcListenerFactory`＋`RpcConnector` 組合上。

use std::io::{Read, Write};
use std::path::Path;
use std::sync::Arc;
use std::time::{Duration, Instant};

use crate::traits::{
    EndpointSpec, ListenOptions, RpcConnector, RpcListener, RpcListenerFactory, RpcServerStream,
    SockTimeout,
};

const BASELINE: ListenOptions = ListenOptions {
    mode: 0o660,
    group: None,
    ensure_dir_mode: None,
};

/// TCP loopback 拒絕訊息（逐字照 `sw_core/rpc_win.py::_parse_tcp`）。
pub const NON_LOOPBACK_MESSAGE_0000: &str = "RPC server 僅允許 loopback 位址（127.0.0.1/localhost/::1），拒絕 '0.0.0.0' 避免遠端暴露（RPC 無認證，非 loopback 會讓遠端呼叫所有命令）";

fn accept_within(listener: &mut dyn RpcListener, total: Duration) -> Box<dyn RpcServerStream> {
    let deadline = Instant::now() + total;
    loop {
        if let Some(s) = listener.accept(Duration::from_millis(50)).expect("accept") {
            return s;
        }
        assert!(Instant::now() < deadline, "accept 逾時");
    }
}

fn roundtrip(listener: &mut dyn RpcListener, connector: &dyn RpcConnector, ep: &EndpointSpec) {
    // 尚無連線：accept 有界等待回 None。
    let t0 = Instant::now();
    assert!(listener.accept(Duration::from_millis(50)).expect("accept").is_none());
    assert!(t0.elapsed() < Duration::from_secs(2), "accept 未遵守有界等待");
    let mut client = connector
        .connect(ep, SockTimeout::Blocking(Duration::from_secs(5)))
        .expect("connect");
    let mut server = accept_within(listener, Duration::from_secs(5));
    client.send_all(b"{\"id\":1}\n").expect("send");
    server
        .set_read_timeout(Some(Duration::from_secs(5)))
        .expect("set_read_timeout");
    let mut got = Vec::new();
    let mut buf = [0u8; 64];
    while !got.ends_with(b"\n") {
        let n = server.read(&mut buf).expect("server read");
        assert!(n > 0, "server 端提早 EOF");
        got.extend_from_slice(&buf[..n]);
    }
    assert_eq!(got, b"{\"id\":1}\n");
    server.write_all(b"{\"ok\":true}\n").expect("server write");
    server.flush().expect("flush");
    let mut back = Vec::new();
    while !back.ends_with(b"\n") {
        let n = client.recv(&mut buf).expect("client recv");
        assert!(n > 0, "client 端提早 EOF");
        back.extend_from_slice(&buf[..n]);
    }
    assert_eq!(back, b"{\"ok\":true}\n");
    // shutdown_read 後 server 讀回 EOF。
    server.shutdown_read().expect("shutdown_read");
    assert_eq!(server.read(&mut buf).expect("read after shutdown"), 0);
}

fn free_tcp_port() -> u16 {
    std::net::TcpListener::bind("127.0.0.1:0")
        .and_then(|l| l.local_addr())
        .map(|a| a.port())
        .expect("free port")
}

/// listener 共用斷言：bind／accept 有界等待、close 移除 socket 檔、TCP loopback 限制（Task 17）。
pub fn rpc_suite(listeners: Arc<dyn RpcListenerFactory>, connector: Arc<dyn RpcConnector>, dir: &Path) {
    // TCP：非 loopback 拒絕（逐字訊息）。
    let refused = listeners.bind(
        &EndpointSpec::Tcp {
            host: "0.0.0.0".into(),
            port: 0,
        },
        &BASELINE,
    );
    match refused {
        Ok(_) => panic!("非 loopback host 不得 bind 成功"),
        Err(e) => assert_eq!(e.py_message, NON_LOOPBACK_MESSAGE_0000),
    }
    // TCP loopback：bind、往返一行。
    let ep = EndpointSpec::Tcp {
        host: "127.0.0.1".into(),
        port: free_tcp_port(),
    };
    let mut tcp = listeners.bind(&ep, &BASELINE).expect("bind tcp loopback");
    roundtrip(tcp.as_mut(), connector.as_ref(), &ep);
    tcp.close();

    if !connector.af_unix_available() {
        return;
    }
    // AF_UNIX：既有一般檔被 unlink 後 bind、往返一行、close 移除 socket 檔。
    let path = dir.join("rpc_suite.sock");
    std::fs::write(&path, b"stale").expect("建立既有檔");
    let ep = EndpointSpec::Unix(path.clone().into_os_string());
    let mut uds = listeners.bind(&ep, &BASELINE).expect("bind uds");
    assert!(path.exists(), "bind 後 socket 檔應存在");
    roundtrip(uds.as_mut(), connector.as_ref(), &ep);
    uds.close();
    assert!(!path.exists(), "close 後 socket 檔應被移除");
}
