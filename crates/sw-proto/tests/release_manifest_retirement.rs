//! Python daemon 退役切換（P9-T16）：release manifest 退役守門的 Rust 測試（紅燈）。
//!
//! 夾具位於 `tests/fixtures/`（arch §10.1 形狀；Rust daemon 只有 `launcher`，無 entry／runtime／package）。
use sw_proto::release_manifest::{validate_retirement, ManifestError, ReleaseManifest};

fn load(name: &str) -> Result<ReleaseManifest, ManifestError> {
    let text = std::fs::read_to_string(format!("{}/tests/fixtures/{name}", env!("CARGO_MANIFEST_DIR"))).unwrap();
    ReleaseManifest::parse(&text)
}

#[test]
fn retired_manifest_requires_rust_daemon() {
    assert!(matches!(load("stage3_retired_python_daemon.json"), Err(ManifestError::RetirementInconsistent(_))));
}

#[test]
fn stage3_without_marker_is_invalid() {
    assert!(matches!(load("stage3_not_retired.json"), Err(ManifestError::RetirementInconsistent(_))));
}

#[test]
fn retired_rust_only_manifest_is_valid_without_delegate() {
    let m = load("stage3_retired_rust.json").unwrap();
    assert!(validate_retirement(&m).is_ok());
    assert!(m.python_daemon_retired());
    assert!(m.python_cli_delegate_entry.is_none());
}

#[test]
fn unretired_manifest_still_requires_delegate_entry() {
    assert!(matches!(load("stage1b_missing_delegate.json"), Err(ManifestError::Schema(_))));   // P3 既有行為不變
}
