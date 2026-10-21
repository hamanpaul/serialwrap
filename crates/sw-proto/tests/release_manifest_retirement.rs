//! Python daemon 退役切換（P9-T16）：release manifest 退役守門的 Rust 測試（紅燈）。
//!
//! 夾具位於 `tests/fixtures/`（arch §10.1 形狀；Rust daemon 只有 `launcher`，無 entry／runtime／package）。
use sw_proto::release_manifest::{validate_retirement, ManifestError, ReleaseManifest};

fn load(name: &str) -> Result<ReleaseManifest, ManifestError> {
    let text = match name {
        "stage3_retired_python_daemon.json" => {
            include_str!("fixtures/stage3_retired_python_daemon.json")
        }
        "stage3_retired_but_runtime_injected.json" => {
            include_str!("fixtures/stage3_retired_but_runtime_injected.json")
        }
        "stage3_not_retired.json" => include_str!("fixtures/stage3_not_retired.json"),
        "stage3_retired_rust.json" => include_str!("fixtures/stage3_retired_rust.json"),
        "stage1b_missing_delegate.json" => {
            include_str!("fixtures/stage1b_missing_delegate.json")
        }
        _ => panic!("未知 manifest fixture: {name}"),
    };
    ReleaseManifest::parse(&text)
}

#[test]
fn retired_manifest_requires_rust_daemon() {
    assert!(matches!(
        load("stage3_retired_python_daemon.json"),
        Err(ManifestError::RetirementInconsistent(_))
    ));
}

#[test]
fn retired_manifest_rejects_python_runtime_field() {
    assert!(matches!(
        load("stage3_retired_but_runtime_injected.json"),
        Err(ManifestError::RetirementInconsistent(_))
    ));
}

#[test]
fn stage3_without_marker_is_invalid() {
    assert!(matches!(
        load("stage3_not_retired.json"),
        Err(ManifestError::RetirementInconsistent(_))
    ));
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
    assert!(matches!(
        load("stage1b_missing_delegate.json"),
        Err(ManifestError::Schema(_))
    )); // P3 既有行為不變
}

#[test]
fn schema_version_integer_and_decimal_forms_are_equivalent() {
    let text = include_str!("fixtures/stage3_retired_rust.json")
        .replace("\"schema_version\": 1,", "\"schema_version\": 1.0,");
    assert!(ReleaseManifest::parse(&text).is_ok());
}
