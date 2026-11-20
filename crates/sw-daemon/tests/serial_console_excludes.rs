//! P6 Task 10（07 D-12）：`windows.serial_console` 的兩個埠併入 `DeviceSelection.exclude_coms`。
#![cfg(feature = "test-fakes")]
use std::sync::{Arc, Mutex};

use sw_daemon::daemon_main::serial_console_excludes;
use sw_pal::fakes::platform::{posix_caps, windows_caps};
use sw_proto::config_types::RuntimeConfig;

fn run(text: &str, caps: sw_pal::Capabilities) -> (Vec<String>, Vec<String>) {
    let cfg = RuntimeConfig::from_text(Some(text)).unwrap();
    let warns = Arc::new(Mutex::new(Vec::new()));
    let sink = warns.clone();
    let out = serial_console_excludes(&cfg, &caps, &move |m: String| sink.lock().unwrap().push(m));
    let w = warns.lock().unwrap().clone();
    (out, w)
}

#[test]
fn serial_console_ports_excluded() {
    let (out, warns) = run(
        "windows:\n  serial_console: {COM5: {console_port: COM40, peer_port: COM41}}\n",
        windows_caps(),
    );
    assert_eq!(out, ["COM40", "COM41"]);
    assert!(warns.is_empty());
}

#[test]
fn serial_console_bad_shape_ignored() {
    let (out, warns) = run("windows:\n  serial_console: [1, 2]\n", windows_caps());
    assert!(out.is_empty());
    assert_eq!(warns.len(), 1, "{warns:?}");
}

#[test]
fn serial_console_ignored_on_posix() {
    let (out, warns) = run(
        "windows:\n  serial_console: {COM5: {console_port: COM40, peer_port: COM41}}\n",
        posix_caps(),
    );
    assert!(out.is_empty());
    assert_eq!(warns, ["windows.serial_console 在此平台不支援，已忽略"]);
}
