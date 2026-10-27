//! P6 Task 10（07 D-12，SC-29 Rust-only）：`config.yaml` 的 `windows.serial_console` 讀取。
use sw_proto::config_types::{RuntimeConfig, SerialConsolePair};

fn cfg(text: &str) -> RuntimeConfig {
    RuntimeConfig::from_text(Some(text)).expect("YAML 合法")
}

#[test]
fn serial_console_absent_is_none() {
    assert!(cfg("windows: {exclude_coms: [COM1]}\n").windows_serial_console().is_none());
    assert!(cfg("supervision_mode: on-demand\n").windows_serial_console().is_none());
}

#[test]
fn serial_console_mapping_parsed_in_order() {
    let c = cfg("windows:\n  serial_console:\n    COM5: {console_port: COM40, peer_port: COM41}\n    COM6: {console_port: COM42, peer_port: COM43}\n");
    let map = c.windows_serial_console().expect("有鍵").expect("形狀正確");
    let rows: Vec<(String, SerialConsolePair)> = map.0.into_iter().collect();
    assert_eq!(
        rows,
        vec![
            ("COM5".to_string(), SerialConsolePair { console_port: "COM40".into(), peer_port: "COM41".into() }),
            ("COM6".to_string(), SerialConsolePair { console_port: "COM42".into(), peer_port: "COM43".into() }),
        ]
    );
}

#[test]
fn serial_console_bad_shape_is_err() {
    for text in [
        "windows:\n  serial_console: [1, 2]\n",
        "windows:\n  serial_console: {COM5: COM40}\n",
        "windows:\n  serial_console: {COM5: {console_port: COM40}}\n",
        "windows:\n  serial_console: {COM5: {console_port: [x], peer_port: COM41}}\n",
    ] {
        assert_eq!(cfg(text).windows_serial_console(), Some(Err(())), "{text}");
    }
}
