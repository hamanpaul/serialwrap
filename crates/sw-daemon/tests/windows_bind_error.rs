//! Windows loopback bind 失敗診斷提示測試。

use sw_daemon::daemon_main::rpc_bind_error_with_windows_hint;
use sw_pal::traits::EndpointSpec;
use sw_pal::BackendKind;

#[test]
fn windows_loopback_bind_failure_has_actionable_reserved_port_hint() {
    let endpoint = EndpointSpec::Tcp {
        host: "127.0.0.1".to_string(),
        port: 48_700,
    };
    let message = rpc_bind_error_with_windows_hint("bind failed", &endpoint, BackendKind::Win);
    assert!(message.contains("bind failed"));
    assert!(message.contains("netsh int ipv4 show excludedportrange protocol=tcp"));
    assert!(message.contains("config.yaml 的 socket_path"));
    assert!(message.contains("SERIALWRAP_TCP_PORT"));
    assert!(message.contains("daemon 執行期不會自動換埠"));
}
