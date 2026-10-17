//! P4-T2：`RegistryView`（SERIALCOMM 登錄讀取）與藍牙排除純函式的紅燈測試。
//!
//! 執行：`cargo test -p sw-pal --features test-fakes --test registry_view`。
//! 純函式案例跨平台執行；原生登錄案例只在 Windows 編譯（`win-test.sh` 原生執行）。
use std::collections::HashSet;
use sw_pal::device_source::windows_model::{exclude_bluetooth, SERIALCOMM_KEY};
#[cfg(unix)]
use sw_pal::device_source::windows_model::{read_bt_ports, NullRegistryView, RegistryView};
#[cfg(unix)]
use sw_pal::traits::PalErrorKind;

#[test]
fn exclude_bluetooth_matches_python_rules_and_keeps_order() {
    let sc = vec![
        ("\\Device\\Serial0".to_string(), "COM3".to_string()),
        ("\\Device\\BthModem0".to_string(), "COM4".to_string()),
        ("\\Device\\VCP0".to_string(), "COM10".to_string()),
        ("\\Device\\VCP1".to_string(), "COM7".to_string()),
    ];
    let bt: HashSet<String> = ["COM7".to_string()].into();
    let ex: HashSet<String> = ["COM10".to_string()].into();
    assert_eq!(exclude_bluetooth(&sc, &bt, &ex), vec!["COM3".to_string()]);
}

#[cfg(unix)]
#[test]
fn registry_view_posix_unsupported() {
    let err = NullRegistryView
        .enum_values(SERIALCOMM_KEY)
        .expect_err("POSIX 無登錄，應回 Err(Unsupported)");
    assert_eq!(err.kind, PalErrorKind::Unsupported);
    assert!(read_bt_ports(&NullRegistryView).is_empty());
}

#[cfg(windows)]
#[test]
fn registry_view_windows_serialcomm_readable() {
    use sw_pal::device_source::windows_model::RegistryView;
    use sw_pal::device_source::windows_registry::WinRegistryView;

    // SERIALCOMM 不存在時回空集合，存在時每筆 COM 名以 `COM` 開頭。
    let rows = WinRegistryView
        .enum_values(SERIALCOMM_KEY)
        .expect("SERIALCOMM 應可讀取（可為空）");
    for (_device_path, com) in rows {
        assert!(com.starts_with("COM"), "{com}");
    }
}
