//! P4 案例綁定占位（P4-T1）：`tests/acceptance/p4_case_map.yaml` 綁定的承接節點中，測試作者尚未交付正式測試者。
//!
//! 每個占位以 `#[ignore]` 標示、主體 `unimplemented!`，讓 `cargo test -- --list` 與靜態存在性檢查
//! （`tests/test_rust_p4_case_map.py`）可以定位節點。對應 Task 的實作者必須以正式測試取代本檔案中的占位，
//! 並同步更新 `p4_case_map.yaml`。占位不得計為通過證據：`scripts/rust/check_p4_cases.py` 對只綁占位節點的案例直接 FAIL。

#[test]
#[ignore = "P4-T10 占位：sc12-reconcile-wait-old-daemon 的正式測試尚未交付"]
fn sc12_reconcile_wait_old_daemon() {
    unimplemented!("P4-T10: sc12-reconcile-wait-old-daemon");
}

#[test]
#[ignore = "P4-T7 占位：sc18-windows-ondemand-autostart 的正式測試尚未交付"]
fn sc18_windows_ondemand_autostart() {
    unimplemented!("P4-T7: sc18-windows-ondemand-autostart");
}

#[test]
#[ignore = "P4-T7 占位：sc20-daemon-restart 的正式測試尚未交付"]
fn sc20_daemon_restart() {
    unimplemented!("P4-T7: sc20-daemon-restart");
}

#[test]
#[ignore = "P4-T7 占位：df05-daemon-locate-order 的正式測試尚未交付"]
fn df05_daemon_locate_order() {
    unimplemented!("P4-T7: df05-daemon-locate-order");
}

#[test]
#[ignore = "P4-T10 占位：df07-bad-config-yaml 的正式測試尚未交付"]
fn df07_bad_config_yaml() {
    unimplemented!("P4-T10: df07-bad-config-yaml");
}

#[test]
#[ignore = "P4-T10 占位：df10-system-scope-state-path 的正式測試尚未交付"]
fn df10_system_scope_state_path() {
    unimplemented!("P4-T10: df10-system-scope-state-path");
}

#[test]
#[ignore = "P4-T12 占位：df12-setup-ran-field 的正式測試尚未交付"]
fn df12_setup_ran_field() {
    unimplemented!("P4-T12: df12-setup-ran-field");
}

#[test]
#[ignore = "P4-T14 占位：df13-minicom-broker-rename 的正式測試尚未交付"]
fn df13_minicom_broker_rename() {
    unimplemented!("P4-T14: df13-minicom-broker-rename");
}

#[test]
#[ignore = "P4-T12 占位：sc22-supervision-mode-set 的正式測試尚未交付"]
fn sc22_supervision_mode_set() {
    unimplemented!("P4-T12: sc22-supervision-mode-set");
}

#[test]
#[ignore = "P4-T12 占位：sc23-setup-rollback 的正式測試尚未交付"]
fn sc23_setup_rollback() {
    unimplemented!("P4-T12: sc23-setup-rollback");
}

#[test]
#[ignore = "P4-T11 占位：sc25-release-manifest-version-pairing 的正式測試尚未交付"]
fn sc25_release_manifest_version_pairing() {
    unimplemented!("P4-T11: sc25-release-manifest-version-pairing");
}

#[test]
#[ignore = "P4-T10 占位：df16-windows-skill-copy 的正式測試尚未交付"]
fn df16_windows_skill_copy() {
    unimplemented!("P4-T10: df16-windows-skill-copy");
}
