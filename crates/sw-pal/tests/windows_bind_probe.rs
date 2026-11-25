//! 原生 Windows loopback TCP bind 探測測試。

#[cfg(windows)]
#[test]
fn windows_loopback_bind_probe_accepts_ephemeral_port() {
    assert!(sw_pal::rpc_transport::native_loopback_tcp_bind_available(
        "127.0.0.1",
        0,
    ));
}
